"""Network-free behavior checks for timing, sourcing and demo isolation.

Run: python -B -m unittest test_purchase_outlook -v
"""
import datetime as dt
import io
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch, MagicMock

import numpy as np
import engine as E
import connectors as C
import country_rank as CR
import live_agent
import purchase_outlook as P


def quote(value=100):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    return NS(value=value, observed_at=now, retrieved_at=now, quality="ok", source="TEST ONLY", age_days=0)


def country(origin="JPN", price=700):
    return NS(origin=origin, origin_name={"JPN":"Japan","KOR":"Korea"}[origin],
        proven_tonnes=10000, landed_usd_per_t=price, fob_usd_per_t=600,
        duty_pct=10, duty_basis="TEST", freight_usd_per_t=40,
        duty_decision_grade=False, surcharges=[], notes=[], fob_source="TEST historical CIF")


class OutlookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A declining synthetic history makes the expected timing direction
        # unambiguous; it is never claimed to represent current market prices.
        cls.prices = tuple(np.exp(np.linspace(5, 4, 180)))

    def build(self, **overrides):
        args = dict(inputs=P.validate_inputs({}), material="STEEL_HRC", destination="IND", hs6="720810",
            prices=self.prices, benchmark=100, observation=vars(quote()), series_basis="TEST PROXY",
            monthly_demand=300, usable_stock=2000, safety_stock=100, policy_weeks=4,
            ranked=[country()], fx=1)
        args.update(overrides)
        return P.build_outlook(**args)

    def test_validation_rejects_invalid_thresholds_and_country(self):
        for payload in ({"safe_value_pct":float("nan")}, {"safe_value_pct":0},
                        {"safe_value_pct":101}, {"monitoring_baseline_price":-1},
                        {"preferred_source_country":"NOPE"}, {"demo_threshold_crossed":"false"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                P.validate_inputs(payload)

    def test_demo_crosses_threshold_without_changing_forecast_or_live_price(self):
        real = self.build()
        demo = self.build(inputs=P.validate_inputs({"demo_threshold_crossed":True}))
        self.assertFalse(real["trigger"]["crossed"])
        self.assertTrue(demo["trigger"]["crossed"])
        self.assertEqual(demo["live_reference_price"], real["live_reference_price"])
        self.assertEqual([r["p50"] for r in demo["forecast"]], [r["p50"] for r in real["forecast"]])
        self.assertEqual(real["mode"], "LATEST_AVAILABLE_MARKET_DATA")

    def test_saved_baseline_detects_real_crossing(self):
        o = self.build(inputs=P.validate_inputs({"monitoring_baseline_price":90,"safe_value_pct":5}))
        self.assertTrue(o["trigger"]["crossed"])
        self.assertEqual(o["trigger"]["threshold_price"], 94.5)

    def test_waiting_with_stock_and_lower_future_price(self):
        o = self.build()
        self.assertEqual(o["timing"]["action"], "WAIT_AND_REVIEW")
        self.assertEqual(o["timing"]["buy_after_days"], 90)
        self.assertEqual([r["trading_steps"] for r in o["forecast"]], [21,42,63])
        for r in o["forecast"]:
            self.assertLessEqual(r["p10"], r["p50"])
            self.assertLessEqual(r["p50"], r["p90"])

    def test_shortage_beats_cheaper_future_price(self):
        o = self.build(usable_stock=40)
        self.assertEqual(o["timing"]["action"], "SECURE_NEAR_TERM_SUPPLY")
        self.assertEqual(o["timing"]["request_quantity_now_t"], 540)
        self.assertEqual(o["timing"]["buy_after_days"], 0)

    def test_zero_demand_does_not_create_a_purchase(self):
        o = self.build(monthly_demand=0)
        self.assertEqual(o["timing"]["request_quantity_now_t"], 0)
        self.assertEqual(o["timing"]["action"], "NO_PURCHASE_REQUIREMENT")

    def test_preference_changes_shortlist_without_inventing_supplier_quotes(self):
        o = self.build(inputs=P.validate_inputs({"preferred_source_country":"IND"}))
        self.assertEqual(len(o["top_suppliers"]), 3)
        self.assertTrue(all(s["country"] == "IND" for s in o["top_suppliers"]))
        self.assertTrue(all(s["supplier_quote"] is None for s in o["top_suppliers"]))
        self.assertTrue(all(s["quantity_discount"]["percent"] is None for s in o["top_suppliers"]))
        self.assertEqual(o["timing"]["recommended_country_for_rfq"], "IND")

    def test_empty_imports_remain_unavailable(self):
        o = self.build(ranked=[])
        self.assertIsNone(o["timing"]["lowest_import_screen_country"])
        self.assertEqual(len(o["top_suppliers"]), 3)

    def test_missing_preferred_country_price_keeps_country_visible(self):
        tariff = NS(source="TEST tariff", usable_for_decision=False)
        catalogue=P.research()
        catalogue["suppliers"]=[s for s in catalogue["suppliers"] if s["country"]!="CHN"]
        with patch('tariff.total_duty_pct', return_value=(8,tariff,[])), patch.object(P,"research",return_value=catalogue):
            o = self.build(inputs=P.validate_inputs({"preferred_source_country":"CHN"}))
        row = next(r for r in o["countries"] if r["country"] == "CHN")
        self.assertIsNone(row["screen_cost_local"])
        self.assertEqual(row["duty_pct"], 8)
        self.assertFalse(o["timing"]["preferred_country_has_researched_suppliers"])

    def test_zinc_research_and_historical_discount_are_not_current_offers(self):
        o = self.build(material="ZINC_SHG", hs6="790111", inputs=P.validate_inputs({"preferred_source_country":"IND"}))
        self.assertEqual(len(o["top_suppliers"]),3)
        hzl = o["top_suppliers"][0]
        self.assertEqual(hzl["name"],"Hindustan Zinc")
        self.assertFalse(hzl["quantity_discount"]["historical_lead"]["applied_to_cost"])

    def test_stale_observation_blocks_market_timing(self):
        obs = vars(quote())
        obs["observed_at"] = "2020-01-01T00:00:00Z"
        o = self.build(observation=obs)
        self.assertTrue(o["market_data_stale"])
        self.assertEqual(o["timing"]["action"],"REFRESH_MARKET_DATA")

    def test_fresh_benchmark_does_not_mask_stale_forecast_history(self):
        obs = vars(quote())
        obs["history_observed_at"] = "2020-01-01T00:00:00Z"
        o = self.build(observation=obs)
        self.assertTrue(o["market_data_stale"])
        self.assertEqual(o["timing"]["action"],"REFRESH_MARKET_DATA")


class EndpointTests(unittest.TestCase):
    def test_real_handler_demo_isolated_and_outlook_serializes(self):
        agent = MagicMock()
        with patch.object(live_agent, 'ProcurementDecisionAgent', return_value=agent):
            import server
        baseline_agent = server.AGENT
        server.AGENT = agent
        prices = tuple(np.exp(np.linspace(5,4,180)))
        fc = E.ForecastResult('TEST',100,30,70,80,90,0,0,1,20,180,'TEST',{},[])
        try:
            with (patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden')),
                 patch.object(C,'fx_usd_inr_fbil', return_value=quote(85)),
                 patch.object(C,'india_hrc_nexarc', return_value=(quote(60000),[])),
                 patch.object(C,'steel_hrc_futures', return_value=(quote(100),list(enumerate(prices)))),
                 patch.object(C,'cross_exchange_basis_audit',return_value={'available':False,'reason':'TEST','verdict':'UNAVAILABLE','audit_trail':'TEST'}),
                 patch.object(CR,'rank_source_countries',return_value=[country()]),
                 patch.object(E,'forecast',return_value=fc)):
                payload={'material_type':'STEEL_HRC','destination_iso3':'IND',
                    'demo_threshold_crossed':True,'safe_value_pct':5,'preferred_source_country':'IND'}
                h=server.ProcurementRequestHandler.__new__(server.ProcurementRequestHandler)
                body=json.dumps(payload).encode()
                h.path='/api/evaluate'; h.headers={'Content-Length':str(len(body))}; h.rfile=io.BytesIO(body)
                captured={}
                h._send_json=lambda data,status=200:captured.update(data=data,status=status)
                h.do_POST()
                self.assertEqual(captured['status'],200)
                d=captured['data']['data']
                self.assertEqual(d['purchase_outlook']['mode'],'DEMO_SIMULATION')
                self.assertEqual(d['action_request']['approval_state'],'DEMO_ONLY')
                self.assertEqual(len(d['purchase_outlook']['top_suppliers']),3)
                json.dumps(captured,allow_nan=False)
                agent.create_action_request.assert_not_called()
        finally:
            server.AGENT=baseline_agent


if __name__ == '__main__':
    unittest.main()

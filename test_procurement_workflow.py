"""Network-free regression tests for the focused procurement desk."""
import datetime as dt
from dataclasses import asdict
import math
import unittest
from unittest.mock import patch

import connectors as C
import country_rank as CR
import procurement_workflow as W
from market_stream import MarketStreamManager


def market():
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    return dict(benchmark=3000, prices=[100*math.exp(-i/500) for i in range(180)],
        observation={"observed_at":now,"source":"TEST ONLY"}, history_observed_at=now,
        basis="SYNTHETIC TEST", currency="USD", fx=None, fx_observation=None, fresh=True)


def country(code, price):
    return CR.SourceOption(origin=code,origin_name=code,proven_tonnes=10000,
        fob_usd_per_t=price, fob_source="SYNTHETIC historical CIF",duty_pct=5,
        duty_basis="TEST",duty_decision_grade=False,landed_usd_per_t=price,lpi_score=3)


class WorkflowTests(unittest.TestCase):
    def build(self, **kw):
        return W.build(W.validate(kw),market(),[country("KOR",3000),country("JPN",3200),country("CHN",2900)])

    def test_validation(self):
        for p in ([], {"material":"gold"},{"destination":"BAD"},{"preferred_source_country":"BAD"},
                  {"safe_value_pct":100},{"safe_value_pct":-1},{"on_hand_t":float("nan")},
                  {"on_hand_t":True},{"monthly_demand_t":-1},{"demo_stock_breach":"false"}):
            with self.subTest(p=p),self.assertRaises(ValueError):W.validate(p)

    def test_demo_changes_only_inventory_not_real_prices_or_forecasts(self):
        real=self.build();demo=self.build(demo_stock_breach=True)
        self.assertEqual(demo["inventory"]["on_hand_t"],90)
        self.assertTrue(demo["inventory"]["crossed"])
        self.assertEqual(real["market"]["benchmark"],demo["market"]["benchmark"])
        self.assertEqual([f["p50"] for f in real["forecast"]],[f["p50"] for f in demo["forecast"]])
        self.assertEqual(demo["recommendation"]["buy_after_days"],0)
        self.assertEqual(demo["recommendation"]["quantity_now_t"],380)

    def test_boundary_at_trigger_and_above(self):
        self.assertTrue(self.build(on_hand_t=90)["inventory"]["crossed"])
        self.assertFalse(self.build(on_hand_t=90.01)["inventory"]["crossed"])
        self.assertTrue(self.build(on_hand_t=100,safe_value_pct=0)["inventory"]["crossed"])

    def test_waiting_includes_lead_time(self):
        d=self.build(on_hand_t=650)
        one=d["forecast"][0]
        self.assertFalse(one["feasible"])  # 55 safe days < 30 + 35 import days
        self.assertEqual(one["bridge_t"],100)
        rich=self.build(on_hand_t=2000)
        self.assertEqual(rich["recommendation"]["buy_after_days"],90)

    def test_no_consumption_no_purchase(self):
        d=self.build(monthly_demand_t=0,demo_stock_breach=True)
        self.assertEqual(d["recommendation"]["quantity_now_t"],0)
        self.assertIsNone(d["inventory"]["stockout_days"])

    def test_cheap_import_cannot_beat_urgent_domestic_route(self):
        d=self.build(demo_stock_breach=True)
        self.assertEqual(d["countries"][0]["country"],"IND")
        self.assertEqual(d["recommendation"]["country"],"India")
        self.assertEqual(d["recommendation"]["supplier"],"Hindustan Zinc")

    def test_top_three_plus_preferred_no_duplicates(self):
        d=self.build(on_hand_t=2000,preferred_source_country="AUS")
        codes=[c["country"] for c in d["countries"]]
        self.assertEqual(len(codes),4)
        self.assertEqual(len(codes),len(set(codes)))
        self.assertIn("AUS",codes)
        self.assertTrue(d["countries"][-1]["preferred"])

    def test_three_suppliers_per_country_and_explicit_gaps(self):
        catalogue=W.O.research()
        catalogue["suppliers"]=[s for s in catalogue["suppliers"] if s["country"]!="CHN"]
        with patch.object(W.O,"research",return_value=catalogue):
            d=self.build(material="STEEL_HRC",preferred_source_country="IND",on_hand_t=2000)
        ind=next(c for c in d["countries"] if c["country"]=="IND")
        self.assertEqual(len(ind["suppliers"]),3)
        jpn=next(c for c in d["countries"] if c["country"]=="JPN")
        self.assertEqual(len(jpn["suppliers"]),3)
        chn=next(c for c in d["countries"] if c["country"]=="CHN")
        self.assertEqual(chn["supplier_gap"],3)
        for c in d["countries"]:
            for s in c["suppliers"]:
                self.assertIsNone(s["quote_per_t"])
                self.assertIsNone(s["delivery_days"])
                self.assertIsNone(s["quantity_discount"])

    def test_missing_and_stale_market_never_recommends_waiting(self):
        inputs=W.validate({"on_hand_t":2000})
        for m in (None,dict(market(),fresh=False)):
            d=W.build(inputs,m,[])
            self.assertEqual(d["recommendation"]["buy_after_days"],0)
            self.assertIn("Refresh",d["recommendation"]["action"])

    def test_no_delivery_before_stockout_is_explicit(self):
        d=self.build(on_hand_t=0)
        self.assertIsNone(d["recommendation"]["supplier"])
        self.assertGreater(d["inventory"]["urgent_bridge_t"],0)
        self.assertTrue(any("No researched country" in e for e in d["errors"]))

    def test_missing_fx_never_maps_usd_costs_as_inr(self):
        m=market();m.update(currency="INR",fx=None)
        d=W.build(W.validate({}),m,[country("KOR",3000)])
        self.assertTrue(all(c["screen_cost"] is None for c in d["countries"]))


class FeedTests(unittest.TestCase):
    def obs(self, observed=None):
        now=dt.datetime.now(dt.timezone.utc).isoformat()
        return C.Observation("ZINC.LME.CASH",3000,"USD/t","USD",observed or now,now,"TEST")

    def test_canonical_feed_ids(self):
        mgr=MarketStreamManager();mgr.ingest_observation(self.obs())
        self.assertIn("LME:ZINC:CASH",mgr.get_snapshot())
        self.assertNotIn("ZINC.LME.CASH",mgr.get_snapshot())

    def test_staleness_recalculated_at_read(self):
        mgr=MarketStreamManager();q=mgr.ingest_observation(self.obs())
        q.observed_at=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=5)).isoformat()
        self.assertTrue(mgr.get_snapshot()[q.symbol]["is_stale"])

    def test_future_timestamp_not_fresh(self):
        mgr=MarketStreamManager();mgr.ingest_observation(self.obs((dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=1)).isoformat()))
        self.assertEqual(mgr.get_snapshot()["LME:ZINC:CASH"]["quality"],"UNKNOWN_AGE")

    def test_simulation_off_worker_does_not_push_ticks(self):
        mgr=MarketStreamManager(use_simulation=False)
        with patch.object(mgr,"refresh_public_feeds"),patch.object(mgr,"push_tick") as push,patch("market_stream.time.sleep",side_effect=lambda _:setattr(mgr,"_running",False)):
            mgr.start_background_stream();mgr._worker_thread.join(timeout=2)
        push.assert_not_called()


if __name__ == "__main__":unittest.main()

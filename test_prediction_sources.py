"""Network-free traceability regressions for the recommendation source ledger.

Synthetic inputs exercise attribution, not the validity of supplier websites or
the predictive skill of the internal model. Network access is forbidden here.
"""
import copy
import datetime as dt
import math
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import country_rank as CR
import prediction_sources as S
import procurement_workflow as W
import purchase_outlook as O


def sample_market(material="ZINC_SHG", currency="INR", fx=85.0):
    observed = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
    retrieved = dt.datetime.now(dt.timezone.utc).isoformat()
    return dict(
        benchmark=255000.0 if currency == "INR" else 3000.0,
        prices=[3000 * math.exp(-n / 2000) for n in range(180)],
        observation=dict(source="SYNTHETIC benchmark", observed_at=observed, retrieved_at=retrieved),
        history_observation=dict(source="SYNTHETIC history", observed_at=observed, retrieved_at=retrieved),
        history_observed_at=observed, history_count=180,
        history_start="2026-01-01", history_end="2026-09-07",
        basis="SYNTHETIC TEST DATA", currency=currency, fx=fx,
        fx_observation=dict(source="SYNTHETIC FX", observed_at=observed, retrieved_at=retrieved) if fx else None,
        fresh=True,
    )


def sample_country(code="KOR"):
    return CR.SourceOption(
        origin=code, origin_name=code, proven_tonnes=10000,
        fob_usd_per_t=3000, fob_source="Comtrade 2024 unit value, CIF-basis (screening proxy, not a quote)",
        duty_pct=5, duty_basis="SYNTHETIC UNVERIFIED DUTY", duty_decision_grade=False,
        landed_usd_per_t=3150, lpi_score=3,
    )


class PredictionSourceTests(unittest.TestCase):
    def setUp(self):
        blocker = patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden in source tests"))
        blocker.start()
        self.addCleanup(blocker.stop)
        history = patch("supplier_refresh.read_status", return_value={"last_run":None,"publications":{},"note":"ISOLATED TEST"})
        history.start()
        self.addCleanup(history.stop)

    def build(self, material="ZINC_SHG", destination="IND", market=None, **inputs):
        if market is None:
            market = sample_market(material, "INR" if destination == "IND" else "USD", 85 if destination == "IND" else None)
        return W.build(W.validate(dict(material=material, destination=destination,
            on_hand_t=2000, preferred_source_country="ESP", **inputs)), market, [sample_country()])

    def ledger(self, result):
        return {row["id"]: row for row in result["sources"]}

    def test_automatic_retrieval_is_not_supplier_publication_date(self):
        result = self.build()
        supplier = result["countries"][-1]["suppliers"][0]
        supplier["automatic_check"] = dict(checked_at="2026-09-08T12:00:00+05:30", status="retrieved",
            last_success_at="2026-09-08T12:00:00+05:30", last_change_at=None, note="Retrieval only")
        S.attach(result)
        source = self.ledger(result)["supplier-"+supplier["id"]]
        self.assertIsNone(source["observed_at"])
        self.assertEqual(source["retrieved_at"],supplier["automatic_check"]["last_success_at"])

    def test_all_forecast_and_country_references_resolve(self):
        result = self.build()
        ids = self.ledger(result)
        self.assertEqual(len(ids), len(result["sources"]))
        objects = result["forecast"] + result["countries"] + [result["recommendation"]]
        objects += [future for country in result["countries"] for future in country["future_screen"]]
        objects += [supplier for country in result["countries"] for supplier in country["suppliers"]]
        for row in objects:
            self.assertTrue(row["source_ids"])
            self.assertTrue(set(row["source_ids"]) <= ids.keys(), row["source_ids"])

    def test_every_forecast_identifies_internal_model_and_horizon(self):
        result = self.build()
        model = self.ledger(result)["model"]
        self.assertIn("NOT EXTERNAL PREDICTION", model["kind"])
        self.assertIsNone(model["url"])
        for fragment in ("8,000", "five-observation", "not independently validated"):
            self.assertIn(fragment, model["description"])
        for row, steps in zip(result["forecast"], (21, 42, 63)):
            self.assertEqual(row["model_steps"], steps)
            self.assertTrue({"model", "benchmark", "history", "inventory"} <= set(row["source_ids"]))
        self.assertIn("NOT A FORECAST", self.ledger(result)["benchmark"]["kind"])

    def test_observation_and_retrieval_dates_are_distinct_and_preserved(self):
        market = sample_market()
        result = self.build(market=market)
        sources = self.ledger(result)
        for key, observation in (("benchmark", "observation"), ("history", "history_observation"), ("fx", "fx_observation")):
            self.assertEqual(sources[key]["retrieved_at"], market[observation]["retrieved_at"])
            self.assertLessEqual(dt.datetime.fromisoformat(sources[key]["observed_at"]),
                                 dt.datetime.fromisoformat(sources[key]["retrieved_at"]))
        self.assertIn("2026-01-01", sources["history"]["description"])
        self.assertIn("2026-09-07", sources["history"]["description"])
        self.assertEqual(sources["trade-KOR"]["observed_at"], "2024")
        self.assertIsNone(sources["trade-KOR"]["retrieved_at"])

    def test_missing_source_dates_stay_unknown(self):
        market = sample_market()
        for key in ("observation", "history_observation", "fx_observation"):
            market[key].pop("retrieved_at")
        for source in self.build(market=market)["sources"]:
            self.assertIsNone(source["retrieved_at"])

    def test_indian_steel_does_not_turn_fetch_date_into_quote_date(self):
        result = self.build(material="STEEL_HRC")
        source = self.ledger(result)["benchmark"]
        self.assertIsNone(source["observed_at"])
        self.assertIsNotNone(source["retrieved_at"])
        self.assertIn("does not establish", source["description"])
        self.assertIn("cross-region proxy", self.ledger(result)["history"]["description"])

    def test_comtrade_uses_reporter_code_and_hs4_not_iso_numeric(self):
        for destination, expected in (("IND", "699"), ("USA", "842"), ("ITA", "381")):
            with self.subTest(destination=destination):
                source = self.ledger(self.build(destination=destination))["trade-KOR"]
                query = parse_qs(urlparse(source["url"]).query)
                self.assertEqual(query["reporterCode"], [expected])
                self.assertEqual(query["cmdCode"], ["7901"])
                self.assertEqual(query["period"], ["2024"])
                self.assertEqual(query["flowCode"], ["M"])

    def test_wits_matches_connector_iso_codes_and_date_range(self):
        # Mirror tariff.wits_series: reporter unpadded, partner padded to 3.
        for destination, code in (("IND", "356"), ("AUS", "36"), ("BEL", "56")):
            with self.subTest(destination=destination):
                source = self.ledger(self.build(destination=destination))["duty-KOR"]
                self.assertIn(f"/A.{code}.410.790111.Reported/", source["url"])
                self.assertEqual(parse_qs(urlparse(source["url"]).query),
                    {"startPeriod": ["2015"], "endPeriod": ["2026"]})
                mfn = self.ledger(self.build(destination=destination))["mfn-KOR"]
                self.assertIn(f"/A.{code}.000.790111.Reported/", mfn["url"])

    def test_fx_is_only_a_forecast_input_when_benchmark_conversion_uses_it(self):
        zinc = self.build()
        steel = self.build(material="STEEL_HRC")
        usd = self.build(destination="ESP")
        for row in zinc["forecast"]:
            self.assertIn("fx", row["source_ids"])
        for result in (steel, usd):
            for row in result["forecast"]:
                self.assertNotIn("fx", row["source_ids"])
        self.assertNotIn("fx", self.ledger(usd))

    def test_current_and_future_inr_country_screens_cite_currency_conversion(self):
        for material in ("ZINC_SHG", "STEEL_HRC"):
            result = self.build(material=material)
            country = next(c for c in result["countries"] if c["country"] == "KOR")
            self.assertEqual(country["screen_cost"], 3150 * 85)
            self.assertIn("fx", country["source_ids"])
            for future in country["future_screen"]:
                self.assertIn("fx", future["source_ids"])

    def test_missing_fx_does_not_fabricate_country_costs_or_a_source(self):
        result = self.build(material="STEEL_HRC", market=sample_market(currency="INR", fx=None))
        self.assertNotIn("fx", self.ledger(result))
        for country in result["countries"]:
            self.assertIsNone(country["screen_cost"])
            self.assertEqual(country["future_screen"], [])
            self.assertNotIn("fx", country["source_ids"])

    def test_recommended_country_evidence_is_part_of_recommendation_trace(self):
        result = self.build()
        country = next(c for c in result["countries"] if c["name"] == result["recommendation"]["country"])
        self.assertTrue(set(country["source_ids"]) <= set(result["recommendation"]["source_ids"]))

    def test_no_market_has_no_fabricated_observed_or_forecast_sources(self):
        result = W.build(W.validate({"preferred_source_country": "ESP"}), None, [])
        self.assertEqual(result["forecast"], [])
        for key in ("benchmark", "history", "fx"):
            self.assertNotIn(key, self.ledger(result))

    def test_spain_has_three_distinct_evidenced_leads_per_commodity(self):
        catalog = O.research()
        for material in ("ZINC_SHG", "STEEL_HRC"):
            leads = [s for s in catalog["suppliers"] if s["country"] == "ESP" and s["material"] == material]
            self.assertGreaterEqual(len({s["id"] for s in leads}), 3)
            result = self.build(material=material)
            country = next(c for c in result["countries"] if c["country"] == "ESP")
            self.assertEqual(country["supplier_gap"], 0)
            for supplier in country["suppliers"]:
                self.assertTrue(supplier["url"].startswith("https://"))
                self.assertTrue(supplier["evidence"])
                self.assertIsNone(supplier["quote_per_t"])
                self.assertIsNone(supplier["delivery_days"])
                self.assertIsNone(supplier["quantity_discount"])
                evidence = self.ledger(result)["supplier-" + supplier["id"]]
                self.assertEqual(evidence["url"], supplier["url"])
                self.assertIn("NOT A QUOTE", evidence["kind"])
                self.assertIsNone(evidence["observed_at"])
                self.assertIsNone(evidence["retrieved_at"])

    def test_attaching_sources_is_idempotent(self):
        result = self.build()
        self.assertEqual(result, S.attach(copy.deepcopy(result)))


if __name__ == "__main__":
    unittest.main()

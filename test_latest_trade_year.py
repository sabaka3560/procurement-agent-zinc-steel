"""Network-free checks: select the latest published trade year, never relabel it.

Fixtures are synthetic and do not assert that UN Comtrade publishes 2026 data
for any particular reporter. Freeze the clock so fallback behavior stays stable.
"""
import datetime as dt
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch
from urllib.parse import parse_qs, urlparse

import country_rank as CR
import procurement_workflow as W


class September2026(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 9, 9, 8, 0, tzinfo=dt.timezone.utc)
        return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)


FLOW = dict(partner_code=410, partner="Rep. of Korea", value_usd=6000000,
            net_weight_kg=2000000, unit_value_usd_per_t=3000)


class LatestTradeYearTests(unittest.TestCase):
    def setUp(self):
        for target, kwargs in (
            ("requests.sessions.Session.request", dict(side_effect=AssertionError("Network forbidden"))),
            ("country_rank.dt.datetime", dict(new=September2026)),
            ("country_rank.T.total_duty_pct", dict(return_value=(5, SimpleNamespace(
                notes=[], source="SYNTHETIC duty", usable_for_decision=False), []))),
            ("country_rank.lpi", dict(return_value=3.0)),
            ("supplier_refresh.read_status", dict(return_value={"last_run": None, "publications": {}})),
        ):
            active = patch(target, **kwargs)
            active.start()
            self.addCleanup(active.stop)
        for cache in (CR._flow_cache, CR._flow_cached_at):
            active = patch.dict(cache, {}, clear=True)
            active.start()
            self.addCleanup(active.stop)

    def rank_and_build(self, available_year):
        def published(reporter, hs, year):
            return [dict(FLOW)] if year == available_year else []
        with patch.object(CR, "_imports_by_partner", side_effect=published) as fetch:
            ranked = CR.rank_source_countries("IND", "790111")
        result = W.build(W.validate(dict(preferred_source_country="KOR", on_hand_t=2000)), None, ranked)
        source = next(s for s in result["sources"] if s["id"] == "trade-KOR")
        country = next(c for c in result["countries"] if c["country"] == "KOR")
        return ranked[0], country, source, fetch.call_args_list

    def assert_actual_year(self, option, country, source, year):
        self.assertIn(f"Comtrade {year} ", option.fob_source)
        self.assertEqual(country["price_basis"], option.fob_source)
        self.assertEqual(source["observed_at"], str(year))
        self.assertIsNone(source["retrieved_at"])
        self.assertEqual(parse_qs(urlparse(source["url"]).query)["period"], [str(year)])
        self.assertIn("not a quote", option.fob_source)
        self.assertIn("Not a current vendor offer", source["description"])

    def test_2026_response_is_used_and_explicitly_provisional_through_ledger(self):
        option, country, source, calls = self.rank_and_build(2026)
        self.assertEqual(calls, [call(699, "7901", 2026)])
        self.assert_actual_year(option, country, source, 2026)
        self.assertIn("incomplete-year / provisional", option.fob_source)
        self.assertTrue(any("year is incomplete" in note for note in option.notes))
        self.assertIn("incomplete-year / provisional", source["description"])
        self.assertEqual(option.landed_usd_per_t, 3150)

    def test_2025_response_is_used_when_2026_is_unpublished(self):
        option, country, source, calls = self.rank_and_build(2025)
        self.assertEqual(calls, [call(699, "7901", 2026), call(699, "7901", 2025)])
        self.assert_actual_year(option, country, source, 2025)
        self.assertNotIn("incomplete-year", option.fob_source)
        self.assertNotIn("Current-year", source["description"])

    def test_2024_fallback_keeps_actual_year_in_country_and_source(self):
        option, country, source, calls = self.rank_and_build(2024)
        self.assertEqual(calls, [call(699, "7901", year) for year in (2026, 2025, 2024)])
        self.assert_actual_year(option, country, source, 2024)
        self.assertNotIn("2026", source["title"])
        self.assertNotIn("Current-year", source["description"])

    def test_all_years_empty_produces_no_trade_evidence_or_screen_price(self):
        with patch.object(CR, "_imports_by_partner", return_value=[]) as fetch:
            ranked = CR.rank_source_countries("IND", "790111")
        self.assertEqual(fetch.call_args_list, [call(699, "7901", year) for year in (2026, 2025, 2024, 2023)])
        self.assertEqual(ranked, [])
        result = W.build(W.validate(dict(preferred_source_country="KOR")), None, ranked)
        self.assertFalse(any(s["id"].startswith("trade-") for s in result["sources"]))
        self.assertTrue(all(c["screen_cost"] is None for c in result["countries"]))

    def test_workflow_explicitly_requests_current_year_not_last_completed_year(self):
        with patch.object(W, "fetch_market", return_value=None), \
             patch.object(CR, "rank_source_countries", return_value=[]) as rank:
            W.evaluate({})
        rank.assert_called_once_with("IND", "790111", year=2026, top_n=50)

    def test_connector_rejects_mismatched_and_missing_response_periods(self):
        rows = [
            dict(period=2024, partnerCode=392, partner2Code=0, primaryValue=9000000, netWgt=2000000),
            dict(partnerCode=36, partner2Code=0, primaryValue=8000000, netWgt=2000000),
            dict(period=2026, partnerCode=410, partner2Code=0, primaryValue=6000000, netWgt=2000000),
        ]
        response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": rows})
        with patch.object(CR.requests, "get", return_value=response) as fetch:
            flows = CR._imports_by_partner(699, "7901", 2026)
        self.assertEqual([row["partner_code"] for row in flows], [410])
        self.assertEqual(flows[0]["unit_value_usd_per_t"], 3000)
        self.assertEqual(parse_qs(urlparse(fetch.call_args.args[0]).query)["period"], ["2026"])


if __name__ == "__main__":
    unittest.main()

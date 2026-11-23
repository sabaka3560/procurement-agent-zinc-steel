"""Actual stage reporting, independent of network services."""
import time
import unittest
from unittest.mock import patch
import procurement_workflow as W
from test_procurement_workflow import market, country


class ProgressTests(unittest.TestCase):
    def test_reports_completed_jobs_without_waiting_for_slower_country_screen(self):
        updates = []
        def slow_countries(*args, **kwargs):
            time.sleep(.05)
            return [country("JPN", 3000)]
        with patch.object(W, "fetch_market", return_value=market()), \
             patch.object(W.CR, "rank_source_countries", side_effect=slow_countries), \
             patch("tariff.total_duty_pct", side_effect=ValueError("test gap")):
            result = W.evaluate({}, progress=updates.append)
        events = [(u["stage"], u["status"]) for u in updates]
        self.assertLess(events.index(("market", "done")), events.index(("countries", "done")))
        for stage in ("market", "countries", "analysis", "tariffs", "sources"):
            self.assertIn((stage, "running"), events)
            self.assertTrue(any(s == stage and status in ("done", "warning") for s, status in events))
        self.assertEqual(events[-1], ("sources", "done"))
        self.assertIn("sources", result)

    def test_failed_market_is_warning_not_fake_success(self):
        updates = []
        with patch.object(W, "fetch_market", side_effect=RuntimeError("offline")), \
             patch.object(W.CR, "rank_source_countries", return_value=[]), \
             patch("tariff.total_duty_pct", side_effect=ValueError("test gap")):
            result = W.evaluate({}, progress=updates.append)
        self.assertIn(("market", "warning"), [(u["stage"], u["status"]) for u in updates])
        self.assertIsNone(result["market"])
        self.assertEqual(result["forecast"], [])

    def test_invalid_input_does_not_start_fetches(self):
        updates = []
        with patch.object(W, "fetch_market") as fetch:
            with self.assertRaises(ValueError):
                W.evaluate({"destination": "INVALID"}, progress=updates.append)
        fetch.assert_not_called()
        self.assertEqual(updates, [])

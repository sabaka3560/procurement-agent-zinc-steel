"""No-network tests of the supplier evidence freshness/audit contract."""
import datetime as dt
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import country_rank as CR
import supplier_refresh as S
import procurement_workflow as W


def record(content=b"first", status="retrieved"):
    return dict(url="https://example.com/product", checked_at=S.utcnow(), status=status,
                http_status=200 if status == "retrieved" else 503, final_url="https://example.com/product",
                sha256=hashlib.sha256(content).hexdigest() if status == "retrieved" else None,
                title="Test only", last_modified="Mon, 01 Jan 2024 00:00:00 GMT", note="Not verified",
                content=content if status == "retrieved" else None)


class RefreshTests(unittest.TestCase):
    def test_change_history_survives_failure_and_unchanged_check(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp)/"evidence.db"
            for item in (record(), record(b"second"), record(b"second"), record(status="failed")):
                S.run(db, urls=[item["url"]], fetcher=lambda url: item)
            state = S.read_status(db)
            check = state["publications"][item["url"]]
            self.assertEqual(check["status"], "failed")
            self.assertIsNotNone(check["last_success_at"])
            self.assertIsNotNone(check["last_change_at"])
            self.assertEqual(check["last_modified"], "Mon, 01 Jan 2024 00:00:00 GMT")
            with S.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0], 4)

    def test_stale_and_missing_checks_are_not_current(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Path(temp)/"evidence.db"
            self.assertIsNone(S.read_status(db)["last_run"])
            item=record()
            item["checked_at"]=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=3)).isoformat()
            S.run(db, urls=[item["url"]], fetcher=lambda url:item)
            self.assertTrue(S.read_status(db)["publications"][item["url"]]["stale"])

    def test_overlapping_run_does_not_fetch(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Path(temp)/"evidence.db"
            with S.connect(db) as conn:
                conn.execute("INSERT INTO runs(started_at,status) VALUES (?, 'running')", (S.utcnow(),))
            with patch.object(S, "targets") as targets:
                self.assertEqual(S.run(db)["status"], "already_running")
                targets.assert_not_called()

    def test_non_public_and_non_https_urls_blocked(self):
        for url in ("http://example.com", "https://user:pass@example.com", "https://example.com:8765"):
            with self.assertRaises(ValueError):S.public_url(url)
        with patch.object(S.socket, "getaddrinfo", return_value=[(0,0,0,"",("127.0.0.1",443))]):
            with self.assertRaises(ValueError):S.public_url("https://example.com")

    def test_robots_block_prevents_publication_fetch(self):
        with patch.object(S, "download", return_value=(200,"",{},b"User-agent: *\nDisallow: /")) as get:
            item=S.fetch_publication("https://example.com/product")
        self.assertEqual(item["status"], "robots_blocked")
        self.assertEqual(get.call_count,1)

    def test_checked_timestamp_does_not_become_publication_date(self):
        with patch.object(S, "download", side_effect=[(404,"",{},b""),
             (200,"https://example.com/product",{"Content-Type":"text/html","Last-Modified":"2024"},b"<title>Old product</title>")]):
            item=S.fetch_publication("https://example.com/product")
        self.assertEqual(item["last_modified"], "2024")
        self.assertEqual(item["title"], "Old product")
        self.assertNotIn("observed_at",item)

    def test_workflow_requests_current_year_first(self):
        with patch.object(W, "fetch_market", return_value=None), \
             patch.object(W.CR, "rank_source_countries", return_value=[]) as rank, \
             patch("tariff.total_duty_pct", side_effect=ValueError("test")):
            W.evaluate({})
        self.assertEqual(rank.call_args.kwargs["year"],dt.datetime.now(dt.timezone.utc).year)

    def test_latest_year_wins_without_fetching_older_years(self):
        with patch.dict(CR._flow_cache,{},clear=True), \
             patch.object(CR,"_imports_by_partner",return_value=[{"test":True}]) as fetch:
            _, year=CR.imports_by_partner_any_year(699,"7901",2026)
        self.assertEqual(year,2026)
        fetch.assert_called_once_with(699,"7901",2026)

    def test_2025_used_when_2026_has_no_data(self):
        with patch.dict(CR._flow_cache,{},clear=True), \
             patch.object(CR,"_imports_by_partner",side_effect=[[],[{"test":True}]]) as fetch:
            _, year=CR.imports_by_partner_any_year(699,"7901",2026)
        self.assertEqual(year,2025)
        self.assertEqual([call.args[-1] for call in fetch.call_args_list],[2026,2025])

    def test_expired_trade_cache_rechecks_and_keeps_actual_year(self):
        key=(699,"7901",2025)
        with patch.dict(CR._flow_cache,{key:[]},clear=True), \
             patch.dict(CR._flow_cached_at,{key:CR.time.monotonic()-3601},clear=True), \
             patch.object(CR,"_imports_by_partner",return_value=[{"test":True}]) as fetch:
            rows, year=CR.imports_by_partner_any_year(*key)
        fetch.assert_called_once_with(*key)
        self.assertEqual(year,2025)
        self.assertEqual(rows,[{"test":True}])

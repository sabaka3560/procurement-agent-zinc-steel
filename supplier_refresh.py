"""Daily public supplier evidence checks. Never changes approved catalogue claims.

Re-fetches only catalogue/terms URLs, respects robots.txt, records content hashes
and immutable compressed snapshots in SQLite. Retrieval is NOT verification of
price, availability, origin or publication date. No LLM or search API is used.
"""
import argparse
from contextlib import closing, contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
from pathlib import Path
import socket
import sqlite3
import time
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
import zlib

import requests

BASE = Path(__file__).resolve().parent
DB = BASE / "runtime" / "supplier_evidence.sqlite3"
AGENT = "ProcurementEvidenceMonitor/1.0"
MAX_BYTES = 3_000_000


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def targets():
    import supplier_catalogue
    catalog = supplier_catalogue.load()
    urls = {s["url"] for s in catalog["suppliers"]}
    urls.update(s["contact_url"] for s in catalog["suppliers"] if s.get("contact_url"))
    commercial = json.loads((BASE / "spain_commercial_evidence.json").read_text(encoding="utf-8"))
    urls.update(e["url"] for fields in commercial["suppliers"].values() for e in fields.values() if e.get("url"))
    return sorted(urls)


def public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("Only public HTTPS publication URLs are allowed")
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Non-public address blocked")


def download(session, url, limit=MAX_BYTES):
    # Validate redirects as well; never follow a publication link to a local service.
    original_host = urlsplit(url).hostname
    for _ in range(5):
        public_url(url)
        if urlsplit(url).hostname != original_host:
            raise ValueError("Cross-host redirect requires manual URL and robots review")
        with session.get(url, timeout=(8, 15), allow_redirects=False, stream=True) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get("Location", ""))
                continue
            if response.status_code != 200:
                return response.status_code, url, dict(response.headers), b""
            content = bytearray()
            started = time.monotonic()
            for part in response.iter_content(32768):
                content.extend(part)
                if len(content) > limit or time.monotonic() - started > 45:
                    raise ValueError("Publication exceeded size/time limit")
            return response.status_code, url, dict(response.headers), bytes(content)
    raise ValueError("Too many redirects")


class PageTitle(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "title": self.inside = True

    def handle_endtag(self, tag):
        if tag == "title": self.inside = False

    def handle_data(self, data):
        if self.inside: self.parts.append(data)


def fetch_publication(url):
    record = dict(url=url, checked_at=utcnow(), status="failed", http_status=None,
                  final_url=None, sha256=None, title=None, last_modified=None,
                  note="", content=None)
    try:
        with requests.Session() as session:
            session.headers["User-Agent"] = AGENT
            origin = urlsplit(url)
            robots_url = f"{origin.scheme}://{origin.netloc}/robots.txt"
            status, _, _, body = download(session, robots_url, 512_000)
            if status == 200:
                robots = RobotFileParser()
                robots.parse(body.decode("utf-8", errors="replace").splitlines())
                if not robots.can_fetch(AGENT, url):
                    record.update(status="robots_blocked", note="Automated access disallowed by robots.txt; manual review required.")
                    return record
                # Do not ignore publisher-requested crawl delays/rate limits.
                if robots.crawl_delay(AGENT) or robots.request_rate(AGENT):
                    record.update(status="manual_required", note="Publisher specifies crawl-rate controls; this simple daily connector defers to manual review.")
                    return record
            elif status not in (404, 410):
                record.update(status="robots_unavailable", note=f"Robots policy returned HTTP {status}; publication not fetched.")
                return record
            status, final_url, headers, content = download(session, url)
            record.update(http_status=status, final_url=final_url)
            if status != 200:
                record["note"] = f"Publication returned HTTP {status}; prior evidence retained, not refreshed."
                return record
            media = headers.get("Content-Type", "").lower()
            title = PageTitle()
            if "html" in media:
                title.feed(content.decode("utf-8", errors="replace"))
            elif "pdf" not in media and not content.startswith(b"%PDF"):
                record.update(status="manual_required", note="Unsupported publication format; content not treated as checked evidence.")
                return record
            record.update(status="retrieved", sha256=hashlib.sha256(content).hexdigest(),
                          title=" ".join(" ".join(title.parts).split())[:300] or None,
                          last_modified=headers.get("Last-Modified"), content=content,
                          note="Page/file retrieved only. Content, product fit and commercial claims require review; HTTP 200 is not supplier verification.")
    except (requests.RequestException, ValueError, OSError) as exc:
        record["note"] = f"Refresh failed ({type(exc).__name__}); prior evidence retained."
    return record


@contextmanager
def connect(db=DB):
    db = Path(db)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT);
        CREATE TABLE IF NOT EXISTS snapshots (sha256 TEXT PRIMARY KEY, compressed BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS checks (id INTEGER PRIMARY KEY, run_id INTEGER, url TEXT, checked_at TEXT,
            status TEXT, http_status INTEGER, final_url TEXT, sha256 TEXT, title TEXT, last_modified TEXT,
            note TEXT, changed INTEGER);
        CREATE INDEX IF NOT EXISTS checks_url ON checks(url, id);
    """)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def save_check(conn, run_id, record):
    previous = conn.execute("SELECT sha256 FROM checks WHERE url=? AND status='retrieved' ORDER BY id DESC LIMIT 1", (record["url"],)).fetchone()
    changed = bool(previous and record["sha256"] and previous[0] != record["sha256"])
    if record.get("content") is not None:
        conn.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?)", (record["sha256"], zlib.compress(record["content"])))
    keys = ("url", "checked_at", "status", "http_status", "final_url", "sha256", "title", "last_modified", "note")
    conn.execute("INSERT INTO checks (run_id,url,checked_at,status,http_status,final_url,sha256,title,last_modified,note,changed) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (run_id, *(record[k] for k in keys), int(changed)))
    conn.commit()


def run(db=DB, urls=None, fetcher=fetch_publication):
    with connect(db) as conn:
        # Scheduler IgnoreNew plus durable lease protects manual/scheduled overlap.
        conn.execute("BEGIN IMMEDIATE")
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=45)).isoformat()
        if conn.execute("SELECT 1 FROM runs WHERE finished_at IS NULL AND started_at>?", (cutoff,)).fetchone():
            conn.commit()
            return {"status": "already_running"}
        conn.execute("UPDATE runs SET status='interrupted', finished_at=? WHERE finished_at IS NULL", (utcnow(),))
        run_id = conn.execute("INSERT INTO runs(started_at,status) VALUES (?, 'running')", (utcnow(),)).lastrowid
        conn.commit()
        statuses = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs = {pool.submit(fetcher, url): url for url in (urls if urls is not None else targets())}
            for job in as_completed(jobs):
                record = job.result()
                save_check(conn, run_id, record)
                statuses.append(record["status"])
                print(f"{record['status']}: {record['url']}", flush=True)
        status = "completed" if statuses and all(s == "retrieved" for s in statuses) else "partial"
        conn.execute("UPDATE runs SET finished_at=?,status=? WHERE id=?", (utcnow(), status, run_id))
    return {"status": status, "checked": len(statuses), "retrieved": statuses.count("retrieved")}


def read_status(db=DB):
    if not Path(db).exists():
        return {"last_run": None, "publications": {}, "note": "No automatic supplier checks recorded yet."}
    with closing(sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        run_row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        rows = conn.execute("SELECT * FROM checks WHERE id IN (SELECT MAX(id) FROM checks GROUP BY url)").fetchall()
        publications = {}
        for row in rows:
            record = dict(row)
            last_success = conn.execute("SELECT checked_at FROM checks WHERE url=? AND status='retrieved' ORDER BY id DESC LIMIT 1", (row["url"],)).fetchone()
            last_change = conn.execute("SELECT checked_at FROM checks WHERE url=? AND changed=1 ORDER BY id DESC LIMIT 1", (row["url"],)).fetchone()
            record["last_success_at"] = last_success[0] if last_success else None
            record["last_change_at"] = last_change[0] if last_change else None
            record["stale"] = not last_success or (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last_success[0])).total_seconds() > 36*3600
            publications[row["url"]] = record
        return {"last_run": dict(run_row) if run_row else None, "publications": publications,
                "note": "Automatic retrieval does not update the publication date or verify prices. Content changes need review."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    print(json.dumps(read_status() if args.status else run(), indent=2))

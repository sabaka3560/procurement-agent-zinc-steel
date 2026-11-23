"""Local browser-test fixture. All prices are SYNTHETIC; never use as a demo feed.

python -B tests/outlook_browser_server.py
"""
import datetime as dt
import math
from pathlib import Path
import sys
import time
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connectors as C
import country_rank as CR
import procurement_mcp as MCP
from market_stream import MarketStreamManager
from test_purchase_outlook import country

now = dt.datetime.now(dt.timezone.utc).isoformat()
def obs(value, series, unit, currency):
    return C.Observation(series=series, value=value, unit=unit, currency=currency,
        observed_at=now, retrieved_at=now, source="SYNTHETIC BROWSER TEST", quality="proxy")

history = [(str(i), 100 * math.exp(-i / 1000)) for i in range(180)]
C.fx_usd_inr_fbil = lambda: obs(85, "FX", "INR/USD", "INR")
C.india_hrc_nexarc = lambda: (obs(60000, "HRC", "INR/t", "INR"), [])
C.steel_hrc_futures = lambda: (obs(history[-1][1], "CME", "USD/short ton", "USD"), history)
C.lme_zinc_westmetall = lambda: (obs(3000,"ZINC","USD/t","USD"), obs(3100,"ZINC3M","USD/t","USD"), obs(5000,"STOCK","t","USD"), list(reversed(history)))
C.cross_exchange_basis_audit = lambda *a,**k: {"available":False,"reason":"SYNTHETIC TEST","verdict":"UNAVAILABLE","audit_trail":"SYNTHETIC TEST"}
def test_countries(*a, **k):
    time.sleep(.7)  # Make real streaming stages observable in the isolated browser test.
    rows = [country("JPN"),country("KOR",650)]
    china = country("JPN",600)
    china.origin, china.origin_name = "CHN", "China"
    rows.append(china)
    for row in rows:
        row.lpi_score = 3
    return CR.RankedSources(rows, worldwide_rates=[dict(country="ARG",name="Argentina",comtrade_code=32,
        unit_value_usd_per_t=3000,tonnes=20,year=2025,provisional=False,tariff_registry_supported=False,
        source_url="https://example.com/SYNTHETIC-TEST-ONLY")])
CR.rank_source_countries = test_countries
MCP._market_mgr = MarketStreamManager()
MCP._market_mgr.ingest_observation(obs(3000,"ZINC.LME.CASH","USD/t","USD"))
MCP._market_mgr.ingest_observation(obs(422,"ZINC.MCX.FUT","INR/kg","INR"))
MCP._market_mgr.ingest_observation(obs(85,"FX.USDINR.FBIL","INR/USD","INR"))
MCP._market_mgr.ingest_observation(obs(60000,"STEEL.HRC.INDIA","INR/t","INR"))
MCP._market_mgr.ingest_observation(obs(1244,"STEEL.HRC.CME","USD/short ton","USD"))

# Block all outbound requests in this test process.
import requests
def no_network(*a,**k):
    raise requests.RequestException("SYNTHETIC TEST: outbound requests disabled")
requests.sessions.Session.request = no_network
import server

if __name__ == "__main__":
    print("SYNTHETIC TEST SERVER http://127.0.0.1:8878", flush=True)
    ThreadingHTTPServer(("127.0.0.1",8878),server.ProcurementRequestHandler).serve_forever()

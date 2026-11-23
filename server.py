"""
server.py — Zero-Dependency Real-Time Web Server for the Procurement Desk Cockpit.

Serves the interactive live dashboard, streams real-time prices, handles
conversational agent dialogue, and processes human approval gate workflows.
Uses Python standard library http.server — runs anywhere without additional pip installs.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

from live_agent import ProcurementDecisionAgent
import procurement_mcp as MCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("server")

def _age(q: dict):
    """age_seconds is None for UNKNOWN_AGE quotes. `.get(k, 0)` only defaults
    when the KEY IS ABSENT, not when its value is None — so round() received
    None and raised. Unknown age stays None all the way to the client."""
    v = q.get("age_seconds")
    return round(v, 1) if isinstance(v, (int, float)) else None


BASE_DIR = Path(__file__).parent
HTML_FILE = BASE_DIR / "decision_desk.html"

# Global agent singleton
AGENT = ProcurementDecisionAgent()

# Last successfully fetched domestic benchmark. Used ONLY to degrade
# gracefully on a transient fetch failure, always with its real age
# attached. Never seeded with a constant.
_LAST_GOOD_HRC = None


class ProcurementRequestHandler(BaseHTTPRequestHandler):
    server_version = "ProcurementDesk/2.0"

    def _set_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self._set_cors_headers()
        self.end_headers()

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, default=str).encode("utf-8")
        try:
            self.send_response(status)
            self._set_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            # Client closed the connection before we could send the response.
            # This is expected when clients timeout on slow live API calls.
            pass

    def do_GET(self):
        url_path = self.path.split("?")[0]

        if url_path in ("/purchase-outlook.js", "/purchase-outlook.css", "/decision-desk.js", "/decision-desk.css"):
            asset = BASE_DIR / url_path.lstrip("/")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/javascript; charset=utf-8" if asset.suffix == ".js" else "text/css; charset=utf-8")
            self.end_headers()
            self.wfile.write(asset.read_bytes())
            return

        if url_path == "/api/procurement/context":
            import countries as CO
            self._send_json({"countries": [{"code": c.iso3, "name": c.name} for c in CO._C],
                "quotes": AGENT.market_mgr.get_snapshot()})
            return

        if url_path == "/api/procurement/market":
            self._send_json({"quotes": AGENT.market_mgr.get_snapshot()})
            return

        if url_path == "/" or url_path == "/index.html" or url_path == "/legacy":
            if not HTML_FILE.exists():
                self.send_error(HTTPStatus.NOT_FOUND, "live_desk.html not found")
                return
            content = (BASE_DIR / "live_desk.html" if url_path == "/legacy" else HTML_FILE).read_bytes()
            self.send_response(HTTPStatus.OK)
            self._set_cors_headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if url_path == "/api/refresh":
            try:
                AGENT.market_mgr.refresh_public_feeds()
            except Exception as e:
                logger.error(f"Force refresh error: {e}")
            # fall through to return fresh status
            url_path = "/api/status"

        if url_path == "/api/status":
            mkt = MCP.tool_get_live_market_status()
            cov = MCP.tool_check_inventory_coverage("HRC-E250A-2.0", "PLANT-PUNE-1")
            alerts = AGENT.market_mgr.get_recent_alerts(limit=5)
            pars = [r.to_dict() for r in AGENT.action_requests.values()]

            quotes = mkt.get("quotes", {})
            stale_count = sum(1 for q in quotes.values() if q.get("is_stale", False))

            provenance = {
                "audit_rule": "Rule AD-1 (Deterministic Math Only — Zero LLM Numbers)",
                "watchdog_ttl": {
                    "intraday_ttl_minutes": 15,
                    "eod_ttl_hours": 72,
                },
                "stale_feeds_count": stale_count,
                # HTTP status and latency are NOT reported: the stream layer does
                # not record them. They were previously hardcoded (200 / 142ms etc.)
                # inside the panel that claims to verify upstream feeds. What is
                # real and kept: observed_at, retrieved_at, age_seconds, quality,
                # is_stale.
                "telemetry_note": ("Per-feed HTTP status and latency are not "
                                   "instrumented and are therefore not shown. "
                                   "Freshness fields below are measured."),
                "feeds": [
                    {
                        "name": "LME Zinc Cash Settlement",
                        "symbol": "LME:ZINC:CASH",
                        "provider": "Westmetall / London Metal Exchange",
                        "endpoint_url": "https://www.westmetall.com/en/markdaten.php?action=table&field=LME_ZN_Cash",
                        "protocol": "HTTPS Daily Official Settlement Scraper",
                        "observed_at": quotes.get("LME:ZINC:CASH", {}).get("observed_at", ""),
                        "retrieved_at": quotes.get("LME:ZINC:CASH", {}).get("retrieved_at", ""),
                        "age_seconds": _age(quotes.get("LME:ZINC:CASH", {})),
                        "quality": quotes.get("LME:ZINC:CASH", {}).get("quality", "POLL_FRESH"),
                        "is_stale": quotes.get("LME:ZINC:CASH", {}).get("is_stale", False),
                    },
                    {
                        "name": "USD/INR Reference Rate",
                        "symbol": "FX:USDINR:FBIL",
                        "provider": "European Central Bank / Frankfurter API (FBIL Mirror)",
                        "endpoint_url": "https://api.frankfurter.app/latest?from=USD&to=INR",
                        "protocol": "REST JSON API (Daily Official Reference Rate)",
                        "observed_at": quotes.get("FX:USDINR:FBIL", {}).get("observed_at", ""),
                        "retrieved_at": quotes.get("FX:USDINR:FBIL", {}).get("retrieved_at", ""),
                        "age_seconds": _age(quotes.get("FX:USDINR:FBIL", {})),
                        "quality": quotes.get("FX:USDINR:FBIL", {}).get("quality", "POLL_FRESH"),
                        "is_stale": quotes.get("FX:USDINR:FBIL", {}).get("is_stale", False),
                    },
                    {
                        "name": "India Domestic HRC Steel Index",
                        "symbol": "STEEL:HRC:INDIA",
                        "provider": "Tata Nexarc Flat Steel Mumbai Benchmark",
                        "endpoint_url": "https://www.tatanexarc.com",
                        "protocol": "HTTPS Spot Index Scraper (Ex-Mill Mumbai)",
                        "observed_at": quotes.get("STEEL:HRC:INDIA", {}).get("observed_at", ""),
                        "retrieved_at": quotes.get("STEEL:HRC:INDIA", {}).get("retrieved_at", ""),
                        "age_seconds": _age(quotes.get("STEEL:HRC:INDIA", {})),
                        "quality": quotes.get("STEEL:HRC:INDIA", {}).get("quality", "POLL_FRESH"),
                        "is_stale": quotes.get("STEEL:HRC:INDIA", {}).get("is_stale", False),
                    },
                    {
                        "name": "CME HRC Futures",
                        "symbol": "CME:HRC",
                        "provider": "CME Group North American Flat Rolled Steel",
                        "endpoint_url": "https://www.cmegroup.com/markets/metals/ferrous/hrc-steel.html",
                        "protocol": "Futures Settlement Feed",
                        "observed_at": quotes.get("CME:HRC", {}).get("observed_at", ""),
                        "retrieved_at": quotes.get("CME:HRC", {}).get("retrieved_at", ""),
                        "age_seconds": _age(quotes.get("CME:HRC", {})),
                        "quality": quotes.get("CME:HRC", {}).get("quality", "SIMULATED"),
                        "is_stale": quotes.get("CME:HRC", {}).get("is_stale", False),
                    },
                    {
                        "name": "MCX Zinc Intraday Stream",
                        "symbol": "MCX:ZINC",
                        "provider": "Broker Protocol Bridge (Angel One / Zerodha with RFC 6238 TOTP)",
                        "endpoint_url": "wss://smartapisocket.angelone.in/smart-stream",
                        "protocol": "WebSocket Real-Time Tick Stream",
                        "observed_at": quotes.get("MCX:ZINC", {}).get("observed_at", ""),
                        "retrieved_at": quotes.get("MCX:ZINC", {}).get("retrieved_at", ""),
                        "age_seconds": _age(quotes.get("MCX:ZINC", {})),
                        "quality": quotes.get("MCX:ZINC", {}).get("quality", "LIVE_STREAM"),
                        "is_stale": quotes.get("MCX:ZINC", {}).get("is_stale", False),
                    },
                    {
                        "name": "COMEX Copper Futures",
                        "symbol": "COMEX:COPPER",
                        "provider": "CME Group / COMEX Market Data",
                        "endpoint_url": "https://www.cmegroup.com/markets/metals/base/copper.html",
                        "protocol": "Futures Settlement Feed",
                        "observed_at": quotes.get("COMEX:COPPER", {}).get("observed_at", ""),
                        "retrieved_at": quotes.get("COMEX:COPPER", {}).get("retrieved_at", ""),
                        "age_seconds": _age(quotes.get("COMEX:COPPER", {})),
                        "quality": quotes.get("COMEX:COPPER", {}).get("quality", "POLL_FRESH"),
                        "is_stale": quotes.get("COMEX:COPPER", {}).get("is_stale", False),
                    },
                    {
                        "name": "SHFE Steel Rebar Benchmark",
                        "symbol": "SHFE:STEEL:REBAR",
                        "provider": "Shanghai Futures Exchange (SHFE) Daily Settlement",
                        "endpoint_url": "https://www.shfe.com.cn",
                        "protocol": "Exchange Settlement Daily Report",
                        "observed_at": quotes.get("SHFE:STEEL:REBAR", {}).get("observed_at", ""),
                        "retrieved_at": quotes.get("SHFE:STEEL:REBAR", {}).get("retrieved_at", ""),
                        "age_seconds": _age(quotes.get("SHFE:STEEL:REBAR", {})),
                        "quality": quotes.get("SHFE:STEEL:REBAR", {}).get("quality", "POLL_FRESH"),
                        "is_stale": quotes.get("SHFE:STEEL:REBAR", {}).get("is_stale", False),
                    },
                    {
                        "name": "Global Tariffs & Customs Schedules",
                        "symbol": "CUSTOMS:WITS:COMTRADE",
                        "provider": "UN Comtrade REST API & World Bank WITS/TRAINS",
                        "endpoint_url": "https://comtradeapi.un.org/public/v1/preview/C/A/HS",
                        "protocol": "Bilateral Preferential & MFN Customs API",
                        "observed_at": None,
                        "retrieved_at": None,
                        "age_seconds": None,
                        "quality": "UNKNOWN_AGE",
                        "is_stale": True,
                        "note": "Reference endpoints only. See each country result for its actual trade year and tariff evidence; this status record has no verified observation timestamp.",
                    }
                ]
            }

            self._send_json({
                "status": "success",
                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "market": mkt,
                "coverage": cov,
                "alerts": alerts,
                "action_requests": pars,
                "provenance": provenance,
            })
            return

        if url_path == "/api/actions":
            pars = [r.to_dict() for r in AGENT.action_requests.values()]
            self._send_json({"status": "success", "action_requests": pars})
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Endpoint not found")

    def do_POST(self):
        url_path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))

        # Security: reject oversized payloads to prevent DoS (max 100 KB)
        MAX_BODY_SIZE = 100_000  # 100 KB
        if length > MAX_BODY_SIZE:
            self.send_response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b'{"status": "error", "message": "Request body too large"}')
            self.close_connection = True
            return

        post_data = self.rfile.read(length) if length > 0 else b"{}"

        try:
            payload = json.loads(post_data.decode("utf-8")) if post_data else {}
        except Exception:
            self._send_json({"status": "error", "message": "Invalid JSON"}, status=400)
            return


        if url_path == "/api/procurement/recommend":
            import procurement_workflow as workflow
            if "application/x-ndjson" in self.headers.get("Accept", ""):
                try:
                    workflow.validate(payload)
                except ValueError as exc:
                    self._send_json({"message": str(exc)}, status=400)
                    return
                self.send_response(200)
                self._set_cors_headers()
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                def emit(event):
                    self.wfile.write((json.dumps(event, default=str) + "\n").encode("utf-8"))
                    self.wfile.flush()
                try:
                    result = workflow.evaluate(payload, progress=lambda update: emit(dict(type="progress", **update)))
                    emit(dict(type="result", data=result))
                except (ConnectionError, OSError):
                    pass  # Browser disconnected. Read-only evaluation never creates an order.
                except Exception:
                    logger.exception("Streaming procurement workflow failed")
                    try:
                        emit(dict(type="error", message="Analysis unavailable. Please retry; no order was created."))
                    except (ConnectionError, OSError):
                        pass
                return
            try:
                self._send_json(workflow.evaluate(payload))
            except ValueError as exc:
                self._send_json({"message": str(exc)}, status=400)
            except Exception:
                logger.exception("Procurement workflow failed")
                self._send_json({"message": "Analysis unavailable. Please retry; no order was created."}, status=500)
            return

        if not isinstance(payload, dict):
            self._send_json({"message": "Expected a JSON object"}, status=400)
            return

        # 1. Chat with Live Agent
        if url_path == "/api/chat":
            msg = payload.get("message", "").strip()
            if not msg:
                self._send_json({"status": "error", "message": "Message is empty"}, status=400)
                return
            res = AGENT.chat(msg)
            self._send_json({"status": "success", "data": res})
            return

        # 2. Approve Action Request
        if url_path == "/api/action/approve":
            aid = payload.get("action_id", "").strip()
            approver = payload.get("approver", "Category Manager").strip()
            notes = payload.get("notes", "Approved via Procurement Desk").strip()
            req = AGENT.approve_action_request(aid, approver=approver, notes=notes)
            if req:
                self._send_json({"status": "success", "action_request": req.to_dict()})
            else:
                self._send_json({"status": "error", "message": f"Action ID {aid} not found"}, status=404)
            return

        # 3. Reject Action Request
        if url_path == "/api/action/reject":
            aid = payload.get("action_id", "").strip()
            approver = payload.get("approver", "Category Manager").strip()
            reason = payload.get("reason", "Declined by procurement manager").strip()
            req = AGENT.reject_action_request(aid, approver=approver, reason=reason)
            if req:
                self._send_json({"status": "success", "action_request": req.to_dict()})
            else:
                self._send_json({"status": "error", "message": f"Action ID {aid} not found"}, status=404)
            return

        # 4. Modify Action Request
        if url_path == "/api/action/modify":
            aid = payload.get("action_id", "").strip()
            approver = payload.get("approver", "Category Manager").strip()
            new_tonnes = float(payload.get("new_tonnes", 100.0))
            notes = payload.get("notes", "Adjusted order volume").strip()
            req = AGENT.modify_action_request(aid, approver=approver, new_tonnes=new_tonnes, notes=notes)
            if req:
                self._send_json({"status": "success", "action_request": req.to_dict()})
            else:
                self._send_json({"status": "error", "message": f"Action ID {aid} not found"}, status=404)
            return

        # 5. What-If Sensitivity Simulation
        if url_path == "/api/what_if":
            fx_shift = float(payload.get("fx_shift_pct", 0.0))
            freight_shift = float(payload.get("freight_usd_shift", 0.0))
            tariff_override = payload.get("tariff_pct_override")
            tariff_pct = float(tariff_override) if tariff_override is not None else None

            res = MCP.tool_run_what_if_scenario(
                destination_iso3="IND",
                origin_iso3="JPN",
                hs6="720810",
                base_fob_usd=630.0,
                fx_shift_pct=fx_shift,
                freight_usd_shift=freight_shift,
                tariff_pct_override=tariff_pct,
            )
            self._send_json(res)
            return

        # 6. Live Business User Evaluation (Real calculations on user/company inputs)
        if url_path == "/api/evaluate":
            import connectors as C
            import countries as CO
            import country_rank as CR
            import engine as E
            import negotiation as NEG
            import tariff as T

            import purchase_outlook as OUTLOOK
            try:
                outlook_inputs = OUTLOOK.validate_inputs(payload)
            except ValueError as exc:
                self._send_json({"status": "error", "message": str(exc)}, status=400)
                return

            company = str(payload.get("company_name", "XYZ Heavy Industries Ltd"))[:500]
            mat_type = str(payload.get("material_type", "STEEL_HRC"))[:50]
            dest_iso3 = str(payload.get("destination_iso3", "IND"))[:10]

            # Robust numeric parsing — return 400 on type errors, never 500
            try:
                monthly_demand = float(payload.get("monthly_demand_t", 500.0))
                usable_stock = float(payload.get("stock_on_hand_t", 430.0))
                in_transit = float(payload.get("in_transit_t", 210.0))
                open_po = float(payload.get("open_po_t", 360.0))
                safety_stock = float(payload.get("safety_stock_t", 160.0))
                policy_weeks = float(payload.get("policy_weeks", 4.0))
            except (TypeError, ValueError) as e:
                self._send_json({"status": "error", "message": f"Invalid numeric input: {e}"}, status=400)
                return

            # Guard against absurd values to prevent DoS via extreme computation
            monthly_demand = max(0.0, min(monthly_demand, 10_000_000.0))
            usable_stock = min(abs(usable_stock), 10_000_000.0) * (1 if usable_stock >= 0 else -1)
            in_transit = max(0.0, min(in_transit, 10_000_000.0))
            open_po = max(0.0, min(open_po, 10_000_000.0))
            safety_stock = max(0.0, min(safety_stock, 10_000_000.0))
            policy_weeks = max(0.0, min(policy_weeks, 52.0))

            dest = CO.get(dest_iso3) or CO.get("IND")
            fx_val = C.fx_usd_inr_fbil().value if dest.iso3 == "IND" else 1.0

            # 1. Live Market Benchmark & Commodity Series
            if mat_type == "ZINC_SHG":
                hs6 = "790111"
                mat_label = "Special High Grade Zinc (IS 209 / SHG 99.995%)"
                zc, z3, zs, zhist = C.lme_zinc_westmetall()
                unit_price_inr = round(zc.value * fx_val, 0) if dest.iso3 == "IND" else zc.value
                benchmark_source = f"LME Zinc Cash Settlement (${zc.value:,.2f}/t)"
                hist_series = [r[1] for r in reversed(zhist)]
                price_series_basis = ("DIRECT — LME zinc cash settlement, the benchmark "
                                      "this material is actually priced against.")
                vol_ann = None   # computed from the series, never assumed
            else:
                hs6 = "720810"
                mat_label = "Hot-Rolled Steel Coil (IS 2062 / E250A 2.0mm)"
                # No invented price. A hardcoded 59,800 previously stood in for a
                # failed fetch and was presented as the live benchmark.
                #
                # Degradation ladder, in order:
                #   1. fresh fetch
                #   2. LAST KNOWN GOOD — a real observation we actually made
                #      earlier, carried with its own timestamp so its age is
                #      visible. This is data, just older data.
                #   3. refuse (503). Never a constant.
                benchmark_degraded = None
                try:
                    hrc_obs, _ = C.india_hrc_nexarc()
                    if hrc_obs:
                        globals()["_LAST_GOOD_HRC"] = hrc_obs
                except Exception as e:
                    logger.error(f"India HRC fetch failed: {e}")
                    hrc_obs = globals().get("_LAST_GOOD_HRC")
                    if hrc_obs:
                        benchmark_degraded = (
                            f"LAST KNOWN GOOD — live fetch failed ({type(e).__name__}); "
                            f"using the observation made at {hrc_obs.observed_at[:10]}, "
                            f"{hrc_obs.age_days:.1f} days old. Not a current quote.")
                        logger.warning(benchmark_degraded)
                if dest.iso3 != "IND":
                    # No free domestic steel benchmark exists outside India. The
                    # previous code substituted 59,800 -- an INR figure -- and the
                    # UI printed it as "$59,800/t USD". That is a ~6,000% error on
                    # a number a buyer would read as a price.
                    #
                    # Instead: quote the real international reference (CME HRC),
                    # converted short ton -> metric tonne, in USD, and say plainly
                    # that it is NOT a domestic delivered price for this country.
                    try:
                        cme_obs, chist = C.steel_hrc_futures()
                    except Exception as e:
                        self._send_json({
                            "status": "unavailable",
                            "reason": (f"No domestic benchmark exists for {dest.iso3} "
                                       f"and the international reference (CME HRC) is "
                                       f"unreachable ({type(e).__name__}). No price "
                                       f"will be substituted."),
                        }, status=503)
                        return
                    unit_price_inr = round(cme_obs.value * 1.10231, 2)   # $/short ton -> $/t
                    price_currency = "USD"
                    price_unit = "USD/metric tonne"
                    benchmark_source = (
                        f"CME US Midwest HRC ${unit_price_inr:,.0f}/t (metric), "
                        f"as of {cme_obs.observed_at[:10]} — INTERNATIONAL REFERENCE. "
                        f"No free domestic delivered benchmark exists for "
                        f"{dest.name}; this is not that country's mill price.")
                    hist_series = [r[1] for r in chist]
                    price_series_basis = (
                        "CME US Midwest HRC futures (USD/short ton), used as the "
                        "reference for a destination with no domestic benchmark.")
                    vol_ann = None
                elif not hrc_obs:
                    self._send_json({
                        "status": "unavailable",
                        "reason": ("India HRC benchmark unavailable and no prior "
                                   "observation is held. No substitute price will "
                                   "be invented."),
                    }, status=503)
                    return
                else:
                    unit_price_inr = hrc_obs.value
                    price_currency = "INR"
                    price_unit = "INR/tonne"
                if dest.iso3 == "IND":
                    benchmark_source = (f"Tata Nexarc Mumbai Domestic Benchmark "
                                        f"(₹{unit_price_inr:,.0f}/t)"
                                        + (f" [{benchmark_degraded}]" if benchmark_degraded else ""))
                # There is NO free Indian HRC price history — Tata Nexarc publishes
                # current levels, not a series. So the only available series is CME
                # HRC, which is US Midwest, quoted per SHORT TON, and a different
                # market. We use its RETURNS as an openly-labelled volatility proxy
                # and say so in the response; we do not claim it predicts Indian
                # delivered steel. The relationship between the two is not estimated.
                if dest.iso3 == "IND":
                  try:
                    _cme_obs, chist = C.steel_hrc_futures()
                    hist_series = [r[1] for r in chist]
                    price_series_basis = (
                        "PROXY — CME US Midwest HRC futures (USD/short ton). Returns "
                        "only. No estimated relationship to Indian delivered prices; "
                        "treat the band as a scenario range, not a forecast of this "
                        "material.")
                  except Exception as e:
                    logger.error(f"CME HRC history unavailable: {e}")
                    hist_series = []
                    price_series_basis = "UNAVAILABLE — no price history; no band shown."
                  vol_ann = None   # computed from the series below, never assumed

            # Volatility is MEASURED from the series above. It was previously
            # hardcoded (23.5 for zinc, 22.0 for steel) and presented as if derived.
            if hist_series and len(hist_series) > 30:
                import math as _m
                _r = [_m.log(hist_series[i] / hist_series[i - 1])
                      for i in range(1, len(hist_series))
                      if hist_series[i - 1] > 0 and hist_series[i] > 0]
                if len(_r) > 10:
                    _mean = sum(_r) / len(_r)
                    _sd = (sum((x - _mean) ** 2 for x in _r) / (len(_r) - 1)) ** 0.5
                    vol_ann = round(_sd * (252 ** 0.5) * 100, 1)
            if vol_ann is None:
                vol_ann = 0.0   # no series -> no volatility claim; levers below degrade

            # 2. Real Inventory Netting & Coverage
            weekly_burn = monthly_demand / 4.345 if monthly_demand > 0 else 1.0
            pipeline_supply = in_transit + open_po
            available_tonnes = usable_stock + pipeline_supply - safety_stock
            coverage_weeks = round(available_tonnes / weekly_burn, 2) if weekly_burn > 0 else 99.0
            net_need_t = max(0.0, monthly_demand - available_tonnes)

            # 3. Real Probabilistic Forecast & Coverage Backtest
            # forecast() requires >=40 observations and raises otherwise. An
            # unavailable price series previously reached it as [] and threw a
            # 500. Refuse cleanly and say which input is missing.
            if not hist_series or len(hist_series) < 40:
                self._send_json({
                    "status": "unavailable",
                    "reason": (
                        f"Price history unavailable for {mat_type} "
                        f"({len(hist_series or [])} observations, 40 required). "
                        f"{price_series_basis} No forecast, band or timing "
                        f"recommendation is produced without it."),
                    "price_series_basis": price_series_basis,
                }, status=503)
                return
            fc = E.forecast(hist_series, horizon_days=30, series=mat_type)

            # 4. Real Recommendation Optimization (Cost + CVaR + Stockout penalty)
            rec = E.recommend(
                fc=fc,
                requirement_tonnes=monthly_demand,
                current_price_inr_per_t=unit_price_inr,
                coverage_weeks_on_hand=coverage_weeks,
                policy_min_coverage_weeks=policy_weeks,
            )

            # 5. Real Global Sourcing Matrix from UN Comtrade & WITS
            sourcing_options = []
            ranked = []
            try:
                ranked = CR.rank_source_countries(dest.iso3, hs6, top_n=50)
                for idx, r in enumerate(ranked):
                    landed_inr = round(r.landed_usd_per_t * fx_val, 0) if r.landed_usd_per_t else 0
                    sourcing_options.append({
                        "origin_iso3": r.origin,
                        "origin_name": r.origin_name,
                        "price_basis": r.fob_source,
                        "proven_tonnes": round(r.proven_tonnes, 0) if r.proven_tonnes else 0,
                        "fob_usd": round(r.fob_usd_per_t, 1) if r.fob_usd_per_t else 0,
                        "duty_pct": round(r.duty_pct, 1) if r.duty_pct is not None else 0,
                        "tariff_type": r.duty_basis,
                        "freight_est_usd": round(r.freight_usd_per_t, 1) if r.freight_usd_per_t else 0,
                        "landed_usd": round(r.landed_usd_per_t, 1) if r.landed_usd_per_t else 0,
                        "landed_local": landed_inr,
                        "is_cheapest": (idx == 0),
                    })
            except Exception as e:
                logger.error(f"Sourcing matrix error: {e}")

            # 6. Real Rupee Negotiation Levers (10.5% Corporate WACC)
            annual_spend = monthly_demand * 12 * unit_price_inr
            credit_30d_val = round(annual_spend * 0.105 * (30.0 / 365.0), 0)
            flex_val = round(annual_spend * 0.10 * (vol_ann / 100.0) * 0.40, 0)

            # 7. Multi-Stage Consulting Evaluation & Policy Validation Layer
            deficit_to_floor_t = max(0.0, (policy_weeks * weekly_burn) - available_tonnes)
            # DO NOT recompute coverage here. The engine already evaluated it
            # deferral-aware (coverage - weeks_deferred + tonnes/weekly). The
            # server previously used coverage + tonnes/burn, omitting the
            # deferral window, which read ~4.3 weeks more optimistic. Result:
            # the engine returned 0/5 feasible and "shortage to escalate" while
            # the API reported COMMIT 100% with GATE-01 COMPLIANT. Take the
            # engine's own number so the two cannot disagree.
            _chosen = next((o for o in rec["options"]
                            if o["commit_now_pct"] == rec["recommended_commit_now_pct"]),
                           None)
            post_order_coverage = (_chosen or {}).get("coverage_after_weeks")
            if post_order_coverage is None:      # engine gave us nothing usable
                post_order_coverage = coverage_weeks
            engine_feasible = bool((_chosen or {}).get("feasible", False))
            engine_feasible_count = rec.get("feasible_option_count")
            engine_selection_basis = rec.get("selection_basis", "")

            # Cross-exchange basis audit. Not "4-exchange": SHFE is not
            # obtainable, so zinc compares LME vs MCX and steel compares CME
            # vs domestic vs a Comtrade-derived China parity.
            basis_audit = C.cross_exchange_basis_audit(mat_type, dest.iso3)

            # Section 3: cross-exchange basis.
            # NO NUMERIC FALLBACKS. Every figure below is fetched or the section
            # says it is unavailable. A `.get(key, 3995.5)` here previously meant
            # a failed fetch printed a real-looking price captured weeks earlier.
            def _f(key, fmt="{:,.2f}"):
                v = basis_audit.get(key)
                return fmt.format(v) if isinstance(v, (int, float)) else "unavailable"

            if not basis_audit.get("available"):
                sec3_title = "3. Cross-Exchange Basis: UNAVAILABLE"
                sec3_body = (
                    f"The basis audit could not run: {basis_audit.get('reason', 'unknown')}. "
                    f"No import-parity comparison is presented, because presenting one "
                    f"would require substituting a figure we did not fetch."
                )
            elif mat_type == "ZINC_SHG":
                sec3_title = "3. Cross-Exchange Basis (LME vs MCX India):"
                verdict = basis_audit.get("verdict")
                sec3_body = (
                    f"LME cash settles at ${_f('lme_cash_usd_t')}/t as of "
                    f"{basis_audit.get('lme_observed_at', 'n/a')} (curve: "
                    f"{basis_audit.get('lme_curve_state', 'n/a')}). Converted at the official "
                    f"FBIL rate and loaded with a ${basis_audit.get('assumptions', {}).get('ocean_freight_usd_t', 'n/a')}/t "
                    f"freight estimate plus statutory 10.5% duty (5% BCD + 5% AIDC + 0.5% SWS) "
                    f"and port logistics, import parity is ₹{_f('landed_lme_parity_inr_kg')}/kg. "
                    f"MCX front-month trades at ₹{_f('mcx_spot_inr_kg')}/kg as of "
                    f"{basis_audit.get('mcx_observed_at', 'n/a')} "
                    f"({basis_audit.get('mcx_age_days', '?')}d old) — a basis of "
                    f"{_f('basis_spread_pct', '{:+.2f}')}%. "
                    f"VERDICT: {verdict}. {basis_audit.get('verdict_reason', '')} "
                    f"Physical premium is not modelled."
                )
            else:
                sec3_title = "3. Cross-Exchange Basis (CME vs Domestic Nexarc):"
                china_pct = basis_audit.get("domestic_vs_china_basis_pct")
                sec3_body = (
                    f"Domestic ex-mill spot at ₹{unit_price_inr:,.0f}/t "
                    f"({basis_audit.get('domestic_source', 'n/a')}). "
                    f"CME HRC trades at ${_f('cme_hrc_usd_metric_ton', '{:,.0f}')}/MT as of "
                    f"{basis_audit.get('cme_observed_at', 'n/a')}. "
                    + (f"China landed parity is ₹{_f('china_landed_parity_inr_t', '{:,.0f}')}/t "
                       f"({basis_audit.get('china_parity_source', 'source unstated')}), "
                       f"a basis of {china_pct:+.2f}%. VERDICT: {basis_audit.get('verdict')}. "
                       f"{basis_audit.get('verdict_reason', '')} "
                       if china_pct is not None else
                       "No China parity leg is available, so no import-vs-domestic verdict "
                       "is offered for that origin. ")
                    + f"SHFE: {basis_audit.get('shfe_status', 'n/a')}"
                )

            # Executive Consulting Advisory Memo (McKinsey/Bain research quality)
            consulting_memo = (
                f"STRATEGIC PROCUREMENT MEMORANDUM | {company.upper()}\n"
                f"COMMODITY: {mat_label} | DESTINATION: {dest.name} PLANT\n\n"
                f"1. Executive Sourcing Verdict:\n"
                f"Our multi-echelon optimization recommends committing {rec['recommended_commit_now_pct']}% "
                f"({rec['recommended_tonnes_now']:,.0f} MT) immediately at domestic benchmark ₹{unit_price_inr:,.0f}/t "
                f"(total capital commitment: ₹{rec['recommended_tonnes_now'] * unit_price_inr / 1e7:.2f} Crores).\n\n"
                f"2. Core Operational & Supply Chain Rationale:\n"
                f"Current unrestricted inventory stands at {usable_stock:,.0f} MT with {pipeline_supply:,.0f} MT confirmed pipeline, "
                f"yielding an effective operational runway of {coverage_weeks:.1f} weeks against your mandated {policy_weeks:.1f}-week floor. "
                f"Factoring in standard domestic mill rolling cycles (18–24 days) and transit lead times, failure to commit now creates a "
                f"{deficit_to_floor_t:,.0f} MT deficit against safety buffer thresholds. Committing {rec['recommended_tonnes_now']:,.0f} MT restores "
                f"coverage runway to {post_order_coverage:.1f} weeks without incurring excessive holding or carrying costs.\n\n"
                f"{sec3_title}\n"
                f"{sec3_body}\n\n"
                f"4. Supplier Negotiation Directive:\n"
                f"Use the best ranked import origin as a BATNA counter-anchor against the primary domestic mill "
                f"Domestic Mill A. Prioritize negotiating a 30-day working capital extension (valued at ₹{credit_30d_val/1e5:.1f} Lakhs @ 10.5% WACC) "
                f"and ±10% volume flexibility before agreeing to contract baseline prices."
            )

            # Structured Memo Data for Visual Executive Panels
            # No ranked origin means no import comparison exists. Naming one
            # anyway (with an invented FOB, duty and freight) is the failure
            # mode this whole pass is closing out: the buyer reads a country
            # and a price that no fetch produced.
            best_import = sourcing_options[0] if sourcing_options else None
            japan_opt = next((s for s in sourcing_options if "japan" in s["origin_name"].lower()), None)
            vietnam_opt = next((s for s in sourcing_options if "viet" in s["origin_name"].lower()), None)
            china_opt = next((s for s in sourcing_options if "china" in s["origin_name"].lower()), None)

            memo_data = {
                "company": company,
                "commodity": mat_label,
                "destination": dest.name,
                "verdict_commit_pct": rec['recommended_commit_now_pct'],
                "verdict_tonnes": rec['recommended_tonnes_now'],
                "verdict_spend_cr": round(rec['recommended_tonnes_now'] * unit_price_inr / 1e7, 2),
                "verdict_benchmark_price": unit_price_inr,
                "verdict_lead_time": "18–24 Days (Domestic Mill Rolling Slot)",
                "operational_usable_stock": usable_stock,
                "operational_pipeline": pipeline_supply,
                "operational_current_runway": round(coverage_weeks, 1),
                "operational_policy_floor": round(policy_weeks, 1),
                "operational_deficit_tonnes": round(deficit_to_floor_t, 0),
                "operational_post_runway": round(post_order_coverage, 1),
                "operational_status": "DEFICIT" if coverage_weeks < policy_weeks else "HEALTHY",
                "arbitrage_domestic_price": unit_price_inr,
                "arbitrage_best_import_price": (round(best_import["landed_local"], 0)
                    if best_import and best_import.get("landed_local") is not None else None),
                "arbitrage_best_import_origin": best_import["origin_name"] if best_import else None,
                "arbitrage_unavailable_reason": (
                    None if best_import else
                    "No import origin cleared the tariff ranking, so there is no "
                    "import parity to compare against. Domestic is the only priced option."),
                "arbitrage_domestic_savings": (round(best_import["landed_local"] - unit_price_inr, 0)
                    if best_import and best_import.get("landed_local") is not None else None),
                "arbitrage_japan_landed": round(japan_opt["landed_local"], 0) if japan_opt and japan_opt.get("landed_local") is not None else None,
                "arbitrage_vietnam_landed": round(vietnam_opt["landed_local"], 0) if vietnam_opt and vietnam_opt.get("landed_local") is not None else None,
                "arbitrage_china_landed": round(china_opt["landed_local"], 0) if china_opt and china_opt.get("landed_local") is not None else None,
                "negotiation_batna": (f"{best_import['origin_name']} Landed Parity"
                    if best_import else "No ranked import origin available"),
                "negotiation_batna_price": (round(best_import["landed_local"], 0)
                    if best_import and best_import.get("landed_local") is not None else None),
                "negotiation_credit_terms_lakhs": round(credit_30d_val / 1e5, 1),
                "negotiation_flexibility_lakhs": round(flex_val / 1e5, 1),
                "negotiation_total_lakhs": round((credit_30d_val + flex_val) / 1e5, 1),
                "four_exchange_audit": basis_audit,
            }

            # Structured Reasoning Stages
            _sb = price_series_basis.split(chr(8212), 1)
            _series_name = (_sb[1].split(".")[0].strip() if len(_sb) > 1 else "").lower()                 or "the price series"
            reasoning_stages = [
                {
                    "stage_num": 1,
                    "title": "Inventory Runway & Stockout Risk Audit",
                    "status": "PASS" if coverage_weeks >= policy_weeks else "ACTION_REQUIRED",
                    "metric": f"{coverage_weeks:.2f} Weeks Coverage (Policy: {policy_weeks:.1f}w Floor)",
                    "finding": (
                        f"Unrestricted on-hand stock ({usable_stock:,.0f}t) plus pipeline supply ({pipeline_supply:,.0f}t) "
                        f"less quarantined safety stock ({safety_stock:,.0f}t) yields {available_tonnes:,.0f}t net available. "
                        f"Weekly consumption rate of {weekly_burn:.1f}t/wk results in {coverage_weeks:.1f} weeks runway. "
                        f"Operating buffer is {'below target policy by ' + str(round(policy_weeks - coverage_weeks, 1)) + 'w' if coverage_weeks < policy_weeks else 'above safety policy floor'}."
                    )
                },
                {
                    "stage_num": 2,
                    "title": "Cross-Exchange Basis & Tariff Audit (LME / MCX / CME; SHFE unavailable)",
                    "status": "COMPLETED",
                    "metric": f"Basis Verdict: {basis_audit['verdict']}",
                    "finding": basis_audit["audit_trail"]
                },
                {
                    # "PROXY — CME US Midwest HRC futures (USD/short ton). …"
                    # the name is the clause after the dash, not the tag before it.
                    "stage_num": 3,
                    "title": "Probabilistic Market Risk & Regret Analysis",
                    "status": "AUDITED",
                    "metric": f"30d Upward Probability: {fc.prob_up*100:.1f}% | Regret Exposure: ₹{rec['expected_regret_of_waiting_inr']:,.0f}",
                    "finding": (
                        f"The 30-day band is modelled on {_series_name} and only its "
                        f"RETURNS carry over, so it is stated as relative moves rather than "
                        f"rupee levels beside the domestic benchmark: "
                        f"P10 {(fc.p10/fc.spot - 1)*100:+.1f}%, P50 {(fc.p50/fc.spot - 1)*100:+.1f}%, "
                        f"P90 {(fc.p90/fc.spot - 1)*100:+.1f}%. Applied to ₹{unit_price_inr:,.0f}/t that spans "
                        f"₹{unit_price_inr*fc.p10/fc.spot:,.0f}–₹{unit_price_inr*fc.p90/fc.spot:,.0f}/t. "
                        f"{price_series_basis} "
                        f"Probability of a steady or rising price over the horizon: {fc.prob_up*100:.1f}%."
                    )
                },
                {
                    "stage_num": 4,
                    "title": "Executive Decision & Allocation Synthesis",
                    "status": "RECOMMENDED" if engine_feasible else "ACTION_REQUIRED",
                    "metric": (
                        f"Commit {rec['recommended_commit_now_pct']}% ({rec['recommended_tonnes_now']:,.0f} MT) Now"
                        if engine_feasible else
                        f"Escalate — {rec['recommended_tonnes_now']:,.0f} MT is the largest action and still falls short"
                    ),
                    "finding": (
                        f"Allocating {rec['recommended_tonnes_now']:,.0f} MT moves post-order coverage to "
                        f"{post_order_coverage:.1f} weeks against the {policy_weeks:.1f}-week floor. "
                        + ("This secures plant operations through the next manufacturing cycle."
                           if engine_feasible else
                           f"That is still short of the floor: {engine_selection_basis} "
                           f"Placing the order remains the least-bad action, but it does not close the gap — "
                           f"raise the requirement, pull delivery dates forward, or accept the shortage.")
                    )
                }
            ]

            # Multi-Option Decision Trade-off Matrix
            opt_a_spend = rec["recommended_tonnes_now"] * unit_price_inr
            opt_b_vol = round(monthly_demand * 0.5, 0)
            opt_b_spend = opt_b_vol * unit_price_inr
            opt_b_cov = round(coverage_weeks + (opt_b_vol / weekly_burn), 1)

            tradeoff_matrix = [
                {
                    "option_id": "OPT-A",
                    "option_name": f"Recommended Strategy: Commit {rec['recommended_commit_now_pct']}% Now",
                    "volume_t": rec["recommended_tonnes_now"],
                    "capital_spend_inr": opt_a_spend,
                    "post_coverage_weeks": post_order_coverage,
                    # Stockout PROBABILITY is not modelled: that needs a demand
                    # distribution and a lead-time distribution, neither of which
                    # this system estimates. What IS computable is the resulting
                    # coverage and whether it clears the policy floor.
                    "stockout_risk": None,
                    "coverage_after_weeks": post_order_coverage,
                    "clears_policy_floor": bool(post_order_coverage >= policy_weeks),
                    "strategic_tradeoff": "Restores coverage above the policy floor and fixes price at today's benchmark. Shutdown risk is reduced, not eliminated — residual risk depends on supplier delivery performance, which is not modelled here.",
                    "is_recommended": True,
                },
                {
                    "option_id": "OPT-B",
                    "option_name": "Alternative Strategy: Partial Split Buy (50% Now / 50% Defer)",
                    "volume_t": opt_b_vol,
                    "capital_spend_inr": opt_b_spend,
                    "post_coverage_weeks": opt_b_cov,
                    "stockout_risk": None,
                    "coverage_after_weeks": opt_b_cov,
                    "clears_policy_floor": bool(opt_b_cov >= policy_weeks),
                    "strategic_tradeoff": "Preserves ₹" + f"{opt_b_spend/1e7:.2f} Cr working capital for 30 days, but leaves half of demand exposed to potential price surges and rolling slot unavailability.",
                    "is_recommended": False,
                },
                {
                    "option_id": "OPT-C",
                    "option_name": "Conservative Alternative: 100% Deferral (Wait 30 Days)",
                    "volume_t": 0.0,
                    "capital_spend_inr": 0.0,
                    "post_coverage_weeks": coverage_weeks,
                    "stockout_risk": None,
                    "coverage_after_weeks": coverage_weeks,
                    "clears_policy_floor": bool(coverage_weeks >= policy_weeks),
                    "strategic_tradeoff": "Zero cash outlay today, but severely breaches minimum inventory buffer floor and exposes plant to emergency spot buying premiums.",
                    "is_recommended": False,
                }
            ]

            # Duty gate reflects the tariff layer's OWN verified flag rather than
            # asserting compliance. Both Indian HS codes are verified=False pending
            # customs sign-off, so this gate must not read COMPLIANT.
            try:
                import tariff as _T
                _reg = _T.VERIFIED_OVERRIDES.get((dest.iso3, "*", hs6))
                _verified = bool(_reg and _reg.get("verified"))
                _sc = _T.resolve_surcharges(dest.iso3, "*", hs6)
                _sc_txt = ", ".join(f"{x['kind']} {x['pct']}%" for x in _sc) or "none on file"
                duty_gate_status = "COMPLIANT" if _verified else "UNVERIFIED"
                duty_gate_measured = f"HS {hs6}: surcharges {_sc_txt}"
                duty_gate_note = (
                    "Duty rates confirmed against the destination's tariff schedule."
                    if _verified else
                    "Duty rates are NOT customs-verified. WITS/TRAINS corroborates them, "
                    "but a customs professional has not signed off, so no import "
                    "recommendation here is decision-grade.")
            except Exception as _e:
                duty_gate_status = "UNVERIFIED"
                duty_gate_measured = "tariff layer unavailable"
                duty_gate_note = f"Could not evaluate duty status: {type(_e).__name__}"

            # Corporate Policy Validation Gates
            policy_validation_gates = [
                {
                    "gate_id": "GATE-01",
                    "policy_name": "Minimum Production Runway Floor",
                    "threshold": f"Must maintain ≥ {policy_weeks:.1f} weeks operational coverage",
                    "measured_value": f"Current: {coverage_weeks:.1f}w -> Post-Order: {post_order_coverage:.1f}w",
                    "status": ("COMPLIANT" if engine_feasible
                               else ("BREACH" if engine_feasible_count == 0 else "BREACH")),
                    "audit_note": (
                        f"Post-order coverage {post_order_coverage:.1f}w vs floor "
                        f"{policy_weeks:.1f}w, evaluated by the decision engine over the "
                        f"deferral window. {engine_feasible_count} of "
                        f"{len(rec['options'])} options clear the floor. "
                        f"{engine_selection_basis} "
                        f"Coverage is a deterministic ratio, not a probability — no "
                        f"interruption risk figure is claimed.")
                },
                {
                    "gate_id": "GATE-02",
                    "policy_name": "Treasury Working Capital Ceiling",
                    "threshold": f"Quarterly allocation limit for {mat_type}",
                    "measured_value": f"Commitment: ₹{opt_a_spend/1e7:.2f} Cr",
                    "status": "NOT_EVALUATED",
                    "audit_note": ("No working-capital limit has been supplied, so this "
                                   "gate cannot pass or fail. Provide the treasury ceiling "
                                   "to enable it.")
                },
                {
                    "gate_id": "GATE-03",
                    "policy_name": "Trade Law & Anti-Dumping Safeguard Compliance",
                    "threshold": "DGTR Notification / Bilateral FTA origin vetting",
                    "measured_value": duty_gate_measured,
                    "status": duty_gate_status,
                    "audit_note": duty_gate_note
                }
            ]

            # Build the longer-horizon advisory from the same freshly fetched
            # history. A simulated threshold must never create an actual PAR.
            price_obs = zc if mat_type == "ZINC_SHG" else (hrc_obs if dest.iso3 == "IND" else cme_obs)
            observed = {"source": price_obs.source, "observed_at": price_obs.observed_at,
                        "retrieved_at": price_obs.retrieved_at, "quality": price_obs.quality}
            history_obs = zc if mat_type == "ZINC_SHG" else (_cme_obs if dest.iso3 == "IND" else cme_obs)
            observed["history_observed_at"] = history_obs.observed_at
            observed["history_source"] = history_obs.source
            outlook = OUTLOOK.build_outlook(
                inputs=outlook_inputs, material=mat_type, destination=dest.iso3, hs6=hs6,
                prices=hist_series, benchmark=unit_price_inr, observation=observed,
                series_basis=price_series_basis, monthly_demand=monthly_demand,
                usable_stock=usable_stock, safety_stock=safety_stock, policy_weeks=policy_weeks,
                ranked=ranked, fx=fx_val)
            if outlook_inputs["demo_threshold_crossed"]:
                from live_agent import ActionRequest
                def create_request(**fields):
                    return ActionRequest(action_id="DEMO-NO-ORDER", supplier_name="Research shortlist only",
                        total_commitment_inr=fields["recommended_tonnes"] * fields["unit_price_inr"],
                        approval_state="DEMO_ONLY", created_at=dt.datetime.now(dt.timezone.utc).isoformat(), **fields)
            else:
                create_request = AGENT.create_action_request
            action_req = create_request(
                material_id=mat_type,
                plant=f"{dest.name} Manufacturing Plant",
                recommended_tonnes=rec["recommended_tonnes_now"],
                supplier_id="SUP-DOM-01",
                unit_price_inr=unit_price_inr,
                order_type="COMMITMENT_ORDER" if rec["recommended_commit_now_pct"] > 0 else "DEFER_ORDER",
                rationale=(
                    f"Coverage at {coverage_weeks:.1f} weeks against {policy_weeks:.1f}w policy. "
                    f"Optimal decision is to commit {rec['recommended_commit_now_pct']}% ({rec['recommended_tonnes_now']:,.0f}t) "
                    f"at benchmark ₹{unit_price_inr:,.0f}/t to secure production buffer without carrying excess inventory."
                ),
                binding_constraint=rec["binding_constraint"],
            )

            # Executive Business Synthesis
            exec_summary = {
                "company_name": company,
                "material_label": mat_label,
                "destination": dest.name,
                "benchmark_price_local": unit_price_inr,
                "benchmark_source": benchmark_source,
                "coverage_weeks": coverage_weeks,
                "usable_stock_t": usable_stock,
                "pipeline_supply_t": pipeline_supply,
                "net_requirement_t": round(net_need_t, 1),
                # DISCLOSURES — these were computed and then dropped. A caveat
                # written inside the engine does not make the DISPLAYED
                # recommendation transparent, which was the whole point.
                "price_series_basis": price_series_basis,
                "purchase_outlook": outlook,
                "decision_disclosure": {
                    "selection_basis": rec.get("selection_basis"),
                    "feasible_option_count": rec.get("feasible_option_count"),
                    "total_options": len(rec.get("options", [])),
                    "objective_is_linear": rec.get("objective_is_linear"),
                    "objective_shape_note": rec.get("objective_shape_note"),
                    "stockout_probability": rec.get("stockout_probability"),
                    "stockout_probability_note": rec.get("stockout_probability_note"),
                    "binding_constraint": rec.get("binding_constraint"),
                    "coverage_after_weeks": post_order_coverage,
                    "engine_says_feasible": engine_feasible,
                },
                "forecast_calibration": {
                    "empirical_coverage": fc.calibration.get("empirical_coverage"),
                    "nominal_coverage": fc.calibration.get("nominal_coverage"),
                    "verdict": fc.calibration.get("verdict"),
                    "effective_independent_windows":
                        fc.calibration.get("effective_independent_windows"),
                    "independence_note": fc.calibration.get("independence_note"),
                },
                "recommendation": {
                    # A verdict of "COMMIT ... NOW" over a plan the engine has
                    # marked infeasible is the same divergence the disclosure
                    # block exists to expose. The headline states the escalation.
                    "verdict": (
                        f"COMMIT {rec['recommended_commit_now_pct']}% "
                        f"({rec['recommended_tonnes_now']:,.0f} T) NOW"
                        if engine_feasible else
                        f"ESCALATE — {rec['recommended_tonnes_now']:,.0f} T is the largest "
                        f"action available and still leaves coverage below the "
                        f"{policy_weeks:.1f}w floor"
                    ),
                    "commit_now_pct": rec["recommended_commit_now_pct"],
                    "tonnes_now": rec["recommended_tonnes_now"],
                    "tonnes_deferred": rec["recommended_tonnes_deferred"],
                    "expected_spend_local": round(rec["recommended_tonnes_now"] * unit_price_inr, 0),
                    "binding_constraint": rec["binding_constraint"],
                    "confidence": rec["confidence"].upper(),
                    "regret_of_waiting_local": rec["expected_regret_of_waiting_inr"],
                },
                "forecast": {
                    "spot": fc.spot,
                    "p10": fc.p10,
                    "p50": fc.p50,
                    "p90": fc.p90,
                    "prob_up": round(fc.prob_up * 100, 1),
                    "calibration_verdict": fc.calibration.get("verdict", "UNINFORMATIVE"),
                    "empirical_coverage": fc.calibration.get("empirical_coverage"),  # None if not measured
                },
                "global_sourcing": sourcing_options,
                "negotiation_levers": {
                    "annual_spend_local": annual_spend,
                    "credit_terms_30d_saving_local": credit_30d_val,
                    "volume_flexibility_option_local": flex_val,
                    "total_levers_on_table_local": credit_30d_val + flex_val,
                },
                "action_request": action_req.to_dict(),
                "consulting_memo": consulting_memo,
                "memo_data": memo_data,
                "reasoning_stages": reasoning_stages,
                "tradeoff_matrix": tradeoff_matrix,
                "policy_validation_gates": policy_validation_gates,
            }

            self._send_json({"status": "success", "data": exec_summary})
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Endpoint not found")


def run_server(port: int = 8765):
    # Keep the UI reachable while slower trade sources warm in the background.
    # A public-source delay must not make the entire local server disappear.
    import connectors as C   # imported per-scope elsewhere in this module
    def warm():
        print("  Warming public-data caches in background...", flush=True)
        try:
            for k, v in C.warm_caches("IND").items():
                print(f"    {k:<14} {v}", flush=True)
        except Exception as e:
            print(f"    warm-up skipped: {type(e).__name__}: {e}", flush=True)
    threading.Thread(target=warm, daemon=True, name="PublicDataWarmup").start()

    AGENT.start_background_monitors()
    # The focused workspace creates advice only. Do not seed an unrelated
    # mock approval request merely because the server started.

    server_address = ("", port)
    httpd = ThreadingHTTPServer(server_address, ProcurementRequestHandler)
    print(f"\n{'='*72}")
    print(f"  PROCUREMENT DESK LIVE COCKPIT RUNNING")
    print(f"  URL: http://localhost:{port}/")
    print(f"  Connected to live market streams, staleness watchdog & ERP inbox")
    print(f"{'='*72}\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    except Exception as e:
        print(f"\n[SERVER EXCEPTION] {e}", flush=True)
        import traceback
        traceback.print_exc()
    finally:
        AGENT.stop_background_monitors()
        httpd.server_close()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    run_server(port)

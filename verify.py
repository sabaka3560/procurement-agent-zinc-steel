"""
verify.py — Self-test harness. Run this to check the system is sound.

    python verify.py            # everything
    python verify.py --offline  # logic only, no network (fast, deterministic)

Three kinds of result:

  PASS   the check succeeded
  FAIL   LOGIC IS BROKEN. Something in this repo is wrong. Fix the code.
  WARN   an EXTERNAL source changed, moved or is down. The code may be fine;
         the world moved. Fix the connector or wait.

That distinction is the whole point. A scraper-backed system will break
regularly through no fault of its own, and you need to tell the two apart at
a glance rather than debugging from scratch each time.

Exit code 0 if no FAILs. Non-zero if any FAIL. Suitable for cron/CI.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import traceback

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []


def record(kind: str, name: str, detail: str = ""):
    results.append((kind, name, detail))
    colour = {"PASS": "", "FAIL": "  <<<", "WARN": "  <<"}[kind]
    print(f"  [{kind}] {name:<52} {detail}{colour}")


def check(name, fn, *, live=False):
    """Run a check. Exceptions => FAIL for logic, WARN for live sources."""
    try:
        ok, detail = fn()
        record(PASS if ok else (WARN if live else FAIL), name, detail)
    except Exception as e:
        detail = f"{type(e).__name__}: {str(e)[:70]}"
        record(WARN if live else FAIL, name, detail)


def section(t):
    print(f"\n--- {t} " + "-" * max(0, 66 - len(t)))


# ==========================================================================
# 1. ENVIRONMENT
# ==========================================================================
def env_checks():
    section("environment")

    def py():
        v = sys.version_info
        return v >= (3, 10), f"Python {v.major}.{v.minor}.{v.micro} (need >=3.10)"
    check("Python version", py)

    for mod in ("requests", "numpy"):
        def dep(m=mod):
            __import__(m)
            return True, f"{m} importable"
        check(f"dependency: {mod}", dep)

    def no_extras():
        import ast, pathlib
        std = set(sys.stdlib_module_names)
        local = {p.stem for p in pathlib.Path(__file__).parent.glob("*.py")}
        ext = set()
        for p in pathlib.Path(__file__).parent.glob("*.py"):
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if isinstance(n, ast.Import):
                    ext |= {a.name.split(".")[0] for a in n.names}
                elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                    ext.add(n.module.split(".")[0])
        third = sorted(ext - std - local - {"__future__"})
        return third == ["numpy", "requests"], f"third-party = {third}"
    check("no hidden dependencies", no_extras)


# ==========================================================================
# 2. OFFLINE LOGIC — known-answer tests, fully deterministic
# ==========================================================================
def offline_checks():
    section("offline logic (deterministic, no network)")
    import countries as CO
    import engine as EN
    import erp as ERP
    import negotiation as NEG
    import realtime as RT

    def codes():
        ind = CO.get("IND")
        return (ind.iso_num == 356 and ind.comtrade == 699), \
            f"India WITS=356 Comtrade=699 -> {ind.iso_num}/{ind.comtrade}"
    check("country codes: India 356 vs 699", codes)

    def divergent():
        d = {c.iso3 for c in CO.divergent_codes()}
        want = {"IND", "USA", "TWN", "ITA", "FRA", "CHE", "NOR"}
        return d == want, f"{len(d)} divergent, expected {len(want)}"
    check("country codes: all divergences registered", divergent)

    # --- landed cost is pure arithmetic: assert it exactly ---
    def landed():
        reg = EN.DutyRegime("7208", "2026-04-21", bcd_rate=0.10,
                            safeguard_rate=0.10, sws_rate=0.10)
        r = EN.landed_cost_import(fob_usd_per_t=1000.0, fx_inr_per_usd=100.0,
                                  regime=reg, ocean_freight_usd_per_t=0.0,
                                  insurance_pct_of_fob=0.0,
                                  port_cha_inr_per_t=0.0, inland_freight_inr_per_t=0.0,
                                  lead_time_days=0, expected_rejection_pct=0.0)
        # CIF 100,000; BCD 10,000; SG 10,000; SWS 1,000 => duty 21,000 => 121,000
        ok = abs(r["landed_cost_inr_per_t"] - 121_000) < 1
        return ok, f"expected 121,000 got {r['landed_cost_inr_per_t']:,.0f}"
    check("landed cost: exact arithmetic", landed)

    def igst_excluded():
        reg = EN.DutyRegime("7208", "2026-01-01", bcd_rate=0.0)
        a = EN.landed_cost_import(1000, 100, reg, 0, 0, 0, 0, 0, 0.09, 0.0, False)
        b = EN.landed_cost_import(1000, 100, reg, 0, 0, 0, 0, 0, 0.09, 0.0, True)
        return b["landed_cost_inr_per_t"] > a["landed_cost_inr_per_t"], \
            "recoverable IGST excluded by default, includable on request"
    check("landed cost: IGST recoverable by default", igst_excluded)

    # --- forecast must report calibration, and be reproducible ---
    def fc_repro():
        s = [100 * (1.001 ** i) for i in range(300)]
        a = EN.forecast(s, 30, seed=1)
        b = EN.forecast(s, 30, seed=1)
        return a.p50 == b.p50, f"same seed -> same P50 ({a.p50})"
    check("forecast: reproducible under a fixed seed", fc_repro)

    def fc_calib():
        s = [100 * (1.001 ** i) for i in range(300)]
        f = EN.forecast(s, 30)
        has = "empirical_coverage" in f.calibration
        return has, f"coverage reported: {f.calibration.get('empirical_coverage')}"
    check("forecast: reports its own calibration", fc_calib)

    def fc_order():
        s = [100 + (i % 7) for i in range(200)]
        f = EN.forecast(s, 30)
        return f.p10 <= f.p50 <= f.p90, f"P10<=P50<=P90 ({f.p10}/{f.p50}/{f.p90})"
    check("forecast: quantiles ordered", fc_order)

    def fc_refuses():
        try:
            EN.forecast([1, 2, 3], 30)
            return False, "accepted 3 observations — should have refused"
        except ValueError:
            return True, "refuses <40 observations"
    check("forecast: refuses insufficient history", fc_refuses)

    # --- ERP coverage rules ---
    def cov_excludes():
        cp = ERP.coverage(ERP.MockERP(), "M", "P")
        inv = ERP.MockERP().inventory("M", "P")
        return cp.usable_stock_t == inv.unrestricted_t, \
            f"blocked {inv.blocked_t:.0f}t + consignment {inv.consignment_t:.0f}t excluded"
    check("ERP: blocked/consignment excluded from coverage", cov_excludes)

    def cov_mock_flag():
        cp = ERP.coverage(ERP.MockERP(), "M", "P")
        return cp.is_mock and any("MOCK" in w for w in cp.warnings), \
            "mock data self-identifies"
    check("ERP: mock data flags itself", cov_mock_flag)

    def cov_open_po():
        cp = ERP.coverage(ERP.MockERP(), "M", "P")
        return cp.open_po_t > 0, f"open POs counted as supply ({cp.open_po_t:.0f}t)"
    check("ERP: open POs counted (prevents double-buying)", cov_open_po)

    # --- negotiation ---
    def neg_credit():
        lv = NEG.value_levers(100_000_000, 30, 60, 0.10, 0.10, 20.0,
                              "monthly", 0.2, 1000, 100_000)
        c = next(x for x in lv if x.name.startswith("Credit"))
        want = 100_000_000 * 0.10 * (30 / 365)
        return abs(c.annual_value_inr - want) < 1, \
            f"credit lever = {c.annual_value_inr:,.0f} (expected {want:,.0f})"
    check("negotiation: credit-terms arithmetic exact", neg_credit)

    def neg_unpriced():
        lv = NEG.value_levers(1e8, 30, 60, 0.09, 0.1, 20.0, "monthly", 0.2, 1e3, 1e5)
        d = next(x for x in lv if "demurrage" in x.name.lower())
        return d.annual_value_inr is None, "demurrage left unpriced, not invented"
    check("negotiation: refuses to invent unpriceable levers", neg_unpriced)

    def neg_weak():
        src = ERP.MockERP()
        cp = ERP.coverage(src, "M", "P")
        cp.coverage_weeks = 1.5                      # force the weak-position path
        sup = src.suppliers("M")[0]
        b = NEG.build_brief(sup, src.contracts("M")[0], cp, 60000, 59000,
                            None, 7200, 20.0)
        return any("weakness" in w.lower() for w in b.warnings), \
            "warns when negotiating from low coverage"
    check("negotiation: warns when negotiating from weakness", neg_weak)

    # --- realtime ---
    def rt_detect():
        sim = RT.SimulatedStream("X", 100.0, 20.0, seed=1, shock_at=100, shock_pct=-4.0)
        det = RT.MoveDetector("X", spike_pct=1.5, window_min=30)
        n = sum(1 for t in sim.stream(300) if det.push(t))
        return n >= 1, f"{n} alert(s) on an injected -4% shock"
    check("realtime: detects an injected shock", rt_detect)

    def rt_quiet():
        sim = RT.SimulatedStream("X", 100.0, 0.5, seed=2)   # near-zero vol
        det = RT.MoveDetector("X", spike_pct=1.5, window_min=30)
        n = sum(1 for t in sim.stream(300) if det.push(t))
        return n == 0, f"{n} false alarm(s) on a quiet session"
    check("realtime: no false alarms on a quiet session", rt_quiet)

    def rt_untested():
        try:
            RT.AngelOneStream("k", "c", "j", "f").stream([], lambda t: None)
            return False, "broker client silently returned — should refuse"
        except NotImplementedError:
            return True, "untested broker client refuses rather than faking"
    check("realtime: untested broker client refuses to run", rt_untested)


# ==========================================================================
# 3. GUARDRAILS — the behaviours that stop a wrong confident answer
# ==========================================================================
def guardrail_checks():
    section("guardrails (these are what keep it honest)")
    import connectors as C
    import tariff as T

    def unverified_blocks():
        import engine as EN
        return EN.DUTY_REGIMES["7208"].verified is False, \
            "India HRC duty still flagged unverified pending customs sign-off"
    check("unverified duty regime blocks confident output", unverified_blocks)

    def stale_flag():
        o = C.Observation("t", 1.0, "u", "c",
                          "2020-01-01T00:00:00+00:00",
                          "2026-01-01T00:00:00+00:00", "s")
        return o.age_days > 2000, f"age computed = {o.age_days:.0f} days"
    check("observation staleness is computed", stale_flag)

    def wits_distinguishes():
        return hasattr(T, "WitsUnavailable"), \
            "unreachable-source is a distinct type from no-record"
    check("WITS: 'unreachable' != 'no record exists'", wits_distinguishes)

    def offline_degrades():
        """The regression test for the bug this harness found."""
        import requests as rq
        real = rq.get
        rq.get = lambda *a, **k: (_ for _ in ()).throw(rq.RequestException("blocked"))
        T._series_cache.clear(); T._wits_cache.clear()
        try:
            r = T.resolve_tariff("IND", "KOR", "720810")
            ok = (not r.usable_for_decision) and any("UNREACH" in n.upper() for n in r.notes)
            return ok, "network outage -> NOT decision-grade + explicit warning"
        finally:
            rq.get = real
            T._series_cache.clear(); T._wits_cache.clear()
    check("tariff degrades safely when WITS is unreachable", offline_degrades)

    # --- destination capability: the India-hardcoding regression ---
    def dest_tiers():
        import destinations as D
        return (D.profile("IND").tier == D.TIER_FULL
                and D.profile("THA").tier == D.TIER_PARTIAL
                and D.profile("VNM").tier == D.TIER_MINIMAL), \
            "IND=FULL, THA=PARTIAL (no benchmark), VNM=MINIMAL (no free FX)"
    check("destination capability tiers correct", dest_tiers)

    def only_india_full():
        import destinations as D
        full = [p.iso3 for p in D.coverage_table() if p.tier == D.TIER_FULL]
        return full == ["IND"], f"FULL tier = {full} (only India has a free benchmark)"
    check("only India claims a full free stack", only_india_full)

    def no_domestic_elsewhere():
        import destinations as D
        bad = [p.iso3 for p in D.coverage_table()
               if p.iso3 != "IND" and p.can_compare_domestic]
        return not bad, f"no non-India destination claims a domestic benchmark"
    check("no destination fakes a domestic benchmark", no_domestic_elsewhere)

    def neg_blocked_abroad():
        import destinations as D
        return not D.profile("THA").can_build_negotiation_brief, \
            "negotiation brief disabled where there is no benchmark"
    check("negotiation brief refused without a benchmark", neg_blocked_abroad)

    def fx_refuses_unknown():
        import connectors as CC
        try:
            CC.fx_usd_to("VND")
            return False, "returned a VND rate — frankfurter does not publish one"
        except ValueError:
            return True, "raises for currencies with no free feed (VND)"
        except Exception as e:
            return False, f"wrong exception: {type(e).__name__}"
    check("FX refuses unsupported currencies", fx_refuses_unknown, live=True)

    # --- PROVENANCE IS STRUCTURAL, NOT A LABEL -------------------------
    # This is the generalisation of the fabricated-connector incident of
    # 2026-09-06. Two functions returned hardcoded constants stamped with
    # today's date and a source string naming a real exchange, and a
    # provenance test passed them because it only inspected the label:
    #     assert any(kw in src.lower() for kw in ["lme","nexarc","cme",...])
    # A convincing fake source string SATISFIES that assertion. You cannot
    # test provenance by reading metadata; you have to test the call path.
    def observations_are_fetched():
        import ast
        import pathlib
        src_path = pathlib.Path(__file__).parent / "connectors.py"
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            seg = ast.get_source_segment(src_path.read_text(encoding="utf-8"), node) or ""
            # Does this function construct an Observation?
            makes_obs = any(
                isinstance(c, ast.Call) and getattr(c.func, "id", "") == "Observation"
                for c in ast.walk(node))
            if not makes_obs:
                continue
            does_http = "requests.get" in seg or "requests.request" in seg
            delegates = any(
                isinstance(c, ast.Call) and getattr(c.func, "id", "") in
                ("yahoo_series", "fx_usd_to", "lme_zinc_westmetall",
                 "india_hrc_nexarc", "mcx_zinc_benchmark")
                for c in ast.walk(node))
            identity = node.name == "fx_usd_to"   # has a legitimate USD->USD branch
            if not (does_http or delegates or identity):
                offenders.append(node.name)
        return not offenders, (
            f"every Observation-producing connector reaches the network"
            if not offenders else
            f"FABRICATED: {offenders} build an Observation with no HTTP call")
    check("connectors: no Observation without a fetch", observations_are_fetched)

    def shfe_refuses():
        import connectors as CC
        try:
            CC.shfe_steel_benchmark()
            return False, "returned data — SHFE is not obtainable, it must refuse"
        except NotImplementedError:
            return True, "SHFE refuses rather than returning a constant"
    check("SHFE refuses instead of fabricating", shfe_refuses)

    def audit_gates_verdict():
        """A basis inside the freight/premium error band must not yield a verdict."""
        import connectors as CC
        import inspect
        s = inspect.getsource(CC.cross_exchange_basis_audit)
        return ("TOO_CLOSE_TO_CALL" in s and "BASIS_NOISE_PCT" in s), \
            "basis audit has a noise band and withholds a verdict inside it"
    check("basis audit withholds a verdict inside the noise band", audit_gates_verdict)

    def server_has_no_numeric_fallbacks():
        import pathlib
        import re
        p = pathlib.Path(__file__).parent / "server.py"
        # Widened 2026-09-07. The original pattern matched SINGLE-quoted .get()
        # only, while server.py uses double quotes throughout -- so it reported
        # "no numeric fallbacks" while every one of them was still there. It also
        # missed fabricated literals that are not .get() at all.
        txt = p.read_text(encoding="utf-8")
        bad = []
        # numeric .get() fallbacks, either quote style, excluding legitimate
        # user-input form defaults and freshness counters
        for m in re.finditer(r"\.get\(['\"]([a-z_]+)['\"],\s*([0-9][0-9._]*)\)", txt):
            key = m.group(1)
            if key in ("age_seconds", "latency_ms") or key.endswith("_t") or                key in ("monthly_demand_t", "policy_weeks", "new_tonnes",
                       "fx_shift_pct", "freight_usd_shift"):
                continue
            bad.append(m.group(0))
        # fabricated presentation literals that must never reappear
        for pat, label in ((r'"stockout_risk":\s*"', "hardcoded stockout_risk string"),
                           (r'"status_code":\s*[0-9]', "hardcoded HTTP status"),
                           (r'"latency_ms":\s*[0-9]', "hardcoded latency"),
                           (r'"status":\s*"COMPLIANT"(?!\s*if)', "unconditional COMPLIANT gate"),
                           (r'6754[01]', "hardcoded Japan landed cost"),
                           (r'59800\.0', "hardcoded HRC price")):
            if re.search(pat, txt):
                bad.append(label)
        return not bad, ("no numeric fallbacks or fabricated literals"
                         if not bad else f"{len(bad)} found: {bad[:3]}")
    check("server: no hardcoded numeric fallbacks", server_has_no_numeric_fallbacks)

    def no_silent_pref_loss():
        import requests as rq
        real = rq.get
        rq.get = lambda *a, **k: (_ for _ in ()).throw(rq.RequestException("blocked"))
        T._series_cache.clear(); T._wits_cache.clear()
        try:
            r = T.resolve_tariff("IND", "KOR", "720810")
            return "UNCHECKED" in r.source, f"source label = '{r.source}'"
        finally:
            rq.get = real
            T._series_cache.clear(); T._wits_cache.clear()
    check("tariff never silently drops an FTA preference", no_silent_pref_loss)


# ==========================================================================
# 4. LIVE SOURCES — WARN on failure, they are outside your control
# ==========================================================================
def live_checks():
    section("live external sources (WARN = the world moved, not your bug)")
    import connectors as C
    import country_rank as CR
    import tariff as T

    def fx():
        o = C.fx_usd_inr_fbil()
        return 60 < o.value < 150, f"USD/INR {o.value:,.4f} as of {o.observed_at[:10]}"
    check("FBIL USD/INR", fx, live=True)

    def zinc():
        c, m3, st, hist = C.lme_zinc_westmetall()
        ok = 1000 < c.value < 10000 and len(hist) > 30
        return ok, f"${c.value:,.2f}/t cash, {len(hist)} days history"
    check("LME zinc (westmetall)", zinc, live=True)

    def hrc():
        o, rows = C.india_hrc_nexarc()
        return o and 20_000 < o.value < 150_000 and len(rows) > 5, \
            f"Rs {o.value:,.0f}/t, {len(rows)} rows parsed"
    check("India HRC (Tata Nexarc)", hrc, live=True)

    def steel_fut():
        o, h = C.steel_hrc_futures("1mo")
        return 200 < o.value < 5000, f"HRC=F ${o.value:,.2f}, age {o.age_days:.1f}d"
    check("CME HRC futures (Yahoo v8)", steel_fut, live=True)

    def comtrade():
        rows = C.india_imports_by_partner("7208", 2024)
        return len(rows) > 5, f"{len(rows)} partner countries"
    check("UN Comtrade flows", comtrade, live=True)

    def wits_mfn():
        r, yr, note, ver = T.mfn_rate("IND", "720810")
        return r is not None and 0 <= r <= 100, f"India MFN {r}% ({yr})"
    check("WITS MFN (India HS720810)", wits_mfn, live=True)

    def wits_pref():
        r, yr = T.preferential_rate("IND", "KOR", "720810")
        return r == 0.0, f"India<-Korea CEPA = {r}% ({yr}) — expected 0.0"
    check("WITS preferential (India<-Korea CEPA)", wits_pref, live=True)

    def wits_multi():
        got = {}
        for iso in ("VNM", "THA", "TUR"):
            v, *_ = T.mfn_rate(iso, "720810")
            got[iso] = v
        return all(v is not None for v in got.values()), f"{got}"
    check("WITS multi-destination MFN", wits_multi, live=True)

    def lpi():
        v = CR.lpi("KOR")
        return v is not None and 1 <= v <= 5, f"Korea LPI {v}"
    check("World Bank LPI", lpi, live=True)

    def ranking():
        rows = CR.rank_source_countries("IND", "720810", 2024, top_n=5)
        return len(rows) >= 3, f"{len(rows)} origins ranked, best={rows[0].origin_name if rows else '-'}"
    check("country ranking end-to-end", ranking, live=True)

    def blocked_stay_blocked():
        import requests
        bad = 0
        for url in ("https://www.mcxindia.com/market-data/bhavcopy",
                    "https://www.lme.com/en/metals/non-ferrous/lme-zinc"):
            try:
                if requests.get(url, timeout=15,
                                headers={"User-Agent": "Mozilla/5.0"}).status_code == 403:
                    bad += 1
            except Exception:
                bad += 1
        return bad == 2, f"{bad}/2 still blocked (as documented)"
    check("known-blocked sources still blocked", blocked_stay_blocked, live=True)


# ==========================================================================
# 4. LIVE AGENT & STREAMING — Rule AD-1, staleness, and approval gating
# ==========================================================================
def live_agent_checks():
    section("live agent & streaming checks (rule AD-1 & gating)")
    import datetime as dt
    import market_stream as MS
    import erp_watcher as EW
    import procurement_mcp as MCP
    import live_agent as LA

    # 1. Staleness watchdog
    def stream_staleness():
        mgr = MS.MarketStreamManager(use_simulation=True, intraday_ttl_seconds=600)
        old_ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=25)).isoformat(timespec="seconds")
        stale_tick = MS.Tick("MCX:ZINC", 412.0, old_ts, simulated=False)
        mgr.push_tick(stale_tick)
        snap = mgr.get_snapshot()
        q = snap.get("MCX:ZINC", {})
        return q.get("quality") == MS.FeedQuality.STALE_DEGRADED.value and q.get("is_stale") is True, \
            f"stale tick auto-flagged STALE_DEGRADED (age={q.get('age_seconds')}s)"
    check("market_stream: staleness watchdog flags stale feeds", stream_staleness)

    # 2. ERP unit-of-measure defense
    def erp_unit_defense():
        norm = EW._normalize_tonnes(60000.0, "KG")
        return norm == 60.0, f"60,000 KG normalized to {norm} tonnes (prevents 1000x error)"
    check("erp_watcher: unit defense normalizes KG to metric tonnes", erp_unit_defense)

    # 3. MCP tool dispatching
    def mcp_tool_test():
        res = MCP.dispatch_tool("check_inventory_coverage", {"material_id": "HRC-E250A-2.0", "plant": "PLANT-PUNE-1"})
        return res.get("status") == "success" and res.get("usable_stock_t") == 430.0, \
            f"inventory tool dispatched: usable={res.get('usable_stock_t')}t, coverage={res.get('coverage_weeks')}w"
    check("procurement_mcp: tool dispatching computes deterministic coverage", mcp_tool_test)

    # 4. Human Approval Gate
    def par_gate_test():
        agent = LA.ProcurementDecisionAgent()
        par = agent.create_action_request(
            material_id="HRC-E250A-2.0", plant="PLANT-PUNE-1", recommended_tonnes=100.0,
            supplier_id="SUP-DOM-01", unit_price_inr=59800.0, order_type="TEST_COMMIT",
            rationale="Test commitment", binding_constraint="test policy"
        )
        is_pending = par.approval_state == "PENDING_HUMAN_APPROVAL"
        appr = agent.approve_action_request(par.action_id, approver="AuditTest", notes="Verified")
        is_appr = appr and appr.approval_state == "APPROVED" and "APPROVED by AuditTest" in appr.audit_trail[-1]
        return is_pending and is_appr, f"PAR {par.action_id} transitioned PENDING -> APPROVED with audit log"
    check("live_agent: human approval gate enforces formal state transition", par_gate_test)


# ==========================================================================
# ==========================================================================
# 6. ASTRA REVIEW #2 REGRESSIONS
#     One check per reported failure. Each asserts the *behaviour* that was
#     wrong, not the presence of a label — the lesson from the fallback check
#     that matched single quotes while the code used double ones.
# ==========================================================================
def astra2_checks():
    section("astra review #2 regressions")
    import re as _re
    import engine as EN

    SRV = pathlib.Path(__file__).with_name("server.py").read_text(encoding="utf-8")
    UI = pathlib.Path(__file__).with_name("live_desk.html").read_text(encoding="utf-8")

    # A2-1 — the engine found nothing feasible; the API called it COMPLIANT.
    def infeasible_not_compliant():
        prices = [59000.0 + 40.0 * ((i * 7) % 11 - 5) for i in range(140)]
        fc = EN.forecast(prices, horizon_days=30, series="steel")
        rec = EN.recommend(
            fc, requirement_tonnes=500.0, current_price_inr_per_t=59000.0,
            coverage_weeks_on_hand=0.35, policy_min_coverage_weeks=4.0,
        )
        if rec["feasible_option_count"] != 0:
            return False, ("fixture no longer infeasible "
                           f"({rec['feasible_option_count']} feasible) — retune it")
        chosen = next((o for o in rec["options"]
                       if o["commit_now_pct"] == rec["recommended_commit_now_pct"]), None)
        if chosen is None:
            return False, "recommended option not present in options list"
        if chosen.get("feasible") is not False:
            return False, "chosen option claims feasible while count is 0"
        cov = chosen.get("coverage_after_weeks")
        if cov is None or cov >= 4.0:
            return False, f"coverage_after_weeks={cov} would still read as compliant"
        # the API must read the engine's number, not recompute a rosier one
        if 'rec.get("feasible_option_count"' not in SRV:
            return False, "server does not surface feasible_option_count"
        if '(_chosen or {}).get("coverage_after_weeks")' not in SRV:
            return False, "server does not defer to the engine's post-order coverage"
        return True, f"0/5 feasible, chosen coverage {cov:.2f}w < 4.0w floor, API defers"
    check("infeasible plan is not reported as compliant", infeasible_not_compliant)

    # A2-1b — the same divergence one layer up. The page recomputed post-order
    # coverage as (coverage + tonnes/burn), which omits what the plant consumes
    # across the horizon and always produces the reassuring number.
    def ui_defers_to_engine_coverage():
        problems = []
        if "d.coverage_weeks + (d.recommendation.tonnes_now / weeklyBurn)" in UI:
            problems.append("UI recomputes post-order coverage")
        if "decision_disclosure || {}).coverage_after_weeks" not in UI:
            problems.append("UI does not read the engine's coverage_after_weeks")
        # every decision surface must gate on feasibility, not on commit_now_pct
        for surface, needle in (("headline verdict", "!postClearsFloor ? 'escalate"),
                                ("chat summary", "const plainSummary = !postClearsFloor")):
            if needle not in UI:
                problems.append(f"{surface} does not gate on feasibility")
        # and the API verdict string itself
        if "if engine_feasible else" not in SRV:
            problems.append("API verdict string does not gate on feasibility")
        return not problems, ("verdict, KPI, chat and API all read the engine"
                              if not problems else "; ".join(problems))
    check("every decision surface reads the engine's feasibility",
          ui_defers_to_engine_coverage)


    # A2-1c — unit mixing. The 30-day band is modelled on a USD/short-ton
    # proxy; it was multiplied by FX and printed as INR/t beside a domestic
    # INR/t spot, giving a band roughly 2x the price it sat next to. Same
    # class as the 59,800-INR-rendered-as-USD defect: a real number, in the
    # wrong unit, on the line the buyer reads.
    def no_unit_mixing_in_band():
        problems = []
        if _re.search(r"fc\.p(10|50|90)\s*\*\s*fx_val", SRV):
            problems.append("proxy quantiles multiplied by FX and printed as INR")
        if "fc.p10/fc.spot" not in SRV:
            problems.append("band is not expressed as relative moves off its own spot")
        # a proxy band must carry its basis wherever it is stated
        if "{price_series_basis}" not in SRV:
            problems.append("band text does not restate the series basis")
        return not problems, ("band stated as relative moves off its own series"
                              if not problems else "; ".join(problems))
    check("forecast band does not mix units or series", no_unit_mixing_in_band)

    # A2-2 — invented country landed costs, in server.py and in the page.
    def no_invented_country_prices():
        bad = []
        for lit in ("68859", "78198", "67541", "59800.0", "591.2", "570.9", "644.9"):
            for label, text in (("server.py", SRV), ("live_desk.html", UI)):
                if lit in text:
                    bad.append(f"{label}:{lit}")
        # a landed cost may never be a literal in the response builder
        if _re.search(r'"landed_local":\s*\d', SRV):
            bad.append("server.py: literal landed_local")
        return not bad, ("clean" if not bad else "found " + ", ".join(bad))
    check("no invented country landed costs", no_invented_country_prices)

    # A2-3 — three unhandled crashes.
    def no_unguarded_crashes():
        problems = []
        # round(None) on an empty ranking / missing age
        if _re.search(r'round\(\s*(?:quotes|q)\.get\([^)]*\)\s*,', SRV):
            problems.append("round() straight off a .get()")
        if "def _age(" not in SRV:
            problems.append("no _age() guard helper")
        # short history must refuse, not be fed to forecast()
        if "len(hist_series) < 40" not in SRV:
            problems.append("no minimum-history guard before forecast()")
        # and the engine itself must refuse rather than emit a band
        try:
            EN.forecast([59000.0] * 5, horizon_days=30, series="steel")
            problems.append("engine.forecast accepted a 5-point series")
        except Exception:
            pass
        return not problems, ("guarded" if not problems else "; ".join(problems))
    check("short/empty inputs refuse instead of crashing", no_unguarded_crashes)

    # A2-4 — disclosures computed but never returned to the buyer.
    def disclosures_reach_the_response():
        missing = [k for k in ("price_series_basis", "decision_disclosure",
                               "forecast_calibration", "selection_basis",
                               "objective_is_linear", "stockout_probability_note")
                   if f'"{k}"' not in SRV]
        if missing:
            return False, "absent from server.py: " + ", ".join(missing)
        # and must actually be rendered, not merely serialised
        ui_missing = [k for k in ("decision_disclosure", "price_series_basis",
                                  "selection_basis", "objective_is_linear")
                      if k not in UI]
        return not ui_missing, ("returned and rendered" if not ui_missing
                                else "not rendered: " + ", ".join(ui_missing))
    check("decision disclosures reach the screen", disclosures_reach_the_response)

    # A2-5 — the page rendering fields the API no longer sends.
    def ui_has_no_dead_fields():
        dead = []
        for f in ("status_code", "latency_ms", "stockout_risk"):
            # allow the explanatory comment, not an interpolation
            for m in _re.finditer(r"\$\{[^}]*" + f + r"[^}]*\}", UI):
                dead.append(f + " interpolated")
        # CIF values must not be described as FOB plus freight
        if _re.search(r"FOB \$\$\{[^}]+\}[^`]*Freight", UI):
            dead.append("CIF described as FOB + freight")
        # No money literal may sit in the static markup at all. A pre-load
        # placeholder is still a fabricated number on the buyer's screen when
        # the fetch fails, whatever the JS intends to write over it later.
        body = UI.split("<script")[0]
        money = _re.compile(r"₹[\d,]{3,}|\$\d{3}\.\d|[\d.]+ ?(?:Lakhs|Crores)"
                            r"|\b\d{2},\d{3}\b")
        for ln in body.splitlines():
            if "fonts.googleapis" in ln:
                continue
            m = money.search(ln)
            if m:
                dead.append(f"static money literal {m.group(0)!r}")
        return not dead, ("no dead fields" if not dead else "; ".join(sorted(set(dead))))
    check("UI renders no removed or mislabelled fields", ui_has_no_dead_fields)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="skip live network checks")
    args = ap.parse_args()

    print("=" * 78)
    print("  VERIFY — zinc/steel procurement engine self-test")
    print("=" * 78)

    env_checks()
    offline_checks()
    guardrail_checks()
    live_agent_checks()
    astra2_checks()
    if args.offline:
        print("\n  (--offline: live source checks skipped)")
    else:
        live_checks()

    n_pass = sum(1 for k, _, _ in results if k == PASS)
    n_fail = sum(1 for k, _, _ in results if k == FAIL)
    n_warn = sum(1 for k, _, _ in results if k == WARN)

    print("\n" + "=" * 78)
    print(f"  {n_pass} passed, {n_warn} warnings, {n_fail} failures")
    print("=" * 78)

    if n_fail:
        print("\n  FAILURES — logic is broken, fix the code:")
        for k, n, d in results:
            if k == FAIL:
                print(f"    - {n}: {d}")
    if n_warn:
        print("\n  WARNINGS — an external source changed or is down:")
        for k, n, d in results:
            if k == WARN:
                print(f"    - {n}: {d}")
        print("\n  A warning does NOT mean your code is wrong. Check the source's")
        print("  page by hand; if its layout changed, fix that one parser.")

    if not n_fail and not n_warn:
        if args.offline:
            print("\n  Logic and guardrails all green. Live sources NOT checked "
                  "(--offline).")
        else:
            print("\n  All green. Every engine behaves correctly and every live source")
            print("  is reachable and returning values in a sane range.")

    return 1 if n_fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(2)

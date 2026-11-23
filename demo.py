"""
demo.py — Presenter-facing demo runner.

Built for standing in front of people. Three rules it obeys:

  1. IT NEVER CRASHES MID-DEMO. Every section is isolated. If a live source
     fails, that section says so calmly and the demo continues.
  2. IT CAN RUN WITHOUT THE INTERNET. `--snapshot` captures today's live data;
     `--cached` replays it instantly. Use cached mode in a boardroom with
     hostile wifi. The banner always says which mode you are in — you should
     never accidentally present cached data as live.
  3. IT IS PACED. `--pause` waits for a keypress between sections so you can
     talk over each one.

Usage
    python demo.py --snapshot     # do this the morning of the demo
    python demo.py --pause        # live, paced  (the normal way to present)
    python demo.py --cached --pause   # safe mode, no network needed
    python demo.py --auto         # run straight through, no pauses

Timing: roughly 7-9 minutes with pauses, ~25 seconds without.
See ../09_DEMO_RUNBOOK.md for what to actually say.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import traceback

SNAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_snapshot.json")
W = 84
MODE = "live"
_snapshot: dict = {}
_capture: dict = {}


# ---------------------------------------------------------------- plumbing
def rule(ch="="):
    print(ch * W)


def head(n, title, seconds=None):
    print()
    rule()
    t = f"  {n}. {title}"
    if seconds:
        print(f"{t}{' ' * max(1, W - len(t) - 10)}[~{seconds}s]")
    else:
        print(t)
    rule()


def say(s=""):
    print(s)


def pause(enabled):
    if enabled:
        try:
            input("\n        [enter] ")
        except (EOFError, KeyboardInterrupt):
            raise SystemExit(0)


def cached(key, fn):
    """
    Return the live value and capture it, or replay from the snapshot.

    In live mode an already-fetched key is reused rather than re-fetched, so a
    later section can reference an earlier section's data (and pass `lambda:
    None`) without hitting the network twice — or getting None.
    """
    if MODE == "cached":
        if key not in _snapshot:
            raise RuntimeError(f"'{key}' not in snapshot — re-run --snapshot")
        return _snapshot[key]
    if key in _capture:
        return _capture[key]
    val = fn()
    if val is None:
        raise RuntimeError(f"'{key}' was referenced before it was fetched")
    _capture[key] = val
    return val


def section(fn):
    """Isolate a section so one failure cannot kill the demo."""
    def wrapped(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:
            say()
            say(f"  [ this section could not run: {type(e).__name__}: {str(e)[:90]} ]")
            if MODE == "live":
                say("  [ tip: run  python demo.py --cached  to present from a snapshot ]")
        return None
    return wrapped


# ---------------------------------------------------------------- sections
@section
def s1_framing():
    head(1, "The question this answers")
    say()
    say('  Not:  "will zinc go up next week?"')
    say()
    say('  But:  "we need 550 t of this HRC spec inside 23 days.')
    say('         How much do we commit now, from which country,')
    say('         at what landed cost, and on what terms?"')
    say()
    say("  Those are different problems. The second one is the buyable one.")


@section
def s2_live_data(dest="IND"):
    import connectors as C
    import destinations as D
    head(2, "Live market data — free, no API keys", 5)
    prof = D.profile(dest)
    if dest != "IND":
        say()
        say(f"  NOTE: destination is {prof.name} (tier {prof.tier}), but the domestic")
        say(f"  price line below is INDIA-ONLY. There is no free equivalent of Tata")
        say(f"  Nexarc for {prof.name}. Sections 7 and 8 are India-only for the same")
        say(f"  reason. Sections 4 and 5 ARE destination-aware.")
    say()

    fx = cached("fx", lambda: {"v": C.fx_usd_inr_fbil().value,
                               "d": C.fx_usd_inr_fbil().observed_at[:10]})
    z = cached("zinc", lambda: (lambda t: {"cash": t[0].value, "m3": t[1].value,
                                           "stock": t[2].value, "d": t[0].observed_at[:10],
                                           "n": len(t[3])})(C.lme_zinc_westmetall()))
    h = cached("hrc", lambda: (lambda t: {"med": t[0].value, "n": len(t[1]),
                                          "rows": [dict(r) for r in t[1][:3]]})(C.india_hrc_nexarc()))

    say(f"  USD/INR (FBIL, India's official rate)   {fx['v']:>12,.4f}    {fx['d']}")
    say(f"  LME zinc cash                           ${z['cash']:>11,.2f}/t   {z['d']}")
    say(f"  LME zinc 3-month                        ${z['m3']:>11,.2f}/t")
    curve = z["cash"] - z["m3"]
    say(f"  LME zinc warehouse stock                {z['stock']:>12,.0f} t")
    say(f"     -> curve {curve:+,.2f}  "
        f"{'BACKWARDATION: spot is tight, deferring has a real cost' if curve > 0 else 'contango: deferral is cheaper'}")
    say()
    say(f"  India HR Coil — {z and h['n']} rows, by brand / city / grade / thickness:")
    for r in h["rows"]:
        say(f"     {r['brand']:<5} {r['city']:<7} {r['grade']:<8} {str(r['thickness_mm']):>9} mm"
            f"   Rs {r['price_inr_per_t']:>7,}/t")
    say()
    say("  Every one of those is free and needs no account. Four research reports")
    say("  concluded this data required a $25,000/year exchange licence.")


@section
def s3_trap():
    import connectors as C
    head(3, "Why 'just use a free API' fails", 3)
    say()
    zc = cached("zinc", lambda: None)
    cx = cached("comex", lambda: (lambda t: {"v": t[0].value, "age": t[0].age_days})(
        C.zinc_comex_futures("1mo")))
    say(f"  The obvious free zinc feed (COMEX ZNC=F) says:  ${cx['v']:,.2f}/t")
    say(f"  Real LME zinc cash settlement says:             ${zc['cash']:,.2f}/t")
    say()
    say(f"  The free one last traded {cx['age']:.0f} DAYS AGO. It is "
        f"{abs(cx['v'] - zc['cash']) / zc['cash'] * 100:.2f}% wrong.")
    say()
    say("  It returns a confident-looking number with no warning. Every price in")
    say("  this system therefore carries its own timestamp and auto-flags itself")
    say("  stale after three days. That is the whole discipline in one slide.")


@section
def s4_country(dest="IND"):
    import country_rank as CR
    head(4, f"Cheapest source country — destination {dest}", 8)
    say()
    rows = cached(f"rank_{dest}", lambda: [
        {"n": r.origin_name, "t": r.proven_tonnes, "fob": r.fob_usd_per_t,
         "duty": r.duty_pct, "frt": r.freight_usd_per_t,
         "landed": r.landed_usd_per_t, "basis": r.duty_basis}
        for r in CR.rank_source_countries(dest, "720810", 2024, top_n=6)])
    say(f"  {'origin':<15} {'proven t':>11} {'FOB $/t':>9} {'duty%':>7} "
        f"{'LANDED $/t':>11}   basis")
    say("  " + "-" * 74)
    for r in rows:
        say(f"  {r['n'][:14]:<15} {r['t']:>11,.0f} {r['fob']:>9,.0f} "
            f"{r['duty']:>7.1f} {r['landed']:>11,.0f}   {r['basis']}")
    say()
    say("  Built from: who actually ships to this country (UN Comtrade), what they")
    say("  charge (Comtrade unit values), what the state takes (WITS/UNCTAD TRAINS")
    say("  MFN + FTA preferential), and what the trade costs. All free.")


@section
def s5_flip():
    import country_rank as CR
    head(5, "The same engine, a different country — and the answer flips", 8)
    say()
    rows = cached("rank_IDN", lambda: [
        {"n": r.origin_name, "fob": r.fob_usd_per_t, "duty": r.duty_pct,
         "landed": r.landed_usd_per_t, "basis": r.duty_basis}
        for r in CR.rank_source_countries("IDN", "720810", 2024, top_n=4)])
    say("  Destination: INDONESIA")
    say(f"  {'origin':<15} {'FOB $/t':>9} {'duty%':>7} {'LANDED $/t':>11}   basis")
    say("  " + "-" * 62)
    for r in rows:
        say(f"  {r['n'][:14]:<15} {r['fob']:>9,.0f} {r['duty']:>7.1f} "
            f"{r['landed']:>11,.0f}   {r['basis']}")
    say()
    say("  Look at Vietnam. LOWEST raw price in the table — and it still loses,")
    say("  because China's ASEAN-China FTA preference is worth 15 percentage points.")
    say()
    say("  That is the entire argument for the tool. Cheapest quote is not cheapest")
    say("  cost, and no buyer holds every country's tariff schedule in their head.")


@section
def s6_forecast():
    import connectors as C
    import engine as E
    head(6, "The forecast — and its own report card", 4)
    say()
    hist = cached("zinc_hist", lambda: [r[1] for r in reversed(C.lme_zinc_westmetall()[3])])
    fc = E.forecast(hist, 30, series="ZINC.LME.CASH")
    say(f"  Zinc, 30 days:   P10 ${fc.p10:,.2f}   P50 ${fc.p50:,.2f}   P90 ${fc.p90:,.2f}")
    say(f"  P(price rises)   {fc.prob_up:.0%}        annualised vol {fc.ann_vol_pct:.1f}%")
    say()
    cal = fc.calibration
    say(f"  Backtested over {cal.get('walk_forward_windows', '?')} windows:")
    say(f"     stated confidence  80%")
    say(f"     actual coverage    {cal.get('empirical_coverage', '?')}")
    say(f"     verdict            {cal.get('verdict', '?')}")
    say()
    say("  Say this part out loud. The band is too wide to trade on, and the system")
    say("  says so itself rather than dressing it up. A forecast that cannot report")
    say("  its own calibration is decoration.")


@section
def s7_erp():
    import erp as ERP
    head(7, "Where the answer actually comes from", 2)
    say()
    cov = ERP.coverage(ERP.MockERP(), "HRC-E250A-2.0", "PLANT-PUNE-1")
    say(f"  Usable stock      {cov.usable_stock_t:>8,.0f} t   (blocked + consignment excluded)")
    say(f"  In transit        {cov.in_transit_t:>8,.0f} t")
    say(f"  Open POs          {cov.open_po_t:>8,.0f} t   (counted, or you double-buy)")
    say(f"  30-day demand     {cov.demand_next_30d_t:>8,.0f} t   "
        f"(firm {cov.firm_demand_t:,.0f} t)")
    say(f"  COVERAGE          {cov.coverage_weeks:>8,.1f} weeks")
    say()
    for w in cov.warnings[:2]:
        say(f"   ! {w}")
    say()
    say("  This is the honest part of the demo: these numbers are MOCK, and they")
    say("  are also what decides the recommendation. Connect the ERP and this")
    say("  becomes a procurement agent. Without it, it is a market dashboard.")


@section
def s8_negotiation():
    import connectors as C
    import erp as ERP
    import negotiation as NEG
    head(8, "Vendor negotiation brief", 3)
    say()
    src = ERP.MockERP()
    cov = ERP.coverage(src, "HRC-E250A-2.0", "PLANT-PUNE-1")
    sup = next(s for s in src.suppliers("HRC-E250A-2.0") if s.supplier_id == "SUP-DOM-01")
    ctr = src.contracts("HRC-E250A-2.0")[0]
    bench = cached("hrc", lambda: None)["med"]
    b = NEG.build_brief(sup, ctr, cov, bench + 1150, bench,
                        ("Japan (import)", bench * 1.13), 7200, 23.5,
                        supplier_annual_capacity_t=sup.annual_capacity_t)
    say(f"  Spend Rs {b.annual_spend_inr:,.0f}/yr   price Rs {b.current_price_inr_per_t:,.0f}/t"
        f"   vs benchmark {b.premium_vs_benchmark_pct:+.1f}%")
    say()
    say("  Ask in this order — cheapest for them first:")
    for i, lv in enumerate(b.levers[:3], 1):
        v = f"Rs {lv.annual_value_inr:,.0f}/yr" if lv.annual_value_inr else "unpriced"
        say(f"   {i}. {lv.name}")
        say(f"      {v}   their cost: {lv.likely_cost_to_supplier}   ({lv.certainty})")
    say()
    say("  Note what it refuses to do: the demurrage lever stays UNPRICED rather")
    say("  than invented, because that needs your own claim history. And if")
    say("  coverage drops below three weeks it tells the buyer not to negotiate")
    say("  at all — secure tonnage first.")


@section
def s9_honesty():
    head(9, "What is real, and what is not")
    say()
    say("  REAL, live, free    LME zinc · India HRC by brand/city · FBIL FX ·")
    say("                      UN Comtrade flows · WITS MFN + FTA tariffs · LPI")
    say("  SIMULATED           intraday ticks (needs a broker account)")
    say("  MOCK                every ERP figure")
    say("  ESTIMATED           ocean freight (no free API prices arbitrary lanes)")
    say("  NOT BUILT YET       the LLM layer, the UI, persistence")
    say()
    say("  Two things that cannot be automated at all:")
    say("    - rules-of-origin qualification (no machine-readable source exists)")
    say("    - the final duty sign-off (a customs professional owns that)")
    say()
    say("  DESTINATION COVERAGE — the honest picture:")
    say("    tariffs + country ranking     ANY destination (WITS + Comtrade, free)")
    say("    local FX                      19 of 25 major currencies free")
    say("                                  (NOT Vietnam, UAE, Saudi, Taiwan, Egypt, Russia)")
    say("    domestic price benchmark      INDIA ONLY. No free equivalent found")
    say("                                  anywhere else — that needs a paid local")
    say("                                  assessment such as BigMint, per country.")
    say()
    say("  So: 'cheapest source country' works globally today. 'import vs domestic'")
    say("  and the negotiation brief work only where a benchmark exists. The system")
    say("  REFUSES rather than substituting India's numbers — try:")
    say("      python run_deployment_demo.py THA")
    say()
    say("  python verify.py   ->   42 checks, exit 0 = sound")


# ---------------------------------------------------------------- runner
def do_snapshot():
    global MODE
    MODE = "live"
    print("Capturing a live snapshot for offline presentation...\n")
    t0 = time.time()
    for fn in (s2_live_data, s3_trap, s4_country, s5_flip, s6_forecast, s8_negotiation):
        fn()
    _capture["_captured_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with open(SNAP, "w", encoding="utf-8") as f:
        json.dump(_capture, f, indent=1, default=str)
    print(f"\n\nSnapshot written to {os.path.basename(SNAP)} "
          f"({len(_capture)} keys, {time.time() - t0:.0f}s)")
    print("Present it with:  python demo.py --cached --pause")


def main():
    global MODE, _snapshot
    ap = argparse.ArgumentParser()
    ap.add_argument("--cached", action="store_true", help="replay a snapshot, no network")
    ap.add_argument("--snapshot", action="store_true", help="capture a snapshot and exit")
    ap.add_argument("--pause", action="store_true", help="wait for a keypress between sections")
    ap.add_argument("--auto", action="store_true", help="run straight through")
    ap.add_argument("--dest", default="IND", help="destination country ISO3")
    a = ap.parse_args()

    if a.snapshot:
        do_snapshot()
        return 0

    if a.cached:
        if not os.path.exists(SNAP):
            print("No snapshot found. Run:  python demo.py --snapshot")
            return 1
        with open(SNAP, encoding="utf-8") as f:
            _snapshot = json.load(f)
        MODE = "cached"

    p = a.pause and not a.auto

    print()
    rule()
    print("  ZINC & STEEL PROCUREMENT DECISION ENGINE — proof of concept")
    if MODE == "cached":
        print(f"  MODE: CACHED — replaying a snapshot captured "
              f"{_snapshot.get('_captured_at', '?')}")
    else:
        print(f"  MODE: LIVE — every figure fetched now, "
              f"{dt.datetime.now():%Y-%m-%d %H:%M}")
    rule()

    t0 = time.time()
    s1_framing(); pause(p)
    s2_live_data(a.dest); pause(p)
    s3_trap(); pause(p)
    s4_country(a.dest); pause(p)
    s5_flip(); pause(p)
    s6_forecast(); pause(p)
    s7_erp(); pause(p)
    s8_negotiation(); pause(p)
    s9_honesty()

    print()
    rule()
    print(f"  end — {time.time() - t0:.0f}s of compute, mode={MODE}")
    rule()
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[interrupted]")
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(2)

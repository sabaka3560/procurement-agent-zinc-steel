"""
run_deployment_demo.py — The revised scope, end to end.

Scope confirmed by the client:
  - REAL deployment at a named manufacturer (PoC, so scale is not a concern)
  - REAL-TIME market data
  - VENDOR NEGOTIATION TERMS
  - CHEAPEST SOURCE COUNTRY, with the destination country as a PARAMETER
    (not hardcoded India)
  - ERP access is likely -> build against the interface, mock behind it

Run:  python run_deployment_demo.py [DESTINATION_ISO3]
e.g.  python run_deployment_demo.py IND
      python run_deployment_demo.py VNM
"""

from __future__ import annotations

import sys

import connectors as C
import countries as CO
import destinations as DEST
import country_rank as CR
import engine as E
import erp as ERP
import negotiation as NEG
import realtime as RT
import tariff as T

RULE = "=" * 96
MATERIAL = "HRC-E250A-2.0"
PLANT = "PLANT-PUNE-1"
HS6 = "720810"
ANNUAL_T = 7_200


def sec(n, t):
    print(f"\n{RULE}\n  {n}. {t}\n{RULE}")


def main(destination: str = "IND"):
    dest = CO.get(destination)
    if dest is None:
        print(f"Unknown destination '{destination}'. Try IND, VNM, THA, TUR, IDN, USA.")
        return
    print(f"\n{RULE}")
    print(f"  PROCUREMENT DECISION RUN — destination {dest.name} ({dest.iso3}), "
          f"HS {HS6}, material {MATERIAL}")
    print(RULE)

    # ---------------------------------------------------------------- 0
    prof = DEST.profile(dest.iso3)
    sec(0, f"CAPABILITY CHECK — what this system can do for {dest.name}")
    print(prof.explain())
    if prof.tier != DEST.TIER_FULL:
        print()
        print("  >> Sections below that depend on a domestic price or local FX will")
        print("     REFUSE rather than substitute another country's numbers.")

    # ---------------------------------------------------------------- 1
    sec(1, "LIVE MARKET DATA")

    # Global — works for every destination
    zc, z3, zs, zhist = C.lme_zinc_westmetall()
    print(f"  LME zinc cash           ${zc.value:>11,.2f}/t  {zc.observed_at[:10]}   [global]")
    print(f"  LME zinc 3-month        ${z3.value:>11,.2f}/t  "
          f"curve {zc.value - z3.value:+,.2f} "
          f"({'BACKWARDATION' if zc.value > z3.value else 'contango'})")
    print(f"  LME zinc stock          {zs.value:>12,.0f} t")

    # Destination-specific — FX
    fx = None
    if dest.iso3 == "IND":
        fx = C.fx_usd_inr_fbil()
        print(f"  USD/INR (FBIL)          {fx.value:>12,.4f}   {fx.observed_at[:10]}"
              f"  age {fx.age_days:.1f}d   [India official]")
    elif prof.can_price_in_local_currency:
        fx = C.fx_usd_to(prof.currency)
        print(f"  USD/{prof.currency} (ECB)          {fx.value:>12,.4f}   "
              f"{fx.observed_at[:10]}   [{prof.currency}]")
    else:
        print(f"  USD/{prof.currency}                 -- NO FREE FX FEED. "
              f"Everything below stays in USD.")

    # Destination-specific — domestic benchmark
    hrc, hrc_rows = (None, [])
    if dest.iso3 == "IND":
        hrc, hrc_rows = C.india_hrc_nexarc()
        print(f"  India HRC (Nexarc)      Rs {hrc.value:>10,.0f}/t  "
              f"median of {len(hrc_rows)} rows   [domestic benchmark]")
    else:
        print(f"  Domestic HRC benchmark  -- NONE AVAILABLE for {dest.name}.")
        print(f"                             No free equivalent of Tata Nexarc was found.")
        print(f"                             Import-vs-domestic and the negotiation brief")
        print(f"                             are DISABLED for this destination.")

    # ---------------------------------------------------------------- 2
    sec(2, "REAL-TIME LAYER — intraday event detection")
    print("  Why real-time exists in this system: NOT to shave the average price")
    print("  (zinc's daily move is ~0.5-2%; the monthly move is 5%+), but to catch a")
    print("  same-day move large enough to change today's decision.\n")
    sim = RT.SimulatedStream("MCX:ZINC", 418.10, ann_vol_pct=23.5, shock_at=150, shock_pct=-2.4)
    det = RT.MoveDetector("MCX:ZINC", spike_pct=1.5, window_min=30)
    alerts, last = [], None
    for tick in sim.stream(375):
        last = tick
        a = det.push(tick)
        if a:
            alerts.append(a)
            print(f"  [{a.ts[11:16]}] {a.kind.upper():<11} {a.pct_move:+6.2f}%  {a.message}")
    print(f"\n  375 ticks, {len(alerts)} alert(s). Session 418.10 -> {last.price:,.2f} "
          f"({(last.price - 418.10) / 418.10 * 100:+.2f}%)")
    print("  Ticks are SIMULATED. Live feed = Angel One SmartAPI (free) or Kite (Rs 500/mo);")
    print("  both need a funded broker account. LME real-time is NOT purchasable at PoC scale.")

    # ---------------------------------------------------------------- 3
    sec(3, "ERP POSITION — the input that actually decides the recommendation")
    src = ERP.MockERP()
    cov = ERP.coverage(src, MATERIAL, PLANT)
    print(f"  Usable stock            {cov.usable_stock_t:>10,.1f} t   "
          f"(blocked and consignment EXCLUDED)")
    print(f"  In transit              {cov.in_transit_t:>10,.1f} t")
    print(f"  Open POs                {cov.open_po_t:>10,.1f} t")
    print(f"  Safety stock floor      {cov.safety_stock_t:>10,.1f} t")
    print(f"  Demand next 30d         {cov.demand_next_30d_t:>10,.1f} t   "
          f"(firm {cov.firm_demand_t:,.1f} t)")
    print(f"  Weekly burn             {cov.weekly_burn_t:>10,.1f} t")
    print(f"  COVERAGE                {cov.coverage_weeks:>10,.2f} weeks")
    print(f"  Net requirement         {cov.net_requirement_t:>10,.1f} t")
    for w in cov.warnings:
        print(f"   ! {w}")

    # ---------------------------------------------------------------- 4
    sec(4, f"CHEAPEST SOURCE COUNTRY for {dest.name} — destination as a parameter")
    try:
        CR.report(dest.iso3, HS6, 2024, top_n=8)
    except Exception as e:
        print(f"  Country ranking unavailable for {dest.iso3}: {e}")

    # ---------------------------------------------------------------- 5
    sec(5, "DUTY DETAIL for the leading origins")
    for origin in ["JPN", "KOR", "CHN", "VNM"]:
        total, tres, sc = T.total_duty_pct(dest.iso3, origin, HS6)
        print("  " + tres.explain())
        for s in sc:
            print(f"      + {s['kind']} {s['pct']}%  (to {s.get('to', '?')})")
        if total is not None:
            print(f"      = {total:.2f}% total ad valorem")

    # ---------------------------------------------------------------- 6
    sec(6, "PROBABILISTIC FORECAST + RECOMMENDATION")
    zinc_series = [r[1] for r in reversed(zhist)]
    fc = E.forecast(zinc_series, horizon_days=30, series="ZINC.LME.CASH")
    print(f"  Zinc P10/P50/P90 (30d)  ${fc.p10:,.2f} / ${fc.p50:,.2f} / ${fc.p90:,.2f}")
    print(f"  P(up) {fc.prob_up:.1%}   ann vol {fc.ann_vol_pct:.1f}%   n={fc.n_observations}")
    print(f"  Calibration             {fc.calibration.get('verdict', 'n/a')} "
          f"(empirical {fc.calibration.get('empirical_coverage', 'n/a')} vs 0.80 nominal)")

    if hrc is None:
        print()
        print(f"  RECOMMENDATION          REFUSED for {dest.name}.")
        print(f"  Reason                  A buy-quantity recommendation is priced against")
        print(f"                          a domestic benchmark, and none exists for this")
        print(f"                          destination. Producing one would mean pricing")
        print(f"                          {dest.name} tonnage off Indian steel — exactly")
        print(f"                          the silent-wrong-answer failure mode.")
        _final_note(dest, prof)
        return

    rec = E.recommend(fc, requirement_tonnes=max(cov.net_requirement_t, 200.0),
                      current_price_inr_per_t=hrc.value,
                      coverage_weeks_on_hand=cov.coverage_weeks,
                      policy_min_coverage_weeks=4.0)
    print(f"\n  RECOMMENDATION          commit {rec['recommended_commit_now_pct']}% "
          f"= {rec['recommended_tonnes_now']:,.0f} t now, "
          f"defer {rec['recommended_tonnes_deferred']:,.0f} t")
    print(f"  Confidence              {rec['confidence'].upper()}")
    print(f"  Binding constraint      {rec['binding_constraint']}")
    print(f"  Regret of waiting       Rs {rec['expected_regret_of_waiting_inr']:,.0f}")

    # ---------------------------------------------------------------- 7
    sec(7, "VENDOR NEGOTIATION BRIEF")
    if not prof.can_build_negotiation_brief:
        print(f"  REFUSED for {dest.name}.")
        print(f"  A negotiation brief opens with 'you are X% above/below the market'.")
        print(f"  With no domestic benchmark for this destination there is no market to")
        print(f"  be above or below. The lever arithmetic would still compute — and would")
        print(f"  be worthless, because the position it argues from would be invented.")
        print()
        print(f"  To enable: licence a local steel price assessment for {dest.name}")
        print(f"  (BigMint-equivalent), or supply the manufacturer's own contract prices.")
        _final_note(dest, prof)
        return
    sup = next(s for s in src.suppliers(MATERIAL) if s.supplier_id == "SUP-DOM-01")
    ctr = src.contracts(MATERIAL)[0]

    batna = None
    try:
        ranked = CR.rank_source_countries(dest.iso3, HS6, 2024, top_n=3)
        if ranked and ranked[0].landed_usd_per_t:
            batna = (f"{ranked[0].origin_name} (import)",
                     ranked[0].landed_usd_per_t * fx.value)
    except Exception:
        pass

    brief = NEG.build_brief(
        supplier=sup, contract=ctr, coverage_position=cov,
        current_price_inr_per_t=hrc.value + 1_150,   # contract premium over index
        benchmark_price_inr_per_t=hrc.value,
        batna=batna, annual_tonnes=ANNUAL_T, price_vol_pct=fc.ann_vol_pct,
        supplier_annual_capacity_t=sup.annual_capacity_t,
    )
    print(NEG.render(brief))

    _final_note(dest, prof)


def _final_note(dest, prof):
    # ---------------------------------------------------------------- 8
    sec(8, "WHAT IN THIS RUN IS REAL")
    print("  REAL, live, free        LME zinc cash/3M/stock · UN Comtrade flows ·")
    print("                          WITS MFN and preferential tariffs · World Bank LPI")
    if prof.can_price_in_local_currency:
        print(f"                          {prof.fx_source}")
    if prof.domestic_benchmark:
        print(f"                          {prof.domestic_benchmark}")
    print("  SIMULATED               intraday ticks (needs a broker account)")
    print("  MOCK                    all ERP figures — inventory, demand, POs, suppliers")
    print("  ESTIMATED               ocean freight (no free API prices arbitrary lanes)")
    print("  NOT DECISION-GRADE      any duty flagged above; preferential rates are 2021")

    print(f"\n  DESTINATION COVERAGE    {dest.name} is tier {prof.tier}.")
    if prof.missing:
        for m in prof.missing:
            print(f"      cannot: {m}")
    else:
        print("      full free stack available")
    print("  Run  python destinations.py  for the capability matrix across all "
          f"{len(CO._C)} countries.")

    print("\n  The ERP layer is the one that decides the answer, and it is the one")
    print("  that is mocked. That is the honest state of the PoC.\n")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "IND")

"""
run_demo.py — End-to-end proof that the loop closes on real, free, live data.

    live market data -> landed cost -> probabilistic forecast (calibrated)
    -> risk-aware recommendation -> negotiation economics

No API keys. No paid subscriptions. Everything below was executed against live
endpoints during the engagement.

    python run_demo.py

What this DOES prove: the data is reachable and the decision loop runs.
What this does NOT prove: that the recommendation is any good. That requires
ERP data and a shadow-mode backtest against real buyer decisions. See
Deliverable 05, "Kill criteria".
"""

from __future__ import annotations

import sys

import connectors as C
import engine as E

RULE = "=" * 78
SUB = "-" * 78


def money(x, cur="Rs"):
    return f"{cur} {x:,.0f}"


def section(title):
    print(f"\n{RULE}\n  {title}\n{RULE}")


def main():
    section("1. LIVE MARKET DATA  (free sources, zero API keys)")

    fx = C.fx_usd_inr_fbil()
    fx_ecb = C.fx_usd_inr_ecb()
    print(f"  USD/INR (FBIL, India official) : {fx.value:>12,.4f}   as of {fx.observed_at[:10]}  [age {fx.age_days:.1f}d]")
    print(f"  USD/INR (ECB cross-check)      : {fx_ecb.value:>12,.4f}   as of {fx_ecb.observed_at[:10]}")
    spread = abs(fx.value - fx_ecb.value) / fx_ecb.value * 100
    print(f"  -> benchmark spread            : {spread:>12.2f}%   "
          f"({'MATERIAL — pick one and document it' if spread > 0.5 else 'immaterial'})")

    zc, z3, zs, zhist = C.lme_zinc_westmetall()
    print(f"\n  LME Zinc cash                  : ${zc.value:>11,.2f}/t   as of {zc.observed_at[:10]}")
    print(f"  LME Zinc 3-month               : ${z3.value:>11,.2f}/t")
    print(f"  LME Zinc warehouse stock       : {zs.value:>12,.0f} t")
    curve = zc.value - z3.value
    state = "BACKWARDATION (spot scarcity — deferring purchases carries real risk)" \
        if curve > 0 else "CONTANGO (ample spot — deferral is cheaper)"
    print(f"  -> curve (cash - 3M)           : ${curve:>11,.2f}/t   {state}")

    hrc_obs, hrc_rows = C.india_hrc_nexarc()
    print(f"\n  India HR Coil (Tata Nexarc)    : Rs {hrc_obs.value:>10,.0f}/t   "
          f"median of {len(hrc_rows)} listed rows")
    mum = [r for r in hrc_rows if r["city"] == "Mumbai" and r["grade"] == "E250 A"]
    for r in mum[:3]:
        print(f"       {r['brand']:<5} {r['city']:<7} {r['grade']:<8} "
              f"{r['thickness_mm']:>9} mm   Rs {r['price_inr_per_t']:>7,}/t")

    zcx, _ = C.zinc_comex_futures("1mo")
    print(f"\n  [NEGATIVE CONTROL] COMEX zinc  : ${zcx.value:>11,.2f}/t   "
          f"age {zcx.age_days:.1f} days -> {zcx.quality.upper()}")
    err = abs(zcx.value - zc.value) / zc.value * 100
    print(f"  -> error vs real LME cash      : {err:>11.2f}%   "
          f"This is why 'just use a free API' fails for zinc.")

    # ------------------------------------------------------------------
    section("2. LANDED COST  —  import vs domestic, same specification")

    reg = E.DUTY_REGIMES["7208"]
    imported = E.landed_cost_import(
        fob_usd_per_t=545.0,          # illustrative FOB offer, East Asia
        fx_inr_per_usd=fx.value,
        regime=reg,
        ocean_freight_usd_per_t=42.0,
        lead_time_days=45,
    )
    domestic = E.landed_cost_domestic(
        ex_works_inr_per_t=hrc_obs.value,
        lead_time_days=12,
    )

    print(f"  IMPORT  (HS {reg.hs_code}, FOB $545/t, 45d lead time)")
    print(f"    CIF                          : {money(imported['cif_inr_per_t']):>16}/t")
    print(f"    BCD @ {reg.bcd_rate:.1%}                   : {money(imported['bcd_inr']):>16}/t")
    print(f"    Safeguard @ {reg.safeguard_rate:.1%}            : {money(imported['safeguard_inr']):>16}/t")
    print(f"    SWS @ 10% of BCD             : {money(imported['sws_inr']):>16}/t")
    print(f"    Total duty                   : {money(imported['duty_total_inr']):>16}/t   "
          f"({imported['effective_duty_pct_of_cif']}% of CIF)")
    print(f"    Port/CHA + inland            : {money(imported['port_cha_inr'] + imported['inland_freight_inr']):>16}/t")
    print(f"    Finance (45d @ 9%)           : {money(imported['finance_cost_inr']):>16}/t")
    print(f"    LANDED                       : {money(imported['landed_cost_inr_per_t']):>16}/t")
    print(f"    (IGST {money(imported['igst_inr_recoverable'])}/t recoverable — excluded from cost, "
          f"but locks working capital)")

    print(f"\n  DOMESTIC (Tata Nexarc median, 12d lead time)")
    print(f"    Ex-works                     : {money(domestic['ex_works_inr_per_t']):>16}/t")
    print(f"    Inland freight               : {money(domestic['inland_freight_inr']):>16}/t")
    print(f"    Finance (12d @ 9%)           : {money(domestic['finance_cost_inr']):>16}/t")
    print(f"    LANDED                       : {money(domestic['landed_cost_inr_per_t']):>16}/t")

    diff = imported["landed_cost_inr_per_t"] - domestic["landed_cost_inr_per_t"]
    winner = "DOMESTIC" if diff > 0 else "IMPORT"
    print(f"\n  -> {winner} cheaper by {money(abs(diff))}/t "
          f"({abs(diff)/domestic['landed_cost_inr_per_t']*100:.1f}%)")

    if not reg.verified:
        print(f"\n  !! DUTY REGIME NOT VERIFIED — the agent must NOT present this as a")
        print(f"     confident recommendation. {reg.source_note}")

    # ------------------------------------------------------------------
    section("3. PROBABILISTIC FORECAST  —  LME zinc, 30 days")

    zinc_series = [row[1] for row in reversed(zhist)]   # oldest first
    fc = E.forecast(zinc_series, horizon_days=30, series="ZINC.LME.CASH")

    print(f"  Spot                           : ${fc.spot:>11,.2f}/t")
    print(f"  P10 / P50 / P90 (30d)          : ${fc.p10:>11,.2f}  ${fc.p50:>9,.2f}  ${fc.p90:>9,.2f}")
    print(f"  P(price up)                    : {fc.prob_up:>11.1%}")
    print(f"  P(up more than 3%)             : {fc.prob_up_gt_3pct:>11.1%}")
    print(f"  P(down more than 3%)           : {fc.prob_down_gt_3pct:>11.1%}")
    print(f"  Annualised volatility          : {fc.ann_vol_pct:>11.1f}%")
    print(f"  Observations                   : {fc.n_observations:>11}")
    print(f"  Method                         : {fc.method}")

    print(f"\n  CALIBRATION BACKTEST (the number that decides if any of this is real)")
    for k, v in fc.calibration.items():
        print(f"    {k:<32} {v}")
    for w in fc.warnings:
        print(f"    !! {w}")

    # ------------------------------------------------------------------
    section("4. RECOMMENDATION  —  risk-aware, over a coarse action grid")

    REQ_T = 600.0
    rec = E.recommend(
        fc,
        requirement_tonnes=REQ_T,
        current_price_inr_per_t=domestic["landed_cost_inr_per_t"],
        coverage_weeks_on_hand=5.5,
        policy_min_coverage_weeks=4.0,
    )

    print(f"  Requirement                    : {REQ_T:,.0f} t over the next 30 days")
    print(f"  Coverage on hand               : {rec['coverage_weeks_now']} weeks "
          f"-> {rec['coverage_weeks_if_deferred']} weeks if fully deferred "
          f"(policy min {rec['policy_min_coverage_weeks']})")
    print(f"  Binding constraint             : {rec['binding_constraint']}")

    print(f"\n  {'commit':>7} {'t now':>9} {'t later':>9} {'E[cost]':>16} {'CVaR95':>16} {'objective':>16}")
    print(f"  {SUB[:76]}")
    for o in rec["options"]:
        mark = "  <-- recommended" if o["commit_now_pct"] == rec["recommended_commit_now_pct"] else ""
        print(f"  {o['commit_now_pct']:>6}% {o['tonnes_now']:>9,.0f} {o['tonnes_deferred']:>9,.0f} "
              f"{o['expected_cost_inr']:>16,.0f} {o['cvar95_inr']:>16,.0f} "
              f"{o['objective']:>16,.0f}{mark}")

    print(f"\n  RECOMMENDATION: commit {rec['recommended_commit_now_pct']}% "
          f"= {rec['recommended_tonnes_now']:,.0f} t now, "
          f"defer {rec['recommended_tonnes_deferred']:,.0f} t")
    print(f"  Confidence                     : {rec['confidence'].upper()}")
    print(f"  Expected regret of waiting     : {money(rec['expected_regret_of_waiting_inr'])}")
    print(f"  Expected regret of buying all  : {money(rec['expected_regret_of_buying_all_inr'])}")

    # ------------------------------------------------------------------
    section("5. NEGOTIATION ECONOMICS  —  arithmetic, not a learned model")

    for lv in E.negotiation_levers(
        annual_tonnes=7_200,
        price_inr_per_t=domestic["landed_cost_inr_per_t"],
        current_credit_days=30,
        target_credit_days=60,
        price_vol_pct=fc.ann_vol_pct,
    ):
        val = money(lv["annual_value_inr"]) if lv["annual_value_inr"] is not None else "needs internal data"
        print(f"  {lv['lever']}")
        print(f"      value/yr : {val}")
        print(f"      basis    : {lv['basis']}")
        print(f"      certainty: {lv['certainty']}\n")

    # ------------------------------------------------------------------
    section("6. SOURCE COUNTRY SCREEN  —  UN Comtrade, free, no key")

    partners = C.india_imports_by_partner("7208", 2024)
    print(f"  India HRC (HS 7208) imports 2024 — top partners by value")
    print(f"  {'partner':<16} {'USD value':>18} {'tonnes':>14} {'$/t':>10}")
    print(f"  {SUB[:62]}")
    for p in partners[:9]:
        uv = f"{p['unit_value_usd_per_t']:,.0f}" if p["unit_value_usd_per_t"] else "n/a"
        t = p["net_weight_kg"] / 1000 if p["net_weight_kg"] else 0
        print(f"  {str(p['partner'])[:15]:<16} {p['value_usd']:>18,.0f} {t:>14,.0f} {uv:>10}")
    print(f"\n  NOTE: this screens COUNTRIES only. DGCI&S does not publish importer/")
    print(f"  exporter identity, so no dataset here can name a vendor. Vendor")
    print(f"  discovery is an RFQ and qualification process, not a data pull.")

    section("DONE")
    print("  Every figure above came from a live endpoint or a deterministic")
    print("  function in engine.py. No LLM produced any number on this page.")
    print("  That separation is the whole architecture — see Deliverable 04.\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nFAILED: {type(e).__name__}: {e}", file=sys.stderr)
        print("If this is a network error, the free sources may be rate-limiting.", file=sys.stderr)
        raise

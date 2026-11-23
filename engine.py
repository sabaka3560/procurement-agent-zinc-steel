"""
engine.py — The deterministic engines the LLM is NOT allowed to do itself.

Three separable services, exactly as doc (4) specifies:
  1. landed_cost()      — arithmetic. No model, no judgement, fully auditable.
  2. forecast()         — probabilistic, WITH an honest calibration backtest.
  3. recommend()        — risk-aware decision over a small action grid.

Design rule enforced throughout: every number the agent will eventually say out
loud is produced here and returned in a structured dict. The LLM narrates; it
never computes. See Deliverable 04, section on numeric guardrails.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field

import numpy as np


# ==========================================================================
# 1. LANDED COST
# ==========================================================================

@dataclass
class DutyRegime:
    """
    India import duty stack for one HS code, as of an effective date.

    THESE ARE NOT CONSTANTS. Every field is effective-dated and must be
    re-verified against CBIC / DGTR before it informs a real purchase.
    Doc (3) gives the cautionary example: the 2025 Refined Zinc QCO was
    notified and then withdrawn. A cached rule is a production bug.
    """
    hs_code: str
    effective_from: str
    bcd_rate: float                 # Basic Customs Duty
    safeguard_rate: float = 0.0     # DGTR safeguard measure
    antidumping_rate: float = 0.0   # DGTR anti-dumping
    aidc_rate: float = 0.0          # Agriculture Infrastructure & Development Cess
    sws_rate: float = 0.10          # Social Welfare Surcharge, levied ON the BCD
    igst_rate: float = 0.18         # recoverable as input tax credit
    source_note: str = ""
    verified: bool = False          # False => the agent must warn the user


# Effective-dated regimes gathered during the engagement (2026-09-04/05).
# `verified=False` on HRC is deliberate and load-bearing: the BCD on 7208 came
# back inconsistent (7.5% vs 15%) across secondary aggregators and was NOT
# resolvable against CBIC's First Schedule. The system must refuse to present
# a confident import recommendation while that flag is False.
DUTY_REGIMES = {
    "7208": DutyRegime(
        hs_code="7208",
        effective_from="2026-04-21",
        bcd_rate=0.075,
        safeguard_rate=0.115,
        aidc_rate=0.0,
        source_note=(
            "Safeguard 11.5% ad valorem for 21-Apr-2026 to 20-Apr-2027 (year 2 of "
            "a 12%/11.5%/11% step-down) on flat steel HS 7208-7212, 7225-7226. "
            "BCD UNRESOLVED: secondary sources split between 7.5% and 15%. "
            "MUST be pinned against the CBIC First Schedule before use."
        ),
        verified=False,
    ),
    "7901": DutyRegime(
        hs_code="7901",
        effective_from="2026-04-01",
        bcd_rate=0.05,
        aidc_rate=0.05,
        source_note=(
            "Unwrought zinc. BCD 5% + AIDC 5% consistent across several trade "
            "aggregators. Refined Zinc QCO 2025 reported RESCINDED — verify "
            "against the Gazette of India before relying on 'no BIS mark needed'."
        ),
        verified=False,
    ),
}


def landed_cost_import(
    fob_usd_per_t: float,
    fx_inr_per_usd: float,
    regime: DutyRegime,
    ocean_freight_usd_per_t: float = 45.0,
    insurance_pct_of_fob: float = 0.001125,   # standard 1.125 per mille
    port_cha_inr_per_t: float = 1_200.0,
    inland_freight_inr_per_t: float = 1_800.0,
    lead_time_days: int = 45,
    cost_of_capital_pa: float = 0.09,
    expected_rejection_pct: float = 0.005,
    include_recoverable_igst: bool = False,
) -> dict:
    """
    Full India import landed cost, INR per tonne.

    IGST is EXCLUDED from economic cost by default because it is recoverable as
    input tax credit. It is still reported separately because it is real working
    capital that is locked up for one GST cycle -- a distinction docs (2)-(4)
    correctly flag and most spreadsheet models get wrong.
    """
    insurance = fob_usd_per_t * insurance_pct_of_fob
    cif_usd = fob_usd_per_t + ocean_freight_usd_per_t + insurance
    cif = cif_usd * fx_inr_per_usd

    bcd = cif * regime.bcd_rate
    safeguard = cif * regime.safeguard_rate
    antidumping = cif * regime.antidumping_rate
    aidc = cif * regime.aidc_rate
    sws = bcd * regime.sws_rate                      # SWS is levied on BCD only
    duty_total = bcd + safeguard + antidumping + aidc + sws

    assessable_for_igst = cif + duty_total
    igst = assessable_for_igst * regime.igst_rate

    subtotal = cif + duty_total + port_cha_inr_per_t + inland_freight_inr_per_t
    finance = subtotal * cost_of_capital_pa * (lead_time_days / 365.0)
    quality_loss = subtotal * expected_rejection_pct

    economic = subtotal + finance + quality_loss
    if include_recoverable_igst:
        economic += igst

    return {
        "route": "import",
        "hs_code": regime.hs_code,
        "duty_regime_effective_from": regime.effective_from,
        "duty_regime_verified": regime.verified,
        "fob_usd_per_t": round(fob_usd_per_t, 2),
        "ocean_freight_usd_per_t": round(ocean_freight_usd_per_t, 2),
        "insurance_usd_per_t": round(insurance, 2),
        "cif_usd_per_t": round(cif_usd, 2),
        "fx_inr_per_usd": round(fx_inr_per_usd, 4),
        "cif_inr_per_t": round(cif, 2),
        "bcd_inr": round(bcd, 2),
        "safeguard_inr": round(safeguard, 2),
        "antidumping_inr": round(antidumping, 2),
        "aidc_inr": round(aidc, 2),
        "sws_inr": round(sws, 2),
        "duty_total_inr": round(duty_total, 2),
        "effective_duty_pct_of_cif": round(100 * duty_total / cif, 2),
        "port_cha_inr": round(port_cha_inr_per_t, 2),
        "inland_freight_inr": round(inland_freight_inr_per_t, 2),
        "finance_cost_inr": round(finance, 2),
        "expected_quality_loss_inr": round(quality_loss, 2),
        "igst_inr_recoverable": round(igst, 2),
        "working_capital_locked_inr_per_t": round(igst, 2),
        "landed_cost_inr_per_t": round(economic, 2),
    }


def landed_cost_domestic(
    ex_works_inr_per_t: float,
    inland_freight_inr_per_t: float = 900.0,
    lead_time_days: int = 12,
    cost_of_capital_pa: float = 0.09,
    expected_rejection_pct: float = 0.002,
    gst_rate: float = 0.18,
) -> dict:
    """Domestic purchase. GST is recoverable, so excluded from economic cost."""
    subtotal = ex_works_inr_per_t + inland_freight_inr_per_t
    finance = subtotal * cost_of_capital_pa * (lead_time_days / 365.0)
    quality_loss = subtotal * expected_rejection_pct
    gst = ex_works_inr_per_t * gst_rate
    return {
        "route": "domestic",
        "ex_works_inr_per_t": round(ex_works_inr_per_t, 2),
        "inland_freight_inr": round(inland_freight_inr_per_t, 2),
        "finance_cost_inr": round(finance, 2),
        "expected_quality_loss_inr": round(quality_loss, 2),
        "gst_inr_recoverable": round(gst, 2),
        "working_capital_locked_inr_per_t": round(gst, 2),
        "lead_time_days": lead_time_days,
        "landed_cost_inr_per_t": round(subtotal + finance + quality_loss, 2),
    }


# ==========================================================================
# 2. PROBABILISTIC FORECAST  (with an honest calibration backtest)
# ==========================================================================

@dataclass
class ForecastResult:
    series: str
    spot: float
    horizon_days: int
    p10: float
    p50: float
    p90: float
    prob_up: float
    prob_up_gt_3pct: float
    prob_down_gt_3pct: float
    ann_vol_pct: float
    n_observations: int
    method: str
    calibration: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def _log_returns(prices: list[float]) -> np.ndarray:
    p = np.asarray(prices, dtype=float)
    return np.diff(np.log(p))


def forecast(
    prices_oldest_first: list[float],
    horizon_days: int = 30,
    series: str = "series",
    n_sims: int = 20_000,
    seed: int = 7,
) -> ForecastResult:
    """
    Block-bootstrap forecast of the price distribution `horizon_days` ahead.

    WHY THIS AND NOT SOMETHING FANCIER — this is the single most important
    modelling decision in the project, so the reasoning is recorded here:

      * The research corpus (docs 2-4) specifies Bayesian regime-switching +
        joint Monte Carlo + CVaR multistage stochastic programming. With 170
        daily observations you cannot fit, let alone validate, any of that.
      * Published evidence is actively against the fashionable alternative:
        FedChronos (arXiv:2608.01290, Aug 2026) evaluated Chronos zero-shot on
        15 Indian commodity markets and got 101.76% MAPE -- explicitly "no
        better than predicting the series mean" -- because the pretraining
        corpus is Western financial/energy data. Fine-tuning got it to 69.78%,
        still worse than classical baselines.
      * A stationary block bootstrap preserves volatility clustering and fat
        tails without estimating parameters you cannot identify. It is honest
        about what it is: a resampling of the recent past.

    The bootstrap is NOT the clever part. `calibration` is: we walk the series
    forward and measure whether the stated 80% band actually covered 80% of
    outcomes. A forecast without that number is decoration.
    """
    rng = np.random.default_rng(seed)
    prices = [float(p) for p in prices_oldest_first if p and not math.isnan(p)]
    warnings: list[str] = []

    if len(prices) < 40:
        raise ValueError(f"need >=40 observations, got {len(prices)}")
    if len(prices) < 250:
        warnings.append(
            f"Only {len(prices)} observations (~{len(prices)/21:.0f} months). "
            "The tails of this distribution are extrapolation, not evidence. "
            "Treat P10/P90 as indicative, not as risk limits."
        )

    rets = _log_returns(prices)
    spot = prices[-1]

    # stationary block bootstrap, mean block ~ 5 trading days
    block = 5
    n_blocks = int(np.ceil(horizon_days / block))

    def simulate(r: np.ndarray, h: int, n: int) -> np.ndarray:
        starts = rng.integers(0, max(1, len(r) - block), size=(n, n_blocks))
        paths = np.concatenate(
            [r[s:s + block] for row in starts for s in row]
        ).reshape(n, n_blocks * block)[:, :h]
        return paths.sum(axis=1)

    total = simulate(rets, horizon_days, n_sims)
    terminal = spot * np.exp(total)

    p10, p50, p90 = np.percentile(terminal, [10, 50, 90])
    ann_vol = float(np.std(rets, ddof=1) * np.sqrt(252) * 100)

    # ---- calibration backtest: does the 80% band really cover 80%? ----
    cal = {"note": "insufficient history for a walk-forward backtest"}
    min_train = 60
    if len(prices) >= min_train + horizon_days + 10:
        hits = tot = 0
        lo_widths = []
        # HORIZON ALIGNMENT (fixed 2026-09-07).
        # `prices[:t]` ends at index t-1, so the forecast ORIGIN is prices[t-1].
        # The outcome was read at prices[t + horizon_days], which is h+1 steps
        # after the origin, not h. Every reported coverage figure was therefore
        # measuring the wrong horizon. Origin and outcome are now explicit.
        for origin_i in range(min_train - 1, len(prices) - horizon_days - 1):
            hist = prices[:origin_i + 1]          # ends AT the origin
            r_t = _log_returns(hist)
            if len(r_t) < block + 1:
                continue
            sim = prices[origin_i] * np.exp(simulate(r_t, horizon_days, 1200))
            lo, hi = np.percentile(sim, [10, 90])
            actual = prices[origin_i + horizon_days]   # exactly h steps on
            hits += int(lo <= actual <= hi)
            tot += 1
            lo_widths.append((hi - lo) / prices[origin_i])
        if tot >= 10:
            cov = hits / tot
            # Overlapping windows are NOT independent observations. With a
            # h-day horizon, consecutive windows share h-1 days of outcome, so
            # the effective sample is roughly tot/h, not tot. Reporting 81
            # windows without this invites over-reading the coverage figure.
            eff = max(1, int(tot / max(horizon_days, 1)))
            cal = {
                "walk_forward_windows": tot,
                "effective_independent_windows": eff,
                "independence_note": (
                    f"{tot} windows overlap by {horizon_days-1} days each; roughly "
                    f"{eff} are independent. Treat the coverage figure as "
                    f"indicative, not a precise estimate."),
                "nominal_coverage": 0.80,
                "empirical_coverage": round(cov, 3),
                "mean_band_width_pct_of_spot": round(100 * float(np.mean(lo_widths)), 2),
                "verdict": (
                    "well calibrated" if 0.72 <= cov <= 0.88
                    else ("OVERCONFIDENT — band too narrow, real risk exceeds stated"
                          if cov < 0.72 else
                          "UNDERCONFIDENT — band too wide, forecast is uninformative")
                ),
            }
            if cov < 0.72:
                warnings.append(
                    f"Backtest coverage {cov:.0%} vs 80% nominal. The model UNDERSTATES "
                    "risk. Widen the band or reduce position sizing accordingly."
                )

    return ForecastResult(
        series=series,
        spot=round(spot, 2),
        horizon_days=horizon_days,
        p10=round(float(p10), 2),
        p50=round(float(p50), 2),
        p90=round(float(p90), 2),
        prob_up=round(float((terminal > spot).mean()), 3),
        prob_up_gt_3pct=round(float((terminal > spot * 1.03).mean()), 3),
        prob_down_gt_3pct=round(float((terminal < spot * 0.97).mean()), 3),
        ann_vol_pct=round(ann_vol, 2),
        n_observations=len(prices),
        method="stationary block bootstrap (block=5d) on log returns",
        calibration=cal,
        warnings=warnings,
    )


# ==========================================================================
# 3. DECISION LAYER
# ==========================================================================

def recommend(
    fc: ForecastResult,
    requirement_tonnes: float,
    current_price_inr_per_t: float,
    coverage_weeks_on_hand: float,
    policy_min_coverage_weeks: float = 4.0,
    carrying_cost_pa: float = 0.14,
    stockout_penalty_inr_per_t: float = 25_000.0,
    cvar_alpha: float = 0.95,
    risk_aversion: float = 0.35,
    n_sims: int = 20_000,
    seed: int = 11,
) -> dict:
    """
    Choose what fraction of `requirement_tonnes` to commit NOW vs defer.

    Deliberately a coarse grid (0%,25%,...,100%), not a continuous optimiser.
    With this much data, precision beyond 25% buckets is false precision, and
    a buyer cannot act on "commit 63.4%" anyway.

    Objective: E[cost] + risk_aversion * CVaR_95, plus expected stockout cost
    if deferring drops coverage below policy.
    """
    rng = np.random.default_rng(seed)

    # price ratio distribution at the horizon, implied by the forecast
    sigma = math.log(fc.p90 / fc.p10) / (2 * 1.2816)   # z(0.9) = 1.2816
    mu = math.log(fc.p50 / fc.spot)
    ratio = np.exp(rng.normal(mu, max(sigma, 1e-6), n_sims))
    future_price = current_price_inr_per_t * ratio

    horizon_yrs = fc.horizon_days / 365.0
    weeks_deferred = fc.horizon_days / 7.0
    coverage_if_defer = coverage_weeks_on_hand - weeks_deferred

    options = []
    for f in (0.0, 0.25, 0.50, 0.75, 1.0):
        qty_now = requirement_tonnes * f
        qty_later = requirement_tonnes * (1 - f)

        cost_now = qty_now * current_price_inr_per_t
        # material bought early is carried for the deferral window
        carrying = qty_now * current_price_inr_per_t * carrying_cost_pa * horizon_yrs
        cost_later = qty_later * future_price

        # shortfall risk applies only to the deferred portion
        shortfall_w = max(0.0, policy_min_coverage_weeks - coverage_if_defer)
        exposure = shortfall_w / max(policy_min_coverage_weeks, 1e-6)
        stockout = float(qty_later * stockout_penalty_inr_per_t * exposure * 0.10)

        total = cost_now + carrying + cost_later + stockout
        exp_cost = float(total.mean())
        var = float(np.percentile(total, cvar_alpha * 100))
        cvar = float(total[total >= var].mean()) if (total >= var).any() else var

        options.append({
            "commit_now_pct": int(f * 100),
            "tonnes_now": round(qty_now, 1),
            "tonnes_deferred": round(qty_later, 1),
            "expected_cost_inr": round(exp_cost, 0),
            "cvar95_inr": round(cvar, 0),
            "objective": round(exp_cost + risk_aversion * cvar, 0),
            "expected_stockout_cost_inr": round(stockout, 0),
        })

    # ---- FEASIBILITY IS A HARD FILTER, NOT A PENALTY --------------------
    # Previously the coverage policy was priced as a soft cost, so the model
    # could -- and did -- return 0% while reporting
    # coverage_policy_breached_if_deferred=True and naming the policy as the
    # binding constraint. A floor you can buy your way through is not a floor.
    weekly = requirement_tonnes / max(fc.horizon_days / 7.0, 1e-6)
    for o in options:
        cov_after = coverage_weeks_on_hand - weeks_deferred + (
            o["tonnes_now"] / weekly if weekly > 0 else 0.0)
        o["coverage_after_weeks"] = round(cov_after, 2)
        o["feasible"] = bool(cov_after >= policy_min_coverage_weeks)
        if not o["feasible"]:
            o["infeasible_reason"] = (
                f"coverage {cov_after:.1f}w after this action is below the "
                f"{policy_min_coverage_weeks:.1f}w policy floor")

    feasible = [o for o in options if o["feasible"]]
    if feasible:
        best = min(feasible, key=lambda o: o["objective"])
        selection_basis = (
            f"cheapest of the {len(feasible)} option(s) that keep coverage at or "
            f"above the {policy_min_coverage_weeks:.1f}w floor")
    else:
        best = max(options, key=lambda o: o["coverage_after_weeks"])
        selection_basis = (
            "NO option clears the policy floor. Showing the one that recovers the "
            "most coverage. This is a shortage to escalate, not a buying choice.")

    # ---- BE HONEST ABOUT THE SHAPE OF THIS OBJECTIVE -------------------
    # Cost = f*P_now + (1-f)*E[P_future] + carrying(f), all linear in f, and
    # CVaR of a linear combination with a deterministic leg is linear too. So
    # the objective is linear across the grid and its optimum is ALWAYS an
    # endpoint. The five rows are a sensitivity display, not an optimisation
    # with an interior solution. Presenting them as an optimiser would overstate
    # what this computes. A genuine interior optimum needs a convex term the
    # system does not yet model -- demand uncertainty, or a shortage cost that
    # rises non-linearly with the gap.
    _objs = [o["objective"] for o in options]
    _incs = [_objs[i + 1] - _objs[i] for i in range(len(_objs) - 1)]
    _linear = (max(_incs) - min(_incs)) < max(1.0, abs(_incs[0]) * 0.01) if _incs else True

    do_nothing = next(o for o in options if o["commit_now_pct"] == 0)
    buy_all = next(o for o in options if o["commit_now_pct"] == 100)

    # regret of the two anchor policies against perfect hindsight
    hindsight = min(o["expected_cost_inr"] for o in options)

    coverage_breach = coverage_if_defer < policy_min_coverage_weeks
    confidence = "medium"
    if fc.warnings or not fc.calibration.get("empirical_coverage"):
        confidence = "low"
    elif fc.calibration.get("verdict") == "well calibrated" and fc.n_observations > 200:
        confidence = "high"

    return {
        "recommended_commit_now_pct": best["commit_now_pct"],
        "recommended_tonnes_now": best["tonnes_now"],
        "recommended_tonnes_deferred": best["tonnes_deferred"],
        "confidence": confidence,
        "options": options,
        "prob_price_rises": fc.prob_up,
        "prob_rise_gt_3pct": fc.prob_up_gt_3pct,
        "prob_fall_gt_3pct": fc.prob_down_gt_3pct,
        "expected_regret_of_waiting_inr": round(do_nothing["expected_cost_inr"] - hindsight, 0),
        "expected_regret_of_buying_all_inr": round(buy_all["expected_cost_inr"] - hindsight, 0),
        "coverage_weeks_now": round(coverage_weeks_on_hand, 1),
        "coverage_weeks_if_deferred": round(coverage_if_defer, 1),
        "policy_min_coverage_weeks": policy_min_coverage_weeks,
        "coverage_policy_breached_if_deferred": coverage_breach,
        "binding_constraint": (
            "production coverage policy" if coverage_breach else "price risk"
        ),
        "selection_basis": selection_basis,
        "feasible_option_count": len(feasible),
        "objective_is_linear": bool(_linear),
        "objective_shape_note": (
            "The objective is linear in the commit fraction, so its optimum is "
            "always an endpoint (0% or 100%). These rows are a sensitivity "
            "display, not an interior optimisation. A genuine interior optimum "
            "requires demand uncertainty or a non-linear shortage cost, neither "
            "of which is modelled."
            if _linear else
            "Objective is non-linear across the grid; an interior optimum is "
            "meaningful."),
        "stockout_probability": None,
        "stockout_probability_note": (
            "Not estimated. A stockout probability needs a demand distribution "
            "and a lead-time distribution; this system models neither. Coverage "
            "weeks against the policy floor is what is actually computed."),
        "forecast": fc.to_dict(),
    }


# ==========================================================================
# 4. NEGOTIATION ECONOMICS  (deterministic — see review finding R-11)
# ==========================================================================

def negotiation_levers(
    annual_tonnes: float,
    price_inr_per_t: float,
    current_credit_days: int = 30,
    target_credit_days: int = 60,
    cost_of_capital_pa: float = 0.09,
    volume_flex_pct: float = 0.10,
    price_vol_pct: float = 18.0,
) -> list[dict]:
    """
    Value each negotiation lever in rupees per year.

    This is arithmetic, NOT a learned model. Review finding R-11: you cannot
    learn what a supplier *would* have accepted, because you only ever observe
    the path you took. So we do not pretend to. We price the lever from the
    buyer's side and let the human decide what to trade.
    """
    spend = annual_tonnes * price_inr_per_t
    extra_days = target_credit_days - current_credit_days

    credit_value = spend * cost_of_capital_pa * (extra_days / 365.0)
    # volume flexibility ~ a straddle on the portion you can flex
    flex_value = spend * volume_flex_pct * (price_vol_pct / 100.0) * 0.4
    # monthly vs quarterly index reset reduces mark-to-market drift exposure
    reset_value = spend * (price_vol_pct / 100.0) * 0.25 * 0.15

    levers = [
        {
            "lever": f"Credit terms {current_credit_days}d -> {target_credit_days}d",
            "annual_value_inr": round(credit_value, 0),
            "basis": f"{extra_days} extra days x {cost_of_capital_pa:.1%} cost of capital on Rs {spend:,.0f} spend",
            "certainty": "high — pure arithmetic",
        },
        {
            "lever": f"Volume flexibility +/-{volume_flex_pct:.0%}",
            "annual_value_inr": round(flex_value, 0),
            "basis": "optionality on the flexible tranche, valued at 40% of naive intrinsic",
            "certainty": "medium — depends on your ability to actually exercise it",
        },
        {
            "lever": "Quarterly -> monthly index reset",
            "annual_value_inr": round(reset_value, 0),
            "basis": "reduced mark-to-market drift between resets",
            "certainty": "low — sign depends on trend direction; this is risk reduction, not saving",
        },
        {
            "lever": "Seller absorbs demurrage from documentary delay",
            "annual_value_inr": None,
            "basis": "price from your own historical demurrage claims — no market proxy exists",
            "certainty": "requires internal data",
        },
    ]
    return sorted(
        levers,
        key=lambda x: -(x["annual_value_inr"] or -1),
    )

"""
negotiation.py — Vendor negotiation terms, priced.

Review finding R-11 governs this module. All four research reports proposed
"learning" the value of negotiation levers. You cannot. To learn what a
supplier *would have accepted* you need the counterfactual, and you only ever
observe the path you took. There is no dataset, anywhere, of rejected offers.

So this module does the thing that IS defensible and IS useful:

  1. BENCHMARK POSITION  — is your current price above or below the market?
                           Computed from live index vs. your contract formula.
  2. LEVER VALUATION     — what each concession is worth TO YOU, in rupees,
                           as arithmetic a category manager can audit.
  3. BATNA               — your credible walk-away, built from the country
                           ranking and the approved-supplier list. This is the
                           only thing that actually creates leverage.
  4. LEVERAGE READ       — share of wallet, capacity slack, switching cost.
  5. CONCESSION LADDER   — ordered ask list: cheapest-for-them-first.

What it does NOT do: predict whether the supplier will say yes. A category
manager with fifteen years in the market knows that better than any model, and
pretending otherwise is how these tools lose credibility in the first meeting.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Lever:
    name: str
    annual_value_inr: float | None
    basis: str
    certainty: str                # high | medium | low | needs-internal-data
    ask: str                      # what to actually say
    likely_cost_to_supplier: str  # low | medium | high — drives the ask order

    @property
    def sort_key(self):
        order = {"low": 0, "medium": 1, "high": 2}
        return (order.get(self.likely_cost_to_supplier, 3),
                -(self.annual_value_inr or 0))


@dataclass
class NegotiationBrief:
    supplier_id: str
    supplier_name: str
    material_id: str
    annual_tonnes: float
    annual_spend_inr: float
    current_price_inr_per_t: float
    benchmark_price_inr_per_t: float | None
    premium_vs_benchmark_inr_per_t: float | None
    premium_vs_benchmark_pct: float | None
    batna_supplier: str | None
    batna_landed_inr_per_t: float | None
    batna_gap_inr_per_t: float | None
    batna_credible: bool
    share_of_wallet_pct: float | None
    capacity_utilisation_pct: float | None
    levers: list = field(default_factory=list)
    talking_points: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def value_levers(annual_spend_inr: float,
                 current_credit_days: int, target_credit_days: int,
                 cost_of_capital_pa: float,
                 volume_flex_pct: float,
                 price_vol_pct: float,
                 index_reset_current: str,
                 rejection_rate_pct: float,
                 annual_tonnes: float,
                 price_inr_per_t: float) -> list[Lever]:
    """Price each lever from the BUYER's side. Pure arithmetic."""
    levers: list[Lever] = []

    # 1. Credit terms — exact, uncontroversial, and usually cheap for a
    #    supplier with better funding access than you.
    extra_days = max(0, target_credit_days - current_credit_days)
    credit_val = annual_spend_inr * cost_of_capital_pa * (extra_days / 365.0)
    levers.append(Lever(
        f"Credit terms {current_credit_days}d -> {target_credit_days}d",
        credit_val,
        f"{extra_days} extra days x {cost_of_capital_pa:.1%} WACC on Rs {annual_spend_inr:,.0f}",
        "high",
        f"Move to {target_credit_days}-day payment terms on the annual volume.",
        "low",
    ))

    # 2. Volume flexibility — real option on the flexible tranche.
    #    Haircut hard: you only capture it if you can actually flex production.
    flex_val = annual_spend_inr * volume_flex_pct * (price_vol_pct / 100.0) * 0.40
    levers.append(Lever(
        f"Volume flexibility +/-{volume_flex_pct:.0%}",
        flex_val,
        f"optionality on {volume_flex_pct:.0%} of spend at {price_vol_pct:.1f}% vol, "
        f"haircut to 40% of intrinsic",
        "medium",
        f"Widen the volume band to +/-{volume_flex_pct:.0%} without price penalty.",
        "medium",
    ))

    # 3. Index reset frequency — risk reduction, NOT a saving. Sign depends on
    #    trend direction, which is exactly what you cannot predict.
    if index_reset_current.lower() == "quarterly":
        reset_val = annual_spend_inr * (price_vol_pct / 100.0) * 0.25 * 0.15
        levers.append(Lever(
            "Quarterly -> monthly index reset",
            reset_val,
            "reduced mark-to-market drift between resets",
            "low",
            "Reset the index monthly rather than quarterly.",
            "low",
        ))

    # 4. Quality / rejection — straight cost recovery, well evidenced by your
    #    own goods-receipt data. Usually the easiest ask to justify.
    if rejection_rate_pct > 0.3:
        excess = (rejection_rate_pct - 0.3) / 100.0
        qual_val = annual_tonnes * price_inr_per_t * excess
        levers.append(Lever(
            f"Rejection rate {rejection_rate_pct:.2f}% -> 0.30%",
            qual_val,
            f"{excess:.2%} of volume currently scrapped above a 0.30% benchmark",
            "high",
            "Commit to a 0.30% maximum rejection rate with credit for excess.",
            "medium",
        ))

    # 5. Demurrage / documentary delay — genuinely cannot be priced without
    #    your own claim history. Say so rather than inventing a number.
    levers.append(Lever(
        "Seller absorbs demurrage from documentary delay",
        None,
        "price from your own demurrage claim history — no market proxy exists",
        "needs-internal-data",
        "Seller bears demurrage where the cause is late or incorrect documentation.",
        "low",
    ))

    return sorted(levers, key=lambda x: x.sort_key)


def build_brief(supplier, contract, coverage_position,
                current_price_inr_per_t: float,
                benchmark_price_inr_per_t: float | None,
                batna: tuple[str, float] | None,
                annual_tonnes: float,
                price_vol_pct: float,
                cost_of_capital_pa: float = 0.09,
                target_credit_days: int = 60,
                supplier_annual_capacity_t: float | None = None) -> NegotiationBrief:
    """
    Assemble a negotiation brief for one supplier.

    `batna` is (supplier_or_country_label, landed_cost_inr_per_t) — the best
    credible alternative, normally taken from country_rank.py filtered to
    approved or qualifiable sources.
    """
    spend = annual_tonnes * current_price_inr_per_t

    prem = prem_pct = None
    if benchmark_price_inr_per_t:
        prem = current_price_inr_per_t - benchmark_price_inr_per_t
        prem_pct = prem / benchmark_price_inr_per_t * 100

    batna_name = batna_cost = batna_gap = None
    batna_credible = False
    if batna:
        batna_name, batna_cost = batna
        batna_gap = current_price_inr_per_t - batna_cost
        # An alternative is only credible if it can actually cover the horizon.
        batna_credible = (supplier.lead_time_days_p90 or 999) <= 75

    sow = None
    if supplier_annual_capacity_t:
        sow = min(100.0, annual_tonnes / supplier_annual_capacity_t * 100)

    levers = value_levers(
        annual_spend_inr=spend,
        current_credit_days=contract.credit_days,
        target_credit_days=target_credit_days,
        cost_of_capital_pa=cost_of_capital_pa,
        volume_flex_pct=max(contract.volume_flex_pct, 0.10),
        price_vol_pct=price_vol_pct,
        index_reset_current=contract.index_reset,
        rejection_rate_pct=supplier.reject_rate_pct,
        annual_tonnes=annual_tonnes,
        price_inr_per_t=current_price_inr_per_t,
    )

    points, warns = [], []

    if prem_pct is not None:
        if prem_pct > 1.0:
            points.append(
                f"You are paying Rs {prem:,.0f}/t ({prem_pct:+.1f}%) ABOVE the current "
                f"market benchmark. On {annual_tonnes:,.0f} t/yr that is "
                f"Rs {prem * annual_tonnes:,.0f}. Open with this.")
        elif prem_pct < -1.0:
            points.append(
                f"You are Rs {abs(prem):,.0f}/t ({prem_pct:.1f}%) BELOW benchmark. "
                f"Do not open on price — protect the position and trade for terms instead.")
        else:
            points.append(
                f"Price is within {abs(prem_pct):.1f}% of benchmark — at market. "
                f"Terms, not price, are where the value is in this negotiation.")

    if batna_gap is not None and batna_name:
        if batna_gap > 0 and batna_credible:
            points.append(
                f"Credible alternative: {batna_name} at Rs {batna_cost:,.0f}/t landed, "
                f"Rs {batna_gap:,.0f}/t below your current price. This is your leverage — "
                f"but it is only real if that source is qualified and can meet lead time.")
        elif batna_gap > 0:
            warns.append(
                f"{batna_name} is cheaper on paper (Rs {batna_gap:,.0f}/t) but the "
                f"lead-time P90 of {supplier.lead_time_days_p90:.0f}d makes it NOT credible "
                f"for this horizon. Do not bluff with it — an experienced seller will "
                f"know your qualification status.")
        else:
            points.append(
                f"No cheaper credible alternative found ({batna_name} is "
                f"Rs {abs(batna_gap):,.0f}/t more expensive). Your leverage is weak on "
                f"price; concentrate entirely on terms.")
            if prem is not None and prem > 0:
                points.append(
                    f"RECONCILE THESE TWO: you are above the DOMESTIC index but the "
                    f"IMPORT alternative is still dearer. So the Rs {prem:,.0f}/t "
                    f"premium is real but not a walk-away threat. Use it to justify "
                    f"a terms package, not a price ultimatum — and be ready for them "
                    f"to point out the same thing.")

    if sow is not None:
        if sow < 10:
            warns.append(
                f"You are ~{sow:.1f}% of this supplier's capacity. Low share of wallet "
                f"means low leverage — expect resistance on price, more give on terms.")
        elif sow > 30:
            points.append(
                f"You represent ~{sow:.0f}% of their capacity. That is material to them; "
                f"a multi-year volume commitment is a real card to play.")

    if supplier.otif_pct < 93:
        points.append(
            f"OTIF is {supplier.otif_pct:.1f}%. Service failure is a legitimate, "
            f"documented lever — attach a delivery-performance credit to the renewal.")

    if getattr(coverage_position, "is_mock", False):
        warns.append("Coverage figures are MOCK ERP data. Do not take this brief into "
                     "a real negotiation until the ERP feed is live.")
    if coverage_position.coverage_weeks < 3:
        warns.append(
            f"Coverage is only {coverage_position.coverage_weeks:.1f} weeks. You are "
            f"NEGOTIATING FROM WEAKNESS — the supplier can wait you out. Secure "
            f"tonnage first, negotiate terms at the next window.")

    return NegotiationBrief(
        supplier_id=supplier.supplier_id, supplier_name=supplier.name,
        material_id=contract.material_id, annual_tonnes=annual_tonnes,
        annual_spend_inr=spend, current_price_inr_per_t=current_price_inr_per_t,
        benchmark_price_inr_per_t=benchmark_price_inr_per_t,
        premium_vs_benchmark_inr_per_t=prem, premium_vs_benchmark_pct=prem_pct,
        batna_supplier=batna_name, batna_landed_inr_per_t=batna_cost,
        batna_gap_inr_per_t=batna_gap, batna_credible=batna_credible,
        share_of_wallet_pct=sow,
        capacity_utilisation_pct=None,
        levers=levers, talking_points=points, warnings=warns,
    )


def render(b: NegotiationBrief) -> str:
    L = []
    L.append(f"NEGOTIATION BRIEF — {b.supplier_name} ({b.supplier_id})")
    L.append("=" * 78)
    L.append(f"  Material              {b.material_id}")
    L.append(f"  Annual volume         {b.annual_tonnes:,.0f} t")
    L.append(f"  Annual spend          Rs {b.annual_spend_inr:,.0f}")
    L.append(f"  Current price         Rs {b.current_price_inr_per_t:,.0f}/t")
    if b.benchmark_price_inr_per_t:
        L.append(f"  Market benchmark      Rs {b.benchmark_price_inr_per_t:,.0f}/t"
                 f"   ({b.premium_vs_benchmark_pct:+.1f}%)")
    if b.batna_landed_inr_per_t:
        cred = "CREDIBLE" if b.batna_credible else "not credible for this horizon"
        L.append(f"  Best alternative      {b.batna_supplier} at "
                 f"Rs {b.batna_landed_inr_per_t:,.0f}/t  [{cred}]")
    if b.share_of_wallet_pct is not None:
        L.append(f"  Share of their capacity  ~{b.share_of_wallet_pct:.1f}%")

    L.append("\n  POSITION")
    for p in b.talking_points:
        L.append(f"   - {p}")

    L.append("\n  ASK IN THIS ORDER (cheapest for them first — protects the relationship)")
    total = 0.0
    for i, lv in enumerate(b.levers, 1):
        val = f"Rs {lv.annual_value_inr:,.0f}/yr" if lv.annual_value_inr is not None else "unpriced"
        if lv.annual_value_inr:
            total += lv.annual_value_inr
        L.append(f"   {i}. {lv.name}")
        L.append(f"      worth {val}  |  their cost: {lv.likely_cost_to_supplier}  "
                 f"|  certainty: {lv.certainty}")
        L.append(f"      say: \"{lv.ask}\"")
        L.append(f"      basis: {lv.basis}")
    L.append(f"\n  Total priced value on the table: Rs {total:,.0f}/yr "
             f"({total / b.annual_spend_inr * 100:.2f}% of spend)")

    if b.warnings:
        L.append("\n  WARNINGS")
        for w in b.warnings:
            L.append(f"   ! {w}")

    L.append("\n  This brief prices levers from YOUR side only. It does not predict")
    L.append("  what the supplier will accept — no such data exists (see R-11).")
    return "\n".join(L)


if __name__ == "__main__":
    import erp as E

    src = E.MockERP()
    cov = E.coverage(src, "HRC-E250A-2.0", "PLANT-PUNE-1")
    sup = next(s for s in src.suppliers("HRC-E250A-2.0") if s.supplier_id == "SUP-DOM-01")
    ctr = src.contracts("HRC-E250A-2.0")[0]

    brief = build_brief(
        supplier=sup, contract=ctr, coverage_position=cov,
        current_price_inr_per_t=60_800,
        benchmark_price_inr_per_t=59_650,       # Tata Nexarc median, live
        batna=("Japan (import, 0% preferential + 11.5% safeguard)", 62_100),
        annual_tonnes=7_200, price_vol_pct=23.5,
        supplier_annual_capacity_t=sup.annual_capacity_t,
    )
    print(render(brief))

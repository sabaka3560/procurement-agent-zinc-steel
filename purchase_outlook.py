"""Purchase timing, source-country screening and evidence-backed supplier RFQs.

No vendor prices, discounts, approval or delivery promises are inferred from
product listings. Demo shocks never modify live observations or price history.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from functools import lru_cache
from pathlib import Path

import numpy as np
import countries as CO


def validate_inputs(payload):
    def number(key, default, low, high):
        value = payload.get(key, default)
        if isinstance(value, bool):
            raise ValueError(f"{key} must be a number")
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number") from None
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
        return value

    preferred = str(payload.get("preferred_source_country", "ANY")).upper()
    if preferred != "ANY" and not CO.get(preferred):
        raise ValueError("Unknown preferred source country")
    demo = payload.get("demo_threshold_crossed", False)
    if not isinstance(demo, bool):
        raise ValueError("demo_threshold_crossed must be true or false")
    baseline = payload.get("monitoring_baseline_price")
    if baseline is not None:
        baseline = number("monitoring_baseline_price", None, 0.000001, 1e12)
    return {
        "safe_value_pct": number("safe_value_pct", 5, 0.1, 100),
        "preferred_source_country": preferred,
        "demo_threshold_crossed": demo,
        "monitoring_baseline_price": baseline,
    }


def research():
    # Reread this small catalogue so research updates appear without stale cache.
    import supplier_catalogue
    data = supplier_catalogue.load()
    import supplier_refresh
    try:
        data["refresh_status"] = supplier_refresh.read_status()
    except Exception:
        data["refresh_status"] = {"last_run": None, "publications": {}, "note": "Automatic-check history unavailable; no freshness claim can be made."}
    for supplier in data["suppliers"]:
        supplier["automatic_check"] = data["refresh_status"]["publications"].get(supplier["url"])
    return data


def _sample_ratios(prices, steps, simulations, seed):
    """Fixed five-observation moving-block bootstrap; return terminal ratios."""
    returns = np.diff(np.log(np.asarray(prices, dtype=float)))
    block = min(5, len(returns))
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, len(returns) - block + 1,
                          size=(simulations, math.ceil(steps / block)))
    paths = returns[starts[..., None] + np.arange(block)].reshape(simulations, -1)
    return np.exp(paths[:, :steps].sum(axis=1))


@lru_cache(maxsize=24)
def distributions(prices):
    """60/90 calendar days mapped approximately to 42/63 trading observations.

    Non-overlapping forecast outcomes for the calibration diagnostic. Training
    sets still overlap: the windows are not claimed statistically independent.
    """
    if len(prices) < 40 or any(not math.isfinite(p) or p <= 0 for p in prices):
        raise ValueError("At least 40 finite positive price observations are required")
    rows = []
    for days, steps in ((30, 21), (60, 42), (90, 63)):
        ratios = _sample_ratios(prices, steps, 8000, 73 + steps)
        hits = count = 0
        for origin in range(59, len(prices) - steps, steps):
            history = prices[:origin + 1]
            sims = prices[origin] * _sample_ratios(history, steps, 1000, 101 + origin)
            low, high = np.quantile(sims, [.1, .9])
            hits += int(low <= prices[origin + steps] <= high)
            count += 1
        rows.append({
            "calendar_days": days, "trading_steps": steps,
            "ratios": ratios,
            "calibration": {"nominal_coverage": .8,
                "empirical_coverage": hits / count if count else None,
                "nonoverlapping_outcome_windows": count,
                "note": "Sparse historical check; shared training data. Not proof of predictive skill."},
        })
    return rows


def country_options(ranked, destination, preferred, hs6, benchmark, fx):
    """Retain provenance, valuation basis and tariff uncertainty in the UI."""
    import tariff as T
    import country_rank as CR
    rows = []
    for r in ranked:
        rows.append({
            "country": r.origin, "name": r.origin_name,
            "preferred": r.origin == preferred,
            "cif_usd_per_t": r.fob_usd_per_t,
            "screen_cost_local": round(r.landed_usd_per_t * fx, 2) if r.landed_usd_per_t else None,
            "duty_pct": r.duty_pct, "duty_basis": r.duty_basis,
            "duty_verified": r.duty_decision_grade,
            "surcharges": r.surcharges,
            "freight_estimate_usd_per_t": r.freight_usd_per_t,
            "source": r.fob_source,
            "notes": list(r.notes) + ["CIF includes international freight; the displayed freight estimate is not added again.",
                "Historical HS4 country average plus duties; excludes local delivery, finance and recoverable taxes. Not a supplier quote."],
            "price_basis": "HISTORICAL_IMPORT_SCREEN",
        })
    # A domestic benchmark is a different basis; never mix it into the cheapest
    # import comparison or pretend a foreign exchange price is local delivery.
    if destination == "IND":
        rows.append({"country": destination, "name": CO.get(destination).name,
            "preferred": preferred == destination, "cif_usd_per_t": None,
            "screen_cost_local": benchmark, "duty_pct": 0, "duty_basis": "Domestic purchase: no import duty",
            "duty_verified": False, "surcharges": [], "freight_estimate_usd_per_t": None,
            "source": "Current material reference benchmark; see price observation",
            "notes": ["Benchmark only. Physical premium, delivery and applicable domestic taxes need a quote."],
            "price_basis": "DOMESTIC_BENCHMARK"})
    if preferred != "ANY" and not any(r["country"] == preferred for r in rows):
        total, tariff, surcharges = None, None, []
        errors = []
        if preferred != destination:
            try:
                total, tariff, surcharges = T.total_duty_pct(destination, preferred, hs6)
            except Exception as exc:
                errors.append(f"Tariff lookup unavailable: {type(exc).__name__}")
        freight, _ = CR.freight_estimate(preferred, destination)
        rows.append({"country": preferred, "name": CO.get(preferred).name, "preferred": True,
            "cif_usd_per_t": None, "screen_cost_local": None,
            "duty_pct": total if preferred != destination else 0,
            "duty_basis": tariff.source if tariff else "Not available / domestic route",
            "duty_verified": bool(tariff and tariff.usable_for_decision),
            "surcharges": surcharges, "freight_estimate_usd_per_t": freight,
            "source": "No comparable country price returned",
            "notes": errors + ["Get a current supplier quotation before comparing landed cost."],
            "price_basis": "UNAVAILABLE"})
    return rows


def supplier_shortlist(material, preferred, countries, quantity):
    data = research()
    by_country = {r["country"]: r for r in countries}
    # Preference first, then countries with screening evidence, then alphabetic
    # ties. Never imply a named supplier offers the country's average price.
    def key(s):
        country = by_country.get(s["country"], {})
        priced_import = country.get("price_basis") == "HISTORICAL_IMPORT_SCREEN" and country.get("screen_cost_local") is not None
        return (0 if s["country"] == preferred else 1,
                0 if priced_import else 1,
                country["screen_cost_local"] if priced_import else float("inf"), s["name"])
    candidates = sorted((s for s in data["suppliers"] if s["material"] == material), key=key)
    result = []
    for index, s in enumerate(candidates[:3], 1):
        row = dict(s)
        row.update({"rank": index, "country_name": CO.get(s["country"]).name,
            "preferred_country_match": s["country"] == preferred,
            "rank_reason": ("Matches preferred country. " if s["country"] == preferred else "Alternative country. ") +
                "Published product-family evidence. Country screening cost orders comparable import alternatives; supplier-level price ranking requires RFQs.",
            "country_screen": by_country.get(s["country"]),
            "reviewed_at": data["reviewed_at"], "supplier_quote": None,
            "approval_status": "Not yet qualified for this plant/specification",
            "quantity_discount": {"status": "RFQ_REQUIRED", "percent": None,
                "note": "No quantity-discount schedule verified in the cited product publication; this does not mean no discount is available.",
                "rfq_tiers_t": sorted(set(round(quantity * m, 1) for m in (1, 1.5, 2) if quantity > 0)),
                "request": "Request price per tonne at each tier, minimum order, validity, Incoterm, credit terms, delivery date, origin certificate and quality certificate. Compare total cost before increasing volume."},
        })
        if s["id"] == "HZL-IND":
            row["quantity_discount"]["historical_lead"] = {
                "date": "2025-06-09", "status": "HISTORICAL_UNCONFIRMED_TODAY",
                "url": "https://rashtriyametal.com/wp-content/uploads/2025/06/HZL09062025-1.pdf",
                "summary": "A distributor-hosted HZL circular listed INR 1,000/t up to 9t and INR 2,000/t above 9t of monthly zinc lifting, plus a conditional INR 1,000/t quantity-confirmation discount. Current applicability is unverified; request the latest circular.",
                "applied_to_cost": False,
            }
        result.append(row)
    return result


def build_outlook(*, inputs, material, destination, hs6, prices, benchmark,
                  observation, series_basis, monthly_demand, usable_stock,
                  safety_stock, policy_weeks, ranked, fx):
    today = dt.datetime.now(dt.timezone.utc)
    prices = tuple(float(p) for p in prices)
    baseline = inputs["monitoring_baseline_price"] or benchmark
    safe_pct = inputs["safe_value_pct"]
    threshold = baseline * (1 + safe_pct / 100)
    simulated = inputs["demo_threshold_crossed"]
    evaluated_price = max(benchmark, threshold * 1.001) if simulated else benchmark
    triggered = evaluated_price > threshold
    daily = monthly_demand / 30
    reserve = max(safety_stock, daily * policy_weeks * 7)
    # Undated pipeline cannot establish that stock arrives before a shortage.
    safe_wait_days = max(0, (usable_stock - reserve) / daily) if daily > 0 else 3650
    rows = []
    for d in distributions(prices):
        days = d["calendar_days"]
        scenario_prices = benchmark * d["ratios"]
        p10, p50, p90 = np.quantile(scenario_prices, [.1, .5, .9])
        # Compare the same eventual quantity: buying now entails carrying cost.
        buy_now_at_use = evaluated_price * (1 + .14 * days / 365)
        rows.append({"calendar_days": days, "trading_steps": d["trading_steps"],
            "target_date": (today.date() + dt.timedelta(days=days)).isoformat(),
            "p10": round(float(p10), 2), "p50": round(float(p50), 2), "p90": round(float(p90), 2),
            "probability_cheaper_than_now": round(float(np.mean(scenario_prices < evaluated_price)), 3),
            "probability_wait_beats_buy_and_carry": round(float(np.mean(scenario_prices < buy_now_at_use)), 3),
            "median_saving_vs_now_pct": round(100 * (evaluated_price - p50) / evaluated_price, 2),
            "buy_now_plus_carry_per_t": round(buy_now_at_use, 2),
            "waiting_feasible_on_known_stock": days <= safe_wait_days,
            "bridge_supply_needed_t": round(max(0, daily * days + reserve - usable_stock), 1),
            "calibration": d["calibration"],
        })
    # A freshly computed model over old observations is not a fresh market call.
    try:
        source_time = dt.datetime.fromisoformat(observation["observed_at"].replace("Z", "+00:00"))
        source_age_days = (today - source_time).total_seconds() / 86400
        stale = not 0 <= source_age_days <= 3
        history_time = dt.datetime.fromisoformat(observation.get("history_observed_at", observation["observed_at"]).replace("Z", "+00:00"))
        stale = stale or not 0 <= (today - history_time).total_seconds() / 86400 <= 3
    except (KeyError, TypeError, ValueError):
        source_age_days, stale = None, True
    economic = min(rows[1:], key=lambda r: r["p50"])
    waiting = [r for r in rows[1:] if r["waiting_feasible_on_known_stock"]
               and r["p50"] < evaluated_price and r["probability_cheaper_than_now"] >= .6]
    if daily <= 0:
        action, chosen, quantity = "NO_PURCHASE_REQUIREMENT", 0, 0
        reason = "No consumption entered. Monitor prices; no volume purchase is justified by this input."
    elif safe_wait_days < 30:
        action, chosen = "SECURE_NEAR_TERM_SUPPLY", 0
        quantity = max(0, daily * 30 + reserve - usable_stock)
        reason = (f"Known on-hand stock preserves the reserve for {safe_wait_days:.1f} days. "
                  "Secure dated supply now; a cheaper 2–3 month scenario cannot close an earlier shortage.")
    elif waiting:
        best = min(waiting, key=lambda r: r["p50"])
        action, chosen, quantity = "WAIT_AND_REVIEW", best["calendar_days"], 0
        reason = (f"The {chosen}-day scenario has a lower median and at least 60% model probability of a lower price. "
                  "Known stock permits waiting under constant consumption. Recheck each market refresh; this is conditional scenario advice.")
    else:
        action, chosen = "REVIEW_QUOTES_NOW", 0
        quantity = max(0, daily * 30 + reserve - usable_stock)
        reason = "No feasible 2–3 month scenario clears the lower-price probability rule. Request current quotes; do not buy extra solely because the alert fired."
    if stale and action not in ("SECURE_NEAR_TERM_SUPPLY", "NO_PURCHASE_REQUIREMENT"):
        action, chosen, quantity = "REFRESH_MARKET_DATA", 0, 0
        reason = "The benchmark or history observation is stale or undated. Price ranges are illustrative; refresh the source before a timing decision."
    countries = country_options(ranked, destination, inputs["preferred_source_country"], hs6, benchmark, fx)
    suppliers = supplier_shortlist(material, inputs["preferred_source_country"], countries,
                                  quantity if quantity > 0 else monthly_demand)
    selected_country = suppliers[0]["country"] if suppliers else None
    imports = [r for r in countries if r["price_basis"] == "HISTORICAL_IMPORT_SCREEN" and r["screen_cost_local"] is not None]
    cheapest = min(imports, key=lambda r: r["screen_cost_local"]) if imports else None
    selected = next((r for r in countries if r["country"] == selected_country), None)
    for country in countries:
        cost = country["screen_cost_local"]
        country["future_screen_scenarios"] = [
            {"calendar_days": r["calendar_days"], "p10": round(cost * r["p10"] / benchmark, 2),
             "p50": round(cost * r["p50"] / benchmark, 2), "p90": round(cost * r["p90"] / benchmark, 2)}
            for r in rows[1:]] if cost is not None else []
    return {
        "generated_at": today.isoformat(timespec="seconds"),
        "currency": "INR" if destination == "IND" else "USD", "unit": "per metric tonne",
        "mode": "DEMO_SIMULATION" if simulated else "LATEST_AVAILABLE_MARKET_DATA",
        "price_observation": observation, "live_reference_price": benchmark,
        "source_age_days": round(source_age_days, 2) if source_age_days is not None else None,
        "market_data_stale": stale,
        "series_basis": series_basis,
        "trigger": {"safe_value_pct": safe_pct, "baseline_price": baseline,
            "threshold_price": round(threshold, 2), "evaluated_price": round(evaluated_price, 2),
            "change_pct": round(100 * (evaluated_price / baseline - 1), 2), "crossed": triggered,
            "baseline_initialized": inputs["monitoring_baseline_price"] is None,
            "note": "Demo changes only the comparison price. Live history, quotes, country costs and supplier evidence remain unchanged." if simulated else "Threshold crossing triggers a review, not an automatic purchase."},
        "forecast": rows,
        "timing": {"action": action, "buy_after_days": chosen, "request_quantity_now_t": round(quantity, 1),
            "reason": reason, "safe_wait_days": round(safe_wait_days, 1),
            "lowest_median_horizon_days": economic["calendar_days"],
            "preferred_country": inputs["preferred_source_country"],
            "recommended_country_for_rfq": selected_country,
            "recommended_country_name": CO.get(selected_country).name if selected_country else None,
            "country_reason": "Top researched supplier's country; preference is a priority, not a mandatory exclusion. Confirm shipment origin and obtain comparable quotes.",
            "preferred_country_has_researched_suppliers": any(s["preferred_country_match"] for s in suppliers),
            "lowest_import_screen_country": cheapest["country"] if cheapest else None,
            "preferred_premium_vs_lowest_import_pct": round(100 * (selected["screen_cost_local"] / cheapest["screen_cost_local"] - 1), 2)
                if selected and cheapest and selected["price_basis"] == "HISTORICAL_IMPORT_SCREEN" and selected["screen_cost_local"] else None},
        "countries": countries, "top_suppliers": suppliers,
        "supplier_research_note": research()["method"],
        "assumptions": ["On-hand feasibility assumes constant consumption. Undated in-transit and open POs are excluded; confirm their receipt dates before changing the purchase quantity.",
            "Reserve is the larger of entered safety stock and weeks-of-cover policy, to avoid counting the same buffer twice.",
            "Forecast uses a five-observation block bootstrap, 8,000 scenarios and about 21 trading observations per calendar month; exchange holidays are approximated.",
            "14% annual carrying cost is an illustrative assumption. The 60% lower-price threshold is a configurable-policy starting point, not a validated success rate.",
            "Country future ranges scale historical screening costs with the benchmark scenario; FX, duties and physical premiums are held constant. They are not live supplier forecasts.",
            "No supplier quotation, volume discount, qualification or delivery commitment is inferred. Top three are RFQ candidates, not approved purchase awards."],
    }

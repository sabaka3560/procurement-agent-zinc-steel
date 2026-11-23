"""Read-only, inventory-first procurement advice. Never creates an order or PAR.

Country prices are historical trade screens, not vendor quotes. ETA is an
explicit buyer planning input, not a shipping commitment. Research gaps remain
gaps; a three-row layout must never cause fabricated supplier identities.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
import datetime as dt
import math
import json
from pathlib import Path

import numpy as np
import connectors as C
import countries as CO
import country_rank as CR
import purchase_outlook as O
import supplier_catalogue


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    def number(name, default, low=0, high=1e7):
        raw = payload.get(name, default)
        if isinstance(raw, bool):
            raise ValueError(f"{name}: enter a number")
        try:
            value = float(raw)
        except (ValueError, TypeError):
            raise ValueError(f"{name}: enter a number") from None
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{name}: must be between {low} and {high}")
        return value
    material = payload.get("material", "ZINC_SHG")
    if material not in ("ZINC_SHG", "STEEL_HRC"):
        raise ValueError("Choose zinc SHG or steel HRC")
    destination = str(payload.get("destination", "IND")).upper()
    preferred = str(payload.get("preferred_source_country", "IND")).upper()
    if not CO.get(destination) or (preferred != "ANY" and not CO.get(preferred)):
        raise ValueError("Choose a supported destination and preferred country")
    demo = payload.get("demo_stock_breach", False)
    if not isinstance(demo, bool):
        raise ValueError("demo_stock_breach must be true or false")
    return dict(material=material, destination=destination,
        preferred_source_country=preferred, demo_stock_breach=demo,
        plant=str(payload.get("plant", "Pune plant · sample inputs"))[:120],
        specification=str(payload.get("specification", ""))[:240],
        monthly_demand_t=number("monthly_demand_t", 300),
        on_hand_t=number("on_hand_t", 450), safety_stock_t=number("safety_stock_t", 100, .01),
        safe_value_pct=number("safe_value_pct", 10, 0, 99),
        domestic_lead_days=number("domestic_lead_days", 7, 1, 365),
        import_lead_days=number("import_lead_days", 35, 1, 365),
        planning_months=number("planning_months", 1, 1, 3))


def fresh(timestamp):
    try:
        observed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        age = (dt.datetime.now(dt.timezone.utc) - observed).total_seconds() / 86400
        return 0 <= age <= 3
    except (ValueError, TypeError, AttributeError):
        return False


def fetch_market(material, destination):
    # Keep model history separate from the price level for cross-region proxies.
    if material == "ZINC_SHG":
        benchmark, _, _, history = C.lme_zinc_westmetall()
        prices = [float(r[1]) for r in reversed(history)]
        history_obs = benchmark
        basis = "LME zinc cash history; physical premiums and delivery excluded."
        level = benchmark.value
    else:
        history_obs, history = C.steel_hrc_futures()
        prices = [float(r[1]) for r in history]
        if destination == "IND":
            benchmark, _ = C.india_hrc_nexarc()
            level = benchmark.value
            basis = "Indian HRC reference level with US CME HRC return scenarios. Cross-region proxy, not a validated Indian price forecast."
        else:
            benchmark = history_obs
            level = benchmark.value / .90718474
            basis = "US CME HRC futures proxy, converted from short tons to metric tonnes. Not a local physical price."
    fx_obs = None
    if destination == "IND":
        try:
            fx_obs = C.fx_usd_inr_fbil()
        except Exception:
            pass
    currency = "INR" if destination == "IND" else "USD"
    fx = fx_obs.value if fx_obs else None
    if material == "ZINC_SHG" and destination == "IND":
        if fx is None:
            # Preserve the real USD observation instead of inventing an INR rate.
            currency = "USD"
        else:
            level *= fx
    return dict(benchmark=level, prices=prices, observation=asdict(benchmark),
        history_observation=asdict(history_obs), history_count=len(prices),
        history_start=history[-1][0] if material == "ZINC_SHG" else history[0][0],
        history_end=history[0][0] if material == "ZINC_SHG" else history[-1][0],
        history_observed_at=history_obs.observed_at, basis=basis, currency=currency,
        fx=fx, fx_observation=asdict(fx_obs) if fx_obs else None,
        fresh=fresh(benchmark.observed_at) and fresh(history_obs.observed_at)
              and (currency != "INR" or material == "STEEL_HRC" or bool(fx_obs and fresh(fx_obs.observed_at))))


def evaluate(payload, progress=None):
    inputs = validate(payload)
    def report(stage, status, detail):
        if progress:
            progress(dict(stage=stage, status=status, detail=detail))
    hs6 = "790111" if inputs["material"] == "ZINC_SHG" else "720810"
    errors, market, ranked = [], None, []
    report("market", "running", "Requesting latest available benchmark, price history and required FX. Connectors may reuse cached observations; this is not a live exchange tick feed.")
    report("countries", "running", "Screening historical import costs, tariffs and logistics for the buying country. Public trade and tariff services can take several minutes.")
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {
            "market": pool.submit(fetch_market, inputs["material"], inputs["destination"]),
            "countries": pool.submit(CR.rank_source_countries, inputs["destination"], hs6,
                                     year=dt.datetime.now(dt.timezone.utc).year, top_n=50),
        }
        labels = {job: label for label, job in jobs.items()}
        for job in as_completed(labels):
            label = labels[job]
            try:
                value = job.result()
                if label == "market":
                    market = value
                else:
                    ranked = value
                detail = ("Market observations received; checking their dates, not assuming they are live." if label == "market"
                          else f"Historical trade screen returned {len(ranked)} country candidates; these are not current supplier quotes.")
                report(label, "done" if value else "warning", detail)
            except Exception as exc:
                errors.append(f"{label.title()} unavailable ({type(exc).__name__}). Retry or request supplier quotes.")
                report(label, "warning", errors[-1])
    if not ranked:
        errors.append("No historical country trade screen returned. The list contains research leads, not a cheapest-country ranking.")
    report("analysis", "running", "Calculating stock coverage and 1–3-month model scenarios; matching candidates from the stored supplier research catalogue, not searching supplier websites live.")
    result = build(inputs, market, ranked, errors)
    report("analysis", "done", f"Calculated {len(result['forecast'])} forecast horizons and shortlisted {len(result['countries'])} countries. Supplier prices, discounts and delivery commitments remain unverified.")
    # Preference can fall outside Comtrade's reported flows. Still resolve its
    # tariff, without manufacturing a price or treating the duty as verified.
    import tariff as T
    missing = [c for c in result["countries"] if not c["domestic"] and c["duty_pct"] is None]
    report("tariffs", "running", f"Resolving missing tariff references for {len(missing)} shortlisted routes, including your preference where needed.")
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = [(c, pool.submit(T.total_duty_pct, inputs["destination"], c["country"], hs6)) for c in missing]
        for c, job in jobs:
            try:
                total, tariff, surcharges = job.result()
                c.update(duty_pct=total, duty_basis=tariff.source,
                         duty_verified=tariff.usable_for_decision, surcharges=surcharges)
                c["notes"].extend(tariff.notes)
            except Exception:
                c["notes"].append("Tariff lookup failed; confirm with customs before any commitment.")
    unresolved = any(c["duty_pct"] is None for c in missing)
    report("tariffs", "warning" if unresolved else "done", "Tariff lookup finished. Missing or unverified duties still require customs confirmation; this is not purchasing clearance.")
    import prediction_sources
    report("sources", "running", "Consolidating input sources, observation dates, forecast methods and data gaps into the recommendation.")
    result = prediction_sources.attach(result)
    report("sources", "done", "Source trail assembled. Advisory result ready; no suppliers contacted and no order created.")
    return result


def build(i, market, ranked, errors=None):
    errors = list(errors or [])
    now = dt.datetime.now(dt.timezone.utc)
    daily = i["monthly_demand_t"] / 30
    threshold = i["safety_stock_t"] * (1 - i["safe_value_pct"] / 100)
    stock = threshold if i["demo_stock_breach"] else i["on_hand_t"]
    safe_days = max(0, (stock - i["safety_stock_t"]) / daily) if daily else None
    stockout_days = stock / daily if daily else None
    currency = market["currency"] if market else ("INR" if i["destination"] == "IND" else "USD")
    fx = market.get("fx") if market and currency == "INR" else 1
    countries = []
    for r in ranked:
        countries.append(dict(country=r.origin, name=r.origin_name,
            screen_cost=round(r.landed_usd_per_t * fx, 2) if r.landed_usd_per_t is not None and fx else None,
            price_basis=r.fob_source, duty_pct=r.duty_pct, duty_basis=r.duty_basis,
            duty_verified=r.duty_decision_grade, surcharges=r.surcharges,
            freight_usd_per_t=r.freight_usd_per_t, lpi=r.lpi_score,
            proven_tonnes=r.proven_tonnes, notes=list(r.notes), historical=True))
    data = O.research()
    catalog = [s for s in data["suppliers"] if s["material"] == i["material"]]
    # Unpriced researched alternatives remain visible, but cannot win a price ranking.
    extra = {s["country"] for s in catalog} | {i["destination"]}
    if i["preferred_source_country"] != "ANY":
        extra.add(i["preferred_source_country"])
    for code in sorted(extra):
        if any(c["country"] == code for c in countries):
            continue
        domestic = code == i["destination"]
        countries.append(dict(country=code, name=CO.get(code).name, screen_cost=None,
            price_basis="Domestic RFQ required" if domestic else "No comparable import screen available",
            duty_pct=0 if domestic else None, duty_basis="No import duty" if domestic else "Tariff verification required",
            duty_verified=False, surcharges=[], freight_usd_per_t=None,
            lpi=None, proven_tonnes=None, notes=[], historical=False))
    for c in countries:
        c["preferred"] = c["country"] == i["preferred_source_country"]
        c["domestic"] = c["country"] == i["destination"]
        c["lead_days"] = i["domestic_lead_days"] if c["domestic"] else i["import_lead_days"]
        c["can_preserve_reserve"] = safe_days is None or c["lead_days"] <= safe_days
        c["can_arrive_before_stockout"] = stockout_days is None or c["lead_days"] <= stockout_days
        c["researched_count"] = sum(s["country"] == c["country"] for s in catalog)
    # Continuity first; historical comparable import costs second. Domestic costs
    # have no equivalent quote and are never mixed numerically with historical CIF.
    countries.sort(key=lambda c: (not c["can_arrive_before_stockout"],
        not c["can_preserve_reserve"], c["screen_cost"] is None,
        c["screen_cost"] if c["screen_cost"] is not None else float("inf"),
        -(c["lpi"] or 0), not c["preferred"], c["name"]))
    selected = countries[:3]
    preferred = next((c for c in countries if c["preferred"]), None)
    if preferred and preferred not in selected:
        selected.append(preferred)
    for n, c in enumerate(selected):
        c["rank"] = n + 1 if n < 3 else None
    # Do not recommend an unresearched supplier, or let a lower historical
    # country price override an impossible delivery date.
    candidate = next((c for c in selected if c["researched_count"] and c["can_arrive_before_stockout"]), None)
    lead = candidate["lead_days"] if candidate else min(i["domestic_lead_days"], i["import_lead_days"])
    forecast = []
    if market:
        try:
            for dist in O.distributions(tuple(market["prices"])):
                days = dist["calendar_days"]
                p10, p50, p90 = np.quantile(market["benchmark"] * dist["ratios"], [.1,.5,.9])
                forecast.append(dict(days=days, date=(now.date()+dt.timedelta(days=days)).isoformat(),
                    p10=round(float(p10),2), p50=round(float(p50),2), p90=round(float(p90),2),
                    probability_cheaper=round(float(np.mean(dist["ratios"] < 1)),3),
                    change_pct=round(100*(float(p50)/market["benchmark"]-1),2),
                    feasible=safe_days is None or days + lead <= safe_days,
                    bridge_t=round(max(0, daily*(days+lead)+i["safety_stock_t"]-stock),1),
                    calibration=dist["calibration"]))
        except ValueError as exc:
            errors.append(str(exc))
    eligible = [f for f in forecast if f["feasible"] and f["probability_cheaper"] >= .6 and f["change_pct"] < 0]
    horizon = 0
    quantity = max(0, daily*(lead+30*i["planning_months"])+i["safety_stock_t"]-stock) if daily else 0
    if not daily:
        action, reason, quantity = "No consumption requirement", "No demand entered. Review inventory policy before ordering more material.", 0
    elif safe_days < lead:
        action = "Secure near-term supply now"
        reason = "Inventory cannot preserve safety stock until the assumed delivery date. Obtain dated availability now; a lower future price cannot resolve this gap."
    elif market and market["fresh"] and eligible:
        choice = min(eligible, key=lambda f: f["p50"])
        horizon = choice["days"]
        action, reason, quantity = f"Review buying in {horizon // 30} month(s)", "A lower-price scenario clears the 60% model-probability rule and stock covers both the wait and assumed delivery time. This is conditional, not a purchase commitment.", 0
    elif not market or not market["fresh"]:
        action, reason = "Refresh prices; request quotes now", "Current price/history or required FX is missing or stale. Inventory planning remains available, but no price-timing claim is decision-ready."
    else:
        action, reason = "Request current quotes now", "No stock-feasible future scenario clears the lower-price rule. Secure current offers without speculatively increasing volume."
    order_later = max(0, daily*(horizon+lead+30*i["planning_months"])+i["safety_stock_t"]-stock) if daily else 0
    rfq_quantity = quantity or order_later or i["monthly_demand_t"]
    tiers = sorted(set(round(rfq_quantity*m,1) for m in (1,1.5,2) if rfq_quantity > 0))
    commercial_path = Path(__file__).with_name("spain_commercial_evidence.json")
    commercial = json.loads(commercial_path.read_text(encoding="utf-8")).get("suppliers", {}) if commercial_path.exists() else {}
    for fields in commercial.values():
        for record in fields.values():
            record["automatic_check"] = data.get("refresh_status", {}).get("publications", {}).get(record.get("url"))
    supplier_groups = []
    for country in sorted(CO._C, key=lambda c:c.name):
        suppliers = sorted((s for s in catalog if s["country"] == country.iso3), key=supplier_catalogue.priority)
        rows = [dict(s, rank=n+1, quote_per_t=None, delivery_days=None,
            quantity_discount=None, discount_tiers_t=tiers, reviewed_at=s.get("reviewed_at",data["reviewed_at"]),
            negotiation="Request delivered unit price, origin certificate, dated availability, quality certificate and price breaks at each volume tier. No discount applied.") for n,s in enumerate(suppliers)]
        for supplier in rows:
            supplier["commercial_evidence"] = commercial.get(supplier["id"], {})
            supplier["contact_check"] = data.get("refresh_status", {}).get("publications", {}).get(supplier.get("contact_url"))
        supplier_groups.append(dict(country=country.iso3, name=country.name, suppliers=rows,
            supplier_gap=max(0,3-len(rows)), lead_days=i["domestic_lead_days"] if country.iso3==i["destination"] else i["import_lead_days"],
            coverage_notes=[r.get("note", "") for r in data.get("coverage", []) if r["country"]==country.iso3 and r["material"]==i["material"]]))
    groups = {g["country"]:g for g in supplier_groups}
    for c in selected:
        c["suppliers"] = groups[c["country"]]["suppliers"][:3]
        c["supplier_gap"] = max(0,3-len(c["suppliers"]))
        c["future_screen"] = [{"days":f["days"], "median":round(c["screen_cost"]*f["p50"]/market["benchmark"],2)} for f in forecast] if c["screen_cost"] is not None and market else []
    if not candidate:
        errors.append("No researched country candidate has an assumed arrival before stockout. Expedite existing local supply; no supplier award can be recommended.")
    if market and not market["fresh"]:
        errors.append("Market data is degraded. Forecast ranges are scenarios only until source freshness is restored.")
    if i["destination"] == "IND" and (not market or not market.get("fx_observation") or not fresh(market["fx_observation"]["observed_at"])):
        errors.append("INR import screens require a fresh FX quote; the available FX observation is stale or missing.")
    result = dict(generated_at=now.isoformat(timespec="seconds"), inputs=i, currency=currency,
        mode="DEMO INVENTORY" if i["demo_stock_breach"] else "MANUAL SAMPLE INPUTS",
        inventory=dict(on_hand_t=round(stock,2), entered_stock_t=i["on_hand_t"], safety_stock_t=i["safety_stock_t"],
            trigger_t=round(threshold,2), crossed=stock <= threshold, safe_days=safe_days, stockout_days=stockout_days,
            lead_days=lead, urgent_bridge_t=round(max(0,daily*lead-stock),1)),
        recommendation=dict(action=action, reason=reason, buy_after_days=horizon,
            quantity_now_t=round(quantity,1), quantity_later_t=round(order_later,1) if horizon else 0,
            country=candidate["name"] if candidate else None,
            supplier=candidate["suppliers"][0]["name"] if candidate and candidate.get("suppliers") else None,
            supplier_basis="RFQ contact priority: published sales channel, product-fit evidence, then alphabetical ties. Not a price ranking or confirmation of order acceptance."),
        market={k:v for k,v in market.items() if k != "prices"} if market else None,
        forecast=forecast, countries=selected, errors=errors,
        worldwide_rates=getattr(ranked, "worldwide_rates", []), supplier_catalogue=supplier_groups,
        coverage=dict(selectable_countries=len(CO._C), countries_with_candidates=sum(bool(g["suppliers"]) for g in supplier_groups),
                      supplier_count=len(catalog), public_contact_count=sum(bool(s.get("contact_url")) for s in catalog),
                      limitation="Worldwide trade view contains reported partner countries/areas, including those outside the tariff registry. API coverage can be incomplete; unreported rates and unresearched suppliers remain unknown."),
        supplier_refresh={k:v for k,v in data.get("refresh_status", {}).items() if k != "publications"},
        assumptions=["Entered stock and demand are sample planning inputs, not a live ERP connection. Undated pipeline is excluded.",
            "Trigger = safety stock × (1 − selected percentage / 100); crossing triggers advice, never an order.",
            "Quantity = demand through lead time and the selected replenishment cycle + safety stock − usable stock. Round to vendor lot size after quoting.",
            "Domestic/import lead times are buyer-entered planning assumptions, not verified supplier commitments. Waiting includes delivery time.",
            "8,000 five-observation block-bootstrap scenarios; 21 trading observations per month. Probabilities are model outputs, not validated odds of success.",
            "Country order: arrival feasibility, reserve preservation, comparable historical import cost, then logistics score. Preferred country is included even outside the top three.",
            "Historical HS4 CIF trade averages include international freight; it is not added twice. Screens exclude local delivery, financing and recoverable taxes. Verify HS code, origin-specific duty, safeguards and domestic GST before purchase.",
            "Future country screens scale historical cost by benchmark scenarios with unchanged FX and duties; these are not live supplier forecasts.",
            "Secondary research identifies RFQ candidates, not approved vendors. Fewer than three evidenced suppliers is an explicit research gap; company location does not prove shipment origin."])
    import prediction_sources
    return prediction_sources.attach(result)

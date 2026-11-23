"""Trace advisory outputs to input publications and to our own calculations.

Links identify upstream data, never third-party endorsement of our forecasts.
Missing source dates stay missing. Manual assumptions get no invented citation.
"""
import re
import datetime as dt
import countries as CO

ZINC_URL = "https://www.westmetall.com/en/markdaten.php?action=table&field=LME_Zn_cash"
HRC_URL = "https://www.tatanexarc.com/steel/steel-prices-in-india/"
HISTORY_URL = "https://query1.finance.yahoo.com/v8/finance/chart/HRC=F?range=2y&interval=1d"
FX_URL = "https://api.frankfurter.dev/v2/rate/USD/INR?providers=FBIL"


def attach(result):
    i, m = result["inputs"], result["market"]
    sources = []
    def add(id, title, kind, description, url=None, observed=None, retrieved=None):
        sources.append(dict(id=id, title=title, kind=kind, description=description,
                            url=url, observed_at=observed, retrieved_at=retrieved))
        return id
    add("model", "Our forecast model · block bootstrap v1", "MODEL, NOT EXTERNAL PREDICTION",
        "We resample five-observation blocks of historical log returns into 8,000 paths. "
        "One/two/three months use 21/42/63 trading observations. P10/P50/P90 are path quantiles; "
        "probability cheaper is the fraction ending below today's reference. Fixed seeds make identical inputs reproducible. "
        "The nominal 80% band and 60% timing rule are not independently validated success probabilities. "
        f"Analysis generated at {result['generated_at']}.")
    add("inventory", "Your entered inventory and procurement policy", "USER INPUT / CALCULATION",
        "On-hand stock, monthly consumption, safety stock, trigger percentage, replenishment cycle and lead times "
        "come from the form. Demo overrides stock only. Daily consumption = monthly / 30; safe days = "
        "max(0, stock − safety) / daily consumption. Waiting is feasible only if wait + lead time fits safe days. "
        "Quantity = max(0, daily demand × (lead time + replenishment days) + safety stock − stock). "
        "No external source confirms the entered inventory or delivery assumptions.")
    add("negotiation", "RFQ and negotiation assumptions", "BUYER REQUESTS, NOT SUPPLIER OFFERS",
        "Volume tiers are 1×, 1.5× and 2× the RFQ requirement. Credit, staggered delivery and discounts "
        "are proposed negotiation requests. No current vendor discount, availability or delivery commitment is verified.")
    forecast_ids = ["model"]
    if m:
        zinc = i["material"] == "ZINC_SHG"
        obs = m["observation"]
        benchmark_url = ZINC_URL if zinc else HRC_URL if i["destination"] == "IND" else HISTORY_URL
        portal_proxy = not zinc and i["destination"] == "IND"
        add("benchmark", obs.get("source", "Material benchmark"), "OBSERVED INPUT, NOT A FORECAST",
            ("Indicative HRC portal median. The connector stamps the fetching date; it does not establish "
             "a supplier price-update date. Monthly portal labels are not intraday quotes." if portal_proxy else
             "Public settlement/futures reference. Physical premium and delivery are not supplier quotations."),
            benchmark_url, None if portal_proxy else obs.get("observed_at"), obs.get("retrieved_at"))
        hist = m.get("history_observation", {})
        add("history", "LME zinc cash history via Westmetall" if zinc else "US CME HRC futures history via Yahoo Finance",
            "MODEL TRAINING INPUT",
            f"{m.get('history_count', 'Unknown count of')} observations. "
            f"Window: {m.get('history_start', 'not recorded')} to {m.get('history_end', 'not recorded')}. "
            + ("Zinc returns drive the zinc scenarios." if zinc else
               "US steel returns drive these scenarios; applying them to Indian HRC is a cross-region proxy, not a locally validated forecast."),
            ZINC_URL if zinc else HISTORY_URL, m.get("history_observed_at"), hist.get("retrieved_at"))
        forecast_ids += ["benchmark", "history"]
        if m.get("fx_observation"):
            fx = m["fx_observation"]
            add("fx", fx.get("source", "USD/INR reference"), "CURRENCY-CONVERSION INPUT",
                "Mirror of the FBIL reference. Fetching again does not make an old observation current. "
                "FX is held constant in future scenarios; this is not an exchange-rate forecast.",
                FX_URL, fx.get("observed_at"), fx.get("retrieved_at"))
            if i["material"] == "ZINC_SHG" and result["currency"] == "INR":
                forecast_ids.append("fx")
    for f in result["forecast"]:
        f["source_ids"] = forecast_ids + ["inventory"]
        f["model_steps"] = {30:21, 60:42, 90:63}[f["days"]]
    result["recommendation"]["source_ids"] = forecast_ids + ["inventory"]
    dest = CO.get(i["destination"])
    hs6 = "790111" if i["material"] == "ZINC_SHG" else "720810"
    for c in result["countries"]:
        origin = CO.get(c["country"])
        ids = ["inventory"]
        yr = re.search(r"Comtrade (\d{4})", c["price_basis"])
        if yr:
            url = ("https://comtradeapi.un.org/public/v1/preview/C/A/HS"
                   f"?reporterCode={dest.comtrade}&period={yr[1]}&cmdCode={hs6[:4]}&flowCode=M")
            ids.append(add("trade-"+c["country"], f"UN Comtrade · {c['name']} · {yr[1]}", "HISTORICAL COUNTRY INPUT",
                "Country CIF value divided by reported weight; includes international freight. "
                "HS4 is broader than the requested product specification. Not a current vendor offer. "
                "Future screen = historical duty-adjusted cost × forecast median / current benchmark, holding FX/duty constant. "
                + ("Current-year annual-series observation: incomplete-year / provisional coverage, not a complete annual average."
                   if int(yr[1]) == dt.datetime.now(dt.timezone.utc).year else ""),
                url, yr[1]))
        if not c["domestic"]:
            # Reference endpoints for checking the tariff records, not a claim
            # that every resolved surcharge or override came from WITS.
            ids.append(add("duty-"+c["country"], f"Tariff evidence · {c['name']}", "TARIFF SCREEN / VERIFY BEFORE PURCHASE",
                c["duty_basis"] + ". " + " ".join(c["notes"]) +
                " This links the bilateral WITS record for checking; MFN overrides and additional trade measures "
                "are separate. The resolver does not record a complete legal-source URL trail for every surcharge.",
                "https://wits.worldbank.org/API/V1/SDMX/V21/rest/data/DF_WITS_Tariff_TRAINS/"
                f"A.{dest.iso_num}.{origin.iso_num:03d}.{hs6}.Reported/?startPeriod=2015&endPeriod=2026"))
            ids.append(add("mfn-"+c["country"], f"MFN tariff record · imports from {c['name']}", "TARIFF REFERENCE",
                "MFN record used as the baseline before preferential rates and local overrides. This is a data "
                "reference, not legal confirmation of the full payable tax burden or current tariff eligibility.",
                "https://wits.worldbank.org/API/V1/SDMX/V21/rest/data/DF_WITS_Tariff_TRAINS/"
                f"A.{dest.iso_num}.000.{hs6}.Reported/?startPeriod=2015&endPeriod=2026"))
        if c["lpi"] is not None:
            ids.append(add("logistics-"+c["country"], f"World Bank logistics index · {c['name']}", "COUNTRY LOGISTICS PROXY",
                "Latest non-empty LPI observation requested by the connector. Publication year/retrieval timestamp "
                "are not retained by the current connector. LPI is not a supplier's delivery record.",
                f"https://api.worldbank.org/v2/country/{origin.iso3}/indicator/LP.LPI.OVRL.XQ?format=json&mrnev=1"))
        if c["screen_cost"] is not None and m and m.get("fx_observation") and result["currency"] == "INR":
            ids.append("fx")
        c["source_ids"] = ids
        for future in c["future_screen"]:
            future["source_ids"] = list(dict.fromkeys(ids + forecast_ids + (["fx"] if m and m.get("fx_observation") and result["currency"] == "INR" else [])))
        for supplier in c["suppliers"]:
            check = supplier.get("automatic_check") or {}
            sid = add("supplier-"+supplier["id"], supplier["name"], "SUPPLIER PRODUCT EVIDENCE, NOT A QUOTE",
                supplier["evidence"] + f" Catalogue review: {supplier['reviewed_at']}; publication date not established. "
                "RFQ priority uses published sales channels and product fit, with alphabetical ties; not a ranking of current prices or order acceptance. "
                + (f"Automatic check: {check.get('checked_at')}; status: {check.get('status')}. "
                   f"{check.get('note', '')} Last content change: {check.get('last_change_at') or 'not established'}. " if check else
                   "No automatic publication check recorded. "), supplier["url"], retrieved=check.get("last_success_at"))
            supplier["source_ids"] = [sid, "negotiation"]
            if supplier.get("contact_url"):
                contact_id=add("contact-"+supplier["id"], supplier["name"]+" · sales channel", "PUBLIC ENQUIRY CHANNEL, NOT ORDER ACCEPTANCE",
                    supplier.get("contact_evidence", "Published contact route")+" No enquiry was sent. Stock, export eligibility, price and acceptance of this order remain unconfirmed.",
                    supplier["contact_url"], retrieved=(supplier.get("contact_check") or {}).get("last_success_at"))
                supplier["source_ids"].append(contact_id)
        result["recommendation"]["source_ids"].extend(ids)
        if c["name"] == result["recommendation"].get("country"):
            result["recommendation"]["source_ids"].extend(sid for s in c["suppliers"] for sid in s["source_ids"])
    result["recommendation"]["source_ids"] = list(dict.fromkeys(result["recommendation"]["source_ids"]))
    result["sources"] = sources
    return result

"""
export_ui_data.py — Dump everything the cockpit UI needs into one JSON.

The UI is a published web page and cannot reach LME, WITS or Comtrade itself
(the artifact sandbox blocks arbitrary hosts). So the page ships with real data
captured here, stamped with when it was captured. That stamp is not decoration:
a procurement screen that cannot tell you how old its prices are is dangerous.

    python export_ui_data.py            # India
    python export_ui_data.py IDN        # any destination

Writes ui_data.json next to this file.
"""

from __future__ import annotations

import datetime as dt
import json
import sys

import connectors as C
import countries as CO
import country_rank as CR
import destinations as DEST
import engine as E
import erp as ERP
import negotiation as NEG
import tariff as T

MATERIAL = "HRC-E250A-2.0"
PLANT = "PLANT-PUNE-1"
HS6 = "720810"
ANNUAL_T = 7_200


def build(destination="IND"):
    dest = CO.get(destination)
    prof = DEST.profile(dest.iso3)
    out = {
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "destination": {"iso3": dest.iso3, "name": dest.name,
                        "currency": prof.currency, "tier": prof.tier,
                        "fx_source": prof.fx_source,
                        "domestic_benchmark": prof.domestic_benchmark,
                        "missing": prof.missing},
        "material": MATERIAL, "plant": PLANT, "hs6": HS6,
    }

    # --- market -------------------------------------------------------
    zc, z3, zs, zhist = C.lme_zinc_westmetall()
    out["zinc"] = {
        "cash": zc.value, "m3": z3.value, "stock": zs.value,
        "as_of": zc.observed_at[:10], "age_days": round(zc.age_days, 1),
        "source": zc.source,
        "history": [{"d": d, "cash": c, "m3": m, "stock": s}
                    for d, c, m, s in reversed(zhist)],
    }

    fx = C.fx_usd_inr_fbil() if dest.iso3 == "IND" else None
    if fx:
        out["fx"] = {"pair": "USD/INR", "value": fx.value,
                     "as_of": fx.observed_at[:10],
                     "age_days": round(fx.age_days, 1), "source": fx.source}

    if dest.iso3 == "IND":
        hrc, rows = C.india_hrc_nexarc()
        out["hrc"] = {"median": hrc.value, "n_rows": len(rows),
                      "source": hrc.source, "note": hrc.note,
                      "rows": rows[:14]}
    else:
        out["hrc"] = None

    zcx, _ = C.zinc_comex_futures("1mo")
    out["stale_control"] = {
        "label": "COMEX zinc (ZNC=F)", "value": zcx.value,
        "age_days": round(zcx.age_days, 1),
        "error_pct": round(abs(zcx.value - zc.value) / zc.value * 100, 2),
    }

    # --- forecast -----------------------------------------------------
    series = [r[1] for r in reversed(zhist)]
    fc = E.forecast(series, 30, series="ZINC.LME.CASH")
    out["forecast"] = fc.to_dict()

    # --- ERP ----------------------------------------------------------
    src = ERP.MockERP()
    cov = ERP.coverage(src, MATERIAL, PLANT)
    out["coverage"] = {k: v for k, v in cov.__dict__.items()}
    out["suppliers"] = [s.__dict__ for s in src.suppliers(MATERIAL)]
    out["contract"] = src.contracts(MATERIAL)[0].__dict__

    # --- sourcing -----------------------------------------------------
    ranked = CR.rank_source_countries(dest.iso3, HS6, 2024, top_n=8)
    out["sourcing"] = [{
        "origin": r.origin, "name": r.origin_name, "tonnes": r.proven_tonnes,
        "fob": r.fob_usd_per_t, "duty": r.duty_pct, "freight": r.freight_usd_per_t,
        "landed": r.landed_usd_per_t, "lpi": r.lpi_score, "basis": r.duty_basis,
        "decision_grade": r.duty_decision_grade,
        "surcharges": [{"kind": s["kind"], "pct": s["pct"], "to": s.get("to")}
                       for s in r.surcharges],
    } for r in ranked]

    out["duties"] = []
    for o in ["JPN", "KOR", "CHN", "VNM"]:
        total, tres, sc = T.total_duty_pct(dest.iso3, o, HS6)
        out["duties"].append({
            "origin": o, "rate": tres.ad_valorem_pct, "total": total,
            "source": tres.source, "year": tres.as_of_year,
            "stale": tres.staleness_years, "decision_grade": tres.usable_for_decision,
            "notes": tres.notes,
            "surcharges": [{"kind": s["kind"], "pct": s["pct"]} for s in sc],
        })

    # --- recommendation + negotiation --------------------------------
    if out["hrc"]:
        bench = out["hrc"]["median"]
        rec = E.recommend(fc, max(cov.net_requirement_t, 200.0), bench,
                          cov.coverage_weeks, 4.0)
        rec.pop("forecast", None)
        out["recommendation"] = rec

        sup = next(s for s in src.suppliers(MATERIAL) if s.supplier_id == "SUP-DOM-01")
        batna = None
        if ranked and ranked[0].landed_usd_per_t and fx:
            batna = (f"{ranked[0].origin_name} (import)",
                     ranked[0].landed_usd_per_t * fx.value)
        b = NEG.build_brief(sup, src.contracts(MATERIAL)[0], cov,
                            bench + 1150, bench, batna, ANNUAL_T,
                            fc.ann_vol_pct,
                            supplier_annual_capacity_t=sup.annual_capacity_t)
        out["negotiation"] = {
            "supplier": b.supplier_name, "spend": b.annual_spend_inr,
            "price": b.current_price_inr_per_t,
            "benchmark": b.benchmark_price_inr_per_t,
            "premium": b.premium_vs_benchmark_inr_per_t,
            "premium_pct": b.premium_vs_benchmark_pct,
            "batna": b.batna_supplier, "batna_cost": b.batna_landed_inr_per_t,
            "batna_credible": b.batna_credible,
            "share_of_wallet": b.share_of_wallet_pct,
            "levers": [{"name": l.name, "value": l.annual_value_inr,
                        "basis": l.basis, "certainty": l.certainty,
                        "their_cost": l.likely_cost_to_supplier, "ask": l.ask}
                       for l in b.levers],
            "points": b.talking_points, "warnings": b.warnings,
        }
    else:
        out["recommendation"] = None
        out["negotiation"] = None

    # --- cross-exchange basis (every input fetched; may withhold a verdict)
    try:
        out["basis"] = C.cross_exchange_basis_audit("ZINC", dest.iso3)
    except Exception as e:
        out["basis"] = {"available": False, "reason": f"{type(e).__name__}: {e}"}

    # --- provenance ---------------------------------------------------
    out["provenance"] = [
        {"item": "LME zinc cash / 3M / stock", "status": "live", "cost": "free",
         "source": "westmetall.com (republished LME settlement)"},
        {"item": "India HR Coil by brand/city/grade", "status": "live", "cost": "free",
         "source": "Tata Nexarc"},
        {"item": "USD/INR reference rate", "status": "live", "cost": "free",
         "source": "FBIL via frankfurter.dev"},
        {"item": "Import flows by partner country", "status": "live", "cost": "free",
         "source": "UN Comtrade (preview endpoint, no key)"},
        {"item": "MFN + FTA preferential tariffs", "status": "live", "cost": "free",
         "source": "WITS / UNCTAD TRAINS"},
        {"item": "Logistics performance", "status": "live", "cost": "free",
         "source": "World Bank LPI"},
        {"item": "Safeguard / AIDC surcharges", "status": "manual", "cost": "human",
         "source": "DGTR / CBIC — hand-maintained, India only"},
        {"item": "Ocean freight", "status": "estimate", "cost": "n/a",
         "source": "region-pair table — no free API prices arbitrary lanes"},
        {"item": "Intraday ticks", "status": "simulated", "cost": "Rs 0-500/mo live",
         "source": "Angel One SmartAPI / Kite — needs a broker account"},
        {"item": "Inventory, demand, open POs, suppliers", "status": "mock",
         "cost": "ERP integration", "source": "MockERP — synthetic"},
    ]
    return out


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else "IND"
    data = build(d)
    with open("ui_data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, default=str)
    print(f"ui_data.json written for {d}")
    print(f"  zinc history points : {len(data['zinc']['history'])}")
    print(f"  sourcing options    : {len(data['sourcing'])}")
    print(f"  HRC rows            : {data['hrc']['n_rows'] if data['hrc'] else 0}")
    print(f"  captured_at         : {data['captured_at']}")

"""
procurement_mcp.py — Standard Model Context Protocol (MCP) Tool Suite.

Architectural Rule AD-1 Enforced:
All computational operations, duty calculations, landed costs, inventory nettings,
and lever valuations are executed by the deterministic engines.
The MCP tools return structured JSON envelopes that the LLM agent reasons over.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

import connectors as C
import countries as CO
import destinations as DEST
import country_rank as CR
import engine as E
import erp as ERP
import negotiation as NEG
import tariff as T
from market_stream import MarketStreamManager
from erp_watcher import FileBackedERP

logger = logging.getLogger("procurement_mcp")

# Shared singletons for tool runtime
_market_mgr: Optional[MarketStreamManager] = None
_erp_client: Optional[FileBackedERP] = None


def get_market_manager() -> MarketStreamManager:
    global _market_mgr
    if _market_mgr is None:
        _market_mgr = MarketStreamManager(use_simulation=False)
        _market_mgr.refresh_public_feeds()
    return _market_mgr


def get_erp_client() -> FileBackedERP:
    global _erp_client
    if _erp_client is None:
        _erp_client = FileBackedERP()
    return _erp_client


# =====================================================================
# MCP Tool Definitions & Handlers
# =====================================================================

def tool_get_live_market_status() -> Dict[str, Any]:
    """
    Get live and reference commodity market quotes with data freshness and staleness audit.
    Returns quotes for MCX Zinc, LME Zinc, CME HRC, India domestic HRC, and USD/INR FX.
    """
    mgr = get_market_manager()
    snap = mgr.get_snapshot()
    alerts = mgr.get_recent_alerts(limit=5)

    # Check for any degraded feeds
    stale_feeds = [k for k, v in snap.items() if v.get("is_stale")]

    return {
        "status": "success",
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "has_stale_feeds": len(stale_feeds) > 0,
        "stale_feeds": stale_feeds,
        "quotes": snap,
        "recent_alerts": alerts,
    }


def tool_check_inventory_coverage(material_id: str = "HRC-E250A-2.0", plant: str = "PLANT-PUNE-1") -> Dict[str, Any]:
    """
    Check enterprise inventory coverage position.
    Strictly excludes blocked and consignment inventory; nets confirmed open POs.
    """
    erp = get_erp_client()
    cov = ERP.coverage(erp, material_id, plant)
    inv = erp.inventory(material_id, plant)

    return {
        "status": "success",
        "material_id": material_id,
        "plant": plant,
        # effective_origin reflects what was ACTUALLY served per object.
        # source_origin only says "a file exists", which reported FILE_INBOX
        # while half the objects were mock.
        "data_source": getattr(erp, "effective_origin", None) or erp.source_origin,
        "data_source_per_object": getattr(erp, "provenance", {}),
        "usable_stock_t": cov.usable_stock_t,
        "in_transit_t": cov.in_transit_t,
        "open_po_t": cov.open_po_t,
        "safety_stock_t": cov.safety_stock_t,
        "blocked_stock_t": inv.blocked_t if inv else 0.0,
        "consignment_stock_t": inv.consignment_t if inv else 0.0,
        "demand_next_30d_t": cov.demand_next_30d_t,
        "firm_demand_t": cov.firm_demand_t,
        "weekly_burn_t": cov.weekly_burn_t,
        "coverage_weeks": round(cov.coverage_weeks, 2),
        "net_requirement_t": round(cov.net_requirement_t, 1),
        "warnings": cov.warnings,
    }


def tool_calculate_landed_cost(
    destination_iso3: str,
    origin_iso3: str,
    hs6: str,
    fob_usd_per_t: float,
    freight_usd_per_t: Optional[float] = None,
    include_igst: bool = False,
) -> Dict[str, Any]:
    """
    Calculate deterministic landed cost into a destination country from an origin.
    Applies official MFN or preferential FTA customs duty, surcharges (AIDC, SWS, ADD),
    freight estimates, and currency conversion.
    """
    dest = CO.get(destination_iso3)
    orig = CO.get(origin_iso3)
    if not dest or not orig:
        return {"status": "error", "message": f"Invalid country codes: {destination_iso3}, {origin_iso3}"}

    prof = DEST.profile(dest.iso3)
    fx_rate = 1.0
    fx_currency = "USD"

    if dest.iso3 == "IND":
        fx_obs = C.fx_usd_inr_fbil()
        fx_rate = fx_obs.value
        fx_currency = "INR"
    elif prof.can_price_in_local_currency:
        fx_obs = C.fx_usd_to(prof.currency)
        fx_rate = fx_obs.value
        fx_currency = prof.currency

    # Freight estimate if not provided
    if freight_usd_per_t is None:
        est_freight, _ = CR.freight_estimate(orig.iso3, dest.iso3)
        freight_usd_per_t = est_freight

    # Duty calculation
    tot_duty_pct, tres, surcharges = T.total_duty_pct(dest.iso3, orig.iso3, hs6)
    duty_pct = tot_duty_pct if tot_duty_pct is not None else 7.5

    # Landed cost arithmetic
    fob_local = fob_usd_per_t * fx_rate
    freight_local = freight_usd_per_t * fx_rate
    cif_local = fob_local + freight_local
    duty_amount_local = cif_local * (duty_pct / 100.0)
    landed_local = cif_local + duty_amount_local
    landed_usd = landed_local / fx_rate if fx_rate > 0 else 0.0

    return {
        "status": "success",
        "destination": dest.iso3,
        "origin": orig.iso3,
        "hs6": hs6,
        "fob_usd_per_t": fob_usd_per_t,
        "freight_usd_per_t": freight_usd_per_t,
        "cif_usd_per_t": round(fob_usd_per_t + freight_usd_per_t, 2),
        "duty_pct": round(duty_pct, 2),
        "tariff_type": tres.trust if tres else "UNKNOWN",
        "tariff_basis": tres.source if tres else "ESTIMATE",
        "surcharges": surcharges,
        "fx_currency": fx_currency,
        "fx_rate": fx_rate,
        "landed_cost_local_per_t": round(landed_local, 2),
        "landed_cost_usd_per_t": round(landed_usd, 2),
    }


def tool_rank_sourcing_countries(destination_iso3: str = "IND", hs6: str = "720810", year: Optional[int] = None, top_n: int = 5) -> Dict[str, Any]:
    """
    Rank source countries by landed cost, proven trade volume from UN Comtrade,
    and World Bank Logistics Performance Index (LPI). Search from the current
    year when omitted; report the actual returned trade year, not the request.
    """
    dest = CO.get(destination_iso3)
    if not dest:
        return {"status": "error", "message": f"Unknown destination: {destination_iso3}"}

    try:
        requested_year = year if year is not None else dt.datetime.now(dt.timezone.utc).year
        ranked = CR.rank_source_countries(dest.iso3, hs6, requested_year, top_n=top_n)
        results = []
        # Compute cheapest for each origin
        valid_costs = [r.landed_usd_per_t for r in ranked if r.landed_usd_per_t is not None]
        min_cost = min(valid_costs) if valid_costs else None
        for r in ranked:
            period = re.search(r"Comtrade (\d{4})\b", r.fob_source)
            results.append({
                "origin_iso3": r.origin,           # SourceOption uses .origin (not .origin_iso3)
                "origin_name": r.origin_name,
                "proven_tonnes": round(r.proven_tonnes, 0),
                "fob_usd_per_t": round(r.fob_usd_per_t, 1) if r.fob_usd_per_t is not None else None,
                "price_basis": r.fob_source,
                "data_year": int(period.group(1)) if period else None,
                "duty_pct": round(r.duty_pct, 1) if r.duty_pct is not None else None,
                "tariff_type": r.duty_basis,
                "freight_est_usd_per_t": round(r.freight_usd_per_t, 1),
                "landed_usd_per_t": round(r.landed_usd_per_t, 1) if r.landed_usd_per_t is not None else None,
                "lpi_score": r.lpi_score,
                "is_cheapest": (r.landed_usd_per_t == min_cost) if min_cost is not None else False,
            })
        return {
            "status": "success",
            "destination": dest.iso3,
            "hs6": hs6,
            "requested_year": requested_year,
            "year": results[0]["data_year"] if results else None,
            "ranking": results,
        }
    except Exception as e:
        logger.error(f"Error ranking source countries: {e}")
        return {"status": "error", "message": str(e)}


def tool_run_what_if_scenario(
    destination_iso3: str = "IND",
    origin_iso3: str = "JPN",
    hs6: str = "720810",
    base_fob_usd: float = 640.0,
    fx_shift_pct: float = 0.0,
    freight_usd_shift: float = 0.0,
    tariff_pct_override: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Perform a sensitivity simulation: evaluate how FX depreciation, freight spikes,
    or tariff adjustments change landed cost compared to a domestic benchmark.
    """
    dest = CO.get(destination_iso3)
    orig = CO.get(origin_iso3)
    if not dest or not orig:
        return {"status": "error", "message": "Invalid country code"}

    base_res = tool_calculate_landed_cost(dest.iso3, orig.iso3, hs6, base_fob_usd)
    base_fx = base_res.get("fx_rate", 1.0)
    base_freight = base_res.get("freight_usd_per_t", 35.0)
    base_duty = base_res.get("duty_pct", 7.5)

    sim_fx = base_fx * (1.0 + fx_shift_pct / 100.0)
    sim_freight = max(0.0, base_freight + freight_usd_shift)
    sim_duty = tariff_pct_override if tariff_pct_override is not None else base_duty

    # Recompute landed cost with perturbed variables
    cif_local = (base_fob_usd + sim_freight) * sim_fx
    duty_local = cif_local * (sim_duty / 100.0)
    sim_landed_local = cif_local + duty_local

    variance_pct = round((sim_landed_local - base_res["landed_cost_local_per_t"]) / base_res["landed_cost_local_per_t"] * 100, 2)

    return {
        "status": "success",
        "scenario": {
            "fx_shift_pct": fx_shift_pct,
            "sim_fx_rate": round(sim_fx, 4),
            "freight_usd_shift": freight_usd_shift,
            "sim_freight_usd": round(sim_freight, 2),
            "tariff_pct_override": sim_duty,
        },
        "base_landed_cost_local": base_res["landed_cost_local_per_t"],
        "simulated_landed_cost_local": round(sim_landed_local, 2),
        "cost_variance_pct": variance_pct,
        "currency": base_res.get("fx_currency", "INR"),
    }


def tool_generate_negotiation_dossier(
    material_id: str = "HRC-E250A-2.0",
    plant: str = "PLANT-PUNE-1",
    supplier_id: str = "SUP-DOM-01",
) -> Dict[str, Any]:
    """
    Generate an executive negotiation brief against a supplier.
    Computes benchmark premium/discount, prices negotiation levers in rupees,
    evaluates BATNA viability, and produces an ordered concession ladder.
    """
    erp = get_erp_client()
    cov = ERP.coverage(erp, material_id, plant)
    sups = erp.suppliers(material_id)
    sup = next((s for s in sups if s.supplier_id == supplier_id), None)
    if not sup:
        return {"status": "error", "message": f"Supplier {supplier_id} not found for material {material_id}"}

    ctrs = erp.contracts(material_id)
    ctr = ctrs[0] if ctrs else None
    if not ctr:
        return {"status": "error", "message": f"No contract found for material {material_id}"}

    # Fetch domestic benchmark (Tata Nexarc or fallback)
    hrc_obs, _ = C.india_hrc_nexarc()
    benchmark_inr = hrc_obs.value if hrc_obs else 59500.0
    current_price_inr = benchmark_inr + ctr.premium_per_t

    # BATNA from ranked import countries
    batna = None
    try:
        ranked = CR.rank_source_countries("IND", "720810", top_n=2)
        if ranked and ranked[0].landed_usd_per_t:
            fx = C.fx_usd_inr_fbil().value
            batna = (f"{ranked[0].origin_name} (import)", ranked[0].landed_usd_per_t * fx)
    except Exception:
        pass

    brief = NEG.build_brief(
        supplier=sup,
        contract=ctr,
        coverage_position=cov,
        current_price_inr_per_t=current_price_inr,
        benchmark_price_inr_per_t=benchmark_inr,
        batna=batna,
        annual_tonnes=7200.0,
        price_vol_pct=22.0,
        supplier_annual_capacity_t=sup.annual_capacity_t,
    )

    total_levers_val = sum(l.annual_value_inr for l in brief.levers if l.annual_value_inr is not None)

    return {
        "status": "success",
        "brief_text": NEG.render(brief),
        "metrics": {
            "supplier_name": sup.name,
            "current_price_inr_per_t": brief.current_price_inr_per_t,
            "benchmark_price_inr_per_t": brief.benchmark_price_inr_per_t,
            "variance_pct": round(brief.premium_vs_benchmark_pct or 0.0, 2),
            "coverage_weeks": round(cov.coverage_weeks, 2),
            "total_levers_value_inr": round(total_levers_val, 2),
            "batna_viable": brief.batna_credible,
            "batna_name": brief.batna_supplier or "None",
            "concession_order": [f"{l.name} (Cost: {l.likely_cost_to_supplier})" for l in brief.levers],
        }
    }


def tool_create_procurement_action_request(
    material_id: str,
    plant: str,
    recommended_tonnes: float,
    supplier_id: str,
    unit_price_inr: float,
    order_type: str = "SPOT_PURCHASE",
    justification: str = "",
) -> Dict[str, Any]:
    """
    Generate a formal Procurement Action Request (PAR) requiring human sign-off.
    """
    total_val = round(recommended_tonnes * unit_price_inr, 2)
    req_id = f"PAR-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}"

    return {
        "status": "success",
        "action_request_id": req_id,
        "material_id": material_id,
        "plant": plant,
        "supplier_id": supplier_id,
        "recommended_tonnes": recommended_tonnes,
        "unit_price_inr": unit_price_inr,
        "total_commitment_inr": total_val,
        "order_type": order_type,
        "justification": justification,
        "approval_state": "PENDING_HUMAN_APPROVAL",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "audit_trail": [
            f"Action request {req_id} generated by Procurement Agent",
            f"Pending review by Category Procurement Manager before ERP release",
        ]
    }


# Registry of all tool specifications for MCP and LLMs
TOOL_REGISTRY = {
    "get_live_market_status": {
        "function": tool_get_live_market_status,
        "description": "Fetch live and reference commodity quotes (MCX, LME, CME, domestic HRC, FX) with staleness audit.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "check_inventory_coverage": {
        "function": tool_check_inventory_coverage,
        "description": "Check enterprise stock, open POs, burn rate, and weeks-on-hand. Excludes blocked/consignment stock.",
        "parameters": {
            "type": "object",
            "properties": {
                "material_id": {"type": "string", "default": "HRC-E250A-2.0"},
                "plant": {"type": "string", "default": "PLANT-PUNE-1"},
            },
        },
    },
    "calculate_landed_cost": {
        "function": tool_calculate_landed_cost,
        "description": "Deterministic landed cost engine with official MFN/FTA duties, surcharges, freight, and FX.",
        "parameters": {
            "type": "object",
            "properties": {
                "destination_iso3": {"type": "string"},
                "origin_iso3": {"type": "string"},
                "hs6": {"type": "string"},
                "fob_usd_per_t": {"type": "number"},
                "freight_usd_per_t": {"type": "number"},
            },
            "required": ["destination_iso3", "origin_iso3", "hs6", "fob_usd_per_t"],
        },
    },
    "rank_sourcing_countries": {
        "function": tool_rank_sourcing_countries,
        "description": "Rank cheapest source countries for any destination using Comtrade flows, WITS duties, and LPI.",
        "parameters": {
            "type": "object",
            "properties": {
                "destination_iso3": {"type": "string", "default": "IND"},
                "hs6": {"type": "string", "default": "720810"},
                "top_n": {"type": "integer", "default": 5},
            },
        },
    },
    "run_what_if_scenario": {
        "function": tool_run_what_if_scenario,
        "description": "Run sensitivity analysis on FX shocks, freight jumps, or tariff changes.",
        "parameters": {
            "type": "object",
            "properties": {
                "destination_iso3": {"type": "string", "default": "IND"},
                "origin_iso3": {"type": "string", "default": "JPN"},
                "hs6": {"type": "string", "default": "720810"},
                "base_fob_usd": {"type": "number", "default": 640.0},
                "fx_shift_pct": {"type": "number", "default": 0.0},
                "freight_usd_shift": {"type": "number", "default": 0.0},
                "tariff_pct_override": {"type": "number"},
            },
        },
    },
    "generate_negotiation_dossier": {
        "function": tool_generate_negotiation_dossier,
        "description": "Build an executive negotiation brief with market benchmark, BATNA, rupee levers, and concession order.",
        "parameters": {
            "type": "object",
            "properties": {
                "material_id": {"type": "string", "default": "HRC-E250A-2.0"},
                "plant": {"type": "string", "default": "PLANT-PUNE-1"},
                "supplier_id": {"type": "string", "default": "SUP-DOM-01"},
            },
        },
    },
    "create_procurement_action_request": {
        "function": tool_create_procurement_action_request,
        "description": "Generate a formal purchase commitment request requiring human approval before ERP dispatch.",
        "parameters": {
            "type": "object",
            "properties": {
                "material_id": {"type": "string"},
                "plant": {"type": "string"},
                "recommended_tonnes": {"type": "number"},
                "supplier_id": {"type": "string"},
                "unit_price_inr": {"type": "number"},
                "order_type": {"type": "string", "default": "SPOT_PURCHASE"},
                "justification": {"type": "string"},
            },
            "required": ["material_id", "plant", "recommended_tonnes", "supplier_id", "unit_price_inr"],
        },
    },
}


def dispatch_tool(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a registered tool by name with arguments."""
    if tool_name not in TOOL_REGISTRY:
        return {"status": "error", "message": f"Unknown tool: {tool_name}"}
    fn = TOOL_REGISTRY[tool_name]["function"]
    try:
        return fn(**args)
    except Exception as e:
        logger.error(f"Error executing {tool_name}: {e}")
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    print("Testing Procurement MCP Tool Suite...")

    # Test 1: Market status
    m_res = dispatch_tool("get_live_market_status", {})
    print(f"Market status: {m_res['status']}, quotes available: {len(m_res['quotes'])}")
    assert m_res["status"] == "success"

    # Test 2: Inventory coverage
    c_res = dispatch_tool("check_inventory_coverage", {"material_id": "HRC-E250A-2.0", "plant": "PLANT-PUNE-1"})
    print(f"Inventory coverage: {c_res['coverage_weeks']} weeks, usable: {c_res['usable_stock_t']} t")
    assert c_res["status"] == "success"
    assert c_res["usable_stock_t"] > 0

    # Test 3: Landed cost (Japan into India)
    l_res = dispatch_tool("calculate_landed_cost", {
        "destination_iso3": "IND", "origin_iso3": "JPN", "hs6": "720810", "fob_usd_per_t": 630.0
    })
    print(f"Japan -> India landed: Rs {l_res['landed_cost_local_per_t']:,.2f}/t (Duty: {l_res['duty_pct']}%)")
    assert l_res["status"] == "success"

    # Test 4: What-If simulation
    w_res = dispatch_tool("run_what_if_scenario", {
        "destination_iso3": "IND", "origin_iso3": "JPN", "hs6": "720810", "base_fob_usd": 630.0,
        "fx_shift_pct": 5.0, "freight_usd_shift": 15.0
    })
    print(f"What-If 5% FX + $15 freight shift: variance = {w_res['cost_variance_pct']}%")
    assert w_res["status"] == "success"

    print("\nAll Procurement MCP tools PASSED verification!")

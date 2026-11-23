"""
erp.py — The ERP contract, and a realistic mock behind it.

Feasibility finding M-5 / §2.3: without internal data this system is a market
dashboard, not a procurement agent. In the original demo
`coverage_weeks_on_hand = 5.5` was invented — and it was the input that decided
the recommendation. This module makes that dependency explicit and swappable.

ERP access is confirmed likely, so the design goal is: **build against this
interface, and swapping the mock for SAP/Oracle is a config change, not a
rewrite.** Six read-only objects. Nothing here writes to the ERP, ever.

    Requirement      what production needs, by material/plant/date
    InventoryPosition  what you actually have — usable vs blocked vs in-transit
    OpenSupply       POs and schedule lines already placed
    ContractTerms    the pricing formula you are actually exposed to
    SupplierRecord   approved suppliers with lead-time and quality history
    PriceHistory     what you actually paid, for basis and negotiation

Implement `ERPSource` against the real system and everything downstream works
unchanged.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field
from typing import Protocol


# ==========================================================================
# The contract
# ==========================================================================

@dataclass
class Requirement:
    material_id: str
    plant: str
    need_by: str                 # ISO date
    quantity_t: float
    source: str = "MRP"
    firm: bool = True            # firm order vs forecast — do not double-count


@dataclass
class InventoryPosition:
    material_id: str
    plant: str
    as_of: str
    unrestricted_t: float        # the only stock you can actually consume
    blocked_t: float = 0.0       # quality hold — NOT available
    in_transit_t: float = 0.0    # shipped, not received
    consignment_t: float = 0.0   # supplier-owned — NOT yours
    safety_stock_t: float = 0.0

    @property
    def usable_t(self) -> float:
        """Blocked, consignment and in-transit stock are NOT coverage.
        Counting them is the single most common inventory error in
        procurement analytics."""
        return self.unrestricted_t


@dataclass
class OpenSupply:
    po_number: str
    material_id: str
    plant: str
    supplier_id: str
    quantity_t: float
    confirmed_date: str
    price_per_t: float
    currency: str = "INR"


@dataclass
class ContractTerms:
    contract_id: str
    supplier_id: str
    material_id: str
    pricing_basis: str           # e.g. "LME_CASH_M-1_AVG + premium"
    premium_per_t: float = 0.0
    currency: str = "INR"
    credit_days: int = 30
    volume_flex_pct: float = 0.0
    index_reset: str = "monthly"  # monthly | quarterly | fixed
    min_order_t: float = 0.0
    valid_to: str = ""


@dataclass
class SupplierRecord:
    supplier_id: str
    name: str
    country: str                 # ISO3 — feeds the country/tariff layer
    approved_materials: list = field(default_factory=list)
    lead_time_days_p50: float = 30
    lead_time_days_p90: float = 45
    otif_pct: float = 95.0
    reject_rate_pct: float = 0.4
    annual_capacity_t: float = 0.0


@dataclass
class PricePoint:
    date: str
    material_id: str
    supplier_id: str
    price_per_t: float
    quantity_t: float
    currency: str = "INR"


class ERPSource(Protocol):
    """Implement this against SAP/Oracle/Dynamics. Read-only by design."""
    def requirements(self, material_id: str, plant: str, horizon_days: int) -> list[Requirement]: ...
    def inventory(self, material_id: str, plant: str) -> InventoryPosition: ...
    def open_supply(self, material_id: str, plant: str) -> list[OpenSupply]: ...
    def contracts(self, material_id: str) -> list[ContractTerms]: ...
    def suppliers(self, material_id: str) -> list[SupplierRecord]: ...
    def price_history(self, material_id: str, days: int) -> list[PricePoint]: ...


# ==========================================================================
# Mock implementation — realistic shapes, obviously fake numbers
# ==========================================================================

class MockERP:
    """
    A deliberately realistic stand-in: skewed lead times, blocked stock,
    a firm/forecast split, one domestic and one import supplier.

    Every value is synthetic. `is_mock` is True so downstream code and the
    agent's own output can say so out loud.
    """
    is_mock = True

    def __init__(self, seed: int = 42):
        self.rng = random.Random(seed)

    def requirements(self, material_id: str, plant: str, horizon_days: int = 60):
        today = dt.date.today()
        out, week = [], 0
        while week * 7 < horizon_days:
            d = today + dt.timedelta(days=week * 7 + 5)
            out.append(Requirement(
                material_id, plant, d.isoformat(),
                round(self.rng.uniform(120, 190), 1),
                firm=(week < 3),          # near weeks firm, later weeks forecast
            ))
            week += 1
        return out

    def inventory(self, material_id: str, plant: str):
        return InventoryPosition(
            material_id, plant, dt.date.today().isoformat(),
            unrestricted_t=430.0, blocked_t=45.0,
            in_transit_t=210.0, consignment_t=60.0, safety_stock_t=160.0,
        )

    def open_supply(self, material_id: str, plant: str):
        today = dt.date.today()
        return [
            OpenSupply("4500091823", material_id, plant, "SUP-DOM-01", 210.0,
                       (today + dt.timedelta(days=9)).isoformat(), 59_400.0),
            OpenSupply("4500091977", material_id, plant, "SUP-DOM-01", 150.0,
                       (today + dt.timedelta(days=24)).isoformat(), 59_900.0),
        ]

    def contracts(self, material_id: str):
        return [ContractTerms(
            "CTR-2026-114", "SUP-DOM-01", material_id,
            pricing_basis="INDIA_HRC_MONTHLY_AVG + premium",
            premium_per_t=1_150.0, credit_days=30, volume_flex_pct=0.05,
            index_reset="monthly", min_order_t=100.0, valid_to="2027-03-31",
        )]

    def suppliers(self, material_id: str):
        return [
            SupplierRecord("SUP-DOM-01", "Domestic Mill A", "IND", [material_id],
                           lead_time_days_p50=12, lead_time_days_p90=21,
                           otif_pct=96.5, reject_rate_pct=0.3, annual_capacity_t=40_000),
            SupplierRecord("SUP-IMP-KR1", "Korean Mill B", "KOR", [material_id],
                           lead_time_days_p50=42, lead_time_days_p90=63,
                           otif_pct=91.0, reject_rate_pct=0.6, annual_capacity_t=120_000),
            SupplierRecord("SUP-IMP-JP1", "Japanese Mill C", "JPN", [material_id],
                           lead_time_days_p50=45, lead_time_days_p90=70,
                           otif_pct=94.0, reject_rate_pct=0.2, annual_capacity_t=90_000),
        ]

    def price_history(self, material_id: str, days: int = 540):
        out, today, base = [], dt.date.today(), 57_500.0
        for i in range(days // 30):
            d = today - dt.timedelta(days=30 * i)
            base *= (1 + self.rng.gauss(0.004, 0.028))
            out.append(PricePoint(d.isoformat(), material_id, "SUP-DOM-01",
                                  round(base, 2), round(self.rng.uniform(140, 200), 1)))
        return list(reversed(out))


# ==========================================================================
# Derived position — the numbers the decision engine actually consumes
# ==========================================================================

@dataclass
class CoveragePosition:
    material_id: str
    plant: str
    usable_stock_t: float
    in_transit_t: float
    open_po_t: float
    safety_stock_t: float
    demand_next_30d_t: float
    firm_demand_t: float
    weekly_burn_t: float
    coverage_weeks: float
    net_requirement_t: float
    is_mock: bool
    warnings: list = field(default_factory=list)


def coverage(src, material_id: str, plant: str, horizon_days: int = 60) -> CoveragePosition:
    """
    Turn six raw ERP objects into the coverage number the recommender needs.

    This function is the whole point of the module. It is also where the most
    expensive mistakes live, so the rules are explicit:
      - blocked and consignment stock are NOT coverage
      - in-transit counts only once it is inside the horizon
      - open POs count as supply, or you will double-buy
      - safety stock is a floor, not available inventory
    """
    reqs = src.requirements(material_id, plant, horizon_days)
    inv = src.inventory(material_id, plant)
    pos = src.open_supply(material_id, plant)

    horizon_end = dt.date.today() + dt.timedelta(days=30)
    demand_30 = sum(r.quantity_t for r in reqs
                    if dt.date.fromisoformat(r.need_by) <= horizon_end)
    firm = sum(r.quantity_t for r in reqs
               if r.firm and dt.date.fromisoformat(r.need_by) <= horizon_end)
    open_po = sum(p.quantity_t for p in pos
                  if dt.date.fromisoformat(p.confirmed_date) <= horizon_end)

    burn = demand_30 / 4.345 if demand_30 else 0.0
    available = inv.usable_t + inv.in_transit_t + open_po - inv.safety_stock_t
    weeks = (available / burn) if burn > 0 else 99.0
    net_req = max(0.0, demand_30 - (inv.usable_t + inv.in_transit_t + open_po - inv.safety_stock_t))

    warns = []
    if getattr(src, "is_mock", False):
        warns.append("MOCK ERP DATA — every internal figure here is synthetic. "
                     "No recommendation derived from it is actionable.")
    if inv.blocked_t > 0:
        warns.append(f"{inv.blocked_t:.0f} t blocked (quality hold) excluded from coverage.")
    if inv.consignment_t > 0:
        warns.append(f"{inv.consignment_t:.0f} t consignment stock excluded — supplier-owned.")
    if demand_30 > firm:
        warns.append(f"{demand_30 - firm:.0f} t of 30-day demand is FORECAST, not firm.")

    return CoveragePosition(
        material_id, plant, inv.usable_t, inv.in_transit_t, open_po,
        inv.safety_stock_t, round(demand_30, 1), round(firm, 1),
        round(burn, 1), round(weeks, 2), round(net_req, 1),
        getattr(src, "is_mock", False), warns,
    )


if __name__ == "__main__":
    src = MockERP()
    cp = coverage(src, "HRC-E250A-2.0", "PLANT-PUNE-1")
    print("ERP-derived coverage position\n" + "=" * 62)
    for k, v in cp.__dict__.items():
        if k != "warnings":
            print(f"  {k:<22} {v}")
    print("\n  Warnings:")
    for w in cp.warnings:
        print(f"   ! {w}")
    print("\n  Suppliers on the approved list:")
    for s in src.suppliers("HRC-E250A-2.0"):
        print(f"   {s.supplier_id:<13} {s.name:<18} {s.country}  "
              f"lead p50/p90 {s.lead_time_days_p50:.0f}/{s.lead_time_days_p90:.0f}d  "
              f"OTIF {s.otif_pct}%  reject {s.reject_rate_pct}%")

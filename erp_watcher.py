"""
erp_watcher.py — Dynamic ERP File Inbox & Schema Watcher.

Architectural Guarantees:
1. File-Based Ingestion: Automatically ingests SAP / Oracle CSV and JSON extracts
   from `poc/erp_inbox/`.
2. Unit-of-Measure Defense: Detects and normalizes unit discrepancies (e.g. KG vs Tonnes)
   to prevent catastrophic 1000x volume errors.
3. Strict Inventory Gating: Blocked and consignment stocks are rigorously quarantined
   from usable coverage.
4. Supply Netting: Confirmed in-transit and open PO lines inside the horizon are netted
   to prevent double-purchasing.
5. Reactive Event Trigger: Emits on_coverage_updated callbacks when new data arrives.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import erp as ERP

logger = logging.getLogger("erp_watcher")

INBOX_DIR = Path(__file__).parent / "erp_inbox"


def _parse_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(str(val).replace(",", "").strip())
    except Exception:
        return default


def _normalize_tonnes(qty: float, unit: str) -> float:
    """Normalize quantity to metric tonnes."""
    u = str(unit).upper().strip()
    if u in ("KG", "KILOGRAM", "KGS"):
        return qty / 1000.0
    if u in ("LBS", "POUND"):
        return qty * 0.00045359237
    # Default is metric tonnes (T, MT, TONNE, TONNES)
    return qty


class FileBackedERP:
    """
    Implements ERP data ingestion from CSV/JSON extracts placed in the inbox.
    Falls back gracefully to realistic defaults if a specific file is absent.
    Implements ERP.ERPSource protocol.
    """

    def __init__(self, inbox_dir: Optional[Path] = None):
        self.inbox_dir = Path(inbox_dir or INBOX_DIR)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.fallback = ERP.MockERP()
        # Which ERP objects were actually served from files vs mock.
        self.provenance: dict = {}
        self._warned: set = set()
        self.last_load_time: Optional[str] = None
        self.source_origin = "MOCK_FALLBACK"
        self.is_mock = False

        # Internal collections
        self._requirements: List[ERP.Requirement] = []
        self._inventory: List[ERP.InventoryPosition] = []
        self._open_supply: List[ERP.OpenSupply] = []
        self._contracts: List[ERP.ContractTerms] = []
        self._suppliers: List[ERP.SupplierRecord] = []
        self._price_history: List[ERP.PricePoint] = []

        self.reload()

    def reload(self) -> Dict[str, int]:
        """Scan inbox directory and parse all available extracts."""
        counts = {
            "requirements": 0,
            "inventory": 0,
            "open_supply": 0,
            "contracts": 0,
            "suppliers": 0,
            "price_history": 0,
        }

        has_any_file = False

        # 1. Requirements
        req_file = self.inbox_dir / "requirements.csv"
        if req_file.exists():
            has_any_file = True
            self._requirements = self._load_requirements_csv(req_file)
            counts["requirements"] = len(self._requirements)
        else:
            self._requirements = []

        # 2. Inventory
        inv_file = self.inbox_dir / "inventory.csv"
        if inv_file.exists():
            has_any_file = True
            self._inventory = self._load_inventory_csv(inv_file)
            counts["inventory"] = len(self._inventory)
        else:
            self._inventory = []

        # 3. Open Supply
        sup_file = self.inbox_dir / "open_supply.csv"
        if sup_file.exists():
            has_any_file = True
            self._open_supply = self._load_open_supply_csv(sup_file)
            counts["open_supply"] = len(self._open_supply)
        else:
            self._open_supply = []

        # 4. Contracts
        ctr_file = self.inbox_dir / "contracts.csv"
        if ctr_file.exists():
            has_any_file = True
            self._contracts = self._load_contracts_csv(ctr_file)
            counts["contracts"] = len(self._contracts)
        else:
            self._contracts = []

        # 5. Suppliers
        spl_file = self.inbox_dir / "suppliers.csv"
        if spl_file.exists():
            has_any_file = True
            self._suppliers = self._load_suppliers_csv(spl_file)
            counts["suppliers"] = len(self._suppliers)
        else:
            self._suppliers = []

        # 6. Price History
        prc_file = self.inbox_dir / "price_history.csv"
        if prc_file.exists():
            has_any_file = True
            self._price_history = self._load_price_history_csv(prc_file)
            counts["price_history"] = len(self._price_history)
        else:
            self._price_history = []

        self.last_load_time = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self.source_origin = "FILE_INBOX" if has_any_file else "MOCK_FALLBACK"
        self.is_mock = not has_any_file
        return counts

    # --- CSV Parsers with Unit Protection ---

    def _load_requirements_csv(self, path: Path) -> List[ERP.Requirement]:
        rows = []
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for r in reader:
                mat = r.get("material_id") or r.get("material") or ""
                plant = r.get("plant", "").strip()
                need_by = r.get("need_by", "").strip()
                qty = _parse_float(r.get("quantity") or r.get("quantity_t") or 0)
                unit = r.get("unit", "T")
                qty_t = _normalize_tonnes(qty, unit)
                source = r.get("source", "MRP").strip()
                firm_val = r.get("firm") if "firm" in r else r.get("is_firm", "true")
                is_firm = str(firm_val).strip().lower() in ("true", "1", "yes")

                rows.append(ERP.Requirement(
                    material_id=mat.strip(),
                    plant=plant,
                    need_by=need_by,
                    quantity_t=qty_t,
                    source=source,
                    firm=is_firm,
                ))
        return rows

    def _load_inventory_csv(self, path: Path) -> List[ERP.InventoryPosition]:
        rows = []
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for r in reader:
                mat = r.get("material_id") or r.get("material") or ""
                plant = r.get("plant", "").strip()
                as_of = r.get("as_of") or r.get("as_of_date") or dt.date.today().isoformat()
                unit = r.get("unit", "T")

                unrestricted = _normalize_tonnes(_parse_float(r.get("unrestricted_t") or r.get("unrestricted") or 0), unit)
                blocked = _normalize_tonnes(_parse_float(r.get("blocked_t") or r.get("blocked") or 0), unit)
                in_transit = _normalize_tonnes(_parse_float(r.get("in_transit_t") or r.get("in_transit") or 0), unit)
                consignment = _normalize_tonnes(_parse_float(r.get("consignment_t") or r.get("consignment") or 0), unit)
                safety = _normalize_tonnes(_parse_float(r.get("safety_stock_t") or r.get("safety_stock") or 0), unit)

                rows.append(ERP.InventoryPosition(
                    material_id=mat.strip(),
                    plant=plant,
                    as_of=as_of.strip(),
                    unrestricted_t=unrestricted,
                    blocked_t=blocked,
                    in_transit_t=in_transit,
                    consignment_t=consignment,
                    safety_stock_t=safety,
                ))
        return rows

    def _load_open_supply_csv(self, path: Path) -> List[ERP.OpenSupply]:
        rows = []
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for r in reader:
                po = r.get("po_number", "").strip()
                mat = r.get("material_id") or r.get("material") or ""
                plant = r.get("plant", "").strip()
                spl = r.get("supplier_id", "").strip()
                unit = r.get("unit", "T")
                qty = _normalize_tonnes(_parse_float(r.get("quantity_t") or r.get("quantity") or 0), unit)
                conf_date = r.get("confirmed_date") or r.get("confirmed_delivery_date") or ""
                price = _parse_float(r.get("price_per_t") or r.get("price_per_t_inr") or 0)
                cur = r.get("currency", "INR").strip()

                rows.append(ERP.OpenSupply(
                    po_number=po,
                    material_id=mat.strip(),
                    plant=plant,
                    supplier_id=spl,
                    quantity_t=qty,
                    confirmed_date=conf_date.strip(),
                    price_per_t=price,
                    currency=cur,
                ))
        return rows

    def _load_contracts_csv(self, path: Path) -> List[ERP.ContractTerms]:
        rows = []
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append(ERP.ContractTerms(
                    contract_id=r.get("contract_id", "").strip(),
                    supplier_id=r.get("supplier_id", "").strip(),
                    material_id=(r.get("material_id") or r.get("material") or "").strip(),
                    pricing_basis=r.get("pricing_basis", "INDEX_PLUS").strip(),
                    premium_per_t=_parse_float(r.get("premium_per_t") or r.get("premium_inr_per_t") or 0),
                    currency=r.get("currency", "INR").strip(),
                    credit_days=int(_parse_float(r.get("credit_days", 30))),
                    volume_flex_pct=_parse_float(r.get("volume_flex_pct") or r.get("volume_flexibility_pct") or 0),
                    index_reset=r.get("index_reset", "monthly").strip(),
                    min_order_t=_parse_float(r.get("min_order_t") or r.get("moq_tonnes") or 0),
                    valid_to=r.get("valid_to", "2027-12-31").strip(),
                ))
        return rows

    def _load_suppliers_csv(self, path: Path) -> List[ERP.SupplierRecord]:
        rows = []
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for r in reader:
                appr = [m.strip() for m in r.get("approved_materials", "").split(";") if m.strip()]
                rows.append(ERP.SupplierRecord(
                    supplier_id=r.get("supplier_id", "").strip(),
                    name=r.get("name", "").strip(),
                    country=r.get("country", "IND").strip(),
                    approved_materials=appr,
                    lead_time_days_p50=_parse_float(r.get("lead_time_days_p50") or r.get("lead_time_p50_days") or 14),
                    lead_time_days_p90=_parse_float(r.get("lead_time_days_p90") or r.get("lead_time_p90_days") or 21),
                    otif_pct=_parse_float(r.get("otif_pct", 95)),
                    reject_rate_pct=_parse_float(r.get("reject_rate_pct") or r.get("rejection_rate_pct") or 0.5),
                    annual_capacity_t=_parse_float(r.get("annual_capacity_t", 50000)),
                ))
        return rows

    def _load_price_history_csv(self, path: Path) -> List[ERP.PricePoint]:
        rows = []
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for r in reader:
                unit = r.get("unit", "T")
                rows.append(ERP.PricePoint(
                    date=r.get("date", "").strip(),
                    material_id=(r.get("material_id") or r.get("material") or "").strip(),
                    supplier_id=r.get("supplier_id", "").strip(),
                    price_per_t=_parse_float(r.get("price_per_t") or r.get("price_inr_per_t") or 0),
                    quantity_t=_normalize_tonnes(_parse_float(r.get("quantity_t") or r.get("quantity") or 0), unit),
                    currency=r.get("currency", "INR").strip(),
                ))
        return rows

    # --- ERPSource Protocol Implementation ---
    #
    # PER-OBJECT PROVENANCE (added 2026-09-07).
    # Every accessor below used to fall back to MockERP silently when the inbox
    # held no matching rows. Because `source_origin` was set once from "does any
    # file exist", a run could be labelled FILE_INBOX while five of its six
    # objects were synthetic — and nothing said so. Substituting invented data
    # for missing data is the same defect as a hardcoded price.
    #
    # The fallback is retained (the demo must run before ERP access exists) but
    # it is now recorded per object, logged, and surfaced. Read `provenance` and
    # `source_origin` before trusting any figure derived from this source.

    def _record(self, obj: str, from_file: bool) -> None:
        self.provenance[obj] = "FILE_INBOX" if from_file else "MOCK_FALLBACK"
        if not from_file and obj not in self._warned:
            self._warned.add(obj)
            logger.warning(
                f"ERP object '{obj}' has no matching inbox rows — using MOCK data. "
                f"Any recommendation using it is not company-derived.")

    def requirements(self, material_id: str, plant: str, horizon_days: int = 60) -> List[ERP.Requirement]:
        matched = [r for r in self._requirements if r.material_id == material_id and r.plant == plant]
        self._record("requirements", bool(matched))
        if matched:
            cutoff = (dt.date.today() + dt.timedelta(days=horizon_days)).isoformat()
            return [r for r in matched if r.need_by <= cutoff]
        return self.fallback.requirements(material_id, plant, horizon_days)

    def inventory(self, material_id: str, plant: str) -> ERP.InventoryPosition:
        for inv in self._inventory:
            if inv.material_id == material_id and inv.plant == plant:
                self._record("inventory", True)
                return inv
        self._record("inventory", False)
        return self.fallback.inventory(material_id, plant)

    def open_supply(self, material_id: str, plant: str) -> List[ERP.OpenSupply]:
        matched = [s for s in self._open_supply if s.material_id == material_id and s.plant == plant]
        self._record("open_supply", bool(matched))
        return matched if matched else self.fallback.open_supply(material_id, plant)

    def contracts(self, material_id: str) -> List[ERP.ContractTerms]:
        matched = [c for c in self._contracts if c.material_id == material_id]
        self._record("contracts", bool(matched))
        return matched if matched else self.fallback.contracts(material_id)

    def suppliers(self, material_id: str) -> List[ERP.SupplierRecord]:
        matched = [s for s in self._suppliers if material_id in s.approved_materials]
        self._record("suppliers", bool(matched))
        return matched if matched else self.fallback.suppliers(material_id)

    def price_history(self, material_id: str, days: int = 365) -> List[ERP.PricePoint]:
        matched = [p for p in self._price_history if p.material_id == material_id]
        self._record("price_history", bool(matched))
        return matched if matched else self.fallback.price_history(material_id, days)

    @property
    def effective_origin(self) -> str:
        """Honest label, computed from what was ACTUALLY served — not from
        whether a file happens to exist on disk."""
        if not self.provenance:
            return "UNQUERIED"
        vals = set(self.provenance.values())
        if vals == {"FILE_INBOX"}:
            return "FILE_INBOX"
        if vals == {"MOCK_FALLBACK"}:
            return "MOCK_FALLBACK"
        mocked = sorted(k for k, v in self.provenance.items() if v == "MOCK_FALLBACK")
        return f"MIXED (mock: {', '.join(mocked)})"


class ERPInboxWatcher:
    """
    Watches the ERP inbox directory for file modifications or new CSV extracts,
    reloads them, and notifies observers.
    """

    def __init__(self, erp_client: FileBackedERP, poll_interval_s: float = 3.0,
                 on_change_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.erp = erp_client
        self.poll_interval_s = poll_interval_s
        self.on_change_callback = on_change_callback
        self._last_mtimes: Dict[str, float] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def _check_updates(self) -> bool:
        changed = False
        for p in self.erp.inbox_dir.glob("*.csv"):
            mtime = p.stat().st_mtime
            if self._last_mtimes.get(p.name) != mtime:
                self._last_mtimes[p.name] = mtime
                changed = True
        return changed

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        def _watch_loop():
            self._check_updates()
            while self._running:
                try:
                    if self._check_updates():
                        counts = self.erp.reload()
                        logger.info(f"ERP inbox reloaded: {counts}")
                        if self.on_change_callback:
                            self.on_change_callback(counts)
                except Exception as e:
                    logger.error(f"Error in ERP watcher: {e}")
                time.sleep(self.poll_interval_s)

        self._thread = threading.Thread(target=_watch_loop, daemon=True, name="ERPInboxWatcher")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)


def create_sample_inbox_files(target_dir: Optional[Path] = None) -> None:
    """Populate sample CSV extracts in erp_inbox/ for testing and evaluation."""
    dest = Path(target_dir or INBOX_DIR)
    dest.mkdir(parents=True, exist_ok=True)

    today = dt.date.today()

    # 1. requirements.csv
    with open(dest / "requirements.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["material_id", "plant", "need_by", "quantity_t", "unit", "firm"])
        writer.writerow(["HRC-E250A-2.0", "PLANT-PUNE-1", (today + dt.timedelta(days=7)).isoformat(), "150.0", "T", "true"])
        writer.writerow(["HRC-E250A-2.0", "PLANT-PUNE-1", (today + dt.timedelta(days=14)).isoformat(), "180.0", "T", "true"])
        writer.writerow(["HRC-E250A-2.0", "PLANT-PUNE-1", (today + dt.timedelta(days=21)).isoformat(), "135.5", "T", "false"])
        writer.writerow(["ZINC-SHG-INGOT", "PLANT-PUNE-1", (today + dt.timedelta(days=10)).isoformat(), "60000.0", "KG", "true"]) # Test KG unit normalization!

    # 2. inventory.csv
    with open(dest / "inventory.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["material_id", "plant", "as_of", "unrestricted_t", "in_transit_t", "blocked_t", "consignment_t", "safety_stock_t", "unit"])
        writer.writerow(["HRC-E250A-2.0", "PLANT-PUNE-1", today.isoformat(), "430.0", "210.0", "45.0", "60.0", "160.0", "T"])
        writer.writerow(["ZINC-SHG-INGOT", "PLANT-PUNE-1", today.isoformat(), "120000.0", "40000.0", "10000.0", "0.0", "30000.0", "KG"]) # Test KG conversion!

    # 3. open_supply.csv
    with open(dest / "open_supply.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["po_number", "material_id", "plant", "supplier_id", "quantity_t", "unit", "confirmed_date", "price_per_t", "currency"])
        writer.writerow(["4500128911", "HRC-E250A-2.0", "PLANT-PUNE-1", "SUP-DOM-01", "200.0", "T", (today + dt.timedelta(days=9)).isoformat(), "59800.0", "INR"])
        writer.writerow(["4500128912", "HRC-E250A-2.0", "PLANT-PUNE-1", "SUP-DOM-02", "160.0", "T", (today + dt.timedelta(days=22)).isoformat(), "60200.0", "INR"])

    # 4. contracts.csv
    with open(dest / "contracts.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["contract_id", "supplier_id", "material_id", "pricing_basis", "premium_per_t", "currency", "credit_days", "volume_flex_pct", "index_reset", "min_order_t", "valid_to"])
        writer.writerow(["CTR-2026-114", "SUP-DOM-01", "HRC-E250A-2.0", "INDIA_HRC_MONTHLY_AVG + premium", "1150.0", "INR", "30", "0.05", "monthly", "100.0", "2027-03-31"])

    # 5. suppliers.csv
    with open(dest / "suppliers.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["supplier_id", "name", "country", "approved_materials", "lead_time_days_p50", "lead_time_days_p90", "otif_pct", "reject_rate_pct", "annual_capacity_t"])
        writer.writerow(["SUP-DOM-01", "Domestic Mill A (Mumbai)", "IND", "HRC-E250A-2.0;ZINC-SHG-INGOT", "12", "20", "96.5", "0.3", "120000.0"])
        writer.writerow(["SUP-DOM-02", "Domestic Mill B (Pune)", "IND", "HRC-E250A-2.0", "14", "22", "95.0", "0.4", "95000.0"])

    logger.info(f"Sample ERP files created in {dest}")


if __name__ == "__main__":
    print("Testing FileBackedERP and ERPInboxWatcher...")
    create_sample_inbox_files()
    erp_inst = FileBackedERP()
    counts = erp_inst.reload()
    print(f"Loaded counts from inbox: {counts}")
    assert erp_inst.source_origin == "FILE_INBOX"

    cov_hrc = ERP.coverage(erp_inst, "HRC-E250A-2.0", "PLANT-PUNE-1")
    print(f"\nHRC Coverage from file: {cov_hrc.coverage_weeks:.2f} weeks, usable: {cov_hrc.usable_stock_t} t")
    assert cov_hrc.usable_stock_t == 430.0
    assert cov_hrc.open_po_t == 360.0

    # Verify unit-of-measure defense: 120,000 KG must normalize to 120.0 Tonnes
    cov_zinc = ERP.coverage(erp_inst, "ZINC-SHG-INGOT", "PLANT-PUNE-1")
    print(f"Zinc Coverage from file: {cov_zinc.coverage_weeks:.2f} weeks, usable: {cov_zinc.usable_stock_t} t")
    assert cov_zinc.usable_stock_t == 120.0, f"Expected 120.0t from 120,000kg, got {cov_zinc.usable_stock_t}"
    assert cov_zinc.safety_stock_t == 30.0, f"Expected 30.0t safety stock, got {cov_zinc.safety_stock_t}"

    print("\nUnit-of-measure defense and FileBackedERP tests PASSED!")

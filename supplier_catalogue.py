"""Merge reviewed regional evidence without turning research leads into offers."""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
FILES = ("supplier_research.json", "supplier_research_europe.json",
         "supplier_research_asia.json", "supplier_research_other.json")


def load(base=BASE):
    suppliers, coverage, reviews = {}, [], []
    for name in FILES:
        path = Path(base) / name
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        reviews.append(data.get("reviewed_at", ""))
        coverage.extend(data.get("coverage", []))
        for supplier in data["suppliers"]:
            entry = dict(supplier)
            entry.setdefault("reviewed_at", data.get("reviewed_at"))
            entry.setdefault("order_status", "not_confirmed")
            entry.setdefault("product_match", "unconfirmed_grade")
            entry.setdefault("origin_status", "unconfirmed")
            # Regional re-research of the same ID can enrich the old entry.
            suppliers[entry["id"]] = {**suppliers.get(entry["id"], {}), **entry}
    return {"reviewed_at": max(reviews, default=""), "suppliers": list(suppliers.values()),
            "coverage": coverage,
            "method": "Primary product and sales-channel research. Published RFQ channels accept enquiries, not necessarily this order. No current quote, stock, shipment origin, discount or delivery commitment is inferred. Product-family and grade gaps are explicit."}


def priority(supplier):
    """Evidence/contact priority, never a best-price or accepted-order ranking."""
    product = {"exact":0, "unconfirmed_grade":1, "family_only":2}.get(supplier.get("product_match"), 1)
    contact = bool(supplier.get("contact_url") and supplier.get("contact_evidence"))
    return (not contact, product, supplier["name"].casefold())

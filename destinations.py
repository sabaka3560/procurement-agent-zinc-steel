"""
destinations.py — What the system can and cannot do, per destination country.

WHY THIS MODULE EXISTS
======================
The tariff and country-ranking layers are genuinely global — WITS and Comtrade
cover any destination. But three other layers are NOT:

  * DOMESTIC PRICE BENCHMARK.  India has Tata Nexarc (free, HRC by brand, city,
    grade and thickness). **No free equivalent was found for any other country.**
    Without it there is no "import vs domestic" comparison and no benchmark to
    negotiate against.
  * FX.  India has FBIL, its official reference rate. Other currencies come from
    frankfurter's ECB feed — which covers 30 currencies, and **not VND, AED,
    SAR, TWD, EGP or RUB.**
  * SURCHARGES.  Safeguards, anti-dumping and cesses are hand-maintained. Only
    India's are populated today.

Before this module existed, `run_deployment_demo.py --dest THA` produced a
**Thai** procurement recommendation priced in **rupees** against **Indian**
domestic steel prices. Confident, fully formatted, and completely wrong.

That is precisely the failure this architecture is supposed to prevent, so
capability is now explicit and the system refuses rather than substitutes.
Verified 2026-09-05.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import countries as CO

# ECB-sourced currencies available free via frankfurter.app (tested 2026-09-05).
FRANKFURTER_CURRENCIES = {
    "AUD", "BGN", "BRL", "CAD", "CHF", "CNY", "CZK", "DKK", "EUR", "GBP",
    "HKD", "HUF", "IDR", "ILS", "INR", "ISK", "JPY", "KRW", "MXN", "MYR",
    "NOK", "NZD", "PHP", "PLN", "RON", "SEK", "SGD", "THB", "TRY", "USD",
    "ZAR",
}

TIER_FULL = "FULL"          # tariffs + FX + domestic benchmark + surcharges
TIER_PARTIAL = "PARTIAL"    # tariffs + FX, no domestic benchmark
TIER_MINIMAL = "MINIMAL"    # tariffs only — no free FX for this currency
TIER_NONE = "NONE"          # not usable


@dataclass
class DestinationProfile:
    iso3: str
    name: str
    currency: str
    fx_source: str | None
    domestic_benchmark: str | None
    surcharges_maintained: bool
    notes: str = ""
    missing: list = field(default_factory=list)

    @property
    def tier(self) -> str:
        if self.fx_source is None:
            return TIER_MINIMAL
        if self.domestic_benchmark and self.surcharges_maintained:
            return TIER_FULL
        return TIER_PARTIAL

    @property
    def can_rank_countries(self) -> bool:
        """Country ranking needs only WITS + Comtrade — works everywhere."""
        return True

    @property
    def can_compare_domestic(self) -> bool:
        """Import-vs-domestic needs a local price benchmark."""
        return self.domestic_benchmark is not None

    @property
    def can_build_negotiation_brief(self) -> bool:
        """A negotiation brief without a benchmark is just arithmetic on air."""
        return self.domestic_benchmark is not None

    @property
    def can_price_in_local_currency(self) -> bool:
        return self.fx_source is not None

    def explain(self) -> str:
        L = [f"{self.name} ({self.iso3}) — capability tier: {self.tier}",
             f"   currency              {self.currency}",
             f"   FX source             {self.fx_source or 'NONE — no free feed for this currency'}",
             f"   domestic benchmark    {self.domestic_benchmark or 'NONE — no free source found'}",
             f"   surcharges maintained {'yes' if self.surcharges_maintained else 'NO — safeguards/AD not populated'}"]
        L.append("   can do:      rank source countries, resolve MFN + FTA tariffs")
        if self.can_price_in_local_currency:
            L.append("                convert to local currency")
        if self.can_compare_domestic:
            L.append("                compare import vs domestic, build a negotiation brief")
        if self.missing:
            L.append("   CANNOT do:   " + "; ".join(self.missing))
        if self.notes:
            L.append(f"   note: {self.notes}")
        return "\n".join(L)


# --------------------------------------------------------------------------
# Explicitly configured destinations. Everything else is derived.
# --------------------------------------------------------------------------
_EXPLICIT = {
    "IND": dict(
        currency="INR",
        fx_source="FBIL (India's official reference rate) via frankfurter",
        domestic_benchmark="Tata Nexarc — HRC by brand/city/grade/thickness, free",
        surcharges_maintained=True,
        notes="The only destination with a full free stack. DGTR steel safeguard "
              "11.5% and zinc AIDC 5% are populated in tariff.SURCHARGES.",
    ),
}


def profile(dest: str) -> DestinationProfile:
    """Capability profile for a destination. Never guesses — reports gaps."""
    c = CO.get(dest)
    if c is None:
        return DestinationProfile(str(dest), str(dest), "?", None, None, False,
                                  notes="unknown country code",
                                  missing=["everything — country not in the registry"])

    if c.iso3 in _EXPLICIT:
        e = _EXPLICIT[c.iso3]
        return DestinationProfile(c.iso3, c.name, e["currency"], e["fx_source"],
                                  e["domestic_benchmark"], e["surcharges_maintained"],
                                  e.get("notes", ""))

    cur = _CURRENCY.get(c.iso3, "?")
    fx = (f"ECB via frankfurter (USD/{cur})"
          if cur in FRANKFURTER_CURRENCIES else None)

    missing = ["compare import vs domestic (no free local price benchmark)",
               "build a negotiation brief (needs a benchmark)"]
    if fx is None:
        missing.insert(0, f"price in local currency (no free feed for {cur}) "
                          f"— USD only")
    missing.append("apply safeguards / anti-dumping (not populated for this destination)")

    return DestinationProfile(
        c.iso3, c.name, cur, fx, None, False,
        notes="Country ranking and MFN/FTA tariffs work. Everything downstream "
              "of a domestic price does not.",
        missing=missing,
    )


_CURRENCY = {
    "IND": "INR", "KOR": "KRW", "JPN": "JPY", "CHN": "CNY", "VNM": "VND",
    "IDN": "IDR", "THA": "THB", "MYS": "MYR", "TWN": "TWD", "USA": "USD",
    "DEU": "EUR", "ITA": "EUR", "FRA": "EUR", "NLD": "EUR", "ESP": "EUR",
    "BEL": "EUR", "AUT": "EUR", "FIN": "EUR", "CHE": "CHF", "NOR": "NOK",
    "GBR": "GBP", "TUR": "TRY", "ARE": "AED", "SAU": "SAR", "OMN": "OMR",
    "QAT": "QAR", "RUS": "RUB", "UKR": "UAH", "KAZ": "KZT", "BRA": "BRL",
    "MEX": "MXN", "ZAF": "ZAR", "EGY": "EGP", "AUS": "AUD", "CAN": "CAD",
    "POL": "PLN", "SWE": "SEK", "SGP": "SGD", "BHR": "BHD", "PHL": "PHP",
    "BGD": "BDT", "LKA": "LKR",
}


def coverage_table() -> list[DestinationProfile]:
    return [profile(c.iso3) for c in
            sorted(CO._C, key=lambda x: (profile(x.iso3).tier != TIER_FULL, x.name))]


if __name__ == "__main__":
    print("DESTINATION CAPABILITY MATRIX")
    print("=" * 92)
    print(f"  {'dest':<6} {'name':<18} {'cur':<5} {'tier':<9} {'FX':<5} "
          f"{'domestic':<9} {'surchg':<7}")
    print("  " + "-" * 88)
    counts = {}
    for p in coverage_table():
        counts[p.tier] = counts.get(p.tier, 0) + 1
        print(f"  {p.iso3:<6} {p.name[:17]:<18} {p.currency:<5} {p.tier:<9} "
              f"{'yes' if p.fx_source else 'NO':<5} "
              f"{'yes' if p.domestic_benchmark else 'no':<9} "
              f"{'yes' if p.surcharges_maintained else 'no':<7}")
    print("\n  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print("\n" + "=" * 92)
    for iso in ("IND", "THA", "VNM"):
        print()
        print(profile(iso).explain())

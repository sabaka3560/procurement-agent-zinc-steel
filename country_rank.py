"""
country_rank.py — "Which country is cheapest to buy from?" for ANY destination.

This is the module the scope change created. It answers the client's question:
    "countries which are favourable for the manufacturer's country ...
     someone with less taxes and less trade expense"

Method — four things stacked, each from a free live source:

  1. WHO CAN ACTUALLY SUPPLY?      UN Comtrade: which countries demonstrably
                                   shipped this HS code to this destination,
                                   in what tonnage. Proven capability beats
                                   a directory listing.
  2. WHAT DOES THE GOODS COST?     Comtrade unit value (trade value / net
                                   weight) per origin. Import flows are valued
                                   CIF, so freight and insurance are ALREADY in
                                   this number and are not added again.
  3. WHAT DOES THE STATE CHARGE?   tariff.py — MFN vs FTA preferential,
                                   plus safeguards/anti-dumping.
  4. WHAT DOES THE TRADE COST?     freight estimate by region pair +
                                   World Bank Logistics Performance Index
                                   for the origin (friction proxy).

Everything is labelled with its provenance and staleness. Nothing is presented
as decision-grade unless the tariff layer says it is.

HONEST LIMITS, stated up front:
  - Comtrade unit values are trade-value/quantity averages across every grade,
    width and contract in that HS line, on a CIF basis. They are a SCREENING
    signal, not a quotable price. A real price comes from an RFQ.
  - The freight estimate is reported for context but is NOT added to landed
    cost, because CIF already includes it. No free API gives port-to-port rates
    for arbitrary country pairs anyway.
  - The trade screen runs at HS4, which is a broader product population than the
    HS6 specification being priced. Treat the origin list as candidates.
  - This ranks COUNTRIES. It cannot name a vendor — no public trade dataset
    publishes exporter identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt

import time
import threading

import requests

import connectors as C
import countries as CO
import tariff as T

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125"}


# --------------------------------------------------------------------------
# Freight — region-pair estimates. REPLACE WITH REAL QUOTES.
# --------------------------------------------------------------------------
# USD per tonne, breakbulk/containerised steel, indicative mid-2020s levels.
# Tested: no free API returns port-to-port RATES for arbitrary country pairs
# (Searoutes 403s without a key; Drewry/Freightos free indices carry no India
# lane; Xeneta targets shippers above 10,000 TEU/yr). So this is a table, and
# it is labelled as an estimate everywhere it surfaces.
_FREIGHT_USD_T = {
    ("South Asia", "East Asia"): 42, ("South Asia", "Southeast Asia"): 34,
    ("South Asia", "Middle East"): 26, ("South Asia", "Europe"): 62,
    ("South Asia", "Europe/CIS"): 58, ("South Asia", "North America"): 78,
    ("South Asia", "South America"): 85, ("South Asia", "Africa"): 48,
    ("South Asia", "Oceania"): 60, ("South Asia", "Central Asia"): 70,
    ("South Asia", "South Asia"): 18,
    ("Southeast Asia", "East Asia"): 30, ("Southeast Asia", "Southeast Asia"): 20,
    ("East Asia", "East Asia"): 22, ("Europe", "Europe"): 25,
    ("Europe", "East Asia"): 65, ("Europe", "Europe/CIS"): 30,
    ("Europe", "Middle East"): 45, ("North America", "East Asia"): 72,
    ("North America", "Europe"): 55, ("Middle East", "East Asia"): 48,
    ("Middle East", "Middle East"): 20, ("Africa", "Europe"): 45,
}
_DEFAULT_FREIGHT = 70


def freight_estimate(origin: str, destination: str) -> tuple[float, str]:
    o, d = CO.get(origin), CO.get(destination)
    if not o or not d:
        return _DEFAULT_FREIGHT, "default (unknown region)"
    if o.iso3 == d.iso3:
        return 12.0, "domestic"
    for k in ((d.region, o.region), (o.region, d.region)):
        if k in _FREIGHT_USD_T:
            return float(_FREIGHT_USD_T[k]), f"region estimate {o.region} -> {d.region}"
    return _DEFAULT_FREIGHT, f"default (no entry for {o.region} -> {d.region})"


# --------------------------------------------------------------------------
# World Bank Logistics Performance Index — free, no key
# --------------------------------------------------------------------------
_lpi_cache: dict[str, float | None] = {}


def lpi(iso3: str) -> float | None:
    """LPI overall score, 1 (low) to 5 (high). Free World Bank API."""
    if iso3 in _lpi_cache:
        return _lpi_cache[iso3]
    try:
        r = requests.get(
            f"https://api.worldbank.org/v2/country/{iso3}/indicator/LP.LPI.OVRL.XQ"
            "?format=json&mrnev=1", headers=UA, timeout=30)
        data = r.json()
        val = data[1][0]["value"] if len(data) > 1 and data[1] else None
    except Exception:
        val = None
    _lpi_cache[iso3] = val
    return val


# --------------------------------------------------------------------------
@dataclass
class SourceOption:
    origin: str
    origin_name: str
    proven_tonnes: float
    fob_usd_per_t: float | None    # NOTE: CIF-basis for import flows, not FOB
    fob_source: str
    duty_pct: float | None
    duty_basis: str
    duty_decision_grade: bool
    surcharges: list = field(default_factory=list)
    freight_usd_per_t: float = 0.0
    freight_basis: str = ""
    lpi_score: float | None = None
    landed_usd_per_t: float | None = None
    notes: list = field(default_factory=list)


class RankedSources(list):
    """Legacy-compatible ranking with an unfiltered view of reported partners."""
    def __init__(self, rows=(), worldwide_rates=()):
        super().__init__(rows)
        self.worldwide_rates = list(worldwide_rates)


def worldwide_rates(flows, flow_year, dest, hs6):
    source_url = ("https://comtradeapi.un.org/public/v1/preview/C/A/HS"
                  f"?reporterCode={dest.comtrade}&period={flow_year}&cmdCode={hs6[:4]}&flowCode=M")
    rows = []
    for flow in flows:
        country = CO.BY_COMTRADE.get(flow["partner_code"])
        if country and country.iso3 == dest.iso3:
            continue
        rows.append(dict(country=country.iso3 if country else flow.get("partner_iso"),
            name=country.name if country else flow["partner"], comtrade_code=flow["partner_code"],
            unit_value_usd_per_t=flow["unit_value_usd_per_t"],
            tonnes=flow["net_weight_kg"]/1000 if flow["net_weight_kg"] else None,
            year=flow_year, source_url=source_url, tariff_registry_supported=bool(country),
            provisional=flow_year==dt.datetime.now(dt.timezone.utc).year))
    return sorted(rows,key=lambda r:(r["unit_value_usd_per_t"] is None,r["unit_value_usd_per_t"] or 0,r["name"]))


def rank_source_countries(
    destination: str,
    hs6: str,
    year: int | None = None,
    top_n: int = 10,
    insurance_pct: float = 0.001125,
    min_tonnes: float = 1_000.0,
) -> list[SourceOption]:
    """
    Rank origin countries by estimated landed cost into `destination`.

    `hs6` is the 6-digit HS code; the Comtrade call uses its 4-digit parent
    so the trade-flow screen isn't over-narrow.
    """
    dest = CO.get(destination)
    if dest is None:
        raise ValueError(f"unknown destination: {destination}")
    if year is None:
        year = dt.datetime.now(dt.timezone.utc).year

    # 1. Who actually supplies this destination? (Comtrade uses its OWN codes.)
    #    Walk back through years — reporters publish on different schedules.
    flows, flow_year = imports_by_partner_any_year(dest.comtrade, hs6[:4], year)
    if not flows:
        return RankedSources()
    worldwide = worldwide_rates(flows, flow_year, dest, hs6)

    out: list[SourceOption] = []
    for f in flows:
        origin = CO.BY_COMTRADE.get(f["partner_code"])
        if origin is None or origin.iso3 == dest.iso3:
            continue
        tonnes = (f["net_weight_kg"] or 0) / 1000
        if tonnes < min_tonnes:
            continue

        # IMPORTANT — UN Comtrade valuation.
        # UNCTAD: "It is recommended that import values be recorded as CIF-type
        # values and export values as FOB-type values." We read IMPORT flows
        # (flowCode=M), so this unit value ALREADY includes freight and insurance
        # to the destination border. The previous code named it `fob` and then
        # added freight + insurance again -- double-counting both, unequally,
        # because the freight estimate varies by region ($34 SE Asia, $42 East
        # Asia, $62 Europe). That distorted the ranking, not just the level.
        # Caveat kept on the record: a few reporters (e.g. Canada, Mexico) file
        # imports FOB, so for those the CIF assumption understates landed cost.
        cif_unit = f["unit_value_usd_per_t"]
        total, tres, sc = T.total_duty_pct(dest.iso3, origin.iso3, hs6)
        fr, fr_basis = freight_estimate(origin.iso3, dest.iso3)

        landed = None
        if cif_unit and total is not None:
            # Duty is levied on the CIF value; no freight is added on top.
            landed = cif_unit * (1 + total / 100)

        notes = list(tres.notes)
        if flow_year < year:
            notes.append(f"Requested {year} first; using {flow_year} fallback. A newer-year lookup may have returned no rows, failed or been rate-limited. This does not establish that newer data is unpublished.")
        current_year = flow_year == dt.datetime.now(dt.timezone.utc).year
        if current_year:
            notes.append("Current-year annual-series data: year is incomplete and coverage may be provisional. Not a full-year average or a live supplier quote.")
        if not tres.usable_for_decision:
            notes.append("Duty not decision-grade — ranking is INDICATIVE ONLY.")

        out.append(SourceOption(
            origin=origin.iso3, origin_name=origin.name,
            proven_tonnes=tonnes,
            fob_usd_per_t=round(cif_unit, 2) if cif_unit else None,   # CIF-basis; see note above
            fob_source=f"Comtrade {flow_year} unit value, CIF-basis (" + ("incomplete-year / provisional; " if current_year else "") + "screening proxy, not a quote)",
            duty_pct=total, duty_basis=tres.source,
            duty_decision_grade=tres.usable_for_decision,
            surcharges=sc, freight_usd_per_t=fr, freight_basis=fr_basis,
            lpi_score=lpi(origin.iso3),
            landed_usd_per_t=round(landed, 2) if landed else None,
            notes=notes,
        ))

    out.sort(key=lambda x: (x.landed_usd_per_t is None, x.landed_usd_per_t or 0))
    return RankedSources(out[:top_n], worldwide_rates=worldwide)



_flow_cache: dict[tuple, list] = {}
_flow_cached_at: dict[tuple, float] = {}
_flow_lock = threading.Lock()


def imports_by_partner_any_year(reporter_comtrade: int, hs: str,
                                year: int, back: int = 3) -> tuple[list[dict], int | None]:
    """
    Comtrade flows, walking back until a year with data is found.

    Two behaviours learned the hard way during this engagement:
      - Reporters publish on different schedules. Vietnam had NO 2024 data but
        23 partners for 2023. Failing on the first empty year silently drops
        whole destinations from the analysis.
      - The free `preview` tier rate-limits with HTTP 429 under a burst. Cache
        and back off, or a multi-destination run will half-fail.
    """
    for yr in range(year, year - back - 1, -1):
        key = (reporter_comtrade, hs, yr)
        with _flow_lock:
            # Recheck publication availability instead of pinning empty or old
            # annual responses for the lifetime of a long-running server.
            if key in _flow_cache and time.monotonic() - _flow_cached_at.get(key, 0) < (86400 if _flow_cache[key] else 3600):
                if _flow_cache[key]:
                    return _flow_cache[key], yr
                continue
        fetched = True
        try:
            rows = _imports_by_partner(reporter_comtrade, hs, yr)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                time.sleep(2.5)
                try:
                    rows = _imports_by_partner(reporter_comtrade, hs, yr)
                except Exception:
                    rows = []
                    fetched = False
            else:
                rows = []
                fetched = False
        except requests.RequestException:
            rows = []
            fetched = False
        # An outage is not evidence of no trade: do not poison this process's
        # cache with an empty response manufactured by an exception handler.
        if fetched:
            with _flow_lock:
                _flow_cache[key] = rows
                _flow_cached_at[key] = time.monotonic()
        if rows:
            return rows, yr
    return [], None


def _imports_by_partner(reporter_comtrade: int, hs: str, year: int) -> list[dict]:
    """Generalised Comtrade pull for any destination (mirrors connectors.py)."""
    url = ("https://comtradeapi.un.org/public/v1/preview/C/A/HS"
           f"?reporterCode={reporter_comtrade}&period={year}"
           f"&cmdCode={hs}&flowCode=M")
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    best: dict[int, dict] = {}
    for row in r.json().get("data", []):
        # Do not label a response with the requested year when the payload says
        # otherwise. Annual periods must exactly match the selected year.
        if str(row.get("period", "")) != str(year):
            continue
        pc = row.get("partnerCode")
        if not pc or row.get("partner2Code") not in (0, None):
            continue
        val = row.get("primaryValue") or 0
        if not val:
            continue
        # Supplementary qty may be pieces or another unit. Never call it kg.
        qty = row.get("netWgt") or (row.get("qty") if row.get("qtyUnitCode") == 8 else 0) or 0
        if pc in best and best[pc]["value_usd"] >= float(val):
            continue
        best[pc] = {
            "partner_code": pc,
            "partner": row.get("partnerDesc") or CO.comtrade_name(pc),
            "partner_iso": row.get("partnerISO"),
            "value_usd": float(val),
            "net_weight_kg": float(qty or 0),
            "unit_value_usd_per_t": (float(val) / (float(qty) / 1000)) if qty else None,
        }
    return sorted(best.values(), key=lambda x: -x["value_usd"])


def report(destination: str, hs6: str, year: int | None = None, top_n: int = 8) -> None:
    if year is None:
        year = dt.datetime.now(dt.timezone.utc).year
    dest = CO.get(destination)
    print(f"\nCHEAPEST SOURCE COUNTRIES  —  destination {dest.name} ({dest.iso3}), "
          f"HS {hs6}, trade year {year}")
    print("=" * 108)
    print(f"  {'origin':<16} {'proven t':>10} {'FOB $/t':>9} {'duty%':>7} "
          f"{'frt $/t':>8} {'LANDED $/t':>11} {'LPI':>5}  basis")
    print("  " + "-" * 104)
    rows = rank_source_countries(destination, hs6, year, top_n)
    if not rows:
        print(f"  No Comtrade import records found for {dest.iso3} on HS {hs6[:4]} "
              f"within {year-3}-{year}. This destination cannot be ranked from free data.")
        return
    for r in rows:
        landed = f"{r.landed_usd_per_t:,.0f}" if r.landed_usd_per_t else "n/a"
        duty = f"{r.duty_pct:.1f}" if r.duty_pct is not None else "n/a"
        fob = f"{r.fob_usd_per_t:,.0f}" if r.fob_usd_per_t else "n/a"
        lp = f"{r.lpi_score:.1f}" if r.lpi_score else "  -"
        flag = "" if r.duty_decision_grade else "  [indicative]"
        print(f"  {r.origin_name[:15]:<16} {r.proven_tonnes:>10,.0f} {fob:>9} {duty:>7} "
              f"{r.freight_usd_per_t:>8,.0f} {landed:>11} {lp:>5}  {r.duty_basis}{flag}")
        for s in r.surcharges:
            print(f"       + {s['kind']} {s['pct']}% in force to {s.get('to','?')}")

    if rows:
        best, worst = rows[0], rows[-1]
        if best.landed_usd_per_t and worst.landed_usd_per_t:
            gap = worst.landed_usd_per_t - best.landed_usd_per_t
            print(f"\n  Spread across ranked origins: ${gap:,.0f}/t "
                  f"({gap / best.landed_usd_per_t * 100:.1f}% of the cheapest)")
    print("\n  CAVEATS — read before acting:")
    print("   - FOB is a Comtrade unit-value average across all grades in the HS line.")
    print("     It is a screening signal. A real price comes from an RFQ.")
    print("   - Freight is a region-pair ESTIMATE. No free API prices arbitrary")
    print("     country pairs. Replace with forwarder quotes.")
    print("   - Preferential duty requires a valid certificate of origin.")
    print("   - This ranks COUNTRIES. No public dataset names an exporter.")


if __name__ == "__main__":
    report("IND", "720810")

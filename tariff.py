"""
tariff.py — Multi-country tariff resolution with an explicit trust hierarchy.

The scope change ("destination is a parameter, not India") makes this the
hardest module in the system, because there is no single authoritative,
current, machine-readable source of applied bilateral tariffs for arbitrary
country pairs. There is no such thing. Anyone who tells you otherwise has not
tried to query one.

What actually exists, tested live 2026-09-05:

  WITS / UNCTAD TRAINS  free, no API key, works for ANY destination.
                        MFN applied rates reach **2023** (3-year lag).
                        Preferential/FTA rates reach only **2021** (5-year lag)
                        -- the preference is the more stale half, so a recently
                        signed or lapsed FTA will not appear.
                        Verified MFN, HS 720810: India 7.5%, Vietnam 0%,
                        USA 0%, EU 0%, Thailand 5%, Turkiye 13%, Indonesia 15%.

  WTO                   NO API. api.wto.org does not exist; the Tariff Download
                        Facility and Tariff Analysis Online were both retired in
                        2025. The replacement (ttd.wto.org) needs a login and
                        restricts redistribution. Manual cross-check only.

  ITC MacMap            No API. Async email-triggered bulk export. Free only for
                        developing-country users; developed-country users must
                        subscribe. Useful for ad-valorem equivalents of specific
                        and compound duties, which WITS handles less well.

  Rules of origin       NO machine-readable global source at all. WITS gives you
                        the rate IF the good qualifies; nothing tells you whether
                        it does (regional value content, tariff shift, cumulation).
                        This stays a human expert step. See §"IMPORTANT LIMITS".

IMPORTANT LIMITS you must carry into any decision:
  1. WITS MFN does NOT include unilateral measures -- safeguards, anti-dumping,
     US Section 301, etc. Those go in SURCHARGES below and must be maintained by
     hand. A USA<-China result of 0% MFN is *correct as MFN* and *wrong as the
     duty actually payable*.
  2. A preferential rate is only claimable with a valid certificate of origin.
     The system reports the rate; it cannot certify eligibility.
  3. Everything here is ad valorem. Specific/compound duties need MacMap AVEs.

So the design is a **trust hierarchy with mandatory staleness disclosure**, not
a lookup. A tariff the system cannot stand behind is reported as unverified and
blocks a confident recommendation. That behaviour is the feature.
"""

from __future__ import annotations

import re
import datetime as dt
import threading
from dataclasses import dataclass, field

import requests

import countries as CO

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125", "Accept": "*/*"}
WITS = "https://wits.worldbank.org/API/V1/SDMX/V21/datasource/TRN"


# --------------------------------------------------------------------------
# Trust levels
# --------------------------------------------------------------------------
TRUST = {
    "verified_official": 0,   # a human checked the destination's own tariff schedule
    "wits_bilateral": 1,      # WITS TRAINS, this exact origin-destination pair
    "wits_mfn": 2,            # WITS MFN applied, no preference assumed
    "unknown": 9,             # nothing found -> must block confident output
}


@dataclass
class TariffResult:
    destination: str
    origin: str
    hs6: str
    ad_valorem_pct: float | None
    source: str
    trust: str
    as_of_year: int | None
    staleness_years: int | None
    notes: list[str] = field(default_factory=list)

    @property
    def usable_for_decision(self) -> bool:
        """A tariff older than 3 years or of unknown provenance must not drive
        a confident sourcing recommendation."""
        if self.ad_valorem_pct is None or self.trust == "unknown":
            return False
        if self.trust == "verified_official":
            return True
        return (self.staleness_years or 99) <= 3

    def explain(self) -> str:
        if self.ad_valorem_pct is None:
            return f"{self.destination} <- {self.origin} HS{self.hs6}: NO TARIFF DATA"
        age = f", {self.staleness_years}y stale" if self.staleness_years else ""
        flag = "" if self.usable_for_decision else "  [NOT DECISION-GRADE]"
        return (f"{self.destination} <- {self.origin} HS{self.hs6}: "
                f"{self.ad_valorem_pct:.2f}%  ({self.source}, {self.as_of_year}{age}){flag}")


# --------------------------------------------------------------------------
# Layer 0 — human-verified overrides. Highest trust. Effective-dated.
# --------------------------------------------------------------------------
# Populate this from the DESTINATION country's own customs schedule, checked by
# a customs professional. This is the only layer that should ever drive a
# confident import recommendation.
VERIFIED_OVERRIDES: dict[tuple[str, str, str], dict] = {
    # (destination_iso3, origin_iso3 or "*", hs6): {...}
    ("IND", "*", "720810"): {
        "ad_valorem_pct": 7.5,
        "effective_from": "2026-04-01",
        "note": ("BCD. Secondary aggregators split 7.5% vs 15%, but WITS/TRAINS "
                 "now corroborates 7.5% for 2021, 2022 AND 2023 (down from 12.5% "
                 "in 2019-20). Three consecutive years of official UNCTAD data "
                 "make 7.5% the strongly-supported figure. Still requires a "
                 "customs professional to confirm against the CBIC First "
                 "Schedule before it drives a purchase."),
        "verified": False,          # flip to True only after customs sign-off
    },
    ("IND", "*", "790111"): {
        "ad_valorem_pct": 5.0,
        "effective_from": "2026-04-01",
        "note": ("Unwrought zinc BCD 5%, corroborated by WITS/TRAINS MFN 5.0% "
                 "for 2019-2023. AIDC 5% stacks on top (see SURCHARGES). "
                 "Refined Zinc QCO 2025 reported rescinded — verify against the "
                 "Gazette of India."),
        "verified": False,
    },
}

# Additional non-tariff duties that stack on top (safeguards, AD, cesses).
# Keyed the same way. These are the ones that actually move sourcing decisions.
SURCHARGES: dict[tuple[str, str, str], list[dict]] = {
    ("IND", "*", "720810"): [
        {"kind": "safeguard", "pct": 11.5, "from": "2026-04-21", "to": "2027-04-20",
         "note": "DGTR steel safeguard, yr 2 of 12/11.5/11% step-down, HS 7208-7212 & 7225-7226"},
    ],
    ("IND", "*", "790111"): [
        {"kind": "AIDC", "pct": 5.0, "from": "2021-02-01", "to": "9999-12-31",
         "note": "Agriculture Infrastructure & Development Cess on unwrought zinc"},
    ],
}


# --------------------------------------------------------------------------
# Layer 1/2 — WITS TRAINS
# --------------------------------------------------------------------------
class WitsUnavailable(RuntimeError):
    """WITS could not be reached. Distinct from 'WITS has no record'."""


_wits_cache: dict[tuple, TariffResult] = {}
_series_cache: dict[tuple, list] = {}
_tariff_lock = threading.Lock()

_OBS_RE = re.compile(r"<generic:Obs>(.*?)</generic:Obs>", re.S)


@dataclass
class TariffObs:
    year: int
    rate: float
    tariff_type: str      # MFN | PREF
    min_rate: float | None = None
    max_rate: float | None = None
    n_lines: int | None = None


def wits_series(reporter_num: int, partner_num: int, hs6: str,
                start: int = 2015, end: int = 2026) -> list[TariffObs]:
    """
    Query the WITS SDMX **REST** endpoint and parse the generic-format response.

    IMPORTANT — this is the correct base path:
        https://wits.worldbank.org/API/V1/SDMX/V21/rest/data/DF_WITS_Tariff_TRAINS
                 /A.{reporter}.{partner}.{hs6}.Reported/?startPeriod=&endPeriod=

    The older /datasource/reporter/.../partner/... form also responds, but it
    does NOT serve MFN (partner 000) and tops out ~2021. The REST path serves
    both MFN and preferential and reaches 2023. Verified 2026-09-05.

    partner_num 0 (sent as "000") = MFN applied rate.
    Codes are ISO-3166 numeric, NOT Comtrade codes. India is 356 here, 699 in
    Comtrade. See countries.py.

    Returns observations oldest-first. Empty list means WITS has no record —
    which is itself meaningful, not an error.
    """
    key = (reporter_num, partner_num, hs6, start, end)
    with _tariff_lock:
        if key in _series_cache:
            return _series_cache[key]

    skey = f"A.{reporter_num}.{partner_num:03d}.{hs6}.Reported"
    url = (f"https://wits.worldbank.org/API/V1/SDMX/V21/rest/data/"
           f"DF_WITS_Tariff_TRAINS/{skey}/?startPeriod={start}&endPeriod={end}")
    out: list[TariffObs] = []
    reachable = True
    try:
        r = requests.get(url, headers=UA, timeout=90)
        if r.status_code >= 500 or r.status_code in (403, 408, 429):
            reachable = False
        if r.status_code == 200:
            for block in _OBS_RE.findall(r.text):
                yr = re.search(r'id="TIME_PERIOD" value="(\d+)"', block)
                val = re.search(r'<generic:ObsValue value="([\d.]+)"', block)
                if not (yr and val):
                    continue
                tt = re.search(r'id="TARIFFTYPE" value="(\w+)"', block)
                mn = re.search(r'id="MIN_RATE" value="([\d.]+)"', block)
                mx = re.search(r'id="MAX_RATE" value="([\d.]+)"', block)
                nl = re.search(r'id="TOTALNOOFLINES" value="(\d+)"', block)
                out.append(TariffObs(
                    int(yr.group(1)), float(val.group(1)),
                    tt.group(1) if tt else "?",
                    float(mn.group(1)) if mn else None,
                    float(mx.group(1)) if mx else None,
                    int(nl.group(1)) if nl else None,
                ))
    except requests.RequestException:
        reachable = False

    if not reachable:
        # CRITICAL: an unreachable source is NOT the same as "no record exists".
        # Returning [] here would silently convert "Korea is duty-free under
        # CEPA" into "Korea pays MFN" during a network outage. Raise instead —
        # the caller degrades the result to unknown and says so out loud.
        raise WitsUnavailable(
            f"WITS unreachable for reporter={reporter_num} partner={partner_num} "
            f"hs={hs6}. Cannot distinguish 'no preferential agreement' from "
            f"'could not check'.")

    out.sort(key=lambda o: o.year)
    with _tariff_lock:
        _series_cache[key] = out
    return out


def mfn_rate(destination: str, hs6: str) -> tuple[float | None, int | None, str, bool]:
    """
    The destination's MFN applied rate — what an origin with NO trade
    agreement pays. Works for ANY destination. Returns (rate, year, note, verified).

    Source order: human-verified override, then WITS TRAINS (partner 000).

    Verified 2026-09-05, HS 720810 MFN by destination:
        India 7.5% (2021-2023, down from 12.5% in 2019-20)
        Vietnam 0%   USA 0%   EU 0%   Thailand 5%   Turkiye 13%   Indonesia 15%
    """
    dest = CO.get(destination)
    if dest is None:
        return None, None, "unknown destination", False

    ov = VERIFIED_OVERRIDES.get((dest.iso3, "*", hs6))
    if ov and ov.get("verified"):
        return ov["ad_valorem_pct"], int(ov["effective_from"][:4]), ov.get("note", ""), True

    try:
        obs = [o for o in wits_series(dest.iso_num, 0, hs6) if o.tariff_type == "MFN"]
    except WitsUnavailable:
        if ov:
            return (ov["ad_valorem_pct"], int(ov["effective_from"][:4]),
                    "WITS UNREACHABLE — falling back to an unverified local override. "
                    "DO NOT act on this. " + ov.get("note", ""), False)
        return None, None, "WITS UNREACHABLE and no local override on file.", False
    if obs:
        last = obs[-1]
        note = f"WITS/TRAINS MFN applied, {last.year}."
        if last.min_rate is not None and last.max_rate is not None and last.min_rate != last.max_rate:
            note += f" Range across tariff lines {last.min_rate}-{last.max_rate}%."
        if ov:
            note += (f" (Unverified local override on file says "
                     f"{ov['ad_valorem_pct']}% — reconcile.)")
        return last.rate, last.year, note, False

    if ov:
        return (ov["ad_valorem_pct"], int(ov["effective_from"][:4]),
                ov.get("note", "") + " [unverified local override; WITS has no record]", False)
    return None, None, ("No MFN rate on file and WITS has no record. A human must supply "
                        "it from the destination's tariff schedule."), False


def preferential_rate(destination: str, origin: str, hs6: str
                      ) -> tuple[float | None, int | None]:
    """
    The preferential (FTA) rate this origin enjoys, from WITS TRAINS.

    KEY INTERPRETATION, verified live 2026-09-05: WITS tags each observation
    MFN or PREF. A PREF record for a country pair means a trade agreement
    covers that line. Observed for HS 720810 into India:

        India <- Korea      0.0% PREF (India-Korea CEPA)     2018-2021
        India <- Japan      0.0% PREF (India-Japan CEPA)     2018-2021
        India <- Sri Lanka  0.0% PREF (ISFTA/SAFTA)          2018-2021
        India <- China      no PREF record -> MFN 7.5% applies

    **Absence of a record is itself informative**: no preference, pay MFN.
    That is exactly the signal country ranking needs.

    NOTE the asymmetry: MFN data reaches 2023, preferential only 2021. The
    preference is the more stale half, so a recently-signed or recently-lapsed
    FTA will not show here. Cross-check the WTO RTA list for agreements in
    force: https://rtais.wto.org/UI/ExportAllRTAList.aspx (free XLSX, no auth).
    """
    dest, orig = CO.get(destination), CO.get(origin)
    if dest is None or orig is None:
        return None, None
    obs = [o for o in wits_series(dest.iso_num, orig.iso_num, hs6)
           if o.tariff_type == "PREF"]
    if obs:
        return obs[-1].rate, obs[-1].year
    return None, None   # genuinely no preferential record -> MFN applies


def resolve_tariff(destination: str, origin: str, hs6: str,
                   try_years: int = 4) -> TariffResult:
    """
    Resolve the ad-valorem import tariff `destination` applies to `origin`.

    Takes the lower of (MFN, preferential-if-any) and records which applied,
    so the caller can see the preference margin — the number that actually
    decides "which country is cheapest to buy from".

    Always returns a result. Check `.usable_for_decision` before acting.
    """
    key = (destination, origin, hs6)
    with _tariff_lock:
        if key in _wits_cache:
            return _wits_cache[key]

    dest, orig = CO.get(destination), CO.get(origin)
    this_year = dt.date.today().year

    if dest is None or orig is None:
        res = TariffResult(destination, origin, hs6, None, "unknown country code",
                           "unknown", None, None,
                           [f"Unknown country: {destination if dest is None else origin}"])
        with _tariff_lock:
            _wits_cache[key] = res
        return res

    mfn, mfn_year, mfn_note, mfn_verified = mfn_rate(dest.iso3, hs6)
    pref_unknown = False
    try:
        pref, pref_year = preferential_rate(dest.iso3, orig.iso3, hs6)
    except WitsUnavailable:
        pref, pref_year, pref_unknown = None, None, True
    notes: list[str] = []
    if pref_unknown:
        notes.append("WITS UNREACHABLE — could not check for a preferential rate. "
                     "This origin may enjoy an FTA preference that is NOT reflected "
                     "below. Treat the figure as an upper bound, not a rate.")
    if mfn_note:
        notes.append(mfn_note)

    # --- pick the applicable rate ---
    if pref is not None and (mfn is None or pref <= mfn):
        margin = (mfn - pref) if mfn is not None else None
        notes.append(
            f"PREFERENTIAL rate applies (WITS/TRAINS bilateral record, {pref_year})."
            + (f" Preference margin vs MFN: {margin:.2f} pp." if margin is not None else "")
            + " Requires a valid certificate of origin to claim."
        )
        res = TariffResult(dest.iso3, orig.iso3, hs6, pref,
                           "WITS/TRAINS preferential", "wits_bilateral",
                           pref_year, this_year - (pref_year or this_year), notes)
    elif mfn is not None:
        notes.append("No preferential record found in WITS -> MFN applies "
                     "(no trade agreement, or the agreement excludes this line).")
        if pref_unknown:
            src_lbl, trust_lbl = "MFN, preference UNCHECKED", "unknown"
        elif mfn_verified:
            src_lbl, trust_lbl = "MFN (verified override)", "verified_official"
        else:
            src_lbl, trust_lbl = "WITS/TRAINS MFN", "wits_mfn"
        res = TariffResult(dest.iso3, orig.iso3, hs6, mfn, src_lbl, trust_lbl,
                           mfn_year, this_year - (mfn_year or this_year), notes)
    else:
        notes.append("No tariff found in any layer. A human must supply this from "
                     "the destination's customs schedule.")
        res = TariffResult(dest.iso3, orig.iso3, hs6, None, "none", "unknown",
                           None, None, notes)

    with _tariff_lock:
        _wits_cache[key] = res
    return res


def resolve_surcharges(destination: str, origin: str, hs6: str,
                       on_date: str | None = None) -> list[dict]:
    """Safeguards / anti-dumping / cesses in force on `on_date`."""
    d = on_date or dt.date.today().isoformat()
    out = []
    for k in ((destination, origin, hs6), (destination, "*", hs6)):
        for s in SURCHARGES.get(k, []):
            if s.get("from", "0000") <= d <= s.get("to", "9999"):
                out.append(s)
    return out


def total_duty_pct(destination: str, origin: str, hs6: str) -> tuple[float | None, TariffResult, list[dict]]:
    """Headline tariff + surcharges, as a single ad-valorem percentage."""
    t = resolve_tariff(destination, origin, hs6)
    sc = resolve_surcharges(destination, origin, hs6)
    if t.ad_valorem_pct is None:
        return None, t, sc
    return t.ad_valorem_pct + sum(s["pct"] for s in sc), t, sc


if __name__ == "__main__":
    print("Multi-country tariff resolution — live WITS, no API key\n")
    tests = [
        ("IND", "KOR", "720810"), ("IND", "CHN", "720810"), ("IND", "JPN", "720810"),
        ("VNM", "CHN", "720810"), ("DEU", "KOR", "720810"), ("USA", "CHN", "720810"),
        ("IND", "KOR", "790111"), ("THA", "KOR", "720810"),
    ]
    for dest, orig, hs in tests:
        tot, t, sc = total_duty_pct(dest, orig, hs)
        print("  " + t.explain())
        for s in sc:
            print(f"      + {s['kind']} {s['pct']}%  ({s['note'][:60]})")
        if tot is not None:
            print(f"      = TOTAL {tot:.2f}%   decision-grade: {t.usable_for_decision}")
        for n in t.notes:
            if n:
                print(f"      ! {n}")
        print()

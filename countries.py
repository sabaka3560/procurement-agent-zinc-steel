"""
countries.py — One country registry, three incompatible code systems.

This module exists because of a trap that will silently corrupt any
multi-country procurement system:

    India is 356 in ISO-3166 / WITS
    India is 699 in UN Comtrade
    India is IND in ISO alpha-3

Comtrade uses M49 for *most* countries but overrides a handful for historical
reasons (India, USA incl. Puerto Rico, France incl. Monaco, Switzerland incl.
Liechtenstein, Italy incl. San Marino, Norway, "Other Asia, nes" for Taiwan).
Query WITS with a Comtrade code and you get HTTP 404 "NoRecordsFound" — not an
error you would notice, just a country that silently disappears from your
sourcing analysis.

Verified against live WITS and Comtrade responses, 2026-09-05.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Country:
    iso3: str
    name: str
    iso_num: int          # ISO-3166 numeric == WITS country code
    comtrade: int         # UN Comtrade reporter/partner code (often but NOT always == iso_num)
    region: str = ""

    @property
    def codes_differ(self) -> bool:
        return self.iso_num != self.comtrade


# Steel- and zinc-relevant sourcing and destination countries.
# `comtrade` differs from `iso_num` only where UN Comtrade overrides M49.
_C = [
    Country("IND", "India",            356, 699, "South Asia"),
    Country("KOR", "Korea, Rep.",      410, 410, "East Asia"),
    Country("JPN", "Japan",            392, 392, "East Asia"),
    Country("CHN", "China",            156, 156, "East Asia"),
    Country("VNM", "Viet Nam",         704, 704, "Southeast Asia"),
    Country("IDN", "Indonesia",        360, 360, "Southeast Asia"),
    Country("THA", "Thailand",         764, 764, "Southeast Asia"),
    Country("MYS", "Malaysia",         458, 458, "Southeast Asia"),
    Country("TWN", "Taiwan",           158, 490, "East Asia"),      # Comtrade: "Other Asia, nes"
    Country("USA", "United States",    840, 842, "North America"),  # Comtrade incl. Puerto Rico
    Country("DEU", "Germany",          276, 276, "Europe"),
    Country("ITA", "Italy",            380, 381, "Europe"),         # Comtrade incl. San Marino
    Country("FRA", "France",           250, 251, "Europe"),         # Comtrade incl. Monaco
    Country("CHE", "Switzerland",      756, 757, "Europe"),         # Comtrade incl. Liechtenstein
    Country("NOR", "Norway",           578, 579, "Europe"),
    Country("BEL", "Belgium",           56,  56, "Europe"),
    Country("NLD", "Netherlands",      528, 528, "Europe"),
    Country("ESP", "Spain",            724, 724, "Europe"),
    Country("GBR", "United Kingdom",   826, 826, "Europe"),
    Country("TUR", "Turkiye",          792, 792, "Middle East"),
    Country("ARE", "UAE",              784, 784, "Middle East"),
    Country("SAU", "Saudi Arabia",     682, 682, "Middle East"),
    Country("OMN", "Oman",             512, 512, "Middle East"),
    Country("QAT", "Qatar",            634, 634, "Middle East"),
    Country("RUS", "Russia",           643, 643, "Europe/CIS"),
    Country("UKR", "Ukraine",          804, 804, "Europe/CIS"),
    Country("KAZ", "Kazakhstan",       398, 398, "Central Asia"),
    Country("BRA", "Brazil",            76,  76, "South America"),
    Country("MEX", "Mexico",           484, 484, "North America"),
    Country("ZAF", "South Africa",     710, 710, "Africa"),
    Country("EGY", "Egypt",            818, 818, "Africa"),
    Country("AUS", "Australia",         36,  36, "Oceania"),
    Country("CAN", "Canada",           124, 124, "North America"),
    Country("POL", "Poland",           616, 616, "Europe"),
    Country("AUT", "Austria",           40,  40, "Europe"),
    Country("SWE", "Sweden",           752, 752, "Europe"),
    Country("FIN", "Finland",          246, 246, "Europe"),
    Country("SGP", "Singapore",        702, 702, "Southeast Asia"),
    Country("BHR", "Bahrain",           48,  48, "Middle East"),
    Country("PHL", "Philippines",      608, 608, "Southeast Asia"),
    Country("BGD", "Bangladesh",        50,  50, "South Asia"),
    Country("LKA", "Sri Lanka",        144, 144, "South Asia"),
]

BY_ISO3 = {c.iso3: c for c in _C}
BY_ISO_NUM = {c.iso_num: c for c in _C}
BY_COMTRADE = {c.comtrade: c for c in _C}


def get(key: str | int) -> Country | None:
    """Look up by ISO3 ('KOR'), ISO numeric (410) or country name."""
    if isinstance(key, int):
        return BY_ISO_NUM.get(key)
    k = str(key).strip()
    if k.upper() in BY_ISO3:
        return BY_ISO3[k.upper()]
    if k.isdigit():
        return BY_ISO_NUM.get(int(k))
    for c in _C:
        if c.name.lower() == k.lower():
            return c
    return None


def comtrade_name(code: int) -> str:
    c = BY_COMTRADE.get(code)
    return c.name if c else f"M49:{code}"


def divergent_codes() -> list[Country]:
    """The countries where Comtrade and WITS disagree. Test against these."""
    return [c for c in _C if c.codes_differ]


if __name__ == "__main__":
    print("Countries whose Comtrade code differs from their ISO/WITS code")
    print("(query the wrong system with the wrong code and they vanish silently)\n")
    print(f"  {'iso3':<6} {'name':<18} {'ISO/WITS':>9} {'Comtrade':>9}")
    print("  " + "-" * 46)
    for c in divergent_codes():
        print(f"  {c.iso3:<6} {c.name:<18} {c.iso_num:>9} {c.comtrade:>9}")
    print(f"\n  {len(_C)} countries registered, {len(divergent_codes())} with divergent codes")

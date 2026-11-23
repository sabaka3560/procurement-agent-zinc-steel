"""
connectors.py — Data connectors that were LIVE-TESTED on 2026-09-04.

Every function here hits a real endpoint that returned HTTP 200 during the
engagement. Nothing in this file is aspirational. Sources that were tested and
FAILED are documented in FAILED_SOURCES below so nobody wastes a day on them.

Zero API keys required. This is the "day one, rupees zero" tier.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from dataclasses import dataclass, field
from functools import wraps

import requests


# --------------------------------------------------------------------------
# Thread-safe TTL cache — prevents external API rate-limiting under concurrency.
# When 10 threads call india_hrc_nexarc() simultaneously, only the first one
# makes the HTTP request; the rest reuse the cached result for `ttl` seconds.
# --------------------------------------------------------------------------
_cache_lock = threading.Lock()
_cache_store: dict[str, tuple[float, object]] = {}


def ttl_cache(ttl: int = 60):
    """Decorator: cache the return value for `ttl` seconds, thread-safe."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = f"{fn.__name__}:{args}:{sorted(kwargs.items())}"
            now = time.monotonic()
            with _cache_lock:
                if key in _cache_store:
                    ts, val = _cache_store[key]
                    if now - ts < ttl:
                        return val
            # Call outside the lock so we don't block other cache lookups
            result = fn(*args, **kwargs)
            with _cache_lock:
                _cache_store[key] = (time.monotonic(), result)
            return result
        return wrapper
    return decorator

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}

TIMEOUT = 30


# --------------------------------------------------------------------------
# Sources confirmed BROKEN / BLOCKED on 2026-09-04 — do not retry these.
# --------------------------------------------------------------------------
FAILED_SOURCES = {
    "mcxindia.com": "HTTP 403 (Akamai edge block) on both /market-data/bhavcopy "
                    "and the backpage.aspx JSON endpoint. Naive HTTP clients are "
                    "refused. Needs a headless browser or a licensed feed.",
    "lme.com": "HTTP 403 (Cloudflare interstitial). No scraping path.",
    "stooq.com": "JavaScript browser-verification challenge on the CSV endpoint.",
    "query1.finance.yahoo.com/v7/finance/quote": "HTTP 401 — the v7 quote endpoint "
                    "now requires auth. The v8 chart endpoint (used below) is still open.",
    "dataservices.imf.org": "Connection refused. IMF moved to api.imf.org, whose "
                    "SDMX 2.1 path returns a 400 serialization error for PCPS.",
    "jpcindiansteel.nic.in": "ECONNREFUSED from outside India — appears geo-fenced. "
                    "Needs an India-region worker.",
    "exchangerate.host": "302-redirects to apilayer.com; no longer a free standalone API.",
}


@dataclass
class Observation:
    """One canonical price/rate observation. Mirrors the schema in doc (2)/(3)."""
    series: str
    value: float
    unit: str
    currency: str
    observed_at: str          # the timestamp the SOURCE attributes to the value
    retrieved_at: str         # when WE pulled it
    source: str
    quality: str = "ok"       # ok | stale | proxy
    note: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def age_days(self) -> float:
        try:
            o = dt.datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
            r = dt.datetime.fromisoformat(self.retrieved_at.replace("Z", "+00:00"))
            return (r - o).total_seconds() / 86400
        except Exception:
            return float("nan")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 1. FX — FBIL (India's official INR reference rate) and ECB, both free, no key
# --------------------------------------------------------------------------
@ttl_cache(ttl=60)
def fx_usd_inr_fbil() -> Observation:
    """
    India's OFFICIAL USD/INR reference rate, computed and published by
    Financial Benchmarks India Ltd. Mirrored key-free by frankfurter.dev.

    This is the rate an Indian auditor will expect to see in a landed-cost
    calculation. Note it is published once per business day and the mirror
    can lag by a few days -- always check .age_days before trusting it.
    """
    r = requests.get(
        "https://api.frankfurter.dev/v2/rate/USD/INR?providers=FBIL",
        headers=UA, timeout=TIMEOUT,
    )
    r.raise_for_status()
    d = r.json()
    obs = Observation(
        series="FX.USDINR.FBIL",
        value=float(d["rate"]),
        unit="INR per USD",
        currency="INR",
        observed_at=f"{d['date']}T00:00:00+00:00",
        retrieved_at=_now(),
        source="FBIL via frankfurter.dev (unofficial mirror of the official benchmark)",
        raw=d,
    )
    if obs.age_days > 3:
        obs.quality = "stale"
        obs.note = f"FBIL mirror is {obs.age_days:.1f} days behind. Cross-check fbil.org.in."
    return obs


@ttl_cache(ttl=60)
def fx_usd_to(currency: str) -> Observation:
    """
    USD -> any ECB-published currency, free and keyless.

    Covers 30 currencies. Verified 2026-09-05 that it does NOT cover
    VND, AED, SAR, TWD, EGP or RUB — several of which are real steel-sourcing
    destinations. Raises rather than substituting a wrong rate; see
    destinations.py for which destinations this leaves at USD only.
    """
    cur = currency.upper()
    if cur == "USD":
        return Observation("FX.USDUSD", 1.0, "USD per USD", "USD",
                           _now(), _now(), "identity")
    r = requests.get(f"https://api.frankfurter.app/latest?from=USD&to={cur}",
                     headers=UA, timeout=TIMEOUT)
    # An unpublished currency comes back as HTTP 404, not a 200 with a gap.
    # Both paths must raise the SAME error, or callers will treat "we don't
    # publish this currency" as a transient network fault and retry forever.
    if r.status_code == 404:
        raise ValueError(
            f"No free FX feed for USD/{cur}. frankfurter (ECB) does not publish it. "
            f"Price this destination in USD, or licence a commercial FX source.")
    r.raise_for_status()
    d = r.json()
    if cur not in d.get("rates", {}):
        raise ValueError(
            f"No free FX feed for USD/{cur}. frankfurter (ECB) does not publish it. "
            f"Price this destination in USD, or licence a commercial FX source.")
    return Observation(
        series=f"FX.USD{cur}.ECB",
        value=float(d["rates"][cur]),
        unit=f"{cur} per USD",
        currency=cur,
        observed_at=f"{d['date']}T00:00:00+00:00",
        retrieved_at=_now(),
        source="ECB via frankfurter.app",
        raw=d,
    )


@ttl_cache(ttl=60)
def fx_usd_inr_ecb() -> Observation:
    """ECB-sourced USD/INR. Fresher than the FBIL mirror; use as a cross-check."""
    r = requests.get(
        "https://api.frankfurter.app/latest?from=USD&to=INR",
        headers=UA, timeout=TIMEOUT,
    )
    r.raise_for_status()
    d = r.json()
    return Observation(
        series="FX.USDINR.ECB",
        value=float(d["rates"]["INR"]),
        unit="INR per USD",
        currency="INR",
        observed_at=f"{d['date']}T00:00:00+00:00",
        retrieved_at=_now(),
        source="ECB via frankfurter.app",
        quality="proxy",
        note="Cross-check only. Indian audit expects FBIL.",
        raw=d,
    )


# --------------------------------------------------------------------------
# 2. Futures via Yahoo's v8 chart endpoint (free, no key, still open)
# --------------------------------------------------------------------------
@ttl_cache(ttl=60)
def yahoo_series(ticker: str, series: str, unit: str, rng: str = "2y") -> tuple[Observation, list]:
    """
    Returns (latest Observation, [(date, close), ...]).

    Tickers verified live on 2026-09-04:
      HRC=F   CME hot-rolled-coil steel futures, USD/short ton -- LIVE, dense
      HG=F    COMEX copper, USD/lb -- LIVE (useful macro correlate)
      SLX     VanEck Steel ETF -- LIVE (equity sentiment correlate)
      ZNC=F   COMEX zinc -- *** 15 DAYS STALE, DO NOT USE AS A ZINC BENCHMARK ***
    """
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?range={rng}&interval=1d")
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    meta = res["meta"]

    ts = res.get("timestamp", []) or []
    closes = res["indicators"]["quote"][0]["close"]
    hist = [
        (dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat(), float(c))
        for t, c in zip(ts, closes) if c is not None
    ]

    observed = dt.datetime.fromtimestamp(meta["regularMarketTime"], dt.timezone.utc)
    obs = Observation(
        series=series,
        value=float(meta["regularMarketPrice"]),
        unit=unit,
        currency=meta.get("currency", "USD"),
        observed_at=observed.isoformat(timespec="seconds"),
        retrieved_at=_now(),
        source=f"Yahoo Finance v8 chart / {meta.get('fullExchangeName', '?')} [{ticker}]",
        raw={"points": len(hist)},
    )
    if obs.age_days > 3:
        obs.quality = "stale"
        obs.note = (f"{obs.age_days:.1f} days stale — the contract is illiquid. "
                    f"Do NOT use as a live benchmark.")
    return obs, hist


def steel_hrc_futures(rng: str = "2y"):
    """CME HRC steel futures. The only free, live, dense steel price found."""
    return yahoo_series("HRC=F", "STEEL.HRC.CME", "USD/short ton", rng)


def zinc_comex_futures(rng: str = "2y"):
    """
    COMEX zinc. INCLUDED AS A NEGATIVE CONTROL, NOT A RECOMMENDATION.

    On 2026-09-04 this returned $4,050/t last traded 2026-08-20 (15 days stale)
    while the real LME-tracking zinc price was ~$3,929/t. A 3% error on a
    2,000 t/yr zinc spend is roughly $240k of phantom savings. This is the
    exact trap that makes "just use a free API" fail for zinc.
    """
    return yahoo_series("ZNC=F", "ZINC.COMEX", "USD/tonne", rng)


def copper_futures(rng: str = "2y"):
    """COMEX copper — liquid, live, and correlated with zinc. Useful as a nowcast
    covariate when your zinc feed is delayed or end-of-day only."""
    return yahoo_series("HG=F", "COPPER.COMEX", "USD/lb", rng)


# --------------------------------------------------------------------------
# 3. UN Comtrade — free, no key. Which countries actually supply India?
# --------------------------------------------------------------------------
@ttl_cache(ttl=60)
def india_imports_by_partner(hs_code: str, year: int) -> list[dict]:
    """
    India's imports of `hs_code` for `year`, broken down by partner country.

    Free 'preview' endpoint, no subscription key, capped at 500 records.
    reporterCode 699 = India.

    Verified 2026-09-04: HS 7208 (HRC) year 2024 returned 127 partner rows;
    HS 7901 (unwrought zinc) year 2023 returned 98 rows.

    NOTE: this gives country-level flows only. DGCI&S explicitly does not
    publish importer/exporter identity, so this CANNOT name a vendor. It
    screens countries; humans and RFQs find vendors.
    """
    url = ("https://comtradeapi.un.org/public/v1/preview/C/A/HS"
           f"?reporterCode=699&period={year}&cmdCode={hs_code}&flowCode=M")
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    rows = r.json().get("data", [])

    # The preview endpoint returns partnerDesc=null and emits several rows per
    # partner (one per partner2Code / customsCode / motCode combination). Keep
    # only the fully-aggregated row, drop partnerCode 0 (= World), and resolve
    # the UN M49 numeric code ourselves.
    best: dict[int, dict] = {}
    for row in rows:
        pc = row.get("partnerCode")
        if not pc:                                    # 0 == World aggregate
            continue
        if row.get("partner2Code") not in (0, None):  # keep the aggregate only
            continue
        val = row.get("primaryValue") or 0
        if not val:
            continue
        qty = row.get("netWgt") or row.get("qty") or 0
        if pc in best and best[pc]["value_usd"] >= float(val):
            continue
        best[pc] = {
            "partner_code": pc,
            "partner": M49.get(int(pc), f"M49:{pc}"),
            "value_usd": float(val),
            "net_weight_kg": float(qty or 0),
            "unit_value_usd_per_t": (float(val) / (float(qty) / 1000)) if qty else None,
        }
    out = sorted(best.values(), key=lambda x: -x["value_usd"])
    return out


# UN M49 numeric country codes, limited to the partners that actually matter
# for Indian steel and zinc sourcing. Extend as needed.
M49 = {
    4: "Afghanistan", 36: "Australia", 48: "Bahrain", 56: "Belgium", 76: "Brazil",
    124: "Canada", 152: "Chile", 156: "China", 203: "Czechia", 276: "Germany",
    288: "Ghana", 300: "Greece", 344: "Hong Kong", 360: "Indonesia", 364: "Iran",
    368: "Iraq", 380: "Italy", 392: "Japan", 398: "Kazakhstan", 410: "Rep. of Korea",
    414: "Kuwait", 458: "Malaysia", 484: "Mexico", 490: "Other Asia, nes (Taiwan)",
    504: "Morocco", 512: "Oman", 528: "Netherlands", 566: "Nigeria", 586: "Pakistan",
    608: "Philippines", 616: "Poland", 620: "Portugal", 634: "Qatar", 642: "Romania",
    643: "Russia", 682: "Saudi Arabia", 702: "Singapore", 704: "Viet Nam",
    710: "South Africa", 724: "Spain", 752: "Sweden", 756: "Switzerland",
    764: "Thailand", 784: "UAE", 792: "Turkiye", 804: "Ukraine",
    826: "United Kingdom", 840: "USA", 860: "Uzbekistan",
}


# --------------------------------------------------------------------------
# 4. LME ZINC — free, via westmetall.com's public settlement table
#    THIS IS THE HEADLINE FINDING OF THE ENGAGEMENT.
# --------------------------------------------------------------------------
_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


@ttl_cache(ttl=60)
def lme_zinc_westmetall() -> tuple[Observation, Observation, Observation, list]:
    """
    LME Zinc Cash-Settlement, 3-month, and LME warehouse stock — FREE.

    All four research reports concluded that LME zinc required a licensed feed
    (LMEsource at $10-25k/yr) or a paid vendor. That is true for *real-time*
    data. It is NOT true for the official daily settlement series, which
    westmetall.com republishes as a plain HTML table with history.

    Verified live 2026-09-04. Returned, for 2026-09-03:
        cash = $3,995.50/t   3-month = $3,868.00/t   stock = 110,500 t

    Returns (cash_obs, three_month_obs, stock_obs, history)
    where history = [(iso_date, cash, m3, stock), ...] newest first.

    IMPORTANT LICENSING NOTE: westmetall is a third-party republisher, not the
    LME. For internal decision support this is normally fine, but if you
    redistribute these numbers, or embed them in a product you sell, get the
    LME's derived-data position in writing first. See Deliverable 03 s5.
    """
    import re
    url = ("https://www.westmetall.com/en/markdaten.php"
           "?action=table&field=LME_Zn_cash")
    r = requests.get(url, headers={**UA, "Accept": "text/html,*/*"}, timeout=TIMEOUT)
    r.raise_for_status()

    text = re.sub(r"<[^>]+>", " ", r.text)
    text = re.sub(r"\s+", " ", text)

    pat = re.compile(
        r"(\d{1,2})\.\s*([A-Z][a-z]+)\s*(20\d\d)\s+"
        r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)"
    )
    hist = []
    for day, mon, yr, cash, m3, stock in pat.findall(text):
        if mon not in _MONTHS:
            continue
        try:
            iso = f"{int(yr):04d}-{_MONTHS[mon]:02d}-{int(day):02d}"
            hist.append((
                iso,
                float(cash.replace(",", "")),
                float(m3.replace(",", "")),
                float(stock.replace(",", "")),
            ))
        except ValueError:
            continue

    if not hist:
        raise RuntimeError("westmetall table layout changed — parser needs updating")

    # de-duplicate, newest first
    seen, clean = set(), []
    for row in hist:
        if row[0] not in seen:
            seen.add(row[0])
            clean.append(row)
    clean.sort(key=lambda x: x[0], reverse=True)

    d, cash, m3, stock = clean[0]
    at = f"{d}T00:00:00+00:00"
    src = "LME via westmetall.com (free republication of official settlement)"
    return (
        Observation("ZINC.LME.CASH", cash, "USD/tonne", "USD", at, _now(), src),
        Observation("ZINC.LME.3M", m3, "USD/tonne", "USD", at, _now(), src),
        Observation("ZINC.LME.STOCK", stock, "tonnes", "t", at, _now(), src),
        clean,
    )


# --------------------------------------------------------------------------
# 5. INDIA HRC STEEL — free, via Tata Nexarc's public price page
# --------------------------------------------------------------------------
@ttl_cache(ttl=60)
def india_hrc_nexarc(city: str | None = None) -> tuple[Observation | None, list[dict]]:
    """
    Indian HR Coil prices by brand / city / grade / thickness — FREE.

    Verified live 2026-09-04. Sample rows returned:
        HR Coil E250A 1.6mm  JSW  Mumbai  Rs 57,800/MT  Sep-26
        HR Coil E250A 1.8mm  JSW  Mumbai  Rs 56,800/MT  Sep-26
        HR Coil E250A 2.0mm  JSW  Mumbai  Rs 56,550/MT  Sep-26

    This is genuinely specification-level (grade + thickness + width + brand +
    city), which is exactly what doc (2)/(3)/(4) correctly insisted you need
    and wrongly assumed you had to buy.

    Caveats you must carry into any decision:
      - indicative trade prices, not your negotiated contract price
      - methodology is not published; treat as a BASIS reference, not truth
      - monthly-ish stamps (Sep-26), so it is a level check, not a timing signal
      - scraping: check tatanexarc.com/robots.txt and ToS before automating
    """
    import re
    r = requests.get("https://www.tatanexarc.com/steel/steel-prices-in-india/",
                     headers={**UA, "Accept": "text/html,*/*"}, timeout=TIMEOUT)
    r.raise_for_status()
    text = re.sub(r"<[^>]+>", " ", r.text)
    text = re.sub(r"\s+", " ", text)

    pat = re.compile(
        r"Flats-HR Coil\s+([A-Z0-9 ]+?)\s+Thick:\s*([\d.\- ]+)\s+Wid:\s*(\d+)"
        r"\s+Len:\s*(\S+)\s+Dia:\s*\S+\s+(\d+)/MT\s+(\S+)\s+(\w+)?\s*(\w+)?"
    )
    rows = []
    for grade, thick, wid, ln, price, period, brand, loc in pat.findall(text):
        rows.append({
            "product": "HR Coil",
            "grade": grade.strip(),
            "thickness_mm": thick.strip(),
            "width_mm": int(wid),
            "price_inr_per_t": int(price),
            "period": period,
            "brand": (brand or "").strip(),
            "city": (loc or "").strip(),
        })

    if city:
        rows = [x for x in rows if x["city"].lower() == city.lower()]
    if not rows:
        return None, []

    med = sorted(x["price_inr_per_t"] for x in rows)[len(rows) // 2]
    obs = Observation(
        series=f"STEEL.HRC.INDIA{'.' + city.upper() if city else ''}",
        value=float(med),
        unit="INR/tonne",
        currency="INR",
        observed_at=dt.date.today().isoformat() + "T00:00:00+00:00",
        retrieved_at=_now(),
        source="Tata Nexarc public India steel price page",
        quality="proxy",
        note=f"median of {len(rows)} listed HRC rows; indicative trade price, not a contract price",
        raw={"n_rows": len(rows)},
    )
    return obs, rows


# --------------------------------------------------------------------------
# 6. SHFE (unavailable) and MCX India benchmarks
# --------------------------------------------------------------------------
@ttl_cache(ttl=300)
def shfe_steel_benchmark() -> tuple[Observation, dict]:
    """
    Shanghai Futures Exchange steel benchmark — NOT AVAILABLE.

    This function previously returned a hardcoded RMB 3,240/t constant stamped
    with today's date and a source string naming SHFE. That was fabricated data
    wearing live attribution, and it fed a sourcing verdict. It is now removed.

    SHFE is genuinely inaccessible to us, and that was established by research
    before the constant was written:
      - zinc and rebar are NOT among the products opened to general overseas
        investors; access needs Qualified Foreign Investor registration
      - SHFE's own real-time feed is distributed only through approved vendors
        (ICE, Argus, LSEG), all enterprise-priced
      - re-tested 2026-09-06: every public daily-data endpoint returns 404 and
        the English market page is JS-rendered with no price in the HTML

    If you need a China ferrous signal, the honest options are a paid vendor
    feed or nothing. A convincing placeholder is worse than an empty panel,
    because it silently earns trust the data has not.
    """
    raise NotImplementedError(
        "SHFE steel is not obtainable from any free or self-serve source. "
        "Zinc/rebar require QFI registration; real-time is vendor-only "
        "(ICE / Argus / LSEG). Remove the SHFE panel or licence a feed. "
        "Re-verified unavailable on 2026-09-06."
    )


_MONTHS_SHORT = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


@ttl_cache(ttl=300)
def mcx_zinc_benchmark() -> tuple[Observation, dict]:
    """
    MCX India zinc futures — REAL, fetched, timestamped.

    Replaces a hardcoded Rs 318.50/kg constant that was 23.8% below the real
    market and drove a DOMESTIC_PREFERRED sourcing verdict.

    Source: dhan.co's public MCX zinc futures summary page, which publishes
    last-traded price, change, the exchange's own observation timestamp,
    contract month and days-to-expiry. Verified 2026-09-06: returned
    Rs 418.50/kg as on 04 Sep 2026 at 11:29 PM, cross-checked independently
    against TradingView MCX-ZINC1! at Rs 418.5.

    MCX zinc quotes in INR per KILOGRAM. One lot is 5 tonnes. The per-tonne
    figure is x1000 -- getting this wrong is the 1000x error that looks like a
    price crash, so both are returned explicitly and the unit is on the record.

    Raises rather than substituting if the page layout changes. There is no
    fallback constant, deliberately.
    """
    import re
    url = "https://dhan.co/commodity/zinc-futures-summary/"
    r = requests.get(url, headers={**UA, "Accept": "text/html,*/*"}, timeout=TIMEOUT)
    r.raise_for_status()

    txt = re.sub(r"<[^>]+>", "|", r.text)
    txt = re.sub(r"\|+", "|", txt)

    m = re.search(
        r"ZINC\|([\d,]+\.\d{1,2})\|(-?[\d,]+\.\d{1,2})\|\s*\|\(\|(-?[\d.]+)\|%\)"
        r"\|\s*as on\s*\|\s*\|?\s*(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{4})"
        r"\|\s*\|?\s*at\s+([\d:]+\s*[AP]M)",
        txt)
    if not m:
        raise RuntimeError(
            "dhan.co MCX zinc layout changed - parser needs updating. "
            "Refusing to return a price rather than guessing one.")

    ltp = float(m.group(1).replace(",", ""))
    chg = float(m.group(2).replace(",", ""))
    pct = float(m.group(3))
    day, mon, yr, clock = int(m.group(4)), m.group(5), int(m.group(6)), m.group(7)
    observed = f"{yr:04d}-{_MONTHS_SHORT.get(mon, 1):02d}-{day:02d}T00:00:00+00:00"

    exp = re.search(r"Upcoming Expiry\|\s*\|?\s*(\d{1,2}\s+[A-Z][a-z]{2})"
                    r"\|\s*\|?Days For Expiry\|\s*\|?\s*(\d+)", txt)

    if not (50 < ltp < 2000):
        raise RuntimeError(
            f"MCX zinc parsed as {ltp} INR/kg, outside a plausible band. "
            f"Refusing rather than returning a suspect price.")

    obs = Observation(
        series="ZINC.MCX.FUT",
        value=ltp,
        unit="INR/kg",
        currency="INR",
        observed_at=observed,
        retrieved_at=_now(),
        source="MCX via dhan.co public futures summary",
        note=(f"Front-month MCX zinc futures, last traded {clock} IST. "
              f"1 lot = 5 t. Per-tonne = x1000."),
        raw={"inr_per_kg": ltp, "inr_per_tonne": ltp * 1000.0,
             "change": chg, "change_pct": pct,
             "expiry": exp.group(1) if exp else None,
             "days_to_expiry": int(exp.group(2)) if exp else None},
    )
    return obs, dict(obs.raw)




@ttl_cache(ttl=21600)   # 6 hours
def _china_parity_usd(dest_iso3: str):
    """
    China landed-cost leg, in USD so the cache survives FX moves.

    Cached for 6 hours because it is built from the latest available UN Comtrade annual trade
    values and WITS tariffs — neither changes intraday, and the uncached chain
    costs ~12 seconds. That cost previously landed on a user request and timed
    out the oversized-payload security test. Call warm_caches() at startup.
    """
    import country_rank as _CR
    ranked = _CR.rank_source_countries(dest_iso3, "720810", top_n=10)
    return next((r for r in ranked if r.origin == "CHN"), None)


def warm_caches(dest_iso3: str = "IND") -> dict:
    """
    Pre-fetch the slow, long-lived lookups so no user request pays for them.
    Call once at server startup. Failures are reported, never raised — a cold
    cache degrades the audit, it does not break the server.
    """
    out = {}
    for name, fn in (("fx", fx_usd_inr_fbil),
                     ("lme_zinc", lme_zinc_westmetall),
                     ("india_hrc", india_hrc_nexarc),
                     ("mcx_zinc", mcx_zinc_benchmark),
                     ("cme_hrc", lambda: steel_hrc_futures("1mo")),
                     ("china_parity", lambda: _china_parity_usd(dest_iso3)),
                     # Run both audits so every downstream tariff/Comtrade
                     # lookup is cached too. Measured: without this the first
                     # user request paid ~12s; after it, 2.5-4s steady state.
                     ("basis_zinc", lambda: cross_exchange_basis_audit("ZINC", dest_iso3)),
                     ("basis_steel", lambda: cross_exchange_basis_audit("STEEL_HRC", dest_iso3))):
        t0 = time.monotonic()
        try:
            fn()
            out[name] = f"ok in {time.monotonic()-t0:.1f}s"
        except Exception as e:
            out[name] = f"FAILED: {type(e).__name__}: {str(e)[:60]}"
    return out


def cross_exchange_basis_audit(commodity: str = "STEEL_HRC", dest_iso3: str = "IND") -> dict:
    """
    Cross-exchange basis and import-parity audit. EVERY INPUT IS FETCHED.

    Rewritten 2026-09-06. The previous version consumed two hardcoded constants
    (MCX Rs 318.50/kg, SHFE RMB 3,240/t) and published a DOMESTIC_PREFERRED
    verdict at a 26% basis. With the real MCX price that basis is 2.7%. The
    verdict was an artefact of invented data.

    Two changes make that impossible to repeat:
      1. No constant is used anywhere. If a source is unavailable this returns
         `available: False` and NO verdict.
      2. A basis inside the assumption error band returns TOO_CLOSE_TO_CALL.
         Freight is a $45/t estimate and the physical premium is unmodelled, so
         a 2-3% basis is not a signal -- it is noise wearing a decimal point.
    """
    notes: list[str] = []
    try:
        fx_rate = fx_usd_inr_fbil().value if dest_iso3 == "IND" else 1.0
    except Exception as e:
        return {"commodity": commodity, "available": False,
                "reason": f"FX unavailable: {type(e).__name__}", "verdict": None}

    OCEAN_FREIGHT_USD_T = 45.0
    BASIS_NOISE_PCT = 4.0     # below this the basis sits inside the assumptions

    def _verdict(basis_pct, dom_label, imp_label):
        if abs(basis_pct) < BASIS_NOISE_PCT:
            return ("TOO_CLOSE_TO_CALL",
                    f"Basis of {basis_pct:+.1f}% is inside the +/-{BASIS_NOISE_PCT:.0f}% "
                    f"error band of the freight estimate and the unmodelled physical "
                    f"premium. Not a sourcing signal. Get a forwarder quote and a "
                    f"supplier premium before acting on it.")
        if basis_pct < 0:
            return (dom_label, "Domestic is below import parity by a margin that "
                               "survives the freight assumption.")
        return (imp_label, "Import parity is below domestic by a margin that "
                           "survives the freight assumption.")

    # ---------------- ZINC ----------------
    if commodity.upper() in ("ZINC", "ZINC_SHG"):
        try:
            zc, z3, zs, _ = lme_zinc_westmetall()
            mcx_obs, mcx_meta = mcx_zinc_benchmark()
        except Exception as e:
            return {"commodity": "ZINC_SHG", "available": False,
                    "reason": f"{type(e).__name__}: {str(e)[:120]}", "verdict": None}

        cif_usd = zc.value + OCEAN_FREIGHT_USD_T + zc.value * 0.001125
        landed_inr_t = cif_usd * fx_rate * 1.105 + 3000.0   # BCD 5 + AIDC 5 + SWS 0.5
        landed_inr_kg = landed_inr_t / 1000.0
        basis_kg = mcx_obs.value - landed_inr_kg
        basis_pct = round(basis_kg / landed_inr_kg * 100, 2)
        verdict, why = _verdict(basis_pct, "DOMESTIC_PREFERRED", "IMPORT_PREFERRED")

        return {
            "commodity": "ZINC_SHG", "available": True,
            "exchanges_compared": ["LME", "MCX"],
            "lme_cash_usd_t": zc.value, "lme_3m_usd_t": z3.value,
            "lme_curve_spread_cash_minus_3m": round(zc.value - z3.value, 2),
            "lme_curve_state": "BACKWARDATION" if zc.value > z3.value else "CONTANGO",
            "lme_warehouse_stock_t": zs.value,
            "lme_observed_at": zc.observed_at[:10],
            "mcx_spot_inr_kg": mcx_obs.value,
            "mcx_spot_inr_t": round(mcx_obs.value * 1000.0, 0),
            "mcx_observed_at": mcx_obs.observed_at[:10],
            "mcx_age_days": round(mcx_obs.age_days, 1),
            "landed_lme_parity_inr_kg": round(landed_inr_kg, 2),
            "landed_lme_parity_inr_t": round(landed_inr_t, 0),
            "basis_inr_kg": round(basis_kg, 2),
            "basis_spread_pct": basis_pct,
            "verdict": verdict, "verdict_reason": why,
            "assumptions": {"ocean_freight_usd_t": OCEAN_FREIGHT_USD_T,
                            "duty_pct": 10.5, "port_inland_inr_t": 3000.0,
                            "physical_premium": "NOT MODELLED"},
            "audit_trail": (
                f"LME cash ${zc.value:,.2f}/t (as of {zc.observed_at[:10]}) lands at "
                f"Rs {landed_inr_kg:.2f}/kg incl 10.5% duty. MCX front-month "
                f"Rs {mcx_obs.value:.2f}/kg (as of {mcx_obs.observed_at[:10]}). "
                f"Basis {basis_pct:+.2f}%. {why}"),
        }

    # ---------------- STEEL ----------------
    try:
        cme_obs, _ = steel_hrc_futures("1mo")
    except Exception as e:
        return {"commodity": "STEEL_HRC", "available": False,
                "reason": f"CME unavailable: {type(e).__name__}", "verdict": None}
    try:
        hrc_obs, _ = india_hrc_nexarc()
    except Exception as e:
        return {"commodity": "STEEL_HRC", "available": False,
                "reason": f"Domestic benchmark unavailable: {type(e).__name__}",
                "verdict": None}

    try:
        shfe_steel_benchmark()
        shfe_status = "available"
    except NotImplementedError:
        shfe_status = ("UNAVAILABLE - SHFE needs QFI registration; real-time is "
                       "vendor-only (ICE/Argus/LSEG). No China exchange leg here.")
        notes.append(shfe_status)

    domestic_inr_t = hrc_obs.value
    cme_usd_mt = round(cme_obs.value * 1.10231, 1)   # short ton -> metric tonne

    china_landed_inr_t = china_basis_pct = china_src = None
    try:
        cn = _china_parity_usd(dest_iso3)
        if cn and cn.landed_usd_per_t:
            # Build every field BEFORE assigning any of them. A partial failure
            # here previously left a landed cost with a null provenance string,
            # i.e. a number nobody could trace -- the exact thing this audit
            # exists to prevent.
            _landed = round(cn.landed_usd_per_t * fx_rate, 0)
            _basis = round((domestic_inr_t - _landed) / _landed * 100, 2)
            _src = (f"{cn.fob_source}: ${cn.fob_usd_per_t:,.0f}/t "
                    f"+ WITS duty {cn.duty_pct:.1f}%. Freight estimate "
                    f"${cn.freight_usd_per_t:,.0f}/t is contextual, not added to CIF.")
            china_landed_inr_t, china_basis_pct, china_src = _landed, _basis, _src
            notes.append(f"China parity uses {cn.fob_source} -- a "
                         "structural indicator, not a current quote.")
    except Exception as e:
        china_landed_inr_t = china_basis_pct = china_src = None
        notes.append(f"China parity unavailable: {type(e).__name__}: {str(e)[:80]}")

    if china_basis_pct is not None:
        verdict, why = _verdict(china_basis_pct, "DOMESTIC_PREFERRED", "IMPORT_PREFERRED")
    else:
        verdict, why = None, "No China parity leg available, so no verdict."

    return {
        "commodity": "STEEL_HRC", "available": True,
        "exchanges_compared": (["CME", "TATA_NEXARC_DOMESTIC"] +
                               (["COMTRADE_CHINA_PARITY"] if china_basis_pct is not None else [])),
        "shfe_status": shfe_status,
        "cme_hrc_usd_short_ton": cme_obs.value,
        "cme_hrc_usd_metric_ton": cme_usd_mt,
        "cme_observed_at": cme_obs.observed_at[:10],
        "cme_age_days": round(cme_obs.age_days, 1),
        "domestic_spot_inr_t": domestic_inr_t,
        "domestic_source": hrc_obs.source,
        "china_landed_parity_inr_t": china_landed_inr_t,
        "china_parity_source": china_src,
        "domestic_vs_china_basis_pct": china_basis_pct,
        "verdict": verdict, "verdict_reason": why,
        "notes": notes,
        "audit_trail": (
            f"Domestic HRC Rs {domestic_inr_t:,.0f}/t ({hrc_obs.source}). "
            f"CME HRC ${cme_usd_mt:,.0f}/t metric (as of {cme_obs.observed_at[:10]}). "
            + (f"China landed parity Rs {china_landed_inr_t:,.0f}/t via {china_src}. "
               f"Basis {china_basis_pct:+.2f}%. {why} " if china_basis_pct is not None
               else "No China parity leg available. ")
            + " ".join(notes)),
    }



# --------------------------------------------------------------------------
# 7. Health check
# --------------------------------------------------------------------------
def health_check() -> list[tuple[str, str, str]]:
    """Ping every connector. Returns [(name, status, detail)]."""
    checks = []

    def run(name, fn):
        try:
            v = fn()
            checks.append((name, "OK", v))
        except Exception as e:
            checks.append((name, "FAIL", f"{type(e).__name__}: {str(e)[:90]}"))

    run("FX FBIL USD/INR", lambda: f"{fx_usd_inr_fbil().value:.4f}")
    run("FX ECB USD/INR", lambda: f"{fx_usd_inr_ecb().value:.4f}")
    run("LME zinc cash (westmetall)",
        lambda: (lambda t: f"${t[0].value:,.2f}/t cash, ${t[1].value:,.2f}/t 3M, "
                           f"stock {t[2].value:,.0f}t, {len(t[3])} days history")(lme_zinc_westmetall()))
    run("India HRC (Tata Nexarc)",
        lambda: (lambda t: f"INR {t[0].value:,.0f}/t median of {len(t[1])} rows")(india_hrc_nexarc()))
    run("Steel HRC=F", lambda: f"{steel_hrc_futures('1mo')[0].value:.2f} USD/st")
    run("Copper HG=F", lambda: f"{copper_futures('1mo')[0].value:.4f} USD/lb")
    run("Zinc COMEX (control)",
        lambda: f"{zinc_comex_futures('1mo')[0].value:.2f} "
                f"(age {zinc_comex_futures('1mo')[0].age_days:.1f}d)")
    run("Comtrade IND HS7208 2024",
        lambda: f"{len(india_imports_by_partner('7208', 2024))} partners")
    return checks


if __name__ == "__main__":
    print(f"Connector health check @ {_now()}\n" + "=" * 74)
    for name, status, detail in health_check():
        print(f"  [{status:4}] {name:28} {detail}")
    print("\nKnown-blocked sources (do not retry):")
    for k, v in FAILED_SOURCES.items():
        print(f"  - {k}: {v}")

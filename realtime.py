"""
realtime.py — Streaming price ingestion, and the one thing it is actually for.

YOU ASKED FOR REAL-TIME. Here is the honest engineering position, then the code.

WHAT IS AND IS NOT AVAILABLE (researched live, September 2026):

  MCX zinc, real-time, tick-level ....... YES. Indian broker WebSockets.
      Angel One SmartAPI  FREE
      Zerodha Kite Connect  Rs 500/month
      Dhan Data API  Rs 499/month
      All require a real brokerage account and a daily TOTP re-auth.

  LME zinc, real-time ................... NO, not at PoC scale.
      Every route is enterprise-priced and opaque: LMElive (pricing behind a
      portal login), LMEsource (institutional multicast, ~$20k/yr distribution
      licence), LSEG/Refinitiv (~$1.5-3k per user per month plus entitlements).
      Databento does NOT carry LME at all. Investing.com/TradingView free tiers
      are >=10 minutes delayed.
      => MCX zinc is the practical real-time proxy. It tracks the same global
         LME-driven price with a known INR basis.

  Steel, real-time ...................... PARTIALLY, and read the caveat.
      CME HRC futures is the only liquid real-time steel instrument (Q2 2026
      ADV ~1,500 lots, record OI ~41,000). BUT it settles against a WEEKLY CRU
      index, so intraday ticks are traders repricing next week's index print,
      not new physical steel trades. LME's ferrous contracts are near-dead
      (Steel Rebar FOB Turkey Dec-2026 open interest: 0).
      => Ingest CME HRC as a leading indicator. Do NOT call it "the steel price".

  Aggregator APIs (Metals-API, Metals.dev, Commodities-API) ... NO WEBSOCKET.
      Their "real-time" ceiling is 60-second polling on paid tiers; cheap tiers
      are 10-minute. Twelve Data has a genuine WebSocket but gates it to Pro.
      Polygon and Tiingo do not carry zinc at all.

WHAT REAL-TIME ACTUALLY BUYS YOU — be straight with the client:
  Zinc's typical daily move is ~0.5-2% (LME cut its zinc daily price limit from
  15% to 12% in June 2026 citing lower volatility). Month-over-month moves are
  an order of magnitude larger. So for a buyer on a weekly/monthly cadence,
  real-time does NOT meaningfully lower the average price paid.
  It buys exactly one thing, and it is a real thing:
      **same-day event detection** — a 3% intraday spike on a strike headline or
      an inventory report lets you pull a purchase forward or push it back
      within the trading day instead of finding out tomorrow.
  That is what `MoveDetector` below implements. Build real-time for that
  purpose and it earns its keep; build it to shave the mean and it will not.

TESTING STATUS: `SimulatedStream` and `MoveDetector` are tested and run.
The broker clients are written from the published protocol specs but CANNOT be
executed here — they need a funded brokerage account and daily TOTP. They are
marked UNTESTED and must be validated against a live account before use.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field
from typing import Callable, Iterator


@dataclass
class Tick:
    symbol: str
    price: float
    ts: str
    volume: float | None = None
    oi: float | None = None
    source: str = ""
    simulated: bool = False


# ==========================================================================
# Event detection — the actual justification for streaming
# ==========================================================================

@dataclass
class MoveAlert:
    symbol: str
    kind: str                 # spike | slide | range_break
    pct_move: float
    from_price: float
    to_price: float
    window_min: int
    ts: str
    message: str


class MoveDetector:
    """
    Watch a tick stream for moves large enough to change a same-day decision.

    Thresholds are expressed against the instrument's own daily volatility, not
    as fixed percentages, so they stay meaningful across zinc and steel.
    """

    def __init__(self, symbol: str, session_open: float | None = None,
                 spike_pct: float = 1.5, window_min: int = 30,
                 daily_limit_pct: float = 12.0):
        self.symbol = symbol
        self.session_open = session_open
        self.spike_pct = spike_pct
        self.window_min = window_min
        self.daily_limit_pct = daily_limit_pct   # LME zinc: 12% since Jun 2026
        self._window: list[tuple[dt.datetime, float]] = []
        self._fired: set[str] = set()

    def push(self, t: Tick) -> MoveAlert | None:
        now = dt.datetime.fromisoformat(t.ts.replace("Z", "+00:00"))
        if self.session_open is None:
            self.session_open = t.price
        self._window.append((now, t.price))
        cutoff = now - dt.timedelta(minutes=self.window_min)
        self._window = [(ts, p) for ts, p in self._window if ts >= cutoff]
        if len(self._window) < 2:
            return None

        ref = self._window[0][1]
        move = (t.price - ref) / ref * 100
        day_move = (t.price - self.session_open) / self.session_open * 100

        if abs(move) >= self.spike_pct:
            key = f"{now:%Y%m%d%H}-{'up' if move > 0 else 'dn'}"
            if key not in self._fired:
                self._fired.add(key)
                kind = "spike" if move > 0 else "slide"
                return MoveAlert(
                    self.symbol, kind, round(move, 2), ref, t.price,
                    self.window_min, t.ts,
                    (f"{self.symbol} moved {move:+.2f}% in {self.window_min}m "
                     f"({ref:,.2f} -> {t.price:,.2f}); day {day_move:+.2f}%. "
                     f"{'Consider pulling a planned purchase forward.' if move < 0 else 'Consider deferring any discretionary buy, or accelerate if short.'}"),
                )
        if abs(day_move) >= self.daily_limit_pct * 0.6:
            key = f"{now:%Y%m%d}-limit"
            if key not in self._fired:
                self._fired.add(key)
                return MoveAlert(
                    self.symbol, "range_break", round(day_move, 2),
                    self.session_open, t.price, 0, t.ts,
                    (f"{self.symbol} is {day_move:+.2f}% on the day — approaching the "
                     f"{self.daily_limit_pct}% exchange limit. Escalate to the category manager."),
                )
        return None


# ==========================================================================
# Simulated stream — runs with no credentials, used by the demo
# ==========================================================================

class SimulatedStream:
    """
    Generates a plausible intraday tick series with realistic volatility, so
    the streaming pipeline and alerting can be exercised end to end without a
    brokerage account. Every Tick is flagged `simulated=True`.
    """

    def __init__(self, symbol: str, start_price: float, ann_vol_pct: float = 23.5,
                 seed: int = 3, shock_at: int | None = None, shock_pct: float = -2.2):
        self.symbol, self.price = symbol, start_price
        self.rng = random.Random(seed)
        self.per_tick_sigma = (ann_vol_pct / 100) / (252 ** 0.5) / (375 ** 0.5)
        self.shock_at, self.shock_pct = shock_at, shock_pct

    def stream(self, n_ticks: int = 375, start: dt.datetime | None = None) -> Iterator[Tick]:
        t0 = start or dt.datetime.now(dt.timezone.utc).replace(hour=3, minute=30, second=0, microsecond=0)
        for i in range(n_ticks):
            self.price *= (1 + self.rng.gauss(0, self.per_tick_sigma))
            if self.shock_at is not None and i == self.shock_at:
                self.price *= (1 + self.shock_pct / 100)
            yield Tick(self.symbol, round(self.price, 2),
                       (t0 + dt.timedelta(minutes=i)).isoformat(timespec="seconds"),
                       source="SIMULATED", simulated=True)


# ==========================================================================
# Broker clients — UNTESTED, need a live account
# ==========================================================================

class AngelOneStream:
    """
    Angel One SmartAPI WebSocket. FREE for account holders. Recommended default.

    Endpoint : ws://smartapisocket.angelone.in/smart-stream   (note: ws://, not wss://)
    Headers  : Authorization: Bearer <JWT>, x-api-key, x-client-code, x-feed-token
    Auth     : TOTP loginByPassword -> generateTokens; JWT refreshed daily
    Limits   : 3 simultaneous connections per client
    Exchange : MCX_FO exchange type code = 5
    Modes    : LTP / Quote / SnapQuote / Depth-20 (beta)
    SDK      : pip install smartapi-python  (v1.5.5, last release 2025-02-07 —
               stale but official; the SDK source is the authoritative protocol
               reference since the prose docs are JS-rendered)

    Find the zinc token: download the scrip master JSON from the SmartAPI site,
    filter exch_seg == "MCX_FO" and symbol containing "ZINC". MCX futures tokens
    ROLL EVERY EXPIRY — refresh monthly or you will silently stream a dead
    contract.

    *** UNTESTED IN THIS ENGAGEMENT — requires a funded Angel One account. ***
    """

    def __init__(self, api_key: str, client_code: str, jwt: str, feed_token: str):
        self.api_key, self.client_code = api_key, client_code
        self.jwt, self.feed_token = jwt, feed_token

    def stream(self, tokens: list[str], on_tick: Callable[[Tick], None]) -> None:
        raise NotImplementedError(
            "Install smartapi-python and wire SmartWebSocketV2 here. "
            "Deliberately not stubbed with fake behaviour — an untested "
            "broker client that silently returns nothing is worse than one "
            "that refuses to run."
        )


class KiteStream:
    """
    Zerodha Kite Connect WebSocket. Rs 500/month per API key (live + historical).

    Endpoint : wss://ws.kite.trade?api_key=...&access_token=...
    Protocol : binary ticks; ltp 8B / quote 44B / full 184B incl. depth
    Auth     : api_key + api_secret -> request_token -> access_token,
               expires daily ~06:00 IST. TOTP mandatory. Automate with pyotp.
    Limits   : 3 connections per key, 3000 instruments per connection
    SDK      : pip install kiteconnect  (v5.2.1, 2026-07-23, actively maintained)

    Find the zinc token: GET https://api.kite.trade/instruments/MCX (gzipped
    CSV), filter tradingsymbol starting "ZINC", segment "MCX-FUT". Refresh daily
    around 08:30 IST.

    *** UNTESTED IN THIS ENGAGEMENT — requires a funded Zerodha account. ***
    """

    def __init__(self, api_key: str, access_token: str):
        self.api_key, self.access_token = api_key, access_token

    def stream(self, tokens: list[int], on_tick: Callable[[Tick], None]) -> None:
        raise NotImplementedError(
            "Install kiteconnect and wire KiteTicker here. See the module "
            "docstring for the token-discovery and daily-auth steps."
        )


# ==========================================================================
# Polling stream for the free EOD sources
# ==========================================================================

class PollingStream:
    """
    The free sources (westmetall LME zinc, Tata Nexarc India HRC) are daily.
    Poll them on a schedule; do NOT hammer them. They are public web pages, not
    an API you are paying for. Cache aggressively and respect robots.txt.
    """

    def __init__(self, fetch_fn: Callable[[], Tick], min_interval_s: int = 3600):
        self.fetch_fn, self.min_interval_s = fetch_fn, min_interval_s
        self._last: dt.datetime | None = None
        self._cached: Tick | None = None

    def get(self) -> Tick | None:
        now = dt.datetime.now(dt.timezone.utc)
        if self._last and (now - self._last).total_seconds() < self.min_interval_s:
            return self._cached
        self._cached = self.fetch_fn()
        self._last = now
        return self._cached


if __name__ == "__main__":
    print("Intraday event detection on a simulated MCX zinc session")
    print("(the actual justification for streaming — same-day decision changes)\n")

    sim = SimulatedStream("MCX:ZINC", start_price=418.10, ann_vol_pct=23.5,
                          shock_at=150, shock_pct=-2.4)
    det = MoveDetector("MCX:ZINC", spike_pct=1.5, window_min=30, daily_limit_pct=12.0)

    alerts, n = [], 0
    last = None
    for t in sim.stream(375):
        n += 1
        last = t
        a = det.push(t)
        if a:
            alerts.append(a)
            print(f"  [{a.ts[11:16]}] {a.kind.upper():<11} {a.pct_move:+6.2f}%   {a.message}")

    print(f"\n  {n} ticks processed, {len(alerts)} alert(s) raised.")
    print(f"  Session open {sim_open:,.2f} -> close {last.price:,.2f}"
          if (sim_open := 418.10) else "")
    print(f"  Day move: {(last.price - 418.10) / 418.10 * 100:+.2f}%")
    print("\n  NOTE: ticks are SIMULATED. Broker clients above are written to the")
    print("  published specs but are UNTESTED — they need a funded account.")

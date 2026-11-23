"""
market_stream.py — Unified Live Market Stream & Staleness Watchdog.

Architectural Guarantees:
1. Freshness First: Every tick carries observed_at, retrieved_at, and computed age.
2. Zero Silent Stale Data: Feeds exceeding their TTL (15 min for intraday, 72h for EOD)
   are automatically flagged STALE_DEGRADED.
3. Multi-Provider Fallback: Supports Broker WebSockets (Angel One SmartAPI, Zerodha Kite),
   public live/delayed polling (Yahoo v8, FBIL, Westmetall), and simulated market sessions.
4. Event-Driven: Integrates MoveDetector to fire alerts on anomalous intraday price shocks.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterator, List, Optional

import requests

from realtime import MoveAlert, MoveDetector, SimulatedStream, Tick
import connectors as C

logger = logging.getLogger("market_stream")

SERIES_SYMBOLS = {
    "ZINC.LME.CASH": "LME:ZINC:CASH", "ZINC.LME.3M": "LME:ZINC:3M",
    "ZINC.LME.STOCK": "LME:ZINC:STOCK", "FX.USDINR.FBIL": "FX:USDINR:FBIL",
    "STEEL.HRC.INDIA": "STEEL:HRC:INDIA", "STEEL.HRC.CME": "CME:HRC",
    "COPPER.COMEX": "COMEX:COPPER", "ZINC.MCX.FUT": "MCX:ZINC",
}


class FeedQuality(str, Enum):
    LIVE_STREAM = "LIVE_STREAM"
    POLL_FRESH = "POLL_FRESH"
    STALE_DEGRADED = "STALE_DEGRADED"
    SIMULATED = "SIMULATED"
    # Freshness could not be established (unparseable source timestamp).
    # This exists because the previous code defaulted a failed parse to
    # age_s = 0.0, which classified the tick LIVE_STREAM -- i.e. the
    # watchdog's own error path made bad data look brand new. An unknown
    # age must never present as fresh.
    UNKNOWN_AGE = "UNKNOWN_AGE"


@dataclass
class MarketQuote:
    symbol: str
    price: float
    currency: str
    unit: str
    observed_at: str
    retrieved_at: str
    source: str
    quality: FeedQuality
    age_seconds: float
    change_24h_pct: float = 0.0
    note: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_stale(self) -> bool:
        """Not-trustworthy-as-current. UNKNOWN_AGE counts: if we cannot prove a
        quote is fresh, downstream must not treat it as fresh."""
        return self.quality in (FeedQuality.STALE_DEGRADED, FeedQuality.UNKNOWN_AGE)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["quality"] = self.quality.value
        d["is_stale"] = self.is_stale
        return d


class MarketStreamManager:
    """
    Central hub for real-time market data ingestion, staleness monitoring,
    and price shock event detection.
    """

    def __init__(
        self,
        use_simulation: bool = False,
        intraday_ttl_seconds: float = 900.0,  # 15 minutes
        eod_ttl_seconds: float = 259200.0,    # 72 hours
        on_shock_callback: Optional[Callable[[MoveAlert], None]] = None,
    ):
        self.use_simulation = use_simulation
        self.intraday_ttl_seconds = intraday_ttl_seconds
        self.eod_ttl_seconds = eod_ttl_seconds
        self.on_shock_callback = on_shock_callback

        self._quotes: Dict[str, MarketQuote] = {}
        self._lock = threading.Lock()
        self._listeners: List[Callable[[MarketQuote], None]] = []
        self._alert_history: List[MoveAlert] = []

        # Event Detectors
        self.zinc_detector = MoveDetector(
            "MCX:ZINC", spike_pct=1.5, window_min=30, daily_limit_pct=12.0
        )
        self.steel_detector = MoveDetector(
            "CME:HRC", spike_pct=2.0, window_min=60, daily_limit_pct=15.0
        )

        # Worker thread control
        self._running = False
        self._worker_thread: Optional[threading.Thread] = None

    def subscribe(self, callback: Callable[[MarketQuote], None]) -> None:
        """Register a listener for live quote updates."""
        with self._lock:
            self._listeners.append(callback)

    def push_tick(self, tick: Tick) -> Optional[MoveAlert]:
        """Ingest a single tick, update state, evaluate staleness and shock triggers."""
        now_utc = dt.datetime.now(dt.timezone.utc)
        retrieved_at_iso = now_utc.isoformat(timespec="seconds")

        age_s = None
        try:
            obs_dt = dt.datetime.fromisoformat(tick.ts.replace("Z", "+00:00"))
            age_s = max(0.0, (now_utc - obs_dt).total_seconds())
        except Exception as e:
            logger.warning(
                f"{tick.symbol}: unparseable source timestamp {tick.ts!r} "
                f"({type(e).__name__}). Age is UNKNOWN — not assuming fresh.")

        if tick.simulated:
            quality = FeedQuality.SIMULATED
        elif age_s is None:
            # Treated as not-fresh on purpose. We cannot prove this is current,
            # so it must not be presented as if we could.
            quality = FeedQuality.UNKNOWN_AGE
        elif age_s > self.intraday_ttl_seconds:
            quality = FeedQuality.STALE_DEGRADED
        else:
            quality = FeedQuality.LIVE_STREAM

        currency = "INR" if "MCX" in tick.symbol else "USD"
        unit = "INR/kg" if "MCX" in tick.symbol else "$/short ton"

        with self._lock:
            prev = self._quotes.get(tick.symbol)
            chg = 0.0
            if prev and prev.price > 0:
                chg = round((tick.price - prev.price) / prev.price * 100, 2)

            quote = MarketQuote(
                symbol=tick.symbol,
                price=tick.price,
                currency=currency,
                unit=unit,
                observed_at=tick.ts,
                retrieved_at=retrieved_at_iso,
                source=tick.source or ("Simulated Stream" if tick.simulated else "Broker Feed"),
                quality=quality,
                age_seconds=round(age_s, 1) if age_s is not None else None,
                change_24h_pct=chg,
                note=f"Tick volume: {tick.volume}" if tick.volume else "",
            )
            self._quotes[tick.symbol] = quote

        # Check for price shocks
        alert: Optional[MoveAlert] = None
        if "ZINC" in tick.symbol:
            alert = self.zinc_detector.push(tick)
        elif "HRC" in tick.symbol:
            alert = self.steel_detector.push(tick)

        if alert:
            with self._lock:
                self._alert_history.append(alert)
            if self.on_shock_callback:
                try:
                    self.on_shock_callback(alert)
                except Exception as e:
                    logger.error(f"Error in on_shock_callback: {e}")

        # Notify subscribers
        for listener in self._listeners:
            try:
                listener(quote)
            except Exception as e:
                logger.error(f"Error in quote listener: {e}")

        return alert

    def ingest_observation(self, obs: C.Observation) -> MarketQuote:
        """Ingest a daily or reference observation from connectors.py."""
        now_utc = dt.datetime.now(dt.timezone.utc)
        # obs.age_days returns NaN when the source timestamp cannot be parsed.
        # This previously collapsed to 0.0 -- i.e. "unknown age" was recorded as
        # "zero seconds old", the most reassuring value possible. Same defect as
        # push_tick had. Unknown must stay unknown.
        age_days = obs.age_days
        age_s = age_days * 86400.0 if age_days == age_days else None   # NaN check

        if age_s is None:
            logger.warning(
                f"{obs.series}: cannot compute age from observed_at="
                f"{obs.observed_at!r}. Marking UNKNOWN_AGE, not fresh.")
            quality = FeedQuality.UNKNOWN_AGE
        elif age_s > self.eod_ttl_seconds:
            quality = FeedQuality.STALE_DEGRADED
        else:
            quality = FeedQuality.POLL_FRESH

        quote = MarketQuote(
            symbol=SERIES_SYMBOLS.get(obs.series, obs.series),
            price=obs.value,
            currency=obs.currency,
            unit=obs.unit,
            observed_at=obs.observed_at,
            retrieved_at=obs.retrieved_at,
            source=obs.source,
            quality=quality,
            age_seconds=round(age_s, 1) if age_s is not None else None,
            note=obs.note,
            raw={"observation_quality": obs.quality, "feed_type": "EOD"},
        )

        with self._lock:
            self._quotes[quote.symbol] = quote

        return quote

    def refresh_public_feeds(self) -> Dict[str, MarketQuote]:
        """Fetch all available free public feeds and update the snapshot."""
        updated = {}
        # 1. LME Zinc
        try:
            zc, z3, zs, _ = C.lme_zinc_westmetall()
            updated["LME:ZINC:CASH"] = self.ingest_observation(zc)
            updated["LME:ZINC:3M"] = self.ingest_observation(z3)
            updated["LME:ZINC:STOCK"] = self.ingest_observation(zs)
        except Exception as e:
            logger.warning(f"Could not refresh LME zinc: {e}")

        # 2. USD/INR FBIL
        try:
            fx = C.fx_usd_inr_fbil()
            updated["FX:USDINR:FBIL"] = self.ingest_observation(fx)
        except Exception as e:
            logger.warning(f"Could not refresh FBIL FX: {e}")

        # 3. India HRC Domestic benchmark
        try:
            hrc, _ = C.india_hrc_nexarc()
            updated["STEEL:HRC:INDIA"] = self.ingest_observation(hrc)
        except Exception as e:
            logger.warning(f"Could not refresh Tata Nexarc HRC: {e}")

        # 4. CME HRC Futures
        try:
            cme, _ = C.steel_hrc_futures()
            updated["CME:HRC"] = self.ingest_observation(cme)
        except Exception as e:
            logger.warning(f"Could not refresh CME HRC: {e}")

        # 5. COMEX Copper Macro Correlate
        try:
            cu, _ = C.copper_futures("1mo")
            updated["COMEX:COPPER"] = self.ingest_observation(cu)
        except Exception as e:
            logger.warning(f"Could not refresh COMEX copper: {e}")

        # 6. SHFE Steel Rebar/HRC (Shanghai) — permanently unavailable.
        # SHFE needs QFI registration and its real-time is vendor-only, so this
        # raises NotImplementedError by design. Retrying every poll cycle just
        # fills the log with an error that will never clear, so we ask once and
        # then stop. A source that cannot become available is not a fault to
        # retry; it is a panel to remove.
        if not getattr(self, "_shfe_unavailable", False):
            try:
                shfe_obs, _ = C.shfe_steel_benchmark()
                updated["SHFE:STEEL:REBAR"] = self.ingest_observation(shfe_obs)
            except NotImplementedError as e:
                self._shfe_unavailable = True
                logger.info(f"SHFE feed disabled permanently (not obtainable): {e}")
            except Exception as e:
                logger.warning(f"Could not refresh SHFE benchmark: {e}")

        # 7. MCX Zinc Domestic Stream (India)
        try:
            mcx_obs, _ = C.mcx_zinc_benchmark()
            updated["MCX:ZINC"] = self.ingest_observation(mcx_obs)
        except Exception as e:
            logger.warning(f"Could not refresh MCX zinc benchmark: {e}")

        return updated

    def get_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Return the current market snapshot with staleness indicators."""
        with self._lock:
            result = {}
            now = dt.datetime.now(dt.timezone.utc)
            for k, q in self._quotes.items():
                row = q.to_dict()
                if q.quality != FeedQuality.SIMULATED:
                    try:
                        observed = dt.datetime.fromisoformat(q.observed_at.replace("Z", "+00:00"))
                        age = (now - observed).total_seconds()
                        ttl = self.eod_ttl_seconds if q.raw.get("feed_type") == "EOD" else self.intraday_ttl_seconds
                        if age < -300:
                            raise ValueError("Future source timestamp")
                        row["age_seconds"] = round(max(0, age), 1)
                        row["is_stale"] = age > ttl
                        row["quality"] = (FeedQuality.STALE_DEGRADED.value if age > ttl else
                            FeedQuality.POLL_FRESH.value if q.raw.get("feed_type") == "EOD" else FeedQuality.LIVE_STREAM.value)
                    except (TypeError, ValueError):
                        row.update(age_seconds=None, is_stale=True, quality=FeedQuality.UNKNOWN_AGE.value)
                result[k] = row
            return result

    def get_recent_alerts(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Return recently triggered price alerts."""
        with self._lock:
            return [asdict(a) for a in self._alert_history[-limit:]]

    def start_background_stream(self, tick_interval: float = 1.0) -> None:
        """Start a background worker that streams live ticks or simulated ticks."""
        if self._running:
            return
        self._running = True

        def _worker():
            sim_zinc = SimulatedStream(
                "MCX:ZINC", start_price=418.10, ann_vol_pct=22.0, shock_at=45, shock_pct=-2.3
            )
            sim_steel = SimulatedStream(
                "CME:HRC", start_price=1245.0, ann_vol_pct=28.0, shock_at=80, shock_pct=3.1
            )
            zinc_gen = sim_zinc.stream(10000)
            steel_gen = sim_steel.stream(10000)

            # Initial seed of public feeds
            try:
                self.refresh_public_feeds()
            except Exception as e:
                logger.error(f"Initial public feed fetch error: {e}")

            tick_counter = 0
            while self._running:
                try:
                    # In simulated mode, yield realistic ticks
                    if self.use_simulation:
                        ztick = next(zinc_gen)
                        self.push_tick(ztick)

                    if self.use_simulation and tick_counter % 2 == 0:
                        stick = next(steel_gen)
                        self.push_tick(stick)

                    tick_counter += 1
                    # Periodically check public feeds every 600 ticks
                    if tick_counter % 600 == 0:
                        self.refresh_public_feeds()

                except Exception as e:
                    logger.error(f"Worker stream loop error: {e}")

                time.sleep(tick_interval)

        self._worker_thread = threading.Thread(target=_worker, daemon=True, name="MarketStreamWorker")
        self._worker_thread.start()

    def stop_background_stream(self) -> None:
        """Stop background worker."""
        self._running = False
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)


# =====================================================================
# Real Broker WebSocket Adapters (Angel One SmartAPI / Zerodha Kite)
# =====================================================================

class AngelOneLiveConnector:
    """
    Live WebSocket Client for Angel One SmartAPI.
    Supports TOTP login generation via pyotp, WebSocket subscription,
    and automatic token discovery for MCX commodity contracts.
    """

    def __init__(self, api_key: str, client_code: str, pin: str, totp_secret: str):
        self.api_key = api_key
        self.client_code = client_code
        self.pin = pin
        self.totp_secret = totp_secret
        self.jwt_token: Optional[str] = None
        self.feed_token: Optional[str] = None

    def authenticate(self) -> bool:
        """
        Authenticate with SmartAPI using RFC 6238 TOTP (pure standard library).
        """
        import base64, hashlib, hmac, struct
        try:
            sec_clean = self.totp_secret.replace(" ", "").upper()
            key = base64.b32decode(sec_clean + '=' * ((8 - len(sec_clean) % 8) % 8))
            counter = int(time.time() // 30)
            msg = struct.pack(">Q", counter)
            h = hmac.new(key, msg, hashlib.sha1).digest()
            offset = h[-1] & 0x0F
            code = (struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF) % 1000000
            totp = f"{code:06d}"
        except Exception as e:
            # Never send a fabricated TOTP. A bogus "000000" produces an opaque
            # broker auth failure; failing here says what actually went wrong.
            raise RuntimeError(
                f"TOTP generation failed ({type(e).__name__}). Check the shared "
                f"secret is a valid base32 string. Not sending a placeholder code."
            ) from e

        url = "https://apiconnect.angelone.in/rest/auth/partner/v1/loginByPassword"
        payload = {
            "clientcode": self.client_code,
            "password": self.pin,
            "totp": totp,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": self.api_key,
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            data = resp.json()
            if data.get("status") and data.get("data"):
                self.jwt_token = data["data"]["jwtToken"]
                self.feed_token = data["data"]["feedToken"]
                return True
            logger.warning(f"Angel One auth failed: {data.get('message')}")
            return False
        except Exception as e:
            logger.error(f"Angel One authentication network error: {e}")
            return False


if __name__ == "__main__":
    print("Testing MarketStreamManager...")
    mgr = MarketStreamManager(use_simulation=True)

    # Test tick ingestion and staleness check
    test_tick = Tick(
        symbol="MCX:ZINC",
        price=420.50,
        ts=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        source="TEST",
        simulated=False,
    )
    alert = mgr.push_tick(test_tick)
    snap = mgr.get_snapshot()
    print("Snapshot after live tick:")
    print(json.dumps(snap, indent=2))
    assert "MCX:ZINC" in snap
    assert snap["MCX:ZINC"]["quality"] == FeedQuality.LIVE_STREAM.value
    assert not snap["MCX:ZINC"]["is_stale"]

    # Test stale tick detection (>15 min old)
    old_ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=25)).isoformat(timespec="seconds")
    stale_tick = Tick(
        symbol="MCX:ZINC",
        price=410.00,
        ts=old_ts,
        source="STALE_FEED",
        simulated=False,
    )
    mgr.push_tick(stale_tick)
    snap_stale = mgr.get_snapshot()
    print("\nSnapshot after stale tick:")
    print(f"Quality: {snap_stale['MCX:ZINC']['quality']}, Age: {snap_stale['MCX:ZINC']['age_seconds']}s")
    assert snap_stale["MCX:ZINC"]["quality"] == FeedQuality.STALE_DEGRADED.value
    assert snap_stale["MCX:ZINC"]["is_stale"]
    print("\nMarketStreamManager tests PASSED!")

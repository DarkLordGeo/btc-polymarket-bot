"""
Live BTC/USD reference price feed with a rolling price history, used by the
decision engine to estimate short-horizon drift and realized volatility.

Primary source: Coinbase's public WS ticker (no auth, no key needed).
Falls back to polling CoinGecko REST if the WS is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque

import requests
import websockets

import config

logger = logging.getLogger(__name__)


class BtcPriceFeed:
    def __init__(self, lookback_sec: float = config.VOL_LOOKBACK_SEC):
        self.lookback_sec = lookback_sec
        # (timestamp, price) pairs, oldest first.
        self._history: deque[tuple[float, float]] = deque(maxlen=5000)
        self._stop = asyncio.Event()

    @property
    def price(self) -> float | None:
        return self._history[-1][1] if self._history else None

    @property
    def price_ts(self) -> float | None:
        """Timestamp (time.time()) the current `price` was actually recorded
        at — `price` alone can't tell a caller whether it's fresh or minutes
        stale from a dead feed. See is_fresh()."""
        return self._history[-1][0] if self._history else None

    def is_fresh(self, max_age_sec: float, now: float) -> bool:
        """
        True only if the latest recorded price is within max_age_sec of
        `now`. If the WS drops and the REST fallback also fails, `.price`
        keeps returning the last value it ever saw — this is what actually
        distinguishes "live" from "stale but still sitting in memory".
        Pure/synchronous — `now` is passed in explicitly rather than read
        internally, so callers (and tests) control what "now" means.
        """
        ts = self.price_ts
        if ts is None:
            return False
        return (now - ts) <= max_age_sec

    def _record(self, price: float):
        now = time.time()
        self._history.append((now, price))
        cutoff = now - self.lookback_sec * 4  # keep a bit more than we need
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

    def price_at_or_before(self, ts: float) -> float | None:
        """Most recent recorded price at or before timestamp `ts`."""
        hit = self.price_at_or_before_with_ts(ts)
        return hit[0] if hit is not None else None

    def price_at_or_before_with_ts(self, ts: float) -> tuple[float, float] | None:
        """
        Same lookup as price_at_or_before, but also returns the matched
        sample's OWN timestamp as (price, sample_ts) — price_at_or_before
        alone throws that away, which made it impossible for a caller to
        tell "the last price before market open" apart from "the last price
        we saw 10 minutes ago because the feed had an outage spanning market
        open". Returns None if there's no sample at or before `ts` at all.
        """
        result = None
        for t, p in self._history:
            if t > ts:
                break
            result = (p, t)
        return result

    def history_span_sec(self) -> float:
        """Seconds between the oldest and newest recorded sample."""
        if len(self._history) < 2:
            return 0.0
        return self._history[-1][0] - self._history[0][0]

    def recent_returns(self, window_sec: float) -> list[float]:
        """Log-returns between consecutive samples within the last window_sec."""
        return self.recent_returns_with_span(window_sec)[0]

    def recent_returns_with_span(self, window_sec: float) -> tuple[list[float], float]:
        """
        Log-returns between consecutive samples within the last window_sec,
        together with the ACTUAL elapsed time between the first and last
        sample used to compute them. Use this (not history_span_sec(), which
        measures the whole retained buffer) when the two need to line up —
        e.g. feeding both into estimate_sigma_per_sqrt_sec.
        """
        cutoff = time.time() - window_sec
        pts = [(t, p) for t, p in self._history if t >= cutoff]
        returns = []
        for (_, p0), (_, p1) in zip(pts, pts[1:]):
            if p0 > 0 and p1 > 0:
                import math

                returns.append(math.log(p1 / p0))
        span = (pts[-1][0] - pts[0][0]) if len(pts) >= 2 else 0.0
        return returns, span

    def stop(self):
        self._stop.set()

    async def run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._run_ws_once()
                backoff = 1.0
            except Exception:  # noqa: BLE001
                logger.warning("BTC WS feed dropped, falling back to REST poll for a bit", exc_info=True)
                await self._poll_rest_until(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _run_ws_once(self):
        async with websockets.connect(config.COINBASE_WS_URL) as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "subscribe",
                        "product_ids": [config.COINBASE_PRODUCT_ID],
                        "channels": ["ticker"],
                    }
                )
            )
            async for raw in ws:
                if self._stop.is_set():
                    return
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "ticker" and msg.get("price"):
                    self._record(float(msg["price"]))

    async def _poll_rest_until(self, duration_sec: float):
        """Best-effort REST polling used only while the WS is reconnecting."""
        end = time.time() + max(duration_sec, 5.0)
        while time.time() < end and not self._stop.is_set():
            try:
                resp = requests.get(
                    config.COINGECKO_REST_URL,
                    params={"ids": "bitcoin", "vs_currencies": "usd"},
                    timeout=5,
                )
                resp.raise_for_status()
                price = resp.json()["bitcoin"]["usd"]
                self._record(float(price))
            except Exception:  # noqa: BLE001
                logger.debug("CoinGecko fallback poll failed", exc_info=True)
            await asyncio.sleep(5)

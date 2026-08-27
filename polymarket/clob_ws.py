"""
Real-time market-data feed from Polymarket's CLOB WebSocket ("market"
channel): order book snapshots, price changes, and last-trade prints for a
set of token ids. Read-only — no auth needed for market data.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Callable

import websockets

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass
class TokenState:
    token_id: str
    best_bid: float | None = None
    best_ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    last_trade_price: float | None = None
    last_update_ts: float | None = None

    def midpoint(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return self.last_trade_price

    def imbalance(self) -> float | None:
        if self.bid_size is None or self.ask_size is None:
            return None
        total = self.bid_size + self.ask_size
        if total <= 0:
            return None
        return (self.bid_size - self.ask_size) / total

    def is_fresh(self, max_age_sec: float, now: float) -> bool:
        """
        True only if this state has actually received an update within
        max_age_sec of `now`. A TokenState that has never received an
        update (last_update_ts is None, e.g. the WS just subscribed and
        nothing has arrived yet) is never "fresh" — there's no data to be
        fresh or stale, it's simply missing, and callers must treat that the
        same as a stale book (never label it live, never trade on it).

        This is a pure/synchronous check deliberately kept on the data
        object itself rather than reaching for time.time() internally, so
        callers (and tests) control what "now" means.
        """
        if self.last_update_ts is None:
            return False
        return (now - self.last_update_ts) <= max_age_sec


class ClobMarketFeed:
    """
    Maintains live TokenState for a set of token ids by consuming the CLOB
    market WebSocket channel. Call `run()` as an asyncio task; read
    `.state[token_id]` from anywhere else, or pass `on_update` to be notified.
    """

    def __init__(self, token_ids: list[str], on_update: Callable[[str], None] | None = None):
        self.token_ids = list(token_ids)
        self.state: dict[str, TokenState] = {tid: TokenState(tid) for tid in token_ids}
        self._on_update = on_update
        self._stop = asyncio.Event()

    def stop(self):
        self._stop.set()

    async def run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._run_once()
                backoff = 1.0
            except Exception:  # noqa: BLE001 - reconnect on anything
                logger.exception("CLOB WS connection dropped, reconnecting in %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _run_once(self):
        async with websockets.connect(WS_URL, ping_interval=None) as ws:
            await ws.send(json.dumps({"assets_ids": self.token_ids, "type": "market"}))
            heartbeat = asyncio.create_task(self._heartbeat(ws))
            try:
                async for raw in ws:
                    if self._stop.is_set():
                        break
                    self._handle_message(raw)
            finally:
                heartbeat.cancel()

    async def _heartbeat(self, ws):
        while True:
            await asyncio.sleep(10)
            try:
                await ws.send("PING")
            except Exception:  # noqa: BLE001
                return

    def _handle_message(self, raw: str | bytes):
        if raw in ("PONG", b"PONG"):
            return
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        events = msg if isinstance(msg, list) else [msg]
        for event in events:
            self._handle_event(event)

    def _handle_event(self, event: dict):
        import time

        etype = event.get("event_type") or event.get("type")
        token_id = event.get("asset_id") or event.get("market")
        if token_id not in self.state:
            return
        st = self.state[token_id]

        if etype == "book":
            bids = event.get("bids") or []
            asks = event.get("asks") or []
            if bids:
                best = max(bids, key=lambda l: float(l["price"]))
                st.best_bid, st.bid_size = float(best["price"]), float(best["size"])
            if asks:
                best = min(asks, key=lambda l: float(l["price"]))
                st.best_ask, st.ask_size = float(best["price"]), float(best["size"])
            st.last_update_ts = time.time()

        elif etype == "price_change":
            price = event.get("price")
            side = event.get("side")
            size = event.get("size")
            if price is None:
                return
            if side == "BUY":
                st.best_bid, st.bid_size = float(price), float(size or 0)
            elif side == "SELL":
                st.best_ask, st.ask_size = float(price), float(size or 0)
            st.last_update_ts = time.time()

        elif etype == "last_trade_price":
            price = event.get("price")
            if price is not None:
                st.last_trade_price = float(price)
                st.last_update_ts = time.time()
        else:
            return

        if self._on_update:
            self._on_update(token_id)

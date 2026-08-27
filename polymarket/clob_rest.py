"""
Read-only REST helpers for Polymarket's CLOB API. No auth required for
public market data (book/midpoint/price). Placing orders requires an
authenticated client (py-clob-client + a funded Polygon wallet) which this
project deliberately does not implement — see README.md.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

CLOB_BASE = "https://clob.polymarket.com"


@dataclass
class OrderBookSnapshot:
    token_id: str
    bids: list[tuple[float, float]]  # (price, size), best bid last per API convention
    asks: list[tuple[float, float]]  # (price, size), best ask first

    def best_bid(self) -> float | None:
        return self.bids[-1][0] if self.bids else None

    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    def imbalance(self) -> float | None:
        """
        (bid_size - ask_size) / (bid_size + ask_size) over the top few levels,
        in [-1, 1]. Positive means more resting buy interest than sell.
        """
        bid_sz = sum(sz for _, sz in self.bids[-5:])
        ask_sz = sum(sz for _, sz in self.asks[:5])
        total = bid_sz + ask_sz
        if total <= 0:
            return None
        return (bid_sz - ask_sz) / total


def get_order_book(token_id: str, timeout: float = 5.0) -> OrderBookSnapshot:
    resp = requests.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    bids = [(float(lvl["price"]), float(lvl["size"])) for lvl in data.get("bids", [])]
    asks = [(float(lvl["price"]), float(lvl["size"])) for lvl in data.get("asks", [])]
    return OrderBookSnapshot(token_id=token_id, bids=bids, asks=asks)


def get_midpoint(token_id: str, timeout: float = 5.0) -> float | None:
    resp = requests.get(f"{CLOB_BASE}/midpoint", params={"token_id": token_id}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    mid = data.get("mid")
    return float(mid) if mid is not None else None

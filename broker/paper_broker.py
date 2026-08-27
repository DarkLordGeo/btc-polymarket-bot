"""
Simulated execution and settlement. No real orders are ever placed — this
buys/sells against the observed best bid/ask as if the fill happened, then
settles at $1/$0 when the market resolves. That's the honest version of
"paper trading": it does NOT model slippage from your own order walking the
book, adverse selection, or the possibility the fill simply doesn't happen
in a thin market — real execution will be worse than this, not better.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import config
from engine.decision_engine import Action


@dataclass
class OpenPosition:
    market_slug: str
    question: str
    side: str  # "UP" or "DOWN"
    entry_price: float  # price paid per share (0..1)
    stake: float  # USD committed, including fee
    fee_paid: float
    shares: float
    opened_at: float
    end_ts: float
    reasoning_snapshot: dict


@dataclass
class SettledTrade:
    position: OpenPosition
    won: bool
    payout: float
    pnl: float
    settled_at: float


class PaperBroker:
    def __init__(self):
        self.open_positions: dict[str, OpenPosition] = {}  # keyed by market_slug
        self.settled_trades: list[SettledTrade] = []

    def has_open_position(self, market_slug: str) -> bool:
        return market_slug in self.open_positions

    def open_position(
        self,
        *,
        market_slug: str,
        question: str,
        action: Action,
        entry_price: float,
        stake: float,
        end_ts: float,
        reasoning_snapshot: dict,
    ) -> OpenPosition:
        fee = stake * config.PAPER_FEE_RATE
        net_stake = max(stake - fee, 0.0)
        shares = net_stake / entry_price if entry_price > 0 else 0.0
        side = "UP" if action == Action.BUY_UP else "DOWN"
        pos = OpenPosition(
            market_slug=market_slug,
            question=question,
            side=side,
            entry_price=entry_price,
            stake=stake,
            fee_paid=fee,
            shares=shares,
            opened_at=time.time(),
            end_ts=end_ts,
            reasoning_snapshot=reasoning_snapshot,
        )
        self.open_positions[market_slug] = pos
        return pos

    def settle(self, market_slug: str, resolved_up: bool) -> SettledTrade | None:
        pos = self.open_positions.pop(market_slug, None)
        if pos is None:
            return None
        won = (pos.side == "UP") == resolved_up
        payout = pos.shares * 1.0 if won else 0.0
        pnl = payout - pos.stake
        trade = SettledTrade(position=pos, won=won, payout=payout, pnl=pnl, settled_at=time.time())
        self.settled_trades.append(trade)
        return trade

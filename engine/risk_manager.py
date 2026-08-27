"""
Position sizing and circuit breakers, applied on top of whatever the
decision engine says. This is what stands between "the model says trade"
and an order actually going out.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import config


@dataclass
class RiskManager:
    bankroll: float = config.STARTING_BANKROLL
    realized_pnl: float = 0.0
    _last_trade_ts_by_market: dict[str, float] = field(default_factory=dict)
    # NOTE: this used to default via its own `field(default_factory=lambda:
    # config.STARTING_BANKROLL)`, independent of whatever `bankroll` the
    # instance was actually constructed with. That meant RiskManager(bankroll=X)
    # for any X != config.STARTING_BANKROLL silently mis-tracked the daily
    # loss breaker from the very first tick. __post_init__ below fixes it to
    # always start from the bankroll this instance actually has.
    _day_start_bankroll: float | None = None

    def __post_init__(self):
        if self._day_start_bankroll is None:
            self._day_start_bankroll = self.bankroll

    def stake_for(self, edge: float) -> float:
        """Flat-ish sizing, lightly scaled by edge, capped both ways."""
        fraction = config.MAX_STAKE_FRACTION * min(1.0, abs(edge) / config.MIN_EDGE_TO_TRADE)
        stake = self.bankroll * fraction
        return max(0.0, min(stake, config.MAX_STAKE_USD, self.bankroll))

    def daily_loss_limit_hit(self) -> bool:
        loss = self._day_start_bankroll - self.bankroll
        return loss >= self._day_start_bankroll * config.MAX_DAILY_LOSS_FRACTION

    def cooldown_active(self, market_slug: str) -> bool:
        last = self._last_trade_ts_by_market.get(market_slug)
        return last is not None and (time.time() - last) < config.TRADE_COOLDOWN_SEC

    def can_trade(self, market_slug: str, available_liquidity_usd: float | None = None) -> tuple[bool, str]:
        if self.daily_loss_limit_hit():
            return False, "daily loss limit reached"
        if self.cooldown_active(market_slug):
            return False, "cooldown active for this market"
        if self.bankroll <= 0:
            return False, "bankroll depleted"
        # MIN_LIQUIDITY_USD defaults to 0 (gate disabled) so this preserves
        # baseline behavior unless deliberately configured. `None` (unknown
        # liquidity, e.g. no order-book snapshot yet) is never blocked by
        # this gate — only a liquidity figure we actually have and that's
        # too thin trips it.
        if (
            config.MIN_LIQUIDITY_USD > 0
            and available_liquidity_usd is not None
            and available_liquidity_usd < config.MIN_LIQUIDITY_USD
        ):
            return False, f"insufficient liquidity (${available_liquidity_usd:.2f} < ${config.MIN_LIQUIDITY_USD:.2f})"
        return True, ""

    def record_trade(self, market_slug: str):
        self._last_trade_ts_by_market[market_slug] = time.time()

    def apply_settlement(self, pnl: float):
        self.bankroll += pnl
        self.realized_pnl += pnl

    def reset_day(self):
        self._day_start_bankroll = self.bankroll

"""
Strategy variants for the A/B/C comparison. All three see the exact same
market observation on every tick (same BTC price, same order book, same
market price) — only how each turns that observation into a probability
estimate differs. Each strategy gets its own RiskManager + PaperBroker
("shadow" bankroll) so their paper P&L is directly comparable: any
difference in outcomes comes from the model, not from different markets or
different timing.

Strategy A never trades by construction (its edge relative to the market is
always exactly 0) — see config.py for why it exists anyway.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import config
from broker.paper_broker import PaperBroker
from engine.decision_engine import fair_probability_up
from engine.risk_manager import RiskManager


@dataclass
class Observation:
    """Everything every strategy needs, computed once per tick."""

    btc_price: float
    reference_price: float
    seconds_remaining: float
    sigma_per_sqrt_sec: float | None
    market_prob_up: float
    market_prob_source: str  # "live_orderbook" or "fallback_snapshot" — see main.py._maybe_decide
    orderbook_imbalance: float | None


def compute_model_prob_up(strategy_id: str, obs: Observation) -> float:
    if strategy_id == "A":
        # Baseline: "the model" is just the market's own price. Edge is 0 by
        # definition, so decide() will always return HOLD for this strategy.
        return obs.market_prob_up
    if strategy_id == "B":
        return fair_probability_up(
            current_price=obs.btc_price,
            reference_price=obs.reference_price,
            seconds_remaining=obs.seconds_remaining,
            sigma_per_sqrt_sec=obs.sigma_per_sqrt_sec,
            orderbook_imbalance=None,  # imbalance term deliberately excluded
        )
    if strategy_id == "C":
        return fair_probability_up(
            current_price=obs.btc_price,
            reference_price=obs.reference_price,
            seconds_remaining=obs.seconds_remaining,
            sigma_per_sqrt_sec=obs.sigma_per_sqrt_sec,
            orderbook_imbalance=obs.orderbook_imbalance,
        )
    raise ValueError(f"unknown strategy id: {strategy_id!r}")


@dataclass
class StrategyRunner:
    strategy_id: str
    risk: RiskManager = field(default_factory=RiskManager)
    broker: PaperBroker = field(default_factory=PaperBroker)


def build_strategy_runners() -> dict[str, StrategyRunner]:
    return {sid: StrategyRunner(sid) for sid in config.STRATEGIES}

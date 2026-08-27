"""
Deterministic decision engine — this is the "fast" layer from the
architecture: no LLM call sits on this path, because these Polymarket
windows are only 5 minutes long and every second of latency is edge given
away to faster bots.

Model: each "BTC Up or Down" market resolves based on whether BTC/USD at
expiry is above or below a reference price fixed at the market's open
("price to beat"). We treat that as a barrier problem under a simple
random-walk approximation:

    z = log(current_price / reference_price) / (sigma * sqrt(T_remaining))
    fair_prob_up = Phi(z)

where sigma is BTC's realized volatility per sqrt(second), estimated from
recent price history (quadratic variation over the lookback window), and
Phi is the standard normal CDF. This is a coarse model — no drift term, no
mean reversion, no correlation with order flow beyond the small imbalance
nudge below — deliberately kept simple and auditable rather than
curve-fit. Treat MIN_EDGE_TO_TRADE as the knob that encodes "how wrong I
think this model can be, plus fees."

The order-book imbalance term is a secondary nudge, not the primary
signal: sustained one-sided resting size can front-run where the
random-walk model has no opinion (it's directionless by construction).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

import config


class Action(str, Enum):
    BUY_UP = "BUY_UP"
    BUY_DOWN = "BUY_DOWN"
    HOLD = "HOLD"


@dataclass
class Decision:
    action: Action
    fair_prob_up: float
    market_prob_up: float
    edge: float  # RAW edge: fair_prob_up - market_prob_up, signed toward "Up". Logged for
                 # analysis; the trading decision below gates on net_edge, not this.
    net_edge: float  # edge minus the assumed cost buffer (see cost_buffer_prob). THIS is
                      # what actually gates the action — see decide().
    cost_buffer: float
    sigma_per_sqrt_sec: float | None
    seconds_remaining: float
    reason: str


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def estimate_sigma_per_sqrt_sec(returns: list[float], window_span_sec: float) -> float | None:
    """
    Realized volatility per sqrt(second), estimated as
    sqrt(sum(r_i^2) / elapsed_seconds). Using summed squared returns (a
    quadratic-variation estimator) rather than a plain std-dev makes this
    fairly insensitive to irregular sampling intervals.
    """
    if not returns or window_span_sec <= 0:
        return None
    sum_sq = sum(r * r for r in returns)
    variance_per_sec = sum_sq / window_span_sec
    if variance_per_sec <= 0:
        return None
    return math.sqrt(variance_per_sec)


def fair_probability_up(
    current_price: float,
    reference_price: float,
    seconds_remaining: float,
    sigma_per_sqrt_sec: float | None,
    orderbook_imbalance: float | None = None,
) -> float:
    if reference_price <= 0 or current_price <= 0:
        return 0.5

    seconds_remaining = max(seconds_remaining, 1.0)

    if not sigma_per_sqrt_sec or sigma_per_sqrt_sec <= 0:
        # No usable vol estimate yet — fall back to "already above/below by
        # any amount" as a weak signal, heavily shrunk toward 0.5.
        prob = 0.5 + (0.05 if current_price > reference_price else -0.05 if current_price < reference_price else 0.0)
    else:
        z = math.log(current_price / reference_price) / (sigma_per_sqrt_sec * math.sqrt(seconds_remaining))
        prob = _normal_cdf(z)

    if orderbook_imbalance is not None:
        prob += config.ORDERBOOK_IMBALANCE_WEIGHT * max(-1.0, min(1.0, orderbook_imbalance))

    return max(0.01, min(0.99, prob))


def mean_log_return(returns: list[float]) -> float | None:
    """Simple momentum indicator: mean log-return over whatever window the
    caller passed to compute `returns`. This is NOT currently used as an
    input to fair_probability_up (see module docstring: the model has no
    explicit drift term) — it's logged for analysis so a future drift term
    could be evaluated against real data rather than added on a hunch."""
    if not returns:
        return None
    return sum(returns) / len(returns)


def cost_buffer_prob(spread: float | None, fee_rate: float, extra_buffer: float = 0.0) -> float:
    """
    Approximate assumed round-trip trading cost, expressed in probability
    points so it can be compared directly against `edge`:

        half the bid-ask spread (cost of crossing to enter a taker order)
      + fee_rate (Polymarket's per-trade fee, approximated 1:1 as a
        probability-point cost — a simplification, not an exact model)
      + extra_buffer (config.ASSUMED_SLIPPAGE_BUFFER, 0 by default)

    This is an analysis-time approximation, not a precise execution-cost
    model — real slippage on a thin book can be larger than half the quoted
    spread. Treat it as a floor, not a ceiling.
    """
    spread_cost = (spread / 2.0) if spread is not None else 0.0
    return spread_cost + fee_rate + extra_buffer


def net_edge_after_costs(raw_edge: float, cost_buffer: float) -> float:
    """raw_edge shrunk toward zero by cost_buffer; the sign never flips from
    the buffer alone (a huge cost buffer just floors the magnitude at 0)."""
    magnitude = max(0.0, abs(raw_edge) - cost_buffer)
    return math.copysign(magnitude, raw_edge) if raw_edge != 0 else 0.0


def decide(
    market_prob_up: float,
    fair_prob_up: float,
    seconds_remaining: float,
    sigma_per_sqrt_sec: float | None,
    cost_buffer: float = 0.0,
) -> Decision:
    """
    Gates on NET edge (raw edge minus the assumed cost buffer — half-spread
    + fee rate + any extra slippage buffer, see cost_buffer_prob), not raw
    edge. This is a cost-accounting fix, not a strategy change: the
    threshold compared against is still exactly `config.MIN_EDGE_TO_TRADE`,
    unchanged — only what gets compared to it changed, from "does the model
    disagree with the market by enough" to "does the model disagree with
    the market by enough, AFTER what it would actually cost to act on that
    disagreement". A previous version of this bot traded on raw edge, which
    meant it could — and in testing, did — open positions where the
    assumed transaction cost had already eaten the entire edge before the
    trade was even placed.

    cost_buffer defaults to 0.0 so a caller that doesn't pass it (e.g. an
    old test) gets the pre-existing raw-edge-equivalent behavior rather
    than a silent behavior change.
    """
    edge = fair_prob_up - market_prob_up
    net_edge = net_edge_after_costs(edge, cost_buffer)

    if seconds_remaining < config.MIN_SECONDS_REMAINING_TO_TRADE:
        return Decision(
            Action.HOLD, fair_prob_up, market_prob_up, edge, net_edge, cost_buffer,
            sigma_per_sqrt_sec, seconds_remaining, reason="too close to expiry",
        )

    if net_edge >= config.MIN_EDGE_TO_TRADE:
        return Decision(
            Action.BUY_UP, fair_prob_up, market_prob_up, edge, net_edge, cost_buffer,
            sigma_per_sqrt_sec, seconds_remaining,
            reason=(f"fair {fair_prob_up:.0%} vs market {market_prob_up:.0%}, "
                    f"raw edge +{edge:.1%}, net edge +{net_edge:.1%} (after {cost_buffer:.1%} assumed cost)"),
        )
    if net_edge <= -config.MIN_EDGE_TO_TRADE:
        return Decision(
            Action.BUY_DOWN, fair_prob_up, market_prob_up, edge, net_edge, cost_buffer,
            sigma_per_sqrt_sec, seconds_remaining,
            reason=(f"fair {fair_prob_up:.0%} vs market {market_prob_up:.0%}, "
                    f"raw edge {edge:.1%}, net edge {net_edge:.1%} (after {cost_buffer:.1%} assumed cost)"),
        )
    return Decision(
        Action.HOLD, fair_prob_up, market_prob_up, edge, net_edge, cost_buffer,
        sigma_per_sqrt_sec, seconds_remaining,
        reason=f"net edge {net_edge:+.1%} below threshold ({config.MIN_EDGE_TO_TRADE:.1%}); raw edge was {edge:+.1%}",
    )

"""
Orchestrator — research-grade version. Ties together: BTC price feed ->
Polymarket market discovery -> Polymarket order-book feed -> three parallel
strategy variants (A/B/C, see engine/strategy.py) -> per-strategy risk
manager -> per-strategy paper broker -> logging DB, plus an optional
slow-timer Claude supervisor that is commentary-only.

Run with:  python main.py

This is paper-trading only (see config.LIVE_TRADING_ENABLED and README.md).
Nothing in this file places a real order or touches real funds.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from dataclasses import dataclass

import config
import storage.logging_db as db
from alerting import telegram_notify
from engine import strategy as strat
from engine.decision_engine import (
    Action,
    cost_buffer_prob,
    decide,
    estimate_sigma_per_sqrt_sec,
    mean_log_return,
)
from market_data.btc_feed import BtcPriceFeed
from polymarket import gamma_client
from polymarket.clob_ws import ClobMarketFeed, TokenState

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")

DECISION_INTERVAL_SEC = 3
SETTLEMENT_POLL_INTERVAL_SEC = 5
# Give up on ever learning a market's outcome after this long past expiry
# (official settlement not posted AND our own feed has no usable fallback
# price) rather than retrying forever.
SETTLEMENT_GIVE_UP_SEC = 300

MISSING_REFERENCE_PRICE_REASON = "missing_reference_price"


def should_warn_about_missing_market(consecutive_misses: int) -> bool:
    """
    First warning after 3 consecutive market-discovery misses (~1 minute at
    the default 20s poll interval — a brief gap between windows is normal
    for a few seconds, but not for a full minute), then a repeat every 9
    misses (~3 minutes) so a bot that's been silently finding nothing for
    a long time doesn't stay silent forever, without spamming every poll.
    Pure/testable so this doesn't require driving the actual async loop.
    """
    return consecutive_misses == 3 or (consecutive_misses > 3 and consecutive_misses % 9 == 0)


@dataclass
class BookResolution:
    """
    Pure, synchronously-testable result of resolving "what market
    probability — and which order-book states — should this tick actually
    use", honoring CLOB freshness (config.CLOB_DATA_MAX_AGE_SEC). See
    resolve_book_state() below.
    """
    market_prob_up: float | None
    market_prob_source: str | None  # "live_orderbook" | "fallback_snapshot" | None (neither available)
    market_no_price: float | None
    orderbook_imbalance: float | None
    spread: float | None
    up_state: TokenState | None  # FRESH state only — safe to use for execution pricing
    down_state: TokenState | None  # FRESH state only


def resolve_book_state(
    market_up_price: float | None,
    up_state_raw: TokenState | None,
    down_state_raw: TokenState | None,
    now: float,
    max_age_sec: float | None = None,
) -> BookResolution:
    """
    Decide what this tick's market probability actually is, honoring CLOB
    freshness. A TokenState that hasn't received an update recently enough
    (or has never received one) is treated as UNAVAILABLE, not as live
    data: its midpoint/imbalance/spread are never used, and it is never
    labeled "live_orderbook" even though a stale value technically still
    sits in memory (see TokenState.is_fresh). market_prob_up then falls back
    to the market's static discovery-time snapshot — the "existing
    legitimate fallback" — ONLY if the live book is unavailable (missing OR
    stale). If neither is available, market_prob_up and market_prob_source
    are both None and the caller must treat this tick as HOLD/skip; it must
    never trade on a stale midpoint just because a number is technically
    sitting there.

    Kept as a plain module-level function (not a Bot method) specifically
    so it's unit-testable without an event loop, a live WS connection, or a
    constructed Bot.
    """
    if max_age_sec is None:
        max_age_sec = config.CLOB_DATA_MAX_AGE_SEC

    def _fresh(state: TokenState | None) -> TokenState | None:
        if state is None or not state.is_fresh(max_age_sec, now):
            return None
        return state

    up_state = _fresh(up_state_raw)
    down_state = _fresh(down_state_raw)

    market_prob_up = up_state.midpoint() if up_state is not None else None
    market_prob_source = "live_orderbook" if market_prob_up is not None else None

    if market_prob_up is None:
        # Live book missing or stale — fall back to the static discovery-time
        # snapshot ONLY. Never to a stale live value, however recent-looking.
        market_prob_up = market_up_price
        market_prob_source = "fallback_snapshot" if market_prob_up is not None else None

    market_no_price = down_state.midpoint() if down_state is not None else None
    imbalance = up_state.imbalance() if up_state is not None else None
    spread = None
    if up_state is not None and up_state.best_bid is not None and up_state.best_ask is not None:
        spread = up_state.best_ask - up_state.best_bid

    return BookResolution(
        market_prob_up=market_prob_up,
        market_prob_source=market_prob_source,
        market_no_price=market_no_price,
        orderbook_imbalance=imbalance,
        spread=spread,
        up_state=up_state,
        down_state=down_state,
    )


def resolve_reference_price(
    market: gamma_client.MarketInfo,
    btc_feed: BtcPriceFeed,
    max_staleness_sec: float | None = None,
) -> tuple[float | None, str]:
    """
    Decide a market's reference/strike price ("price to beat") WITHOUT ever
    inventing one. Priority order:

      1. An exact structured strike from Gamma market metadata, if
         Polymarket ever exposes one — see
         gamma_client.extract_structured_strike_price for why this is a
         forward-compatible no-op today, not dead code.
      2. Our own recorded BTC price at-or-before the market's actual
         start_date, ONLY if that sample is within max_staleness_sec of
         start_date (config.REFERENCE_PRICE_MAX_STALENESS_SEC). A stale
         match here would silently model the wrong barrier problem — e.g.
         a market discovered late, or a feed outage spanning market open,
         must not quietly get "whatever price we happened to have three
         minutes ago" as its strike.
      3. Neither available -> (None, "missing_reference_price"). The caller
         must skip this market for trading, never fall back to the current
         BTC price as an invented strike.

    Returns (reference_price_or_None, reason), where reason is one of
    "structured_metadata", "historical_btc_price", or
    MISSING_REFERENCE_PRICE_REASON.
    """
    if max_staleness_sec is None:
        max_staleness_sec = config.REFERENCE_PRICE_MAX_STALENESS_SEC

    structured = gamma_client.extract_structured_strike_price(market.raw)
    if structured is not None:
        return structured, "structured_metadata"

    if market.start_date is not None:
        hit = btc_feed.price_at_or_before_with_ts(market.start_date.timestamp())
        if hit is not None:
            price, sample_ts = hit
            staleness = market.start_date.timestamp() - sample_ts
            if staleness <= max_staleness_sec:
                return price, "historical_btc_price"

    return None, MISSING_REFERENCE_PRICE_REASON


class Bot:
    def __init__(self):
        self.btc_feed = BtcPriceFeed()
        self.strategies: dict[str, strat.StrategyRunner] = strat.build_strategy_runners()

        self.current_slug: str | None = None
        self.current_market: gamma_client.MarketInfo | None = None
        self.ws_feed: ClobMarketFeed | None = None
        self.ws_task: asyncio.Task | None = None

        # Reference price ("price to beat") is per-market, not a single
        # shared value — a previous version of this bot stored it as one
        # attribute that got overwritten on every market switch, which
        # silently corrupted the feed-fallback settlement price for any
        # market other than the currently active one.
        self.reference_price_by_slug: dict[str, float] = {}

        # Every market we've observed and are waiting to learn the outcome
        # of — NOT limited to markets we traded. This is what makes
        # calibration analysis on the full decision log possible.
        self._pending_outcomes: list[gamma_client.MarketInfo] = []
        self._seen_slugs: set[str] = set()

    async def run(self):
        db.init_db()
        if config.LIVE_TRADING_ENABLED:
            logger.warning(
                "LIVE_TRADING_ENABLED is true, but this project does not implement "
                "order placement. No real orders will be sent regardless — see README."
            )
        logger.info("Strategies running in parallel (shadow bankrolls): %s", ", ".join(config.STRATEGIES))
        if config.TELEGRAM_ENABLED:
            telegram_notify.notify_started(config.STRATEGIES, config.MARKET_SLUG_CONTAINS)
        else:
            logger.info("Telegram alerting off (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to enable).")

        tasks = [
            asyncio.create_task(self.btc_feed.run()),
            asyncio.create_task(self._market_rollover_loop()),
            asyncio.create_task(self._decision_loop()),
            asyncio.create_task(self._settlement_loop()),
        ]

        if config.TELEGRAM_ENABLED:
            tasks.append(asyncio.create_task(self._telegram_status_loop()))

        if config.SUPERVISOR_ENABLED:
            import supervisor

            tasks.append(asyncio.create_task(supervisor.run_supervisor_loop()))
        else:
            logger.info("Supervisor layer off (set ANTHROPIC_API_KEY to enable).")

        await asyncio.gather(*tasks)

    async def _market_rollover_loop(self):
        consecutive_misses = 0
        while True:
            try:
                market = gamma_client.find_live_btc_updown_market(config.MARKET_SLUG_CONTAINS)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Market discovery failed, retrying")
                telegram_notify.notify_error("market discovery", str(exc), dedupe_key="market_discovery")
                await asyncio.sleep(config.MARKET_DISCOVERY_INTERVAL_SEC)
                continue

            if market is not None:
                consecutive_misses = 0
                if market.slug != self.current_slug:
                    await self._switch_market(market)
            else:
                consecutive_misses += 1
                if should_warn_about_missing_market(consecutive_misses):
                    missing_for_sec = consecutive_misses * config.MARKET_DISCOVERY_INTERVAL_SEC
                    logger.warning(
                        "No live market found matching MARKET_SLUG_CONTAINS=%r for ~%.0fs. A short gap "
                        "between windows is normal for a few seconds, but this long is not — the most "
                        "likely cause is that MARKET_SLUG_CONTAINS no longer matches Polymarket's "
                        "current slug format (it has changed before). Check a live market's actual slug "
                        "(e.g. from its Polymarket URL or the Gamma API response) rather than guessing.",
                        config.MARKET_SLUG_CONTAINS, missing_for_sec,
                    )
                    # should_warn_about_missing_market() already throttles how
                    # often this branch runs (first at 3 misses, then every 9),
                    # so this can send unconditionally rather than needing its
                    # own dedupe cooldown on top.
                    telegram_notify.send(
                        f"⚠️ <b>No live market found</b> for ~{missing_for_sec:.0f}s "
                        f"(filter <code>{config.MARKET_SLUG_CONTAINS}</code>). This is the exact "
                        f"symptom of a stale slug format — check a live market's real slug."
                    )

            await asyncio.sleep(config.MARKET_DISCOVERY_INTERVAL_SEC)

    async def _switch_market(self, market: gamma_client.MarketInfo):
        logger.info("New live market: %s (ends %s)", market.slug, market.end_date.isoformat())

        if self.current_market is not None and self.current_market.slug not in self._seen_slugs:
            # Shouldn't happen (we mark seen on entry below) but guard anyway.
            self._pending_outcomes.append(self.current_market)

        if self.ws_feed is not None:
            self.ws_feed.stop()
        if self.ws_task is not None:
            self.ws_task.cancel()

        self.current_market = market
        self.current_slug = market.slug

        if market.slug not in self._seen_slugs:
            self._seen_slugs.add(market.slug)
            self._pending_outcomes.append(market)

        ref, reason = resolve_reference_price(market, self.btc_feed)
        if ref is None:
            # NEVER fall back to the current BTC price as an invented
            # strike — that changes the mathematical problem being modeled
            # (see resolve_reference_price's docstring). Leave this slug
            # absent from reference_price_by_slug: _maybe_decide() already
            # treats a missing reference price as "skip this market", which
            # means no decisions are logged and no trade can ever be opened
            # for it, while future markets are picked up normally by the
            # rollover loop.
            logger.warning(
                "Market %s has no trustworthy reference price (%s) — skipping it for trading. "
                "Future markets are unaffected.", market.slug, reason,
            )
            self.reference_price_by_slug.pop(market.slug, None)
        else:
            logger.info("Reference price for %s: %.2f (source: %s)", market.slug, ref, reason)
            self.reference_price_by_slug[market.slug] = ref

        self.ws_feed = ClobMarketFeed([market.up_token_id, market.down_token_id])
        self.ws_task = asyncio.create_task(self.ws_feed.run())

    async def _decision_loop(self):
        while True:
            await asyncio.sleep(DECISION_INTERVAL_SEC)
            try:
                self._maybe_decide()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Decision cycle failed")
                telegram_notify.notify_error("decision cycle", str(exc), dedupe_key="decision_cycle")

    async def _telegram_status_loop(self):
        while True:
            await asyncio.sleep(config.TELEGRAM_STATUS_INTERVAL_SEC)
            try:
                self._send_status_summary()
            except Exception:  # noqa: BLE001 — a status-summary bug must never take down the bot
                logger.exception("Telegram status summary failed")

    def _send_status_summary(self):
        lines = []
        for strategy_id, runner in self.strategies.items():
            trades = db.all_trades(strategy_id)  # `trades` table = settled trades only
            wins = sum(1 for t in trades if t["won"])
            pnl = sum(t["pnl"] for t in trades if t["pnl"] is not None)
            lines.append(
                f"[{strategy_id}] {len(trades)} settled, {wins}/{len(trades)} won, "
                f"pnl ${pnl:+.2f}, bankroll ${runner.risk.bankroll:.2f}"
            )
        telegram_notify.notify_status(lines)

    def _maybe_decide(self):
        market = self.current_market
        if market is None or self.ws_feed is None:
            return
        reference_price = self.reference_price_by_slug.get(market.slug)
        if reference_price is None:
            return

        now = time.time()

        btc_price = self.btc_feed.price
        if btc_price is None:
            return
        if not self.btc_feed.is_fresh(config.BTC_DATA_MAX_AGE_SEC, now):
            # STALE BTC PRICE -> NEVER TRADE. Skip the whole tick (all
            # strategies) rather than deciding off a price that might
            # already be minutes wrong for a 5-minute market.
            age = (now - self.btc_feed.price_ts) if self.btc_feed.price_ts is not None else float("inf")
            logger.warning("BTC price feed stale (%.1fs old > %.1fs max) — holding this tick",
                            age, config.BTC_DATA_MAX_AGE_SEC)
            return

        seconds_remaining = (market.end_date - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if seconds_remaining <= 0:
            return  # rollover loop will pick this up

        up_state_raw = self.ws_feed.state.get(market.up_token_id)
        down_state_raw = self.ws_feed.state.get(market.down_token_id)

        # Resolve market probability with CLOB freshness honored — see
        # resolve_book_state()'s docstring. Never silently trade on a stale
        # midpoint, and never label stale data "live_orderbook".
        res = resolve_book_state(market.up_price, up_state_raw, down_state_raw, now)
        if res.market_prob_up is None:
            return

        returns, actual_span = self.btc_feed.recent_returns_with_span(config.VOL_LOOKBACK_SEC)
        sigma = estimate_sigma_per_sqrt_sec(returns, actual_span)
        momentum = mean_log_return(returns)

        obs = strat.Observation(
            btc_price=btc_price,
            reference_price=reference_price,
            seconds_remaining=seconds_remaining,
            sigma_per_sqrt_sec=sigma,
            market_prob_up=res.market_prob_up,
            market_prob_source=res.market_prob_source,
            orderbook_imbalance=res.orderbook_imbalance,
        )

        for strategy_id, runner in self.strategies.items():
            self._decide_and_maybe_trade(
                strategy_id, runner, market, obs, momentum, actual_span, res.spread, res.market_no_price,
                res.up_state, res.down_state,
            )

    def _decide_and_maybe_trade(self, strategy_id, runner, market, obs, momentum, vol_window_actual_sec,
                                 spread, market_no_price, up_state, down_state):
        model_prob_up = strat.compute_model_prob_up(strategy_id, obs)

        # Cost buffer must be known BEFORE deciding — decide() gates on NET
        # edge (raw edge minus this buffer), not raw edge. See decide()'s
        # own docstring for why: gating on raw edge could and did open
        # positions where the assumed transaction cost already exceeded the
        # entire predicted edge.
        cost_buffer = cost_buffer_prob(spread, config.PAPER_FEE_RATE, config.ASSUMED_SLIPPAGE_BUFFER)
        decision = decide(obs.market_prob_up, model_prob_up, obs.seconds_remaining, obs.sigma_per_sqrt_sec, cost_buffer)

        traded = False
        entry_price = None
        stake = None

        if decision.action != Action.HOLD and not runner.broker.has_open_position(market.slug):
            side_state = up_state if decision.action == Action.BUY_UP else down_state
            available_liquidity_usd = None
            if side_state and side_state.best_ask is not None and side_state.ask_size is not None:
                available_liquidity_usd = side_state.best_ask * side_state.ask_size

            can_trade, why_not = runner.risk.can_trade(market.slug, available_liquidity_usd)
            if can_trade:
                entry_price = side_state.best_ask if side_state else None
                entry_price = entry_price or (
                    obs.market_prob_up if decision.action == Action.BUY_UP else 1 - obs.market_prob_up
                )
                # Sized off NET edge, not raw edge, for the same reason
                # decide() gates on it: raw edge overstates how far past
                # the threshold a trade actually is once costs are netted out.
                stake = runner.risk.stake_for(decision.net_edge)
                if stake > 0.5:  # skip dust-sized trades
                    runner.broker.open_position(
                        market_slug=market.slug,
                        question=market.question,
                        action=decision.action,
                        entry_price=entry_price,
                        stake=stake,
                        end_ts=market.end_date.timestamp(),
                        reasoning_snapshot={
                            "strategy": strategy_id,
                            "model_prob_up": model_prob_up,
                            "market_prob_up": obs.market_prob_up,
                            "market_prob_source": obs.market_prob_source,
                            "raw_edge": decision.edge,
                            "cost_buffer": decision.cost_buffer,
                            "net_edge": decision.net_edge,
                            "sigma_per_sqrt_sec": obs.sigma_per_sqrt_sec,
                            "btc_price": obs.btc_price,
                            "reference_price": obs.reference_price,
                            "orderbook_imbalance": obs.orderbook_imbalance,
                            "spread": spread,
                            "seconds_remaining": obs.seconds_remaining,
                            "reason": decision.reason,
                        },
                    )
                    runner.risk.record_trade(market.slug)
                    traded = True
                    logger.info(
                        "TRADE[%s] %s %s stake=$%.2f entry=%.2f | %s",
                        strategy_id, market.slug, decision.action.value, stake, entry_price, decision.reason,
                    )
                    telegram_notify.notify_trade_opened(
                        strategy_id, market.slug, decision.action.value, stake, entry_price, decision.net_edge,
                    )
                else:
                    stake = None
            elif decision.action != Action.HOLD:
                logger.debug("Strategy %s signal %s suppressed: %s", strategy_id, decision.action.value, why_not)

        db.log_decision({
            "ts": time.time(),
            "strategy": strategy_id,
            "market_slug": market.slug,
            "question": market.question,
            "market_start_ts": market.start_date.timestamp() if market.start_date else None,
            "market_end_ts": market.end_date.timestamp(),
            "seconds_remaining": obs.seconds_remaining,
            "btc_price": obs.btc_price,
            "reference_price": obs.reference_price,
            "btc_momentum": momentum,
            "realized_vol": obs.sigma_per_sqrt_sec,
            "vol_window_actual_sec": vol_window_actual_sec,
            "model_prob_up": model_prob_up,
            "model_prob_down": 1 - model_prob_up,
            "market_yes_price": obs.market_prob_up,
            "market_no_price": market_no_price,
            "market_implied_prob": obs.market_prob_up,
            "market_prob_source": obs.market_prob_source,
            "raw_edge": decision.edge,
            "cost_buffer": decision.cost_buffer,
            "net_edge": decision.net_edge,
            "orderbook_imbalance": obs.orderbook_imbalance,
            "spread": spread,
            "action": decision.action.value,
            "position_size": stake,
            "entry_price": entry_price,
            "traded": int(traded),
            "reason": decision.reason,
        })

    async def _settlement_loop(self):
        while True:
            await asyncio.sleep(SETTLEMENT_POLL_INTERVAL_SEC)
            still_pending = []
            for market in self._pending_outcomes:
                self._try_resolve_market(market, still_pending)
            self._pending_outcomes = still_pending

    def _try_resolve_market(self, market: gamma_client.MarketInfo, still_pending: list):
        """
        `resolution_source` on the resulting market_outcomes row is one of:

          "gamma_official"      — Polymarket's own settled outcome, read back
                                   from the closed market's outcomePrices via
                                   the Gamma API. This IS Polymarket's actual
                                   resolution mechanism, not a proxy for it.
          "proxy_coinbase_feed" — Polymarket's own resolution hasn't posted
                                   within 30s of expiry, so we substitute
                                   "was our own Coinbase-fed BTC price above
                                   or below the reference price at expiry".
                                   This assumes our feed and Polymarket's
                                   actual settlement source agree — plausible,
                                   NOT verified — and must never be treated as
                                   equivalent to a confirmed resolution. Every
                                   consumer of this data (evaluate.py,
                                   dashboard.py) reports these two sources
                                   separately rather than pooling them.
          "unresolved_timeout"  — neither of the above arrived within
                                   SETTLEMENT_GIVE_UP_SEC; the outcome is
                                   unknown and recorded as such (resolved_up
                                   stays NULL), not guessed.
        """
        seconds_past_end = time.time() - market.end_date.timestamp()

        resolved_up = None
        source = None
        if seconds_past_end >= 0:
            # get_resolved_up_outcome() also refuses a still-live market on
            # its own now (checks the market's own endDate), so this is a
            # redundant, cheap early-out rather than the only thing standing
            # between a still-open market and a premature "resolution" — it
            # just skips a pointless network call for every pending market
            # that hasn't ended yet (this loop runs every 5s over ALL
            # pending markets, most of which are still mid-window).
            try:
                resolved_up = gamma_client.get_resolved_up_outcome(market.slug)
                if resolved_up is not None:
                    source = "gamma_official"
            except Exception as exc:  # noqa: BLE001
                logger.exception("Settlement check failed for %s", market.slug)
                telegram_notify.notify_error("settlement check", str(exc), dedupe_key="settlement_check")

        reference_price = self.reference_price_by_slug.get(market.slug)
        end_price = self.btc_feed.price_at_or_before(market.end_date.timestamp())

        if resolved_up is None and seconds_past_end > 30:
            if end_price is not None and reference_price is not None:
                resolved_up = end_price > reference_price
                source = "proxy_coinbase_feed"

        if resolved_up is None:
            if seconds_past_end > SETTLEMENT_GIVE_UP_SEC:
                logger.warning("Giving up on resolving %s after %.0fs — no official or fallback outcome available",
                                market.slug, seconds_past_end)
                db.upsert_market_outcome(
                    market_slug=market.slug, question=market.question,
                    start_ts=market.start_date.timestamp() if market.start_date else None,
                    end_ts=market.end_date.timestamp(), reference_price=reference_price,
                    resolution_btc_price=end_price, resolved_up=None, resolution_source="unresolved_timeout",
                )
                self._settle_all_strategies(market, resolved_up=None)
            else:
                still_pending.append(market)
            return

        db.upsert_market_outcome(
            market_slug=market.slug, question=market.question,
            start_ts=market.start_date.timestamp() if market.start_date else None,
            end_ts=market.end_date.timestamp(), reference_price=reference_price,
            resolution_btc_price=end_price, resolved_up=resolved_up, resolution_source=source,
        )
        self._settle_all_strategies(market, resolved_up=resolved_up)

    def _settle_all_strategies(self, market: gamma_client.MarketInfo, resolved_up: bool | None):
        for strategy_id, runner in self.strategies.items():
            if not runner.broker.has_open_position(market.slug):
                continue
            if resolved_up is None:
                # We're giving up on this market — drop the shadow position
                # without a P&L entry rather than guessing an outcome.
                runner.broker.open_positions.pop(market.slug, None)
                logger.warning("Dropped unresolved shadow position [%s] %s", strategy_id, market.slug)
                continue
            trade = runner.broker.settle(market.slug, resolved_up)
            if trade is None:
                continue
            runner.risk.apply_settlement(trade.pnl)
            snap = trade.position.reasoning_snapshot
            db.log_settled_trade(
                strategy_id, trade,
                edge_at_entry=snap.get("raw_edge"),
                net_edge_at_entry=snap.get("net_edge"),
                seconds_remaining_at_entry=snap.get("seconds_remaining"),
                orderbook_imbalance_at_entry=snap.get("orderbook_imbalance"),
            )
            logger.info(
                "SETTLED[%s] %s side=%s won=%s pnl=$%.2f bankroll=$%.2f",
                strategy_id, market.slug, trade.position.side, trade.won, trade.pnl, runner.risk.bankroll,
            )
            telegram_notify.notify_trade_settled(
                strategy_id, market.slug, trade.position.side, trade.won, trade.pnl, runner.risk.bankroll,
            )


if __name__ == "__main__":
    try:
        asyncio.run(Bot().run())
    except KeyboardInterrupt:
        pass
    except Exception:
        # Best-effort only — this can't be the reliable "the bot is down"
        # signal, since a hard kill (OOM, SIGKILL, VM reboot) never reaches
        # this line at all. That reliable signal is deploy/telegram_alert.sh,
        # run by systemd's OnFailure= independently of this process.
        logger.exception("Fatal error — bot process crashing")
        try:
            telegram_notify.send("🔴 <b>btc-polymarket-bot crashed</b> — process exiting, see logs.")
        except Exception:  # noqa: BLE001
            pass
        raise

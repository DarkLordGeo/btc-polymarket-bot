"""
Replay/backtest mode: re-runs the strategy logic over ALREADY-COLLECTED
decision snapshots, optionally with different config parameters, to answer
"what would have happened if MIN_EDGE_TO_TRADE / ORDERBOOK_IMBALANCE_WEIGHT
/ MIN_SECONDS_REMAINING_TO_TRADE had been set differently" — without needing
live network access and without touching bot_state.sqlite3.

Usage:
    python replay.py
    python replay.py --min-edge 0.08 --imbalance-weight 0.03
    python replay.py --min-edge 0.04 --min-edge 0.06 --min-edge 0.08   # sweep

WHY THIS DOES NOT HAVE LOOK-AHEAD BIAS
---------------------------------------
Every row this reads from the `decisions` table already reflects only the
BTC price, realized volatility, order-book imbalance, and market price that
were actually observed at that row's own timestamp — captured live, in
real time, by main.py. Replaying those exact rows through a different
MIN_EDGE_TO_TRADE / ORDERBOOK_IMBALANCE_WEIGHT / MIN_SECONDS_REMAINING_TO_TRADE
threshold cannot leak future information into a past decision, because we
never recompute those observation fields from anything — we reuse them
verbatim. Settlement uses each market's own eventual outcome (from
market_outcomes), which is standard backtesting practice, not look-ahead:
by definition you only ever learn how a specific 5-minute window resolved
after it ends, exactly as the live bot does.

WHAT THIS CANNOT DO — clearly-documented limitations
------------------------------------------------------
1. It reuses the STORED realized_vol / btc_momentum for each tick rather
   than recomputing volatility from raw tick history (raw BTC price history
   isn't persisted, only the derived per-tick snapshot). You cannot use
   this to test a different VOL_LOOKBACK_SEC — only parameters that can be
   applied post-hoc to an already-computed observation (edge threshold,
   imbalance weight, min-seconds-remaining, position sizing).
2. Entry price is approximated as the logged market price at decision time
   (market_yes_price / market_no_price) with NO spread-crossing simulated,
   unlike the live PaperBroker which fills at best_ask. This makes replay
   P&L friendlier/less realistic than a live paper trading run — treat it
   as directionally indicative only, never as a tighter estimate. (Trading
   IS gated on the same logged cost_buffer / net_edge the live bot used at
   that tick, matching decide()'s net-edge gating — see engine/decision_engine.py
   — but that buffer only approximates half-spread + fees; it does not
   simulate an actual fill against a moving book the way PaperBroker does.)
3. Only markets with a resolved outcome in `market_outcomes` are included.
   Markets the live bot gave up resolving (resolution_source =
   'unresolved_timeout') or hasn't resolved yet are skipped, not guessed.
4. This does NOT manufacture historical data. If you haven't run main.py
   for a while first, there is nothing to replay — it will say so and
   exit rather than inventing anything.
4.5. Settlement happens EXACTLY ONCE per market, after all of that market's
   own ticks are processed — never mid-window. An earlier version of this
   function settled inside the per-tick loop, which immediately freed
   PaperBroker.has_open_position() back to False and let a single 5-minute
   market fabricate a fresh "trade" on every subsequent tick a signal fired,
   inflating trade counts and corrupting P&L stats. See _group_rows_by_market().
5. Risk-manager state (cooldowns, daily loss breaker, bankroll) is fresh
   for each replay run, independent of whatever the live bot's state was.
"""

from __future__ import annotations

import argparse

import config
import storage.logging_db as db
from analysis.metrics import max_drawdown, profit_factor, safe_mean, safe_median
from broker.paper_broker import PaperBroker
from engine import strategy as strat
from engine.decision_engine import Action, decide
from engine.risk_manager import RiskManager


def load_replayable_rows() -> list[dict]:
    """
    One row per decision tick (using strategy='C' rows as the canonical
    source of the shared observation fields — A/B/C log identical
    btc_price/reference_price/seconds_remaining/realized_vol/orderbook_imbalance/
    market_yes_price/market_no_price for the same tick, so any one of them
    works; 'C' is picked arbitrarily), joined to each market's resolved
    outcome. Unresolved markets are excluded.
    """
    dwo = db.decisions_with_outcomes(strategy="C")
    rows = []
    for r in dwo:
        if r["outcome_resolved_up"] is None:
            continue
        if r["btc_price"] is None or r["reference_price"] is None:
            continue
        rows.append(dict(r))
    rows.sort(key=lambda r: r["ts"])
    return rows


def _group_rows_by_market(rows: list[dict]) -> dict[str, list[dict]]:
    """
    Group ticks by market_slug, preserving each market's own tick order
    (rows is already globally sorted by ts on the way in — that per-market
    order is preserved by this grouping too). This exists specifically so
    replay() can process one market's ENTIRE tick sequence before settling
    it, instead of settling mid-window — see replay()'s docstring for why
    that used to fabricate trades.
    """
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["market_slug"], []).append(row)
    return grouped


def replay(rows: list[dict], strategies: tuple[str, ...] = config.STRATEGIES) -> dict[str, dict]:
    runners = {sid: strat.StrategyRunner(sid, RiskManager(), PaperBroker()) for sid in strategies}

    for market_slug, market_rows in _group_rows_by_market(rows).items():
        # A market's outcome is constant across all of its own ticks (it's
        # joined in from market_outcomes, not per-tick data) — take it once.
        resolved_up = bool(market_rows[0]["outcome_resolved_up"])

        for row in market_rows:
            obs = strat.Observation(
                btc_price=row["btc_price"],
                reference_price=row["reference_price"],
                seconds_remaining=row["seconds_remaining"],
                sigma_per_sqrt_sec=row["realized_vol"],
                market_prob_up=row["market_yes_price"],
                market_prob_source=row["market_prob_source"] if "market_prob_source" in row.keys() else None,
                orderbook_imbalance=row["orderbook_imbalance"],
            )
            # Reuse the SAME assumed cost buffer that was logged live at this
            # tick (rather than recomputing or omitting it) so replay gates
            # on net edge exactly like the live/frozen strategy does
            # post-fix — otherwise replay would silently answer a different
            # question ("what if we ignored costs") than the live bot runs.
            cost_buffer = row["cost_buffer"] if row["cost_buffer"] is not None else 0.0

            for sid, runner in runners.items():
                if runner.broker.has_open_position(market_slug):
                    continue  # already holding a position opened on an earlier tick in this SAME window
                model_prob_up = strat.compute_model_prob_up(sid, obs)
                decision = decide(obs.market_prob_up, model_prob_up, obs.seconds_remaining, obs.sigma_per_sqrt_sec, cost_buffer)
                if decision.action == Action.HOLD:
                    continue
                can_trade, _why = runner.risk.can_trade(market_slug)
                if not can_trade:
                    continue
                entry_price = obs.market_prob_up if decision.action == Action.BUY_UP else (
                    (1 - obs.market_prob_up) if row["market_no_price"] is None else row["market_no_price"]
                )
                stake = runner.risk.stake_for(decision.net_edge)
                if stake <= 0.5:
                    continue
                runner.broker.open_position(
                    market_slug=market_slug, question=row["question"], action=decision.action,
                    entry_price=entry_price, stake=stake, end_ts=row["market_end_ts"] or 0.0,
                    reasoning_snapshot={"replay": True, "edge": decision.edge, "cost_buffer": cost_buffer,
                                        "net_edge": decision.net_edge},
                )
                runner.risk.record_trade(market_slug)

        # Settle EXACTLY ONCE, after every tick belonging to this market has
        # been processed — never mid-window. Settling inside the per-tick
        # loop above (the previous bug) immediately freed has_open_position()
        # back to False, so the very next tick of the SAME market could open
        # a brand-new position and get immediately settled again — one real
        # 5-minute market could fabricate a fresh "trade" on every tick a
        # signal fired, inflating trade counts and corrupting P&L stats.
        for sid, runner in runners.items():
            if runner.broker.has_open_position(market_slug):
                trade = runner.broker.settle(market_slug, resolved_up)
                if trade is not None:
                    runner.risk.apply_settlement(trade.pnl)

    results = {}
    for sid, runner in runners.items():
        trades = runner.broker.settled_trades
        pnls = [t.pnl for t in trades]
        wins = [t for t in trades if t.won]
        dd = max_drawdown(pnls, starting_equity=config.STARTING_BANKROLL)
        results[sid] = {
            "trades": len(trades),
            "win_rate": (len(wins) / len(trades)) if trades else None,
            "total_pnl": sum(pnls) if pnls else 0.0,
            "avg_pnl": safe_mean(pnls),
            "median_pnl": safe_median(pnls),
            "profit_factor": profit_factor(pnls) if pnls else None,
            "max_drawdown_abs": dd.max_drawdown_abs,
            "final_bankroll": runner.risk.bankroll,
        }
    return results


def fmt(results: dict[str, dict]) -> str:
    lines = [f"{'Strategy':8} {'Trades':7} {'WinRate':8} {'TotalPnL':>10} {'AvgPnL':>9} {'MedPnL':>9} {'PF':>6} {'MaxDD':>9}"]
    for sid, r in results.items():
        wr = f"{r['win_rate']:.1%}" if r["win_rate"] is not None else "n/a"
        pf = f"{r['profit_factor']:.2f}" if r["profit_factor"] not in (None,) else "n/a"
        lines.append(
            f"{sid:8} {r['trades']:7} {wr:8} {r['total_pnl']:10.2f} "
            f"{(r['avg_pnl'] or 0):9.2f} {(r['median_pnl'] or 0):9.2f} {pf:>6} {r['max_drawdown_abs']:9.2f}"
        )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--min-edge", type=float, action="append", default=None,
                         help="Override MIN_EDGE_TO_TRADE. Repeat the flag to sweep multiple values.")
    parser.add_argument("--imbalance-weight", type=float, default=None, help="Override ORDERBOOK_IMBALANCE_WEIGHT.")
    parser.add_argument("--min-seconds", type=float, default=None, help="Override MIN_SECONDS_REMAINING_TO_TRADE.")
    args = parser.parse_args()

    rows = load_replayable_rows()
    if not rows:
        print("No resolved historical decisions to replay yet. Run `python main.py` for a while first —")
        print("this script never fabricates data, so with an empty/unresolved log there is nothing to do.")
        return

    print(f"Replaying {len(rows)} resolved decision ticks "
          f"({len({r['market_slug'] for r in rows})} distinct markets)...\n")

    edge_values = args.min_edge or [config.MIN_EDGE_TO_TRADE]

    orig_min_edge = config.MIN_EDGE_TO_TRADE
    orig_imbalance = config.ORDERBOOK_IMBALANCE_WEIGHT
    orig_min_secs = config.MIN_SECONDS_REMAINING_TO_TRADE
    try:
        for edge_val in edge_values:
            config.MIN_EDGE_TO_TRADE = edge_val
            if args.imbalance_weight is not None:
                config.ORDERBOOK_IMBALANCE_WEIGHT = args.imbalance_weight
            if args.min_seconds is not None:
                config.MIN_SECONDS_REMAINING_TO_TRADE = args.min_seconds

            print(f"--- MIN_EDGE_TO_TRADE={config.MIN_EDGE_TO_TRADE:.3f}  "
                  f"ORDERBOOK_IMBALANCE_WEIGHT={config.ORDERBOOK_IMBALANCE_WEIGHT:.3f}  "
                  f"MIN_SECONDS_REMAINING_TO_TRADE={config.MIN_SECONDS_REMAINING_TO_TRADE:.0f} ---")
            results = replay(rows)
            print(fmt(results))
            print()
    finally:
        # Never leave config mutated for anything importing it afterward.
        config.MIN_EDGE_TO_TRADE = orig_min_edge
        config.ORDERBOOK_IMBALANCE_WEIGHT = orig_imbalance
        config.MIN_SECONDS_REMAINING_TO_TRADE = orig_min_secs

    print("This is a what-if replay against approximated fills (no spread-crossing simulated) — see")
    print("the module docstring for full limitations. It reports what parameters WOULD have done; it")
    print("does not change config.py, and nothing here should be treated as 'the new best setting'")
    print("without a fresh, independent paper-trading run to confirm it out of sample.")


if __name__ == "__main__":
    main()

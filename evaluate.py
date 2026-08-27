"""
Analyze collected paper-trading data. Read-only — never modifies the DB,
never changes config, never "tunes" anything. Prints a report to stdout;
pass an output path to also save it as plain text.

Usage:
    python evaluate.py
    python evaluate.py report.txt

This script measures two DIFFERENT things and keeps them separate on
purpose (see README "Separating signal quality from profitability"):

  1. SIGNAL QUALITY — is the probability model's P(Up) actually predictive?
     Measured over EVERY logged decision with a known outcome, whether or
     not that decision resulted in a trade (calibration, Brier score, edge
     vs. outcome correlation).

  2. PROFITABILITY — did the paper trades that were actually placed make
     money, before and after the assumed cost buffer? Measured only over
     the `trades` table.

A model can have real signal (1) and still be unprofitable after costs (2),
or vice versa by luck on a small sample. Don't collapse them into one number.
"""

from __future__ import annotations

import sys
from collections import defaultdict

import config
import storage.logging_db as db
from analysis.metrics import (
    bucket_index,
    bucket_label,
    brier_score,
    max_drawdown,
    pearson_corr,
    profit_factor,
    quantile_thresholds,
    regime_from_thresholds,
    safe_mean,
    safe_median,
)

OUT_LINES: list[str] = []


def out(line: str = ""):
    OUT_LINES.append(line)
    print(line)


def section(title: str):
    out()
    out(f"=== {title} ===")


def table(headers: list[str], rows: list[list[str]]):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    def fmt_row(cells):
        return "  ".join(str(c).ljust(w) for c, w in zip(cells, widths))
    out(fmt_row(headers))
    out(fmt_row(["-" * w for w in widths]))
    for row in rows:
        out(fmt_row(row))


def fnum(x, fmt="{:.3f}") -> str:
    return fmt.format(x) if x is not None else "n/a"


def fmoney(x) -> str:
    return f"${x:,.2f}" if x is not None else "n/a"


def fpct(x) -> str:
    return f"{x:.1%}" if x is not None else "n/a"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_all():
    decisions = db.all_decisions()
    trades = db.all_trades()
    outcomes = db.all_market_outcomes()
    dwo = db.decisions_with_outcomes()
    return decisions, trades, outcomes, dwo


# ---------------------------------------------------------------------------
# Section 1: overview / per-strategy summary (profitability)
# ---------------------------------------------------------------------------


def strategy_summary(trades: list, strategy: str) -> dict:
    st = [t for t in trades if t["strategy"] == strategy]
    pnls = [t["pnl"] for t in st]
    wins = [t for t in st if t["won"]]
    st_by_time = sorted(st, key=lambda t: t["settled_at"] or 0)
    dd = max_drawdown([t["pnl"] for t in st_by_time], starting_equity=config.STARTING_BANKROLL)
    return {
        "strategy": strategy,
        "total_trades": len(st),
        "win_rate": (len(wins) / len(st)) if st else None,
        "total_pnl": sum(pnls) if pnls else 0.0,
        "avg_pnl": safe_mean(pnls),
        "median_pnl": safe_median(pnls),
        "profit_factor": profit_factor(pnls) if pnls else None,
        "max_drawdown_abs": dd.max_drawdown_abs if st else None,
        "max_drawdown_pct": dd.max_drawdown_pct if st else None,
    }


# ---------------------------------------------------------------------------
# Section 2: signal quality — calibration, independent of trading
# ---------------------------------------------------------------------------


def resolved_rows(dwo: list, strategy: str | None = None) -> list:
    rows = [r for r in dwo if r["outcome_resolved_up"] is not None]
    if strategy:
        rows = [r for r in rows if r["strategy"] == strategy]
    return rows


def calibration_report(dwo: list, strategy: str) -> tuple[list[dict], float | None]:
    rows = resolved_rows(dwo, strategy)
    buckets = config.CALIBRATION_BUCKETS
    bucket_rows = [[] for _ in buckets]
    for r in rows:
        p = r["model_prob_up"]
        if p is None:
            continue
        confidence = max(p, 1 - p)
        idx = bucket_index(confidence, buckets)
        if idx is not None:
            predicted_up = p >= 0.5
            actual_up = bool(r["outcome_resolved_up"])
            bucket_rows[idx].append((confidence, predicted_up == actual_up))

    report = []
    for (lo, hi), items in zip(buckets, bucket_rows):
        n = len(items)
        hit_rate = safe_mean([1.0 if correct else 0.0 for _, correct in items]) if n else None
        avg_conf = safe_mean([c for c, _ in items]) if n else None
        report.append({"bucket": bucket_label(lo, min(hi, 1.0)), "n": n, "avg_confidence": avg_conf, "hit_rate": hit_rate})

    probs = [r["model_prob_up"] for r in rows if r["model_prob_up"] is not None]
    outcomes = [int(r["outcome_resolved_up"]) for r in rows if r["model_prob_up"] is not None]
    brier = brier_score(probs, outcomes)
    return report, brier


def edge_outcome_correlation(dwo: list, strategy: str) -> float | None:
    rows = resolved_rows(dwo, strategy)
    edges = [r["raw_edge"] for r in rows]
    outcomes = [1.0 if r["outcome_resolved_up"] else 0.0 for r in rows]
    return pearson_corr(edges, outcomes)


# ---------------------------------------------------------------------------
# Section 3: performance by bucket (edge / time / vol / imbalance) — trades only
# ---------------------------------------------------------------------------


def bucketed_trade_performance(trades: list, strategy: str, field: str, buckets: list[tuple[float, float]],
                                abs_value: bool = False) -> list[dict]:
    st = [t for t in trades if t["strategy"] == strategy]
    rows = []
    for lo, hi in buckets:
        in_bucket = []
        for t in st:
            v = t[field]
            if v is None:
                continue
            v = abs(v) if abs_value else v
            if lo <= v < hi:
                in_bucket.append(t)
        pnls = [t["pnl"] for t in in_bucket]
        wins = [t for t in in_bucket if t["won"]]
        rows.append({
            "bucket": bucket_label(lo, hi, as_pct=(field != "seconds_remaining_at_entry")),
            "n": len(in_bucket),
            "win_rate": (len(wins) / len(in_bucket)) if in_bucket else None,
            "total_pnl": sum(pnls) if pnls else 0.0,
            "avg_pnl": safe_mean(pnls),
        })
    return rows


def vol_regime_performance(decisions: list, trades: list, strategy: str) -> list[dict]:
    all_vols = [d["realized_vol"] for d in decisions if d["realized_vol"] is not None]
    cuts = quantile_thresholds(all_vols, config.VOL_REGIME_BUCKET_COUNT)
    if not cuts:
        return []
    labels = ["low", "medium", "high"][: len(cuts) + 1]

    # We need each trade's realized_vol at entry; trades table doesn't store
    # it directly, so join back to the decisions log by (market_slug, strategy,
    # traded=1) — approximate but the only link we have without adding a
    # foreign key column retroactively.
    vol_by_slug = {}
    for d in decisions:
        if d["strategy"] == strategy and d["traded"]:
            vol_by_slug[d["market_slug"]] = d["realized_vol"]

    st = [t for t in trades if t["strategy"] == strategy]
    grouped = defaultdict(list)
    for t in st:
        v = vol_by_slug.get(t["market_slug"])
        if v is None:
            continue
        grouped[regime_from_thresholds(v, cuts)].append(t)

    rows = []
    for i, label in enumerate(labels):
        group = grouped.get(i, [])
        pnls = [t["pnl"] for t in group]
        wins = [t for t in group if t["won"]]
        rows.append({
            "bucket": label,
            "n": len(group),
            "win_rate": (len(wins) / len(group)) if group else None,
            "total_pnl": sum(pnls) if pnls else 0.0,
            "avg_pnl": safe_mean(pnls),
        })
    return rows


# ---------------------------------------------------------------------------
# Section: data quality — market_prob_source and resolution_source, kept
# strictly separate from every other stat above. See README "Data quality"
# and main.py's docstrings on Observation.market_prob_source /
# _try_resolve_market for what these values mean and why they must never be
# silently pooled.
# ---------------------------------------------------------------------------


def source_breakdown_counts(rows: list, field: str) -> dict[str, int]:
    counts = defaultdict(int)
    for r in rows:
        counts[r[field] or "unknown"] += 1
    return counts


def filter_dwo_by_field(dwo: list, field: str, value: str) -> list:
    return [r for r in dwo if r[field] == value]


def trades_by_decision_field(decisions: list, trades: list, strategy: str, field: str) -> dict[str, list]:
    """
    Join each settled trade back to `field` (market_prob_source, say) as
    recorded on its OWN entry decision — the traded=1 decisions row for that
    market+strategy. `trades` doesn't store this directly, so this uses the
    same join-back-to-decisions pattern as vol_regime_performance().
    """
    value_by_slug = {}
    for d in decisions:
        if d["strategy"] == strategy and d["traded"]:
            value_by_slug[d["market_slug"]] = d[field]
    grouped = defaultdict(list)
    for t in trades:
        if t["strategy"] != strategy:
            continue
        grouped[value_by_slug.get(t["market_slug"], "unknown")].append(t)
    return grouped


def _trade_group_row(label: str, group: list) -> list:
    pnls = [t["pnl"] for t in group]
    wins = [t for t in group if t["won"]]
    return [
        label, len(group),
        fpct((len(wins) / len(group)) if group else None),
        fmoney(sum(pnls) if pnls else 0.0),
        fmoney(safe_mean(pnls)),
    ]


def data_quality_report(decisions: list, trades: list, outcomes: list, dwo: list):
    # --- market_prob_source: live order book vs. stale discovery-time fallback
    section("Data quality — market probability source (live order book vs. stale fallback)")
    prob_counts = source_breakdown_counts(dwo, "market_prob_source")
    out(f"Live order-book decisions:    {prob_counts.get('live_orderbook', 0)}")
    out(f"Fallback-snapshot decisions:  {prob_counts.get('fallback_snapshot', 0)}")
    out(f"Unknown/missing source:       {prob_counts.get('unknown', 0)}")
    out("")
    out("fallback_snapshot means the order book had no usable bid/ask at that tick, so the")
    out("decision used the market price captured once at market-discovery time, which can be")
    out("stale relative to the live market by the time the decision fires. Calibration and")
    out("trading performance below are reported SEPARATELY per source — never averaged into")
    out("one number — because a fallback-sourced 'edge' may just be measuring staleness, not a")
    out("real model/market disagreement.")

    for s in ("B", "C"):
        out(f"\nStrategy {s} — calibration by market-probability source:")
        for label in ("live_orderbook", "fallback_snapshot"):
            subset = filter_dwo_by_field(dwo, "market_prob_source", label)
            report, brier = calibration_report(subset, s)
            n_total = sum(r["n"] for r in report)
            out(f"  [{label}] n={n_total}, Brier={fnum(brier)}")

        out(f"\nStrategy {s} — trading performance by market-probability source at entry:")
        grouped = trades_by_decision_field(decisions, trades, s, "market_prob_source")
        table(
            ["Source", "n trades", "Win rate", "Total PnL", "Avg PnL"],
            [_trade_group_row(label, grouped.get(label, [])) for label in ("live_orderbook", "fallback_snapshot")],
        )

    # --- resolution_source: Polymarket's own settlement vs. our unverified proxy
    section("Data quality — market resolution source (official Polymarket vs. Coinbase-feed proxy)")
    res_counts = source_breakdown_counts(outcomes, "resolution_source")
    out(f"gamma_official (Polymarket's own settlement):       {res_counts.get('gamma_official', 0)}")
    out(f"proxy_coinbase_feed (unverified proxy assumption):  {res_counts.get('proxy_coinbase_feed', 0)}")
    out(f"unresolved_timeout (outcome unknown, not guessed):  {res_counts.get('unresolved_timeout', 0)}")
    out("")
    out("proxy_coinbase_feed assumes our own BTC feed agrees with Polymarket's actual settlement")
    out("source at expiry — plausible, NOT verified. It is never pooled with gamma_official above,")
    out("and the same discipline applies here: a large calibration gap between the two would be a")
    out("red flag about the proxy assumption itself, not just sampling noise.")

    for s in ("B", "C"):
        out(f"\nStrategy {s} — calibration by resolution source:")
        for label in ("gamma_official", "proxy_coinbase_feed"):
            subset = filter_dwo_by_field(dwo, "outcome_source", label)
            report, brier = calibration_report(subset, s)
            n_total = sum(r["n"] for r in report)
            out(f"  [{label}] n={n_total}, Brier={fnum(brier)}")

    out("")
    out("If either breakdown above shows very few live_orderbook or gamma_official decisions,")
    out("treat ANY conclusion from the rest of this report as provisional — the strategy would")
    out("mostly have been evaluated on stale prices and/or an unverified resolution proxy, not")
    out("on what it would actually see and be settled against in real conditions.")


# ---------------------------------------------------------------------------
# Main report
# ---------------------------------------------------------------------------


def main():
    decisions, trades, outcomes, dwo = load_all()

    if not decisions:
        out("No data collected yet. Run `python main.py` for a while first, then re-run this script.")
        return

    observed_slugs = {d["market_slug"] for d in decisions}
    resolved_slugs = {o["market_slug"] for o in outcomes if o["resolved_up"] is not None}
    unresolved_slugs = {o["market_slug"] for o in outcomes if o["resolved_up"] is None}

    section("Overview")
    out(f"Total markets observed:        {len(observed_slugs)}")
    out(f"Markets with a known outcome:  {len(resolved_slugs)}")
    out(f"Markets we gave up resolving:  {len(unresolved_slugs)}")
    out(f"Total decision ticks logged:   {len(decisions)}  (all strategies combined)")
    out(f"Total settled paper trades:    {len(trades)}  (all strategies combined)")
    if len(resolved_slugs) < 20:
        out("")
        out("NOTE: fewer than 20 resolved markets so far — every stat below is a very")
        out("small sample. Treat all of this as 'not yet enough data', not as a verdict.")

    # --- Data quality — read this before trusting anything below --------------
    data_quality_report(decisions, trades, outcomes, dwo)

    # --- Strategy A vs B vs C summary (profitability) ------------------------
    section("Strategy A vs B vs C — profitability summary")
    summaries = [strategy_summary(trades, s) for s in config.STRATEGIES]
    table(
        ["Strategy", "Trades", "Win rate", "Total PnL", "Avg PnL", "Median PnL", "Profit factor", "Max DD", "Max DD %"],
        [[
            s["strategy"], s["total_trades"], fpct(s["win_rate"]), fmoney(s["total_pnl"]),
            fmoney(s["avg_pnl"]), fmoney(s["median_pnl"]),
            fnum(s["profit_factor"]) if s["profit_factor"] not in (None,) else "n/a",
            fmoney(s["max_drawdown_abs"]), fpct(s["max_drawdown_pct"]),
        ] for s in summaries],
    )
    out("")
    out("Strategy A trades by construction never fire (its edge vs. the market is")
    out("always exactly 0) — its row exists to anchor the comparison, not to trade.")

    # --- Signal quality (independent of trading) -----------------------------
    section("Signal quality — does the probability model predict outcomes? (ALL logged decisions, not just trades)")
    for s in config.STRATEGIES:
        report, brier = calibration_report(dwo, s)
        out(f"\nStrategy {s} — Brier score: {fnum(brier)} (0=perfect, 0.25=coin-flip, 1=always wrong)")
        table(
            ["Confidence bucket", "n", "Avg confidence", "Empirical hit rate"],
            [[r["bucket"], r["n"], fpct(r["avg_confidence"]), fpct(r["hit_rate"])] for r in report],
        )
        corr = edge_outcome_correlation(dwo, s)
        if s == "A":
            out("Edge-vs-outcome correlation: n/a (Strategy A's edge is always 0 by construction)")
        else:
            out(f"Edge-vs-outcome correlation: {fnum(corr)} (Pearson r between raw_edge and actual Up/Down)")

    # --- Cost buffer: does edge survive costs? --------------------------------
    section("Does the strategy remain profitable after the assumed cost buffer?")
    out("The live decision rule gates on NET edge (raw edge minus the assumed half-spread + fee +")
    out("slippage buffer) vs MIN_EDGE_TO_TRADE, not on raw edge — see engine/decision_engine.py.")
    out("So every trade below should already have net_edge_at_entry > 0 by construction; any row")
    out("in 'cost buffer already ate the edge' is either a bug or a trade logged before this fix")
    out("was deployed (mixed old/new data in the same DB) — don't mix those into one run's numbers.")
    for s in ("B", "C"):
        st = [t for t in trades if t["strategy"] == s]
        cleared = [t for t in st if (t["net_edge_at_entry"] or 0) > 0]
        not_cleared = [t for t in st if (t["net_edge_at_entry"] or 0) <= 0]
        out(f"\nStrategy {s}:")
        out(f"  Trades where net edge (after fee+spread) was positive at entry: {len(cleared)}, "
            f"total pnl {fmoney(sum(t['pnl'] for t in cleared))}")
        out(f"  Trades where net edge was already <= 0 at entry (should be ~0 post-fix): {len(not_cleared)}, "
            f"total pnl {fmoney(sum(t['pnl'] for t in not_cleared))}")

    # --- Does order-book imbalance help? (B vs C) -----------------------------
    section("Does adding order-book imbalance improve performance? (Strategy B vs C, same market conditions)")
    b, c = strategy_summary(trades, "B"), strategy_summary(trades, "C")
    table(
        ["", "B (no imbalance)", "C (with imbalance)", "Delta (C - B)"],
        [
            ["Trades", b["total_trades"], c["total_trades"], c["total_trades"] - b["total_trades"]],
            ["Win rate", fpct(b["win_rate"]), fpct(c["win_rate"]),
             fpct((c["win_rate"] or 0) - (b["win_rate"] or 0)) if b["win_rate"] is not None and c["win_rate"] is not None else "n/a"],
            ["Total PnL", fmoney(b["total_pnl"]), fmoney(c["total_pnl"]), fmoney(c["total_pnl"] - b["total_pnl"])],
            ["Avg PnL/trade", fmoney(b["avg_pnl"]), fmoney(c["avg_pnl"]), "n/a"],
        ],
    )

    # --- Performance by edge / time / vol / imbalance buckets -----------------
    for s in ("B", "C"):
        section(f"Strategy {s} — performance by edge size (|edge| at entry)")
        table(
            ["Edge bucket", "n", "Win rate", "Total PnL", "Avg PnL"],
            [[r["bucket"], r["n"], fpct(r["win_rate"]), fmoney(r["total_pnl"]), fmoney(r["avg_pnl"])]
             for r in bucketed_trade_performance(trades, s, "edge_at_entry", config.EDGE_BUCKETS, abs_value=True)],
        )

        section(f"Strategy {s} — performance by time remaining at entry")
        table(
            ["Seconds-remaining bucket", "n", "Win rate", "Total PnL", "Avg PnL"],
            [[r["bucket"], r["n"], fpct(r["win_rate"]), fmoney(r["total_pnl"]), fmoney(r["avg_pnl"])]
             for r in bucketed_trade_performance(trades, s, "seconds_remaining_at_entry", config.TIME_REMAINING_BUCKETS)],
        )

        section(f"Strategy {s} — performance by realized-volatility regime (data-driven terciles)")
        vol_rows = vol_regime_performance(decisions, trades, s)
        if vol_rows:
            table(
                ["Vol regime", "n", "Win rate", "Total PnL", "Avg PnL"],
                [[r["bucket"], r["n"], fpct(r["win_rate"]), fmoney(r["total_pnl"]), fmoney(r["avg_pnl"])] for r in vol_rows],
            )
        else:
            out("Not enough distinct realized_vol samples yet to form regime buckets.")

    section("Strategy C — performance by order-book imbalance at entry")
    table(
        ["Imbalance bucket", "n", "Win rate", "Total PnL", "Avg PnL"],
        [[r["bucket"], r["n"], fpct(r["win_rate"]), fmoney(r["total_pnl"]), fmoney(r["avg_pnl"])]
         for r in bucketed_trade_performance(trades, "C", "orderbook_imbalance_at_entry", config.IMBALANCE_BUCKETS)],
    )
    out("(Strategy B's trades are excluded here since its model never uses imbalance — showing it would")
    out(" just be noise. Strategy A never trades. C is the only strategy where this bucketing is meaningful.)")

    out()
    out("Reminder: do not tune MIN_EDGE_TO_TRADE, ORDERBOOK_IMBALANCE_WEIGHT, or anything else in")
    out("config.py in response to these numbers. Report proposed changes separately from making them.")


if __name__ == "__main__":
    main()
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w") as f:
            f.write("\n".join(OUT_LINES) + "\n")
        print(f"\n(Report also written to {sys.argv[1]})")

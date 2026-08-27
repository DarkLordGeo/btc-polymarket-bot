"""
Static HTML research dashboard, generated from bot_state.sqlite3. Re-run any
time you want a fresh view — this has no server component, it's a snapshot.

Usage:  python dashboard.py [output.html]

Answers two questions:
  "Why did the bot make this trade?"      -> full decision snapshots table
  "Is the strategy actually working?"     -> equity curves, calibration,
                                              edge-vs-pnl, strategy comparison

Chart colors follow the project's validated categorical palette (3 slots,
CVD-checked both light and dark — see `node scripts/validate_palette.js`
in the dataviz skill this was built with). Strategy A = blue, B = orange,
C = aqua, consistently across every chart.
"""

from __future__ import annotations

import sys
from html import escape

import config
import storage.logging_db as db
from evaluate import (
    bucketed_trade_performance,
    calibration_report,
    edge_outcome_correlation,
    filter_dwo_by_field,
    source_breakdown_counts,
    strategy_summary,
    trades_by_decision_field,
    vol_regime_performance,
)

# --- Validated categorical palette (see dataviz skill / palette.md) --------
COLORS = {
    "A": {"light": "#2a78d6", "dark": "#3987e5"},  # blue
    "B": {"light": "#eb6834", "dark": "#d95926"},  # orange
    "C": {"light": "#1baf7a", "dark": "#199e70"},  # aqua
}
STRATEGY_NAMES = {"A": "A — market baseline", "B": "B — statistical model", "C": "C — model + imbalance"}


def _fmt_pct(x) -> str:
    return f"{x:.1%}" if x is not None else "—"


def _fmt_money(x) -> str:
    return f"${x:,.2f}" if x is not None else "—"


def _fmt_num(x, nd=3) -> str:
    return f"{x:.{nd}f}" if x is not None else "—"


# ---------------------------------------------------------------------------
# Minimal inline-SVG chart toolkit — no external JS/CSS libraries, so this
# stays a self-contained file you can just open. Hover tooltips use native
# SVG <title> elements (no JS needed) rather than a full crosshair layer;
# this is a research/diagnostic report, not a polished product surface.
# ---------------------------------------------------------------------------

CHART_CSS = """
.chart { font-family: -apple-system, "Segoe UI", sans-serif; }
.chart .axis { stroke: var(--baseline); stroke-width: 1; }
.chart .grid { stroke: var(--gridline); stroke-width: 1; }
.chart .tick { fill: var(--muted); font-size: 10px; }
.chart .legend-text { fill: var(--text-secondary); font-size: 11px; }
.chart .ref-line { stroke: var(--muted); stroke-width: 1; stroke-dasharray: 4 3; }
.chart .zero-line { stroke: var(--baseline); stroke-width: 1.5; }
"""


def _svg_open(width: int, height: int) -> str:
    return f'<svg class="chart" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">'


def _legend(names_colors: list[tuple[str, str]], x: int, y: int) -> str:
    parts = []
    cx = x
    for name, color in names_colors:
        parts.append(f'<rect x="{cx}" y="{y}" width="10" height="10" rx="2" fill="{color}"/>')
        parts.append(f'<text x="{cx + 14}" y="{y + 9}" class="legend-text">{escape(name)}</text>')
        cx += 16 + 8 * len(name) + 18
    return "".join(parts)


def line_chart(named_series: list[tuple[str, str, list[float]]], *, width=640, height=260,
               value_fmt=_fmt_money, title="") -> str:
    """named_series: [(name, color, [y0, y1, ...]), ...], all same length, x = index."""
    pad_l, pad_r, pad_t, pad_b = 46, 16, 24, 44
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b

    all_y = [v for _, _, ys in named_series for v in ys]
    if not all_y:
        return f'{_svg_open(width, 80)}<text x="10" y="30" class="tick">No data yet.</text></svg>'
    y_min, y_max = min(all_y + [0.0]), max(all_y + [0.0])
    if y_min == y_max:
        y_min, y_max = y_min - 1, y_max + 1
    y_span = y_max - y_min
    n = max(len(ys) for _, _, ys in named_series if ys) if any(ys for _, _, ys in named_series) else 1
    n = max(n, 2)

    def px(i):
        return pad_l + (i / (n - 1)) * plot_w if n > 1 else pad_l

    def py(v):
        return pad_t + plot_h - ((v - y_min) / y_span) * plot_h

    svg = [_svg_open(width, height)]
    if title:
        svg.append(f'<text x="{pad_l}" y="16" class="legend-text" font-weight="600">{escape(title)}</text>')

    # gridlines + y labels (5 ticks)
    for k in range(5):
        v = y_min + y_span * k / 4
        y = py(v)
        svg.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" class="grid"/>')
        svg.append(f'<text x="{pad_l - 6}" y="{y + 3:.1f}" text-anchor="end" class="tick">{value_fmt(v)}</text>')
    if y_min <= 0 <= y_max:
        svg.append(f'<line x1="{pad_l}" y1="{py(0):.1f}" x2="{width - pad_r}" y2="{py(0):.1f}" class="zero-line"/>')

    for name, color, ys in named_series:
        if not ys:
            continue
        pts = [(px(i), py(v)) for i, v in enumerate(ys)]
        path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(pts))
        svg.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2"/>')
        # sparse markers with tooltips (start, end, and up to ~8 in between)
        step = max(1, len(pts) // 8)
        for i in range(0, len(pts), step):
            x, y = pts[i]
            svg.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}">'
                       f'<title>{escape(name)} #{i + 1}: {value_fmt(ys[i])}</title></circle>')
        x, y = pts[-1]
        svg.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}">'
                   f'<title>{escape(name)} #{len(ys)}: {value_fmt(ys[-1])}</title></circle>')

    svg.append(_legend([(name, color) for name, color, ys in named_series if ys], pad_l, height - 14))
    svg.append("</svg>")
    return "".join(svg)


def grouped_bar_chart(categories: list[str], named_series: list[tuple[str, str, list[float]]], *,
                       width=640, height=260, value_fmt=_fmt_money, title="") -> str:
    pad_l, pad_r, pad_t, pad_b = 46, 16, 24, 50
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    all_v = [v for _, _, vs in named_series for v in vs if v is not None]
    if not all_v or not categories:
        return f'{_svg_open(width, 80)}<text x="10" y="30" class="tick">No data yet.</text></svg>'
    y_min, y_max = min(all_v + [0.0]), max(all_v + [0.0])
    if y_min == y_max:
        y_min, y_max = y_min - 1, y_max + 1
    y_span = y_max - y_min

    def py(v):
        return pad_t + plot_h - ((v - y_min) / y_span) * plot_h

    n_cat = len(categories)
    n_series = len(named_series)
    group_w = plot_w / n_cat
    bar_w = max(4, (group_w * 0.7) / max(n_series, 1))

    svg = [_svg_open(width, height)]
    if title:
        svg.append(f'<text x="{pad_l}" y="16" class="legend-text" font-weight="600">{escape(title)}</text>')
    for k in range(5):
        v = y_min + y_span * k / 4
        y = py(v)
        svg.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" class="grid"/>')
        svg.append(f'<text x="{pad_l - 6}" y="{y + 3:.1f}" text-anchor="end" class="tick">{value_fmt(v)}</text>')
    zero_y = py(0) if y_min <= 0 <= y_max else pad_t + plot_h
    svg.append(f'<line x1="{pad_l}" y1="{zero_y:.1f}" x2="{width - pad_r}" y2="{zero_y:.1f}" class="zero-line"/>')

    for ci, cat in enumerate(categories):
        group_x = pad_l + ci * group_w
        for si, (name, color, vs) in enumerate(named_series):
            v = vs[ci] if ci < len(vs) else None
            if v is None:
                continue
            bx = group_x + group_w * 0.15 + si * bar_w
            y = py(v)
            bar_top, bar_h = (min(y, zero_y), abs(zero_y - y))
            svg.append(f'<rect x="{bx:.1f}" y="{bar_top:.1f}" width="{bar_w - 2:.1f}" height="{bar_h:.1f}" '
                       f'rx="2" fill="{color}"><title>{escape(name)} / {escape(cat)}: {value_fmt(v)}</title></rect>')
        svg.append(f'<text x="{group_x + group_w / 2:.1f}" y="{height - pad_b + 16}" text-anchor="middle" '
                   f'class="tick">{escape(cat)}</text>')

    svg.append(_legend([(name, color) for name, color, _ in named_series], pad_l, height - 18))
    svg.append("</svg>")
    return "".join(svg)


def scatter_chart(named_series: list[tuple[str, str, list[tuple[float, float]]]], *, width=640, height=280,
                   x_fmt=_fmt_pct, y_fmt=_fmt_money, title="", x_label="", y_label="") -> str:
    pad_l, pad_r, pad_t, pad_b = 50, 16, 24, 46
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    all_pts = [p for _, _, pts in named_series for p in pts]
    if not all_pts:
        return f'{_svg_open(width, 80)}<text x="10" y="30" class="tick">No data yet.</text></svg>'
    xs, ys = [p[0] for p in all_pts], [p[1] for p in all_pts]
    x_min, x_max = min(xs + [0.0]), max(xs + [0.0])
    y_min, y_max = min(ys + [0.0]), max(ys + [0.0])
    if x_min == x_max:
        x_min, x_max = x_min - 1, x_max + 1
    if y_min == y_max:
        y_min, y_max = y_min - 1, y_max + 1

    def px(v):
        return pad_l + (v - x_min) / (x_max - x_min) * plot_w

    def py(v):
        return pad_t + plot_h - (v - y_min) / (y_max - y_min) * plot_h

    svg = [_svg_open(width, height)]
    if title:
        svg.append(f'<text x="{pad_l}" y="16" class="legend-text" font-weight="600">{escape(title)}</text>')
    for k in range(5):
        yv = y_min + (y_max - y_min) * k / 4
        y = py(yv)
        svg.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" class="grid"/>')
        svg.append(f'<text x="{pad_l - 6}" y="{y + 3:.1f}" text-anchor="end" class="tick">{y_fmt(yv)}</text>')
    if x_min <= 0 <= x_max:
        svg.append(f'<line x1="{px(0):.1f}" y1="{pad_t}" x2="{px(0):.1f}" y2="{pad_t + plot_h}" class="ref-line"/>')
    if y_min <= 0 <= y_max:
        svg.append(f'<line x1="{pad_l}" y1="{py(0):.1f}" x2="{width - pad_r}" y2="{py(0):.1f}" class="zero-line"/>')

    for name, color, pts in named_series:
        for x, y in pts:
            cx, cy = px(x), py(y)
            svg.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.5" fill="{color}" fill-opacity="0.75">'
                       f'<title>{escape(name)}: edge {x_fmt(x)}, pnl {y_fmt(y)}</title></circle>')

    svg.append(_legend([(name, color) for name, color, pts in named_series if pts], pad_l, height - 14))
    svg.append("</svg>")
    return "".join(svg)


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------


def equity_curve_section() -> str:
    trades = db.all_trades()
    series = []
    for sid in config.STRATEGIES:
        st = sorted([t for t in trades if t["strategy"] == sid], key=lambda t: t["settled_at"] or 0)
        cum, curve = 0.0, []
        for t in st:
            cum += t["pnl"]
            curve.append(cum)
        series.append((f"Strategy {sid}", COLORS[sid]["light"], curve))
    return line_chart(series, title="Cumulative paper P&L by strategy (trade sequence, not wall-clock time)")


def win_loss_section() -> str:
    trades = db.all_trades()
    cats = ["Wins", "Losses"]
    series = []
    for sid in config.STRATEGIES:
        st = [t for t in trades if t["strategy"] == sid]
        wins = sum(1 for t in st if t["won"])
        losses = sum(1 for t in st if not t["won"])
        series.append((f"Strategy {sid}", COLORS[sid]["light"], [wins, losses]))
    return grouped_bar_chart(cats, series, value_fmt=lambda v: f"{v:.0f}", title="Win / loss counts by strategy")


def calibration_section() -> str:
    dwo = db.decisions_with_outcomes()
    lines = []
    for sid in config.STRATEGIES:
        report, brier = calibration_report(dwo, sid)
        lines.append(f"<h3>Strategy {sid} <span class='muted-inline'>(Brier score: {_fmt_num(brier)})</span></h3>")
        rows = "".join(
            f"<tr><td>{r['bucket']}</td><td>{r['n']}</td><td>{_fmt_pct(r['avg_confidence'])}</td>"
            f"<td>{_fmt_pct(r['hit_rate'])}</td></tr>"
            for r in report
        )
        lines.append(
            "<table><tr><th>Confidence bucket</th><th>n</th><th>Avg confidence</th>"
            f"<th>Empirical hit rate</th></tr>{rows}</table>"
        )
    return "".join(lines)


def edge_vs_pnl_section() -> str:
    trades = db.all_trades()
    series = []
    for sid in ("B", "C"):
        st = [t for t in trades if t["strategy"] == sid]
        pts = [(t["edge_at_entry"], t["pnl"]) for t in st if t["edge_at_entry"] is not None]
        series.append((f"Strategy {sid}", COLORS[sid]["light"], pts))
    return scatter_chart(series, title="Edge at entry vs. realized P&L (Strategy A excluded — never trades)",
                          x_label="edge", y_label="pnl")


def time_remaining_section() -> str:
    trades = db.all_trades()
    labels = [f"{lo}-{hi}s" for lo, hi in config.TIME_REMAINING_BUCKETS]
    series = []
    for sid in ("B", "C"):
        rows = bucketed_trade_performance(trades, sid, "seconds_remaining_at_entry", config.TIME_REMAINING_BUCKETS)
        series.append((f"Strategy {sid}", COLORS[sid]["light"], [r["avg_pnl"] or 0.0 for r in rows]))
    return grouped_bar_chart(labels, series, title="Avg P&L/trade by time remaining at entry")


def vol_regime_section(decisions) -> str:
    trades = db.all_trades()
    series = []
    labels = ["low", "medium", "high"]
    for sid in ("B", "C"):
        rows = vol_regime_performance(decisions, trades, sid)
        vals = {r["bucket"]: (r["avg_pnl"] or 0.0) for r in rows}
        series.append((f"Strategy {sid}", COLORS[sid]["light"], [vals.get(l, 0.0) for l in labels]))
    return grouped_bar_chart(labels, series, title="Avg P&L/trade by realized-volatility regime (data-driven terciles)")


def _source_calibration_table(dwo, field: str, labels: list[str], strategies: tuple[str, ...] = ("B", "C")) -> str:
    lines = []
    for sid in strategies:
        row_htmls = []
        for label in labels:
            report, brier = calibration_report(filter_dwo_by_field(dwo, field, label), sid)
            n = sum(r["n"] for r in report)
            row_htmls.append(f"<tr><td>{escape(label)}</td><td>{n}</td><td>{_fmt_num(brier)}</td></tr>")
        lines.append(
            f"<h3>Strategy {sid}</h3><table><tr><th>Source</th><th>n</th><th>Brier score</th></tr>"
            f"{''.join(row_htmls)}</table>"
        )
    return "".join(lines)


def _source_trade_perf_table(decisions, trades, field: str, labels: list[str],
                              strategies: tuple[str, ...] = ("B", "C")) -> str:
    lines = []
    for sid in strategies:
        grouped = trades_by_decision_field(decisions, trades, sid, field)
        rows = []
        for label in labels:
            group = grouped.get(label, [])
            pnls = [t["pnl"] for t in group]
            wins = [t for t in group if t["won"]]
            win_rate = (len(wins) / len(group)) if group else None
            total_pnl = sum(pnls) if pnls else 0.0
            avg_pnl = (sum(pnls) / len(pnls)) if pnls else None
            rows.append(
                f"<tr><td>{escape(label)}</td><td>{len(group)}</td><td>{_fmt_pct(win_rate)}</td>"
                f"<td class=\"{'pos' if total_pnl >= 0 else 'neg'}\">{_fmt_money(total_pnl)}</td>"
                f"<td>{_fmt_money(avg_pnl)}</td></tr>"
            )
        lines.append(
            f"<h3>Strategy {sid}</h3><table><tr><th>Source</th><th>n trades</th><th>Win rate</th>"
            f"<th>Total P&amp;L</th><th>Avg P&amp;L</th></tr>{''.join(rows)}</table>"
        )
    return "".join(lines)


def data_quality_section(decisions, trades, outcomes, dwo) -> str:
    """
    Fix 2 / Fix 3's whole point made visible: never let a reader of this
    dashboard mistake a fallback-priced or proxy-resolved decision for a
    live/officially-resolved one. Every number here is reported per source,
    never pooled. See main.py's docstrings on Observation.market_prob_source
    and _try_resolve_market for what each label means.
    """
    prob_counts = source_breakdown_counts(dwo, "market_prob_source")
    res_counts = source_breakdown_counts(outcomes, "resolution_source")

    prob_labels = ["live_orderbook", "fallback_snapshot"]
    res_labels = ["gamma_official", "proxy_coinbase_feed"]

    prob_chart = grouped_bar_chart(
        ["decisions"],
        [(label, COLORS["A"]["light"] if label == "live_orderbook" else COLORS["B"]["light"],
          [prob_counts.get(label, 0)]) for label in prob_labels],
        value_fmt=lambda v: f"{v:.0f}", title="Decisions by market-probability source",
    )
    res_chart = grouped_bar_chart(
        ["markets"],
        [(label, COLORS["C"]["light"] if label == "gamma_official" else COLORS["B"]["light"],
          [res_counts.get(label, 0)]) for label in res_labels],
        value_fmt=lambda v: f"{v:.0f}", title="Resolved markets by resolution source",
    )

    return f"""
<div class="overview">
  <div>Live order-book decisions: <b>{prob_counts.get('live_orderbook', 0)}</b></div>
  <div>Fallback-snapshot decisions: <b>{prob_counts.get('fallback_snapshot', 0)}</b></div>
  <div>gamma_official resolutions: <b>{res_counts.get('gamma_official', 0)}</b></div>
  <div>proxy_coinbase_feed resolutions: <b>{res_counts.get('proxy_coinbase_feed', 0)}</b></div>
  <div>unresolved_timeout: <b>{res_counts.get('unresolved_timeout', 0)}</b></div>
</div>
<p class="muted-inline">fallback_snapshot means the order book had no usable bid/ask at that tick, so the
market price used was the one captured once at market-discovery time — possibly stale. proxy_coinbase_feed
means Polymarket's own resolution hadn't posted within 30s of expiry, so our own BTC feed's price vs. the
reference price was substituted — a plausible but unverified assumption. Neither is pooled with its
"real" counterpart anywhere on this page or in evaluate.py.</p>

<h3>Decisions / resolutions by source</h3>
{prob_chart}
{res_chart}

<h3>Calibration by market-probability source</h3>
{_source_calibration_table(dwo, "market_prob_source", prob_labels)}

<h3>Trading performance by market-probability source at entry</h3>
{_source_trade_perf_table(decisions, trades, "market_prob_source", prob_labels)}

<h3>Calibration by resolution source</h3>
{_source_calibration_table(dwo, "outcome_source", res_labels)}

<p class="muted-inline">If either breakdown above shows very few live_orderbook or gamma_official rows,
treat every other section on this page as provisional — most of the evaluation would have run on stale
prices and/or an unverified resolution proxy, not on what the strategy would actually see and be settled
against in real conditions.</p>
"""


def strategy_table_section(trades) -> str:
    rows = []
    for sid in config.STRATEGIES:
        s = strategy_summary(trades, sid)
        rows.append(
            f"<tr><td>{STRATEGY_NAMES[sid]}</td><td>{s['total_trades']}</td>"
            f"<td>{_fmt_pct(s['win_rate'])}</td>"
            f"<td class=\"{'pos' if s['total_pnl'] >= 0 else 'neg'}\">{_fmt_money(s['total_pnl'])}</td>"
            f"<td>{_fmt_money(s['avg_pnl'])}</td><td>{_fmt_money(s['median_pnl'])}</td>"
            f"<td>{_fmt_num(s['profit_factor'], 2) if s['profit_factor'] not in (None,) else '—'}</td>"
            f"<td>{_fmt_money(s['max_drawdown_abs'])}</td></tr>"
        )
    return (
        "<table><tr><th>Strategy</th><th>Trades</th><th>Win rate</th><th>Total P&amp;L</th>"
        f"<th>Avg P&amp;L</th><th>Median P&amp;L</th><th>Profit factor</th><th>Max drawdown</th></tr>{''.join(rows)}</table>"
    )


def decisions_table_section(decisions) -> str:
    rows = []
    for d in decisions:
        rows.append(
            f"<tr class=\"{'traded' if d['traded'] else ''}\">"
            f"<td>{escape(d['strategy'] or '')}</td><td>{escape(d['market_slug'])}</td>"
            f"<td>{d['action']}</td><td>{_fmt_pct(d['model_prob_up'])}</td>"
            f"<td>{_fmt_pct(d['market_implied_prob'])}</td><td>{_fmt_pct(d['raw_edge'])}</td>"
            f"<td>{_fmt_pct(d['net_edge'])}</td>"
            f"<td>{d['seconds_remaining']:.0f}s</td><td>{'✓' if d['traded'] else ''}</td>"
            f"<td>{escape(d['reason'] or '')}</td></tr>"
        )
    return (
        "<table><tr><th>Strategy</th><th>Market</th><th>Action</th><th>Model P(Up)</th>"
        "<th>Market P(Up)</th><th>Raw edge</th><th>Net edge</th><th>Time left</th><th>Traded</th>"
        f"<th>Reason</th></tr>{''.join(rows)}</table>"
    )


def trades_table_section(trades) -> str:
    rows = []
    cum_by_strategy = {sid: 0.0 for sid in config.STRATEGIES}
    for t in sorted(trades, key=lambda t: t["settled_at"] or 0):
        cum_by_strategy[t["strategy"]] += t["pnl"]
        rows.append(
            f"<tr><td>{escape(t['strategy'] or '')}</td><td>{escape(t['market_slug'])}</td><td>{t['side']}</td>"
            f"<td>{_fmt_pct(t['entry_price'])}</td><td>{_fmt_money(t['stake'])}</td>"
            f"<td>{'YES' if t['won'] else 'no'}</td>"
            f"<td class=\"{'pos' if t['pnl'] >= 0 else 'neg'}\">{_fmt_money(t['pnl'])}</td>"
            f"<td>{_fmt_pct(t['edge_at_entry'])}</td><td>{_fmt_pct(t['net_edge_at_entry'])}</td>"
            f"<td class=\"{'pos' if cum_by_strategy[t['strategy']] >= 0 else 'neg'}\">{_fmt_money(cum_by_strategy[t['strategy']])}</td></tr>"
        )
    return (
        "<table><tr><th>Strategy</th><th>Market</th><th>Side</th><th>Entry</th><th>Stake</th><th>Won?</th>"
        "<th>P&amp;L</th><th>Edge@entry</th><th>NetEdge@entry</th>"
        f"<th>Cum P&amp;L (that strategy)</th></tr>{''.join(rows)}</table>"
    )


def build_html() -> str:
    decisions = db.all_decisions()
    trades = db.all_trades()
    outcomes = db.all_market_outcomes()
    dwo = db.decisions_with_outcomes()
    notes = db.all_supervisor_notes(20)

    observed = len({d["market_slug"] for d in decisions})
    resolved = len({o["market_slug"] for o in outcomes if o["resolved_up"] is not None})

    note_rows = "".join(f"<div class='note'>{escape(n['note'])}</div>" for n in notes)
    no_notes_msg = "<p>No supervisor notes (supervisor disabled or hasn't run yet).</p>"

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>BTC / Polymarket bot — research dashboard</title>
<style>
:root {{
  color-scheme: light;
  --surface-1: #fcfcfb; --page: #f9f9f7; --text-primary: #0b0b0b; --text-secondary: #52514e;
  --muted: #898781; --gridline: #e1e0d9; --baseline: #c3c2b7; --pos: #006300; --neg: #d03b3b;
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) {{
    color-scheme: dark;
    --surface-1: #1a1a19; --page: #0d0d0d; --text-primary: #ffffff; --text-secondary: #c3c2b7;
    --muted: #898781; --gridline: #2c2c2a; --baseline: #383835; --pos: #0ca30c; --neg: #e66767;
  }}
}}
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, "Segoe UI", sans-serif; max-width: 1080px; margin: 2rem auto; padding: 0 1rem;
       color: var(--text-primary); background: var(--page); }}
h1 {{ font-size: 1.3rem; }}
h2 {{ font-size: 1.05rem; margin-top: 2.5rem; border-bottom: 1px solid var(--gridline); padding-bottom: 0.3rem; }}
h3 {{ font-size: 0.92rem; margin-top: 1.2rem; }}
.muted-inline {{ color: var(--text-secondary); font-weight: 400; font-size: 0.85rem; }}
table {{ border-collapse: collapse; width: 100%; font-size: 0.82rem; margin-bottom: 0.5rem; background: var(--surface-1); }}
th, td {{ border-bottom: 1px solid var(--gridline); padding: 4px 8px; text-align: left; color: var(--text-primary); }}
th {{ color: var(--text-secondary); font-weight: 600; }}
tr.traded {{ background: rgba(235, 104, 52, 0.08); }}
.pos {{ color: var(--pos); }}
.neg {{ color: var(--neg); }}
.note {{ background: var(--surface-1); border: 1px solid var(--gridline); border-radius: 8px; padding: 0.75rem 1rem;
         margin-bottom: 0.75rem; font-size: 0.9rem; }}
.disclaimer {{ font-size: 0.8rem; color: var(--text-secondary); margin-top: 3rem; border-top: 1px solid var(--gridline);
               padding-top: 1rem; }}
.scroll {{ overflow-x: auto; }}
.overview {{ display: flex; gap: 1.5rem; flex-wrap: wrap; font-size: 0.85rem; color: var(--text-secondary); }}
.overview b {{ color: var(--text-primary); }}
{CHART_CSS}
.chart .tick, .chart .legend-text {{ fill: var(--text-secondary); }}
.chart .grid {{ stroke: var(--gridline); }}
.chart .axis, .chart .zero-line {{ stroke: var(--baseline); }}
.chart .ref-line {{ stroke: var(--muted); }}
</style></head>
<body>
<h1>BTC / Polymarket paper-trading bot — research dashboard</h1>
<p class="muted-inline">Snapshot generated from bot_state.sqlite3. Re-run <code>python dashboard.py</code> for a fresh view.
Cross-check anything here against <code>python evaluate.py</code>, which computes the same numbers as plain text.</p>

<div class="overview">
  <div>Markets observed: <b>{observed}</b></div>
  <div>Markets resolved: <b>{resolved}</b></div>
  <div>Decision ticks logged: <b>{len(decisions)}</b></div>
  <div>Settled trades: <b>{len(trades)}</b></div>
</div>

<h2>Data quality — market probability &amp; resolution source</h2>
{data_quality_section(decisions, trades, outcomes, dwo)}

<h2>Strategy A vs B vs C — summary</h2>
{strategy_table_section(trades)}

<h2>Cumulative paper P&amp;L (equity curve)</h2>
{equity_curve_section()}

<h2>Win / loss distribution</h2>
{win_loss_section()}

<h2>Calibration — predicted probability vs. actual outcome (ALL logged decisions, not just trades)</h2>
{calibration_section()}

<h2>Edge at entry vs. realized P&amp;L</h2>
{edge_vs_pnl_section()}

<h2>Performance by time remaining at entry</h2>
{time_remaining_section()}

<h2>Performance by realized-volatility regime</h2>
{vol_regime_section(decisions)}

<h2>Settled trades (full detail)</h2>
<div class="scroll">{trades_table_section(trades)}</div>

<h2>Recent decisions — full snapshots ("why did the bot bet that")</h2>
<div class="scroll">{decisions_table_section(db.recent_decisions(300))}</div>

<h2>Supervisor notes</h2>
{note_rows or no_notes_msg}

<div class="disclaimer">Paper trading only — no real funds involved. This is a research report, not a
recommendation: a small sample, especially early on, says very little about whether any of this survives
real fees, slippage, and competition from other bots. Not financial advice.</div>
</body></html>"""


if __name__ == "__main__":
    out_path = sys.argv[1] if len(sys.argv) > 1 else "dashboard.html"
    with open(out_path, "w") as f:
        f.write(build_html())
    print(f"Wrote {out_path}")

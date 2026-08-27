"""
Optional higher-level reasoning layer. This is deliberately NOT on the
hot path — it runs on a slow timer (config.SUPERVISOR_INTERVAL_SEC),
reviews a batch of recent decisions/trades ACROSS all three strategy
variants (A/B/C — see engine/strategy.py), and writes back a plain-English
note (stored in supervisor_notes) about things like:

  - does the recent win rate look consistent with each strategy's own edge
    estimates, or is something off (fees, a stale vol estimate, a feed
    glitch)?
  - anything anomalous in how often a strategy is trading, or in the
    direction bias of its trades?
  - does the B vs C comparison (imbalance on/off) look like it's telling
    you anything yet, or is it still noise?
  - possible bugs worth investigating, hypotheses worth testing.

Hard boundary, unchanged from the single-strategy version: this function
can only ever call log_supervisor_note(). It has no access to config, no
access to any RiskManager/PaperBroker/StrategyRunner instance, and no
return value that main.py reads — there is no code path by which its
output can place a trade, approve/reject a trade, change a config
parameter, or alter the live decision engine. Treat its notes as
commentary to read later, not as a signal the bot itself acts on.
Requires ANTHROPIC_API_KEY; no-ops cleanly if unset.
"""

from __future__ import annotations

import asyncio
import logging

import config
import storage.logging_db as db

logger = logging.getLogger(__name__)


def _build_prompt() -> str | None:
    decisions = db.recent_decisions(config.SUPERVISOR_LOOKBACK_DECISIONS * len(config.STRATEGIES))
    trades = db.all_trades()
    if not decisions:
        return None

    lines = ["Recent decisions across all strategies (most recent first):"]
    for d in decisions:
        lines.append(
            f"- [{d['strategy']}] {d['market_slug']}: action={d['action']} "
            f"model_p_up={d['model_prob_up']:.2f} market_p_up={d['market_implied_prob']:.2f} "
            f"raw_edge={d['raw_edge']:+.2f} net_edge={d['net_edge']:+.2f} "
            f"vol={d['realized_vol']} reason=\"{d['reason']}\" traded={bool(d['traded'])}"
        )

    recent_trades = trades[-config.SUPERVISOR_LOOKBACK_DECISIONS * len(config.STRATEGIES):]
    if recent_trades:
        lines.append("\nRecently settled paper trades (all strategies):")
        for t in recent_trades:
            lines.append(
                f"- [{t['strategy']}] {t['market_slug']} side={t['side']} entry={t['entry_price']:.2f} "
                f"stake=${t['stake']:.2f} won={bool(t['won'])} pnl=${t['pnl']:+.2f} "
                f"net_edge_at_entry={t['net_edge_at_entry']}"
            )

    lines.append(
        "\nYou are a supervisory analyst for an automated PAPER-trading (no real money) bot running "
        "three strategy variants in parallel on Polymarket's 5-minute 'BTC Up or Down' markets: "
        "A = market-price baseline (never trades, by construction), B = a statistical model, "
        "C = the same model plus an order-book-imbalance nudge. The fast decision engine already "
        "acted on the above; it does not wait for you, and nothing you say here changes what it does "
        "next — you are commentary for a human to read later, not a control input. In 4-6 sentences: "
        "does the recent behavior look sane, is there anything anomalous (e.g. persistent one-sided "
        "bias, a suspiciously high trade rate, wins/losses inconsistent with the stated edges, B and C "
        "diverging in a surprising way), and what's one concrete hypothesis worth investigating? Be "
        "concrete and skeptical rather than reassuring. Do not propose a specific new parameter value — "
        "flag what looks worth investigating and let a human decide what, if anything, to change."
    )
    return "\n".join(lines)


async def run_supervisor_loop():
    if not config.SUPERVISOR_ENABLED:
        logger.info("Supervisor disabled (no ANTHROPIC_API_KEY set) — skipping.")
        return

    import anthropic

    client = anthropic.Anthropic()

    while True:
        await asyncio.sleep(config.SUPERVISOR_INTERVAL_SEC)
        try:
            prompt = _build_prompt()
            if prompt is None:
                continue
            resp = client.messages.create(
                model=config.SUPERVISOR_MODEL,
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )
            note = "".join(block.text for block in resp.content if getattr(block, "type", None) == "text")
            if note:
                db.log_supervisor_note(note)
                logger.info("Supervisor note: %s", note)
        except Exception:  # noqa: BLE001 - never let this loop kill the bot
            logger.exception("Supervisor pass failed, will retry next interval")

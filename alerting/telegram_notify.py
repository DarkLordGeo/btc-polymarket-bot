"""
Minimal Telegram alerting — status/trade/error notifications sent FROM
WITHIN the running bot process.

Uses Telegram's Bot API directly over HTTPS via `requests` (already a hard
dependency of this project) — no third-party Telegram SDK needed for
send-only usage.

Every function here is deliberately best-effort and NON-BLOCKING-ON-FAILURE:
a Telegram outage, bad token, or network hiccup must never crash or stall
the trading loop. `send()` catches everything and returns a bool; callers in
main.py never need their own try/except around a notify_*() call.

This module does NOT and CANNOT implement the "bot process died" alert — a
crashed/killed process can't send its own death notice. That's handled
separately, outside Python entirely, by deploy/telegram_alert.sh via
systemd's OnFailure= — see deploy/btc-bot.service.
"""

from __future__ import annotations

import logging
import time

import requests

import config

logger = logging.getLogger("telegram")

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_REQUEST_TIMEOUT_SEC = 10.0

# Last-sent wall-clock time per dedupe key, so a fast-repeating error (e.g.
# one that could fire every 3s from the decision loop) sends at most one
# alert per key per cooldown window instead of one per occurrence. Process-
# local and intentionally not persisted — a restart just resets the clock,
# which is fine since a restart is itself often worth a fresh alert.
_last_sent_by_key: dict[str, float] = {}


def enabled() -> bool:
    return config.TELEGRAM_ENABLED


def send(text: str, *, dedupe_key: str | None = None, cooldown_sec: float | None = None) -> bool:
    """
    Best-effort send.

    Returns True if a message was actually sent, OR if Telegram is disabled
    (nothing to report as "failed to send" when it was never supposed to
    send), OR if this call was dropped by dedupe (already alerted recently).
    Returns False only when Telegram IS enabled and a real send genuinely
    failed — callers can use this to decide whether to also log locally,
    but must never let it propagate as an exception.

    dedupe_key: use for anything that can fire from a fast/repeating code
    path (decision-loop or settlement-loop errors, the chronic
    missing-market warning). Do NOT use it for one-off events like a trade
    open/close or the startup message — those should always go through.
    """
    if not config.TELEGRAM_ENABLED:
        return True

    if dedupe_key is not None:
        cooldown = config.TELEGRAM_ERROR_COOLDOWN_SEC if cooldown_sec is None else cooldown_sec
        last = _last_sent_by_key.get(dedupe_key)
        now = time.time()
        if last is not None and now - last < cooldown:
            return True
        _last_sent_by_key[dedupe_key] = now

    try:
        resp = requests.post(
            _API_URL.format(token=config.TELEGRAM_BOT_TOKEN),
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=_REQUEST_TIMEOUT_SEC,
        )
        if resp.status_code != 200:
            logger.warning("Telegram send failed (HTTP %s): %s", resp.status_code, resp.text[:300])
            return False
        return True
    except Exception:  # noqa: BLE001 — a notification failure must never crash the caller
        logger.exception("Telegram send raised")
        return False


# ---------------------------------------------------------------------------
# Convenience wrappers — one per alert type main.py sends. Keeping the
# message formatting here (not inline in main.py) means the wording only
# needs to change in one place.
# ---------------------------------------------------------------------------


def notify_started(strategies: tuple[str, ...], market_slug_contains: str) -> bool:
    return send(
        "🟢 <b>btc-polymarket-bot started</b>\n"
        f"Strategies: {', '.join(strategies)}\n"
        f"Market filter: <code>{market_slug_contains}</code>\n"
        "Mode: paper trading only"
    )


def notify_trade_opened(strategy_id: str, market_slug: str, side: str, stake: float,
                         entry_price: float, net_edge: float | None) -> bool:
    # net_edge is signed toward "Up" (see engine/decision_engine.py) — a
    # BUY_DOWN trade is triggered by a strongly NEGATIVE net_edge, so
    # showing that raw signed number here would misleadingly read as "the
    # bot traded on bad edge" for every single Down trade. `side` already
    # states the direction, so this shows the magnitude only.
    edge_str = f"{abs(net_edge):.1%}" if net_edge is not None else "n/a"
    return send(
        f"📈 <b>Trade opened</b> [{strategy_id}]\n"
        f"{market_slug}\n"
        f"{side} @ {entry_price:.3f} — stake ${stake:.2f} — net edge {edge_str}"
    )


def notify_trade_settled(strategy_id: str, market_slug: str, side: str, won: bool,
                          pnl: float, bankroll: float) -> bool:
    emoji = "✅" if won else "❌"
    return send(
        f"{emoji} <b>Trade settled</b> [{strategy_id}]\n"
        f"{market_slug}\n"
        f"{side} — {'WON' if won else 'LOST'} — pnl ${pnl:+.2f} — bankroll ${bankroll:.2f}"
    )


def notify_error(context: str, detail: str, *, dedupe_key: str) -> bool:
    return send(f"⚠️ <b>Error</b> — {context}\n<code>{detail[:500]}</code>", dedupe_key=dedupe_key)


def notify_status(summary_lines: list[str]) -> bool:
    return send("📊 <b>Status update</b>\n" + "\n".join(summary_lines))
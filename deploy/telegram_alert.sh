#!/usr/bin/env bash
# Sends a Telegram "the bot is down" alert. Called by systemd's OnFailure=
# on btc-bot.service (see btc-bot.service / btc-bot-crash-alert.service in
# this directory) — NOT from inside the bot's own Python process.
#
# Deliberately bash + curl only, no Python/venv dependency: this is the
# backstop for exactly the cases alerting/telegram_notify.py (which runs
# INSIDE the process that might be crashing) cannot cover — a broken venv,
# a syntax error in the bot's own code, an OOM kill, SIGKILL, anything that
# never lets the Python process reach its own except-block.
set -euo pipefail

ENV_FILE="${ENV_FILE:-/home/mr_ergeshidze/btc-polymarket-bot/telegram.env}"
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  echo "telegram_alert.sh: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set in $ENV_FILE, skipping" >&2
  exit 0
fi

HOST="$(hostname)"
# Best-effort recent log tail for context — never fatal if journalctl is
# unavailable or the unit name doesn't match.
TAIL="$(journalctl -u btc-bot.service -n 15 --no-pager 2>/dev/null | tail -c 3000 || true)"

TEXT="🔴 btc-polymarket-bot DOWN on ${HOST}
systemd reports btc-bot.service failed (it will still auto-restart per Restart=always, unless it has hit the crash-loop limit — check with: systemctl status btc-bot.service).

Last log lines:
${TAIL}"

curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
  --data-urlencode "text=${TEXT}" \
  >/dev/null || echo "telegram_alert.sh: send failed" >&2

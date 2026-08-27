"""
Central configuration. Tune these before running — the defaults are
reasonable starting points, not a validated strategy.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Market selection
# ---------------------------------------------------------------------------

# Gamma API slug filter used to find the live Bitcoin "up or down" markets.
# Polymarket runs these back-to-back in 5-minute windows with slugs shaped
# like "btc-updown-5m-<unix-timestamp>" (e.g. "btc-updown-5m-1776028800") —
# confirmed directly against a live Polymarket event page, not assumed. An
# earlier version of this file used "bitcoin-up-or-down", which does not
# match any current market's slug at all — find_live_btc_updown_market()
# would silently return None forever, and the bot would never log a single
# decision no matter how long it ran (no crash, no error — just nothing).
# If Polymarket renames these again, this is the first thing to check when
# a run produces zero data: confirm the real slug by looking at an actual
# live market URL/response, don't guess. We poll for whichever market is
# currently open rather than hardcoding a specific slug.
MARKET_SLUG_CONTAINS = "btc-updown-5m"

# How often to re-poll Gamma for "which market is live right now" (seconds).
# Markets roll every 5 minutes, so this only needs to be occasional.
MARKET_DISCOVERY_INTERVAL_SEC = 20

# ---------------------------------------------------------------------------
# BTC reference price feed
# ---------------------------------------------------------------------------

# Public, keyless feeds. Coinbase's WS ticker is used as the primary live
# feed; CoinGecko REST is the fallback if the WS drops.
COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
COINBASE_PRODUCT_ID = "BTC-USD"
COINGECKO_REST_URL = "https://api.coingecko.com/api/v3/simple/price"

# ---------------------------------------------------------------------------
# Data freshness (pre-collection reliability pass)
# ---------------------------------------------------------------------------
# These are DATA-INTEGRITY gates, not strategy parameters: they decide
# whether an input is trustworthy enough to decide on at all, not how to
# weigh it once trusted. Nothing below changes MIN_EDGE_TO_TRADE,
# ORDERBOOK_IMBALANCE_WEIGHT, or any other strategy threshold.

# Maximum age (seconds) of a CLOB order-book update (TokenState.last_update_ts)
# before it's treated as stale rather than live. Polymarket's WS pushes book/
# price_change/last_trade_price events on real activity; if nothing has
# arrived in this long, the WS may have silently stopped delivering updates
# (still "connected" at the socket level) without the reconnect logic
# noticing yet. A stale book must never be labeled "live_orderbook" or used
# to size/price a trade — see polymarket/clob_ws.py TokenState.is_fresh() and
# main.py resolve_book_state().
CLOB_DATA_MAX_AGE_SEC = 10.0

# Maximum age (seconds) of the latest recorded BTC price (BtcPriceFeed.price)
# before it's treated as stale rather than live. A 5-minute market can move
# meaningfully in a few seconds; deciding off a stale price risks modeling
# a BTC move that already reversed. See market_data/btc_feed.py
# BtcPriceFeed.is_fresh().
BTC_DATA_MAX_AGE_SEC = 10.0

# How close (seconds) our own recorded BTC price sample must be to a
# market's actual start_date to be trusted as that market's reference/
# strike price ("price to beat"). Coinbase's WS ticker pushes on essentially
# every trade (sub-second in normal conditions); a much larger gap here
# means the feed likely had an outage spanning market open, and using that
# stale sample as the strike would silently model the wrong barrier problem
# (see main.py resolve_reference_price() and README "Fix — reference/strike
# price handling"). If no sample is close enough, the market is skipped —
# NEVER the current BTC price.
REFERENCE_PRICE_MAX_STALENESS_SEC = 5.0

# ---------------------------------------------------------------------------
# Decision engine
# ---------------------------------------------------------------------------

# Rolling window (seconds) used to estimate short-horizon realized volatility
# and drift/momentum of BTC.
VOL_LOOKBACK_SEC = 90

# Minimum edge (in probability points, e.g. 0.05 = 5 percentage points)
# between our fair-value estimate and the market's implied probability
# before we'll consider trading. This is meant to absorb fees + slippage +
# model error — raise it if paper results show the edge doesn't survive
# costs.
MIN_EDGE_TO_TRADE = 0.06

# Weight applied to order-book imbalance as a secondary nudge to the fair
# probability estimate (0 = ignore order book entirely).
ORDERBOOK_IMBALANCE_WEIGHT = 0.05

# Don't trade in the very last N seconds of a market's life (execution +
# settlement risk rises sharply near expiry).
MIN_SECONDS_REMAINING_TO_TRADE = 30

# ---------------------------------------------------------------------------
# Risk management (paper trading, but sized as if it were real)
# ---------------------------------------------------------------------------

STARTING_BANKROLL = float(os.getenv("STARTING_BANKROLL", "1000"))

# Max stake on any single market, as a fraction of current bankroll.
MAX_STAKE_FRACTION = 0.03

# Absolute cap per trade regardless of bankroll size.
MAX_STAKE_USD = 50

# Stop opening new positions for the rest of the day once cumulative
# realized paper P&L drops this fraction of the starting bankroll.
MAX_DAILY_LOSS_FRACTION = 0.15

# Minimum seconds between two trades on the *same* market (avoid flip-flopping
# on noisy signal updates).
TRADE_COOLDOWN_SEC = 15

# Rough fee model for paper-trading purposes only, applied to the stake at
# entry. Polymarket applies dynamic taker fees specifically on short-duration
# crypto markets to discourage latency arbitrage — the real schedule varies
# and should be checked against current docs before trusting this number for
# anything beyond a paper-trading sanity check.
PAPER_FEE_RATE = 0.02

# Minimum resting size (in USD notional, i.e. price * size) required at the
# top of book on the side we'd be buying before we'll trade it at all. This
# is a NEW gate added in the research-grade pass. Default is 0 (disabled) so
# it does not silently change baseline behavior versus the original bot —
# raise it deliberately once you have evidence thin-book fills are a problem.
MIN_LIQUIDITY_USD = 0.0

# Extra assumed slippage buffer (in probability points, added on top of the
# fee-derived cost buffer below) for the "net edge after costs" analysis
# field. Default 0 — the cost buffer is fee + half-spread only until you have
# a reason to think real slippage runs higher than that.
ASSUMED_SLIPPAGE_BUFFER = 0.0

# ---------------------------------------------------------------------------
# Execution mode
# ---------------------------------------------------------------------------

# This project only implements paper trading. LIVE_TRADING_ENABLED exists so
# the intent is explicit in one place; flipping it to true does nothing by
# itself — see README.md "Going live" section for why that's deliberate.
LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Supervisor (optional Claude reasoning layer)
# ---------------------------------------------------------------------------

SUPERVISOR_ENABLED = bool(os.getenv("ANTHROPIC_API_KEY"))
SUPERVISOR_MODEL = os.getenv("SUPERVISOR_MODEL", "claude-sonnet-4-5")
SUPERVISOR_INTERVAL_SEC = 600  # review recent decisions every 10 minutes
SUPERVISOR_LOOKBACK_DECISIONS = 20

# ---------------------------------------------------------------------------
# Strategy variants (research phase)
# ---------------------------------------------------------------------------

# Three strategies run in parallel, in shadow, against the exact same market
# observations every tick — each with its own bankroll/positions so their
# paper P&L is directly comparable (same market conditions, different model):
#
#   A — market baseline: fair_prob_up := market's own implied probability.
#       Edge is always exactly 0 by construction, so it never trades; it
#       exists purely so evaluate.py can ask "is the market itself
#       well-calibrated", which is the reference point everything else has
#       to beat.
#   B — statistical model: the random-walk/barrier model, WITHOUT the
#       order-book imbalance nudge.
#   C — statistical model + order-book imbalance (this is what the original
#       single-strategy bot did).
#
# This lets evaluate.py answer "does adding order-book imbalance actually
# help" (B vs C) and "does the model beat just trusting the market price"
# (A vs B/C) as controlled comparisons rather than guesses.
STRATEGIES = ("A", "B", "C")

# ---------------------------------------------------------------------------
# Evaluation / research parameters
# ---------------------------------------------------------------------------
# These only affect how evaluate.py / dashboard.py bucket and report
# collected data — they have no effect on live trading decisions.

# Calibration buckets, expressed as "confidence" (i.e. the model's predicted
# probability of whichever side it considered more likely: max(p, 1-p)).
# A well-calibrated model's empirical hit rate in each bucket should roughly
# match the bucket's own range.
CALIBRATION_BUCKETS = [
    (0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70),
    (0.70, 0.75), (0.75, 0.80), (0.80, 1.01),  # 1.01 so 100% falls inside the last bucket
]

# |edge| buckets (probability points) for "performance by edge size".
EDGE_BUCKETS = [(0.00, 0.03), (0.03, 0.06), (0.06, 0.09), (0.09, 0.12), (0.12, 1.0)]

# Seconds-remaining buckets for "performance by time remaining in the window".
TIME_REMAINING_BUCKETS = [(0, 60), (60, 120), (120, 180), (180, 240), (240, 301)]

# Order-book imbalance buckets ([-1, 1] range) for "performance by imbalance".
IMBALANCE_BUCKETS = [(-1.0, -0.3), (-0.3, -0.1), (-0.1, 0.1), (0.1, 0.3), (0.3, 1.0)]

# Number of realized-volatility terciles ("low/medium/high" regime) computed
# from the observed data itself at evaluation time — sigma has no natural
# fixed scale, so this is quantile-based rather than a hardcoded threshold.
VOL_REGIME_BUCKET_COUNT = 3

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

DB_PATH = os.getenv("DB_PATH", "bot_state.sqlite3")

# ---------------------------------------------------------------------------
# Telegram alerting
# ---------------------------------------------------------------------------
# Status/trade/error notifications sent FROM WITHIN the running process — see
# alerting/telegram_notify.py. Fully optional: if either var is unset,
# TELEGRAM_ENABLED is False and every call in that module becomes a silent
# no-op (never raises, never blocks the trading loop on a Telegram outage).
#
# This deliberately does NOT cover "the process itself crashed/got killed" —
# a dead process can't send its own death notice. That's handled separately,
# outside Python entirely, by deploy/telegram_alert.sh via systemd's
# OnFailure= (see deploy/btc-bot.service) — a dependency-free bash+curl
# script that still works even if the venv or code is broken.
#
# Get TELEGRAM_BOT_TOKEN from @BotFather; get TELEGRAM_CHAT_ID by messaging
# your bot once, then checking https://api.telegram.org/bot<token>/getUpdates.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

# How often to send a periodic "still alive, here's a summary" message.
TELEGRAM_STATUS_INTERVAL_SEC = int(os.getenv("TELEGRAM_STATUS_INTERVAL_SEC", str(4 * 3600)))

# Minimum seconds between two alerts sharing the same dedupe_key — stops a
# fast-repeating in-process error (e.g. the decision loop retries every 3s)
# from spamming the chat with one message per occurrence.
TELEGRAM_ERROR_COOLDOWN_SEC = int(os.getenv("TELEGRAM_ERROR_COOLDOWN_SEC", str(15 * 60)))

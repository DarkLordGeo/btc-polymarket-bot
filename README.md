# BTC / Polymarket paper-trading research bot

Watches Polymarket's short-duration "Bitcoin Up or Down" markets (currently
running as back-to-back 5-minute windows) and runs **three parallel
strategy variants** against them in shadow — each with its own paper
bankroll — so their results are directly comparable. A fast, deterministic
decision engine sits on the trading path; an optional slower Claude
"supervisor" reviews the bot's own recent behavior and writes plain-English
notes, but never places, approves, or influences a trade.

**This is paper trading only.** No real orders are placed and no wallet or
private key is ever read by the trading loop. See "Going live" at the
bottom for why that boundary is deliberate.

**This phase's objective is strategy validation, not profit.** Everything
below is built to answer "does this hypothesis have predictive value" —
not to produce a profitable-looking backtest. If early results look bad,
the correct response is to collect more data and read the numbers
honestly, not to retune the strategy until the numbers look better.

## Pre-collection reliability pass (latest)

Before starting the real data-collection run, an audit identified 4
data-integrity gaps that could silently contaminate the experiment. All 4
are fixed; nothing else changed — diffed byte-for-byte against the
previous delivery to confirm it (`config.py`, `engine/decision_engine.py`,
`engine/strategy.py`, `engine/risk_manager.py`, and `broker/paper_broker.py`
are all identical).

1. **CLOB order-book staleness.** `TokenState.is_fresh()` (new) checks
   `last_update_ts` against `config.CLOB_DATA_MAX_AGE_SEC` (10s default). A
   book that hasn't updated recently enough — or has never updated — is no
   longer usable: its midpoint/imbalance/spread are never read, and it is
   never labeled `market_prob_source = "live_orderbook"`, even though the
   stale values are still sitting in memory. `main.py`'s new
   `resolve_book_state()` centralizes this and falls back to the existing
   `fallback_snapshot` path only if that's actually available; otherwise
   the tick is skipped.
2. **Reference/strike price fabrication.** `_switch_market()` used to fall
   back to the CURRENT BTC price if it couldn't find a historical sample at
   market start — silently modeling a different barrier problem than the
   one Polymarket actually resolves. `main.py`'s new
   `resolve_reference_price()` now only ever uses (1) a structured strike
   field from Gamma metadata if Polymarket ever exposes one (confirmed via
   external research that none currently exists — this is a
   forward-compatible no-op today, not dead code), or (2) a historical BTC
   sample within `config.REFERENCE_PRICE_MAX_STALENESS_SEC` (5s) of the
   market's actual start — and otherwise skips the market for trading
   entirely rather than inventing a strike.
3. **BTC price staleness.** `BtcPriceFeed.is_fresh()` (new) checks against
   `config.BTC_DATA_MAX_AGE_SEC` (10s default) before any decision uses
   `.price`. If the feed hasn't updated recently — WS dropped and the REST
   fallback also isn't landing — the whole tick (all 3 strategies) is
   skipped rather than deciding off a possibly-stale price.
4. **`replay.py` settlement timing.** It used to call `broker.settle()`
   inside the per-tick loop, which immediately freed
   `has_open_position()` back to `False` — so a single 5-minute market
   with several ticks that each individually cleared the edge threshold
   could fabricate a fresh "trade" on every one of those ticks, instead of
   one real trade held across the window. Rows are now grouped by market
   (`_group_rows_by_market()`) and a market's position is settled exactly
   once, after all of that market's own ticks are processed.

25 new regression tests cover all 4 (`tests/test_clob_ws.py`,
`tests/test_btc_feed.py`, `tests/test_main_resolution.py`, and additions to
`tests/test_replay.py`) — 133 total, all passing. Reviewed but deliberately
NOT changed this pass (not required for reliable collection, and each would
be a larger, separate change): the daily-loss breaker's `reset_day()` is
defined but never actually called on a day boundary, and in-memory bot
state (open positions, pending outcomes, shadow bankrolls) doesn't survive
a process restart. Neither corrupts data that IS collected — the first
just means a tripped daily breaker could suppress trading for the rest of
a long run, and the second only orphans whatever was strictly in-flight at
the moment of a crash — but both are worth knowing about if you're running
this unattended for many days; see "Remaining known issues" below.

## Strategy freeze (current phase)

As of this pass, the strategy itself is **frozen**. `config.py`,
`engine/risk_manager.py`, and `broker/paper_broker.py` are byte-identical
to the previous delivery — diffed directly against it, not asserted from
memory. The only changes in this pass are 3 correctness fixes to cost
accounting and data provenance (below) and the analysis tooling that
reports on them. Explicitly, until this phase concludes with a decision
about whether the hypothesis has an edge:

- No ML, no LLM on the trading path (unchanged from every prior phase).
- No new indicators or signals added to the probability model.
- No changing `MIN_EDGE_TO_TRADE`, `ORDERBOOK_IMBALANCE_WEIGHT`,
  `VOL_LOOKBACK_SEC`, position sizing, or any other `config.py` value —
  whether early results look good, bad, or noisy.
- No parameter search for values that would have maximized historical
  paper P&L. `replay.py`'s sweep mode exists to show what a *different*
  threshold would have done for reporting purposes, not to pick a new one.
- No real-money trading (see "Going live" at the bottom).

The point of this phase is to find out whether the existing hypothesis has
predictive value, not to manufacture a profitable-looking result. If the
data says no, the correct response is to say so and discard or rethink the
hypothesis — not to keep tuning until it says yes.

## Architecture

```
Polymarket CLOB WebSocket  ──┐
 (order book / trades)       │
                              ▼
Coinbase WS (BTC/USD) ──▶  3 parallel strategies (A/B/C, same observation each tick)
                              │        each: decide() ──▶ RiskManager ──▶ PaperBroker
                              │                                              │
                              ▼                                              ▼
                    Gamma API (market discovery,               SQLite: decisions / trades /
                    settlement confirmation)                   market_outcomes (ALL markets,
                                                                 traded or not)
                                                                              │
                                                        ┌─────────────────────┼─────────────────────┐
                                                        ▼                     ▼                     ▼
                                                  evaluate.py           dashboard.py            replay.py
                                                (plain-text report)  (static HTML report)   (what-if backtest,
                                                                                              no live network)

Claude Supervisor (optional, every ~10 min) reads recent decisions/trades
across all 3 strategies and writes a commentary note back to the log. It
cannot place, approve, reject, or influence a trade, or change config.
```

- `polymarket/gamma_client.py` — finds whichever "BTC Up or Down" market is
  currently live, and later confirms how it resolved.
- `polymarket/clob_rest.py`, `polymarket/clob_ws.py` — read-only order book
  / price data for that market's two outcome tokens (Up / Down). No auth.
- `market_data/btc_feed.py` — live BTC/USD price (Coinbase WS, CoinGecko
  REST fallback) with a rolling history for vol/momentum estimation.
- `engine/decision_engine.py` — the fast, no-LLM-on-path model (probability
  estimate, edge, cost buffer, net edge). Trading is gated on **net edge**
  (raw edge minus the assumed cost buffer), not raw edge. See the module
  docstring for the actual math.
- `engine/strategy.py` — the A/B/C strategy variants and the per-strategy
  shadow runner (its own RiskManager + PaperBroker).
- `engine/risk_manager.py` — position sizing, per-market cooldown, daily
  loss circuit breaker, minimum-liquidity gate.
- `broker/paper_broker.py` — simulates fills and $1/$0 settlement. Tracks
  paper P&L only.
- `storage/logging_db.py` — SQLite log: every decision (traded or not, all
  3 strategies, including where its market probability came from —
  `market_prob_source`), every settled trade, and every observed market's
  outcome (traded or not, including how it was resolved —
  `resolution_source`) — this is what makes calibration analysis possible.
- `analysis/metrics.py` — shared stats helpers (correlation, Brier score,
  drawdown, profit factor, quantile bucketing) used by evaluate/dashboard.
- `evaluate.py` — plain-text analysis report: data quality, profitability,
  calibration, edge/time/vol/imbalance breakdowns, A vs B vs C.
- `dashboard.py` — the same analysis as a static HTML report with charts
  ("why did the bot bet that" / "is the strategy actually working" / "how
  much of this data can I actually trust").
- `replay.py` — what-if backtest over already-collected data, no network,
  no fabricated data. See its own docstring for what it can and can't do.
- `supervisor.py` — optional periodic Claude review of recent decisions.
  Commentary only — see "The Claude supervisor" below for the hard boundary.
- `main.py` — wires the live pipeline together.
- `tests/` — unit tests for all of the above (`python -m unittest discover
  -s tests -t .`).

## The decision model, honestly

Each market resolves on whether BTC is above or below a reference price
("price to beat", fixed when the window opens) at expiry. The engine treats
that as a random-walk barrier problem: given BTC's current distance from
the reference price, its recent realized volatility, and the time left, it
computes a fair probability of "Up" and compares it to the market's implied
probability (the Up token's mid price).

Three variants of "fair probability" run in parallel, every tick, against
the identical observation, each with its own paper bankroll:

- **Strategy A — market baseline.** Fair probability := the market's own
  price. Edge is exactly 0 by construction, so A never trades. It exists so
  `evaluate.py` can ask "is the market itself well-calibrated" — the bar
  everything else has to clear to be worth anything.
- **Strategy B — statistical model.** The random-walk/barrier model,
  *without* the order-book-imbalance nudge.
- **Strategy C — statistical model + order-book imbalance.** Same as B,
  plus a small nudge from resting bid/ask size imbalance. This is what the
  original single-strategy version of this bot did.

B vs C isolates whether the imbalance term helps. A vs (B or C) isolates
whether the model beats just trusting the market price. This is intentionally
simple — no drift term beyond what's implicit in "already above/below", no
cross-asset signals. It is **not** a validated strategy. The honest summary
of the hard part: *the API integration is the easy 20%; whether any of
these three has a real edge once you subtract fees, slippage, and the fact
that other bots are already competing in exactly this market is the other
80%, and nothing here proves that either way.* That's what this phase is for.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # optionally set ANTHROPIC_API_KEY to enable the supervisor
```

### Run the paper-trading bot

```bash
python main.py
```

Leave it running and watch the console log — all three strategies trade
(or don't) in shadow, every decision and trade is written to
`bot_state.sqlite3`.

### Run the analysis

```bash
python evaluate.py                # plain-text report to stdout
python evaluate.py report.txt     # also save it
```

### Generate the dashboard

```bash
python dashboard.py               # writes dashboard.html
open dashboard.html               # or just open the file in a browser
```

### Run the what-if replay/backtest (no network needed)

```bash
python replay.py                                    # baseline config
python replay.py --min-edge 0.08 --imbalance-weight 0.03
python replay.py --min-edge 0.03 --min-edge 0.06 --min-edge 0.09   # sweep
```

Read `replay.py`'s module docstring before trusting its numbers — it
reuses recorded per-tick observations rather than raw tick history, and
approximates fills without simulating spread-crossing, so it's directionally
indicative, not a tighter estimate than the live paper broker.

### Run the tests

```bash
python -m unittest discover -s tests -t . -v
```

133 tests, all passing as of this delivery — probability math, edge/net-edge
calculations (including that a cost buffer big enough to eat the edge
flips the decision to HOLD, not just shrinks the reported number),
settlement, position sizing, the daily loss breaker, the liquidity gate,
strategy A/B/C comparison, calibration/Brier/drawdown/profit-factor
calculations, market-probability-source round-tripping through the DB,
CLOB/BTC-feed freshness (fresh accepted, stale rejected, missing handled
safely, a stale book can't leak into `market_prob_source` or imbalance),
reference-price priority (structured metadata > close historical sample >
skip — never the current BTC price), and replay/backtest logic (including
that raising the edge threshold never *increases* trade count, that
unresolved markets are excluded rather than guessed, and that a market
with many qualifying ticks settles exactly one trade, not one per tick).

### A note on where this needs to run

The endpoints this bot talks to (Polymarket's Gamma/CLOB APIs, Coinbase's
WS feed) are ordinary public internet APIs. **This project was built and
tested in a sandboxed environment whose outbound network is allow-listed to
package registries only — it cannot reach Polymarket or Coinbase at all**
(every attempt returns a 403 from the sandbox's own egress proxy, confirmed
directly, not assumed). Concretely, that means:

- Every live network call (`gamma_client`, `clob_rest`, `clob_ws`,
  `btc_feed`'s Coinbase WS and CoinGecko REST fallback) was verified to
  **fail gracefully** — running `python main.py` in this sandbox logs a
  caught exception and retries on a backoff loop rather than crashing the
  process. That's the honest extent of what "tested against the network"
  means here: graceful failure under a real (if wrong-reason) connection
  failure, not a successful live call.
- Every other component — the decision math, strategy comparison, risk
  management, settlement, SQLite schema/migration, `evaluate.py`,
  `dashboard.py`, and `replay.py` — was exercised end-to-end against
  synthetic data by driving the actual `Bot` class methods from `main.py`
  directly (not a reimplementation), plus the unit test suite.
- Run this from your own machine or a server with normal outbound internet
  access, and **treat the first live run as a shakedown**: watch the logs
  for a while before trusting the numbers, and expect to fix at least minor
  field-name mismatches if Polymarket has changed a response shape since
  this was written.

## What changed in this pass (net-edge gating + data provenance + freeze)

Starting point was the research-grade A/B/C bot from the previous pass.
This pass makes exactly 3 fixes, all cost-accounting/data-integrity fixes
rather than strategy changes, and then freezes everything else (see
"Strategy freeze" above):

1. **Fix — trading now gates on net edge, not raw edge.** Previously
   `MIN_EDGE_TO_TRADE` was compared against `raw_edge` directly; `net_edge`
   (raw edge minus the assumed half-spread + fee + slippage cost buffer)
   was computed and logged but never actually consulted by `decide()`. That
   meant the bot could and did open positions where the assumed transaction
   cost had already eaten the entire predicted edge before the trade was
   even placed. `decide()` now gates on `net_edge`, and position sizing
   (`RiskManager.stake_for`) is sized off `net_edge` too. `MIN_EDGE_TO_TRADE`
   itself is unchanged — this is purely "compare the right number to the
   same threshold," not a new or adjusted threshold.
2. **Fix — market-probability source is now recorded on every decision.**
   `Observation.market_prob_source` / the logged `market_prob_source`
   column records `"live_orderbook"` (a real bid/ask midpoint or the WS
   feed's own last-trade fallback — both real-time) vs. `"fallback_snapshot"`
   (a static price captured once at market-discovery time, used only when
   the order book has no usable state yet — can be stale). Previously this
   distinction existed in the code but wasn't logged anywhere, so a decision
   made on stale data was indistinguishable from one made on live data after
   the fact. `evaluate.py` and `dashboard.py` now report calibration and
   trading performance for each source **separately**.
3. **Fix — resolution source is now unambiguous about official vs. proxy.**
   The settlement column that used to read `"feed_fallback"` is now
   `"proxy_coinbase_feed"`, and `_try_resolve_market()`'s docstring says
   plainly that `"gamma_official"` **is** Polymarket's actual resolution
   mechanism (read back from the closed market's own outcome prices via the
   Gamma API), while `"proxy_coinbase_feed"` is an unverified assumption
   (our own BTC feed's price vs. the reference price, used only when
   Polymarket's own resolution hasn't posted within 30s of expiry) that
   must never be treated as equivalent to a confirmed resolution.
   `evaluate.py` and `dashboard.py` report calibration for each separately
   too, and `replay.py`'s cost-aware gating and the trades/tests referencing
   the old string were all updated to match.

None of `config.py`, `engine/risk_manager.py`, or `broker/paper_broker.py`
changed at all in this pass — diffed byte-for-byte against the previous
delivery to confirm it, not just remembered. `replay.py` was also updated
to gate on the same logged `net_edge` the live bot now uses (previously it
gated on raw edge, which after fix #1 would have made replay silently
answer a different question than what the live bot actually runs).

## What changed in the previous pass (research-grade logging + A/B/C)

Starting point was the single-strategy paper-trading bot from the phase
before that. Changes, in order:

1. **Bug fix — reference price was a single shared value.** The live bot
   used to store "price to beat" as one `self.reference_price` attribute,
   overwritten every time a new market started. The settlement loop's
   feed-fallback path (used when Polymarket's own resolution hasn't posted
   yet) read that attribute at settlement time — for any market other than
   the currently-active one, it silently used the *wrong* market's
   reference price. Now tracked per-market (`reference_price_by_slug`).
2. **Bug fix — outcomes were only ever recorded for traded markets.** The
   settlement loop used to drop any market from its pending list if no
   paper position had been opened on it, so there was no ground truth to
   check the probability model against for markets it declined to trade —
   which is most of them. Now every observed market gets a `market_outcomes`
   row, traded or not (with a bounded give-up timeout, not an infinite
   retry, if neither official resolution nor a feed fallback ever arrives).
3. **Bug fix — daily-loss-breaker tracked the wrong starting bankroll.**
   `RiskManager._day_start_bankroll` defaulted from `config.STARTING_BANKROLL`
   independent of whatever `bankroll` the instance was actually constructed
   with — a `RiskManager(bankroll=250)` would silently mis-track its own
   daily loss limit from tick one. Fixed via `__post_init__`.
4. **Correctness — volatility window span.** The realized-vol estimator now
   uses the actual elapsed time between the specific price samples it
   summed (`recent_returns_with_span`), instead of a separately-computed
   buffer span that happened to usually agree with it.
5. Added: strategies A/B/C, cost-buffer/net-edge fields, spread and
   liquidity fields, `market_outcomes` table, `evaluate.py`, `replay.py`,
   research-grade `dashboard.py`, the test suite, `MIN_LIQUIDITY_USD` (off
   by default — baseline behavior preserved).

None of the existing decision math, risk logic, or fee model was changed —
`MIN_EDGE_TO_TRADE`, `ORDERBOOK_IMBALANCE_WEIGHT`, `VOL_LOOKBACK_SEC`,
position sizing, and the daily loss fraction are all still at their
original baseline values.

## Suspicious assumptions worth knowing about

These aren't bugs — they're modeling choices that could be wrong, listed so
they're visible rather than buried:

- **No spread-crossing in `replay.py`'s simulated fills.** The live
  `PaperBroker` fills at `best_ask` (realistic-ish); `replay.py` fills at
  the logged mid price (optimistic). Don't compare their P&L numbers
  directly as if they used the same execution assumption.
- **Cost buffer is an approximation, not a fee schedule.** `cost_buffer_prob()`
  = half the Up-token spread + `PAPER_FEE_RATE`, treated as directly
  additive in probability-point terms. Polymarket's actual dynamic
  taker-fee schedule on short-duration crypto markets isn't modeled
  precisely — this is a sanity-check buffer, not a cost simulator.
- **No explicit drift term.** The model's only input is "how far is BTC
  from the reference price, scaled by recent vol" — momentum
  (`btc_momentum`) is logged every tick but not used by the probability
  estimate. It's there so a future drift term could be evaluated against
  real data instead of added on a hunch.
- **The no-volatility fallback (`0.5 ± 0.05`) is an arbitrary constant,**
  used only in the rare case there isn't yet enough price history to
  estimate realized vol. It hasn't been validated against anything.
- **`market_prob_up` can still fall back to a stale, discovery-time
  snapshot** (`market.up_price`) if the WebSocket order-book state never
  populates a midpoint for a thin market. There's no periodic REST refresh
  of that fallback — a known gap, not fixed in this pass. What changed:
  every decision now records which case it was (`market_prob_source`), and
  `evaluate.py`/`dashboard.py` report calibration and P&L for
  `fallback_snapshot` decisions separately from `live_orderbook` ones, so
  this gap is now visible in the numbers instead of silently blended in.
- **Proxy settlement (`proxy_coinbase_feed`: BTC price vs. reference price,
  per our own feed) is still an unverified proxy for the market's real
  resolution**, used only when Polymarket's own settlement (`gamma_official`)
  hasn't posted after 30s. It assumes our Coinbase-fed price at expiry
  matches whatever price source Polymarket itself resolves against —
  plausible, not verified. What changed: this pass renamed the label from
  the previous (misleadingly generic) `"feed_fallback"` to
  `"proxy_coinbase_feed"`, and `evaluate.py`/`dashboard.py` now report
  calibration for `gamma_official` vs. `proxy_coinbase_feed` markets
  separately rather than pooling them.

## Tuning

Everything that matters is in `config.py` with comments: `MIN_EDGE_TO_TRADE`,
`VOL_LOOKBACK_SEC`, `ORDERBOOK_IMBALANCE_WEIGHT`, position sizing, the daily
loss breaker, the trade cooldown, and `MIN_LIQUIDITY_USD` /
`ASSUMED_SLIPPAGE_BUFFER`. **Nothing in this pass, or the pass before it,
changed any of these values** — they're still the original baseline (see
"Strategy freeze" above — this is not optional during the current phase).
If `evaluate.py` or `replay.py` suggests a different value looks better,
that's a finding to report and discuss once this phase concludes, not a
config edit to make unilaterally now — see "Suspicious assumptions" above
and don't let a small paper sample talk you into retuning against noise.

## What data you need before evaluating profitability

`evaluate.py` will run on any amount of data, but with fewer than ~20-30
resolved markets per strategy, every number in it (win rate, calibration
buckets, edge-bucket performance) is dominated by sample noise — it prints
a warning below that threshold rather than pretending otherwise. This
phase's target is a **minimum of 200 resolved markets, preferably 500+**,
before drawing any conclusion about whether B or C has real signal. Before
you do:

- Let `main.py` run across enough 5-minute windows to accumulate that many
  *resolved* markets (not just decisions — a decision that HOLDs still
  counts toward calibration once its market resolves, but you want enough
  settled trades specifically for the profitability numbers to mean
  anything). See "Actually collecting 200-500 resolved markets" below —
  this requires many continuous hours of uptime, which does not fit inside
  a single interactive session.
- Check `evaluate.py`'s "Markets we gave up resolving" count — if it's
  nontrivial relative to markets observed, something about settlement
  (network flakiness, Gamma's resolution timing) needs investigating before
  the calibration numbers can be trusted.
- **Read the Data Quality section first, every time.** `evaluate.py` and
  `dashboard.py` both report it right after the overview: how many
  decisions were `live_orderbook` vs. `fallback_snapshot`, and how many
  resolved markets were `gamma_official` vs. `proxy_coinbase_feed`. If
  either breakdown is dominated by the fallback/proxy side, every other
  number in the report was mostly measured on stale prices and/or an
  unverified resolution proxy — treat conclusions from it as provisional
  until enough of the run happened on live data with official resolutions.
- Look at signal quality (calibration, Brier score, edge-outcome
  correlation) and profitability (win rate, P&L, profit factor) as two
  separate questions, per `evaluate.py`'s own section split — a strategy
  can show real signal and still lose money after costs, or vice versa on
  a small sample.
- Don't conclude the strategy works because of a few profitable trades, a
  high win rate alone, a good first day, or a good-looking dashboard. It's
  only interesting if the edge persists across the full sample and
  survives the net-edge/cost-buffer gating that's now actually enforced.

### Actually collecting 200-500 resolved markets

This sandboxed session cannot do this run itself: its outbound network is
allow-listed to package registries only (Polymarket and Coinbase are both
unreachable — confirmed directly, see "A note on where this needs to run"
below), and even with network access, 200-500 resolved 5-minute windows is
16-40+ hours of continuous uptime, which doesn't fit inside one interactive
conversation turn regardless of connectivity. To actually run this
yourself:

```bash
# On your own machine or a server with normal outbound internet access:
nohup python main.py > bot.log 2>&1 &
disown
# check progress any time without stopping it:
python evaluate.py
# or, for a persistent process that survives reboots, run it as a systemd
# service / supervisord program / tmux session instead of nohup — any of
# these work, the only requirement is "keeps running unattended for a day
# or two."
```

Let it run for at least a day or two (5-minute windows → ~288/day if the
market runs continuously, fewer in practice given rollover gaps and any
network hiccups), then run `python evaluate.py` and `python dashboard.py`
and read the Data Quality section before anything else.

## The Claude supervisor

If `ANTHROPIC_API_KEY` is set, `supervisor.py` runs on a slow timer
(default every 10 minutes), reads recent decisions and trades across all
three strategies, and asks Claude to sanity-check them: does the win rate
look consistent with the stated edges, is there a one-sided bias, does the
B vs C split look like it's telling you anything yet. It writes a note into
the `supervisor_notes` table (shown in the dashboard).

**Hard boundary, unchanged from the previous phase and re-verified in this
pass:** `_build_prompt()` only ever reads from the DB; `run_supervisor_loop()`
only ever calls `db.log_supervisor_note()` with the model's text response.
There is no code path — no return value main.py reads, no reference to any
`RiskManager`/`PaperBroker`/`StrategyRunner` instance, no access to `config`
— by which its output can place a trade, approve or reject a trade, resize
a position, or change any parameter. It is explicitly prompted not to
propose specific new parameter values, only to flag what's worth a human
looking into. Treat its notes as commentary, not ground truth.

## Remaining known issues (reviewed, not fixed this pass)

Identified during the pre-collection audit, deliberately left alone because
neither is required for reliable data collection and each would be a
separate, larger change rather than a data-integrity gate:

- **Daily-loss breaker never resets on a day boundary.** `RiskManager.reset_day()`
  exists and is tested, but nothing in `main.py` ever calls it — there's no
  day-rollover check in the decision loop. In practice this means if the
  breaker trips once during a long unattended run, trading (not logging —
  decisions are still recorded every tick regardless) stays suppressed for
  the rest of that run instead of resuming the next day. Worth adding
  before a run longer than a day or two if you want trading to resume after
  a bad day; not required for the calibration/signal-quality side of the
  analysis, which uses every logged decision, not just trades.
- **In-memory state doesn't survive a process restart.** Open positions,
  pending-outcome tracking, and shadow bankrolls all live in the `Bot`
  instance's memory, not the DB. If the process crashes and restarts (or is
  restarted manually) mid-collection, whatever market was strictly
  in-flight at that moment — an open position, or a market still waiting
  on resolution — is silently orphaned: its decision rows stay in the DB,
  but no trade or outcome ever gets recorded for it. This is bounded (only
  affects markets active at the exact moment of a restart, not the whole
  run) and visible after the fact — `evaluate.py`'s "Markets we gave up
  resolving" / observed-vs-resolved counts will reflect any such gaps, not
  hide them — but if you're running this via something that auto-restarts
  on crash, expect a small amount of orphaned data around each restart.

Neither of these was in the audit's list of 4 required fixes, and neither
threatens the correctness of data that IS collected — they're operational
gaps, not silent-corruption risks like the 4 fixes above were.

## Going live (please read before changing `LIVE_TRADING_ENABLED`)

This project stops at paper trading on purpose, and this pass does not
implement or enable real-money trading in any form — no order signing, no
wallet integration, nothing beyond what was already here. Wiring up real
execution would mean handling a funded Polygon wallet's private key inside
a script that autonomously decides when to spend it — that's a decision
with real financial consequences that should be yours to make
deliberately, not something that ships as a config flag flip.

Live execution is worth even considering only after **all** of the
following hold, based on real data from this phase's collection run, not a
short or synthetic one:

1. **Persistent positive expectancy** — net-of-cost P&L stays positive
   across the full sample (200+ resolved markets minimum), not just in a
   lucky subset or the first day.
2. **Reasonable calibration** — predicted probability roughly tracks actual
   frequency (e.g. decisions in the 60-65% bucket resolve Up roughly 60-65%
   of the time), separately confirmed for `live_orderbook` decisions (not
   just pooled with `fallback_snapshot`) and for `gamma_official`
   resolutions (not just pooled with `proxy_coinbase_feed`).
3. **Out-of-sample survival** — the edge holds on data collected *after*
   whatever data any analysis was based on, not just in-sample.
4. **Realistic cost/slippage modeling** — `cost_buffer_prob()`'s
   approximation (half-spread + flat fee rate) checked against Polymarket's
   actual dynamic taker-fee schedule and real fill behavior, not assumed.
5. **Correct resolution handling** — resolution logic verified against
   Polymarket's actual settlement mechanism specifically, not primarily
   validated against the Coinbase-feed proxy.
6. **Stress-tested risk limits** — the daily loss breaker, cooldown, and
   position sizing evaluated against adverse scenarios (a losing streak, a
   volatility spike, a stale/wrong price feed), not just typical conditions.

If you want to take this further yourself once those hold: Polymarket's
order placement goes through `py-clob-client` with an authenticated client
derived from your wallet key and requires USDC allowance set up on Polygon;
read Polymarket's own docs for the current requirements, and — regardless
of how good the paper results look — size any live trial as money you'd be
fine losing entirely, run it for a meaningful sample before scaling up, and
keep the daily loss breaker (or a tighter one) wired to real capital, not
just the paper bankroll.

**Not financial advice.** Nothing here is a claim that any of these three
strategies has a positive expected value, before or after fees. Markets
this short-duration attract fast, well-capitalized automated
counterparties; a plausible-looking model surviving one paper run is very
weak evidence.

"""
Unit tests for main.py's pure, synchronously-testable resolution helpers:
resolve_book_state() (CLOB freshness -> market_prob_source) and
resolve_reference_price() (strike/reference price priority chain). Both are
plain module-level functions specifically so they can be tested here without
an event loop, a live WS connection, or a constructed Bot.
"""

import datetime as dt
import unittest

import config
from main import (
    MISSING_REFERENCE_PRICE_REASON,
    resolve_book_state,
    resolve_reference_price,
    should_warn_about_missing_market,
)
from market_data.btc_feed import BtcPriceFeed
from polymarket.clob_ws import TokenState
from polymarket.gamma_client import MarketInfo


def _feed_with_history(samples: list[tuple[float, float]]) -> BtcPriceFeed:
    feed = BtcPriceFeed(lookback_sec=90)
    for ts, price in samples:
        feed._history.append((ts, price))
    return feed


def _market(start_ts: float | None, raw: dict | None = None) -> MarketInfo:
    start_date = dt.datetime.fromtimestamp(start_ts, tz=dt.timezone.utc) if start_ts is not None else None
    return MarketInfo(
        slug="m1", question="q", condition_id="c1", start_date=start_date,
        end_date=dt.datetime.fromtimestamp((start_ts or 0) + 300, tz=dt.timezone.utc),
        up_token_id="up1", down_token_id="down1", up_price=0.5, down_price=0.5,
        raw=raw or {},
    )


class TestResolveBookState(unittest.TestCase):
    def test_fresh_orderbook_is_used_and_labeled_live(self):
        up = TokenState(token_id="up1", best_bid=0.55, best_ask=0.57, bid_size=100, ask_size=50,
                         last_update_ts=1000.0)
        res = resolve_book_state(market_up_price=0.5, up_state_raw=up, down_state_raw=None, now=1002.0)
        self.assertEqual(res.market_prob_source, "live_orderbook")
        self.assertAlmostEqual(res.market_prob_up, 0.56, places=9)
        self.assertIsNotNone(res.orderbook_imbalance)
        self.assertAlmostEqual(res.spread, 0.02, places=9)
        self.assertIs(res.up_state, up)  # fresh state passed through for execution pricing

    def test_stale_orderbook_is_rejected_and_falls_back(self):
        up = TokenState(token_id="up1", best_bid=0.10, best_ask=0.12, bid_size=999, ask_size=1,
                         last_update_ts=1000.0)  # would strongly imply "Up" if trusted
        res = resolve_book_state(market_up_price=0.5, up_state_raw=up, down_state_raw=None, now=2000.0)
        self.assertEqual(res.market_prob_source, "fallback_snapshot")
        self.assertEqual(res.market_prob_up, 0.5)  # the fallback price, NOT the stale book's 0.11 midpoint
        self.assertIsNone(res.orderbook_imbalance)  # stale imbalance must never be used either
        self.assertIsNone(res.spread)
        self.assertIsNone(res.up_state)  # stale state is NOT passed through for execution pricing

    def test_stale_orderbook_with_no_fallback_price_yields_no_decision(self):
        up = TokenState(token_id="up1", best_bid=0.10, best_ask=0.12, last_update_ts=1000.0)
        res = resolve_book_state(market_up_price=None, up_state_raw=up, down_state_raw=None, now=2000.0)
        self.assertIsNone(res.market_prob_up)
        self.assertIsNone(res.market_prob_source)  # caller must treat this as HOLD/skip

    def test_missing_orderbook_falls_back_to_snapshot(self):
        # No TokenState at all yet (WS just subscribed) — same treatment as stale.
        res = resolve_book_state(market_up_price=0.63, up_state_raw=None, down_state_raw=None, now=1000.0)
        self.assertEqual(res.market_prob_source, "fallback_snapshot")
        self.assertEqual(res.market_prob_up, 0.63)

    def test_missing_orderbook_and_missing_fallback_yields_no_decision(self):
        res = resolve_book_state(market_up_price=None, up_state_raw=None, down_state_raw=None, now=1000.0)
        self.assertIsNone(res.market_prob_up)
        self.assertIsNone(res.market_prob_source)

    def test_stale_book_cannot_produce_a_trade_signal_via_imbalance(self):
        # Even a wildly imbalanced STALE book must not leak into the imbalance
        # field once resolve_book_state has decided it's unusable.
        up = TokenState(token_id="up1", best_bid=0.20, best_ask=0.22, bid_size=10_000, ask_size=1,
                         last_update_ts=1000.0)
        res = resolve_book_state(market_up_price=0.5, up_state_raw=up, down_state_raw=None,
                                  now=1000.0 + config.CLOB_DATA_MAX_AGE_SEC + 1)
        self.assertIsNone(res.orderbook_imbalance)
        self.assertEqual(res.market_prob_source, "fallback_snapshot")

    def test_fresh_down_state_used_for_market_no_price(self):
        down = TokenState(token_id="down1", best_bid=0.40, best_ask=0.44, last_update_ts=1000.0)
        res = resolve_book_state(market_up_price=0.5, up_state_raw=None, down_state_raw=down, now=1001.0)
        self.assertAlmostEqual(res.market_no_price, 0.42, places=9)
        self.assertIs(res.down_state, down)

    def test_stale_down_state_is_not_used_for_market_no_price(self):
        down = TokenState(token_id="down1", best_bid=0.40, best_ask=0.44, last_update_ts=1000.0)
        res = resolve_book_state(market_up_price=0.5, up_state_raw=None, down_state_raw=down, now=5000.0)
        self.assertIsNone(res.market_no_price)
        self.assertIsNone(res.down_state)


class TestResolveReferencePrice(unittest.TestCase):
    def test_structured_metadata_used_when_available(self):
        market = _market(start_ts=1000.0, raw={"priceToBeat": "80123.45"})
        feed = _feed_with_history([(1000.0, 79000.0)])  # would be used if structured metadata weren't present
        price, reason = resolve_reference_price(market, feed)
        self.assertEqual(reason, "structured_metadata")
        self.assertAlmostEqual(price, 80123.45, places=2)

    def test_historical_btc_price_used_when_close_to_start(self):
        market = _market(start_ts=1000.0)
        feed = _feed_with_history([(998.5, 80000.0)])  # 1.5s before start — well within tolerance
        price, reason = resolve_reference_price(market, feed)
        self.assertEqual(reason, "historical_btc_price")
        self.assertEqual(price, 80000.0)

    def test_missing_strike_and_missing_history_means_no_trade(self):
        market = _market(start_ts=1000.0)
        feed = BtcPriceFeed()  # no history at all
        price, reason = resolve_reference_price(market, feed)
        self.assertIsNone(price)
        self.assertEqual(reason, MISSING_REFERENCE_PRICE_REASON)

    def test_current_btc_price_is_never_used_as_a_fabricated_strike(self):
        # The nearest sample to start_date is stale (feed had a long outage
        # spanning market open); a "current" price DOES exist in the feed,
        # far later, but must never be substituted as the strike.
        market = _market(start_ts=1000.0)
        feed = _feed_with_history([(700.0, 79000.0), (1400.0, 85000.0)])  # 1400.0 is "current"-ish, way after start
        price, reason = resolve_reference_price(market, feed)
        self.assertIsNone(price)  # NOT 79000 (too stale) and NOT 85000 (that's not even at-or-before start)
        self.assertEqual(reason, MISSING_REFERENCE_PRICE_REASON)

    def test_stale_historical_sample_beyond_tolerance_is_rejected(self):
        market = _market(start_ts=1000.0)
        # Nearest at-or-before sample is real but 60s stale — feed outage
        # spanning market open. Must not be silently used as the strike.
        feed = _feed_with_history([(940.0, 79500.0)])
        price, reason = resolve_reference_price(market, feed, max_staleness_sec=5.0)
        self.assertIsNone(price)
        self.assertEqual(reason, MISSING_REFERENCE_PRICE_REASON)

    def test_normal_continuous_feed_startup_is_unchanged(self):
        # The common case: feed has been running continuously, a sample
        # lands essentially exactly at market start.
        market = _market(start_ts=2000.0)
        feed = _feed_with_history([(1990.0, 81000.0), (1999.7, 81005.0), (2010.0, 81020.0)])
        price, reason = resolve_reference_price(market, feed)
        self.assertEqual(reason, "historical_btc_price")
        self.assertEqual(price, 81005.0)  # the 1999.7 sample — nearest at-or-before 2000.0

    def test_bot_discovered_market_mid_window_with_no_history_before_start_cannot_corrupt_k(self):
        # Bot process restarted mid-market: its BTC feed only started
        # accumulating history AFTER the market's actual start_date, so
        # there is no sample at-or-before start at all.
        market = _market(start_ts=1000.0)
        feed = _feed_with_history([(1050.0, 82000.0), (1060.0, 82010.0)])  # both AFTER start_date
        price, reason = resolve_reference_price(market, feed)
        self.assertIsNone(price)
        self.assertEqual(reason, MISSING_REFERENCE_PRICE_REASON)

    def test_no_start_date_and_no_structured_metadata_means_no_trade(self):
        market = _market(start_ts=None)
        feed = _feed_with_history([(1000.0, 80000.0)])
        price, reason = resolve_reference_price(market, feed)
        self.assertIsNone(price)
        self.assertEqual(reason, MISSING_REFERENCE_PRICE_REASON)


class TestShouldWarnAboutMissingMarket(unittest.TestCase):
    def test_no_warning_before_third_miss(self):
        for n in (0, 1, 2):
            self.assertFalse(should_warn_about_missing_market(n))

    def test_warns_on_third_miss(self):
        self.assertTrue(should_warn_about_missing_market(3))

    def test_no_warning_on_misses_between_repeats(self):
        for n in (4, 5, 6, 7, 8):
            self.assertFalse(should_warn_about_missing_market(n))

    def test_warns_again_every_ninth_miss_after_the_first(self):
        self.assertTrue(should_warn_about_missing_market(9))
        self.assertTrue(should_warn_about_missing_market(18))
        self.assertTrue(should_warn_about_missing_market(27))

    def test_config_slug_filter_matches_the_real_current_format(self):
        # Regression test for the actual bug that produced zero decisions
        # over a 30-minute run: MARKET_SLUG_CONTAINS used to be
        # "bitcoin-up-or-down", which does not match any real market slug
        # (confirmed against a live Polymarket event page: the real format
        # is "btc-updown-5m-<unix-timestamp>"). This doesn't guarantee
        # Polymarket won't rename things again, but it pins down today's
        # known-correct value so a future accidental revert is caught here
        # instead of silently producing another empty run.
        self.assertIn(config.MARKET_SLUG_CONTAINS, "btc-updown-5m-1776028800")


if __name__ == "__main__":
    unittest.main()

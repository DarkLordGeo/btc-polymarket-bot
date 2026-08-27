import unittest

from market_data.btc_feed import BtcPriceFeed


def _feed_with_history(samples: list[tuple[float, float]]) -> BtcPriceFeed:
    """samples: [(timestamp, price), ...]. Bypasses _record()'s use of
    time.time() so tests fully control the clock."""
    feed = BtcPriceFeed(lookback_sec=90)
    for ts, price in samples:
        feed._history.append((ts, price))
    return feed


class TestBtcPriceFreshness(unittest.TestCase):
    def test_fresh_price_can_be_used(self):
        feed = _feed_with_history([(1000.0, 80000.0)])
        self.assertTrue(feed.is_fresh(max_age_sec=10.0, now=1005.0))
        self.assertEqual(feed.price, 80000.0)

    def test_stale_price_causes_hold(self):
        feed = _feed_with_history([(1000.0, 80000.0)])
        self.assertFalse(feed.is_fresh(max_age_sec=10.0, now=1030.0))
        # .price still returns the old value — is_fresh() is what a caller
        # must check before trusting/using it; this is deliberate, not a bug.
        self.assertEqual(feed.price, 80000.0)

    def test_missing_price_causes_hold(self):
        feed = BtcPriceFeed()
        self.assertIsNone(feed.price)
        self.assertIsNone(feed.price_ts)
        self.assertFalse(feed.is_fresh(max_age_sec=10.0, now=1000.0))

    def test_new_ticks_update_the_timestamp(self):
        feed = BtcPriceFeed()
        feed._record(80000.0)
        first_ts = feed.price_ts
        self.assertIsNotNone(first_ts)
        feed._record(80010.0)
        self.assertEqual(feed.price, 80010.0)
        self.assertGreaterEqual(feed.price_ts, first_ts)

    def test_stale_feed_reports_correct_price_ts_for_diagnostics(self):
        feed = _feed_with_history([(500.0, 79000.0), (1000.0, 80000.0)])
        self.assertEqual(feed.price_ts, 1000.0)
        self.assertFalse(feed.is_fresh(max_age_sec=10.0, now=2000.0))


class TestPriceAtOrBeforeWithTs(unittest.TestCase):
    def test_matches_price_at_or_before(self):
        feed = _feed_with_history([(100.0, 100.0), (200.0, 200.0), (300.0, 300.0)])
        self.assertEqual(feed.price_at_or_before(250.0), 200.0)
        hit = feed.price_at_or_before_with_ts(250.0)
        self.assertEqual(hit, (200.0, 200.0))

    def test_no_sample_before_ts_returns_none(self):
        feed = _feed_with_history([(500.0, 500.0)])
        self.assertIsNone(feed.price_at_or_before(100.0))
        self.assertIsNone(feed.price_at_or_before_with_ts(100.0))

    def test_exact_timestamp_match_is_included(self):
        feed = _feed_with_history([(100.0, 100.0)])
        self.assertEqual(feed.price_at_or_before_with_ts(100.0), (100.0, 100.0))


if __name__ == "__main__":
    unittest.main()

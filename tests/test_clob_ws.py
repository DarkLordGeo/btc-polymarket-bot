import unittest

from polymarket.clob_ws import TokenState


class TestTokenStateFreshness(unittest.TestCase):
    def test_fresh_state_is_fresh(self):
        st = TokenState(token_id="t1", best_bid=0.48, best_ask=0.52, last_update_ts=1000.0)
        self.assertTrue(st.is_fresh(max_age_sec=10.0, now=1005.0))  # 5s old, under the 10s max

    def test_exactly_at_max_age_is_still_fresh(self):
        # <= max_age_sec, not < — boundary is inclusive.
        st = TokenState(token_id="t1", best_bid=0.48, best_ask=0.52, last_update_ts=1000.0)
        self.assertTrue(st.is_fresh(max_age_sec=10.0, now=1010.0))

    def test_stale_state_is_rejected(self):
        st = TokenState(token_id="t1", best_bid=0.48, best_ask=0.52, last_update_ts=1000.0)
        self.assertFalse(st.is_fresh(max_age_sec=10.0, now=1011.0))  # 11s old > 10s max

    def test_very_stale_state_is_rejected(self):
        st = TokenState(token_id="t1", best_bid=0.48, best_ask=0.52, last_update_ts=1000.0)
        self.assertFalse(st.is_fresh(max_age_sec=10.0, now=1600.0))  # 10 minutes old

    def test_never_updated_state_is_never_fresh(self):
        # last_update_ts is None: there's no data to be fresh OR stale, it's
        # simply missing — must be treated the same as stale (never live).
        st = TokenState(token_id="t1")
        self.assertFalse(st.is_fresh(max_age_sec=10.0, now=1000.0))
        self.assertFalse(st.is_fresh(max_age_sec=1e9, now=1000.0))  # even a huge tolerance doesn't help

    def test_stale_state_still_has_a_midpoint_in_memory(self):
        # The whole point of the freshness check: the OLD data doesn't
        # disappear from the object, it just must not be trusted anymore.
        st = TokenState(token_id="t1", best_bid=0.48, best_ask=0.52, last_update_ts=1000.0)
        self.assertIsNotNone(st.midpoint())  # data is still sitting right there...
        self.assertFalse(st.is_fresh(max_age_sec=10.0, now=1600.0))  # ...but must be treated as unusable


if __name__ == "__main__":
    unittest.main()

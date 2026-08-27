import os
import tempfile
import unittest

import config
import storage.logging_db as db
import replay


def _seed_market(slug, *, ts, btc_price, reference_price, seconds_remaining, market_yes_price,
                  realized_vol, orderbook_imbalance, resolved_up, resolved=True):
    db.log_decision({
        "ts": ts, "strategy": "C", "market_slug": slug, "question": "q",
        "market_start_ts": ts - 60.0, "market_end_ts": ts + seconds_remaining,
        "seconds_remaining": seconds_remaining, "btc_price": btc_price,
        "reference_price": reference_price, "btc_momentum": 0.0,
        "realized_vol": realized_vol, "vol_window_actual_sec": 90.0,
        "model_prob_up": 0.5, "model_prob_down": 0.5, "market_yes_price": market_yes_price,
        "market_no_price": 1 - market_yes_price, "market_implied_prob": market_yes_price,
        "market_prob_source": "live_orderbook",
        "raw_edge": 0.0, "cost_buffer": 0.02, "net_edge": 0.0,
        "orderbook_imbalance": orderbook_imbalance, "spread": 0.02,
        "action": "HOLD", "position_size": None, "entry_price": None, "traded": 0,
        "reason": "seed",
    })
    if resolved:
        db.upsert_market_outcome(
            market_slug=slug, question="q", start_ts=ts - 60.0, end_ts=ts + seconds_remaining,
            reference_price=reference_price, resolution_btc_price=btc_price,
            resolved_up=resolved_up, resolution_source="gamma_official",
        )


class TestReplay(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        self._orig_db_path = config.DB_PATH
        config.DB_PATH = path
        self._tmp_path = path
        db.init_db()
        self._orig_min_edge = config.MIN_EDGE_TO_TRADE

    def tearDown(self):
        config.DB_PATH = self._orig_db_path
        config.MIN_EDGE_TO_TRADE = self._orig_min_edge
        try:
            os.remove(self._tmp_path)
        except OSError:
            pass

    def test_no_data_returns_empty(self):
        rows = replay.load_replayable_rows()
        self.assertEqual(rows, [])

    def test_unresolved_markets_are_excluded(self):
        _seed_market("m-unresolved", ts=100.0, btc_price=101.0, reference_price=100.0,
                     seconds_remaining=120.0, market_yes_price=0.5, realized_vol=0.0003,
                     orderbook_imbalance=0.0, resolved_up=None, resolved=False)
        rows = replay.load_replayable_rows()
        self.assertEqual(rows, [])

    def test_resolved_market_is_included(self):
        _seed_market("m1", ts=100.0, btc_price=80900.0, reference_price=80750.0,
                     seconds_remaining=120.0, market_yes_price=0.5, realized_vol=0.0003,
                     orderbook_imbalance=0.2, resolved_up=True)
        rows = replay.load_replayable_rows()
        self.assertEqual(len(rows), 1)

    def test_strategy_a_never_trades_in_replay(self):
        for i in range(5):
            _seed_market(f"m{i}", ts=float(i * 400), btc_price=80900.0 + i, reference_price=80750.0,
                         seconds_remaining=120.0, market_yes_price=0.5, realized_vol=0.0003,
                         orderbook_imbalance=0.3, resolved_up=True)
        rows = replay.load_replayable_rows()
        results = replay.replay(rows, strategies=("A", "B", "C"))
        self.assertEqual(results["A"]["trades"], 0)

    def test_raising_min_edge_threshold_never_increases_trade_count(self):
        for i in range(8):
            # Alternate big up/down moves so edge crosses a range of thresholds.
            move = 300 if i % 2 == 0 else -300
            _seed_market(f"m{i}", ts=float(i * 400), btc_price=80750.0 + move, reference_price=80750.0,
                         seconds_remaining=120.0, market_yes_price=0.5, realized_vol=0.0002,
                         orderbook_imbalance=0.0, resolved_up=(move > 0))
        rows = replay.load_replayable_rows()

        config.MIN_EDGE_TO_TRADE = 0.01
        loose = replay.replay(rows, strategies=("B",))
        config.MIN_EDGE_TO_TRADE = 0.40
        strict = replay.replay(rows, strategies=("B",))

        self.assertGreaterEqual(loose["B"]["trades"], strict["B"]["trades"])

    def test_replay_does_not_write_to_the_live_db(self):
        _seed_market("m1", ts=100.0, btc_price=80900.0, reference_price=80750.0,
                     seconds_remaining=120.0, market_yes_price=0.5, realized_vol=0.0003,
                     orderbook_imbalance=0.2, resolved_up=True)
        rows = replay.load_replayable_rows()
        trades_before = len(db.all_trades())
        replay.replay(rows)
        trades_after = len(db.all_trades())
        self.assertEqual(trades_before, trades_after)  # replay uses in-memory brokers only


class TestReplaySettlementTiming(unittest.TestCase):
    """
    Regression tests for the settlement-timing bug: replay() used to call
    broker.settle() INSIDE the per-tick loop, which immediately freed
    has_open_position() back to False and let a single 5-minute market
    fabricate a fresh "trade" on every subsequent tick a signal fired.
    These seed many qualifying ticks for ONE market and assert exactly one
    trade results — the only way that can happen is if the position opened
    once, stayed open across every later tick (never re-settled mid-window),
    and was settled exactly once after the market's own tick sequence ended.
    """

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        self._orig_db_path = config.DB_PATH
        config.DB_PATH = path
        self._tmp_path = path
        db.init_db()

    def tearDown(self):
        config.DB_PATH = self._orig_db_path
        try:
            os.remove(self._tmp_path)
        except OSError:
            pass

    def _seed_many_qualifying_ticks(self, slug: str, n_ticks: int, *, resolved_up: bool = True) -> None:
        # Every one of these ticks, evaluated in isolation, clears the
        # default MIN_EDGE_TO_TRADE (0.06) comfortably — under the old bug
        # each of these would have opened AND immediately settled its own
        # trade, for n_ticks fabricated "trades" out of one real market.
        for i in range(n_ticks):
            _seed_market(
                slug, ts=100.0 + i * 3.0, btc_price=80750.0 + 300, reference_price=80750.0,
                seconds_remaining=200.0 - i * 3.0, market_yes_price=0.5, realized_vol=0.0003,
                orderbook_imbalance=0.2, resolved_up=resolved_up,
            )

    def test_position_settles_exactly_once_despite_many_qualifying_ticks(self):
        self._seed_many_qualifying_ticks("m-multitick", n_ticks=15)
        rows = replay.load_replayable_rows()
        results = replay.replay(rows, strategies=("C",))
        self.assertEqual(results["C"]["trades"], 1)  # NOT 15

    def test_multiple_ticks_in_one_market_cannot_create_repeated_trades(self):
        # Same scenario, more ticks — the count of qualifying ticks must not
        # correlate with the count of trades produced.
        self._seed_many_qualifying_ticks("m-multitick2", n_ticks=40)
        rows = replay.load_replayable_rows()
        results = replay.replay(rows, strategies=("B", "C"))
        self.assertEqual(results["B"]["trades"], 1)
        self.assertEqual(results["C"]["trades"], 1)

    def test_pnl_reflects_a_single_stake_not_an_accumulation_of_many(self):
        # A single settled trade's |pnl| is bounded by a single stake
        # (MAX_STAKE_USD at most); if the old bug were still present, total
        # pnl across many fabricated trades on the same market would be a
        # multiple of that.
        self._seed_many_qualifying_ticks("m-multitick3", n_ticks=20, resolved_up=True)
        rows = replay.load_replayable_rows()
        results = replay.replay(rows, strategies=("C",))
        self.assertEqual(results["C"]["trades"], 1)
        self.assertLessEqual(abs(results["C"]["total_pnl"]), config.MAX_STAKE_USD)

    def test_two_separate_markets_each_settle_their_own_position_once(self):
        # Confirms the fix doesn't just suppress everything — two distinct
        # markets should each still produce their own trade.
        self._seed_many_qualifying_ticks("m-a", n_ticks=10, resolved_up=True)
        self._seed_many_qualifying_ticks("m-b", n_ticks=10, resolved_up=False)
        rows = replay.load_replayable_rows()
        results = replay.replay(rows, strategies=("C",))
        self.assertEqual(results["C"]["trades"], 2)

    def test_group_rows_by_market_preserves_within_market_tick_order(self):
        self._seed_many_qualifying_ticks("m-order", n_ticks=5)
        rows = replay.load_replayable_rows()
        grouped = replay._group_rows_by_market(rows)
        tss = [r["ts"] for r in grouped["m-order"]]
        self.assertEqual(tss, sorted(tss))


if __name__ == "__main__":
    unittest.main()

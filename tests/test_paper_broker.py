import unittest

import config
from broker.paper_broker import PaperBroker
from engine.decision_engine import Action


class TestSettlement(unittest.TestCase):
    def test_winning_up_position_pays_out(self):
        broker = PaperBroker()
        broker.open_position(
            market_slug="m1", question="q", action=Action.BUY_UP, entry_price=0.5,
            stake=10.0, end_ts=0.0, reasoning_snapshot={},
        )
        trade = broker.settle("m1", resolved_up=True)
        self.assertTrue(trade.won)
        fee = 10.0 * config.PAPER_FEE_RATE
        expected_shares = (10.0 - fee) / 0.5
        self.assertAlmostEqual(trade.payout, expected_shares, places=9)
        self.assertAlmostEqual(trade.pnl, expected_shares - 10.0, places=9)

    def test_losing_up_position_pays_nothing(self):
        broker = PaperBroker()
        broker.open_position(
            market_slug="m1", question="q", action=Action.BUY_UP, entry_price=0.5,
            stake=10.0, end_ts=0.0, reasoning_snapshot={},
        )
        trade = broker.settle("m1", resolved_up=False)
        self.assertFalse(trade.won)
        self.assertEqual(trade.payout, 0.0)
        self.assertAlmostEqual(trade.pnl, -10.0, places=9)

    def test_down_position_wins_when_market_resolves_down(self):
        broker = PaperBroker()
        broker.open_position(
            market_slug="m1", question="q", action=Action.BUY_DOWN, entry_price=0.4,
            stake=20.0, end_ts=0.0, reasoning_snapshot={},
        )
        trade = broker.settle("m1", resolved_up=False)
        self.assertTrue(trade.won)

    def test_down_position_loses_when_market_resolves_up(self):
        broker = PaperBroker()
        broker.open_position(
            market_slug="m1", question="q", action=Action.BUY_DOWN, entry_price=0.4,
            stake=20.0, end_ts=0.0, reasoning_snapshot={},
        )
        trade = broker.settle("m1", resolved_up=True)
        self.assertFalse(trade.won)

    def test_settle_unknown_market_returns_none(self):
        broker = PaperBroker()
        self.assertIsNone(broker.settle("nonexistent", resolved_up=True))

    def test_settle_removes_open_position(self):
        broker = PaperBroker()
        broker.open_position(
            market_slug="m1", question="q", action=Action.BUY_UP, entry_price=0.5,
            stake=10.0, end_ts=0.0, reasoning_snapshot={},
        )
        self.assertTrue(broker.has_open_position("m1"))
        broker.settle("m1", resolved_up=True)
        self.assertFalse(broker.has_open_position("m1"))

    def test_fee_reduces_shares_bought(self):
        broker = PaperBroker()
        pos = broker.open_position(
            market_slug="m1", question="q", action=Action.BUY_UP, entry_price=0.5,
            stake=100.0, end_ts=0.0, reasoning_snapshot={},
        )
        self.assertAlmostEqual(pos.fee_paid, 100.0 * config.PAPER_FEE_RATE, places=9)
        self.assertLess(pos.shares, 100.0 / 0.5)  # fewer shares than a fee-free fill

    def test_zero_entry_price_does_not_crash(self):
        broker = PaperBroker()
        pos = broker.open_position(
            market_slug="m1", question="q", action=Action.BUY_UP, entry_price=0.0,
            stake=10.0, end_ts=0.0, reasoning_snapshot={},
        )
        self.assertEqual(pos.shares, 0.0)


if __name__ == "__main__":
    unittest.main()

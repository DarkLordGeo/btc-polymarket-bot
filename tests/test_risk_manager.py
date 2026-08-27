import time
import unittest

import config
from engine.risk_manager import RiskManager


class TestPositionSizing(unittest.TestCase):
    def test_stake_scales_with_edge_up_to_threshold(self):
        rm = RiskManager(bankroll=1000.0)
        small_edge_stake = rm.stake_for(config.MIN_EDGE_TO_TRADE / 2)
        full_edge_stake = rm.stake_for(config.MIN_EDGE_TO_TRADE)
        self.assertLess(small_edge_stake, full_edge_stake)

    def test_stake_caps_at_max_stake_usd(self):
        rm = RiskManager(bankroll=1_000_000.0)  # huge bankroll
        stake = rm.stake_for(1.0)  # huge edge
        self.assertLessEqual(stake, config.MAX_STAKE_USD)

    def test_stake_never_exceeds_bankroll(self):
        rm = RiskManager(bankroll=5.0)
        stake = rm.stake_for(1.0)
        self.assertLessEqual(stake, 5.0)

    def test_stake_is_nonnegative(self):
        rm = RiskManager(bankroll=1000.0)
        self.assertGreaterEqual(rm.stake_for(0.0), 0.0)
        self.assertGreaterEqual(rm.stake_for(-0.5), 0.0)


class TestDailyLossBreaker(unittest.TestCase):
    def test_not_hit_initially(self):
        rm = RiskManager(bankroll=1000.0)
        self.assertFalse(rm.daily_loss_limit_hit())

    def test_day_start_bankroll_tracks_actual_constructor_arg(self):
        # Regression test: _day_start_bankroll used to default independently
        # from config.STARTING_BANKROLL regardless of the bankroll passed to
        # the constructor, so a RiskManager built with a non-default bankroll
        # silently mis-tracked the daily loss breaker from tick one.
        rm = RiskManager(bankroll=250.0)
        self.assertEqual(rm._day_start_bankroll, 250.0)
        self.assertFalse(rm.daily_loss_limit_hit())

    def test_hit_after_large_loss(self):
        rm = RiskManager(bankroll=1000.0)
        rm.apply_settlement(-1000.0 * config.MAX_DAILY_LOSS_FRACTION)
        self.assertTrue(rm.daily_loss_limit_hit())

    def test_can_trade_blocked_after_daily_loss_limit(self):
        rm = RiskManager(bankroll=1000.0)
        rm.apply_settlement(-1000.0 * config.MAX_DAILY_LOSS_FRACTION)
        can_trade, reason = rm.can_trade("some-market")
        self.assertFalse(can_trade)
        self.assertIn("daily loss", reason)

    def test_reset_day_clears_the_breaker(self):
        rm = RiskManager(bankroll=1000.0)
        rm.apply_settlement(-1000.0 * config.MAX_DAILY_LOSS_FRACTION)
        self.assertTrue(rm.daily_loss_limit_hit())
        rm.reset_day()
        self.assertFalse(rm.daily_loss_limit_hit())


class TestCooldown(unittest.TestCase):
    def test_cooldown_blocks_immediate_retrade(self):
        rm = RiskManager(bankroll=1000.0)
        rm.record_trade("mkt-1")
        can_trade, reason = rm.can_trade("mkt-1")
        self.assertFalse(can_trade)
        self.assertIn("cooldown", reason)

    def test_cooldown_is_per_market(self):
        rm = RiskManager(bankroll=1000.0)
        rm.record_trade("mkt-1")
        can_trade, _ = rm.can_trade("mkt-2")
        self.assertTrue(can_trade)

    def test_cooldown_expires(self):
        rm = RiskManager(bankroll=1000.0)
        rm.record_trade("mkt-1")
        rm._last_trade_ts_by_market["mkt-1"] = time.time() - config.TRADE_COOLDOWN_SEC - 1
        can_trade, _ = rm.can_trade("mkt-1")
        self.assertTrue(can_trade)


class TestLiquidityGate(unittest.TestCase):
    def test_disabled_by_default_does_not_block(self):
        self.assertEqual(config.MIN_LIQUIDITY_USD, 0.0, "baseline default should leave the gate disabled")
        rm = RiskManager(bankroll=1000.0)
        can_trade, _ = rm.can_trade("mkt-1", available_liquidity_usd=0.01)
        self.assertTrue(can_trade)

    def test_unknown_liquidity_never_blocks(self):
        rm = RiskManager(bankroll=1000.0)
        can_trade, _ = rm.can_trade("mkt-1", available_liquidity_usd=None)
        self.assertTrue(can_trade)

    def test_enabled_gate_blocks_thin_liquidity(self):
        original = config.MIN_LIQUIDITY_USD
        try:
            config.MIN_LIQUIDITY_USD = 100.0
            rm = RiskManager(bankroll=1000.0)
            can_trade, reason = rm.can_trade("mkt-1", available_liquidity_usd=10.0)
            self.assertFalse(can_trade)
            self.assertIn("liquidity", reason)

            can_trade2, _ = rm.can_trade("mkt-2", available_liquidity_usd=200.0)
            self.assertTrue(can_trade2)
        finally:
            config.MIN_LIQUIDITY_USD = original


if __name__ == "__main__":
    unittest.main()

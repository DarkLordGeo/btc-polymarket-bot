import math
import unittest

from engine.decision_engine import (
    Action,
    cost_buffer_prob,
    decide,
    estimate_sigma_per_sqrt_sec,
    fair_probability_up,
    mean_log_return,
    net_edge_after_costs,
)


class TestProbabilityCalculations(unittest.TestCase):
    def test_at_reference_price_no_vol_is_neutral(self):
        p = fair_probability_up(100.0, 100.0, 60, None)
        self.assertAlmostEqual(p, 0.5, places=9)

    def test_above_reference_with_vol_increases_probability(self):
        sigma = estimate_sigma_per_sqrt_sec([0.0004] * 5, window_span_sec=40)
        p = fair_probability_up(80800, 80750, 120, sigma)
        self.assertGreater(p, 0.5)

    def test_below_reference_with_vol_decreases_probability(self):
        sigma = estimate_sigma_per_sqrt_sec([0.0004] * 5, window_span_sec=40)
        p = fair_probability_up(80700, 80750, 120, sigma)
        self.assertLess(p, 0.5)

    def test_symmetric_around_reference(self):
        sigma = estimate_sigma_per_sqrt_sec([0.0003] * 5, window_span_sec=40)
        p_up = fair_probability_up(80800, 80750, 120, sigma)
        p_down = fair_probability_up(80700, 80750, 120, sigma)
        # log(80800/80750) != -log(80700/80750) exactly, but very close for
        # small moves — should be within a fraction of a percent of symmetric.
        self.assertAlmostEqual((p_up - 0.5), -(p_down - 0.5), places=2)

    def test_orderbook_imbalance_nudges_probability_up(self):
        sigma = estimate_sigma_per_sqrt_sec([0.0002] * 5, window_span_sec=40)
        base = fair_probability_up(80750, 80750, 120, sigma, orderbook_imbalance=None)
        nudged = fair_probability_up(80750, 80750, 120, sigma, orderbook_imbalance=1.0)
        self.assertGreater(nudged, base)

    def test_probability_is_clamped(self):
        # Absurdly large distance / tiny vol should clamp, not blow past [0.01, 0.99].
        sigma = 1e-9
        p = fair_probability_up(999999, 100, 60, sigma)
        self.assertLessEqual(p, 0.99)
        p2 = fair_probability_up(1, 100, 60, sigma)
        self.assertGreaterEqual(p2, 0.01)

    def test_invalid_prices_return_neutral(self):
        self.assertEqual(fair_probability_up(0, 100, 60, 0.001), 0.5)
        self.assertEqual(fair_probability_up(100, 0, 60, 0.001), 0.5)

    def test_no_vol_estimate_falls_back_to_weak_signal(self):
        p_up = fair_probability_up(101, 100, 60, None)
        p_down = fair_probability_up(99, 100, 60, None)
        self.assertGreater(p_up, 0.5)
        self.assertLess(p_down, 0.5)


class TestSigmaEstimation(unittest.TestCase):
    def test_empty_returns_is_none(self):
        self.assertIsNone(estimate_sigma_per_sqrt_sec([], 40))

    def test_zero_span_is_none(self):
        self.assertIsNone(estimate_sigma_per_sqrt_sec([0.001], 0))

    def test_larger_returns_give_larger_sigma(self):
        small = estimate_sigma_per_sqrt_sec([0.0001] * 5, 40)
        large = estimate_sigma_per_sqrt_sec([0.001] * 5, 40)
        self.assertGreater(large, small)

    def test_matches_hand_computed_value(self):
        returns = [0.001, -0.001, 0.002]
        span = 30.0
        expected = math.sqrt(sum(r * r for r in returns) / span)
        self.assertAlmostEqual(estimate_sigma_per_sqrt_sec(returns, span), expected, places=12)


class TestMomentum(unittest.TestCase):
    def test_empty_is_none(self):
        self.assertIsNone(mean_log_return([]))

    def test_mean_of_returns(self):
        self.assertAlmostEqual(mean_log_return([0.01, -0.01, 0.02]), 0.02 / 3, places=9)


class TestCostBufferAndNetEdge(unittest.TestCase):
    def test_cost_buffer_combines_half_spread_fee_and_extra(self):
        buf = cost_buffer_prob(spread=0.04, fee_rate=0.02, extra_buffer=0.01)
        self.assertAlmostEqual(buf, 0.02 + 0.02 + 0.01, places=9)

    def test_cost_buffer_with_no_spread(self):
        buf = cost_buffer_prob(spread=None, fee_rate=0.02, extra_buffer=0.0)
        self.assertAlmostEqual(buf, 0.02, places=9)

    def test_net_edge_shrinks_toward_zero(self):
        self.assertAlmostEqual(net_edge_after_costs(0.10, 0.03), 0.07, places=9)
        self.assertAlmostEqual(net_edge_after_costs(-0.10, 0.03), -0.07, places=9)

    def test_net_edge_floors_at_zero_not_negative(self):
        # A cost buffer bigger than the edge should floor at 0, never flip sign.
        self.assertEqual(net_edge_after_costs(0.02, 0.05), 0.0)
        self.assertEqual(net_edge_after_costs(-0.02, 0.05), 0.0)

    def test_net_edge_of_zero_edge_is_zero(self):
        self.assertEqual(net_edge_after_costs(0.0, 0.05), 0.0)


class TestDecide(unittest.TestCase):
    def test_buy_up_when_edge_clears_threshold(self):
        d = decide(market_prob_up=0.50, fair_prob_up=0.60, seconds_remaining=120, sigma_per_sqrt_sec=0.001)
        self.assertEqual(d.action, Action.BUY_UP)
        self.assertAlmostEqual(d.edge, 0.10, places=9)

    def test_buy_down_when_edge_clears_threshold_negative(self):
        d = decide(market_prob_up=0.50, fair_prob_up=0.40, seconds_remaining=120, sigma_per_sqrt_sec=0.001)
        self.assertEqual(d.action, Action.BUY_DOWN)

    def test_hold_when_edge_below_threshold(self):
        d = decide(market_prob_up=0.50, fair_prob_up=0.52, seconds_remaining=120, sigma_per_sqrt_sec=0.001)
        self.assertEqual(d.action, Action.HOLD)

    def test_hold_near_expiry_even_with_large_edge(self):
        import config

        d = decide(market_prob_up=0.30, fair_prob_up=0.90, seconds_remaining=5, sigma_per_sqrt_sec=0.001)
        self.assertEqual(d.action, Action.HOLD)
        self.assertLess(5, config.MIN_SECONDS_REMAINING_TO_TRADE)

    def test_edge_exactly_at_threshold_trades(self):
        import config

        d = decide(market_prob_up=0.50, fair_prob_up=0.50 + config.MIN_EDGE_TO_TRADE,
                    seconds_remaining=120, sigma_per_sqrt_sec=0.001)
        self.assertEqual(d.action, Action.BUY_UP)

    def test_decide_defaults_cost_buffer_to_zero_so_net_edge_equals_raw_edge(self):
        # A caller that doesn't pass cost_buffer (e.g. an old test, or replay
        # rows with no logged cost) gets net_edge == edge, not a crash and
        # not a silently different threshold.
        d = decide(market_prob_up=0.50, fair_prob_up=0.60, seconds_remaining=120, sigma_per_sqrt_sec=0.001)
        self.assertAlmostEqual(d.net_edge, d.edge, places=9)
        self.assertAlmostEqual(d.cost_buffer, 0.0, places=9)

    def test_net_edge_gating_holds_when_cost_buffer_eats_the_edge(self):
        # Fix 1's whole point: raw edge alone clears the threshold, but once
        # costs are netted out it no longer does — the decision must flip to
        # HOLD, not BUY, and it must be net_edge (not raw edge) driving that.
        import config

        raw_edge = config.MIN_EDGE_TO_TRADE + 0.01  # clears threshold on raw edge alone
        fair = 0.50 + raw_edge
        cost_buffer = 0.02  # big enough that net_edge = raw_edge - 0.02 < MIN_EDGE_TO_TRADE

        d_raw = decide(market_prob_up=0.50, fair_prob_up=fair, seconds_remaining=120,
                        sigma_per_sqrt_sec=0.001, cost_buffer=0.0)
        self.assertEqual(d_raw.action, Action.BUY_UP)  # sanity: raw edge alone would trade

        d_net = decide(market_prob_up=0.50, fair_prob_up=fair, seconds_remaining=120,
                        sigma_per_sqrt_sec=0.001, cost_buffer=cost_buffer)
        self.assertAlmostEqual(d_net.edge, raw_edge, places=9)  # raw edge is still reported...
        self.assertAlmostEqual(d_net.net_edge, raw_edge - cost_buffer, places=9)
        self.assertEqual(d_net.action, Action.HOLD)  # ...but net edge is what gates the action

    def test_net_edge_gating_still_trades_when_edge_survives_costs(self):
        import config

        raw_edge = config.MIN_EDGE_TO_TRADE + 0.10
        fair = 0.50 + raw_edge
        cost_buffer = 0.01  # small enough that net_edge still clears the threshold

        d = decide(market_prob_up=0.50, fair_prob_up=fair, seconds_remaining=120,
                    sigma_per_sqrt_sec=0.001, cost_buffer=cost_buffer)
        self.assertEqual(d.action, Action.BUY_UP)
        self.assertAlmostEqual(d.net_edge, raw_edge - cost_buffer, places=9)

    def test_net_edge_gating_symmetric_for_buy_down(self):
        import config

        raw_edge = -(config.MIN_EDGE_TO_TRADE + 0.01)
        fair = 0.50 + raw_edge
        cost_buffer = 0.02

        d = decide(market_prob_up=0.50, fair_prob_up=fair, seconds_remaining=120,
                    sigma_per_sqrt_sec=0.001, cost_buffer=cost_buffer)
        self.assertEqual(d.action, Action.HOLD)  # cost buffer ate the (negative) edge too


if __name__ == "__main__":
    unittest.main()

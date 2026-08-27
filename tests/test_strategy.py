import unittest

import config
from engine.decision_engine import estimate_sigma_per_sqrt_sec
from engine.strategy import Observation, build_strategy_runners, compute_model_prob_up


def _obs(market_prob_up=0.5, imbalance=0.4, market_prob_source="live_orderbook"):
    sigma = estimate_sigma_per_sqrt_sec([0.0005, 0.0003, -0.0002, 0.0004], window_span_sec=40)
    return Observation(
        btc_price=80800.0, reference_price=80750.0, seconds_remaining=120.0,
        sigma_per_sqrt_sec=sigma, market_prob_up=market_prob_up,
        market_prob_source=market_prob_source, orderbook_imbalance=imbalance,
    )


class TestStrategyComparison(unittest.TestCase):
    def test_strategy_a_is_always_the_market_price(self):
        obs = _obs(market_prob_up=0.63)
        self.assertEqual(compute_model_prob_up("A", obs), 0.63)

    def test_strategy_a_edge_is_always_zero(self):
        # Strategy A's model IS the market price, so by construction there's
        # no gap between "model" and "market" for the decision engine to act on.
        obs = _obs(market_prob_up=0.71)
        model_p = compute_model_prob_up("A", obs)
        self.assertEqual(model_p - obs.market_prob_up, 0.0)

    def test_strategy_b_ignores_imbalance(self):
        obs_no_imbalance = _obs(imbalance=None)
        obs_with_imbalance = _obs(imbalance=1.0)
        p1 = compute_model_prob_up("B", obs_no_imbalance)
        p2 = compute_model_prob_up("B", obs_with_imbalance)
        self.assertAlmostEqual(p1, p2, places=9)

    def test_strategy_c_uses_imbalance(self):
        obs_no_imbalance = _obs(imbalance=None)
        obs_with_imbalance = _obs(imbalance=1.0)
        p1 = compute_model_prob_up("C", obs_no_imbalance)
        p2 = compute_model_prob_up("C", obs_with_imbalance)
        self.assertNotAlmostEqual(p1, p2, places=6)

    def test_b_and_c_agree_when_imbalance_is_zero(self):
        obs = _obs(imbalance=0.0)
        self.assertAlmostEqual(compute_model_prob_up("B", obs), compute_model_prob_up("C", obs), places=9)

    def test_unknown_strategy_raises(self):
        with self.assertRaises(ValueError):
            compute_model_prob_up("Z", _obs())

    def test_build_strategy_runners_creates_one_per_configured_strategy(self):
        runners = build_strategy_runners()
        self.assertEqual(set(runners.keys()), set(config.STRATEGIES))
        # Each runner gets its OWN broker/risk manager (shadow bankrolls) —
        # not shared instances.
        ids = {id(r.broker) for r in runners.values()}
        self.assertEqual(len(ids), len(runners))


if __name__ == "__main__":
    unittest.main()

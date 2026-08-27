import unittest

from analysis.metrics import (
    brier_score,
    bucket_index,
    max_drawdown,
    pearson_corr,
    profit_factor,
    quantile_thresholds,
    regime_from_thresholds,
    safe_mean,
    safe_median,
)


class TestBasicStats(unittest.TestCase):
    def test_safe_mean_empty_is_none(self):
        self.assertIsNone(safe_mean([]))

    def test_safe_mean_ignores_none(self):
        self.assertAlmostEqual(safe_mean([1.0, None, 3.0]), 2.0, places=9)

    def test_safe_median(self):
        self.assertEqual(safe_median([1.0, 2.0, 3.0]), 2.0)


class TestPearsonCorrelation(unittest.TestCase):
    def test_perfect_positive_correlation(self):
        xs = [0.0, 1.0, 2.0, 3.0]
        ys = [0.0, 1.0, 2.0, 3.0]
        self.assertAlmostEqual(pearson_corr(xs, ys), 1.0, places=6)

    def test_perfect_negative_correlation(self):
        xs = [0.0, 1.0, 2.0, 3.0]
        ys = [3.0, 2.0, 1.0, 0.0]
        self.assertAlmostEqual(pearson_corr(xs, ys), -1.0, places=6)

    def test_too_few_points_is_none(self):
        self.assertIsNone(pearson_corr([1.0], [1.0]))
        self.assertIsNone(pearson_corr([], []))

    def test_no_variance_is_none(self):
        self.assertIsNone(pearson_corr([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]))


class TestBrierScore(unittest.TestCase):
    def test_perfect_predictions_score_zero(self):
        self.assertAlmostEqual(brier_score([1.0, 0.0, 1.0], [1, 0, 1]), 0.0, places=9)

    def test_always_wrong_scores_one(self):
        self.assertAlmostEqual(brier_score([0.0, 1.0], [1, 0]), 1.0, places=9)

    def test_coinflip_scores_quarter(self):
        self.assertAlmostEqual(brier_score([0.5, 0.5, 0.5, 0.5], [1, 0, 1, 0]), 0.25, places=9)

    def test_empty_is_none(self):
        self.assertIsNone(brier_score([], []))


class TestProfitFactor(unittest.TestCase):
    def test_typical_case(self):
        pnls = [10.0, -5.0, 20.0, -10.0]
        self.assertAlmostEqual(profit_factor(pnls), 30.0 / 15.0, places=9)

    def test_no_losses_is_infinite(self):
        self.assertEqual(profit_factor([5.0, 10.0]), float("inf"))

    def test_all_zero_is_none(self):
        self.assertIsNone(profit_factor([]))


class TestMaxDrawdown(unittest.TestCase):
    def test_monotonic_gains_have_zero_drawdown(self):
        result = max_drawdown([10.0, 10.0, 10.0], starting_equity=100.0)
        self.assertEqual(result.max_drawdown_abs, 0.0)

    def test_drawdown_after_peak(self):
        # equity: 100 -> 120 -> 90 -> 110.  Peak 120, trough 90 -> dd 30.
        result = max_drawdown([20.0, -30.0, 20.0], starting_equity=100.0)
        self.assertAlmostEqual(result.max_drawdown_abs, 30.0, places=9)
        self.assertAlmostEqual(result.max_drawdown_pct, 30.0 / 120.0, places=9)

    def test_equity_curve_is_cumulative(self):
        result = max_drawdown([5.0, -2.0, 3.0], starting_equity=0.0)
        self.assertEqual(result.equity_curve, [5.0, 3.0, 6.0])


class TestBucketing(unittest.TestCase):
    def test_bucket_index_finds_correct_bucket(self):
        buckets = [(0.0, 0.5), (0.5, 1.0)]
        self.assertEqual(bucket_index(0.3, buckets), 0)
        self.assertEqual(bucket_index(0.7, buckets), 1)

    def test_bucket_index_boundary_is_half_open(self):
        buckets = [(0.0, 0.5), (0.5, 1.0)]
        self.assertEqual(bucket_index(0.5, buckets), 1)  # lower-inclusive

    def test_bucket_index_out_of_range_is_none(self):
        buckets = [(0.5, 1.0)]
        self.assertIsNone(bucket_index(0.1, buckets))


class TestQuantilesAndRegimes(unittest.TestCase):
    def test_quantile_thresholds_splits_into_terciles(self):
        values = list(range(1, 10))  # 1..9
        cuts = quantile_thresholds(values, 3)
        self.assertEqual(len(cuts), 2)
        self.assertTrue(cuts[0] < cuts[1])

    def test_too_few_values_returns_empty(self):
        self.assertEqual(quantile_thresholds([1.0, 2.0], 5), [])

    def test_regime_from_thresholds_low_medium_high(self):
        cuts = [3.0, 6.0]
        self.assertEqual(regime_from_thresholds(1.0, cuts), 0)
        self.assertEqual(regime_from_thresholds(4.0, cuts), 1)
        self.assertEqual(regime_from_thresholds(7.0, cuts), 2)


if __name__ == "__main__":
    unittest.main()

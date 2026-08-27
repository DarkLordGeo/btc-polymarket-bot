import os
import tempfile
import unittest

import config
import storage.logging_db as db
from engine.decision_engine import Action, Decision
from evaluate import calibration_report, edge_outcome_correlation, strategy_summary


def _decision_snapshot(**overrides):
    base = {
        "ts": 1000.0, "strategy": "C", "market_slug": "m1", "question": "q",
        "market_start_ts": 900.0, "market_end_ts": 1200.0, "seconds_remaining": 200.0,
        "btc_price": 80800.0, "reference_price": 80750.0, "btc_momentum": 0.0001,
        "realized_vol": 0.0002, "vol_window_actual_sec": 90.0,
        "model_prob_up": 0.62, "model_prob_down": 0.38, "market_yes_price": 0.5,
        "market_no_price": 0.5, "market_implied_prob": 0.5, "market_prob_source": "live_orderbook",
        "raw_edge": 0.12,
        "cost_buffer": 0.03, "net_edge": 0.09, "orderbook_imbalance": 0.2, "spread": 0.02,
        "action": "BUY_UP", "position_size": 10.0, "entry_price": 0.5, "traded": 1,
        "reason": "test",
    }
    base.update(overrides)
    return base


class DbTestCase(unittest.TestCase):
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


class TestLoggingDb(DbTestCase):
    def test_log_and_read_decision_round_trips(self):
        db.log_decision(_decision_snapshot())
        rows = db.all_decisions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["market_slug"], "m1")
        self.assertAlmostEqual(rows[0]["raw_edge"], 0.12, places=9)

    def test_market_prob_source_round_trips_and_is_not_silently_dropped(self):
        db.log_decision(_decision_snapshot(market_prob_source="fallback_snapshot"))
        row = db.all_decisions()[0]
        self.assertEqual(row["market_prob_source"], "fallback_snapshot")

    def test_missing_snapshot_fields_are_null_not_fabricated(self):
        snap = _decision_snapshot()
        del snap["btc_momentum"]
        db.log_decision(snap)
        row = db.all_decisions()[0]
        self.assertIsNone(row["btc_momentum"])

    def test_market_outcome_upsert_then_update(self):
        db.upsert_market_outcome(
            market_slug="m1", question="q", start_ts=900.0, end_ts=1200.0,
            reference_price=80750.0, resolution_btc_price=None, resolved_up=None,
            resolution_source="pending",
        )
        row = db.get_market_outcome("m1")
        self.assertIsNone(row["resolved_up"])

        db.upsert_market_outcome(
            market_slug="m1", question="q", start_ts=900.0, end_ts=1200.0,
            reference_price=80750.0, resolution_btc_price=80900.0, resolved_up=True,
            resolution_source="gamma_official",
        )
        row2 = db.get_market_outcome("m1")
        self.assertEqual(row2["resolved_up"], 1)
        self.assertEqual(row2["resolution_source"], "gamma_official")
        # still only one row (upsert, not a duplicate insert)
        self.assertEqual(len(db.all_market_outcomes()), 1)

    def test_decisions_with_outcomes_join(self):
        db.log_decision(_decision_snapshot(market_slug="m1", strategy="C"))
        db.upsert_market_outcome(
            market_slug="m1", question="q", start_ts=900.0, end_ts=1200.0,
            reference_price=80750.0, resolution_btc_price=80900.0, resolved_up=True,
            resolution_source="gamma_official",
        )
        joined = db.decisions_with_outcomes()
        self.assertEqual(len(joined), 1)
        self.assertEqual(joined[0]["outcome_resolved_up"], 1)

    def test_decisions_with_outcomes_null_when_unresolved(self):
        db.log_decision(_decision_snapshot(market_slug="m2"))
        joined = db.decisions_with_outcomes()
        self.assertIsNone(joined[0]["outcome_resolved_up"])

    def test_migration_adds_missing_columns_without_crashing(self):
        # Simulate an "old" DB that only has the base id column, then ensure
        # init_db() brings it up to the current schema without erroring.
        import sqlite3

        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("DROP TABLE IF EXISTS decisions")
        conn.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        conn.commit()
        conn.close()
        db.init_db()  # should not raise
        db.log_decision(_decision_snapshot())
        self.assertEqual(len(db.all_decisions()), 1)


class TestCalibrationReport(DbTestCase):
    def test_perfectly_calibrated_high_confidence_bucket(self):
        # 10 decisions all predicting 90% Up, and Up actually happens every time.
        for i in range(10):
            db.log_decision(_decision_snapshot(market_slug=f"m{i}", model_prob_up=0.90, strategy="C", ts=float(i)))
            db.upsert_market_outcome(
                market_slug=f"m{i}", question="q", start_ts=0.0, end_ts=1.0,
                reference_price=100.0, resolution_btc_price=101.0, resolved_up=True,
                resolution_source="gamma_official",
            )
        dwo = db.decisions_with_outcomes()
        report, brier = calibration_report(dwo, "C")
        bucket_80_plus = next(r for r in report if r["bucket"].startswith("80"))
        self.assertEqual(bucket_80_plus["n"], 10)
        self.assertAlmostEqual(bucket_80_plus["hit_rate"], 1.0, places=9)
        self.assertLess(brier, 0.02)  # should be close to 0

    def test_badly_calibrated_bucket_shows_low_hit_rate(self):
        for i in range(10):
            # predicts 90% Up, but it's actually Down every time -> hit rate 0
            db.log_decision(_decision_snapshot(market_slug=f"m{i}", model_prob_up=0.90, strategy="C", ts=float(i)))
            db.upsert_market_outcome(
                market_slug=f"m{i}", question="q", start_ts=0.0, end_ts=1.0,
                reference_price=100.0, resolution_btc_price=99.0, resolved_up=False,
                resolution_source="gamma_official",
            )
        dwo = db.decisions_with_outcomes()
        report, brier = calibration_report(dwo, "C")
        bucket_80_plus = next(r for r in report if r["bucket"].startswith("80"))
        self.assertAlmostEqual(bucket_80_plus["hit_rate"], 0.0, places=9)
        self.assertGreater(brier, 0.7)

    def test_edge_outcome_correlation_positive_when_edge_predicts_outcome(self):
        # Higher edge -> more likely to actually go up.
        edges = [-0.2, -0.1, 0.0, 0.1, 0.2]
        outcomes = [False, False, False, True, True]
        for i, (e, o) in enumerate(zip(edges, outcomes)):
            db.log_decision(_decision_snapshot(market_slug=f"m{i}", raw_edge=e, strategy="C", ts=float(i)))
            db.upsert_market_outcome(
                market_slug=f"m{i}", question="q", start_ts=0.0, end_ts=1.0,
                reference_price=100.0, resolution_btc_price=101.0 if o else 99.0,
                resolved_up=o, resolution_source="gamma_official",
            )
        dwo = db.decisions_with_outcomes()
        corr = edge_outcome_correlation(dwo, "C")
        self.assertGreater(corr, 0.5)


class TestStrategySummary(DbTestCase):
    def test_summary_computed_from_trades_table(self):
        trade_rows = [
            {"strategy": "C", "market_slug": "m1", "question": "q", "side": "UP", "entry_price": 0.5,
             "stake": 10.0, "fee_paid": 0.2, "shares": 19.6, "opened_at": 1.0, "settled_at": 2.0,
             "won": 1, "payout": 19.6, "pnl": 9.6, "edge_at_entry": 0.1, "net_edge_at_entry": 0.07,
             "seconds_remaining_at_entry": 120.0, "orderbook_imbalance_at_entry": 0.1,
             "reasoning_json": "{}"},
            {"strategy": "C", "market_slug": "m2", "question": "q", "side": "DOWN", "entry_price": 0.5,
             "stake": 10.0, "fee_paid": 0.2, "shares": 19.6, "opened_at": 3.0, "settled_at": 4.0,
             "won": 0, "payout": 0.0, "pnl": -10.0, "edge_at_entry": -0.1, "net_edge_at_entry": -0.07,
             "seconds_remaining_at_entry": 60.0, "orderbook_imbalance_at_entry": -0.1,
             "reasoning_json": "{}"},
        ]
        for tr in trade_rows:
            import sqlite3
            conn = sqlite3.connect(config.DB_PATH)
            cols = ",".join(tr.keys())
            placeholders = ",".join("?" for _ in tr)
            conn.execute(f"INSERT INTO trades ({cols}) VALUES ({placeholders})", list(tr.values()))
            conn.commit()
            conn.close()

        trades = db.all_trades()
        summary = strategy_summary(trades, "C")
        self.assertEqual(summary["total_trades"], 2)
        self.assertAlmostEqual(summary["win_rate"], 0.5, places=9)
        self.assertAlmostEqual(summary["total_pnl"], -0.4, places=9)


if __name__ == "__main__":
    unittest.main()

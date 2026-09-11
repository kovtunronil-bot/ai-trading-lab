import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

import brain


class LiveLearningTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="brain_ll_")
        brain.DB_FILE = os.path.join(self._tmp, "test.db")
        brain._DB_CONN = None
        brain.init_db()

    def tearDown(self):
        brain._DB_CONN = None
        try:
            os.remove(brain.DB_FILE)
        except Exception:
            pass

    def _conn(self):
        return sqlite3.connect(brain.DB_FILE)

    # ---------- live loss memory ----------

    def test_log_live_loss_writes_once_per_symbol_regime_day(self):
        first = brain.log_live_loss("SPY", "BEAR_TREND", -5.4)
        second = brain.log_live_loss("SPY", "BEAR_TREND", -6.1)
        self.assertTrue(first)
        self.assertFalse(second)
        conn = self._conn()
        n = conn.execute("SELECT COUNT(*) FROM loss_memory WHERE source='LIVE'").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_live_loss_engages_loss_count_and_should_avoid(self):
        brain.log_live_loss("SPY", "BEAR_TREND", -5.0)
        conn = self._conn()
        for days_ago in (1, 2):
            ts = (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO loss_memory(ts,symbol,strategy,regime,pnl_pct,conditions,source)"
                " VALUES(?,?,?,?,?,?,?)",
                (ts, "SPY", "LIVE-UNREALIZED", "BEAR_TREND", -4.0, "live", "LIVE"))
        conn.commit()
        conn.close()
        self.assertEqual(brain.loss_count("SPY", "BEAR_TREND"), 3)
        self.assertTrue(brain.should_avoid("SPY", "BEAR_TREND"))

    def test_legacy_exit_loss_rows_still_count(self):
        conn = self._conn()
        for i in range(3):
            ts = (datetime.now() - timedelta(days=i)).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO loss_memory(ts,symbol,strategy,regime,pnl_pct,conditions)"
                " VALUES(?,?,?,?,?,?)",
                (ts, "MSFT", "SOME_STRAT", "BEAR_TREND", -4.2, "exit"))
        conn.commit()
        conn.close()
        self.assertEqual(brain.loss_count("MSFT", "BEAR_TREND"), 3)
        self.assertTrue(brain.should_avoid("MSFT", "BEAR_TREND"))

    # ---------- regime drawdown learning ----------

    def test_empirical_cap_factor_no_records_is_one(self):
        self.assertEqual(brain.empirical_cap_factor("BEAR_TREND"), 1.0)

    def test_empirical_cap_factor_tightens_from_recent_worst_only(self):
        conn = self._conn()
        now = datetime.now()
        conn.execute(
            "INSERT INTO regime_drawdown(regime,ts,portfolio_dd,avg_unrealised_pnl,n_positions)"
            " VALUES(?,?,?,?,?)",
            ("BEAR_TREND", (now - timedelta(hours=1)).isoformat(timespec="seconds"), -0.06, -0.04, 5))
        conn.execute(
            "INSERT INTO regime_drawdown(regime,ts,portfolio_dd,avg_unrealised_pnl,n_positions)"
            " VALUES(?,?,?,?,?)",
            ("BEAR_TREND", (now - timedelta(hours=26)).isoformat(timespec="seconds"), -0.12, -0.10, 5))
        conn.execute(
            "INSERT INTO regime_drawdown(regime,ts,portfolio_dd,avg_unrealised_pnl,n_positions)"
            " VALUES(?,?,?,?,?)",
            ("BEAR_TREND", (now - timedelta(hours=2)).isoformat(timespec="seconds"), -0.02, -0.01, 5))
        conn.commit()
        conn.close()
        self.assertEqual(brain.empirical_cap_factor("BEAR_TREND"), 0.65)

    def test_dynamic_heat_cap_drags_down_and_respects_floor(self):
        self.assertEqual(brain.dynamic_heat_cap("BEAR_TREND", -0.01), 0.60)
        conn = self._conn()
        now = datetime.now()
        conn.execute(
            "INSERT INTO regime_drawdown(regime,ts,portfolio_dd,avg_unrealised_pnl,n_positions)"
            " VALUES(?,?,?,?,?)",
            ("BEAR_TREND", now.isoformat(timespec="seconds"), -0.10, -0.06, 6))
        conn.commit()
        conn.close()
        self.assertEqual(brain.dynamic_heat_cap("BEAR_TREND", -0.01), 0.35)

    # ---------- live learn cycle ----------

    def test_learn_live_period_logs_losses_and_regime_snapshot(self):
        n = brain.learn_live_period("BEAR_TREND", -0.035, {"SPY": -0.05, "AAPL": -0.02})
        self.assertEqual(n, 1)
        conn = self._conn()
        row = conn.execute("SELECT regime,portfolio_dd,avg_unrealised_pnl,n_positions FROM regime_drawdown").fetchone()
        live = conn.execute("SELECT COUNT(*) FROM loss_memory WHERE source='LIVE'").fetchone()[0]
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "BEAR_TREND")
        self.assertAlmostEqual(row[1], -0.035)
        self.assertAlmostEqual(row[2], -0.035)
        self.assertEqual(row[3], 2)
        self.assertEqual(live, 1)


if __name__ == "__main__":
    unittest.main()
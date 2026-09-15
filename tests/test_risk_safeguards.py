import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

import brain


class RiskSafeguardsTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="brain_rs_")
        brain.DB_FILE = os.path.join(self._tmp, "test.db")
        brain._DB_CONN = None
        brain.init_db()

    def tearDown(self):
        brain._DB_CONN = None
        try:
            os.remove(brain.DB_FILE)
        except Exception:
            pass

    # ---------- hard_regime_stop_multiplier ----------

    def test_tighter_in_hard_regimes(self):
        for r in ("BEAR_TREND", "HIGH_VOL", "BEAR_RANGE"):
            self.assertAlmostEqual(brain.hard_regime_stop_multiplier(r), 0.7)

    def test_normal_in_easy_regimes(self):
        for r in ("BULL_TREND", "QUIET_RANGE", "UNKNOWN"):
            self.assertAlmostEqual(brain.hard_regime_stop_multiplier(r), 1.0)

    # ---------- supervised_loss_exit ----------

    def test_exit_triggered_in_hard_regime_with_loss(self):
        self.assertTrue(brain.supervised_loss_exit("BEAR_TREND", -0.06))
        self.assertTrue(brain.supervised_loss_exit("HIGH_VOL", -0.06))

    def test_no_exit_in_easy_regime(self):
        self.assertFalse(brain.supervised_loss_exit("QUIET_RANGE", -0.06))
        self.assertFalse(brain.supervised_loss_exit("BULL_TREND", -0.06))

    def test_no_exit_small_loss(self):
        self.assertFalse(brain.supervised_loss_exit("BEAR_TREND", -0.04))

    # ---------- position_trim_sell ----------

    def test_trim_overweight_position(self):
        # BAC 71% equity → trim to 20%
        trim = brain.position_trim_sell(67000, 95000)
        self.assertAlmostEqual(trim, 67000 - 0.20 * 95000)

    def test_no_trim_under_cap(self):
        self.assertEqual(brain.position_trim_sell(15000, 95000), 0.0)

    def test_no_trim_when_too_small(self):
        # 20500 vs 20000 cap → 500 trim, exactly at min_trim → 0
        self.assertEqual(brain.position_trim_sell(20500, 100000), 0.0)

    # ---------- clamp_stop_from_entry ----------

    def test_clamp_stop_within_band(self):
        # sl=95 on entry 100 → 5% loss, within [2%, 8%] band → stays 95
        self.assertAlmostEqual(brain.clamp_stop_from_entry(100.0, 95.0), 95.0)

    def test_clamp_stop_too_tight(self):
        # sl=99 on entry 100 → 1% loss, too tight → clamp to 98 (2%)
        self.assertAlmostEqual(brain.clamp_stop_from_entry(100.0, 99.0), 98.0)

    def test_clamp_stop_too_wide(self):
        # sl=80 on entry 100 → 20% loss, too wide → clamp to 92 (8%)
        self.assertAlmostEqual(brain.clamp_stop_from_entry(100.0, 80.0), 92.0)


if __name__ == "__main__":
    unittest.main()
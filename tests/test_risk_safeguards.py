import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

import pandas as pd

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

    # ---------- existing_stop_state ----------

    def test_stop_state_none_when_missing(self):
        self.assertEqual(brain.existing_stop_state(100.0, None), "none")
        self.assertEqual(brain.existing_stop_state(100.0, 0), "none")

    def test_stop_state_placeholder(self):
        # entry*0.92 → the 8% fallback the bot writes when no signal exists
        self.assertEqual(brain.existing_stop_state(100.0, 92.0), "placeholder")

    def test_stop_state_real(self):
        # a genuine framework SL (e.g. 5% below entry) is "real"
        self.assertEqual(brain.existing_stop_state(100.0, 95.0), "real")

    # ---------- framework_persist_levels ----------

    def _rl(self, sig=1, sl=95.0, tp=115.0, entry=100.0):
        return pd.DataFrame({"signal": [sig], "sl": [sl], "tp": [tp], "entry": [entry]})

    def test_persist_levels_from_signal(self):
        out = brain.framework_persist_levels({"mode": "flag_fw"}, self._rl(), "TSLA")
        self.assertIsNotNone(out)
        self.assertEqual(out["fw_entry"], 100.0)
        self.assertEqual(out["fw_sl"], 95.0)
        self.assertEqual(out["fw_tp"], 115.0)
        self.assertEqual(out["symbol"], "TSLA")

    def test_no_persist_without_signal(self):
        self.assertIsNone(brain.framework_persist_levels({"mode": "flag_fw"},
                                                         self._rl(sig=0), "TSLA"))

    def test_no_persist_invalid_sl(self):
        self.assertIsNone(brain.framework_persist_levels({"mode": "flag_fw"},
                                                         self._rl(sl=0), "TSLA"))

    def test_fill_entry_overrides_and_clamps(self):
        # hit a signal at entry 100 (SL 5% below) but actually filled at 90 (gap down):
        # levels must be recomputed around the real fill, keeping the relative SL distance.
        out = brain.framework_persist_levels({"mode": "flag_fw"}, self._rl(sl=95.0),
                                             "TSLA", fill_entry=90.0)
        self.assertAlmostEqual(out["fw_entry"], 90.0)
        self.assertAlmostEqual(out["fw_sl"], 85.5)  # 5% below the real fill

    def test_tp_dropped_when_not_profitable(self):
        out = brain.framework_persist_levels({"mode": "flag_fw"}, self._rl(tp=105.0),
                                             "TSLA", fill_entry=110.0)
        self.assertNotIn("fw_tp", out)

    def test_existing_keys_overwritten(self):
        cfg = {"mode": "flag_fw", "fw_sl": 92.0, "fw_tp": 111.0, "fw_entry": 101.0}
        out = brain.framework_persist_levels(cfg, self._rl(), "TSLA")
        self.assertEqual(out["fw_sl"], 95.0)
        self.assertEqual(out["fw_tp"], 115.0)


if __name__ == "__main__":
    unittest.main()
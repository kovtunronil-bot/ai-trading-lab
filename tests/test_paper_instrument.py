import os
import sqlite3
import tempfile
import unittest

import brain


class PaperInstrumentMigrationTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="brain_pi_")
        brain.DB_FILE = os.path.join(self._tmp, "test.db")
        brain._DB_CONN = None

    def tearDown(self):
        brain._DB_CONN = None
        try:
            os.remove(brain.DB_FILE)
        except Exception:
            pass

    def test_fresh_db_has_tags_columns(self):
        conn = brain.init_db()
        c = conn.cursor()
        trades_cols = {r[1] for r in c.execute("PRAGMA table_info(trades)").fetchall()}
        pnl_cols = {r[1] for r in c.execute("PRAGMA table_info(trade_pnl)").fetchall()}
        conn.close()
        self.assertIn("tags", trades_cols)
        self.assertIn("tags", pnl_cols)

    def test_legacy_db_gets_tags_columns(self):
        legacy = os.path.join(self._tmp, "legacy.db")
        conn = sqlite3.connect(legacy)
        conn.execute("""CREATE TABLE trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT,
            side TEXT, notional REAL, qty REAL, limit_price REAL,
            fill_price REAL, status TEXT, algo TEXT, slippage_bps REAL)""")
        conn.execute("""CREATE TABLE trade_pnl(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, symbol TEXT, strategy TEXT, pnl_pct REAL)""")
        conn.commit()
        conn.close()
        brain.DB_FILE = legacy
        brain._DB_CONN = None
        conn = brain.init_db()
        c = conn.cursor()
        trades_cols = {r[1] for r in c.execute("PRAGMA table_info(trades)").fetchall()}
        pnl_cols = {r[1] for r in c.execute("PRAGMA table_info(trade_pnl)").fetchall()}
        conn.close()
        self.assertIn("tags", trades_cols)
        self.assertIn("tags", pnl_cols)

    def test_migration_is_idempotent(self):
        brain.init_db()
        brain._DB_CONN = None
        brain.init_db()   # second call must not raise
        self.assertTrue(os.path.exists(brain.DB_FILE))


class LogTradeTagsTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="brain_pi2_")
        brain.DB_FILE = os.path.join(self._tmp, "test.db")
        brain._DB_CONN = None
        brain.init_db()

    def tearDown(self):
        brain._DB_CONN = None
        try:
            os.remove(brain.DB_FILE)
        except Exception:
            pass

    def test_tags_stored_on_trade_row(self):
        brain.log_trade("2026-09-22T10:00:00", "AAPL", "BUY", 3000, None, None,
                        "filled", "smart-limit", fill_price=150.0,
                        signal_price=149.0, strategy="Flag FW", tags="heat,low_conv")
        conn = brain.init_db()
        row = conn.execute("SELECT tags, strategy, fill_price FROM trades").fetchone()
        conn.close()
        self.assertEqual(row[0], "heat,low_conv")
        self.assertEqual(row[1], "Flag FW")
        self.assertAlmostEqual(row[2], 150.0, places=2)

    def test_default_tags_empty(self):
        brain.log_trade("2026-09-22T10:00:00", "META", "BUY", 1000, None, None,
                        "filled", "smart-limit", fill_price=200.0,
                        signal_price=199.0, strategy="Any")
        conn = brain.init_db()
        row = conn.execute("SELECT tags FROM trades").fetchone()
        conn.close()
        self.assertEqual(row[0], "")


class ClosedTradeTagsTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="brain_pi3_")
        brain.DB_FILE = os.path.join(self._tmp, "test.db")
        brain._DB_CONN = None
        brain.init_db()

    def tearDown(self):
        brain._DB_CONN = None
        try:
            os.remove(brain.DB_FILE)
        except Exception:
            pass

    def test_tags_propagated_from_latest_buy(self):
        brain.log_trade("2026-09-22T10:00:00", "AAPL", "BUY", 3000, None, None,
                        "filled", "smart-limit", fill_price=150.0,
                        signal_price=149.0, strategy="Flag FW", tags="heat")
        brain.log_closed_trade("AAPL", "Flag FW", 150.0, 160.0)
        conn = brain.init_db()
        row = conn.execute("SELECT tags, pnl_pct FROM trade_pnl").fetchone()
        conn.close()
        self.assertEqual(row[0], "heat")
        self.assertAlmostEqual(row[1], (160.0 / 150.0 - 1) * 100, places=2)

    def test_no_buy_row_means_clean(self):
        brain.log_closed_trade("META", "Any", 200.0, 210.0)
        conn = brain.init_db()
        row = conn.execute("SELECT tags FROM trade_pnl").fetchone()
        conn.close()
        self.assertEqual(row[0], "")

    def test_explicit_tags_win(self):
        brain.log_trade("2026-09-22T10:00:00", "MSFT", "BUY", 3000, None, None,
                        "filled", "smart-limit", fill_price=300.0,
                        signal_price=300.0, strategy="X", tags="heat")
        brain.log_closed_trade("MSFT", "X", 300.0, 310.0, tags="low_conv")
        conn = brain.init_db()
        row = conn.execute("SELECT tags FROM trade_pnl").fetchone()
        conn.close()
        self.assertEqual(row[0], "low_conv")


class GateDecisionTest(unittest.TestCase):

    def test_allow_passes_through(self):
        enter, tags = brain.gate_decision("heat", False, False)
        self.assertTrue(enter)
        self.assertEqual(tags, [])

    def test_block_stays_blocked_when_not_relaxed(self):
        enter, tags = brain.gate_decision("heat", True, False)
        self.assertFalse(enter)
        self.assertEqual(tags, [])

    def test_block_relaxes_to_tag(self):
        enter, tags = brain.gate_decision("heat", True, True)
        self.assertTrue(enter)
        self.assertEqual(tags, ["heat"])

    def test_accumulates_tags(self):
        _, tags = brain.gate_decision("heat", True, True, ["momentum"])
        self.assertEqual(tags, ["momentum", "heat"])


class CohortStatsTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="brain_pi5_")
        brain.DB_FILE = os.path.join(self._tmp, "test.db")
        brain._DB_CONN = None
        brain.init_db()

    def tearDown(self):
        brain._DB_CONN = None
        try:
            os.remove(brain.DB_FILE)
        except Exception:
            pass

    def test_is_framework_strategy(self):
        self.assertTrue(brain.is_framework_strategy("Momentum Flag Pullback"))
        self.assertTrue(brain.is_framework_strategy("Bank of America (BAC Flag FW)"))
        self.assertTrue(brain.is_framework_strategy("The ORB Reversal (ORB FW)"))
        self.assertFalse(brain.is_framework_strategy("EMA20/50"))
        self.assertFalse(brain.is_framework_strategy(""))

    def test_ledger_buckets(self):
        rows = [
            ("Momentum Flag Pullback", None, 8.96),
            ("EMA20/50", None, -0.38),               # non-framework -> ignored
            ("Flag Pattern Fade (Flag FW)", "heat", -8.24),
            ("Momentum Flag Pullback", "momentum", 0.19),
            ("Momentum Flag Pullback", "", 0.36),
            ("Bank of America (BAC Flag FW)", "heat,low_conv", -8.28),
        ]
        ledger = brain.expectancy_ledger(rows)
        self.assertEqual(ledger["clean"]["n"], 2)          # None + ""
        self.assertAlmostEqual(ledger["clean"]["sum"], 8.96 + 0.36, places=2)
        self.assertEqual(ledger["full"]["n"], 5)           # framework rows only
        self.assertEqual(sorted(ledger["by_tag"].keys()), ["heat", "low_conv", "momentum"])
        self.assertEqual(ledger["by_tag"]["momentum"]["n"], 1)
        self.assertAlmostEqual(ledger["by_tag"]["heat"]["sum"], -8.24 + -8.28, places=2)

    def test_framework_stats_clean_vs_full(self):
        brain.log_trade("2026-09-22T10:00:00", "META", "BUY", 1000, None, None,
                        "filled", "smart-limit", fill_price=100.0,
                        signal_price=100.0, strategy="Momentum Flag Pullback")
        brain.log_closed_trade("META", "Momentum Flag Pullback", 100.0, 108.96)
        brain.log_trade("2026-09-22T11:00:00", "ETH/USD", "BUY", 1000, None, None,
                        "filled", "smart-limit", fill_price=3000.0,
                        signal_price=3000.0, strategy="Flag Pattern Fade (Flag FW)",
                        tags="heat")
        brain.log_closed_trade("ETH/USD", "Flag Pattern Fade (Flag FW)", 3000.0, 2752.8)
        clean = brain.framework_stats("clean")
        full = brain.framework_stats("full")
        self.assertEqual(clean["n"], 1)
        self.assertEqual(full["n"], 2)

    def test_pruning_recommendations(self):
        ledger = {
            "clean": {"n": 5, "avg": 1.0, "sum": 5.0},
            "full": {"n": 8, "avg": 0.25, "sum": 2.0},
            "by_tag": {
                "heat": {"n": 3, "avg": -3.0, "sum": -9.0},
                "momentum": {"n": 3, "avg": 1.2, "sum": 3.6},
                "corr": {"n": 1, "avg": 0.5, "sum": 0.5},
            },
        }
        recs = brain.pruning_recommendations(ledger)
        text = "\n".join(recs)
        self.assertIn("heat", text)
        self.assertIn("protection", text.lower())
        self.assertIn("review", text.lower())
        self.assertIn("corr", text)          # insufficient evidence line

    def test_pruning_empty_ledger(self):
        self.assertEqual(brain.pruning_recommendations({"clean": {"n": 0, "avg": 0.0, "sum": 0.0},
                                                        "full": {"n": 0, "avg": 0.0, "sum": 0.0},
                                                        "by_tag": {}}), [])


class CloudBotOrderConfirmTest(unittest.TestCase):
    """Regression: cloud_bot must confirm orders via alpaca-py's real SDK
    method get_order_by_id (get_order does not exist on TradingClient)."""

    import types

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("APCA_API_KEY_ID", "test-key-id")
        os.environ.setdefault("APCA_API_SECRET_KEY", "test-secret-key")
        import cloud_bot
        from alpaca.trading.enums import OrderStatus
        cls.cb = cloud_bot
        cls.OrderStatus = OrderStatus

    def setUp(self):
        calls = []
        OS = CloudBotOrderConfirmTest.OrderStatus

        class FakeClient:
            def __init__(self, avg_price=100.0):
                self.status = OS.FILLED
                self.avg_price = avg_price

            def submit_order(self, req, *a, **k):
                calls.append("submit_order:" + type(req).__name__)
                return CloudBotOrderConfirmTest.types.SimpleNamespace(
                    id="o1", status=OS.ACCEPTED)

            def get_order_by_id(self, oid):
                calls.append("get_order_by_id")
                return CloudBotOrderConfirmTest.types.SimpleNamespace(
                    id=oid, status=self.status, filled_avg_price=self.avg_price)

        self.calls = calls
        self._orig_client = CloudBotOrderConfirmTest.cb.client
        self._orig_sleep = CloudBotOrderConfirmTest.cb.time.sleep
        self.fake = FakeClient()
        CloudBotOrderConfirmTest.cb.client = self.fake
        CloudBotOrderConfirmTest.cb.time.sleep = lambda *a, **k: None

    def tearDown(self):
        CloudBotOrderConfirmTest.cb.client = self._orig_client
        CloudBotOrderConfirmTest.cb.time.sleep = self._orig_sleep

    def test_smart_buy_confirms_via_get_order_by_id(self):
        oid, st, fill = CloudBotOrderConfirmTest.cb.smart_buy("SPY", 1000.0, 500.0)
        self.assertEqual(st, "filled")
        self.assertEqual(fill, 100.0)
        self.assertIn("get_order_by_id", self.calls)

    def test_smart_sell_confirms_via_get_order_by_id(self):
        oid, st, fill = CloudBotOrderConfirmTest.cb.smart_sell("SPY", 2.0, 500.0)
        self.assertEqual(st, "filled")
        self.assertEqual(fill, 100.0)
        self.assertIn("get_order_by_id", self.calls)

    def test_smart_buy_partial_fill_accepted_no_market_escalation(self):
        # A partially-filled limit order is a WIN: take the partial at once.
        # It must NOT fall through to a full-notional market escalation
        # (that would double-buy on top of the filled portion).
        self.fake.status = CloudBotOrderConfirmTest.OrderStatus.PARTIALLY_FILLED
        oid, st, fill = CloudBotOrderConfirmTest.cb.smart_buy("SPY", 1000.0, 500.0)
        self.assertEqual(st, "filled")
        self.assertEqual(fill, 100.0)
        self.assertFalse([c for c in self.calls if "MarketOrderRequest" in c],
                         f"market escalation submitted: {self.calls}")

    def test_smart_buy_partial_fill_without_avg_price_no_crash_no_escalation(self):
        # A partially-filled order whose avg-price is still None (Alpaca
        # bookkeeping race) must NOT raise into the cancel/escalate path.
        self.fake.status = CloudBotOrderConfirmTest.OrderStatus.PARTIALLY_FILLED
        self.fake.avg_price = None
        oid, st, fill = CloudBotOrderConfirmTest.cb.smart_buy("SPY", 1000.0, 500.0)
        self.assertEqual(st, "filled")
        self.assertIsNone(fill)
        self.assertFalse([c for c in self.calls if "MarketOrderRequest" in c],
                         f"market escalation submitted: {self.calls}")


class OrderStatusNormalizeTest(unittest.TestCase):
    """str() of alpaca OrderStatus on py3.11+ is 'OrderStatus.FILLED', not
    'filled'. brain.status_str must return the enum's .value so both bots'
    string comparisons work across Python versions."""

    def test_value_takes_priority_over_str(self):
        from alpaca.trading.enums import OrderStatus
        self.assertEqual(brain.status_str(OrderStatus.FILLED), "filled")
        self.assertEqual(brain.status_str(OrderStatus.ACCEPTED), "accepted")
        self.assertEqual(brain.status_str(OrderStatus.PENDING_NEW), "pending_new")

    def test_plain_string_passthrough(self):
        self.assertEqual(brain.status_str("filled"), "filled")
        self.assertEqual(brain.status_str("accepted"), "accepted")

    def test_none(self):
        self.assertEqual(brain.status_str(None), "None")


if __name__ == "__main__":
    unittest.main()

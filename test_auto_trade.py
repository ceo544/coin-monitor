import unittest
from unittest.mock import patch

import main

main.init_db()  # ensure auto_trades table exists before any test in this module runs


def _parsed_with_entries(long_active=True, short_active=False):
    return {
        "signals": {
            "long": {"active": long_active},
            "short": {"active": short_active},
        },
        "table_rows": [
            {"text": "Long 진입가", "numbers_raw": ["100000", "99500", "99000", "98500", "98000"]},
            {"text": "TP", "numbers_raw": ["101000", "101500", "102000", "102500", "103000"]},
            {"text": "SL", "numbers_raw": ["99500", "99000", "98500", "98000", "97500"]},
            {"text": "Short 진입가", "numbers_raw": ["104000", "104500", "105000", "105500", "106000"]},
            {"text": "TP", "numbers_raw": ["103000", "102500", "102000", "101500", "101000"]},
            {"text": "SL", "numbers_raw": ["104500", "105000", "105500", "106000", "106500"]},
        ],
    }


class AutoTradeTestBase(unittest.TestCase):
    def setUp(self):
        self.orig_enabled = main.AUTO_TRADE_ENABLED
        self.orig_dry_run = main.AUTO_TRADE_DRY_RUN
        self.orig_symbol = main.AUTO_TRADE_SYMBOL
        self.orig_max_trades = main.AUTO_TRADE_MAX_DAILY_TRADES
        self.orig_max_loss = main.AUTO_TRADE_MAX_DAILY_LOSS_USDT
        self.orig_telegram_token = main.TELEGRAM_BOT_TOKEN
        main.AUTO_TRADE_SYMBOL = "BTC-USDT"
        main.AUTO_TRADE_MAX_DAILY_TRADES = 10
        main.AUTO_TRADE_MAX_DAILY_LOSS_USDT = 100.0
        main.TELEGRAM_BOT_TOKEN = ""  # avoid real telegram sends in tests
        with main._state_lock:
            main.AUTO_TRADE_STATE["long_prev_active"] = None
            main.AUTO_TRADE_STATE["short_prev_active"] = None
        # Clear the auto_trades table between tests so count-based guards are predictable.
        with main.db_cursor() as (conn, cur):
            cur.execute("DELETE FROM auto_trades")

    def tearDown(self):
        main.AUTO_TRADE_ENABLED = self.orig_enabled
        main.AUTO_TRADE_DRY_RUN = self.orig_dry_run
        main.AUTO_TRADE_SYMBOL = self.orig_symbol
        main.AUTO_TRADE_MAX_DAILY_TRADES = self.orig_max_trades
        main.AUTO_TRADE_MAX_DAILY_LOSS_USDT = self.orig_max_loss
        main.TELEGRAM_BOT_TOKEN = self.orig_telegram_token


class EdgeTriggerTests(AutoTradeTestBase):
    def test_disabled_never_calls_execute(self):
        main.AUTO_TRADE_ENABLED = False
        with patch("main._execute_auto_trade") as mock_exec:
            main._maybe_auto_trade(_parsed_with_entries(long_active=True), True, False)
        mock_exec.assert_not_called()

    def test_first_call_with_unknown_prev_state_does_not_fire(self):
        main.AUTO_TRADE_ENABLED = True
        with patch("main._execute_auto_trade") as mock_exec:
            main._maybe_auto_trade(_parsed_with_entries(long_active=True), True, False)
        mock_exec.assert_not_called()  # prev was None (boot) -> no edge yet

    def test_flip_from_off_to_on_fires_once(self):
        main.AUTO_TRADE_ENABLED = True
        with patch("main._execute_auto_trade") as mock_exec:
            main._maybe_auto_trade(_parsed_with_entries(long_active=False), False, False)  # seed prev=False
            main._maybe_auto_trade(_parsed_with_entries(long_active=True), True, False)     # OFF->ON edge
            main._maybe_auto_trade(_parsed_with_entries(long_active=True), True, False)     # stays ON, no re-fire
        self.assertEqual(mock_exec.call_count, 1)
        self.assertEqual(mock_exec.call_args[0][0], "long")


class DryRunTests(AutoTradeTestBase):
    def test_dry_run_logs_without_calling_create_order(self):
        main.AUTO_TRADE_ENABLED = True
        main.AUTO_TRADE_DRY_RUN = True
        with patch("main._has_open_position", return_value=False), \
             patch("main.bingx_client.create_order") as mock_create:
            main._execute_auto_trade("long", main._row_map_from_parsed(_parsed_with_entries()))
        mock_create.assert_not_called()
        with main.db_cursor() as (conn, cur):
            cur.execute("SELECT action FROM auto_trades ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
        self.assertEqual(row[0], "dry_run")


class SafetyGuardTests(AutoTradeTestBase):
    def test_duplicate_position_blocks_trade(self):
        main.AUTO_TRADE_ENABLED = True
        main.AUTO_TRADE_DRY_RUN = False
        with patch("main._has_open_position", return_value=True), \
             patch("main.bingx_client.create_order") as mock_create:
            main._execute_auto_trade("long", main._row_map_from_parsed(_parsed_with_entries()))
        mock_create.assert_not_called()
        with main.db_cursor() as (conn, cur):
            cur.execute("SELECT action FROM auto_trades ORDER BY id DESC LIMIT 1")
            self.assertEqual(cur.fetchone()[0], "skipped_duplicate")

    def test_failed_position_check_blocks_trade_fail_closed(self):
        main.AUTO_TRADE_ENABLED = True
        main.AUTO_TRADE_DRY_RUN = False
        with patch("main._has_open_position", return_value=None), \
             patch("main.bingx_client.create_order") as mock_create:
            main._execute_auto_trade("long", main._row_map_from_parsed(_parsed_with_entries()))
        mock_create.assert_not_called()
        with main.db_cursor() as (conn, cur):
            cur.execute("SELECT action FROM auto_trades ORDER BY id DESC LIMIT 1")
            self.assertEqual(cur.fetchone()[0], "skipped_duplicate")

    def test_daily_trade_limit_blocks_trade(self):
        main.AUTO_TRADE_ENABLED = True
        main.AUTO_TRADE_DRY_RUN = False
        main.AUTO_TRADE_MAX_DAILY_TRADES = 2
        for _ in range(2):
            main._auto_trade_log("long", "order_placed", symbol="BTC-USDT", entry_price=100000, quantity=0.01, dry_run=False)
        with patch("main._has_open_position", return_value=False), \
             patch("main.bingx_client.create_order") as mock_create:
            main._execute_auto_trade("long", main._row_map_from_parsed(_parsed_with_entries()))
        mock_create.assert_not_called()
        with main.db_cursor() as (conn, cur):
            cur.execute("SELECT action FROM auto_trades ORDER BY id DESC LIMIT 1")
            self.assertEqual(cur.fetchone()[0], "skipped_daily_trade_limit")

    def test_daily_loss_limit_blocks_trade(self):
        main.AUTO_TRADE_ENABLED = True
        main.AUTO_TRADE_DRY_RUN = False
        main.AUTO_TRADE_MAX_DAILY_LOSS_USDT = 50.0
        with patch("main._has_open_position", return_value=False), \
             patch("main._today_realized_pnl_usdt", return_value=-75.0), \
             patch("main.bingx_client.create_order") as mock_create:
            main._execute_auto_trade("long", main._row_map_from_parsed(_parsed_with_entries()))
        mock_create.assert_not_called()
        with main.db_cursor() as (conn, cur):
            cur.execute("SELECT action FROM auto_trades ORDER BY id DESC LIMIT 1")
            self.assertEqual(cur.fetchone()[0], "skipped_daily_loss_limit")

    def test_missing_entry_data_skips_gracefully(self):
        main.AUTO_TRADE_ENABLED = True
        main.AUTO_TRADE_DRY_RUN = True
        with patch("main._has_open_position", return_value=False):
            main._execute_auto_trade("long", {})
        with main.db_cursor() as (conn, cur):
            cur.execute("SELECT action FROM auto_trades ORDER BY id DESC LIMIT 1")
            self.assertEqual(cur.fetchone()[0], "skipped_no_entry_data")


class OrderExecutionTests(AutoTradeTestBase):
    def test_successful_order_is_logged_with_order_id(self):
        main.AUTO_TRADE_ENABLED = True
        main.AUTO_TRADE_DRY_RUN = False
        with patch("main._has_open_position", return_value=False), \
             patch("main.bingx_client.set_leverage"), \
             patch("main.bingx_client.create_order", return_value={"orderId": "12345"}):
            main._execute_auto_trade("long", main._row_map_from_parsed(_parsed_with_entries()))
        with main.db_cursor() as (conn, cur):
            cur.execute("SELECT action, order_id, quantity FROM auto_trades ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
        self.assertEqual(row[0], "order_placed")
        self.assertEqual(row[1], "12345")
        # margin=50 * leverage=5 / entry=100000 -> notional 250, qty 0.0025
        self.assertAlmostEqual(float(row[2]), 250.0 / 100000.0, places=6)

    def test_failed_order_is_logged_with_error(self):
        main.AUTO_TRADE_ENABLED = True
        main.AUTO_TRADE_DRY_RUN = False
        with patch("main._has_open_position", return_value=False), \
             patch("main.bingx_client.set_leverage"), \
             patch("main.bingx_client.create_order", side_effect=RuntimeError("[100001] insufficient margin")):
            main._execute_auto_trade("long", main._row_map_from_parsed(_parsed_with_entries()))
        with main.db_cursor() as (conn, cur):
            cur.execute("SELECT action, error FROM auto_trades ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
        self.assertEqual(row[0], "order_failed")
        self.assertIn("insufficient margin", row[1])


class KillSwitchTests(AutoTradeTestBase):
    def test_kill_switch_disables_and_cancels_orders(self):
        main.AUTO_TRADE_ENABLED = True
        main.load_settings_cache()
        with main.app.test_request_context():
            with patch("main.bingx_client.bingx_configured", return_value=True), \
                 patch("main.bingx_client.cancel_all_open_orders", return_value={"cancelled": 2, "results": []}) as mock_cancel:
                main.session["authenticated"] = True
                resp = main.api_auto_trade_kill_switch()
        self.assertFalse(main.AUTO_TRADE_ENABLED)
        mock_cancel.assert_called_once()


if __name__ == "__main__":
    unittest.main()

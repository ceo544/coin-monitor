import unittest
from unittest.mock import patch

import main

main.init_db()  # ensure auto_trades table exists before any test in this module runs

TEST_USER_ID = 999002


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


def _test_settings(**overrides):
    base = {
        "DASHBOARD_URL": "",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "",
        "TELEGRAM_NOTIFY_OFF": "true",
        "BITGET_API_KEY": "", "BITGET_API_SECRET": "", "BITGET_API_PASSPHRASE": "", "BITGET_CATEGORY": "USDT-FUTURES",
        "AUTO_TRADE_ENABLED": "true",
        "AUTO_TRADE_DRY_RUN": "true",
        "AUTO_TRADE_SYMBOL": "BTC-USDT",
        "AUTO_TRADE_LEVERAGE": "5",
        "AUTO_TRADE_MARGIN_USDT": "50",
        "AUTO_TRADE_MAX_DAILY_TRADES": "10",
        "AUTO_TRADE_MAX_DAILY_LOSS_USDT": "100",
        "BINGX_API_KEY": "test-key",
        "BINGX_API_SECRET": "test-secret",
    }
    base.update(overrides)
    return base


class AutoTradeTestBase(unittest.TestCase):
    def setUp(self):
        main.auto_trade_states.pop(TEST_USER_ID, None)
        with main.db_cursor() as (conn, cur):
            cur.execute("DELETE FROM auto_trades WHERE user_id = ?", (TEST_USER_ID,))

    def tearDown(self):
        main.auto_trade_states.pop(TEST_USER_ID, None)


class ProximityTriggerTests(AutoTradeTestBase):
    """Real entries fire when price approaches the 1st-stage entry level
    (while the signal is ON) - not the instant the signal turns ON. Fixture
    long entry1 = 100000, short entry1 = 104000 (see _parsed_with_entries)."""

    def test_disabled_never_calls_execute(self):
        with patch("main.get_all_user_settings", return_value=_test_settings(AUTO_TRADE_ENABLED="false")), \
             patch("main._execute_auto_trade") as mock_exec:
            main._maybe_auto_trade_for_user(TEST_USER_ID, _parsed_with_entries(long_active=True), True, False, "100050")
        mock_exec.assert_not_called()

    def test_signal_on_but_price_far_from_entry_does_not_fire(self):
        with patch("main.get_all_user_settings", return_value=_test_settings()), \
             patch("main._execute_auto_trade") as mock_exec:
            main._maybe_auto_trade_for_user(TEST_USER_ID, _parsed_with_entries(long_active=True), True, False, "102000")  # 2000 away
        mock_exec.assert_not_called()

    def test_price_near_entry_but_signal_off_does_not_fire(self):
        with patch("main.get_all_user_settings", return_value=_test_settings()), \
             patch("main._execute_auto_trade") as mock_exec:
            main._maybe_auto_trade_for_user(TEST_USER_ID, _parsed_with_entries(long_active=False), False, False, "100050")
        mock_exec.assert_not_called()

    def test_signal_on_and_price_near_entry_fires(self):
        with patch("main.get_all_user_settings", return_value=_test_settings()), \
             patch("main._execute_auto_trade") as mock_exec:
            main._maybe_auto_trade_for_user(TEST_USER_ID, _parsed_with_entries(long_active=True), True, False, "100050")  # 50 away, within default $100
        mock_exec.assert_called_once()
        # _execute_auto_trade(user_id, side, rows, settings)
        self.assertEqual(mock_exec.call_args[0][1], "long")

    def test_staying_near_entry_does_not_refire(self):
        with patch("main.get_all_user_settings", return_value=_test_settings()), \
             patch("main._execute_auto_trade") as mock_exec:
            main._maybe_auto_trade_for_user(TEST_USER_ID, _parsed_with_entries(long_active=True), True, False, "100050")
            main._maybe_auto_trade_for_user(TEST_USER_ID, _parsed_with_entries(long_active=True), True, False, "100040")
        self.assertEqual(mock_exec.call_count, 1)

    def test_moving_away_then_back_refires(self):
        with patch("main.get_all_user_settings", return_value=_test_settings()), \
             patch("main._execute_auto_trade") as mock_exec:
            main._maybe_auto_trade_for_user(TEST_USER_ID, _parsed_with_entries(long_active=True), True, False, "100050")
            main._maybe_auto_trade_for_user(TEST_USER_ID, _parsed_with_entries(long_active=True), True, False, "101000")  # moves away
            main._maybe_auto_trade_for_user(TEST_USER_ID, _parsed_with_entries(long_active=True), True, False, "100010")  # back in range
        self.assertEqual(mock_exec.call_count, 2)

    def test_short_side_tracked_independently_from_long(self):
        with patch("main.get_all_user_settings", return_value=_test_settings()), \
             patch("main._execute_auto_trade") as mock_exec:
            main._maybe_auto_trade_for_user(TEST_USER_ID, _parsed_with_entries(long_active=True, short_active=True), True, True, "100050")  # near LONG entry (100000) only
        self.assertEqual(mock_exec.call_count, 1)
        self.assertEqual(mock_exec.call_args[0][1], "long")

    def test_signal_turning_off_resets_state_so_it_can_refire_later(self):
        with patch("main.get_all_user_settings", return_value=_test_settings()), \
             patch("main._execute_auto_trade") as mock_exec:
            main._maybe_auto_trade_for_user(TEST_USER_ID, _parsed_with_entries(long_active=True), True, False, "100050")   # fires
            main._maybe_auto_trade_for_user(TEST_USER_ID, _parsed_with_entries(long_active=False), False, False, "100050")  # signal OFF, still near - resets, no fire
            main._maybe_auto_trade_for_user(TEST_USER_ID, _parsed_with_entries(long_active=True), True, False, "100050")   # signal back ON, still near - fires fresh
        self.assertEqual(mock_exec.call_count, 2)


class DryRunTests(AutoTradeTestBase):
    def test_dry_run_logs_without_calling_create_order(self):
        with patch("main._has_open_position", return_value=False), \
             patch("main.bingx_client.create_order") as mock_create:
            main._execute_auto_trade(
                TEST_USER_ID, "long", main._row_map_from_parsed(_parsed_with_entries()),
                _test_settings(AUTO_TRADE_DRY_RUN="true"),
            )
        mock_create.assert_not_called()
        with main.db_cursor() as (conn, cur):
            cur.execute("SELECT action FROM auto_trades WHERE user_id = ? ORDER BY id DESC LIMIT 1", (TEST_USER_ID,))
            row = cur.fetchone()
        self.assertEqual(row[0], "dry_run")


class SafetyGuardTests(AutoTradeTestBase):
    def test_duplicate_position_blocks_trade(self):
        with patch("main._has_open_position", return_value=True), \
             patch("main.bingx_client.create_order") as mock_create:
            main._execute_auto_trade(
                TEST_USER_ID, "long", main._row_map_from_parsed(_parsed_with_entries()),
                _test_settings(AUTO_TRADE_DRY_RUN="false"),
            )
        mock_create.assert_not_called()
        with main.db_cursor() as (conn, cur):
            cur.execute("SELECT action FROM auto_trades WHERE user_id = ? ORDER BY id DESC LIMIT 1", (TEST_USER_ID,))
            self.assertEqual(cur.fetchone()[0], "skipped_duplicate")

    def test_failed_position_check_blocks_trade_fail_closed(self):
        with patch("main._has_open_position", return_value=None), \
             patch("main.bingx_client.create_order") as mock_create:
            main._execute_auto_trade(
                TEST_USER_ID, "long", main._row_map_from_parsed(_parsed_with_entries()),
                _test_settings(AUTO_TRADE_DRY_RUN="false"),
            )
        mock_create.assert_not_called()
        with main.db_cursor() as (conn, cur):
            cur.execute("SELECT action FROM auto_trades WHERE user_id = ? ORDER BY id DESC LIMIT 1", (TEST_USER_ID,))
            self.assertEqual(cur.fetchone()[0], "skipped_duplicate")

    def test_daily_trade_limit_blocks_trade(self):
        for _ in range(2):
            main._auto_trade_log(TEST_USER_ID, "long", "order_placed", symbol="BTC-USDT", entry_price=100000, quantity=0.01, dry_run=False)
        with patch("main._has_open_position", return_value=False), \
             patch("main.bingx_client.create_order") as mock_create:
            main._execute_auto_trade(
                TEST_USER_ID, "long", main._row_map_from_parsed(_parsed_with_entries()),
                _test_settings(AUTO_TRADE_DRY_RUN="false", AUTO_TRADE_MAX_DAILY_TRADES="2"),
            )
        mock_create.assert_not_called()
        with main.db_cursor() as (conn, cur):
            cur.execute("SELECT action FROM auto_trades WHERE user_id = ? ORDER BY id DESC LIMIT 1", (TEST_USER_ID,))
            self.assertEqual(cur.fetchone()[0], "skipped_daily_trade_limit")

    def test_daily_loss_limit_blocks_trade(self):
        with patch("main._has_open_position", return_value=False), \
             patch("main._today_realized_pnl_usdt", return_value=-75.0), \
             patch("main.bingx_client.create_order") as mock_create:
            main._execute_auto_trade(
                TEST_USER_ID, "long", main._row_map_from_parsed(_parsed_with_entries()),
                _test_settings(AUTO_TRADE_DRY_RUN="false", AUTO_TRADE_MAX_DAILY_LOSS_USDT="50"),
            )
        mock_create.assert_not_called()
        with main.db_cursor() as (conn, cur):
            cur.execute("SELECT action FROM auto_trades WHERE user_id = ? ORDER BY id DESC LIMIT 1", (TEST_USER_ID,))
            self.assertEqual(cur.fetchone()[0], "skipped_daily_loss_limit")

    def test_missing_entry_data_skips_gracefully(self):
        with patch("main._has_open_position", return_value=False):
            main._execute_auto_trade(TEST_USER_ID, "long", {}, _test_settings(AUTO_TRADE_DRY_RUN="true"))
        with main.db_cursor() as (conn, cur):
            cur.execute("SELECT action FROM auto_trades WHERE user_id = ? ORDER BY id DESC LIMIT 1", (TEST_USER_ID,))
            self.assertEqual(cur.fetchone()[0], "skipped_no_entry_data")


class OrderExecutionTests(AutoTradeTestBase):
    def test_successful_order_is_logged_with_order_id(self):
        with patch("main._has_open_position", return_value=False), \
             patch("main.bingx_client.set_leverage"), \
             patch("main.bingx_client.create_order", return_value={"orderId": "12345"}):
            main._execute_auto_trade(
                TEST_USER_ID, "long", main._row_map_from_parsed(_parsed_with_entries()),
                _test_settings(AUTO_TRADE_DRY_RUN="false"),
            )
        with main.db_cursor() as (conn, cur):
            cur.execute("SELECT action, order_id, quantity FROM auto_trades WHERE user_id = ? ORDER BY id DESC LIMIT 1", (TEST_USER_ID,))
            row = cur.fetchone()
        self.assertEqual(row[0], "order_placed")
        self.assertEqual(row[1], "12345")
        # margin=50 * leverage=5 / entry=100000 -> notional 250, qty 0.0025
        self.assertAlmostEqual(float(row[2]), 250.0 / 100000.0, places=6)

    def test_failed_order_is_logged_with_error(self):
        with patch("main._has_open_position", return_value=False), \
             patch("main.bingx_client.set_leverage"), \
             patch("main.bingx_client.create_order", side_effect=RuntimeError("[100001] insufficient margin")):
            main._execute_auto_trade(
                TEST_USER_ID, "long", main._row_map_from_parsed(_parsed_with_entries()),
                _test_settings(AUTO_TRADE_DRY_RUN="false"),
            )
        with main.db_cursor() as (conn, cur):
            cur.execute("SELECT action, error FROM auto_trades WHERE user_id = ? ORDER BY id DESC LIMIT 1", (TEST_USER_ID,))
            row = cur.fetchone()
        self.assertEqual(row[0], "order_failed")
        self.assertIn("insufficient margin", row[1])


class KillSwitchTests(AutoTradeTestBase):
    def test_kill_switch_disables_and_cancels_orders(self):
        username = f"killswitch_test_{TEST_USER_ID}"
        existing = main.get_user_by_username(username)
        user_id = existing["id"] if existing else main.create_user(username, "testpass123")
        main.save_user_setting(user_id, "AUTO_TRADE_ENABLED", "true")
        with main.app.test_request_context():
            with patch("main.bingx_client.bingx_configured", return_value=True), \
                 patch("main.bingx_client.cancel_all_open_orders", return_value={"cancelled": 2, "results": []}) as mock_cancel:
                main.session["user_id"] = user_id
                main.session["username"] = username
                main.api_auto_trade_kill_switch()
        self.assertEqual(main.get_user_setting(user_id, "AUTO_TRADE_ENABLED"), "false")
        mock_cancel.assert_called_once()


if __name__ == "__main__":
    unittest.main()

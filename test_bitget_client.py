import base64
import hashlib
import hmac
import unittest

import bitget_client


class BuildQueryTests(unittest.TestCase):
    def test_empty_params(self):
        self.assertEqual(bitget_client._build_query({}), "")

    def test_skips_none_and_empty(self):
        qs = bitget_client._build_query({"a": 1, "b": None, "c": "", "d": "x"})
        self.assertEqual(qs, "?a=1&d=x")

    def test_multiple_params_joined_with_ampersand(self):
        qs = bitget_client._build_query({"productType": "USDT-FUTURES", "marginCoin": "USDT"})
        self.assertEqual(qs, "?productType=USDT-FUTURES&marginCoin=USDT")


class SignTests(unittest.TestCase):
    def test_matches_reference_hmac_sha256_base64(self):
        secret, message = "my-secret", "1700000000000GET/api/v2/mix/position/all-position"
        expected = base64.b64encode(
            hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")
        self.assertEqual(bitget_client._sign(secret, message), expected)

    def test_different_messages_produce_different_signatures(self):
        sig1 = bitget_client._sign("secret", "message-a")
        sig2 = bitget_client._sign("secret", "message-b")
        self.assertNotEqual(sig1, sig2)


class ConfiguredTests(unittest.TestCase):
    def test_not_configured_when_any_credential_missing(self):
        bitget_client.BITGET_API_KEY, orig_key = "", bitget_client.BITGET_API_KEY
        bitget_client.BITGET_API_SECRET, orig_secret = "x", bitget_client.BITGET_API_SECRET
        bitget_client.BITGET_API_PASSPHRASE, orig_pass = "x", bitget_client.BITGET_API_PASSPHRASE
        try:
            self.assertFalse(bitget_client.bitget_configured())
        finally:
            bitget_client.BITGET_API_KEY = orig_key
            bitget_client.BITGET_API_SECRET = orig_secret
            bitget_client.BITGET_API_PASSPHRASE = orig_pass

    def test_configured_when_all_three_present(self):
        orig = (bitget_client.BITGET_API_KEY, bitget_client.BITGET_API_SECRET, bitget_client.BITGET_API_PASSPHRASE)
        bitget_client.BITGET_API_KEY = "k"
        bitget_client.BITGET_API_SECRET = "s"
        bitget_client.BITGET_API_PASSPHRASE = "p"
        try:
            self.assertTrue(bitget_client.bitget_configured())
        finally:
            bitget_client.BITGET_API_KEY, bitget_client.BITGET_API_SECRET, bitget_client.BITGET_API_PASSPHRASE = orig


class PnlAndSummaryTests(unittest.TestCase):
    def test_pnl_of_picks_first_known_key(self):
        self.assertEqual(bitget_client._pnl_of({"unrealizedPL": "12.5"}), 12.5)
        self.assertEqual(bitget_client._pnl_of({"pnl": "-3.2"}), -3.2)
        self.assertEqual(bitget_client._pnl_of({"symbol": "BTCUSDT"}), 0.0)

    def test_pnl_of_handles_unparsable_value(self):
        self.assertEqual(bitget_client._pnl_of({"unrealizedPL": "not-a-number"}), 0.0)

    def test_fetch_summary_raises_when_not_configured(self):
        orig = (bitget_client.BITGET_API_KEY, bitget_client.BITGET_API_SECRET, bitget_client.BITGET_API_PASSPHRASE)
        bitget_client.BITGET_API_KEY = ""
        bitget_client.BITGET_API_SECRET = ""
        bitget_client.BITGET_API_PASSPHRASE = ""
        try:
            summary = bitget_client.fetch_summary()
            # Each sub-fetch fails independently and is captured in "errors",
            # rather than raising and blanking out the whole card.
            self.assertEqual(summary["positions"], [])
            self.assertEqual(summary["orders"], [])
            self.assertEqual(summary["fills"], [])
            self.assertIsNotNone(summary["errors"])
        finally:
            bitget_client.BITGET_API_KEY, bitget_client.BITGET_API_SECRET, bitget_client.BITGET_API_PASSPHRASE = orig


class WinRateAndBalanceTests(unittest.TestCase):
    def test_summary_computes_win_rate_and_combined_pnl(self):
        orig_positions = bitget_client.fetch_positions
        orig_orders = bitget_client.fetch_pending_orders
        orig_fills = bitget_client.fetch_fills
        orig_accounts = bitget_client.fetch_account_list
        orig_history = bitget_client.fetch_position_history
        try:
            bitget_client.fetch_positions = lambda: [{"symbol": "BTCUSDT", "unrealizedPL": "60.0"}]
            bitget_client.fetch_pending_orders = lambda: []
            bitget_client.fetch_fills = lambda limit=50: []
            bitget_client.fetch_account_list = lambda: [{"usdtEquity": "1240.55"}]
            bitget_client.fetch_position_history = lambda limit=100: [
                {"netProfit": "50.0"}, {"netProfit": "-20.0"}, {"netProfit": "30.0"}, {"netProfit": "0.0"},
            ]
            summary = bitget_client.fetch_summary()
            self.assertEqual(summary["win_count"], 2)
            self.assertEqual(summary["loss_count"], 1)
            self.assertEqual(summary["trade_count"], 4)
            self.assertAlmostEqual(summary["win_rate_pct"], 66.7, places=1)
            self.assertAlmostEqual(summary["realized_pnl_total"], 60.0)
            self.assertAlmostEqual(summary["combined_pnl"], 120.0)
            self.assertAlmostEqual(summary["total_equity"], 1240.55)
        finally:
            bitget_client.fetch_positions = orig_positions
            bitget_client.fetch_pending_orders = orig_orders
            bitget_client.fetch_fills = orig_fills
            bitget_client.fetch_account_list = orig_accounts
            bitget_client.fetch_position_history = orig_history

    def test_win_rate_none_when_no_decided_trades(self):
        orig = (
            bitget_client.fetch_position_history,
            bitget_client.fetch_positions,
            bitget_client.fetch_pending_orders,
            bitget_client.fetch_fills,
            bitget_client.fetch_account_list,
        )
        try:
            bitget_client.fetch_position_history = lambda limit=100: [{"netProfit": "0.0"}]
            bitget_client.fetch_positions = lambda: []
            bitget_client.fetch_pending_orders = lambda: []
            bitget_client.fetch_fills = lambda limit=50: []
            bitget_client.fetch_account_list = lambda: []
            summary = bitget_client.fetch_summary()
            self.assertIsNone(summary["win_rate_pct"])
        finally:
            (
                bitget_client.fetch_position_history,
                bitget_client.fetch_positions,
                bitget_client.fetch_pending_orders,
                bitget_client.fetch_fills,
                bitget_client.fetch_account_list,
            ) = orig

    def test_equity_of_and_realized_pnl_of_key_fallbacks(self):
        self.assertEqual(bitget_client._equity_of({"accountEquity": "500"}), 500.0)
        self.assertEqual(bitget_client._equity_of({"symbol": "BTCUSDT"}), 0.0)
        self.assertEqual(bitget_client._realized_pnl_of({"pnl": "12.3"}), 12.3)
        self.assertEqual(bitget_client._realized_pnl_of({}), 0.0)


class AccountModulePathTests(unittest.TestCase):
    def test_fetch_positions_uses_configured_module_in_path(self):
        orig_module = bitget_client.BITGET_ACCOUNT_MODULE
        orig_get = bitget_client._get
        captured = {}
        try:
            bitget_client.BITGET_ACCOUNT_MODULE = "uta"
            bitget_client._get = lambda path, params=None: captured.update(path=path) or []
            bitget_client.fetch_positions()
            self.assertEqual(captured["path"], "/api/v2/uta/position/all-position")

            bitget_client.BITGET_ACCOUNT_MODULE = "mix"
            bitget_client.fetch_positions()
            self.assertEqual(captured["path"], "/api/v2/mix/position/all-position")
        finally:
            bitget_client.BITGET_ACCOUNT_MODULE = orig_module
            bitget_client._get = orig_get


if __name__ == "__main__":
    unittest.main()

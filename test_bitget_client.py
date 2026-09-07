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

    def test_category_and_symbol_joined(self):
        qs = bitget_client._build_query({"category": "USDT-FUTURES", "symbol": "BTCUSDT"})
        self.assertEqual(qs, "?category=USDT-FUTURES&symbol=BTCUSDT")


class SignTests(unittest.TestCase):
    def test_matches_reference_hmac_sha256_base64(self):
        secret, message = "my-secret", "1700000000000GET/api/v3/account/assets"
        expected = base64.b64encode(
            hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")
        self.assertEqual(bitget_client._sign(secret, message), expected)

    def test_different_messages_produce_different_signatures(self):
        self.assertNotEqual(bitget_client._sign("secret", "a"), bitget_client._sign("secret", "b"))


class ConfiguredTests(unittest.TestCase):
    def test_not_configured_when_any_credential_missing(self):
        orig = (bitget_client.BITGET_API_KEY, bitget_client.BITGET_API_SECRET, bitget_client.BITGET_API_PASSPHRASE)
        bitget_client.BITGET_API_KEY = ""
        bitget_client.BITGET_API_SECRET = "x"
        bitget_client.BITGET_API_PASSPHRASE = "x"
        try:
            self.assertFalse(bitget_client.bitget_configured())
        finally:
            bitget_client.BITGET_API_KEY, bitget_client.BITGET_API_SECRET, bitget_client.BITGET_API_PASSPHRASE = orig

    def test_configured_when_all_three_present(self):
        orig = (bitget_client.BITGET_API_KEY, bitget_client.BITGET_API_SECRET, bitget_client.BITGET_API_PASSPHRASE)
        bitget_client.BITGET_API_KEY = "k"
        bitget_client.BITGET_API_SECRET = "s"
        bitget_client.BITGET_API_PASSPHRASE = "p"
        try:
            self.assertTrue(bitget_client.bitget_configured())
        finally:
            bitget_client.BITGET_API_KEY, bitget_client.BITGET_API_SECRET, bitget_client.BITGET_API_PASSPHRASE = orig


class AccountSummaryTests(unittest.TestCase):
    def test_pulls_documented_fields_exactly(self):
        acct = bitget_client._account_summary([{
            "accountEquity": "1240.55", "usdtEquity": "1240.55",
            "unrealisedPnl": "12.3", "usdtUnrealisedPnl": "12.3", "effEquity": "1200.0",
        }])
        self.assertEqual(acct["account_equity"], 1240.55)
        self.assertEqual(acct["usdt_equity"], 1240.55)
        self.assertEqual(acct["unrealized_pnl"], 12.3)
        self.assertEqual(acct["usdt_unrealized_pnl"], 12.3)
        self.assertEqual(acct["eff_equity"], 1200.0)

    def test_empty_assets_returns_all_none(self):
        acct = bitget_client._account_summary([])
        self.assertIsNone(acct["account_equity"])
        self.assertIsNone(acct["usdt_equity"])

    def test_unparsable_value_becomes_none(self):
        acct = bitget_client._account_summary([{"accountEquity": "not-a-number"}])
        self.assertIsNone(acct["account_equity"])


class RealizedPnlOfFillTests(unittest.TestCase):
    def test_close_fill_with_pnl(self):
        self.assertEqual(bitget_client._realized_pnl_of_fill({"tradeSide": "close", "execPnl": "42.5"}), 42.5)

    def test_open_fill_returns_none(self):
        self.assertIsNone(bitget_client._realized_pnl_of_fill({"tradeSide": "open", "execPnl": "0"}))

    def test_unparsable_pnl_returns_none(self):
        self.assertIsNone(bitget_client._realized_pnl_of_fill({"tradeSide": "close", "execPnl": "bad"}))


class FetchSummaryTests(unittest.TestCase):
    def test_summary_computes_win_rate_and_combined_pnl(self):
        orig = (bitget_client.fetch_account_assets, bitget_client.fetch_fills, bitget_client.fetch_history_orders)
        try:
            bitget_client.fetch_account_assets = lambda: [{
                "accountEquity": "1240.55", "usdtEquity": "1240.55",
                "unrealisedPnl": "60.0", "usdtUnrealisedPnl": "60.0", "effEquity": "1200.0",
            }]
            bitget_client.fetch_fills = lambda symbol=None: [
                {"symbol": "BTCUSDT", "tradeSide": "open", "execPnl": "0"},
                {"symbol": "BTCUSDT", "tradeSide": "close", "execPnl": "50.0"},
                {"symbol": "ETHUSDT", "tradeSide": "close", "execPnl": "-20.0"},
                {"symbol": "SOLUSDT", "tradeSide": "close", "execPnl": "30.0"},
                {"symbol": "ADAUSDT", "tradeSide": "close", "execPnl": "0.0"},
            ]
            bitget_client.fetch_history_orders = lambda symbol=None: []
            summary = bitget_client.fetch_summary()
            self.assertEqual(summary["win_count"], 2)
            self.assertEqual(summary["loss_count"], 1)
            self.assertEqual(summary["trade_count"], 4)  # all 4 close fills, including the 1 break-even one
            self.assertAlmostEqual(summary["win_rate_pct"], 66.7, places=1)
            self.assertAlmostEqual(summary["realized_pnl_total"], 60.0)
            self.assertAlmostEqual(summary["total_unrealized_pnl"], 60.0)
            self.assertAlmostEqual(summary["combined_pnl"], 120.0)
            self.assertAlmostEqual(summary["total_equity"], 1240.55)
        finally:
            bitget_client.fetch_account_assets, bitget_client.fetch_fills, bitget_client.fetch_history_orders = orig

    def test_win_rate_none_when_no_decided_trades(self):
        orig = (bitget_client.fetch_account_assets, bitget_client.fetch_fills, bitget_client.fetch_history_orders)
        try:
            bitget_client.fetch_account_assets = lambda: []
            bitget_client.fetch_fills = lambda symbol=None: [{"tradeSide": "close", "execPnl": "0.0"}]
            bitget_client.fetch_history_orders = lambda symbol=None: []
            summary = bitget_client.fetch_summary()
            self.assertIsNone(summary["win_rate_pct"])
        finally:
            bitget_client.fetch_account_assets, bitget_client.fetch_fills, bitget_client.fetch_history_orders = orig

    def test_one_failing_call_does_not_blank_out_others(self):
        orig = (bitget_client.fetch_account_assets, bitget_client.fetch_fills, bitget_client.fetch_history_orders)
        try:
            bitget_client.fetch_account_assets = lambda: [{"usdtEquity": "500.0"}]

            def boom(symbol=None):
                raise RuntimeError("[40001] some transient error")
            bitget_client.fetch_fills = boom
            bitget_client.fetch_history_orders = lambda symbol=None: []
            summary = bitget_client.fetch_summary()
            self.assertAlmostEqual(summary["total_equity"], 500.0)
            self.assertEqual(summary["fills"], [])
            self.assertIn("fills", summary["errors"])
        finally:
            bitget_client.fetch_account_assets, bitget_client.fetch_fills, bitget_client.fetch_history_orders = orig


class EndpointPathTests(unittest.TestCase):
    def test_fetch_account_assets_calls_v3_path(self):
        orig_get = bitget_client._get
        captured = {}
        try:
            bitget_client._get = lambda path, params=None: captured.update(path=path, params=params) or []
            bitget_client.fetch_account_assets()
            self.assertEqual(captured["path"], "/api/v3/account/assets")
        finally:
            bitget_client._get = orig_get

    def test_fetch_fills_calls_v3_path_with_category(self):
        orig_get = bitget_client._get
        captured = {}
        try:
            bitget_client._get = lambda path, params=None: captured.update(path=path, params=params) or []
            bitget_client.fetch_fills()
            self.assertEqual(captured["path"], "/api/v3/trade/fills")
            self.assertEqual(captured["params"]["category"], "USDT-FUTURES")
        finally:
            bitget_client._get = orig_get

    def test_fetch_history_orders_calls_v3_path(self):
        orig_get = bitget_client._get
        captured = {}
        try:
            bitget_client._get = lambda path, params=None: captured.update(path=path, params=params) or []
            bitget_client.fetch_history_orders(symbol="BTCUSDT")
            self.assertEqual(captured["path"], "/api/v3/trade/history-orders")
            self.assertEqual(captured["params"]["symbol"], "BTCUSDT")
        finally:
            bitget_client._get = orig_get


if __name__ == "__main__":
    unittest.main()

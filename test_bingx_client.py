import hashlib
import hmac
import json
import unittest
from unittest.mock import patch, MagicMock

import bingx_client


class SignTests(unittest.TestCase):
    def test_sign_sorts_params_and_matches_reference_hmac(self):
        bingx_client.BINGX_API_SECRET = "my-secret"
        params = {"symbol": "BTC-USDT", "timestamp": "1700000000000", "recvWindow": "5000"}
        qs = "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))
        expected = hmac.new(b"my-secret", qs.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(bingx_client._sign(params), expected)

    def test_different_params_produce_different_signatures(self):
        bingx_client.BINGX_API_SECRET = "secret"
        sig1 = bingx_client._sign({"a": "1"})
        sig2 = bingx_client._sign({"a": "2"})
        self.assertNotEqual(sig1, sig2)


class ConfiguredTests(unittest.TestCase):
    def test_not_configured_when_missing(self):
        orig = (bingx_client.BINGX_API_KEY, bingx_client.BINGX_API_SECRET)
        bingx_client.BINGX_API_KEY = ""
        bingx_client.BINGX_API_SECRET = "x"
        try:
            self.assertFalse(bingx_client.bingx_configured())
        finally:
            bingx_client.BINGX_API_KEY, bingx_client.BINGX_API_SECRET = orig

    def test_configured_when_both_present(self):
        orig = (bingx_client.BINGX_API_KEY, bingx_client.BINGX_API_SECRET)
        bingx_client.BINGX_API_KEY = "k"
        bingx_client.BINGX_API_SECRET = "s"
        try:
            self.assertTrue(bingx_client.bingx_configured())
        finally:
            bingx_client.BINGX_API_KEY, bingx_client.BINGX_API_SECRET = orig

    def test_configure_updates_credentials(self):
        orig = (bingx_client.BINGX_API_KEY, bingx_client.BINGX_API_SECRET)
        try:
            bingx_client.configure(api_key="new-key", api_secret="new-secret")
            self.assertEqual(bingx_client.BINGX_API_KEY, "new-key")
            self.assertEqual(bingx_client.BINGX_API_SECRET, "new-secret")
        finally:
            bingx_client.BINGX_API_KEY, bingx_client.BINGX_API_SECRET = orig


class TpSlPayloadTests(unittest.TestCase):
    def test_take_profit_payload_shape(self):
        payload = json.loads(bingx_client._tp_sl_payload(50000.0, is_take_profit=True))
        self.assertEqual(payload["type"], "TAKE_PROFIT_MARKET")
        self.assertEqual(payload["stopPrice"], 50000.0)
        self.assertEqual(payload["workingType"], "MARK_PRICE")

    def test_stop_loss_payload_shape(self):
        payload = json.loads(bingx_client._tp_sl_payload(48000.0, is_take_profit=False))
        self.assertEqual(payload["type"], "STOP_MARKET")
        self.assertEqual(payload["stopPrice"], 48000.0)


class CreateOrderParamsTests(unittest.TestCase):
    def test_limit_order_includes_price_and_gtc(self):
        bingx_client.BINGX_API_KEY = "k"
        bingx_client.BINGX_API_SECRET = "s"
        captured = {}
        with patch("bingx_client._request", side_effect=lambda method, path, params=None, signed=True: captured.update(method=method, path=path, params=params) or {}):
            bingx_client.create_order(
                symbol="BTC-USDT", side="BUY", position_side="LONG", order_type="LIMIT",
                quantity=0.01, price=100000.0, take_profit_price=101000.0, stop_loss_price=99500.0,
            )
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/openApi/swap/v2/trade/order")
        p = captured["params"]
        self.assertEqual(p["price"], 100000.0)
        self.assertEqual(p["timeInForce"], "GTC")
        self.assertIn("takeProfit", p)
        self.assertIn("stopLoss", p)
        tp = json.loads(p["takeProfit"])
        self.assertEqual(tp["stopPrice"], 101000.0)

    def test_limit_order_without_price_raises(self):
        with self.assertRaises(ValueError):
            bingx_client.create_order(symbol="BTC-USDT", side="BUY", position_side="LONG", order_type="LIMIT", quantity=0.01)

    def test_market_order_omits_price_and_tif(self):
        captured = {}
        with patch("bingx_client._request", side_effect=lambda method, path, params=None, signed=True: captured.update(params=params) or {}):
            bingx_client.create_order(symbol="BTC-USDT", side="SELL", position_side="SHORT", order_type="MARKET", quantity=0.01)
        self.assertNotIn("price", captured["params"])
        self.assertNotIn("timeInForce", captured["params"])


class CancelOrderTests(unittest.TestCase):
    def test_requires_order_id_or_client_order_id(self):
        with self.assertRaises(ValueError):
            bingx_client.cancel_order("BTC-USDT")

    def test_cancel_all_open_orders_handles_per_order_failure(self):
        with patch("bingx_client.fetch_open_orders", return_value=[{"orderId": "1"}, {"orderId": "2"}]):
            def fake_cancel(symbol, order_id=None, client_order_id=None):
                if order_id == "2":
                    raise RuntimeError("boom")
                return {"orderId": order_id, "status": "CANCELLED"}
            with patch("bingx_client.cancel_order", side_effect=fake_cancel):
                result = bingx_client.cancel_all_open_orders("BTC-USDT")
        self.assertEqual(result["cancelled"], 2)
        self.assertIn("error", result["results"][1])


class RequestErrorHandlingTests(unittest.TestCase):
    def test_nonzero_code_raises_with_message(self):
        fake_resp = MagicMock()
        fake_resp.status_code = 400
        fake_resp.json.return_value = {"code": 100001, "msg": "invalid signature"}
        bingx_client.BINGX_API_KEY = "k"
        bingx_client.BINGX_API_SECRET = "s"
        with patch("bingx_client.requests.get", return_value=fake_resp):
            with self.assertRaises(RuntimeError) as ctx:
                bingx_client._request("GET", "/openApi/swap/v2/user/positions")
        self.assertIn("100001", str(ctx.exception))
        self.assertIn("invalid signature", str(ctx.exception))

    def test_code_zero_is_success(self):
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"code": 0, "msg": "", "data": {"ok": True}}
        bingx_client.BINGX_API_KEY = "k"
        bingx_client.BINGX_API_SECRET = "s"
        with patch("bingx_client.requests.get", return_value=fake_resp):
            result = bingx_client._request("GET", "/openApi/swap/v2/user/positions")
        self.assertEqual(result, {"ok": True})


if __name__ == "__main__":
    unittest.main()

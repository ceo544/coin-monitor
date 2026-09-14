import unittest
from indicators import ema, rsi, bollinger, indicators_from_klines, stochastic, adx, cci, vwap, taker_flow, ichimoku

class IndicatorTests(unittest.TestCase):
    def test_ema(self):
        values = [float(i) for i in range(1, 31)]
        self.assertIsNotNone(ema(values, 20))

    def test_rsi_range(self):
        values = [1,2,3,2,4,5,4,6,7,6,8,9,8,10,11,10,12,13,12,14]
        value = rsi([float(x) for x in values], 14)
        self.assertIsNotNone(value)
        self.assertGreaterEqual(value, 0)
        self.assertLessEqual(value, 100)

    def test_kline_indicators(self):
        klines = []
        for i in range(1, 230):
            price = 70000 + i
            klines.append([0, str(price-5), str(price+10), str(price-10), str(price), "12.3"])
        result = indicators_from_klines(klines)
        self.assertIsNotNone(result["ema20"])
        self.assertIsNotNone(result["atr14"])
        self.assertIn("bollinger20", result)
        # New indicators added for building an independent long/short model
        self.assertIn("stochastic", result)
        self.assertIn("adx14", result)
        self.assertIn("cci20", result)
        self.assertIn("vwap", result)
        self.assertIn("taker_flow", result)
        self.assertIn("ichimoku", result)


class IchimokuTests(unittest.TestCase):
    def test_not_enough_data_returns_none(self):
        result = ichimoku([1.0] * 10, [1.0] * 10, [1.0] * 10)
        self.assertIsNone(result["kijun_sen"])
        self.assertIsNone(result["price_vs_cloud"])

    def test_strong_uptrend_price_sits_above_cloud(self):
        n = 60
        highs = [float(100 + i * 3) for i in range(n)]
        lows = [float(95 + i * 3) for i in range(n)]
        closes = [float(99 + i * 3) for i in range(n)]
        result = ichimoku(highs, lows, closes)
        self.assertIsNotNone(result["tenkan_sen"])
        self.assertIsNotNone(result["kijun_sen"])
        self.assertIsNotNone(result["senkou_span_a"])
        self.assertIsNotNone(result["senkou_span_b"])
        self.assertEqual(result["chikou_span"], closes[-1])
        # In a clean uptrend, the fast line (tenkan, 9-period) sits above
        # the slow line (kijun, 26-period), and price is above the cloud.
        self.assertGreater(result["tenkan_sen"], result["kijun_sen"])
        self.assertEqual(result["price_vs_cloud"], "above")

    def test_strong_downtrend_price_sits_below_cloud(self):
        n = 60
        highs = [float(500 - i * 3) for i in range(n)]
        lows = [float(495 - i * 3) for i in range(n)]
        closes = [float(499 - i * 3) for i in range(n)]
        result = ichimoku(highs, lows, closes)
        self.assertLess(result["tenkan_sen"], result["kijun_sen"])
        self.assertEqual(result["price_vs_cloud"], "below")

    def test_flat_market_price_sits_inside_cloud(self):
        n = 60
        highs = [101.0] * n
        lows = [99.0] * n
        closes = [100.0] * n
        result = ichimoku(highs, lows, closes)
        self.assertEqual(result["price_vs_cloud"], "inside")

    def test_cloud_top_is_never_less_than_cloud_bottom(self):
        n = 60
        highs = [float(100 + (i % 7)) for i in range(n)]
        lows = [float(95 + (i % 5)) for i in range(n)]
        closes = [float(98 + (i % 6)) for i in range(n)]
        result = ichimoku(highs, lows, closes)
        self.assertGreaterEqual(result["cloud_top"], result["cloud_bottom"])


class StochasticTests(unittest.TestCase):
    def test_uptrend_gives_high_k(self):
        highs = [float(100 + i) for i in range(20)]
        lows = [float(95 + i) for i in range(20)]
        closes = [float(99 + i) for i in range(20)]  # closing near the high of each candle, trending up
        result = stochastic(highs, lows, closes, k_period=14, d_period=3)
        self.assertIsNotNone(result["k"])
        self.assertGreater(result["k"], 50)  # close is near the top of the recent range

    def test_not_enough_data_returns_none(self):
        result = stochastic([1.0, 2.0], [1.0, 2.0], [1.0, 2.0], k_period=14)
        self.assertIsNone(result["k"])
        self.assertIsNone(result["d"])

    def test_flat_range_is_neutral_not_a_crash(self):
        highs = [100.0] * 20
        lows = [100.0] * 20
        closes = [100.0] * 20
        result = stochastic(highs, lows, closes, k_period=14)
        self.assertEqual(result["k"], 50.0)


class AdxTests(unittest.TestCase):
    def test_strong_uptrend_has_high_plus_di(self):
        n = 60
        highs = [float(100 + i * 2) for i in range(n)]
        lows = [float(95 + i * 2) for i in range(n)]
        closes = [float(99 + i * 2) for i in range(n)]
        result = adx(highs, lows, closes, period=14)
        self.assertIsNotNone(result["adx"])
        self.assertIsNotNone(result["plus_di"])
        self.assertGreater(result["plus_di"], result["minus_di"])

    def test_not_enough_data_returns_none(self):
        result = adx([1.0, 2.0], [1.0, 2.0], [1.0, 2.0], period=14)
        self.assertIsNone(result["adx"])


class CciTests(unittest.TestCase):
    def test_price_spike_above_average_gives_positive_cci(self):
        n = 25
        highs = [100.0] * (n - 1) + [130.0]
        lows = [98.0] * (n - 1) + [128.0]
        closes = [99.0] * (n - 1) + [129.0]  # sharp spike on the last candle
        value = cci(highs, lows, closes, period=20)
        self.assertIsNotNone(value)
        self.assertGreater(value, 0)

    def test_not_enough_data_returns_none(self):
        self.assertIsNone(cci([1.0], [1.0], [1.0], period=20))


class VwapTests(unittest.TestCase):
    def test_weights_toward_high_volume_candles(self):
        highs = [100.0, 200.0]
        lows = [100.0, 200.0]
        closes = [100.0, 200.0]
        volumes = [1.0, 99.0]  # almost all volume at price 200
        value = vwap(highs, lows, closes, volumes)
        self.assertIsNotNone(value)
        self.assertGreater(value, 150)  # pulled heavily toward the high-volume price

    def test_zero_volume_returns_none(self):
        self.assertIsNone(vwap([100.0], [100.0], [100.0], [0.0]))


class TakerFlowTests(unittest.TestCase):
    def test_extracts_buy_and_sell_volume_from_kline_row(self):
        # Binance kline row: [open_time, open, high, low, close, volume, close_time,
        #                      quote_volume, trades, taker_buy_base_volume, taker_buy_quote_volume, ignore]
        klines = [[0, "100", "101", "99", "100", "10.0", 0, "0", 5, "7.0", "0", "0"]]
        result = taker_flow(klines)
        self.assertAlmostEqual(result["taker_buy_volume"], 7.0)
        self.assertAlmostEqual(result["taker_sell_volume"], 3.0)
        self.assertAlmostEqual(result["taker_buy_ratio"], 0.7)

    def test_empty_klines_returns_none_fields(self):
        result = taker_flow([])
        self.assertIsNone(result["taker_buy_volume"])
        self.assertIsNone(result["taker_buy_ratio"])

    def test_short_row_without_taker_data_returns_none_fields(self):
        result = taker_flow([[0, "100", "101", "99", "100", "10.0"]])
        self.assertIsNone(result["taker_buy_volume"])


if __name__ == "__main__":
    unittest.main()

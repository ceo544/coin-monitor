import unittest
from indicators import ema, rsi, bollinger, indicators_from_klines

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

if __name__ == "__main__":
    unittest.main()

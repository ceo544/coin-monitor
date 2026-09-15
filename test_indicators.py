import unittest
import math
from indicators import (
    ema, rsi, bollinger, indicators_from_klines, stochastic, adx, cci, vwap, taker_flow, ichimoku,
    slope, atr_pct, stoch_rsi, supertrend, market_structure, cvd_from_klines,
    dema, hma, roc, keltner_channel, bb_keltner_squeeze, volume_ma, pivot_levels,
    vwap_distance_pct, ema_alignment, di_cross,
    price_returns, recent_high_low, ema_distance_features, macd_cross, stoch_cross, cci_cross,
    bollinger_extra, vwap_extra, volume_extra, cvd_divergence, oi_price_classification,
    funding_extra, ichimoku_extra,
)

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
        self.assertIn("atr_pct", result)
        self.assertIn("stoch_rsi", result)
        self.assertIn("supertrend", result)
        self.assertIn("market_structure", result)
        self.assertIn("cvd", result)
        self.assertIn("rsi14_history", result)
        self.assertIn("macd_hist_history", result)
        self.assertIn("adx14_history", result)
        self.assertIn("cci20_history", result)
        self.assertIn("stoch_k_history", result)
        self.assertIn("atr_pct_history", result)


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


class SlopeTests(unittest.TestCase):
    def test_rising_series_has_positive_slope(self):
        self.assertGreater(slope([1.0, 2.0, 3.0, 4.0, 5.0]), 0)

    def test_falling_series_has_negative_slope(self):
        self.assertLess(slope([5.0, 4.0, 3.0, 2.0, 1.0]), 0)

    def test_flat_series_has_zero_slope(self):
        self.assertEqual(slope([3.0, 3.0, 3.0]), 0.0)

    def test_single_point_returns_none(self):
        self.assertIsNone(slope([1.0]))


class AtrPctTests(unittest.TestCase):
    def test_higher_price_gives_lower_pct_for_same_atr(self):
        n = 30
        highs = [100 + i * 0.2 for i in range(n)]
        lows = [99 + i * 0.2 for i in range(n)]
        closes = [99.5 + i * 0.2 for i in range(n)]
        pct_low_price = atr_pct(highs, lows, closes, 14)
        highs2 = [10000 + i * 0.2 for i in range(n)]
        lows2 = [9999 + i * 0.2 for i in range(n)]
        closes2 = [9999.5 + i * 0.2 for i in range(n)]
        pct_high_price = atr_pct(highs2, lows2, closes2, 14)
        self.assertIsNotNone(pct_low_price)
        self.assertIsNotNone(pct_high_price)
        self.assertGreater(pct_low_price, pct_high_price)  # same absolute range, much higher % at the lower price


class StochRsiTests(unittest.TestCase):
    def test_not_enough_data_returns_none(self):
        result = stoch_rsi([1.0] * 10)
        self.assertIsNone(result["k"])

    def test_returns_bounded_values_with_enough_data(self):
        values = [50 + 10 * math.sin(i / 4) for i in range(60)]
        result = stoch_rsi(values)
        if result["k"] is not None:
            self.assertGreaterEqual(result["k"], 0)
            self.assertLessEqual(result["k"], 100)


class SupertrendTests(unittest.TestCase):
    def test_not_enough_data_returns_none(self):
        result = supertrend([1.0] * 5, [1.0] * 5, [1.0] * 5, period=10)
        self.assertIsNone(result["value"])
        self.assertIsNone(result["direction"])

    def test_strong_uptrend_direction_is_up(self):
        n = 60
        highs = [float(100 + i * 3) for i in range(n)]
        lows = [float(95 + i * 3) for i in range(n)]
        closes = [float(99 + i * 3) for i in range(n)]
        result = supertrend(highs, lows, closes)
        self.assertEqual(result["direction"], "up")
        self.assertIsNotNone(result["value"])

    def test_strong_downtrend_direction_is_down(self):
        n = 60
        highs = [float(1000 - i * 3) for i in range(n)]
        lows = [float(995 - i * 3) for i in range(n)]
        closes = [float(999 - i * 3) for i in range(n)]
        result = supertrend(highs, lows, closes)
        self.assertEqual(result["direction"], "down")


class MarketStructureTests(unittest.TestCase):
    def test_wavy_uptrend_detected_as_hh_hl(self):
        n = 60
        highs = [100 + i * 0.5 + 5 * math.sin(i / 3) for i in range(n)]
        lows = [95 + i * 0.5 + 5 * math.sin(i / 3) for i in range(n)]
        result = market_structure(highs, lows)
        self.assertEqual(result["last_high_type"], "HH")
        self.assertEqual(result["last_low_type"], "HL")
        self.assertEqual(result["structure"], "uptrend")

    def test_wavy_downtrend_detected_as_lh_ll(self):
        n = 60
        highs = [500 - i * 0.5 + 5 * math.sin(i / 3) for i in range(n)]
        lows = [495 - i * 0.5 + 5 * math.sin(i / 3) for i in range(n)]
        result = market_structure(highs, lows)
        self.assertEqual(result["last_high_type"], "LH")
        self.assertEqual(result["last_low_type"], "LL")
        self.assertEqual(result["structure"], "downtrend")

    def test_pure_monotonic_trend_has_no_pivots(self):
        # A strictly-increasing series has no local swing points by
        # definition - should return None rather than a false structure.
        n = 30
        highs = [float(100 + i) for i in range(n)]
        lows = [float(95 + i) for i in range(n)]
        result = market_structure(highs, lows)
        self.assertIsNone(result["structure"])


class CvdTests(unittest.TestCase):
    def test_buy_dominant_volume_gives_positive_cvd(self):
        klines = [[0, "100", "101", "99", "100", "10.0", 0, "0", 5, "8.0", "0", "0"] for _ in range(10)]
        result = cvd_from_klines(klines)
        self.assertGreater(result["cvd"], 0)

    def test_sell_dominant_volume_gives_negative_cvd(self):
        klines = [[0, "100", "101", "99", "100", "10.0", 0, "0", 5, "2.0", "0", "0"] for _ in range(10)]
        result = cvd_from_klines(klines)
        self.assertLess(result["cvd"], 0)

    def test_empty_klines_returns_none(self):
        result = cvd_from_klines([])
        self.assertIsNone(result["cvd"])


class DemaHmaTests(unittest.TestCase):
    def test_dema_tracks_uptrend_and_not_none(self):
        values = [float(100 + i) for i in range(60)]
        self.assertIsNotNone(dema(values, 20))

    def test_hma_tracks_uptrend_and_not_none(self):
        values = [float(100 + i) for i in range(60)]
        self.assertIsNotNone(hma(values, 20))

    def test_not_enough_data_returns_none(self):
        self.assertIsNone(dema([1.0, 2.0], 20))
        self.assertIsNone(hma([1.0, 2.0], 20))


class RocTests(unittest.TestCase):
    def test_uptrend_gives_positive_roc(self):
        values = [float(100 + i) for i in range(30)]
        self.assertGreater(roc(values, 12), 0)

    def test_downtrend_gives_negative_roc(self):
        values = [float(500 - i) for i in range(30)]
        self.assertLess(roc(values, 12), 0)

    def test_not_enough_data_returns_none(self):
        self.assertIsNone(roc([1.0, 2.0], 12))


class KeltnerTests(unittest.TestCase):
    def test_upper_above_middle_above_lower(self):
        n = 30
        highs = [100 + i * 0.3 for i in range(n)]
        lows = [99 + i * 0.3 for i in range(n)]
        closes = [99.5 + i * 0.3 for i in range(n)]
        kc = keltner_channel(highs, lows, closes, 20)
        self.assertGreater(kc["upper"], kc["middle"])
        self.assertGreater(kc["middle"], kc["lower"])

    def test_squeeze_true_when_bb_inside_kc(self):
        bb = {"upper": 101.0, "lower": 99.0}
        kc = {"upper": 102.0, "lower": 98.0}
        self.assertTrue(bb_keltner_squeeze(bb, kc))

    def test_squeeze_false_when_bb_wider_than_kc(self):
        bb = {"upper": 105.0, "lower": 95.0}
        kc = {"upper": 102.0, "lower": 98.0}
        self.assertFalse(bb_keltner_squeeze(bb, kc))

    def test_squeeze_none_with_missing_data(self):
        self.assertIsNone(bb_keltner_squeeze({"upper": None, "lower": 99.0}, {"upper": 102.0, "lower": 98.0}))


class VolumeMaTests(unittest.TestCase):
    def test_spike_gives_ratio_above_one(self):
        volumes = [10.0] * 19 + [50.0]  # last candle is a 5x volume spike
        result = volume_ma(volumes, 20)
        self.assertGreater(result["volume_ratio"], 1.0)

    def test_not_enough_data_returns_none(self):
        result = volume_ma([1.0, 2.0], 20)
        self.assertIsNone(result["volume_ma"])


class PivotLevelsTests(unittest.TestCase):
    def test_finds_recent_swing_levels_in_wavy_data(self):
        n = 60
        highs = [100 + 5 * math.sin(i / 3) for i in range(n)]
        lows = [95 + 5 * math.sin(i / 3) for i in range(n)]
        closes = [97.5 + 5 * math.sin(i / 3) for i in range(n)]
        result = pivot_levels(highs, lows, closes)
        self.assertIsNotNone(result["recent_pivot_high"])
        self.assertIsNotNone(result["recent_pivot_low"])
        self.assertIsNotNone(result["dist_to_pivot_high_pct"])


class VwapDistanceTests(unittest.TestCase):
    def test_price_above_vwap_is_positive(self):
        self.assertGreater(vwap_distance_pct([110.0], 100.0), 0)

    def test_price_below_vwap_is_negative(self):
        self.assertLess(vwap_distance_pct([90.0], 100.0), 0)

    def test_none_vwap_returns_none(self):
        self.assertIsNone(vwap_distance_pct([100.0], None))


class EmaAlignmentTests(unittest.TestCase):
    def test_uptrend_is_bullish_alignment(self):
        values = [float(100 + i) for i in range(250)]
        result = ema_alignment(values)
        self.assertEqual(result["alignment"], "bullish")

    def test_downtrend_is_bearish_alignment(self):
        values = [float(1000 - i) for i in range(250)]
        result = ema_alignment(values)
        self.assertEqual(result["alignment"], "bearish")

    def test_not_enough_data_returns_none_alignment(self):
        result = ema_alignment([1.0, 2.0, 3.0])
        self.assertIsNone(result["alignment"])


class DiCrossTests(unittest.TestCase):
    def test_not_enough_data_returns_none(self):
        self.assertIsNone(di_cross([1.0, 2.0], [1.0, 2.0], [1.0, 2.0]))

    def test_returns_a_valid_label_with_enough_data(self):
        n = 60
        highs = [float(100 + i * 2) for i in range(n)]
        lows = [float(95 + i * 2) for i in range(n)]
        closes = [float(99 + i * 2) for i in range(n)]
        result = di_cross(highs, lows, closes)
        self.assertIn(result, ("bullish", "bearish", "none", None))


class PriceReturnsTests(unittest.TestCase):
    def test_uptrend_gives_positive_returns(self):
        closes = [float(100 + i) for i in range(30)]
        result = price_returns(closes)
        self.assertGreater(result["return_1"], 0)
        self.assertGreater(result["return_5"], 0)
        self.assertGreater(result["return_15"], 0)

    def test_not_enough_data_returns_none(self):
        result = price_returns([100.0])
        self.assertIsNone(result["return_15"])


class RecentHighLowTests(unittest.TestCase):
    def test_finds_correct_extremes(self):
        highs = [100.0, 105.0, 103.0, 110.0, 102.0]
        lows = [95.0, 98.0, 97.0, 100.0, 96.0]
        closes = [99.0, 102.0, 100.0, 105.0, 99.0]
        result = recent_high_low(highs, lows, closes, period=5)
        self.assertEqual(result["highest_high"], 110.0)
        self.assertEqual(result["lowest_low"], 95.0)


class EmaDistanceTests(unittest.TestCase):
    def test_uptrend_price_above_all_emas(self):
        closes = [float(100 + i) for i in range(250)]
        result = ema_distance_features(closes)
        self.assertGreater(result["close_vs_ema9_pct"], 0)
        self.assertGreater(result["close_vs_ema200_pct"], 0)
        self.assertGreater(result["ema9_ema21_pct"], 0)


class CrossDetectionTests(unittest.TestCase):
    def test_macd_cross_not_enough_data(self):
        self.assertIsNone(macd_cross([1.0, 2.0]))

    def test_macd_cross_returns_valid_label(self):
        closes = [float(100 + i) for i in range(60)]
        self.assertIn(macd_cross(closes), ("bullish", "bearish", "none"))

    def test_stoch_cross_returns_valid_label(self):
        n = 60
        highs = [float(100 + i) for i in range(n)]
        lows = [float(95 + i) for i in range(n)]
        closes = [float(99 + i) for i in range(n)]
        self.assertIn(stoch_cross(highs, lows, closes), ("bullish", "bearish", "none"))

    def test_cci_cross_returns_dict_with_valid_labels(self):
        n = 60
        highs = [float(100 + i) for i in range(n)]
        lows = [float(95 + i) for i in range(n)]
        closes = [float(99 + i) for i in range(n)]
        result = cci_cross(highs, lows, closes)
        for key in ("zero_cross", "plus100_cross", "minus100_cross"):
            self.assertIn(result[key], ("up", "down", "none"))


class BollingerExtraTests(unittest.TestCase):
    def test_position_bounded_reasonably_inside_band(self):
        bb = {"upper": 110.0, "middle": 100.0, "lower": 90.0}
        result = bollinger_extra([105.0], bb)
        self.assertAlmostEqual(result["bb_width"], 20.0)
        self.assertAlmostEqual(result["bb_position"], 0.75)  # (105-90)/20

    def test_missing_band_returns_none(self):
        result = bollinger_extra([100.0], {"upper": None, "middle": None, "lower": None})
        self.assertIsNone(result["bb_width"])


class VwapExtraTests(unittest.TestCase):
    def test_price_above_vwap_gives_positive_diff(self):
        n = 30
        highs = [110.0] * n
        lows = [100.0] * n
        closes = [105.0] * n
        volumes = [10.0] * n
        result = vwap_extra(highs, lows, closes, volumes, 100.0, 2.0)
        self.assertAlmostEqual(result["vwap_diff"], 5.0)
        self.assertAlmostEqual(result["vwap_diff_atr"], 2.5)


class VolumeExtraTests(unittest.TestCase):
    def test_spike_flagged_true(self):
        volumes = [10.0] * 19 + [50.0]
        result = volume_extra(volumes)
        self.assertTrue(result["volume_spike"])

    def test_normal_volume_not_flagged(self):
        volumes = [10.0] * 20
        result = volume_extra(volumes)
        self.assertFalse(result["volume_spike"])


class CvdDivergenceTests(unittest.TestCase):
    def test_price_up_cvd_down_is_bearish_divergence(self):
        result = cvd_divergence([100.0, 105.0], (50.0, 30.0))
        self.assertEqual(result, "bearish_divergence")

    def test_price_down_cvd_up_is_bullish_divergence(self):
        result = cvd_divergence([105.0, 100.0], (30.0, 50.0))
        self.assertEqual(result, "bullish_divergence")

    def test_missing_data_returns_none(self):
        self.assertIsNone(cvd_divergence([100.0, 105.0], None))


class OiPriceClassificationTests(unittest.TestCase):
    def test_all_four_combinations(self):
        self.assertEqual(oi_price_classification(1.0, 1.0), "price_up_oi_up")
        self.assertEqual(oi_price_classification(1.0, -1.0), "price_up_oi_down")
        self.assertEqual(oi_price_classification(-1.0, 1.0), "price_down_oi_up")
        self.assertEqual(oi_price_classification(-1.0, -1.0), "price_down_oi_down")

    def test_missing_data_returns_none(self):
        self.assertIsNone(oi_price_classification(None, 1.0))


class FundingExtraTests(unittest.TestCase):
    def test_extreme_flag_true_above_threshold(self):
        result = funding_extra(0.001, [], extreme_threshold=0.0005)
        self.assertTrue(result["funding_extreme"])

    def test_extreme_flag_false_below_threshold(self):
        result = funding_extra(0.0001, [], extreme_threshold=0.0005)
        self.assertFalse(result["funding_extreme"])

    def test_change_computed_from_history(self):
        history = [{"fundingRate": "0.0001"}, {"fundingRate": "0.0003"}]
        result = funding_extra(0.0005, history)
        self.assertAlmostEqual(result["funding_change"], 0.0002)  # 0.0005 (current) - 0.0003 (most recent past settlement)


class IchimokuExtraTests(unittest.TestCase):
    def test_thickness_and_distance_above_cloud(self):
        ichi = {"cloud_top": 100.0, "cloud_bottom": 95.0}
        result = ichimoku_extra(ichi, 110.0)
        self.assertAlmostEqual(result["cloud_thickness"], 5.0)
        self.assertGreater(result["distance_to_cloud_pct"], 0)

    def test_inside_cloud_gives_zero_distance(self):
        ichi = {"cloud_top": 100.0, "cloud_bottom": 95.0}
        result = ichimoku_extra(ichi, 97.0)
        self.assertEqual(result["distance_to_cloud_pct"], 0.0)


class MarketStructureBreaksTests(unittest.TestCase):
    def test_uptrend_price_above_last_swing_high_is_bullish_bos(self):
        n = 60
        highs = [100 + i * 0.5 + 5 * math.sin(i / 3) for i in range(n)]
        lows = [95 + i * 0.5 + 5 * math.sin(i / 3) for i in range(n)]
        closes = list(highs)  # force close to be at/above the recent high
        from indicators import market_structure, market_structure_breaks
        ms = market_structure(highs, lows)
        result = market_structure_breaks(ms, closes)
        self.assertIn(result["bos"], ("bullish", None))  # bullish if it actually broke the last swing high

    def test_no_structure_returns_none(self):
        from indicators import market_structure, market_structure_breaks
        ms = market_structure([1.0] * 5, [1.0] * 5)
        result = market_structure_breaks(ms, [1.0] * 5)
        self.assertIsNone(result["bos"])
        self.assertIsNone(result["choch"])



    def test_all_v58_fields_present(self):
        klines = []
        for i in range(1, 260):
            price = 70000 + i * 10
            klines.append([0, str(price-5), str(price+15), str(price-15), str(price), "12.3", 0, "0", 100, "7.5", "0", "0"])
        result = indicators_from_klines(klines)
        for key in (
            "atr7", "cci14", "price_returns", "recent_high_low", "ema_distance",
            "macd_cross", "stoch_cross", "cci_cross", "bollinger_extra", "vwap_extra",
            "volume_extra", "cvd_divergence", "ichimoku_extra",
        ):
            self.assertIn(key, result, f"missing key: {key}")


if __name__ == "__main__":
    unittest.main()

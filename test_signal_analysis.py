import unittest
from signal_analysis import signal_snapshot, summarize_signal_records


def rec(side='long', rsi=65, hist=2, close=100, ema20=90, funding='0.0001', oi='123'):
    return {
        'id': 1, 'observed_at': '2026-09-07T01:00:00+00:00', 'current_price': '100',
        'long_signal': side == 'long', 'short_signal': side == 'short',
        'binance': {
            'premium_index': {'lastFundingRate': funding},
            'open_interest': {'openInterest': oi},
            'indicators': {'15m': {'close': close, 'ema20': ema20, 'ema50': 80, 'ema200': 70, 'rsi14': rsi,
              'macd': {'macd': 3, 'signal': 1, 'histogram': hist},
              'bollinger20': {'upper': 110, 'middle': 95, 'lower': 80}, 'atr14': 4}}
        }
    }

class SignalAnalysisTests(unittest.TestCase):
    def test_snapshot_contains_indicator_context(self):
        s = signal_snapshot(rec('short'))
        self.assertEqual(s['signal'], 'SHORT')
        self.assertEqual(s['timeframes']['15m']['rsi14'], 65)
        self.assertEqual(s['timeframes']['15m']['price_vs_ema20'], 'above')
        self.assertEqual(s['funding_rate'], 0.0001)

    def test_summary_groups_long_short(self):
        out = summarize_signal_records([rec('long', 60, 2), rec('long', 70, -1), rec('short', 40, -3)])
        self.assertEqual(out['LONG']['count'], 2)
        self.assertEqual(out['SHORT']['count'], 1)
        self.assertAlmostEqual(out['LONG']['timeframes']['15m']['avg_rsi14'], 65.0)
        self.assertEqual(out['LONG']['timeframes']['15m']['macd_hist_positive_pct'], 50.0)

if __name__ == '__main__': unittest.main()

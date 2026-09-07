import unittest

from main import _ai_tp_opinion, _atr_distance_opinion, _momentum_caution, _rr_opinion, _to_float_loose


class ToFloatLooseTests(unittest.TestCase):
    def test_parses_comma_numbers(self):
        self.assertEqual(_to_float_loose("68,450.5"), 68450.5)

    def test_none_and_garbage(self):
        self.assertIsNone(_to_float_loose(None))
        self.assertIsNone(_to_float_loose("n/a"))


class RiskRewardOpinionTests(unittest.TestCase):
    def test_low_risk_reward_flagged(self):
        text = _rr_opinion(0.5)
        self.assertIn("낮은 편", text)

    def test_reasonable_risk_reward(self):
        text = _rr_opinion(2.0)
        self.assertIn("무난한 편", text)

    def test_very_high_risk_reward_flagged(self):
        text = _rr_opinion(5.0)
        self.assertIn("매우 높은 편", text)

    def test_none_when_missing(self):
        self.assertIsNone(_rr_opinion(None))


class AtrDistanceOpinionTests(unittest.TestCase):
    def test_tight_tp_flagged(self):
        text = _atr_distance_opinion(tp_distance=100, atr=1000)
        self.assertIn("타이트함", text)

    def test_far_tp_flagged(self):
        text = _atr_distance_opinion(tp_distance=5000, atr=1000)
        self.assertIn("먼 편", text)

    def test_missing_atr_returns_none(self):
        self.assertIsNone(_atr_distance_opinion(100, None))
        self.assertIsNone(_atr_distance_opinion(100, 0))


class MomentumCautionTests(unittest.TestCase):
    def test_long_overbought_flagged(self):
        msgs = _momentum_caution("long", rsi=75, funding_rate=None)
        self.assertTrue(any("과매수권" in m for m in msgs))

    def test_short_oversold_flagged(self):
        msgs = _momentum_caution("short", rsi=20, funding_rate=None)
        self.assertTrue(any("과매도권" in m for m in msgs))

    def test_long_funding_overheated_flagged(self):
        msgs = _momentum_caution("long", rsi=None, funding_rate=0.001)
        self.assertTrue(any("숏 스퀴즈" in m for m in msgs))

    def test_short_funding_overheated_flagged(self):
        msgs = _momentum_caution("short", rsi=None, funding_rate=-0.001)
        self.assertTrue(any("숏 커버링" in m for m in msgs))

    def test_neutral_conditions_no_messages(self):
        msgs = _momentum_caution("long", rsi=50, funding_rate=0.0001)
        self.assertEqual(msgs, [])


class AiTpOpinionTests(unittest.TestCase):
    def test_full_opinion_for_long(self):
        bullets = _ai_tp_opinion(
            "long", entry="100,000", tp="101,000", sl="99,500",
            atr15=1500, rsi15=75, funding_rate=0.001,
        )
        # risk/reward: distance_tp=1000, distance_sl=500 -> rr=2.0 -> "무난한 편"
        self.assertTrue(any("무난한 편" in b for b in bullets))
        # atr ratio: 1000/1500 = 0.67 -> falls in the "무난한 거리" bucket
        self.assertTrue(any("거리" in b for b in bullets))
        self.assertTrue(any("과매수권" in b for b in bullets))
        self.assertTrue(any("숏 스퀴즈" in b for b in bullets))

    def test_missing_entry_or_tp_returns_empty(self):
        self.assertEqual(_ai_tp_opinion("long", None, "101000", "99500", 1500, 75, 0.001), [])
        self.assertEqual(_ai_tp_opinion("long", "100000", None, "99500", 1500, 75, 0.001), [])

    def test_missing_sl_skips_risk_reward_but_keeps_others(self):
        bullets = _ai_tp_opinion("short", "100000", "98500", None, 1500, 20, -0.001)
        self.assertFalse(any("손익비" in b for b in bullets))
        self.assertTrue(any("과매도권" in b for b in bullets))


if __name__ == "__main__":
    unittest.main()

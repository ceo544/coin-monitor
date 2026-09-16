import unittest
import main


def _indicators(tf15_direction=None, tf1h_direction=None, taker_15m=None):
    ind = {
        "15m": {"supertrend": {"direction": tf15_direction}, "taker_flow": {"taker_buy_ratio": taker_15m}},
        "1h": {"supertrend": {"direction": tf1h_direction}},
    }
    return ind


class AssessEntryRiskTests(unittest.TestCase):
    def test_clean_long_signal_has_no_flags(self):
        ind = _indicators(tf15_direction="up", tf1h_direction="up", taker_15m=0.65)
        result = main.assess_entry_risk("long", 5.0, ind)
        self.assertEqual(result["flags"], [])
        self.assertEqual(result["risk_level"], 0)
        self.assertFalse(result["stale"])
        self.assertFalse(result["higher_tf_conflict"])
        self.assertFalse(result["order_flow_weak"])
        self.assertIn(result["grade"], ("A", "B"))  # mostly-favorable but 4h/adx/structure/ichimoku left unset in this fixture

    def test_real_incident_scenario_all_three_flags_raised(self):
        # Mirrors the actual 09/15 20:10 case: signal 210 minutes old,
        # 15m+1h Supertrend both down while side=long, weak taker buy ratio.
        ind = _indicators(tf15_direction="down", tf1h_direction="down", taker_15m=0.30)
        result = main.assess_entry_risk("long", 210.83, ind)
        self.assertTrue(result["stale"])
        self.assertTrue(result["higher_tf_conflict"])
        self.assertTrue(result["order_flow_weak"])
        self.assertEqual(result["risk_level"], 3)
        self.assertEqual(set(result["flags"]), {"stale", "higher_tf_conflict", "order_flow_weak"})
        self.assertEqual(result["grade"], "D")

    def test_full_incident_fixture_grades_D_with_near_zero_score(self):
        ind = {
            "15m": {"supertrend": {"direction": "down"}, "taker_flow": {"taker_buy_ratio": 0.30},
                    "adx14": {"plus_di": 14.92, "minus_di": 23.02, "adx": 48.29},
                    "market_structure": {"structure": "downtrend"}},
            "1h": {"supertrend": {"direction": "down"}, "adx14": {"plus_di": 17.08, "minus_di": 28.16, "adx": 26.11},
                   "ichimoku": {"price_vs_cloud": "below"}},
            "4h": {"supertrend": {"direction": "up"}},
        }
        result = main.assess_entry_risk("long", 210.83, ind)
        self.assertEqual(result["grade"], "D")
        self.assertLess(result["score_pct"], 35)

    def test_fully_aligned_fixture_grades_A(self):
        ind = {
            "15m": {"supertrend": {"direction": "up"}, "taker_flow": {"taker_buy_ratio": 0.68},
                    "adx14": {"plus_di": 30, "minus_di": 12, "adx": 35},
                    "market_structure": {"structure": "uptrend"}},
            "1h": {"supertrend": {"direction": "up"}, "adx14": {"plus_di": 28, "minus_di": 14, "adx": 30},
                   "ichimoku": {"price_vs_cloud": "above"}},
            "4h": {"supertrend": {"direction": "up"}},
        }
        result = main.assess_entry_risk("long", 5.0, ind)
        self.assertEqual(result["grade"], "A")
        self.assertEqual(result["score_pct"], 100.0)

    def test_grade_is_none_when_no_indicator_data_at_all(self):
        result = main.assess_entry_risk("long", None, {})
        self.assertIsNone(result["grade"])
        self.assertIsNone(result["score_pct"])

    def test_grade_direction_aware_for_short(self):
        ind = {
            "15m": {"supertrend": {"direction": "down"}, "taker_flow": {"taker_buy_ratio": 0.30},
                    "adx14": {"plus_di": 12, "minus_di": 30, "adx": 35},
                    "market_structure": {"structure": "downtrend"}},
            "1h": {"supertrend": {"direction": "down"}, "adx14": {"plus_di": 14, "minus_di": 28, "adx": 30},
                   "ichimoku": {"price_vs_cloud": "below"}},
            "4h": {"supertrend": {"direction": "down"}},
        }
        result = main.assess_entry_risk("short", 5.0, ind)
        self.assertEqual(result["grade"], "A")

    def test_stale_threshold_boundary(self):
        ind = _indicators()
        self.assertFalse(main.assess_entry_risk("long", 44.9, ind)["stale"])
        self.assertTrue(main.assess_entry_risk("long", 45.1, ind)["stale"])

    def test_custom_stale_threshold(self):
        ind = _indicators()
        result = main.assess_entry_risk("long", 20.0, ind, stale_minutes=15.0)
        self.assertTrue(result["stale"])

    def test_none_minutes_since_start_is_not_stale(self):
        # e.g. right at boot before streak state has been recovered - should
        # fail safe (not flagged) rather than crash on a None comparison.
        ind = _indicators()
        result = main.assess_entry_risk("long", None, ind)
        self.assertFalse(result["stale"])

    def test_higher_tf_conflict_requires_BOTH_15m_and_1h_against(self):
        # Only 15m down, 1h still up - not a "conflict" (mixed signal, not a
        # confirmed higher-timeframe reversal).
        ind = _indicators(tf15_direction="down", tf1h_direction="up")
        result = main.assess_entry_risk("long", 5.0, ind)
        self.assertFalse(result["higher_tf_conflict"])

    def test_higher_tf_conflict_is_direction_aware_for_short(self):
        # For a SHORT position, the "against" direction is UP, not DOWN.
        ind = _indicators(tf15_direction="up", tf1h_direction="up")
        result = main.assess_entry_risk("short", 5.0, ind)
        self.assertTrue(result["higher_tf_conflict"])
        ind2 = _indicators(tf15_direction="down", tf1h_direction="down")
        result2 = main.assess_entry_risk("short", 5.0, ind2)
        self.assertFalse(result2["higher_tf_conflict"])

    def test_order_flow_weak_is_direction_aware(self):
        # Low taker buy ratio (sellers dominant) is bad for LONG, fine for SHORT.
        ind_low = _indicators(taker_15m=0.25)
        self.assertTrue(main.assess_entry_risk("long", 5.0, ind_low)["order_flow_weak"])
        self.assertFalse(main.assess_entry_risk("short", 5.0, ind_low)["order_flow_weak"])
        ind_high = _indicators(taker_15m=0.75)
        self.assertFalse(main.assess_entry_risk("long", 5.0, ind_high)["order_flow_weak"])
        self.assertTrue(main.assess_entry_risk("short", 5.0, ind_high)["order_flow_weak"])

    def test_missing_indicator_data_does_not_crash(self):
        result = main.assess_entry_risk("long", 5.0, {})
        self.assertFalse(result["higher_tf_conflict"])
        self.assertFalse(result["order_flow_weak"])

    def test_missing_taker_ratio_is_not_flagged(self):
        ind = _indicators(taker_15m=None)
        result = main.assess_entry_risk("long", 5.0, ind)
        self.assertFalse(result["order_flow_weak"])


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch

import main


def _sample_parsed(long_active=True, short_active=False):
    return {
        "signals": {
            "long": {"active": long_active, "detected_color": "#44C27B" if long_active else None, "visual_evidence": "", "matched_text": "LONG" if long_active else None},
            "short": {"active": short_active, "detected_color": "#EA4026" if short_active else None, "visual_evidence": "", "matched_text": "SHORT" if short_active else None},
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


class SimplifiedSignalMessageTests(unittest.TestCase):
    def test_long_message_shows_only_long_1st_stage_entry(self):
        msg = main._build_signal_message("long", True, _sample_parsed(), "100200", "#44C27B", {})
        self.assertIn("[LONG 1차 진입가]", msg)
        self.assertIn("진입1(25%): 100,000.0", msg)
        self.assertNotIn("SHORT", msg)
        self.assertNotIn("진입2", msg)
        self.assertNotIn("판정 근거", msg)
        self.assertNotIn("지표 참고", msg)

    def test_short_message_shows_only_short_1st_stage_entry(self):
        msg = main._build_signal_message("short", True, _sample_parsed(), "104100", "#EA4026", {})
        self.assertIn("[SHORT 1차 진입가]", msg)
        self.assertIn("진입1(25%): 104,000.0", msg)
        self.assertNotIn("LONG", msg)
        self.assertNotIn("판정 근거", msg)
        self.assertNotIn("지표 참고", msg)

    def test_off_message_still_minimal(self):
        msg = main._build_signal_message("long", False, _sample_parsed(), "100200", None, {})
        self.assertIn("신호해제", msg)
        self.assertNotIn("진입가", msg)

    def test_clean_signal_without_risk_context_has_no_grade_block(self):
        # No binance_snapshot/minutes_since_start given at all - should not
        # crash and should not add an empty/misleading risk block.
        msg = main._build_signal_message("long", True, _sample_parsed(), "100200", "#44C27B", {})
        self.assertNotIn("진입 품질", msg)


class SignalMessageRiskWarningTests(unittest.TestCase):
    """The exact real-world scenario the risk grade was built from: a
    person should see 'D등급 · 456분째 지속 · ⏱ 오래된 신호 · ⚠ 상위시간봉
    역행 · ⚠ 체결강도 약함' directly in the signal-ON alert, matching what
    the dashboard shows."""

    def _risky_snapshot(self):
        return {"indicators": {
            "15m": {"supertrend": {"direction": "down"}, "taker_flow": {"taker_buy_ratio": 0.30}},
            "1h": {"supertrend": {"direction": "down"}},
        }}

    def test_risky_context_shows_grade_age_and_all_warnings(self):
        msg = main._build_signal_message(
            "long", True, _sample_parsed(), "77149.3", "#44C27B",
            self._risky_snapshot(), minutes_since_start=456.3,
        )
        self.assertIn("진입 품질", msg)
        self.assertIn("D등급", msg)
        self.assertIn("456분째 지속", msg)
        self.assertIn("⏱ 오래된 신호", msg)
        self.assertIn("⚠ 상위시간봉 역행", msg)
        self.assertIn("⚠ 체결강도 약함", msg)

    def test_fresh_clean_signal_shows_grade_without_warnings(self):
        clean = {"indicators": {
            "15m": {"supertrend": {"direction": "up"}, "taker_flow": {"taker_buy_ratio": 0.65}},
            "1h": {"supertrend": {"direction": "up"}},
        }}
        msg = main._build_signal_message(
            "long", True, _sample_parsed(), "100200", "#44C27B", clean, minutes_since_start=2.0,
        )
        self.assertIn("진입 품질", msg)
        self.assertIn("2분째 지속", msg)
        self.assertNotIn("오래된 신호", msg)
        self.assertNotIn("상위시간봉 역행", msg)
        self.assertNotIn("체결강도 약함", msg)


class EntryProximityAlertTests(unittest.TestCase):
    TEST_USER_ID = 999001

    def setUp(self):
        self.orig_send = main.send_telegram_message
        self.sent = []
        main.send_telegram_message = lambda msg, bot_token, chat_id, user_id=None: self.sent.append(msg)
        main.telegram_states.pop(self.TEST_USER_ID, None)

    def tearDown(self):
        main.send_telegram_message = self.orig_send
        main.telegram_states.pop(self.TEST_USER_ID, None)

    def _notify(self, parsed, current_price_raw):
        main._maybe_notify_entry_proximity(self.TEST_USER_ID, "fake-token", "fake-chat", "", parsed, current_price_raw)

    def test_far_from_entry_does_not_trigger(self):
        self._notify(_sample_parsed(), "100200")  # 200 away, default threshold 100
        self.assertEqual(len(self.sent), 0)

    def test_within_threshold_triggers_once(self):
        self._notify(_sample_parsed(), "100050")  # 50 away
        self.assertEqual(len(self.sent), 1)
        self.assertIn("진입 임박", self.sent[0])
        self.assertIn("LONG", self.sent[0])

    def test_staying_within_threshold_does_not_retrigger(self):
        self._notify(_sample_parsed(), "100050")
        self._notify(_sample_parsed(), "100040")
        self.assertEqual(len(self.sent), 1)

    def test_moving_away_then_back_retriggers(self):
        self._notify(_sample_parsed(), "100050")
        self._notify(_sample_parsed(), "100500")  # moves out of range
        self._notify(_sample_parsed(), "100010")  # back in range
        self.assertEqual(len(self.sent), 2)

    def test_short_side_tracked_independently_from_long(self):
        self._notify(_sample_parsed(long_active=True, short_active=True), "100050")  # near LONG entry only
        self.assertEqual(len(self.sent), 1)
        self.assertIn("LONG", self.sent[0])
        self._notify(_sample_parsed(long_active=True, short_active=True), "104050")  # now also near SHORT entry
        self.assertEqual(len(self.sent), 2)
        self.assertIn("SHORT", self.sent[1])

    def test_no_telegram_credentials_does_nothing(self):
        main._maybe_notify_entry_proximity(self.TEST_USER_ID, "", "fake-chat", "", _sample_parsed(), "100050")
        self.assertEqual(len(self.sent), 0)

    def test_signal_off_never_triggers_even_when_price_is_near(self):
        # The key requirement: proximity alerts only fire while E-RANG's
        # own ON/OFF signal for that side is actually ON - price being
        # close to the entry level is not enough by itself.
        self._notify(_sample_parsed(long_active=False, short_active=False), "100050")
        self.assertEqual(len(self.sent), 0)

    def test_short_signal_off_does_not_trigger_even_near_short_entry(self):
        self._notify(_sample_parsed(long_active=False, short_active=False), "104050")
        self.assertEqual(len(self.sent), 0)

    def test_turning_on_near_price_triggers_fresh(self):
        # Price already near while OFF -> no alert; once it turns ON while
        # still near, it should fire (state must not have gotten "stuck").
        self._notify(_sample_parsed(long_active=False), "100050")
        self.assertEqual(len(self.sent), 0)
        self._notify(_sample_parsed(long_active=True), "100050")
        self.assertEqual(len(self.sent), 1)


class EntryProximityRiskWarningTests(unittest.TestCase):
    """The '진입 임박' alert is exactly the moment a person is likely to
    act, so it's where the risk-grade warning (from the real incident)
    gets attached."""
    TEST_USER_ID = 999003

    def setUp(self):
        self.orig_send = main.send_telegram_message
        self.sent = []
        main.send_telegram_message = lambda msg, bot_token, chat_id, user_id=None: self.sent.append(msg)
        main.telegram_states.pop(self.TEST_USER_ID, None)

    def tearDown(self):
        main.send_telegram_message = self.orig_send
        main.telegram_states.pop(self.TEST_USER_ID, None)

    def test_risky_context_adds_warning_to_message(self):
        binance_json = {"indicators": {
            "15m": {"supertrend": {"direction": "down"}, "taker_flow": {"taker_buy_ratio": 0.30}},
            "1h": {"supertrend": {"direction": "down"}},
        }}
        main._maybe_notify_entry_proximity(
            self.TEST_USER_ID, "fake-token", "fake-chat", "", _sample_parsed(), "100050",
            binance_json=binance_json, minutes_since_long=210.83,
        )
        self.assertEqual(len(self.sent), 1)
        self.assertIn("주의", self.sent[0])
        self.assertIn("오래된 신호", self.sent[0])
        self.assertIn("상위시간봉 역행", self.sent[0])
        self.assertIn("체결강도 약함", self.sent[0])

    def test_clean_context_has_no_warning_in_message(self):
        binance_json = {"indicators": {
            "15m": {"supertrend": {"direction": "up"}, "taker_flow": {"taker_buy_ratio": 0.65}},
            "1h": {"supertrend": {"direction": "up"}},
        }}
        main._maybe_notify_entry_proximity(
            self.TEST_USER_ID, "fake-token", "fake-chat", "", _sample_parsed(), "100050",
            binance_json=binance_json, minutes_since_long=5.0,
        )
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("주의", self.sent[0])

    def test_no_binance_context_sends_plain_message_without_error(self):
        main._maybe_notify_entry_proximity(
            self.TEST_USER_ID, "fake-token", "fake-chat", "", _sample_parsed(), "100050",
        )
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("주의", self.sent[0])


class DayRiskNotificationTests(unittest.TestCase):
    TEST_USER_ID = 999004

    def setUp(self):
        self.orig_send = main.send_telegram_message
        self.sent = []
        main.send_telegram_message = lambda msg, bot_token, chat_id, user_id=None: self.sent.append(msg)
        main._day_risk_notified_date.pop(self.TEST_USER_ID, None)

    def tearDown(self):
        main.send_telegram_message = self.orig_send
        main._day_risk_notified_date.pop(self.TEST_USER_ID, None)

    def test_sends_once_when_risk_exists(self):
        with patch("main.get_today_risk", return_value=[{"type": "calendar", "title": "CPI m/m", "detail": "21:30 KST"}]):
            main._maybe_notify_day_risk(self.TEST_USER_ID, "fake-token", "fake-chat", "")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("오늘은 위험한 날", self.sent[0])
        self.assertIn("CPI m/m", self.sent[0])

    def test_does_not_resend_same_day(self):
        with patch("main.get_today_risk", return_value=[{"type": "calendar", "title": "CPI m/m", "detail": ""}]):
            main._maybe_notify_day_risk(self.TEST_USER_ID, "fake-token", "fake-chat", "")
            main._maybe_notify_day_risk(self.TEST_USER_ID, "fake-token", "fake-chat", "")
        self.assertEqual(len(self.sent), 1)

    def test_no_message_on_a_normal_day(self):
        with patch("main.get_today_risk", return_value=[]):
            main._maybe_notify_day_risk(self.TEST_USER_ID, "fake-token", "fake-chat", "")
        self.assertEqual(len(self.sent), 0)

    def test_missing_telegram_config_does_not_crash(self):
        main._maybe_notify_day_risk(self.TEST_USER_ID, "", "", "")
        self.assertEqual(len(self.sent), 0)


if __name__ == "__main__":
    unittest.main()

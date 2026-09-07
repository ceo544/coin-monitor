import unittest

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


class EntryProximityAlertTests(unittest.TestCase):
    def setUp(self):
        self.orig_token = main.TELEGRAM_BOT_TOKEN
        self.orig_chat = main.TELEGRAM_CHAT_ID
        self.orig_send = main.send_telegram_message
        main.TELEGRAM_BOT_TOKEN = "fake-token"
        main.TELEGRAM_CHAT_ID = "fake-chat"
        self.sent = []
        main.send_telegram_message = lambda msg: self.sent.append(msg)
        with main._state_lock:
            main.telegram_state["long_near_entry"] = False
            main.telegram_state["short_near_entry"] = False

    def tearDown(self):
        main.TELEGRAM_BOT_TOKEN = self.orig_token
        main.TELEGRAM_CHAT_ID = self.orig_chat
        main.send_telegram_message = self.orig_send

    def test_far_from_entry_does_not_trigger(self):
        main._maybe_notify_entry_proximity(_sample_parsed(), "100200")  # 200 away, default threshold 100
        self.assertEqual(len(self.sent), 0)

    def test_within_threshold_triggers_once(self):
        main._maybe_notify_entry_proximity(_sample_parsed(), "100050")  # 50 away
        self.assertEqual(len(self.sent), 1)
        self.assertIn("진입 임박", self.sent[0])
        self.assertIn("LONG", self.sent[0])

    def test_staying_within_threshold_does_not_retrigger(self):
        main._maybe_notify_entry_proximity(_sample_parsed(), "100050")
        main._maybe_notify_entry_proximity(_sample_parsed(), "100040")
        self.assertEqual(len(self.sent), 1)

    def test_moving_away_then_back_retriggers(self):
        main._maybe_notify_entry_proximity(_sample_parsed(), "100050")
        main._maybe_notify_entry_proximity(_sample_parsed(), "100500")  # moves out of range
        main._maybe_notify_entry_proximity(_sample_parsed(), "100010")  # back in range
        self.assertEqual(len(self.sent), 2)

    def test_short_side_tracked_independently_from_long(self):
        main._maybe_notify_entry_proximity(_sample_parsed(), "100050")  # near LONG entry only
        self.assertEqual(len(self.sent), 1)
        self.assertIn("LONG", self.sent[0])
        main._maybe_notify_entry_proximity(_sample_parsed(), "104050")  # now also near SHORT entry
        self.assertEqual(len(self.sent), 2)
        self.assertIn("SHORT", self.sent[1])

    def test_no_telegram_credentials_does_nothing(self):
        main.TELEGRAM_BOT_TOKEN = ""
        main._maybe_notify_entry_proximity(_sample_parsed(), "100050")
        self.assertEqual(len(self.sent), 0)


if __name__ == "__main__":
    unittest.main()

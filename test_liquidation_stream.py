import json
import time
import unittest

import liquidation_stream as ls


def _make_message(symbol="BTCUSDT", side="SELL", qty="0.5", price="70000"):
    return json.dumps({
        "e": "forceOrder",
        "E": int(time.time() * 1000),
        "o": {"s": symbol, "S": side, "o": "LIMIT", "q": qty, "p": price, "ap": price, "X": "FILLED"},
    })


class LiquidationMessageTests(unittest.TestCase):
    def setUp(self):
        with ls._events_lock:
            ls._events.clear()
        ls._state["last_error"] = None

    def test_matching_symbol_is_recorded(self):
        ls._on_message(None, _make_message(symbol="BTCUSDT", side="SELL", qty="0.5", price="70000"))
        with ls._events_lock:
            self.assertEqual(len(ls._events), 1)
            self.assertEqual(ls._events[0]["side"], "SELL")
            self.assertAlmostEqual(ls._events[0]["qty"], 0.5)
            self.assertAlmostEqual(ls._events[0]["notional"], 35000.0)

    def test_other_symbol_is_ignored(self):
        ls._on_message(None, _make_message(symbol="ETHUSDT"))
        with ls._events_lock:
            self.assertEqual(len(ls._events), 0)

    def test_malformed_message_does_not_raise(self):
        try:
            ls._on_message(None, "not valid json")
        except Exception as exc:
            self.fail(f"_on_message raised on malformed input: {exc}")
        self.assertIsNotNone(ls._state["last_error"])


class LiquidationSummaryTests(unittest.TestCase):
    def setUp(self):
        with ls._events_lock:
            ls._events.clear()

    def test_splits_long_vs_short_liquidations(self):
        now = time.time()
        with ls._events_lock:
            ls._events.append({"ts": now, "side": "SELL", "qty": 1.0, "notional": 70000.0})  # long liq
            ls._events.append({"ts": now, "side": "BUY", "qty": 2.0, "notional": 140000.0})  # short liq
        summary = ls.liquidation_summary(windows_minutes=[5])
        self.assertEqual(summary["m5"]["long_liq_count"], 1)
        self.assertEqual(summary["m5"]["short_liq_count"], 1)
        self.assertAlmostEqual(summary["m5"]["long_liq_qty"], 1.0)
        self.assertAlmostEqual(summary["m5"]["short_liq_qty"], 2.0)

    def test_imbalance_negative_when_short_liqs_dominate(self):
        now = time.time()
        with ls._events_lock:
            ls._events.append({"ts": now, "side": "SELL", "qty": 1.0, "notional": 10000.0})
            ls._events.append({"ts": now, "side": "BUY", "qty": 3.0, "notional": 30000.0})
        summary = ls.liquidation_summary(windows_minutes=[5])
        self.assertLess(summary["m5"]["liq_imbalance"], 0)

    def test_imbalance_none_with_no_events(self):
        summary = ls.liquidation_summary(windows_minutes=[5])
        self.assertIsNone(summary["m5"]["liq_imbalance"])

    def test_events_outside_window_are_excluded(self):
        now = time.time()
        with ls._events_lock:
            ls._events.append({"ts": now - 20 * 60, "side": "SELL", "qty": 1.0, "notional": 70000.0})  # 20 min ago
            ls._events.append({"ts": now, "side": "SELL", "qty": 1.0, "notional": 70000.0})  # now
        summary = ls.liquidation_summary(windows_minutes=[5])
        self.assertEqual(summary["m5"]["long_liq_count"], 1)  # only the recent one

    def test_multiple_windows_computed_independently(self):
        now = time.time()
        with ls._events_lock:
            ls._events.append({"ts": now - 10 * 60, "side": "SELL", "qty": 1.0, "notional": 70000.0})  # 10 min ago
            ls._events.append({"ts": now, "side": "SELL", "qty": 1.0, "notional": 70000.0})
        summary = ls.liquidation_summary(windows_minutes=[1, 15])
        self.assertEqual(summary["m1"]["long_liq_count"], 1)   # only the recent one within 1 min
        self.assertEqual(summary["m15"]["long_liq_count"], 2)  # both within 15 min

    def test_empty_events_gives_zeroed_summary_not_error(self):
        summary = ls.liquidation_summary(windows_minutes=[5])
        self.assertEqual(summary["m5"]["long_liq_count"], 0)
        self.assertEqual(summary["m5"]["short_liq_count"], 0)

    def test_summary_includes_connection_state(self):
        summary = ls.liquidation_summary(windows_minutes=[5])
        self.assertIn("connected", summary)
        self.assertIn("last_message_at", summary)


class PruneTests(unittest.TestCase):
    def setUp(self):
        with ls._events_lock:
            ls._events.clear()

    def test_old_events_get_pruned_on_new_message(self):
        old_ts = time.time() - ls.RETENTION_SECONDS - 10
        with ls._events_lock:
            ls._events.append({"ts": old_ts, "side": "SELL", "qty": 1.0, "notional": 1.0})
        ls._on_message(None, _make_message())
        with ls._events_lock:
            self.assertEqual(len(ls._events), 1)  # old one pruned, only the new message remains


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch, MagicMock

import news_feed


SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Sample Feed</title>
<item>
  <title>Clarity Act rejected in Senate vote</title>
  <link>https://example.com/clarity-act-rejected</link>
  <pubDate>Mon, 15 Sep 2026 09:00:00 GMT</pubDate>
</item>
<item>
  <title>Bitcoin holds above $70,000 after volatile session</title>
  <link>https://example.com/btc-70k</link>
  <pubDate>Mon, 15 Sep 2026 07:30:00 GMT</pubDate>
</item>
</channel></rss>"""

MALFORMED_RSS = "<rss><channel><item><title>Broken"


def _fake_response(text, status=200):
    resp = MagicMock()
    resp.content = text.encode("utf-8")
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status}")
    return resp


class FetchFeedTests(unittest.TestCase):
    def test_parses_titles_links_and_dates(self):
        with patch("news_feed.requests.get", return_value=_fake_response(SAMPLE_RSS)):
            items, err = news_feed.fetch_feed("TestSource", "https://example.com/rss")
        self.assertEqual(err, "")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "Clarity Act rejected in Senate vote")
        self.assertEqual(items[0]["link"], "https://example.com/clarity-act-rejected")
        self.assertEqual(items[0]["source"], "TestSource")
        self.assertIsNotNone(items[0]["published_at"])

    def test_network_error_returns_empty_list_and_error_message(self):
        with patch("news_feed.requests.get", side_effect=ConnectionError("boom")):
            items, err = news_feed.fetch_feed("TestSource", "https://example.com/rss")
        self.assertEqual(items, [])
        self.assertIn("boom", err)

    def test_malformed_xml_returns_empty_list_and_error(self):
        with patch("news_feed.requests.get", return_value=_fake_response(MALFORMED_RSS)):
            items, err = news_feed.fetch_feed("TestSource", "https://example.com/rss")
        self.assertEqual(items, [])
        self.assertNotEqual(err, "")

    def test_item_missing_title_or_link_is_skipped(self):
        rss = """<rss><channel>
        <item><title>Has both</title><link>https://example.com/a</link></item>
        <item><title>Missing link only</title></item>
        <item><link>https://example.com/b</link></item>
        </channel></rss>"""
        with patch("news_feed.requests.get", return_value=_fake_response(rss)):
            items, err = news_feed.fetch_feed("TestSource", "https://example.com/rss")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Has both")


class FetchAllNewsTests(unittest.TestCase):
    def test_combines_and_sorts_across_sources_newest_first(self):
        older = """<rss><channel><item><title>Older</title><link>https://a.com/1</link><pubDate>Mon, 15 Sep 2026 01:00:00 GMT</pubDate></item></channel></rss>"""
        newer = """<rss><channel><item><title>Newer</title><link>https://b.com/1</link><pubDate>Mon, 15 Sep 2026 09:00:00 GMT</pubDate></item></channel></rss>"""

        def fake_get(url, timeout=None, headers=None):
            if "coindesk" in url:
                return _fake_response(older)
            return _fake_response(newer)

        with patch("news_feed.requests.get", side_effect=fake_get):
            result = news_feed.fetch_all_news()
        self.assertGreaterEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["title"], "Newer")
        self.assertIsNone(result["errors"])

    def test_one_source_failing_does_not_block_others(self):
        def fake_get(url, timeout=None, headers=None):
            if "coindesk" in url:
                raise ConnectionError("coindesk down")
            return _fake_response(SAMPLE_RSS)

        with patch("news_feed.requests.get", side_effect=fake_get):
            result = news_feed.fetch_all_news()
        self.assertGreater(len(result["items"]), 0)
        self.assertIn("CoinDesk", result["errors"])

    def test_all_sources_failing_gives_empty_items_not_a_crash(self):
        with patch("news_feed.requests.get", side_effect=ConnectionError("all down")):
            result = news_feed.fetch_all_news()
        self.assertEqual(result["items"], [])
        self.assertEqual(len(result["errors"]), len(news_feed.NEWS_SOURCES))

    def test_respects_limit(self):
        with patch("news_feed.requests.get", return_value=_fake_response(SAMPLE_RSS)):
            result = news_feed.fetch_all_news(limit=1)
        self.assertEqual(len(result["items"]), 1)


class EconomicCalendarTests(unittest.TestCase):
    def test_filters_to_high_impact_usd_only(self):
        raw = [
            {"title": "CPI m/m", "country": "USD", "impact": "High", "date": "2026-09-16T12:30:00-04:00", "forecast": "0.3%", "previous": "0.2%"},
            {"title": "Some EUR event", "country": "EUR", "impact": "High", "date": "2026-09-16T08:00:00-04:00"},
            {"title": "Low impact USD event", "country": "USD", "impact": "Low", "date": "2026-09-16T09:00:00-04:00"},
        ]
        with patch("news_feed.requests.get") as mock_get:
            mock_get.return_value.json.return_value = raw
            mock_get.return_value.raise_for_status = MagicMock()
            result = news_feed.fetch_economic_calendar()
        self.assertIsNone(result["error"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["title"], "CPI m/m")

    def test_sorted_soonest_first(self):
        raw = [
            {"title": "Later event", "country": "USD", "impact": "High", "date": "2026-09-18T12:00:00-04:00"},
            {"title": "Sooner event", "country": "USD", "impact": "High", "date": "2026-09-16T12:00:00-04:00"},
        ]
        with patch("news_feed.requests.get") as mock_get:
            mock_get.return_value.json.return_value = raw
            mock_get.return_value.raise_for_status = MagicMock()
            result = news_feed.fetch_economic_calendar()
        self.assertEqual(result["items"][0]["title"], "Sooner event")

    def test_network_error_returns_empty_items_and_error(self):
        with patch("news_feed.requests.get", side_effect=ConnectionError("boom")):
            result = news_feed.fetch_economic_calendar()
        self.assertEqual(result["items"], [])
        self.assertIn("boom", result["error"])

    def test_malformed_row_is_skipped_not_crashing(self):
        raw = [
            {"title": "Bad date", "country": "USD", "impact": "High", "date": "not-a-date"},
            {"title": "Good one", "country": "USD", "impact": "High", "date": "2026-09-16T12:00:00-04:00"},
        ]
        with patch("news_feed.requests.get") as mock_get:
            mock_get.return_value.json.return_value = raw
            mock_get.return_value.raise_for_status = MagicMock()
            result = news_feed.fetch_economic_calendar()
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["title"], "Good one")


class DetectTodayRiskTests(unittest.TestCase):
    def test_calendar_event_today_is_detected(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        calendar_items = [{"title": "CPI m/m", "date": now.isoformat()}]
        risks = news_feed.detect_today_risk([], calendar_items)
        self.assertEqual(len(risks), 1)
        self.assertEqual(risks[0]["type"], "calendar")
        self.assertEqual(risks[0]["title"], "CPI m/m")

    def test_calendar_event_tomorrow_is_not_detected(self):
        from datetime import datetime, timezone, timedelta
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1, hours=2)
        calendar_items = [{"title": "FOMC Statement", "date": tomorrow.isoformat()}]
        risks = news_feed.detect_today_risk([], calendar_items)
        self.assertEqual(risks, [])

    def test_clarity_act_news_today_is_detected(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        news_items = [{"title": "美 상원, 클래리티법 부결...비트코인 하락", "published_at": now.isoformat(), "source": "TokenPost"}]
        risks = news_feed.detect_today_risk(news_items, [])
        self.assertEqual(len(risks), 1)
        self.assertEqual(risks[0]["type"], "news")

    def test_english_clarity_keyword_also_detected(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        news_items = [{"title": "Senate rejects CLARITY Act in surprise vote", "published_at": now.isoformat(), "source": "CoinDesk"}]
        risks = news_feed.detect_today_risk(news_items, [])
        self.assertEqual(len(risks), 1)

    def test_unrelated_news_today_is_not_detected(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        news_items = [{"title": "BTC holds above $70,000 after volatile session", "published_at": now.isoformat(), "source": "CoinDesk"}]
        risks = news_feed.detect_today_risk(news_items, [])
        self.assertEqual(risks, [])

    def test_legislation_news_from_yesterday_is_not_detected(self):
        from datetime import datetime, timezone, timedelta
        yesterday = datetime.now(timezone.utc) - timedelta(days=1, hours=2)
        news_items = [{"title": "클래리티법 관련 논의 계속", "published_at": yesterday.isoformat(), "source": "TokenPost"}]
        risks = news_feed.detect_today_risk(news_items, [])
        self.assertEqual(risks, [])

    def test_combines_both_calendar_and_news_risks(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        calendar_items = [{"title": "FOMC Press Conference", "date": now.isoformat()}]
        news_items = [{"title": "클래리티법 재표결 임박", "published_at": now.isoformat(), "source": "TokenPost"}]
        risks = news_feed.detect_today_risk(news_items, calendar_items)
        self.assertEqual(len(risks), 2)

    def test_missing_published_at_is_skipped_not_crashing(self):
        news_items = [{"title": "클래리티법", "published_at": None, "source": "TokenPost"}]
        risks = news_feed.detect_today_risk(news_items, [])
        self.assertEqual(risks, [])


class TranslateToKoreanTests(unittest.TestCase):
    def setUp(self):
        news_feed._translation_cache.clear()

    def test_already_korean_text_is_not_translated(self):
        with patch("news_feed.requests.get") as mock_get:
            result = news_feed.translate_to_korean("비트코인 급등, 7만달러 돌파")
        mock_get.assert_not_called()
        self.assertEqual(result, "비트코인 급등, 7만달러 돌파")

    def test_english_text_gets_translated(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = [[["비트코인이 급등했다", "Bitcoin surged", None, None, 1]]]
        mock_resp.raise_for_status = MagicMock()
        with patch("news_feed.requests.get", return_value=mock_resp) as mock_get:
            result = news_feed.translate_to_korean("Bitcoin surged")
        mock_get.assert_called_once()
        self.assertEqual(result, "비트코인이 급등했다")

    def test_translation_is_cached(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = [[["번역됨", "text", None, None, 1]]]
        mock_resp.raise_for_status = MagicMock()
        with patch("news_feed.requests.get", return_value=mock_resp) as mock_get:
            news_feed.translate_to_korean("Some headline")
            news_feed.translate_to_korean("Some headline")
        mock_get.assert_called_once()  # 두 번째 호출은 캐시에서 바로 반환

    def test_translation_failure_falls_back_to_original(self):
        with patch("news_feed.requests.get", side_effect=ConnectionError("blocked")):
            result = news_feed.translate_to_korean("Senate rejects CLARITY Act")
        self.assertEqual(result, "Senate rejects CLARITY Act")

    def test_empty_text_returns_as_is(self):
        self.assertEqual(news_feed.translate_to_korean(""), "")


class DetectTodayRiskTranslationTests(unittest.TestCase):
    """detect_today_risk()가 실제로 번역을 거쳐서 결과를 내놓는지 (기존
    DetectTodayRiskTests는 한글 제목만 썼어서 번역 경로를 안 지나갔음)."""

    def test_english_news_title_is_translated_in_output(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        news_items = [{"title": "Senate rejects CLARITY Act in surprise vote", "published_at": now.isoformat(), "source": "CoinDesk"}]
        mock_resp = MagicMock()
        mock_resp.json.return_value = [[["상원, 깜짝 표결로 클래리티법 부결", "x", None, None, 1]]]
        mock_resp.raise_for_status = MagicMock()
        with patch("news_feed.requests.get", return_value=mock_resp):
            risks = news_feed.detect_today_risk(news_items, [])
        self.assertEqual(len(risks), 1)
        self.assertEqual(risks[0]["title"], "상원, 깜짝 표결로 클래리티법 부결")

    def test_korean_news_title_is_left_untouched(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        news_items = [{"title": "美 상원, 클래리티법 부결...비트코인 하락", "published_at": now.isoformat(), "source": "TokenPost"}]
        with patch("news_feed.requests.get") as mock_get:
            risks = news_feed.detect_today_risk(news_items, [])
        mock_get.assert_not_called()
        self.assertEqual(risks[0]["title"], "美 상원, 클래리티법 부결...비트코인 하락")


if __name__ == "__main__":
    unittest.main()

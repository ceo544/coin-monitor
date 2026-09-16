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


if __name__ == "__main__":
    unittest.main()

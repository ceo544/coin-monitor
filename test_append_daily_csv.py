import csv
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))
import append_daily_csv as script


def _csv_response_text(header, rows):
    buf = []
    buf.append(",".join(header))
    for row in rows:
        buf.append(",".join(str(v) for v in row))
    return "\n".join(buf) + "\n"


class ReadWriteLastIdTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_missing_file_returns_zero(self):
        self.assertEqual(script.read_last_id(self.tmpdir), 0)

    def test_write_then_read_roundtrip(self):
        script.write_last_id(self.tmpdir, 42)
        self.assertEqual(script.read_last_id(self.tmpdir), 42)

    def test_corrupt_file_falls_back_to_zero(self):
        with open(os.path.join(self.tmpdir, "last_id.txt"), "w") as f:
            f.write("not-a-number")
        self.assertEqual(script.read_last_id(self.tmpdir), 0)


class ChunkAppendTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.header = ["id", "value"]

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_first_run_creates_part001(self):
        rows = [[str(i), f"v{i}"] for i in range(1, 11)]
        changed = script.append_in_chunks(self.tmpdir, self.header, rows)
        self.assertEqual(changed, [os.path.join(self.tmpdir, "coin_observations_part001.csv")])
        self.assertEqual(script.count_data_rows(changed[0]), 10)

    def test_exceeding_chunk_size_creates_second_file(self):
        script.CHUNK_MAX_ROWS = 5
        try:
            rows = [[str(i), f"v{i}"] for i in range(1, 9)]  # 8 rows, chunk=5 -> part1(5)+part2(3)
            changed = script.append_in_chunks(self.tmpdir, self.header, rows)
            self.assertEqual(len(changed), 2)
            self.assertEqual(script.count_data_rows(changed[0]), 5)
            self.assertEqual(script.count_data_rows(changed[1]), 3)
        finally:
            script.CHUNK_MAX_ROWS = 1200

    def test_appending_to_existing_partially_filled_chunk(self):
        script.CHUNK_MAX_ROWS = 5
        try:
            script.append_in_chunks(self.tmpdir, self.header, [[str(i), f"v{i}"] for i in range(1, 4)])  # 3 rows in part1
            changed = script.append_in_chunks(self.tmpdir, self.header, [[str(i), f"v{i}"] for i in range(4, 6)])  # +2 rows, should still fit in part1 (total 5)
            self.assertEqual(changed, [os.path.join(self.tmpdir, "coin_observations_part001.csv")])
            self.assertEqual(script.count_data_rows(changed[0]), 5)
        finally:
            script.CHUNK_MAX_ROWS = 1200

    def test_new_chunk_started_when_previous_exactly_full(self):
        script.CHUNK_MAX_ROWS = 3
        try:
            script.append_in_chunks(self.tmpdir, self.header, [["1", "a"], ["2", "b"], ["3", "c"]])  # exactly fills part1
            changed = script.append_in_chunks(self.tmpdir, self.header, [["4", "d"]])
            self.assertTrue(changed[0].endswith("part002.csv"))
        finally:
            script.CHUNK_MAX_ROWS = 1200

    def test_no_new_rows_returns_no_changes(self):
        changed = script.append_in_chunks(self.tmpdir, self.header, [])
        self.assertEqual(changed, [])

    def test_chunk_files_never_exceed_max_rows(self):
        script.CHUNK_MAX_ROWS = 100
        try:
            rows = [[str(i), f"v{i}"] for i in range(1, 251)]  # 250 rows across 3 chunks
            changed = script.append_in_chunks(self.tmpdir, self.header, rows)
            for path in changed:
                self.assertLessEqual(script.count_data_rows(path), 100)
            total = sum(script.count_data_rows(p) for p in script.find_chunk_files(self.tmpdir) and
                        [os.path.join(self.tmpdir, n) for n in script.find_chunk_files(self.tmpdir)])
            self.assertEqual(total, 250)
        finally:
            script.CHUNK_MAX_ROWS = 1200


class FetchPageTests(unittest.TestCase):
    def test_parses_header_and_rows(self):
        mock_resp = MagicMock()
        mock_resp.text = _csv_response_text(["id", "price"], [[1, 100], [2, 101]])
        mock_resp.raise_for_status = MagicMock()
        with patch("append_daily_csv.requests.get", return_value=mock_resp) as mock_get:
            header, rows = script.fetch_page("https://example.com", "tok", 0)
        self.assertEqual(header, ["id", "price"])
        self.assertEqual(rows, [["1", "100"], ["2", "101"]])
        called_params = mock_get.call_args.kwargs["params"]
        self.assertEqual(called_params["slim"], "1")
        self.assertEqual(called_params["since_id"], 0)
        self.assertEqual(called_params["limit"], script.PAGE_SIZE)

    def test_empty_response_returns_empty(self):
        mock_resp = MagicMock()
        mock_resp.text = ""
        mock_resp.raise_for_status = MagicMock()
        with patch("append_daily_csv.requests.get", return_value=mock_resp):
            header, rows = script.fetch_page("https://example.com", "tok", 0)
        self.assertEqual(header, [])
        self.assertEqual(rows, [])

    def test_custom_limit_is_passed_through(self):
        mock_resp = MagicMock()
        mock_resp.text = _csv_response_text(["id"], [[1]])
        mock_resp.raise_for_status = MagicMock()
        with patch("append_daily_csv.requests.get", return_value=mock_resp) as mock_get:
            script.fetch_page("https://example.com", "tok", 5, limit=10)
        self.assertEqual(mock_get.call_args.kwargs["params"]["limit"], 10)


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_full_flow_creates_files_and_updates_last_id(self):
        mock_resp = MagicMock()
        mock_resp.text = _csv_response_text(["id", "price"], [[1, 100], [2, 101], [3, 102]])
        mock_resp.raise_for_status = MagicMock()
        with patch("append_daily_csv.requests.get", return_value=mock_resp), \
             patch("sys.argv", ["prog", "--url", "https://example.com", "--token", "tok", "--data-dir", self.tmpdir]):
            rc = script.main()
        self.assertEqual(rc, 0)
        self.assertEqual(script.read_last_id(self.tmpdir), 3)
        chunks = script.find_chunk_files(self.tmpdir)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(script.count_data_rows(os.path.join(self.tmpdir, chunks[0])), 3)

    def test_second_run_only_appends_new_rows(self):
        first_resp = MagicMock()
        first_resp.text = _csv_response_text(["id", "price"], [[1, 100], [2, 101]])
        first_resp.raise_for_status = MagicMock()
        with patch("append_daily_csv.requests.get", return_value=first_resp), \
             patch("sys.argv", ["prog", "--url", "https://example.com", "--token", "tok", "--data-dir", self.tmpdir]):
            script.main()

        second_resp = MagicMock()
        second_resp.text = _csv_response_text(["id", "price"], [[3, 102]])
        second_resp.raise_for_status = MagicMock()
        with patch("append_daily_csv.requests.get", return_value=second_resp) as mock_get, \
             patch("sys.argv", ["prog", "--url", "https://example.com", "--token", "tok", "--data-dir", self.tmpdir]):
            script.main()
        self.assertEqual(mock_get.call_args.kwargs["params"]["since_id"], 2)  # 이전 실행의 last_id부터
        chunks = script.find_chunk_files(self.tmpdir)
        self.assertEqual(script.count_data_rows(os.path.join(self.tmpdir, chunks[0])), 3)

    def test_no_new_data_leaves_files_untouched(self):
        empty_resp = MagicMock()
        empty_resp.text = ""
        empty_resp.raise_for_status = MagicMock()
        with patch("append_daily_csv.requests.get", return_value=empty_resp), \
             patch("sys.argv", ["prog", "--url", "https://example.com", "--token", "tok", "--data-dir", self.tmpdir]):
            rc = script.main()
        self.assertEqual(rc, 0)
        self.assertEqual(script.find_chunk_files(self.tmpdir), [])

    def test_pages_through_multiple_requests_when_more_data_than_page_size(self):
        # PAGE_SIZE 낮춰서, 실제로 여러 페이지에 걸쳐 루프를 도는지 확인.
        script.PAGE_SIZE = 2
        try:
            page1 = MagicMock()
            page1.text = _csv_response_text(["id", "price"], [[1, 100], [2, 101]])  # 꽉 채워진 페이지 -> 다음 페이지 있음
            page1.raise_for_status = MagicMock()
            page2 = MagicMock()
            page2.text = _csv_response_text(["id", "price"], [[3, 102]])  # PAGE_SIZE보다 적음 -> 마지막 페이지
            page2.raise_for_status = MagicMock()
            with patch("append_daily_csv.requests.get", side_effect=[page1, page2]) as mock_get, \
                 patch("sys.argv", ["prog", "--url", "https://example.com", "--token", "tok", "--data-dir", self.tmpdir]):
                rc = script.main()
            self.assertEqual(rc, 0)
            self.assertEqual(mock_get.call_count, 2)
            self.assertEqual(script.read_last_id(self.tmpdir), 3)
            chunks = script.find_chunk_files(self.tmpdir)
            self.assertEqual(script.count_data_rows(os.path.join(self.tmpdir, chunks[0])), 3)
        finally:
            script.PAGE_SIZE = 1200

    def test_last_id_updated_after_each_page_not_just_at_the_end(self):
        # 중간에 실패해도 이미 받은 페이지는 안 날아가야 함 - 페이지마다
        # last_id.txt가 즉시 갱신되는지 확인.
        script.PAGE_SIZE = 2
        try:
            page1 = MagicMock()
            page1.text = _csv_response_text(["id", "price"], [[1, 100], [2, 101]])
            page1.raise_for_status = MagicMock()
            page2_fails = ConnectionError("network blip")
            with patch("append_daily_csv.requests.get", side_effect=[page1, page2_fails]), \
                 patch("sys.argv", ["prog", "--url", "https://example.com", "--token", "tok", "--data-dir", self.tmpdir]):
                with self.assertRaises(ConnectionError):
                    script.main()
            # 첫 페이지까지는 이미 저장되어 있어야 함
            self.assertEqual(script.read_last_id(self.tmpdir), 2)
        finally:
            script.PAGE_SIZE = 1200


if __name__ == "__main__":
    unittest.main()

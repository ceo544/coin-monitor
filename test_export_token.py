import unittest

import main


def _sample_html(long_on=True):
    cls = "coin-strategy__long blue" if long_on else "coin-strategy__long"
    return f'''
    <html><head><style>.coin-strategy .coin-strategy__long.blue .coin-strategy__side {{ background: #44C27B !important; }}</style></head>
    <body><div class="coin-strategy"><div class="{cls}"><span class="coin-strategy__side">LONG</span></div><div class="coin-strategy__short"><span class="coin-strategy__side">SHORT</span></div></div>
    <table><tr><td>Long</td><td>100000</td></tr></table>
    현재가 100200 BTCUSDT</body></html>
    '''


class ExportTokenAuthTests(unittest.TestCase):
    def setUp(self):
        main.init_db()
        main.ENABLE_BINANCE = False
        main.fetch_target = lambda: (200, _sample_html())

    def test_no_auth_at_all_is_rejected(self):
        with main.app.test_client() as c:
            resp = c.get("/export.csv")
            self.assertEqual(resp.status_code, 401)

    def test_wrong_token_is_rejected(self):
        main.get_or_create_export_token()
        with main.app.test_client() as c:
            resp = c.get("/export.csv?token=not-the-real-token")
            self.assertEqual(resp.status_code, 401)

    def test_empty_token_setting_never_matches_empty_query_token(self):
        # Defensive: an unset/blank stored token must never be satisfied by
        # an equally-blank ?token= query value.
        main.save_setting("EXPORT_API_TOKEN", "")
        with main.app.test_client() as c:
            resp = c.get("/export.csv?token=")
            self.assertEqual(resp.status_code, 401)

    def test_correct_token_without_any_session_is_accepted(self):
        token = main.get_or_create_export_token()
        with main.app.test_client() as c:
            resp = c.get(f"/export.csv?token={token}")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("text/csv", resp.content_type)

    def test_export_signals_csv_also_accepts_the_token(self):
        token = main.get_or_create_export_token()
        with main.app.test_client() as c:
            resp = c.get(f"/export-signals.csv?token={token}")
            self.assertEqual(resp.status_code, 200)

    def test_regenerating_invalidates_the_old_token(self):
        old_token = main.get_or_create_export_token()
        with main.app.test_client() as c:
            c.post("/login", data={"username": "admin", "password": "1Q2w3e4r5t!!"})
            resp = c.post("/api/export-token/regenerate")
            new_token = resp.get_json()["token"]
        self.assertNotEqual(old_token, new_token)
        with main.app.test_client() as c2:
            self.assertEqual(c2.get(f"/export.csv?token={old_token}").status_code, 401)
            self.assertEqual(c2.get(f"/export.csv?token={new_token}").status_code, 200)

    def test_logged_in_session_still_works_without_a_token(self):
        with main.app.test_client() as c:
            c.post("/login", data={"username": "admin", "password": "1Q2w3e4r5t!!"})
            resp = c.get("/export.csv")
            self.assertEqual(resp.status_code, 200)


class IncrementalExportTests(unittest.TestCase):
    def setUp(self):
        main.init_db()
        main.ENABLE_BINANCE = False
        main.fetch_target = lambda: (200, _sample_html())
        with main.db_cursor() as (conn, cur):
            cur.execute("DELETE FROM observations")

    def test_since_id_only_returns_newer_rows(self):
        ids = [main.collect_once()["id"] for _ in range(3)]
        token = main.get_or_create_export_token()
        with main.app.test_client() as c:
            full = c.get(f"/export.csv?token={token}").data.decode("utf-8").strip().split("\n")
            incremental = c.get(f"/export.csv?token={token}&since_id={ids[0]}").data.decode("utf-8").strip().split("\n")
        self.assertEqual(len(full), 4)         # header + 3 rows
        self.assertEqual(len(incremental), 3)  # header + 2 rows (id[1], id[2])

    def test_limit_caps_number_of_rows_returned(self):
        [main.collect_once()["id"] for _ in range(5)]
        token = main.get_or_create_export_token()
        with main.app.test_client() as c:
            resp = c.get(f"/export.csv?token={token}&limit=2")
        rows = resp.data.decode("utf-8").strip().split("\n")
        self.assertEqual(len(rows), 3)  # header + 2 rows only

    def test_limit_combined_with_since_id_pages_through_all_data(self):
        ids = [main.collect_once()["id"] for _ in range(5)]
        token = main.get_or_create_export_token()
        collected_ids = []
        cursor = 0
        with main.app.test_client() as c:
            for _ in range(10):  # safety cap on loop iterations
                resp = c.get(f"/export.csv?token={token}&since_id={cursor}&limit=2")
                rows = resp.data.decode("utf-8").strip().split("\n")[1:]
                if not rows or rows == [""]:
                    break
                for row in rows:
                    rid = int(row.split(",")[0])
                    collected_ids.append(rid)
                    cursor = max(cursor, rid)
        self.assertEqual(sorted(collected_ids), sorted(ids))

    def test_since_id_at_the_latest_id_returns_only_header(self):
        ids = [main.collect_once()["id"] for _ in range(2)]
        token = main.get_or_create_export_token()
        with main.app.test_client() as c:
            resp = c.get(f"/export.csv?token={token}&since_id={ids[-1]}")
        rows = resp.data.decode("utf-8").strip().split("\n")
        self.assertEqual(len(rows), 1)  # header only, nothing newer


class SlimExportTests(unittest.TestCase):
    """slim=1은 용량을 대부분 차지하는 parsed_json/binance_json 원본 컬럼을
    빼서, GitHub의 100MB 파일 제한에 걸리지 않게 하기 위한 모드입니다."""

    def setUp(self):
        main.init_db()
        main.ENABLE_BINANCE = False
        main.fetch_target = lambda: (200, _sample_html())
        with main.db_cursor() as (conn, cur):
            cur.execute("DELETE FROM observations")

    def test_slim_mode_excludes_raw_json_columns(self):
        main.collect_once()
        token = main.get_or_create_export_token()
        with main.app.test_client() as c:
            full_header = c.get(f"/export.csv?token={token}").data.decode("utf-8").split("\n")[0]
            slim_header = c.get(f"/export.csv?token={token}&slim=1").data.decode("utf-8").split("\n")[0]
        self.assertIn("parsed_json", full_header)
        self.assertIn("binance_json", full_header)
        self.assertNotIn("parsed_json", slim_header)
        self.assertNotIn("binance_json", slim_header)

    def test_slim_mode_keeps_all_flattened_indicator_columns(self):
        main.collect_once()
        token = main.get_or_create_export_token()
        with main.app.test_client() as c:
            slim_header = c.get(f"/export.csv?token={token}&slim=1").data.decode("utf-8").split("\n")[0]
        for col in ("long_signal", "short_signal", "current_price", "data_quality_score"):
            self.assertIn(col, slim_header)

    def test_slim_mode_is_dramatically_smaller(self):
        main.ENABLE_BINANCE = True
        main.collect_binance_snapshot = lambda: {
            "indicators": {tf: {"rsi14": 55.0, "macd": {"macd": 1, "signal": 1, "histogram": 0}} for tf in ("1m", "5m", "15m", "1h", "4h")},
        }
        main.collect_once()
        token = main.get_or_create_export_token()
        with main.app.test_client() as c:
            full_size = len(c.get(f"/export.csv?token={token}").data)
            slim_size = len(c.get(f"/export.csv?token={token}&slim=1").data)
        self.assertLess(slim_size, full_size)

    def test_slim_query_param_variants_all_work(self):
        main.collect_once()
        token = main.get_or_create_export_token()
        with main.app.test_client() as c:
            for val in ("1", "true", "yes"):
                header = c.get(f"/export.csv?token={token}&slim={val}").data.decode("utf-8").split("\n")[0]
                self.assertNotIn("parsed_json", header)
            header_off = c.get(f"/export.csv?token={token}&slim=0").data.decode("utf-8").split("\n")[0]
            self.assertIn("parsed_json", header_off)


if __name__ == "__main__":
    unittest.main()

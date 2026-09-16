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

    def test_since_id_at_the_latest_id_returns_only_header(self):
        ids = [main.collect_once()["id"] for _ in range(2)]
        token = main.get_or_create_export_token()
        with main.app.test_client() as c:
            resp = c.get(f"/export.csv?token={token}&since_id={ids[-1]}")
        rows = resp.data.decode("utf-8").strip().split("\n")
        self.assertEqual(len(rows), 1)  # header only, nothing newer


if __name__ == "__main__":
    unittest.main()

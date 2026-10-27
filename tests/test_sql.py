"""SQL console, PRAGMA report, suggestions, history and their HTTP endpoints.

    python3 -m unittest discover -s tests -v
"""

import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flutter_db_viewer import sql as s  # noqa: E402
from flutter_db_viewer import viewer as v  # noqa: E402
from flutter_db_viewer.demo import DemoApp  # noqa: E402


def make_db(path: Path) -> Path:
    """A small offline-first schema: masters, a doc table linking to them, a child table."""
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE docs__village (mobile_uuid TEXT PRIMARY KEY, server_name TEXT, village_name TEXT);
        CREATE TABLE docs__survey (
          mobile_uuid TEXT PRIMARY KEY, server_name TEXT, sync_status TEXT NOT NULL DEFAULT 'dirty',
          village TEXT, gender TEXT, phone TEXT, modified TEXT, village__is_local INTEGER);
        CREATE TABLE docs__survey_member (
          mobile_uuid TEXT PRIMARY KEY, parent_uuid TEXT, parent_doctype TEXT, member_name TEXT);
        CREATE TABLE settings (k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID;
        CREATE INDEX ix_survey_modified ON docs__survey(modified);
        """
    )
    con.executemany("INSERT INTO docs__village VALUES (?,?,?)",
                    [(f"vu{i}", f"V{i:03}", f"Village {i}") for i in range(20)])
    rows = []
    for i in range(1500):
        rows.append((f"su{i}", f"S{i}", "synced" if i % 7 else "dirty", f"V{i % 20:03}" if i % 50 else "V999",
                     "F" if i % 2 else "M", f"98{i % 1400:08}", f"2026-10-{1 + i % 27:02} 10:00:00", 0))
    con.executemany("INSERT INTO docs__survey VALUES (?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO docs__survey_member VALUES (?,?,?,?)",
                    [(f"m{i}", f"su{i % 300}", "Survey", f"Member {i}") for i in range(600)])
    con.execute("INSERT INTO settings VALUES ('a', '1')")
    con.commit()
    con.close()
    return path


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = make_db(Path(cls.tmp.name) / "app.db")
        cls.tables = v.analyze(cls.db)
        cls.counts = {n: t.rows for n, t in cls.tables.items()}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class ReadOnlyQueryTest(Fixture):
    def ok(self, sql):
        return s.run_query(self.db, sql)

    def denied(self, sql):
        with self.assertRaises(s.QueryError) as cm:
            s.run_query(self.db, sql)
        return str(cm.exception)

    def test_select_with_cte_and_pragma_functions(self):
        self.assertEqual(self.ok("SELECT count(*) FROM docs__survey;")["rows"], [[1500]])
        self.assertEqual(self.ok("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT 3) SELECT * FROM c")["rows"],
                         [[1], [2], [3]])
        cols = self.ok("SELECT name FROM pragma_table_info('docs__village')")["rows"]
        self.assertEqual([c[0] for c in cols], ["mobile_uuid", "server_name", "village_name"])
        self.assertEqual(self.ok("EXPLAIN QUERY PLAN SELECT * FROM docs__survey WHERE modified > '2026'")["columns"][-1], "detail")

    def test_read_pragmas_allowed(self):
        self.assertEqual(self.ok("PRAGMA user_version")["rows"], [[0]])
        self.assertEqual(len(self.ok("PRAGMA table_info(docs__survey)")["rows"]), 8)
        self.assertEqual(self.ok("PRAGMA quick_check")["rows"], [["ok"]])

    def test_writes_and_escapes_are_blocked(self):
        for sql in ("DELETE FROM docs__survey", "UPDATE docs__survey SET gender = 'X'",
                    "INSERT INTO settings VALUES ('b','2')", "DROP TABLE settings", "CREATE TABLE x(a)",
                    "PRAGMA user_version = 9", "PRAGMA journal_mode = DELETE", "ATTACH '/etc/hosts' AS h",
                    "SELECT load_extension('evil')", "VACUUM", "REINDEX"):
            self.assertIn("read-only", self.denied(sql).lower() + " read-only", sql)
        self.assertEqual(self.ok("SELECT count(*) FROM docs__survey")["rows"], [[1500]], "nothing changed")

    def test_authorizer_decisions(self):
        ok, deny = sqlite3.SQLITE_OK, sqlite3.SQLITE_DENY
        auth = s._authorizer
        # What SQLite 3.37 asks while loading pragma_table_info(…) — must be allowed.
        self.assertEqual(auth(sqlite3.SQLITE_UPDATE, "sqlite_master", "type", "main", None), ok)
        self.assertEqual(auth(sqlite3.SQLITE_PRAGMA, "table_info", "t", None, None), ok)
        self.assertEqual(auth(sqlite3.SQLITE_READ, "pragma_table_info", "name", "main", None), ok)
        # Real writes, even to the schema table from a trigger/view, stay denied.
        self.assertEqual(auth(sqlite3.SQLITE_UPDATE, "orders", "status", "main", None), deny)
        self.assertEqual(auth(sqlite3.SQLITE_UPDATE, "sqlite_master", "sql", "main", "some_trigger"), deny)
        self.assertEqual(auth(sqlite3.SQLITE_INSERT, "sqlite_master", None, "main", None), deny)
        self.assertEqual(auth(sqlite3.SQLITE_PRAGMA, "user_version", "5", None, None), deny)
        self.assertEqual(auth(sqlite3.SQLITE_PRAGMA, "writable_schema", None, None, None), deny)
        self.assertEqual(auth(sqlite3.SQLITE_ATTACH, "/etc/hosts", None, None, None), deny)

    def test_one_statement_at_a_time(self):
        self.assertIn("one statement", self.denied("SELECT 1; SELECT 2"))

    def test_row_cap_and_empty(self):
        res = s.run_query(self.db, "SELECT * FROM docs__survey", limit=10)
        self.assertEqual(len(res["rows"]), 10)
        self.assertTrue(res["truncated"])
        self.assertIn("Write a query", self.denied("   ;  "))

    def test_timeout(self):
        with self.assertRaises(s.QueryError) as cm:
            s.run_query(self.db, "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c",
                        timeout=0.3)
        self.assertIn("Stopped after", str(cm.exception))

    def test_blobs_are_described(self):
        self.assertEqual(self.ok("SELECT x'00ff'")["rows"], [["<blob 2 bytes>"]])


class ExplainTest(Fixture):
    def test_full_scan_tip_names_the_column(self):
        res = s.explain(self.db, "SELECT * FROM docs__survey WHERE gender = 'F' LIMIT 5", self.counts)
        self.assertTrue(res["plan"])
        warn = [t for t in res["tips"] if t["level"] == "warn"]
        self.assertTrue(warn and "docs__survey(gender)" in warn[0]["text"], res["tips"])

    def test_indexed_filter_gets_no_scan_warning(self):
        res = s.explain(self.db, "SELECT * FROM docs__survey WHERE modified = '2026-10-01 10:00:00'", self.counts)
        self.assertFalse([t for t in res["tips"] if t["level"] == "warn"], res["tips"])

    def test_leading_wildcard_and_missing_limit(self):
        tips = " ".join(t["text"] for t in s.explain(self.db, "SELECT * FROM docs__survey WHERE phone LIKE '%12'", {
            **self.counts, "docs__survey": 9000})["tips"])
        self.assertIn("leading %", tips)
        self.assertIn("No LIMIT", tips)

    def test_non_select_has_no_plan(self):
        self.assertEqual(s.explain(self.db, "PRAGMA user_version", self.counts), {"plan": [], "tips": []})

    def test_where_column_parsing(self):
        self.assertEqual(s.where_columns("select * from t a where a.x = 1 and y in (1) order by z", "t", "a"), ["x", "y"])
        self.assertEqual(s.where_columns("select * from t join u b on 1 where b.q = 2", "t", None), [])


class ReportTest(Fixture):
    def test_report(self):
        r = s.db_report(self.db, {"journal_mode": "wal", "main": 10, "wal": 5})
        values = {p["name"]: p["value"] for p in r["pragmas"]}
        self.assertEqual(values["journal_mode (device)"], "wal")
        self.assertEqual(values["encoding"], "UTF-8")
        self.assertEqual(r["counts"]["table"], 4)
        self.assertEqual(r["counts"]["index"], len(r["indexes"]))
        names = {i["name"]: i for i in r["indexes"]}
        self.assertEqual(names["ix_survey_modified"]["columns"], ["modified"])
        self.assertEqual(names["ix_survey_modified"]["origin"], "CREATE INDEX")
        self.assertGreater(r["size"]["bytes"], 0)
        if r["space_available"]:
            self.assertIn("docs__survey", {x["name"] for x in r["space"]})

    def test_checks(self):
        self.assertTrue(s.run_check(self.db, "quick")["ok"])
        self.assertTrue(s.run_check(self.db, "foreign_keys")["ok"])
        with self.assertRaises(s.QueryError):
            s.run_check(self.db, "vacuum")

    def test_table_meta(self):
        m = s.table_meta(self.db, "docs__survey")
        self.assertIn("ix_survey_modified", [i["name"] for i in m["indexes"]])
        self.assertEqual(m["foreign_keys"], [])


class SuggestionTest(Fixture):
    def setUp(self):
        self.sg = s.Suggester(self.db, self.tables)
        self.addCleanup(self.sg.close)

    def titles(self, table):
        return {x["title"]: x for x in self.sg.for_table(table)}

    def test_links_are_found_from_data(self):
        links = {link["column"]: link for link in self.sg.links_from("docs__survey")}
        self.assertEqual((links["village"]["table"], links["village"]["to"]), ("docs__village", "server_name"))
        self.assertGreater(links["village"]["rate"], 0.9)
        child = {link["column"]: link for link in self.sg.links_from("docs__survey_member")}
        self.assertEqual((child["parent_uuid"]["table"], child["parent_uuid"]["to"]), ("docs__survey", "mobile_uuid"))
        self.assertNotIn("village__is_local", links)

    def test_table_suggestions_cover_the_useful_kinds(self):
        t = self.titles("docs__survey")
        self.assertIn("Latest rows by modified", t)
        self.assertIn("Sync status breakdown", t)
        self.assertTrue(any(k.endswith("rows not synced") for k in t))
        self.assertIn("Rows per gender", t)
        self.assertIn("Join village → docs__village", t)
        self.assertIn("Broken links in village", t, "V999 doesn't exist in docs__village")
        self.assertTrue(any(k.startswith("Count of survey_member") for k in t))
        self.assertIn("Duplicate phone values", t)

    def test_every_suggestion_runs(self):
        for table in self.tables:
            for sug in self.sg.for_table(table):
                with self.subTest(table=table, title=sug["title"]):
                    s.run_query(self.db, sug["sql"], limit=5)
        for sug in self.sg.global_suggestions(["docs__survey"]):
            with self.subTest(title=sug["title"]):
                s.run_query(self.db, sug["sql"], limit=5)

    def test_global_suggestions(self):
        titles = [x["title"] for x in self.sg.global_suggestions(["docs__survey"])]
        self.assertIn("Row count of every table", titles)
        self.assertIn("Unsynced rows across tables", titles)
        self.assertIn("Latest rows in docs__survey", titles)

    def test_demo_database_links(self):
        with tempfile.TemporaryDirectory() as d:
            demo = DemoApp(Path(d))
            try:
                tables = v.analyze(demo.path)
                sg = s.Suggester(demo.path, tables)
                links = {link["column"]: link["table"] for link in sg.links_from("order_items")}
                children = {link["from"] for link in sg.links_to("orders")}
                sg.close()
            finally:
                demo.stop()
        self.assertEqual(links, {"order_id": "orders", "product_id": "products"})
        self.assertIn("order_items", children)


class HistoryTest(unittest.TestCase):
    def test_record_star_delete_and_persist(self):
        with tempfile.TemporaryDirectory() as d:
            h = s.QueryHistory("com.example.app", Path(d))
            h.record("SELECT 1;", 1, 0.1)
            h.record("SELECT   1\n", 1, 0.2)  # same query, different spacing
            h.record("SELECT 2", 1, 0.1)
            listing = h.listing()
            self.assertEqual(len(listing["recent"]), 2)
            self.assertEqual(listing["frequent"][0]["runs"], 2)
            h.star("SELECT 2", True, "two")
            again = s.QueryHistory("com.example.app", Path(d)).listing()
            self.assertEqual([q["name"] for q in again["starred"]], ["two"])
            h.delete("SELECT 1")
            self.assertEqual([q["sql"] for q in h.listing()["recent"]], ["SELECT 2"])

    def test_cap_keeps_starred(self):
        with tempfile.TemporaryDirectory() as d:
            h = s.QueryHistory("x", Path(d))
            h.MAX = 5
            h.star("SELECT 'keep'", True)
            for i in range(10):
                h.record(f"SELECT {i}", 1, 0.1)
            items = h.items
            self.assertEqual(len(items), 5)
            self.assertIn("SELECT 'keep'", [q["sql"] for q in items])

    def test_corrupt_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.json").write_text("{not json")
            self.assertEqual(s.QueryHistory("x", Path(d)).listing()["recent"], [])


class HttpSqlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        dev = Path(cls.tmp.name) / "device"
        dev.mkdir()
        make_db(dev / "app.db")
        hist = s.QueryHistory("test", Path(cls.tmp.name) / "hist")
        cls.engine = v.Engine(v.DirSource(dev), Path(cls.tmp.name) / "work", history=hist)
        cls.engine.poll_once()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), v.make_handler(cls.engine))
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        con.request(method, path, json.dumps(body) if body is not None else None, h)
        r = con.getresponse()
        data = r.read()
        con.close()
        return r.status, json.loads(data) if data else None

    def test_query_returns_rows_tips_and_records_history(self):
        st, res = self.call("POST", "/api/query", {"sql": "SELECT gender, count(*) FROM docs__survey GROUP BY 1", "limit": 50})
        self.assertEqual(st, 200)
        self.assertEqual(sorted(r[0] for r in res["rows"]), ["F", "M"])
        self.assertIn("plan", res)
        st, hist = self.call("GET", "/api/history")
        self.assertIn("SELECT gender, count(*) FROM docs__survey GROUP BY 1", [q["sql"] for q in hist["recent"]])

    def test_query_errors_are_400_with_message(self):
        st, res = self.call("POST", "/api/query", {"sql": "DELETE FROM docs__survey"})
        self.assertEqual(st, 400)
        self.assertIn("read-only", res["error"])

    def test_report_meta_suggest_check(self):
        self.assertGreaterEqual(self.call("GET", "/api/dbinfo")[1]["counts"]["table"], 4)
        self.assertIn("indexes", self.call("GET", "/api/table_meta?table=docs__survey")[1])
        self.assertEqual(self.call("GET", "/api/table_meta?table=nope")[0], 404)
        st, sug = self.call("GET", "/api/suggest?table=docs__survey")
        self.assertEqual(st, 200)
        self.assertTrue(any(x["kind"] == "join" for x in sug["suggestions"]))
        self.assertIn("history", sug)
        self.assertTrue(self.call("GET", "/api/suggest")[1]["suggestions"])
        self.assertTrue(self.call("POST", "/api/check", {"kind": "quick"})[1]["ok"])

    def test_star_and_delete(self):
        self.call("POST", "/api/history/star", {"sql": "SELECT 42", "star": True, "name": "answer"})
        st, hist = self.call("GET", "/api/history")
        self.assertEqual(hist["starred"][0]["name"], "answer")
        self.call("POST", "/api/history/delete", {"sql": "SELECT 42"})
        self.assertFalse(self.call("GET", "/api/history")[1]["starred"])

    def test_foreign_host_and_non_json_posts_are_refused(self):
        self.assertEqual(self.call("GET", "/api/state", headers={"Host": "evil.example:80"})[0], 403)
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        con.request("POST", "/api/clear", "x", {"Content-Type": "text/plain"})
        r = con.getresponse()
        r.read()
        con.close()
        self.assertEqual(r.status, 415)

    def test_cached_report_is_dropped_on_new_snapshot(self):
        first = self.engine.db_report()
        self.assertIs(first, self.engine.db_report())
        before = first["counts"]["table"]
        con = sqlite3.connect(Path(self.tmp.name) / "device" / "app.db")
        con.execute("CREATE TABLE extra (a)")
        con.commit()
        con.close()
        time.sleep(0.01)
        self.assertTrue(self.engine.poll_once())
        self.assertEqual(self.engine.db_report()["counts"]["table"], before + 1)


if __name__ == "__main__":
    unittest.main()

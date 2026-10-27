"""Engine / HTTP / CLI tests — no device needed; a temp dir stands in for it.

    python3 -m unittest discover -s tests -v
"""

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flutter_db_viewer import viewer as v  # noqa: E402


class FakeDevice:
    """A WAL-mode database held open by a writer, like the running app."""

    def __init__(self, root: Path, name="app_dev.db"):
        self.dir = root / "device"
        self.dir.mkdir()
        self.name = name
        self.con = sqlite3.connect(self.dir / name, isolation_level=None)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA wal_autocheckpoint=0")  # keep changes in the -wal
        self.con.executescript(
            """
            CREATE TABLE households (
              mobile_uuid TEXT PRIMARY KEY, head_name TEXT, members INTEGER,
              sync_status TEXT NOT NULL DEFAULT 'dirty');
            CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID;
            CREATE TABLE empty_table (x TEXT);
            INSERT INTO households VALUES
              ('u1','Asha',4,'synced'), ('u2','Ravi',3,'synced');
            INSERT INTO kv VALUES ('a','1');
            """
        )

    def sql(self, q, *args):
        self.con.execute(q, args)

    def close(self):
        self.con.close()


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.dev = FakeDevice(root)
        self.engine = v.Engine(v.DirSource(self.dev.dir), root / "work", interval=0.05)
        self.assertTrue(self.engine.poll_once(), self.engine.status)

    def tearDown(self):
        self.dev.close()
        self.tmp.cleanup()

    def table(self, name):
        return next(t for t in self.engine.state()["tables"] if t["name"] == name)

    def test_initial_snapshot_reads_wal_contents(self):
        s = self.engine.state()
        self.assertEqual(s["db"], "app_dev.db")
        self.assertGreater(s["size"]["wal"], 0, "fixture should keep data in the WAL")
        self.assertEqual(self.table("households")["rows"], 2)
        self.assertEqual(self.table("kv")["rows"], 1)
        self.assertEqual(s["totals"], {"tables": 3, "non_empty": 2, "rows": 3, "columns": 7, "unsynced": 0})
        self.assertEqual(self.table("households")["sync"], {"synced": 2})
        self.assertEqual(s["events"], [], "first load is the baseline, not a change")

    def test_db_flag_matches_by_file_name(self):
        eng = v.Engine(v.DirSource(self.dev.dir), Path(self.tmp.name) / "w3", db="app_dev.db")
        self.assertTrue(eng.poll_once(), eng.status)
        self.assertEqual(eng.state()["db"], "app_dev.db")

    def test_unchanged_device_does_not_resnapshot(self):
        self.assertFalse(self.engine.poll_once())

    def test_insert_is_detected_and_marked(self):
        self.dev.sql("INSERT INTO households VALUES ('u3','Meena',5,'dirty')")
        self.assertTrue(self.engine.poll_once())
        s = self.engine.state()
        self.assertEqual(self.table("households")["rows"], 3)
        self.assertEqual(s["totals"]["unsynced"], 1)
        ev = s["events"][0]
        self.assertEqual((ev["table"], ev["inserted"], ev["updated"], ev["deleted"]), ("households", 1, 0, 0))
        self.assertEqual(s["changed_tables"]["households"]["inserted"], 1)

        rows = self.engine.rows("households", only_changed=True)
        self.assertEqual(rows["total"], 1)
        self.assertEqual(rows["rows"][0]["values"][:2], ["u3", "Meena"])
        self.assertEqual(rows["rows"][0]["mark"]["kind"], "inserted")

    def test_update_reports_changed_columns_and_old_values(self):
        self.dev.sql("UPDATE households SET members=6 WHERE mobile_uuid='u1'")
        self.engine.poll_once()
        ev = self.engine.state()["events"][0]
        self.assertEqual((ev["inserted"], ev["updated"]), (0, 1))
        row = self.engine.rows("households", only_changed=True)["rows"][0]
        self.assertEqual(row["mark"]["kind"], "updated")
        self.assertEqual(row["mark"]["cols"], ["members"])
        self.assertEqual(row["mark"]["old"], {"members": 4})

    def test_repeated_edits_accumulate_columns_and_keep_first_old_value(self):
        self.dev.sql("UPDATE households SET members=6 WHERE mobile_uuid='u1'")
        self.engine.poll_once()
        self.dev.sql("UPDATE households SET members=7, head_name='Asha D' WHERE mobile_uuid='u1'")
        self.engine.poll_once()
        mark = self.engine.rows("households", only_changed=True)["rows"][0]["mark"]
        self.assertEqual(mark["cols"], ["members", "head_name"])
        self.assertEqual(mark["old"], {"members": 4, "head_name": "Asha"})

    def test_insert_then_edit_stays_marked_as_new(self):
        self.dev.sql("INSERT INTO households VALUES ('u3','Meena',5,'dirty')")
        self.engine.poll_once()
        self.dev.sql("UPDATE households SET sync_status='synced' WHERE mobile_uuid='u3'")
        self.engine.poll_once()
        row = self.engine.rows("households", only_changed=True)["rows"][0]
        self.assertEqual(row["mark"]["kind"], "inserted")
        self.assertEqual(row["mark"]["cols"], ["sync_status"])

    def test_delete_and_without_rowid_tables(self):
        self.dev.sql("DELETE FROM households WHERE mobile_uuid='u2'")
        self.dev.sql("INSERT INTO kv VALUES ('b','2')")
        self.engine.poll_once()
        evs = {e["table"]: e for e in self.engine.state()["events"]}
        self.assertEqual(evs["households"]["deleted"], 1)
        self.assertEqual(evs["kv"]["count_delta"], 1)
        self.assertEqual(self.table("kv")["rows"], 2)

    def test_new_table_is_reported(self):
        self.dev.sql("CREATE TABLE growth (a TEXT)")
        self.dev.sql("INSERT INTO growth VALUES ('x')")
        self.engine.poll_once()
        ev = next(e for e in self.engine.state()["events"] if e["table"] == "growth")
        self.assertTrue(ev["created"])
        self.assertEqual(ev["inserted"], 1)

    def test_checkpoint_into_main_file_is_followed(self):
        self.dev.sql("INSERT INTO households VALUES ('u3','Meena',5,'dirty')")
        self.dev.sql("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertTrue(self.engine.poll_once())
        self.assertEqual(self.engine.state()["size"]["wal"], 0)
        self.assertEqual(self.table("households")["rows"], 3)

    def test_search_sort_and_paging(self):
        for i in range(5):
            self.dev.sql("INSERT INTO households VALUES (?,?,?,'synced')", f"p{i}", f"Name{i}", i)
        self.engine.poll_once()
        self.assertEqual(self.engine.rows("households", q="ravi")["total"], 1)
        page = self.engine.rows("households", page=1, size=3, sort="members", desc=False)
        self.assertEqual(page["total"], 7)
        self.assertEqual([r["values"][2] for r in page["rows"]], [3, 3, 4])
        newest = self.engine.rows("households", size=1)["rows"][0]
        self.assertEqual(newest["values"][0], "p4", "default order is newest rowid first")

    def test_unknown_table_and_injection_are_rejected(self):
        with self.assertRaises(KeyError):
            self.engine.rows('kv"; DROP TABLE kv; --')
        # A hostile sort column is ignored rather than interpolated.
        self.assertEqual(self.engine.rows("households", sort="1; DROP TABLE x")["total"], 2)

    def test_source_errors_surface_in_status(self):
        class Broken:
            label = "broken"

            def list_files(self):
                raise v.SourceError("no devices/emulators found")

        eng = v.Engine(Broken(), Path(self.tmp.name) / "w2")
        self.assertFalse(eng.poll_once())
        self.assertEqual(eng.state()["status"], {"state": "error", "message": "no devices/emulators found"})

    def test_mark_clearing(self):
        self.dev.sql("INSERT INTO households VALUES ('u3','Meena',5,'dirty')")
        self.engine.poll_once()
        self.engine.clear_marks()
        s = self.engine.state()
        self.assertEqual((s["events"], s["changed_tables"]), ([], {}))


class PickDbTest(unittest.TestCase):
    def test_prefers_apps_largest_db_counting_wal_and_skips_library_dbs(self):
        files = {
            "com.google.android.datatransport.events": v.FileStat(90_000_000, "1"),
            "google_app_measurement.db": v.FileStat(80_000_000, "1"),
            "app_dev.db": v.FileStat(100, "1"),
            "app_dev.db-wal": v.FileStat(50_000_000, "1"),
            "app_staging.db": v.FileStat(1_000_000, "1"),
        }
        self.assertEqual(v.pick_default_db(files), "app_dev.db")
        self.assertNotIn("app_dev.db-wal", v.candidate_dbs(files))

    def test_falls_back_to_library_db_when_app_has_none(self):
        self.assertEqual(v.pick_default_db({"google_app_measurement.db": v.FileStat(1, "1")}), "google_app_measurement.db")
        self.assertIsNone(v.pick_default_db({}))

    def test_paths_from_several_app_dirs(self):
        out = (
            "databases/com.google.android.datatransport.events|9000000|t\n"
            "databases/cache.db|4096|t\n"
            "app_flutter/app.sqlite|2000000|t\n"
            "app_flutter/app.sqlite-wal|10|t\n"
            "garbage line\n"
            "databases/evil;id.db|10|t\n"
        )
        files = v.parse_stat(out)
        self.assertEqual(len(files), 4, "unsafe names are dropped from the listing")
        self.assertEqual(v.pick_default_db(files), "app_flutter/app.sqlite")

    def test_remote_names_are_restricted_to_app_db_dirs(self):
        for ok in ("databases/a.db", "app_flutter/x.sqlite", "files/q.db"):
            self.assertTrue(v.SAFE_REMOTE.match(ok), ok)
        for bad in ("../etc/passwd", "cache/a.db", "databases/a b", "databases/x'y", "databases/sub/x.db",
                    "databases/x;id", "databases/$(id)", "databases/`id`", "databases/a|b", "databases/a&b"):
            self.assertFalse(v.SAFE_REMOTE.match(bad), bad)
        with self.assertRaises(v.SourceError):
            v.AdbSource("com.x").pull("databases/x'; rm -rf /'", Path("/dev/null"))

    def test_not_debuggable_error_is_surfaced(self):
        fake = mock.Mock(stdout=b"", stderr=b"run-as: package not debuggable: com.x", returncode=1)
        with mock.patch.object(v.subprocess, "run", return_value=fake):
            with self.assertRaisesRegex(v.SourceError, "not debuggable"):
                v.AdbSource("com.x").list_files()

    def test_no_files_gives_a_helpful_error(self):
        fake = mock.Mock(stdout=b"", stderr=b"", returncode=0)
        with mock.patch.object(v.subprocess, "run", return_value=fake):
            with self.assertRaisesRegex(v.SourceError, "No SQLite files found for com.x"):
                v.AdbSource("com.x").list_files()

    def test_adb_command_targets_serial_and_package(self):
        cmd = v.AdbSource("com.x", "abc")._cmd("ls")
        self.assertEqual(cmd, ["adb", "-s", "abc", "exec-out", "run-as com.x sh -c 'ls'"])


class DetectPackageTest(unittest.TestCase):
    def test_reads_application_id_from_kts_in_a_parent_dir(self):
        with tempfile.TemporaryDirectory() as d:
            app = Path(d) / "android" / "app"
            app.mkdir(parents=True)
            (app / "build.gradle.kts").write_text('android {\n  defaultConfig {\n    applicationId = "com.example.app"\n  }\n}')
            nested = Path(d) / "lib" / "src"
            nested.mkdir(parents=True)
            self.assertEqual(v.detect_package(nested), "com.example.app")

    def test_reads_groovy_gradle(self):
        with tempfile.TemporaryDirectory() as d:
            app = Path(d) / "android" / "app"
            app.mkdir(parents=True)
            (app / "build.gradle").write_text("defaultConfig {\n applicationId 'com.example.app'\n}")
            self.assertEqual(v.detect_package(Path(d)), "com.example.app")

    def test_none_outside_a_project(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(v.detect_package(Path(d)))


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.dev = FakeDevice(root)
        cls.engine = v.Engine(v.DirSource(cls.dev.dir), root / "work", interval=0.05)
        cls.engine.poll_once()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), v.make_handler(cls.engine))
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.dev.close()
        cls.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.status, r.read()

    def test_page_and_state(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Device DB Live", body)
        state = json.loads(self.get("/api/state")[1])
        self.assertEqual(state["totals"]["rows"], 3)

    def test_rows_endpoint(self):
        res = json.loads(self.get("/api/rows?table=households&q=asha")[1])
        self.assertEqual(res["total"], 1)
        self.assertEqual([c["name"] for c in res["columns"]], ["mobile_uuid", "head_name", "members", "sync_status"])

    def test_running_viewer_is_detected(self):
        state = v.running_viewer(self.server.server_address[1])
        self.assertEqual(state["db"], "app_dev.db")
        self.assertIsNone(v.running_viewer(1))

    def test_unknown_table_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/api/rows?table=nope")
        self.assertEqual(cm.exception.code, 404)
        cm.exception.close()

    def test_stream_pushes_a_version_on_change(self):
        v0 = self.engine.version
        got = {}

        def listen():
            with urllib.request.urlopen(f"{self.base}/api/stream?v={v0}", timeout=10) as r:
                got["line"] = r.readline().decode()

        t = threading.Thread(target=listen)
        t.start()
        self.dev.sql("INSERT INTO households VALUES ('s1','Stream',1,'dirty')")
        self.engine.poll_once()
        t.join(10)
        self.assertTrue(got.get("line", "").startswith("data: "))
        self.assertGreater(int(got["line"].split()[1]), v0)


if __name__ == "__main__":
    unittest.main()


class FakeAdb(v.AdbSource):
    """AdbSource whose remote `dd` / `md5sum` commands run against a local dir."""

    CHUNK = 1024

    def __init__(self, root: Path):
        super().__init__("com.example.app")
        self.root = root
        self.reads: list[int] = []
        self.fail_next = 0

    def _run(self, remote, timeout):
        import hashlib
        import re
        m = re.match(r"dd if=(\S+) bs=(\d+) skip=(\d+) count=1", remote)
        if m:
            if self.fail_next:
                self.fail_next -= 1
                raise v.SourceError("adb transfer stalled for 30 s")
            i = int(m.group(3))
            self.reads.append(i)
            data = (self.root / m.group(1)).read_bytes()
            return data[i * self.CHUNK:(i + 1) * self.CHUNK]
        m = re.match(r"i=0; while \[ \$i -lt (\d+) \]; do dd if=(\S+)", remote)
        if m:
            data = (self.root / m.group(2)).read_bytes()
            return "".join(hashlib.md5(data[i * self.CHUNK:(i + 1) * self.CHUNK]).hexdigest() + "  -\n"
                           for i in range(int(m.group(1)))).encode()
        raise AssertionError(remote)


class ChunkedPullTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "databases").mkdir()
        self.remote = self.root / "databases" / "app.db"
        self.remote.write_bytes(bytes(range(256)) * 40 + b"tail")  # 10 KB + 4 bytes → 11 chunks
        self.dest = self.root / "local.db"
        self.adb = FakeAdb(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def pull(self):
        return self.adb.pull("databases/app.db", self.dest, size=self.remote.stat().st_size)

    def test_full_then_incremental(self):
        self.assertEqual(self.pull(), self.remote.stat().st_size)
        self.assertEqual(self.dest.read_bytes(), self.remote.read_bytes())
        self.assertEqual(len(self.adb.reads), 11)

        data = bytearray(self.remote.read_bytes())
        data[5000] ^= 0xFF  # one byte in chunk 4
        data += b"grown"  # file grows into a new chunk
        self.remote.write_bytes(bytes(data))
        self.adb.reads.clear()
        moved = self.pull()
        self.assertEqual(self.dest.read_bytes(), bytes(data))
        self.assertEqual(sorted(self.adb.reads), [4, 10], "only the changed and the grown chunk")
        self.assertLess(moved, 2 * FakeAdb.CHUNK + 1)

    def test_shrunk_file_is_truncated(self):
        self.pull()
        self.remote.write_bytes(self.remote.read_bytes()[:3000])
        self.pull()
        self.assertEqual(self.dest.read_bytes(), self.remote.read_bytes())

    def test_stalled_chunk_is_retried(self):
        self.adb.fail_next = 2
        self.pull()
        self.assertEqual(self.dest.read_bytes(), self.remote.read_bytes())

    def test_persistent_failure_raises_and_leaves_no_partial_file(self):
        self.adb.fail_next = 99
        with self.assertRaisesRegex(v.SourceError, "chunk 1"):
            self.pull()
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_suffix(".db.part").exists())

    def test_progress_is_reported(self):
        seen = []
        self.adb.pull("databases/app.db", self.dest, size=self.remote.stat().st_size,
                      progress=lambda name, done, todo, total: seen.append((done, todo, total)))
        self.assertEqual(seen[-1], (11, 11, 11))

    def test_unsafe_names_are_refused(self):
        with self.assertRaises(v.SourceError):
            self.adb.pull("databases/x;rm -rf /", self.dest, size=1)

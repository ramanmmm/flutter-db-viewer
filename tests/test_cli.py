"""Demo app, running-viewer registry, port binding and CLI tests.

    python3 -m unittest discover -s tests -v
"""

import contextlib
import io
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from flutter_db_viewer import __version__  # noqa: E402
from flutter_db_viewer import viewer as v  # noqa: E402
from flutter_db_viewer.demo import DemoApp  # noqa: E402


class DemoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.demo = DemoApp(Path(self.tmp.name) / "dev")

    def tearDown(self):
        self.demo.stop()
        self.tmp.cleanup()

    def count(self, table):
        return self.demo.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_seed_data(self):
        self.assertEqual(self.count("products"), 30)
        self.assertEqual(self.count("customers"), 120)
        self.assertEqual(self.count("orders"), 300)
        self.assertGreaterEqual(self.count("order_items"), 300)
        self.assertEqual(self.count("audit_log"), 0)
        mode = self.demo.con.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "wal")

    def test_seed_is_deterministic(self):
        other = DemoApp(Path(self.tmp.name) / "other")
        try:
            q = "SELECT name, city FROM customers ORDER BY id LIMIT 5"
            self.assertEqual(self.demo.con.execute(q).fetchall(), other.con.execute(q).fetchall())
        finally:
            other.stop()

    def test_engine_sees_demo_writes(self):
        engine = v.Engine(v.DirSource(self.demo.dir), Path(self.tmp.name) / "work")
        self.assertTrue(engine.poll_once())
        self.assertEqual(engine.state()["db"], DemoApp.DB)
        for _ in range(12):
            self.demo.step()
        self.assertTrue(engine.poll_once())
        events = engine.state()["events"]
        self.assertTrue(events, "a dozen app actions should produce at least one change event")
        self.assertTrue(any(e["inserted"] or e["updated"] for e in events))

    def test_every_action_kind_runs(self):
        seen = {self.demo.step() for _ in range(80)}
        self.assertEqual(seen, {"new order", "status change", "sync", "queue cleanup"})


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_register_list_and_prune_stale(self):
        v.register(8765, {"package": "com.a", "source": "x"})
        stale = v.registry_dir() / "8766.json"
        stale.write_text('{"pid": 999999, "port": 8766, "package": "com.b"}')
        (v.registry_dir() / "junk.json").write_text("not json")
        alive = v.running_viewers()
        self.assertEqual([x["package"] for x in alive], ["com.a"])
        self.assertFalse(stale.exists())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            v.cmd_list()
        self.assertIn("http://localhost:8765  com.a", out.getvalue())

    def test_stop_only_targets_the_named_package(self):
        procs = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]) for _ in range(2)]
        try:
            for port, (proc, pkg) in enumerate(zip(procs, ["com.a", "com.b"]), start=9001):
                (v.registry_dir()).mkdir(parents=True, exist_ok=True)
                (v.registry_dir() / f"{port}.json").write_text(
                    f'{{"pid": {proc.pid}, "port": {port}, "package": "{pkg}"}}')
            with contextlib.redirect_stdout(io.StringIO()) as out:
                v.cmd_stop("com.a")
            self.assertIn("Stopped com.a", out.getvalue())
            self.assertIsNotNone(procs[0].wait(5))
            self.assertIsNone(procs[1].poll(), "com.b's viewer must keep running")
            with contextlib.redirect_stdout(io.StringIO()):
                v.cmd_stop(None)
            self.assertIsNotNone(procs[1].wait(5))
        finally:
            for p in procs:
                p.kill()
                p.wait()

    def test_stop_inside_a_project_targets_its_package(self):
        with tempfile.TemporaryDirectory() as d:
            app = Path(d) / "android" / "app"
            app.mkdir(parents=True)
            (app / "build.gradle.kts").write_text('applicationId = "com.mine"')
            with mock.patch.object(Path, "cwd", return_value=Path(d)), \
                    mock.patch.object(v, "cmd_stop", return_value=0) as stop:
                v.main(["stop"])
                stop.assert_called_with("com.mine")
                v.main(["stop", "--all"])
                stop.assert_called_with(None)

    def test_stop_with_nothing_running(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(v.main(["stop", "--package", "com.none"]), 0)
        self.assertIn("No flutterdb viewer running for com.none", out.getvalue())


class BindTest(unittest.TestCase):
    def test_skips_a_port_another_program_holds(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        try:
            with mock.patch.object(v, "running_viewer", return_value=None):
                server, existing = v.bind(port, "com.a", reuse=True)
            self.assertIsNone(existing)
            self.assertGreater(server.server_address[1], port)
            server.server_close()
        finally:
            blocker.close()

    def test_reuses_a_viewer_already_showing_the_app(self):
        states = {8765: {"source": "serial · com.other"}, 8766: {"source": "serial · com.a"}}
        with mock.patch.object(v, "running_viewer", side_effect=lambda p: states.get(p)):
            server, existing = v.bind(8765, "com.a", reuse=True)
        self.assertIsNone(server)
        self.assertEqual(existing, "http://localhost:8766")


class CliTest(unittest.TestCase):
    def test_once_against_a_directory(self):
        with tempfile.TemporaryDirectory() as d:
            con = sqlite3.connect(Path(d) / "notes.db")
            con.executescript("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT);"
                              "INSERT INTO notes(body) VALUES ('a'), ('b');")
            con.close()
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(v.main(["--dir", d, "--once"]), 0)
        self.assertIn("notes.db", out.getvalue())
        self.assertRegex(out.getvalue(), r"notes\s+2\s+2")

    def test_outside_a_project_explains_what_to_do(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(Path, "cwd", return_value=Path(d)):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                v.main(["--once"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--demo", err.getvalue())

    def test_module_entry_point_and_version(self):
        out = subprocess.run([sys.executable, "-m", "flutter_db_viewer", "--version"],
                             cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), f"flutterdb {__version__}")

    def test_demo_once(self):
        out = subprocess.run([sys.executable, "-m", "flutter_db_viewer", "--demo", "--once"],
                             cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("shop_app.db", out.stdout)
        self.assertRegex(out.stdout, r"orders\s+300")


if __name__ == "__main__":
    unittest.main()

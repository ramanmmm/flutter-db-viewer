"""Live, auto-refreshing browser view of an Android app's on-device SQLite DB.

    flutterdb                 # from a Flutter / Android project dir
    flutterdb --once          # print a table summary and exit
    flutterdb --package com.x # any debuggable app, from anywhere
    flutterdb --demo          # try it without a phone
    flutterdb list | stop     # running viewers

The package is read from android/app/build.gradle(.kts) in the current
directory (or a parent). An app's databases live in its private dir, readable
only through `adb run-as` on a debuggable build. Every --interval seconds the
viewer stats those files on the device; when anything moved it pulls the
changed files (the main file only after a checkpoint, otherwise just the
-wal), folds them into a local read-only snapshot, and diffs every row against
the previous snapshot so the page can highlight inserted / updated / deleted
rows as they happen.

Standard library only. Nothing is ever written to the device.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from . import sql as sqlmod

HERE = Path(__file__).resolve().parent

# Rows whose change marks stay highlighted in the UI.
MARK_TTL_SECONDS = 15 * 60
# Old values are kept for at most this many updated rows per table per sync.
MAX_UPDATE_DETAILS = 500
MAX_EVENTS = 300


# ── sources ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FileStat:
    size: int
    mtime: str


# Where apps keep SQLite files, relative to the app's data dir: Android's
# databases/ (sqflite, Room, SQLiteOpenHelper) and Flutter's documents dir
# app_flutter/ (drift, sqlite3 with path_provider), plus files/.
DB_DIRS = ("databases", "app_flutter", "files")
LIST_REMOTE = (
    'stat -c "%n|%s|%y" databases/* '
    + " ".join(f"{d}/*.db {d}/*.db-wal {d}/*.sqlite {d}/*.sqlite-wal {d}/*.sqlite3 {d}/*.sqlite3-wal"
               for d in DB_DIRS[1:])
    + " 2>/dev/null; true"
)
# Remote names are interpolated into a shell command run as the app, so only
# plain file-name characters are accepted (no quotes, `;`, `$`, spaces, …).
SAFE_REMOTE = re.compile(r"^(?:%s)/[A-Za-z0-9._+-]+$" % "|".join(DB_DIRS))


def parse_stat(out: str) -> dict[str, FileStat]:
    files = {}
    for line in out.splitlines():
        parts = line.rsplit("|", 2)
        if len(parts) == 3 and parts[1].isdigit() and SAFE_REMOTE.match(parts[0]):
            files[parts[0]] = FileStat(int(parts[1]), parts[2])
    return files


class AdbSource:
    """Reads the app's databases/ dir through `adb exec-out run-as`."""

    def __init__(self, package: str, serial: str | None = None):
        self.package = package
        self.serial = serial
        self._label: str | None = None
        self._procs: set[subprocess.Popen] = set()
        self._procs_lock = threading.Lock()

    @property
    def label(self) -> str:
        if self._label is None:
            self._label = f"{self.serial or self._default_serial()} · {self.package}"
        return self._label

    def close(self) -> None:
        """Kill in-flight adb calls so a stopped viewer leaves no pull running."""
        with self._procs_lock:
            for p in self._procs:
                p.kill()

    def _default_serial(self) -> str:
        try:
            out = subprocess.run(
                ["adb", "get-serialno"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
            return out or "device"
        except Exception:
            return "device"

    def _cmd(self, remote: str) -> list[str]:
        base = ["adb"] + (["-s", self.serial] if self.serial else [])
        return base + ["exec-out", f"run-as {self.package} sh -c '{remote}'"]

    def list_files(self) -> dict[str, FileStat]:
        proc = subprocess.run(self._cmd(LIST_REMOTE), capture_output=True, timeout=15)
        files = parse_stat(proc.stdout.decode(errors="replace"))
        if not files:
            err = proc.stderr.decode(errors="replace").strip()
            if "not debuggable" in err or "unknown package" in err:
                raise SourceError(err)
            raise SourceError(
                err or f"No SQLite files found for {self.package} — is the device connected "
                "and has the app created its database yet?"
            )
        return files

    # Files are read in chunks with `dd`, each its own short adb call: a USB
    # hiccup costs one 4 MB retry instead of restarting a 100 MB stream, and
    # with a local copy already present only chunks whose md5 differs on the
    # device are transferred (a checkpoint usually touches a few).
    CHUNK = 4 * 1024 * 1024
    CHUNK_TIMEOUT = 30
    RETRIES = 3

    def _run(self, remote: str, timeout: float) -> bytes:
        proc = subprocess.Popen(self._cmd(remote), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with self._procs_lock:
            self._procs.add(proc)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise SourceError(f"adb transfer stalled for {timeout:.0f} s") from None
        finally:
            with self._procs_lock:
                self._procs.discard(proc)
        if proc.returncode != 0:
            raise SourceError(err.decode(errors="replace").strip() or f"adb exited with {proc.returncode}")
        return out

    def chunk_hashes(self, name: str, size: int) -> list[str]:
        n = -(-size // self.CHUNK)
        out = self._run(
            f"i=0; while [ $i -lt {n} ]; do dd if={name} bs={self.CHUNK} skip=$i count=1 2>/dev/null | md5sum; "
            "i=$((i+1)); done",
            timeout=120,
        ).decode(errors="replace")
        hashes = [line.split()[0] for line in out.splitlines() if line.strip()]
        if len(hashes) != n:
            raise SourceError("couldn't checksum the file on the device")
        return hashes

    def read_chunk(self, name: str, index: int, expected: int) -> bytes:
        last: Exception | None = None
        for _ in range(self.RETRIES):
            try:
                data = self._run(f"dd if={name} bs={self.CHUNK} skip={index} count=1 2>/dev/null", self.CHUNK_TIMEOUT)
            except SourceError as e:
                last = e
                continue
            if len(data) >= expected:
                return data[:expected]
            last = SourceError(f"short read ({len(data)} of {expected} bytes)")
        raise SourceError(f"couldn't read {name} (chunk {index + 1}): {last}")

    def pull(self, name: str, dest: Path, size: int | None = None, progress=None) -> int:
        """Copy `name` to `dest`; returns the number of bytes transferred."""
        if not SAFE_REMOTE.match(name):
            raise SourceError(f"refusing odd file name {name!r}")
        if size is None:
            st = self.list_files().get(name)
            if st is None:
                raise SourceError(f"{name} is gone from the device")
            size = st.size
        n = -(-size // self.CHUNK)
        todo = list(range(n))
        tmp = dest.with_suffix(dest.suffix + ".part")
        reuse = dest.exists() and n >= 3
        if reuse:
            try:
                remote = self.chunk_hashes(name, size)
                local = file_chunk_hashes(dest, self.CHUNK)
                todo = [i for i in range(n) if i >= len(local) or local[i] != remote[i]]
                clone_or_copy(dest, tmp)
            except SourceError:
                reuse, todo = False, list(range(n))
        moved = 0
        try:
            with open(tmp, "r+b" if reuse else "wb") as fh:
                for done, i in enumerate(todo, 1):
                    data = self.read_chunk(name, i, min(self.CHUNK, size - i * self.CHUNK))
                    fh.seek(i * self.CHUNK)
                    fh.write(data)
                    moved += len(data)
                    if progress:
                        progress(name, done, len(todo), n)
                fh.truncate(size)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, dest)
        return moved


def file_chunk_hashes(path: Path, chunk: int) -> list[str]:
    out = []
    with open(path, "rb") as fh:
        while True:
            data = fh.read(chunk)
            if not data:
                return out
            out.append(hashlib.md5(data, usedforsecurity=False).hexdigest())


class DirSource:
    """A local directory standing in for the device (tests, or a pulled copy)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.label = f"local · {self.path}"

    def list_files(self) -> dict[str, FileStat]:
        files = {}
        for p in self.path.iterdir():
            if p.is_file():
                st = p.stat()
                files[p.name] = FileStat(st.st_size, str(st.st_mtime_ns))
        return files

    def pull(self, name: str, dest: Path, size: int | None = None, progress=None) -> int:
        shutil.copyfile(self.path / name, dest)
        return dest.stat().st_size


class SourceError(Exception):
    pass


def candidate_dbs(files: dict[str, FileStat]) -> list[str]:
    """Main database files (no -wal/-shm/-journal companions), largest first."""
    suffixes = ("-wal", "-shm", "-journal")
    names = [n for n in files if not n.endswith(suffixes) and not n.endswith(".part")]
    return sorted(names, key=lambda n: (-files[n].size, n))


# Databases Google Play services / Firebase drop into every app's dir.
LIBRARY_DBS = ("com.google.", "google_app_measurement", "firebase", "androidx.work", "crashlytics")


def pick_default_db(files: dict[str, FileStat]) -> str | None:
    """The app's own database: the largest (main + -wal) that isn't a library's."""
    def total(n):
        return files[n].size + getattr(files.get(n + "-wal"), "size", 0)

    dbs = sorted(candidate_dbs(files), key=lambda n: (-total(n), n))
    own = [n for n in dbs if not n.rsplit("/", 1)[-1].lower().startswith(LIBRARY_DBS)]
    return (own or dbs or [None])[0]


# ── snapshot analysis ─────────────────────────────────────────────────────


def qi(name: str) -> str:
    """Quote an SQLite identifier."""
    return '"' + name.replace('"', '""') + '"'


def row_hash(values: tuple) -> int:
    return int.from_bytes(
        hashlib.blake2b(repr(values).encode(), digest_size=8).digest(), "little"
    )


@dataclass
class TableInfo:
    name: str
    sql: str
    columns: list[dict]
    rows: int
    has_rowid: bool
    sync: dict[str, int] | None
    hashes: dict[int, int] = field(default_factory=dict, repr=False)

    def public(self) -> dict:
        return {
            "name": self.name,
            "rows": self.rows,
            "columns": self.columns,
            "has_rowid": self.has_rowid,
            "sync": self.sync,
        }


def analyze(db_path: Path) -> dict[str, TableInfo]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = {}
        for name, sql in con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall():
            cols = [
                {"name": c[1], "type": c[2] or "", "notnull": bool(c[3]), "pk": c[5]}
                for c in con.execute(f"PRAGMA table_info({qi(name)})")
            ]
            has_rowid = "WITHOUT ROWID" not in (sql or "").upper()
            hashes: dict[int, int] = {}
            if has_rowid:
                for row in con.execute(f"SELECT rowid, * FROM {qi(name)}"):
                    hashes[row[0]] = row_hash(row[1:])
                rows = len(hashes)
            else:
                rows = con.execute(f"SELECT count(*) FROM {qi(name)}").fetchone()[0]
            sync = None
            if any(c["name"] == "sync_status" for c in cols):
                sync = dict(
                    con.execute(
                        f"SELECT coalesce(sync_status,'null'), count(*) FROM {qi(name)} "
                        "GROUP BY 1 ORDER BY 2 DESC"
                    ).fetchall()
                )
            tables[name] = TableInfo(name, sql or "", cols, rows, has_rowid, sync, hashes)
        return tables
    finally:
        con.close()


def fetch_rows(db_path: Path, table: str, rowids: list[int]) -> dict[int, dict]:
    if not rowids:
        return {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out = {}
        for i in range(0, len(rowids), 500):
            chunk = rowids[i : i + 500]
            cur = con.execute(
                f"SELECT rowid, * FROM {qi(table)} WHERE rowid IN "
                f"({','.join('?' * len(chunk))})",
                chunk,
            )
            names = [d[0] for d in cur.description][1:]
            for row in cur:
                out[row[0]] = dict(zip(names, (jsonable(v) for v in row[1:])))
        return out
    finally:
        con.close()


def jsonable(v):
    if isinstance(v, bytes):
        return f"<blob {len(v)} bytes>"
    return v


def diff_tables(
    old: dict[str, TableInfo], new: dict[str, TableInfo]
) -> dict[str, dict[str, list[int]]]:
    """Per table: rowids inserted / updated / deleted between two snapshots."""
    out = {}
    for name, t in new.items():
        prev = old.get(name)
        if prev is None:
            out[name] = {"created": True, "inserted": sorted(t.hashes), "updated": [], "deleted": []}
            continue
        if not t.has_rowid:
            if t.rows != prev.rows:
                out[name] = {"inserted": [], "updated": [], "deleted": [], "count_delta": t.rows - prev.rows}
            continue
        a, b = prev.hashes, t.hashes
        ins = [r for r in b if r not in a]
        dele = [r for r in a if r not in b]
        upd = [r for r, h in b.items() if r in a and a[r] != h]
        if ins or dele or upd:
            out[name] = {"inserted": sorted(ins), "updated": sorted(upd), "deleted": sorted(dele)}
    for name in old:
        if name not in new:
            out[name] = {"dropped": True, "inserted": [], "updated": [], "deleted": sorted(old[name].hashes)}
    return out


# ── engine ─────────────────────────────────────────────────────────────────


class Engine:
    """Polls the source, keeps the current snapshot, and records changes."""

    def __init__(self, source, workdir: Path, db: str | None = None, interval: float = 2.0,
                 history: sqlmod.QueryHistory | None = None):
        self.source = source
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.interval = interval
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.wake = threading.Event()
        self.db = db
        self.version = 0
        self.status = {"state": "starting", "message": "Connecting to device…"}
        self.files: dict[str, FileStat] = {}
        self.snapshot: Path | None = None
        self.tables: dict[str, TableInfo] = {}
        self.events: list[dict] = []
        self.marks: dict[str, dict[int, dict]] = {}
        self.last_sync: float | None = None
        self.last_change: float | None = None
        self.sync_ms: int | None = None
        self.history = history
        self.pull_bytes = 0
        self.device_journal: str | None = None
        # Per-snapshot results of the slower reports (PRAGMA report, suggestions).
        self._cache: dict = {}
        self._seen: dict[str, FileStat | None] = {}
        self._seq = 0
        self._stop = False

    # public ---------------------------------------------------------------

    def select_db(self, name: str) -> None:
        with self.lock:
            if name == self.db:
                return
            self.db = name
            self._seen = {}
            self.tables = {}
            self.events = []
            self.marks = {}
            self._cache = {}
            self.snapshot = None
            self.last_change = None
            self._bump({"state": "loading", "message": f"Loading {name}…"})
        self.wake.set()

    def clear_marks(self) -> None:
        with self.lock:
            self.marks = {}
            self.events = []
            self._bump()

    def run_forever(self) -> None:
        while not self._stop:
            self.poll_once()
            self.wake.wait(self.interval)
            self.wake.clear()

    def stop(self) -> None:
        self._stop = True
        self.wake.set()
        if hasattr(self.source, "close"):
            self.source.close()

    def poll_once(self) -> bool:
        """One poll. Returns True if a new snapshot was installed."""
        try:
            return self._poll()
        except (SourceError, sqlite3.DatabaseError, subprocess.TimeoutExpired, OSError) as e:
            with self.lock:
                self._bump({"state": "error", "message": str(e)[:400]})
            return False

    # internals ------------------------------------------------------------

    def _bump(self, status: dict | None = None) -> None:
        if status is not None:
            self.status = status
        self.version += 1
        self.changed.notify_all()

    def _poll(self) -> bool:
        files = self.source.list_files()
        with self.lock:
            self.files = files
            if self.db and self.db not in files:
                # --db app.db matches databases/app.db or app_flutter/app.db
                match = [n for n in candidate_dbs(files) if n.rsplit("/", 1)[-1] == self.db]
                if match:
                    self.db = match[0]
            if not self.db or self.db not in files:
                picked = pick_default_db(files)
                if picked is None:
                    raise SourceError("No databases found in the app's data dir yet.")
                if self.db and self.db != picked:
                    self.select_db(picked)
                self.db = picked
            db = self.db
        main, wal = files.get(db), files.get(db + "-wal")
        if self.snapshot and self._seen.get("main") == main and self._seen.get("wal") == wal:
            with self.lock:
                self.last_sync = time.time()
                if self.status.get("state") != "live":
                    self._bump({"state": "live", "message": "Live"})
            return False

        started = time.time()
        with self.lock:
            self._bump({"state": "pulling", "message": "Pulling changes from device…"})
        base = self.workdir / "base.db"
        base_wal = self.workdir / "base.db-wal"
        last_note = [0.0]

        def progress(name, done, todo, total):
            if time.time() - last_note[0] < 0.5 and done != todo:
                return
            last_note[0] = time.time()
            chunk = getattr(self.source, "CHUNK", 0)
            what = f"{done} of {todo} changed chunks" if todo < total else f"{done * chunk / 1048576:.0f} / {total * chunk / 1048576:.0f} MB"
            with self.lock:
                self._bump({"state": "pulling", "message": f"Pulling {name.rsplit('/', 1)[-1]} · {what}"})

        moved = 0
        if self._seen.get("main") != main or not base.exists():
            moved += self.source.pull(db, base, size=main.size, progress=progress) or 0
        if wal and wal.size > 0:
            moved += self.source.pull(db + "-wal", base_wal, size=wal.size, progress=progress) or 0
        else:
            base_wal.unlink(missing_ok=True)
        # A checkpoint during the pull rewrites the main file under us; the
        # WAL we hold may then belong to the new main file. Retry next tick.
        after = self.source.list_files().get(db)
        if after != main:
            self._seen = {}
            raise SourceError("Database checkpointed mid-pull, retrying…")

        with open(base, "rb") as fh:
            header = fh.read(20)
        journal = "wal" if header[18:19] == b"\x02" else "rollback (delete/truncate/persist)"
        snap = self._build_snapshot(base, base_wal)
        tables = analyze(snap)
        with self.lock:
            prev_snap, prev_tables = self.snapshot, self.tables
        changes = diff_tables(prev_tables, tables) if prev_tables else {}
        details = self._update_details(prev_snap, snap, changes) if changes else {}

        with self.lock:
            if db != self.db:  # switched while we were pulling
                return False
            now = time.time()
            self.snapshot, self.tables = snap, tables
            self.device_journal = journal
            self._cache = {}
            self._seen = {"main": main, "wal": wal}
            self._record(changes, details, now)
            self.last_sync = now
            self.sync_ms = int((now - started) * 1000)
            self.pull_bytes = moved
            self._bump({"state": "live", "message": "Live"})
        if prev_snap and prev_snap != snap:
            prev_snap.unlink(missing_ok=True)
        return True

    def _build_snapshot(self, base: Path, base_wal: Path) -> Path:
        self._seq += 1
        snap = self.workdir / f"snap-{self._seq}.db"
        for p in (snap, Path(str(snap) + "-wal"), Path(str(snap) + "-shm")):
            p.unlink(missing_ok=True)
        clone_or_copy(base, snap)
        if base_wal.exists():
            shutil.copyfile(base_wal, str(snap) + "-wal")
        con = sqlite3.connect(snap)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.execute("PRAGMA journal_mode=DELETE")
        finally:
            con.close()
        for p in (Path(str(snap) + "-wal"), Path(str(snap) + "-shm")):
            p.unlink(missing_ok=True)
        return snap

    def _update_details(self, prev_snap, snap, changes) -> dict:
        """Old/new values for updated rows so the UI can show which cells moved."""
        details = {}
        if not prev_snap or not prev_snap.exists():
            return details
        for table, ch in changes.items():
            ids = ch.get("updated", [])[:MAX_UPDATE_DETAILS]
            if not ids:
                continue
            old = fetch_rows(prev_snap, table, ids)
            new = fetch_rows(snap, table, ids)
            per = {}
            for rid in ids:
                o, n = old.get(rid, {}), new.get(rid, {})
                cols = [c for c in n if o.get(c) != n.get(c)]
                per[rid] = {"cols": cols, "old": {c: o.get(c) for c in cols}}
            details[table] = per
        return details

    def _record(self, changes, details, now) -> None:
        cutoff = now - MARK_TTL_SECONDS
        for table in list(self.marks):
            self.marks[table] = {r: m for r, m in self.marks[table].items() if m["ts"] >= cutoff}
            if not self.marks[table]:
                del self.marks[table]
        for table, ch in sorted(changes.items()):
            marks = self.marks.setdefault(table, {})
            for rid in ch["inserted"]:
                marks[rid] = {"kind": "inserted", "ts": now}
            for rid in ch["updated"]:
                d = details.get(table, {}).get(rid, {})
                prior = marks.get(rid) or {}
                kind = "inserted" if prior.get("kind") == "inserted" else "updated"
                # Accumulate across edits: every column touched since the row was
                # first marked, each with the value it had before the first edit.
                cols = list(prior.get("cols", []))
                cols += [c for c in d.get("cols", []) if c not in cols]
                old = {**d.get("old", {}), **prior.get("old", {})}
                marks[rid] = {"kind": kind, "ts": now, "cols": cols, "old": old}
            for rid in ch["deleted"]:
                marks.pop(rid, None)
            if not marks:
                self.marks.pop(table, None)
            self.events.insert(
                0,
                {
                    "ts": now,
                    "table": table,
                    "inserted": len(ch["inserted"]),
                    "updated": len(ch["updated"]),
                    "deleted": len(ch["deleted"]),
                    "created": ch.get("created", False),
                    "dropped": ch.get("dropped", False),
                    "count_delta": ch.get("count_delta"),
                    "sample": (ch["inserted"] or ch["updated"])[:5],
                },
            )
        del self.events[MAX_EVENTS:]
        if changes:
            self.last_change = now

    # read API -------------------------------------------------------------

    def state(self) -> dict:
        with self.lock:
            db = self.db
            main = self.files.get(db) if db else None
            wal = self.files.get(db + "-wal") if db else None
            tables = [t.public() for t in self.tables.values()]
            dirty = sum(
                n for t in self.tables.values() if t.sync for s, n in t.sync.items() if s != "synced"
            )
            return {
                "version": self.version,
                "status": self.status,
                "source": self.source.label,
                "db": db,
                "dbs": [
                    {"name": n, "size": self.files[n].size + getattr(self.files.get(n + "-wal"), "size", 0)}
                    for n in candidate_dbs(self.files)
                ],
                "size": {"main": main.size if main else 0, "wal": wal.size if wal else 0},
                "last_sync": self.last_sync,
                "last_change": self.last_change,
                "sync_ms": self.sync_ms,
                "pull_bytes": self.pull_bytes,
                "interval": self.interval,
                "tables": tables,
                "totals": {
                    "tables": len(tables),
                    "non_empty": sum(1 for t in tables if t["rows"]),
                    "rows": sum(t["rows"] for t in tables),
                    "columns": sum(len(t["columns"]) for t in tables),
                    "unsynced": dirty,
                },
                "events": self.events[:100],
                "changed_tables": {
                    t: {
                        "inserted": sum(1 for m in ms.values() if m["kind"] == "inserted"),
                        "updated": sum(1 for m in ms.values() if m["kind"] == "updated"),
                        "last": max(m["ts"] for m in ms.values()),
                    }
                    for t, ms in self.marks.items()
                },
            }

    def rows(self, table, page=0, size=100, q="", only_changed=False, sort=None, desc=True) -> dict:
        with self.lock:
            snap = self.snapshot
            info = self.tables.get(table)
            marks = dict(self.marks.get(table, {}))
        if not snap or not info:
            raise KeyError(table)
        cols = [c["name"] for c in info.columns]
        where, args = [], []
        if q:
            like = f"%{q}%"
            where.append("(" + " OR ".join(f"CAST({qi(c)} AS TEXT) LIKE ?" for c in cols) + ")")
            args += [like] * len(cols)
        if only_changed:
            if not info.has_rowid or not marks:
                return {"columns": info.columns, "rows": [], "total": 0, "page": 0, "size": size, "has_rowid": info.has_rowid}
            ids = list(marks)
            where.append(f"rowid IN ({','.join('?' * len(ids))})")
            args += ids
        order_col = sort if sort in cols else None
        if order_col:
            order = f"ORDER BY {qi(order_col)} {'DESC' if desc else 'ASC'}"
        elif info.has_rowid:
            order = f"ORDER BY rowid {'DESC' if desc else 'ASC'}"
        else:
            order = ""
        wsql = ("WHERE " + " AND ".join(where)) if where else ""
        sel = "rowid AS __rowid, *" if info.has_rowid else "NULL AS __rowid, *"
        con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        try:
            total = con.execute(f"SELECT count(*) FROM {qi(table)} {wsql}", args).fetchone()[0]
            cur = con.execute(
                f"SELECT {sel} FROM {qi(table)} {wsql} {order} LIMIT ? OFFSET ?",
                args + [size, page * size],
            )
            out = []
            for r in cur:
                rid = r[0]
                out.append({"rowid": rid, "values": [jsonable(v) for v in r[1:]], "mark": marks.get(rid)})
        finally:
            con.close()
        return {"columns": info.columns, "rows": out, "total": total, "page": page, "size": size,
                "has_rowid": info.has_rowid, "sql": info.sql}

    # SQL console / database report -------------------------------------------

    def _current(self):
        with self.lock:
            if not self.snapshot:
                raise sqlmod.QueryError("The database is still loading from the device.")
            return self.snapshot, dict(self.tables)

    def _cached(self, key, build):
        with self.lock:
            snap = self.snapshot
            if key in self._cache:
                return self._cache[key]
        value = build()
        with self.lock:
            if self.snapshot == snap:
                self._cache[key] = value
        return value

    def query(self, sql: str, limit: int = sqlmod.DEFAULT_ROW_LIMIT) -> dict:
        snap, tables = self._current()
        res = sqlmod.run_query(snap, sql, limit=limit)
        res.update(sqlmod.explain(snap, sql, {n: t.rows for n, t in tables.items()}))
        if self.history:
            self.history.record(sql, len(res["rows"]), res["ms"])
        return res

    def explain(self, sql: str) -> dict:
        snap, tables = self._current()
        return sqlmod.explain(snap, sql, {n: t.rows for n, t in tables.items()})

    def db_report(self) -> dict:
        snap, _ = self._current()
        with self.lock:
            device = {
                "journal_mode": self.device_journal,
                "main": getattr(self.files.get(self.db), "size", 0),
                "wal": getattr(self.files.get(f"{self.db}-wal"), "size", 0),
            }
        return self._cached("report", lambda: sqlmod.db_report(snap, device))

    def check(self, kind: str) -> dict:
        snap, _ = self._current()
        return sqlmod.run_check(snap, kind)

    def table_meta(self, table: str) -> dict:
        snap, tables = self._current()
        if table not in tables:
            raise KeyError(table)
        return self._cached(("meta", table), lambda: sqlmod.table_meta(snap, table))

    def suggestions(self, table: str | None) -> dict:
        snap, tables = self._current()
        if table and table not in tables:
            raise KeyError(table)
        with self.lock:
            changed = sorted(self.marks, key=lambda t: -max(m["ts"] for m in self.marks[t].values()))

        def build():
            sg = sqlmod.Suggester(snap, tables)
            try:
                return sg.for_table(table) if table else sg.global_suggestions(changed)
            finally:
                sg.close()

        out = {"table": table, "suggestions": self._cached(("suggest", table, tuple(changed[:3])), build)}
        out["history"] = self.history.listing() if self.history else {"starred": [], "frequent": [], "recent": []}
        return out

    def wait_for_change(self, version: int, timeout: float) -> int:
        with self.lock:
            if self.version == version:
                self.changed.wait(timeout)
            return self.version


def clone_or_copy(src: Path, dst: Path) -> None:
    """APFS clone when available (instant for a 100 MB file), else a copy."""
    if sys.platform == "darwin":
        if subprocess.run(["cp", "-c", str(src), str(dst)], capture_output=True).returncode == 0:
            return
    shutil.copyfile(src, dst)


# ── HTTP ───────────────────────────────────────────────────────────────────


def make_handler(engine: Engine):
    page = (HERE / "index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _json(self, obj, status=200):
            body = json.dumps(obj, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _guard(self) -> bool:
            """Only answer pages served from this machine (blocks DNS rebinding)."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if host not in ("localhost", "127.0.0.1", "::1"):
                self._json({"error": "forbidden host"}, 403)
                return False
            return True

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                raise ValueError("request too large")
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                raise ValueError("body must be JSON") from None
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            return body

        def do_GET(self):
            if not self._guard():
                return
            url = urllib.parse.urlparse(self.path)
            qs = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
            try:
                if url.path == "/":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(page)))
                    self.end_headers()
                    self.wfile.write(page)
                elif url.path == "/api/state":
                    self._json(engine.state())
                elif url.path == "/api/rows":
                    res = engine.rows(
                        qs.get("table", ""),
                        page=max(0, int(qs.get("page", 0))),
                        size=min(1000, max(1, int(qs.get("size", 100)))),
                        q=qs.get("q", ""),
                        only_changed=qs.get("changed") == "1",
                        sort=qs.get("sort") or None,
                        desc=qs.get("dir", "desc") != "asc",
                    )
                    self._json(res)
                elif url.path == "/api/dbinfo":
                    self._json(engine.db_report())
                elif url.path == "/api/table_meta":
                    self._json(engine.table_meta(qs.get("table", "")))
                elif url.path == "/api/suggest":
                    self._json(engine.suggestions(qs.get("table") or None))
                elif url.path == "/api/history":
                    self._json(engine.history.listing() if engine.history else {})
                elif url.path == "/api/stream":
                    self._stream(int(qs.get("v", -1)))
                else:
                    self._json({"error": "not found"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except KeyError:
                self._json({"error": "unknown table"}, 404)
            except sqlmod.QueryError as e:
                self._json({"error": str(e)}, 409)
            except ValueError as e:
                self._json({"error": str(e)}, 400)

        def do_POST(self):
            if not self._guard():
                return
            # A JSON content type forces a CORS preflight, which this server never
            # answers — so other websites can't drive these endpoints.
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._json({"error": "send application/json"}, 415)
            url = urllib.parse.urlparse(self.path)
            qs = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
            try:
                body = self._body()
                if url.path == "/api/select" and (qs.get("db") or body.get("db")):
                    engine.select_db(qs.get("db") or body["db"])
                    self._json({"ok": True})
                elif url.path == "/api/clear":
                    engine.clear_marks()
                    self._json({"ok": True})
                elif url.path == "/api/refresh":
                    engine.wake.set()
                    self._json({"ok": True})
                elif url.path == "/api/query":
                    limit = min(10_000, max(1, int(body.get("limit") or sqlmod.DEFAULT_ROW_LIMIT)))
                    self._json(engine.query(str(body.get("sql", "")), limit=limit))
                elif url.path == "/api/explain":
                    self._json(engine.explain(str(body.get("sql", ""))))
                elif url.path == "/api/check":
                    self._json(engine.check(str(body.get("kind", ""))))
                elif url.path == "/api/history/star" and engine.history:
                    self._json(engine.history.star(str(body.get("sql", "")), bool(body.get("star", True)),
                                                   str(body.get("name", ""))[:120]))
                elif url.path == "/api/history/delete" and engine.history:
                    engine.history.delete(str(body.get("sql", "")))
                    self._json({"ok": True})
                else:
                    self._json({"error": "not found"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except sqlmod.QueryError as e:
                self._json({"error": str(e)}, 400)
            except ValueError as e:
                self._json({"error": str(e)}, 400)

        def _stream(self, version):
            """Server-sent events: one message per engine version bump."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.close_connection = True
            while True:
                version = engine.wait_for_change(version, 15)
                self.wfile.write(f"data: {version}\n\n".encode())
                self.wfile.flush()

    return Handler


def detect_package(start: Path) -> str | None:
    """applicationId from the nearest android/app/build.gradle(.kts)."""
    for d in [start, *start.parents]:
        for f in ("build.gradle.kts", "build.gradle"):
            gradle = d / "android" / "app" / f
            if gradle.is_file():
                m = re.search(r"""applicationId\s*=?\s*["']([\w.]+)["']""", gradle.read_text(errors="replace"))
                if m:
                    return m.group(1)
    return None


def running_viewer(port: int) -> dict | None:
    """State of a viewer already serving on this port, if any."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


def open_browser(url: str) -> None:
    subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", url], capture_output=True)


# ── CLI ────────────────────────────────────────────────────────────────────


def print_summary(engine: Engine) -> None:
    s = engine.state()
    print(f"{s['source']}  ·  {s['db']}")
    print(f"size {fmt_bytes(s['size']['main'])} + wal {fmt_bytes(s['size']['wal'])}  ·  "
          f"{s['totals']['tables']} tables ({s['totals']['non_empty']} with data)  ·  "
          f"{s['totals']['rows']:,} rows  ·  {s['totals']['unsynced']:,} unsynced\n")
    w = max(len(t["name"]) for t in s["tables"])
    print(f"{'table'.ljust(w)}  {'rows':>8}  {'cols':>4}  sync")
    for t in sorted(s["tables"], key=lambda t: -t["rows"]):
        sync = ", ".join(f"{k}:{v}" for k, v in (t["sync"] or {}).items())
        print(f"{t['name'].ljust(w)}  {t['rows']:>8,}  {len(t['columns']):>4}  {sync}")


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# ── running-viewer registry (for `list` / `stop`) ───────────────────────────


def registry_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "flutterdb" / "running"


def register(port: int, info: dict) -> Path:
    d = registry_dir()
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{port}.json"
    f.write_text(json.dumps({**info, "pid": os.getpid(), "port": port, "started": time.time()}))
    return f


def running_viewers() -> list[dict]:
    """Viewers started by this user that are still alive (stale entries pruned)."""
    out = []
    d = registry_dir()
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            info = json.loads(f.read_text())
            os.kill(info["pid"], 0)
            out.append(info)
        except (ValueError, KeyError, ProcessLookupError, OSError):
            f.unlink(missing_ok=True)
    return out


def cmd_list() -> int:
    viewers = running_viewers()
    if not viewers:
        print("No flutterdb viewers running.")
    for v in viewers:
        print(f"http://localhost:{v['port']}  {v['package']}  (pid {v['pid']})")
    return 0


def cmd_stop(package: str | None) -> int:
    targets = [v for v in running_viewers() if package in (None, v["package"])]
    if not targets:
        print(f"No flutterdb viewer running{f' for {package}' if package else ''}.")
        return 0
    for v in targets:
        try:
            os.kill(v["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + 5
    while time.time() < deadline and any(_alive(v["pid"]) for v in targets):
        time.sleep(0.1)
    for v in targets:
        print(f"Stopped {v['package']} on http://localhost:{v['port']}")
    return 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


# ── main ───────────────────────────────────────────────────────────────────


def bind(first_port: int, package: str, reuse: bool):
    """(server, None) on a free port, or (None, url) of a viewer already showing this app."""
    for port in range(first_port, first_port + 20):
        other = running_viewer(port)
        if other is not None:
            if reuse and other.get("source", "").endswith(f"· {package}"):
                return None, f"http://localhost:{port}"
            continue
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), None), None
        except OSError:
            continue
    raise SystemExit(f"error: no free port in {first_port}–{first_port + 19}")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["list"]:
        return cmd_list()
    if argv[:1] == ["stop"]:
        sp = argparse.ArgumentParser(
            prog="flutterdb stop",
            description="Stop running viewers: this project's app when run inside one, otherwise all.",
        )
        sp.add_argument("--package", help="only the viewer for this app")
        sp.add_argument("--all", action="store_true", help="every running viewer")
        sa = sp.parse_args(argv[1:])
        return cmd_stop(None if sa.all else sa.package or detect_package(Path.cwd()))

    ap = argparse.ArgumentParser(prog="flutterdb", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--package", help="Android applicationId (default: detected from android/app/build.gradle)")
    ap.add_argument("--db", help="database file name, e.g. app.db (default: the app's largest database)")
    ap.add_argument("-s", "--serial", help="adb device serial when several are attached")
    ap.add_argument("--dir", help="read a local directory of .db files instead of a device")
    ap.add_argument("--demo", action="store_true", help="run against a built-in demo app that writes rows live")
    ap.add_argument("--port", type=int, default=8765, help="first port to try (default: %(default)s)")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between device checks (default: %(default)s)")
    ap.add_argument("--once", action="store_true", help="print a summary to the terminal and exit")
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    ap.add_argument("--version", action="version", version=f"flutterdb {__version__}")
    args = ap.parse_args(argv)

    demo = None
    if args.demo:
        from .demo import DemoApp

        demo = DemoApp(Path(tempfile.mkdtemp(prefix="flutterdb-demo-")))
        demo.start()
        source = DirSource(demo.dir)
        source.label = f"demo · {DemoApp.PACKAGE}"
        package = DemoApp.PACKAGE
    elif args.dir:
        source = DirSource(Path(args.dir))
        package = f"dir:{Path(args.dir).resolve()}"
    else:
        package = args.package or detect_package(Path.cwd())
        if not package:
            ap.error("not inside a Flutter/Android project — run it from the project dir, pass --package, or try --demo")
        source = AdbSource(package, args.serial)

    # `kill` / closing the terminal should clean up like Ctrl+C does.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    workdir = Path(tempfile.mkdtemp(prefix="flutterdb-"))
    history = None if args.once else sqlmod.QueryHistory(package)
    engine = Engine(source, workdir, db=args.db, interval=args.interval, history=history)
    reg = None
    try:
        if args.once:
            print("Reading database…", file=sys.stderr)
            engine.poll_once()
            if engine.status["state"] == "error":
                print(f"error: {engine.status['message']}", file=sys.stderr)
                return 1
            print_summary(engine)
            return 0

        server, existing = bind(args.port, package, reuse=not args.demo and not args.dir)
        if existing:
            print(f"Already running for {package} on {existing}", file=sys.stderr)
            if not args.no_open:
                open_browser(existing)
            return 0
        server.RequestHandlerClass = make_handler(engine)
        server.daemon_threads = True
        port = server.server_address[1]
        reg = register(port, {"package": package, "source": source.label})
        # Serve right away; the page shows "Connecting…" until the first pull lands.
        threading.Thread(target=engine.run_forever, daemon=True).start()
        url = f"http://localhost:{port}"
        print(f"flutterdb {__version__} · {package} · {url}  (Ctrl+C to stop)", file=sys.stderr)
        if not args.no_open:
            open_browser(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        engine.stop()
        if demo:
            demo.stop()
            shutil.rmtree(demo.dir, ignore_errors=True)
        if reg:
            reg.unlink(missing_ok=True)
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

"""SQL console, database PRAGMA report, schema-aware query suggestions and
query history — everything the page's "SQL" and "Database" views need.

All of it runs against the engine's read-only snapshot, never the device.
User SQL goes through an authorizer that only allows reads, a progress
handler that enforces a time limit, and a row cap.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

DEFAULT_ROW_LIMIT = 1000
DEFAULT_TIMEOUT = 15.0


class QueryError(Exception):
    pass


def qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def ql(value) -> str:
    """SQL literal for a suggestion's text (values come from the snapshot)."""
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def jsonable(v):
    if isinstance(v, bytes):
        return f"<blob {len(v)} bytes>"
    return v


# ── read-only execution ────────────────────────────────────────────────────

# PRAGMAs that only read. Ones marked "noarg" become writes when given a value.
READ_PRAGMAS = {
    "table_info", "table_xinfo", "table_list", "index_list", "index_info", "index_xinfo",
    "foreign_key_list", "foreign_key_check", "integrity_check", "quick_check",
    "database_list", "collation_list", "function_list", "module_list", "pragma_list",
    "compile_options", "data_version", "stats",
}
READ_PRAGMAS_NOARG = {
    "page_size", "page_count", "freelist_count", "user_version", "application_id",
    "schema_version", "encoding", "auto_vacuum", "journal_mode", "max_page_count",
    "cache_size", "foreign_keys", "synchronous", "wal_autocheckpoint", "busy_timeout",
}
_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    getattr(sqlite3, "SQLITE_FUNCTION", 31),
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}
_DENIED_FUNCTIONS = {"load_extension"}


def _authorizer(action, arg1, arg2, dbname, source):
    if action in _ALLOWED_ACTIONS:
        if action == getattr(sqlite3, "SQLITE_FUNCTION", 31) and (arg2 or "").lower() in _DENIED_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        name = (arg1 or "").lower()
        if name in READ_PRAGMAS or (name in READ_PRAGMAS_NOARG and arg2 is None):
            return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def connect_ro(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, check_same_thread=False)
    con.execute("PRAGMA query_only=1")
    return con


def run_query(db: Path, sql: str, limit: int = DEFAULT_ROW_LIMIT, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Run one read-only statement. Raises QueryError with a readable message."""
    sql = strip_sql(sql)
    if not sql:
        raise QueryError("Write a query first.")
    con = connect_ro(db)
    deadline = time.monotonic() + timeout
    con.set_authorizer(_authorizer)
    con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    started = time.perf_counter()
    try:
        cur = con.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(limit + 1)
    except sqlite3.Warning as e:  # "You can only execute one statement at a time."
        raise QueryError(f"{e} Run statements one by one.") from None
    except sqlite3.DatabaseError as e:
        raise QueryError(explain_error(str(e), timeout)) from None
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        con.close()
    return {
        "columns": columns,
        "rows": [[jsonable(v) for v in r] for r in rows[:limit]],
        "truncated": len(rows) > limit,
        "limit": limit,
        "ms": round(elapsed, 1),
    }


def strip_sql(sql: str) -> str:
    sql = (sql or "").strip()
    while sql.endswith(";"):
        sql = sql[:-1].rstrip()
    return sql


def explain_error(msg: str, timeout: float) -> str:
    low = msg.lower()
    if "not authorized" in low:
        return ("Only read-only queries run here: SELECT, WITH … SELECT, EXPLAIN, and read-only PRAGMAs "
                "(table_info, index_list, integrity_check, user_version, …). The device is never written to.")
    if "interrupted" in low:
        return f"Stopped after {timeout:.0f} s. Add a WHERE or LIMIT, or check the plan with Explain."
    if "readonly" in low or "read-only" in low:
        return "The snapshot is read-only; writes aren't possible."
    return msg[:1].upper() + msg[1:]


# ── query plan and tips ────────────────────────────────────────────────────

_SCAN = re.compile(r"^SCAN (?:TABLE )?(\S+)(?: AS (\S+))?(.*)$")


def explain(db: Path, sql: str, row_counts: dict[str, int]) -> dict:
    """EXPLAIN QUERY PLAN plus plain-language tips for making the query faster."""
    sql = strip_sql(sql)
    tips: list[dict] = []
    plan: list[dict] = []
    lowered = re.sub(r"\s+", " ", sql).lower()
    if not lowered.startswith(("select", "with", "values")):
        return {"plan": plan, "tips": tips}
    con = connect_ro(db)
    con.set_authorizer(_authorizer)
    try:
        rows = con.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
    except sqlite3.DatabaseError:
        return {"plan": plan, "tips": tips}
    finally:
        con.close()
    for r in rows:
        plan.append({"id": r[0], "parent": r[1], "detail": r[3]})
        detail = r[3]
        m = _SCAN.match(detail)
        if m and "INDEX" not in (m.group(3) or ""):
            table = m.group(1)
            n = row_counts.get(table, 0)
            if n >= 1000:
                cols = where_columns(sql, table, m.group(2))
                if cols:
                    tips.append({"level": "warn", "text":
                                 f"Full scan of {table} ({n:,} rows) to filter on {', '.join(cols)}. "
                                 f"An index on {table}({cols[0]}) in the app's schema would turn this into a fast SEARCH."})
                else:
                    tips.append({"level": "info", "text": f"Reads all {n:,} rows of {table}. Fine for exploring; "
                                                          "add a WHERE on an indexed column to narrow it."})
        if "USE TEMP B-TREE FOR ORDER BY" in detail:
            tips.append({"level": "info", "text": "Sorting needs a temporary b-tree. Ordering by an indexed column "
                                                  "(or by rowid) avoids it."})
        if "CORRELATED" in detail:
            tips.append({"level": "info", "text": "A correlated subquery runs once per outer row; a JOIN or "
                                                  "GROUP BY is usually faster."})
    if " limit " not in f" {lowered} " and any(row_counts.get(t, 0) > 5000 for t in tables_in(sql)):
        tips.append({"level": "info", "text": "No LIMIT on a large table. Results are capped for display; "
                                              "add LIMIT to make the query cheaper too."})
    if re.search(r"like\s+'%", lowered):
        tips.append({"level": "info", "text": "LIKE with a leading % can't use an index, so it always scans."})
    seen, unique = set(), []
    for t in tips:
        if t["text"] not in seen:
            seen.add(t["text"])
            unique.append(t)
    return {"plan": plan, "tips": unique}


def tables_in(sql: str) -> list[str]:
    return [m.group(1).strip('"`[]') for m in re.finditer(r'(?:from|join)\s+("[^"]+"|[\w$]+)', sql, re.I)]


def where_columns(sql: str, table: str, alias: str | None) -> list[str]:
    m = re.search(r"\bwhere\b(.*?)(?:\bgroup\b|\border\b|\blimit\b|$)", sql, re.I | re.S)
    if not m:
        return []
    clause = m.group(1)
    names = []
    for c in re.finditer(r'(?:(\w+)\.)?("?[A-Za-z_]\w*"?)\s*(?:=|<|>|\bin\b|\bis\b|\blike\b|\bbetween\b)', clause, re.I):
        prefix, col = c.group(1), c.group(2).strip('"')
        if prefix and prefix not in (table, alias):
            continue
        if col.lower() not in ("and", "or", "not") and col not in names:
            names.append(col)
    return names


# ── database report (the "PRAGMA layer") ───────────────────────────────────


def db_report(db: Path, device: dict) -> dict:
    con = connect_ro(db)
    try:
        def p(name):
            return con.execute(f"PRAGMA {name}").fetchone()[0]

        page_size, page_count, freelist = p("page_size"), p("page_count"), p("freelist_count")
        objects = con.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        counts = {"table": 0, "index": 0, "view": 0, "trigger": 0}
        for t, *_ in objects:
            counts[t] = counts.get(t, 0) + 1
        auto_indexes = con.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='index' AND name LIKE 'sqlite_autoindex_%'"
        ).fetchone()[0]

        space, space_ok = [], True
        try:
            space = [
                {"name": n, "bytes": b, "pages": pg}
                for n, b, pg in con.execute(
                    "SELECT name, sum(pgsize), count(*) FROM dbstat GROUP BY name ORDER BY 2 DESC"
                )
            ]
        except sqlite3.DatabaseError:
            space_ok = False

        indexes = []
        for name, table in con.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='index' ORDER BY tbl_name, name"
        ):
            info = con.execute(f"PRAGMA index_list({qi(table)})").fetchall()
            meta = next((i for i in info if i[1] == name), None)
            cols = [r[2] for r in con.execute(f"PRAGMA index_info({qi(name)})") if r[2] is not None]
            indexes.append({
                "name": name, "table": table, "columns": cols,
                "unique": bool(meta[2]) if meta else False,
                "origin": {"c": "CREATE INDEX", "u": "UNIQUE constraint", "pk": "PRIMARY KEY"}.get(
                    meta[3] if meta else "c", "?"),
                "partial": bool(meta[4]) if meta and len(meta) > 4 else False,
            })

        pragmas = [
            {"name": "user_version", "value": p("user_version"),
             "hint": "Schema version the app sets; most migration frameworks bump it per migration."},
            {"name": "journal_mode (device)", "value": device.get("journal_mode", "unknown"),
             "hint": "WAL lets the app read while writing; recent writes live in the -wal file."},
            {"name": "page_size", "value": page_size, "hint": "Bytes per page."},
            {"name": "page_count", "value": page_count, "hint": "Pages in the main file after merging the WAL."},
            {"name": "freelist_count", "value": freelist,
             "hint": "Unused pages. VACUUM in the app would give this space back."},
            {"name": "auto_vacuum", "value": {0: "none", 1: "full", 2: "incremental"}.get(p("auto_vacuum"), p("auto_vacuum")),
             "hint": "Whether SQLite shrinks the file when data is deleted."},
            {"name": "encoding", "value": p("encoding"), "hint": "Text encoding."},
            {"name": "application_id", "value": p("application_id"), "hint": "Optional app tag in the file header."},
            {"name": "schema_version", "value": p("schema_version"),
             "hint": "Bumped by SQLite on every schema change (CREATE/ALTER/DROP)."},
            {"name": "sqlite_version (viewer)", "value": sqlite3.sqlite_version,
             "hint": "SQLite library used to read the snapshot; the app's may differ."},
        ]
        counts["index"] = len(indexes)  # including automatic PRIMARY KEY / UNIQUE indexes
        return {
            "pragmas": pragmas,
            "size": {
                "bytes": page_size * page_count,
                "free_bytes": page_size * freelist,
                "device_main": device.get("main", 0),
                "device_wal": device.get("wal", 0),
            },
            "counts": {**counts, "auto_indexes": auto_indexes},
            "space": space,
            "space_available": space_ok,
            "indexes": indexes,
            "objects": [{"type": t, "name": n, "table": tb, "sql": s} for t, n, tb, s in objects if t in ("view", "trigger")],
        }
    finally:
        con.close()


def run_check(db: Path, kind: str) -> dict:
    """integrity_check / quick_check / foreign_key_check, on demand (can be slow)."""
    pragma = {"integrity": "integrity_check", "quick": "quick_check", "foreign_keys": "foreign_key_check"}.get(kind)
    if not pragma:
        raise QueryError(f"unknown check {kind!r}")
    con = connect_ro(db)
    started = time.perf_counter()
    try:
        cur = con.execute(f"PRAGMA {pragma}")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(500)
    finally:
        con.close()
    ok = (kind != "foreign_keys" and rows == [("ok",)]) or (kind == "foreign_keys" and not rows)
    return {"kind": kind, "ok": ok, "columns": cols, "rows": [list(r) for r in rows],
            "ms": round((time.perf_counter() - started) * 1000, 1)}


def table_meta(db: Path, table: str) -> dict:
    con = connect_ro(db)
    try:
        indexes = []
        for r in con.execute(f"PRAGMA index_list({qi(table)})"):
            cols = [c[2] for c in con.execute(f"PRAGMA index_info({qi(r[1])})") if c[2] is not None]
            indexes.append({"name": r[1], "unique": bool(r[2]), "origin": r[3], "columns": cols})
        fks = [
            {"column": r[3], "table": r[2], "to": r[4], "on_update": r[5], "on_delete": r[6]}
            for r in con.execute(f"PRAGMA foreign_key_list({qi(table)})")
        ]
        triggers = [
            {"name": n, "sql": s}
            for n, s in con.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,))
        ]
        return {"indexes": indexes, "foreign_keys": fks, "triggers": triggers}
    finally:
        con.close()


# ── suggestions ────────────────────────────────────────────────────────────

TS_NAMES = ("modified", "updated_at", "local_modified", "last_modified", "creation", "created_at",
            "created", "timestamp", "date", "pulled_at")
STATUS_HINT = re.compile(r"(status|state|stage|type|kind|category|op|gender|docstatus|city|role|priority|level)$", re.I)
FLAG_SUFFIX = ("__is_local", "__norm")
# Few distinct values in a sample doesn't make these categories.
FREE_TEXT = re.compile(r"(note|comment|remark|description|desc|address|message|body|text|reason|details?|summary|payload|error)s?$", re.I)
KEY_COLS = ("server_name", "name", "id", "uuid", "mobile_uuid", "code")


def _cols(info) -> list[dict]:
    return info["columns"] if isinstance(info, dict) else info.columns


def _rows(info) -> int:
    return info["rows"] if isinstance(info, dict) else info.rows


def _pk(info) -> list[str]:
    return [c["name"] for c in sorted(_cols(info), key=lambda c: c["pk"]) if c["pk"]]


def _snake(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", "_", text.lower()).strip("_")


def _label_col(info) -> str | None:
    names = [c["name"] for c in _cols(info)]
    for want in ("name", "title", "label", "full_name", "server_name"):
        if want in names:
            return want
    for c in _cols(info):
        if c["name"].endswith("_name") and not c["name"].endswith(FLAG_SUFFIX):
            return c["name"]
    pk = _pk(info)
    return pk[0] if pk else (names[0] if names else None)


def _ts_col(info) -> str | None:
    names = {c["name"]: c for c in _cols(info)}
    for want in TS_NAMES:
        if want in names:
            return want
    for c in _cols(info):
        n, t = c["name"].lower(), (c["type"] or "").upper()
        if t in ("DATETIME", "TIMESTAMP", "DATE") or n.endswith(("_at", "_on", "_date", "_time")):
            return c["name"]
    return None


def _target_candidates(col: str, tables: dict) -> list[str]:
    base = col.lower()
    bases = [base]
    for suf in ("_id", "_uuid", "_code", "_ref", "_key"):
        if base.endswith(suf) and len(base) > len(suf):
            bases.append(base[: -len(suf)])
    out = []
    for b in bases:
        for t in (b, b + "s", b + "es", b[:-1] if b.endswith("s") else None, "docs__" + b, "docs__" + b + "s"):
            if t and t in tables and t not in out:
                out.append(t)
    return out


class Suggester:
    """Builds ready-to-run queries from the snapshot's schema and data."""

    def __init__(self, db: Path, tables: dict):
        self.db = db
        self.tables = tables  # name -> TableInfo (or dict with columns/rows)
        self.con = connect_ro(db)

    def close(self):
        self.con.close()

    def q(self, sql, args=()):
        return self.con.execute(sql, args).fetchall()

    # links ---------------------------------------------------------------

    def match(self, table: str, col: str, target: str, sample: int = 200):
        """Best key column of `target` that values of table.col point to, and the match rate."""
        values = [r[0] for r in self.q(
            f"SELECT DISTINCT {qi(col)} FROM {qi(table)} WHERE {qi(col)} IS NOT NULL AND {qi(col)} != '' LIMIT ?",
            (sample,))]
        if not values:
            return None
        tinfo = self.tables[target]
        names = [c["name"] for c in _cols(tinfo)]
        cands = [c for c in _pk(tinfo) + [k for k in KEY_COLS if k in names]]
        best = None
        for tc in dict.fromkeys(cands):
            hit = self.q(
                f"SELECT count(DISTINCT {qi(tc)}) FROM {qi(target)} WHERE {qi(tc)} IN ({','.join('?' * len(values))})",
                values)[0][0]
            rate = hit / len(values)
            if best is None or rate > best[1]:
                best = (tc, rate)
        if best and best[1] >= 0.5:
            return {"column": col, "table": target, "to": best[0], "rate": best[1], "sampled": len(values)}
        return None

    def links_from(self, table: str, limit: int = 8) -> list[dict]:
        info = self.tables[table]
        names = {c["name"] for c in _cols(info)}
        links = []
        for fk in self.q(f"PRAGMA foreign_key_list({qi(table)})"):
            if fk[2] in self.tables:
                m = self.match(table, fk[3], fk[2])
                links.append(m or {"column": fk[3], "table": fk[2], "to": fk[4] or "rowid", "rate": None, "sampled": 0})
        done = {link["column"] for link in links}
        pk = set(_pk(info))
        for c in _cols(info):
            n = c["name"]
            if len(links) >= limit:
                break
            if n in done or n in pk or n.endswith(FLAG_SUFFIX) or (c["type"] or "").upper() in ("REAL", "BLOB"):
                continue
            cands = _target_candidates(n, self.tables)
            if n.lower() in ("parent_uuid", "parent", "parent_id"):
                cands = self._parent_candidates(table, names) + cands
            for target in cands:
                if target == table and not n.lower().startswith("parent"):
                    continue
                m = self.match(table, n, target)
                if m:
                    links.append(m)
                    break
        return links

    def _parent_candidates(self, table: str, names: set) -> list[str]:
        out = []
        for dt_col in ("parent_doctype", "parenttype", "parent_type", "parent_table"):
            if dt_col in names:
                for (v,) in self.q(f"SELECT DISTINCT {qi(dt_col)} FROM {qi(table)} WHERE {qi(dt_col)} IS NOT NULL LIMIT 3"):
                    s = _snake(str(v))
                    for t in (s, "docs__" + s, s + "s"):
                        if t in self.tables and t not in out:
                            out.append(t)
        prefixes = sorted((t for t in self.tables if t != table and table.startswith(t + "_")), key=len, reverse=True)
        return out + [p for p in prefixes if p not in out]

    def links_to(self, table: str, limit: int = 6) -> list[dict]:
        """Other tables whose columns point at this one (children / referencing rows)."""
        out = []
        for other, info in self.tables.items():
            if other == table or not _rows(info):
                continue
            names = {c["name"] for c in _cols(info)}
            for c in _cols(info):
                n = c["name"]
                if n.endswith(FLAG_SUFFIX):
                    continue
                cands = _target_candidates(n, self.tables)
                if n.lower() in ("parent_uuid", "parent", "parent_id"):
                    cands = self._parent_candidates(other, names) + cands
                if table in cands:
                    m = self.match(other, n, table)
                    if m:
                        out.append({**m, "from": other})
                        break
            if len(out) >= limit:
                break
        return out

    # per-table -------------------------------------------------------------

    def for_table(self, table: str) -> list[dict]:
        info = self.tables[table]
        n = _rows(info)
        t = qi(table)
        cols = _cols(info)
        names = [c["name"] for c in cols]
        out = []
        order = "rowid DESC" if getattr(info, "has_rowid", True) else "1"
        ts = _ts_col(info)
        if ts:
            out.append(sug("recent", f"Latest rows by {ts}",
                           f"SELECT *\nFROM {t}\nORDER BY {qi(ts)} DESC\nLIMIT 50",
                           f"{ts} looks like a timestamp column."))
        out.append(sug("recent", "Newest inserted rows", f"SELECT *\nFROM {t}\nORDER BY {order}\nLIMIT 50",
                       "Highest rowid = most recently inserted."))

        if "sync_status" in names:
            extra = ", sync_error" if "sync_error" in names else ""
            out.append(sug("quality", "Sync status breakdown",
                           f"SELECT sync_status{extra}, count(*) AS rows\nFROM {t}\nGROUP BY sync_status{extra}\nORDER BY rows DESC",
                           "Offline-first table: see what's pending or failing."))
            dirty = self.q(f"SELECT count(*) FROM {t} WHERE sync_status IS NOT 'synced'")[0][0]
            if dirty:
                out.append(sug("quality", f"{dirty:,} rows not synced",
                               f"SELECT *\nFROM {t}\nWHERE sync_status IS NOT 'synced'\nORDER BY {order}",
                               "Found in the data right now."))

        status = self._status_cols(table, info)
        # "block_name" next to "block" is the same grouping twice.
        status = [c for c in status if not (c.endswith("_name") and c[:-5] in status)]
        for col in status[:3]:
            out.append(sug("group", f"Rows per {col}",
                           f"SELECT {qi(col)}, count(*) AS rows\nFROM {t}\nGROUP BY {qi(col)}\nORDER BY rows DESC",
                           f"{col} has few distinct values."))

        links = self.links_from(table)
        for link in links:
            target = self.tables[link["table"]]
            label = _label_col(target)
            rate = f"{link['rate']:.0%} of sampled values match" if link["rate"] is not None else "declared FOREIGN KEY"
            show = f",\n       b.{qi(label)} AS {qi(link['table'].replace('docs__', '') + '_' + label)}" if label else ""
            key = _pk(info)[0] if _pk(info) else names[0]
            out.append(sug("join", f"Join {link['column']} → {link['table']}",
                           f"SELECT a.{qi(key)}, a.{qi(link['column'])}{show}\nFROM {t} a\n"
                           f"LEFT JOIN {qi(link['table'])} b ON b.{qi(link['to'])} = a.{qi(link['column'])}\nLIMIT 100",
                           f"{link['column']} → {link['table']}.{link['to']} ({rate})."))
            if link["rate"] is not None and link["rate"] < 1:
                out.append(sug("quality", f"Broken links in {link['column']}",
                               f"SELECT a.*\nFROM {t} a\nWHERE a.{qi(link['column'])} IS NOT NULL\n"
                               f"  AND NOT EXISTS (SELECT 1 FROM {qi(link['table'])} b "
                               f"WHERE b.{qi(link['to'])} = a.{qi(link['column'])})\nLIMIT 100",
                               f"Only {link['rate']:.0%} of sampled values exist in {link['table']} — "
                               "the rest point to rows that aren't on the device."))

        if n:
            for link in self.links_to(table):
                key = link["to"]
                child = link["from"]
                out.append(sug("children", f"Count of {child.replace('docs__', '')} per row",
                               f"SELECT p.{qi(key)}, count(c.rowid) AS {qi(child.replace('docs__', '') + '_count')}\n"
                               f"FROM {t} p\nLEFT JOIN {qi(child)} c ON c.{qi(link['column'])} = p.{qi(key)}\n"
                               f"GROUP BY p.{qi(key)}\nORDER BY 2 DESC\nLIMIT 50",
                               f"{child}.{link['column']} points here ({link['rate']:.0%} match)."))

        # Link columns repeat by design (many rows point at one village / parent).
        linked = {link["column"] for link in links}
        for col in [c for c in self._duplicate_cols(table, info) if c not in linked and not c.lower().startswith("parent")][:2]:
            out.append(sug("quality", f"Duplicate {col} values",
                           f"SELECT {qi(col)}, count(*) AS copies\nFROM {t}\nGROUP BY {qi(col)}\n"
                           f"HAVING count(*) > 1\nORDER BY copies DESC\nLIMIT 50",
                           f"Duplicates of {col} exist in the data right now."))

        if n and len(cols) > 1:
            pick = [c["name"] for c in cols if not c["name"].endswith(FLAG_SUFFIX)][:24]
            out.append(sug("quality", "Empty values per column",
                           "SELECT count(*) AS rows,\n" + ",\n".join(
                               f"       sum({qi(c)} IS NULL OR {qi(c)} = '') AS {qi(c)}" for c in pick)
                           + f"\nFROM {t}",
                           f"How complete the first {len(pick)} columns are."))
        out.append(sug("schema", "Columns and types", f"SELECT cid, name, type, \"notnull\", pk\nFROM pragma_table_info({ql(table)})",
                       "Same as the Columns tab, as a query you can extend."))
        out.append(sug("schema", "Indexes on this table",
                       f"SELECT il.name AS index_name, il.\"unique\", ii.name AS column\n"
                       f"FROM pragma_index_list({ql(table)}) il\nJOIN pragma_index_info(il.name) ii\nORDER BY il.name, ii.seqno",
                       "Which columns are fast to filter on."))
        return out

    def _status_cols(self, table, info) -> list[str]:
        if _rows(info) < 3:
            return []
        scored = []
        pk = set(_pk(info))
        for c in _cols(info)[:60]:
            n = c["name"]
            if (n in pk or n.endswith(FLAG_SUFFIX) or n in ("sync_status", "sync_error") or FREE_TEXT.search(n)
                    or (c["type"] or "").upper() in ("REAL", "BLOB")):
                continue
            distinct = self.q(f"SELECT count(DISTINCT {qi(n)}) FROM (SELECT {qi(n)} FROM {qi(table)} LIMIT 5000)")[0][0]
            if 2 <= distinct <= 20:
                scored.append((0 if STATUS_HINT.search(n) else 1, distinct, n))
        return [n for *_, n in sorted(scored)]

    def _duplicate_cols(self, table, info) -> list[str]:
        if _rows(info) < 2:
            return []
        out = []
        pk = set(_pk(info))
        for c in _cols(info):
            n = c["name"]
            low = n.lower()
            if n in pk or not (low.endswith(("_id", "uuid", "_code", "_number")) or low in ("server_name", "name", "email", "phone")):
                continue
            if self.q(f"SELECT 1 FROM {qi(table)} WHERE {qi(n)} IS NOT NULL AND {qi(n)} != '' "
                      f"GROUP BY {qi(n)} HAVING count(*) > 1 LIMIT 1"):
                out.append(n)
        return out

    # database-wide ------------------------------------------------------------

    def global_suggestions(self, recently_changed: list[str]) -> list[dict]:
        out = []
        non_empty = [name for name, info in self.tables.items() if _rows(info)]
        if non_empty:
            union = "\nUNION ALL ".join(f"SELECT {ql(t)} AS table_name, count(*) AS rows FROM {qi(t)}" for t in non_empty)
            out.append(sug("overview", "Row count of every table", f"{union}\nORDER BY rows DESC",
                           f"{len(non_empty)} tables have data."))
        try:
            self.q("SELECT 1 FROM dbstat LIMIT 1")
            out.append(sug("overview", "Disk space per table and index",
                           "SELECT name, count(*) AS pages, sum(pgsize) AS bytes\nFROM dbstat\nGROUP BY name\nORDER BY bytes DESC",
                           "Uses SQLite's dbstat table."))
        except sqlite3.DatabaseError:
            pass
        sync_tables = [name for name, info in self.tables.items()
                       if any(c["name"] == "sync_status" for c in _cols(info)) and _rows(info)]
        dirty = [t for t in sync_tables if self.q(f"SELECT 1 FROM {qi(t)} WHERE sync_status IS NOT 'synced' LIMIT 1")]
        if sync_tables:
            pick = dirty or sync_tables
            union = "\nUNION ALL ".join(
                f"SELECT {ql(t)} AS table_name, sync_status, count(*) AS rows FROM {qi(t)} GROUP BY sync_status"
                for t in pick[:40])
            out.append(sug("quality", f"{'Unsynced rows' if dirty else 'Sync status'} across tables",
                           f"{union}\nORDER BY rows DESC",
                           f"{len(dirty)} tables have unsynced rows right now." if dirty
                           else "Everything is synced right now; run it again after editing in the app."))
        for t in recently_changed[:3]:
            if t in self.tables:
                order = "rowid DESC" if getattr(self.tables[t], "has_rowid", True) else "1"
                out.append(sug("recent", f"Latest rows in {t}", f"SELECT *\nFROM {qi(t)}\nORDER BY {order}\nLIMIT 50",
                               "Changed while you've been watching."))
        out.append(sug("schema", "All tables and columns",
                       "SELECT m.name AS table_name, p.cid, p.name AS column, p.type, p.pk\n"
                       "FROM sqlite_master m\nJOIN pragma_table_info(m.name) p\nWHERE m.type = 'table'\nORDER BY m.name, p.cid",
                       "Search the whole schema with a WHERE on column."))
        out.append(sug("schema", "Find a column by name",
                       "SELECT m.name AS table_name, p.name AS column, p.type\nFROM sqlite_master m\n"
                       "JOIN pragma_table_info(m.name) p\nWHERE m.type = 'table'\n  AND p.name LIKE '%status%'\nORDER BY 1",
                       "Edit the LIKE pattern."))
        out.append(sug("schema", "All indexes",
                       "SELECT tbl_name AS table_name, name AS index_name, sql\nFROM sqlite_master\nWHERE type = 'index'\nORDER BY 1, 2",
                       "Auto-indexes (from PRIMARY KEY / UNIQUE) have no SQL."))
        return out


def sug(kind: str, title: str, sql: str, why: str) -> dict:
    return {"kind": kind, "title": title, "sql": sql, "why": why}


# ── history ────────────────────────────────────────────────────────────────


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "flutterdb"


def norm_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", strip_sql(sql)).strip()


class QueryHistory:
    """Queries you ran, per app, kept across sessions: recent, frequent and starred."""

    MAX = 300

    def __init__(self, key: str, directory: Path | None = None):
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", key)[:120] or "default"
        self.path = (directory or data_dir() / "history") / f"{safe}.json"
        self.lock = threading.Lock()
        self.items: list[dict] = []
        try:
            self.items = json.loads(self.path.read_text()).get("queries", [])
        except (OSError, ValueError, AttributeError):
            self.items = []

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 1, "queries": self.items}, indent=1))
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _find(self, sql):
        key = norm_sql(sql)
        return next((q for q in self.items if q["key"] == key), None)

    def record(self, sql: str, rows: int, ms: float) -> None:
        with self.lock:
            now = time.time()
            q = self._find(sql)
            if q is None:
                q = {"key": norm_sql(sql), "sql": strip_sql(sql), "runs": 0, "first": now, "star": False, "name": ""}
                self.items.append(q)
            q.update(sql=strip_sql(sql), runs=q["runs"] + 1, last=now, rows=rows, ms=ms)
            plain = sorted((x for x in self.items if not x["star"]), key=lambda x: x.get("last", 0))
            for old in plain[: max(0, len(self.items) - self.MAX)]:
                self.items.remove(old)
            self._save()

    def star(self, sql: str, star: bool, name: str = "") -> dict:
        with self.lock:
            q = self._find(sql)
            if q is None:
                q = {"key": norm_sql(sql), "sql": strip_sql(sql), "runs": 0, "first": time.time(), "last": time.time()}
                self.items.append(q)
            q["star"] = star
            if name or not star:
                q["name"] = name
            self._save()
            return q

    def delete(self, sql: str) -> None:
        with self.lock:
            q = self._find(sql)
            if q:
                self.items.remove(q)
                self._save()

    def listing(self) -> dict:
        with self.lock:
            items = [dict(q) for q in self.items]
        return {
            "starred": sorted((q for q in items if q.get("star")), key=lambda q: -q.get("last", 0)),
            "frequent": sorted((q for q in items if q["runs"] >= 2), key=lambda q: (-q["runs"], -q.get("last", 0)))[:8],
            "recent": sorted(items, key=lambda q: -q.get("last", 0))[:25],
        }

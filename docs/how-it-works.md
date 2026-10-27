# How flutterdb works

## Reading a sandboxed database

An Android app's files live in `/data/data/<applicationId>/`, readable only by
the app's own Linux user. On a **debuggable** build, `adb shell run-as <app>`
runs a command as that user — the same mechanism Android Studio uses. flutterdb
uses three kinds of command, all read-only:

```sh
# what's there (sizes + nanosecond mtimes), every poll
adb exec-out "run-as <app> sh -c 'stat -c \"%n|%s|%y\" databases/* app_flutter/*.db …'"
# md5 of each 4 MB chunk, when a local copy already exists
adb exec-out "run-as <app> sh -c 'i=0; while [ \$i -lt N ]; do dd if=F bs=4194304 skip=\$i count=1 | md5sum; …'"
# one chunk
adb exec-out "run-as <app> sh -c 'dd if=databases/app.db bs=4194304 skip=7 count=1'"
```

`exec-out` (not `shell`) gives a clean binary stream. File names passed to
`dd` are checked against `^(databases|app_flutter|files)/[A-Za-z0-9._+-]+$` before
use, so a hostile file name can't inject shell syntax.

## Chunked, incremental transfer

A single `cat` of a 100 MB file is one long USB stream, and on some phones it
stalls near the end and never finishes. So files are read as 4 MB chunks, each
its own short `adb` call with a 30 s timeout and 3 retries — a hiccup costs one
chunk, not the whole file. Progress (`52 / 104 MB`) is shown in the page.

When a local copy of the file already exists (every poll after the first), the
device computes an md5 per chunk (`dd | md5sum`, ~1 s for 100 MB) and only the
chunks whose hash differs from the local copy are transferred. A checkpoint
usually rewrites a handful of pages, so a 100 MB main file typically costs a
few MB instead of 100.

## WAL mode and why it matters

Most Flutter SQLite libraries open databases in **WAL** (write-ahead log) mode:
new writes are appended to `app.db-wal`, and only folded into `app.db` at a
*checkpoint* (every ~1000 pages, or when the app closes the DB). Copying just
`app.db` — what most "pull the database" scripts do — silently misses every
recent write.

flutterdb copies both and lets SQLite merge them:

1. `stat` every 2 s. Sizes and **nanosecond** mtimes of `app.db` and
   `app.db-wal` are compared with the previous poll; if nothing moved, nothing
   is copied.
2. If `app.db` changed (a checkpoint happened) its changed chunks are copied;
   otherwise the cached copy is reused. The WAL's changed chunks are copied
   whenever it changed.
3. After copying, `app.db` is `stat`ed again. If a checkpoint landed *during*
   the copy, the pair may not belong together, so the result is discarded and
   retried on the next tick.
4. The pair is cloned into a fresh snapshot file (an instant APFS clone on
   macOS), opened, `PRAGMA wal_checkpoint(TRUNCATE)` folds the WAL in, and the
   snapshot is switched to rollback-journal mode. Every page request then reads
   that immutable file with `mode=ro`.

A WAL being appended to while it's copied is fine: SQLite checksums each
frame and ignores a torn trailing frame, so the snapshot is simply a moment
earlier.

## Diffing

For every table with a rowid, each row is hashed (`blake2b` of its values,
8 bytes) and stored as `rowid → hash`. Comparing two snapshots gives:

- **inserted** — rowids only in the new snapshot
- **deleted** — rowids only in the old one
- **updated** — same rowid, different hash

For updated rows (up to 500 per table per poll) the old and new rows are read
from both snapshots to find exactly which columns changed and what their old
values were. Marks accumulate per row for 15 minutes: a row edited twice keeps
every touched column and the value from *before the first* edit.

`WITHOUT ROWID` tables can't be diffed row by row; their row-count changes are
reported instead.

## Pushing to the browser

The page holds one [Server-Sent Events](https://developer.mozilla.org/docs/Web/API/Server-sent_events)
connection (`/api/stream`). Whenever the engine's version counter moves — new
snapshot, status change — the server sends the number and the page refetches
`/api/state` and, if a table is open, `/api/rows`. Rows are re-rendered only if
their content or marks changed, so scrolling isn't disturbed.

## SQL console safety

User SQL runs on the snapshot (never the device) through a connection opened
`mode=ro` with `PRAGMA query_only=1` and an SQLite **authorizer** that allows
only `SELECT`, reads, function calls and recursive CTEs. PRAGMAs are allowed
from a read-only list (`table_info`, `index_list`, `integrity_check`, …);
value-less ones like `user_version` are refused when given a value. `ATTACH`,
`load_extension`, DDL and DML are denied. A progress handler stops a query
after 15 s, results are capped (1,000 rows by default, up to 10,000), and only
one statement runs per request.

## Recommendations

Suggestions are generated per snapshot (and cached until the next one):

- **Links** — for each column, candidate target tables come from declared
  foreign keys, the name (`village`, `village_id`, `customer_id` →
  `village`, `docs__village`, `customers`, …) and, for `parent_uuid`/`parent`,
  the values of a sibling `parent_doctype` column and tables whose name
  prefixes this one. Up to 200 distinct values are sampled and matched against
  each key-like column of the target (primary key, `server_name`, `name`, `id`,
  `uuid`, `mobile_uuid`, `code`); a link is suggested only if ≥ 50 % match. Below
  100 % a "broken links" query is offered too.
- **Breakdowns** — columns with 2–20 distinct values in the first 5,000 rows,
  preferring names like `status`/`type`/`state`, skipping flags and free text.
- **Duplicates** — key-like columns (`*_id`, `*uuid`, `phone`, …) where a
  `GROUP BY … HAVING count(*) > 1` actually finds one, excluding link columns.
- **Sync** — tables with a `sync_status` column get breakdowns and "not synced" queries.

Query history is stored per app in `~/.local/share/flutterdb/history/<app>.json`
(override with `XDG_DATA_HOME`), deduplicated by whitespace-normalised SQL,
capped at 300 unstarred entries.

## HTTP API

All JSON, all on `127.0.0.1`. Requests whose `Host` isn't `localhost`,
`127.0.0.1` or `::1` get 403 (DNS-rebinding protection); POSTs must be
`application/json`, which a cross-site page can't send without a CORS
preflight this server never approves.

| Endpoint | Returns |
|---|---|
| `GET /api/state` | status, databases, sizes, per-table rows / columns / sync counts, activity feed, recently-changed tables |
| `GET /api/rows?table=T&page=0&size=100&q=&sort=col&dir=desc&changed=1` | one page of rows with change marks |
| `GET /api/stream?v=N` | SSE; one `data: <version>` line per change |
| `GET /api/dbinfo` | PRAGMA values, sizes, object counts, space per table/index (dbstat), every index |
| `GET /api/table_meta?table=T` | the table's indexes, foreign keys, triggers |
| `GET /api/suggest[?table=T]` | recommended queries + history (starred / frequent / recent) |
| `GET /api/history` | history only |
| `POST /api/query` `{sql, limit}` | columns, rows, truncated, ms, plan, tips |
| `POST /api/explain` `{sql}` | plan and tips without running |
| `POST /api/check` `{kind: quick\|integrity\|foreign_keys}` | result rows, ok |
| `POST /api/history/star` `{sql, star, name}` · `POST /api/history/delete` `{sql}` | |
| `POST /api/select` `{db}` | switch database |
| `POST /api/clear` | clear highlights and the activity feed |
| `POST /api/refresh` | poll now |

Table and column names from requests are only used after being matched against
the snapshot's schema, and are always quoted as identifiers.

## Performance

Measured on an M-series Mac over USB-C with a 100 MB database (150 tables,
~170k rows) plus a 22 MB WAL:

| Step | Time |
|---|---|
| First copy of the main file (26 chunks) | ~6 s |
| md5 of every chunk on the device | ~1 s |
| Copy of a 22 MB WAL | ~1 s |
| APFS clone + checkpoint | < 0.2 s |
| Hash every row | ~1.5 s |
| Poll with no change | one `stat`, ~50 ms |

## Processes and cleanup

Each viewer writes `~/.cache/flutterdb/running/<port>.json` (pid, port,
package) — that's what `flutterdb list` and `flutterdb stop` read. On Ctrl+C
or `SIGTERM` the viewer kills any in-flight `adb` copy, deletes its temp
snapshot directory and removes its registry entry. Stale entries (crashed
viewers) are pruned automatically.

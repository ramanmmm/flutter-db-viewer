# How flutterdb works

## Reading a sandboxed database

An Android app's files live in `/data/data/<applicationId>/`, readable only by
the app's own Linux user. On a **debuggable** build, `adb shell run-as <app>`
runs a command as that user — the same mechanism Android Studio uses. flutterdb
uses exactly two commands, both read-only:

```sh
adb exec-out "run-as <app> sh -c 'stat -c \"%n|%s|%y\" databases/* app_flutter/*.db …'"
adb exec-out "run-as <app> sh -c 'cat databases/app.db'" > local-copy
```

`exec-out` (not `shell`) gives a clean binary stream. File names passed to
`cat` are checked against `^(databases|app_flutter|files)/[^/'\\\s]+$` before
use, so a hostile file name can't inject shell syntax.

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
2. If `app.db` changed (a checkpoint happened) it is re-copied; otherwise the
   cached copy is reused. The WAL is copied whenever it changed — usually a
   few KB to a few MB.
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

## HTTP API

All JSON, all on `127.0.0.1`.

| Endpoint | Returns |
|---|---|
| `GET /api/state` | status, databases, sizes, per-table rows / columns / sync counts, activity feed, recently-changed tables |
| `GET /api/rows?table=T&page=0&size=100&q=&sort=col&dir=desc&changed=1` | one page of rows with change marks |
| `GET /api/stream?v=N` | SSE; one `data: <version>` line per change |
| `POST /api/select?db=NAME` | switch database |
| `POST /api/clear` | clear highlights and the activity feed |
| `POST /api/refresh` | poll now |

Table and column names from requests are only used after being matched against
the snapshot's schema, and are always quoted as identifiers.

## Performance

Measured on an M-series Mac over USB-C with a 100 MB database (150 tables,
~170k rows) plus a 22 MB WAL:

| Step | Time |
|---|---|
| First copy of the main file | ~3.5 s |
| Copy of a 22 MB WAL | ~0.8 s |
| APFS clone + checkpoint | < 0.2 s |
| Hash every row | ~1.5 s |
| Poll with no change | one `stat`, ~50 ms |

## Processes and cleanup

Each viewer writes `~/.cache/flutterdb/running/<port>.json` (pid, port,
package) — that's what `flutterdb list` and `flutterdb stop` read. On Ctrl+C
or `SIGTERM` the viewer kills any in-flight `adb` copy, deletes its temp
snapshot directory and removes its registry entry. Stale entries (crashed
viewers) are pruned automatically.

# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- `examples/checkLiveDb.sh` failed with "unbound variable" on macOS's bash 3.2
  under a UTF-8 locale when installing or upgrading (a `…` right after
  `$FLUTTERDB_REF` was read as part of the name). Covered by a new test that
  runs the launcher with stand-in `curl` / `flutterdb`.

## [1.1.1] — 2026-10-27

### Fixed
- SQL console and recommendations that use table-valued PRAGMAs
  (`pragma_table_info(…)`, `pragma_index_list(…)`) were refused on SQLite older
  than 3.38 (e.g. python.org Python 3.9 on macOS), which reports an internal
  schema-table check the read-only authorizer denied.

### Changed
- CI can be started manually (`workflow_dispatch`).

## [1.1.0] — 2026-10-27

### Added
- **SQL console** (SQL mode): read-only queries against the live snapshot with
  schema-aware autocomplete (tables, columns of the tables in the query incl.
  aliases, keywords, functions, PRAGMAs), ⌘/Ctrl+Enter to run, CSV export,
  row drawer, and *Re-run on every change* for live queries.
- **Query recommendations** built from the schema and verified against the data:
  joins (declared FKs, `*_id`/`*_uuid` naming, Frappe-style `parent_uuid` +
  `parent_doctype`, link columns holding another table's key — with match
  rates), child counts, broken links, sync breakdowns and unsynced rows,
  low-cardinality breakdowns, real duplicates, empty-value counts, schema and
  index queries; plus database-wide ones (row counts, disk space, sync across tables).
- **Query history** per app — recent, frequently run and starred queries,
  kept across sessions in `~/.local/share/flutterdb/history/`.
- **Plan coach**: query plan plus tips after every run (full scans of big
  tables with the index that would help, temp b-tree sorts, correlated
  subqueries, missing LIMIT, leading-`%` LIKE).
- **Database view** (PRAGMA layer): `user_version`, device journal mode from the
  file header, page size/count, free pages, auto-vacuum, encoding, schema
  cookie; space per table and index (dbstat); every index with columns and
  origin; on-demand `quick_check`, `integrity_check`, `foreign_key_check`.
- Columns tab shows the table's indexes, foreign keys and triggers.
- Pull progress in the status pill; bytes transferred per pull in the header.

### Changed
- Device files are copied in 4 MB chunks with per-chunk timeout and retries,
  and only chunks whose md5 changed on the device are transferred. Fixes pulls
  that stalled near the end of large files on some phones.
- `checkLiveDb.sh` template upgrades flutterdb when the pinned release differs
  from the installed one.

### Security
- Remote file names are restricted to plain file-name characters before being
  used in the on-device shell command (previously `;`/`$` were not rejected).
- The HTTP server rejects foreign `Host` headers (DNS rebinding) and non-JSON
  POSTs (cross-site requests).

## [1.0.0] — 2026-10-27

First public release.

### Added
- Live browser view of a debuggable Android app's SQLite databases over `adb run-as`:
  tables, row and column counts, database size, `sync_status` breakdowns.
- Incremental pulls: only the WAL is copied unless a checkpoint rewrote the main file;
  checkpoints racing a copy are detected and retried.
- Row-level diffing between snapshots: inserted rows in green, edited rows in amber with
  the changed cells and their original values, deletions in the activity feed.
- Data grid with search across all columns, sorting, paging, "only new / changed" filter,
  row drawer with pretty-printed JSON, Columns and Schema tabs.
- Database discovery in `databases/`, `app_flutter/` and `files/`; library databases
  (Google Play services, Firebase, WorkManager) are skipped when choosing the default.
- `applicationId` auto-detection from `android/app/build.gradle(.kts)`.
- `flutterdb list` / `flutterdb stop`; re-running for a watched app re-opens its page;
  several apps can be watched at once on separate ports.
- `--once` terminal summary, `--demo` built-in demo app, `--dir` for local files.
- `install.sh` (no pip needed) and `examples/checkLiveDb.sh` per-project launcher.

[1.1.1]: https://github.com/ramanmmm/flutter-db-viewer/releases/tag/v1.1.1
[1.1.0]: https://github.com/ramanmmm/flutter-db-viewer/releases/tag/v1.1.0
[1.0.0]: https://github.com/ramanmmm/flutter-db-viewer/releases/tag/v1.0.0

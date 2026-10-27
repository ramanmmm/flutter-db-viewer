# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

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

[1.0.0]: https://github.com/ramanmmm/flutter-db-viewer/releases/tag/v1.0.0

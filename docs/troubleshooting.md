# Troubleshooting

## The device

**`adb: command not found`** — install Android SDK Platform-Tools and put them
on your `PATH`. With Flutter/Android Studio installed they're usually at
`~/Library/Android/sdk/platform-tools` (macOS) or `~/Android/Sdk/platform-tools`
(Linux).

**`no devices/emulators found`** — `adb devices` should list your device as
`device`. If it says `unauthorized`, unlock the phone and accept the *Allow USB
debugging* prompt.

**`more than one device/emulator`** — pick one: `flutterdb -s emulator-5554`.

## The app

**`run-as: package not debuggable`** — release and profile builds can't be
read. Install a debug build (`flutter run`, `flutter install --debug`).

**`run-as: unknown package`** — the app id flutterdb read from Gradle isn't
installed. Common causes: a flavour with `applicationIdSuffix` (e.g.
`com.acme.app.dev`), or a different device. Find the real id with
`adb shell pm list packages | grep acme` and pass `--package`.

**`No SQLite files found`** — the app hasn't created its database yet (open it
once), or keeps it outside `databases/`, `app_flutter/` and `files/`. Please
open an issue with the path.

**Wrong database shown** — flutterdb opens the largest one that isn't a
Google/Firebase library database. Use the dropdown, or `--db name.db`.

## The viewer

**`not inside a Flutter/Android project`** — run it from the project (any
subfolder works) or pass `--package`.

**Port already in use** — flutterdb tries the next 20 ports automatically. Use
`--port` to start elsewhere.

**First load is slow** — the whole main file is copied once; budget ~3–4 s per
100 MB over USB 3, more over USB 2 or Wi-Fi adb. After that only changes are
copied.

**`adb transfer stalled for 30 s` / `couldn't read … (chunk N)`** — a chunk
didn't arrive within 30 s three times in a row. The next poll retries. If it
keeps happening, replug the cable (avoid hubs), try another USB port, or run
`adb kill-server && adb start-server`.

**SQL: `Only read-only queries run here`** — the console never writes. To try
a change, do it in the app and watch it arrive.

**SQL: `Stopped after 15 s`** — add a `WHERE`/`LIMIT`; **Explain** shows
whether the query scans a big table.

**`Database checkpointed mid-pull, retrying…`** — harmless; the app folded its
WAL into the main file while flutterdb was copying. The next poll picks it up.

**A viewer won't stop** — `flutterdb list` shows its pid; `flutterdb stop --all`
stops every viewer.

**Encrypted database (SQLCipher)** — the file is copied but can't be opened
without the key. Not supported.

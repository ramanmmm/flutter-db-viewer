# Contributing

Thanks for helping! Bug reports, ideas and pull requests are all welcome.

## Ground rules

- **No runtime dependencies.** flutterdb must keep working with a bare
  `python3` (3.9+) and `adb`. Test-only tools are fine if CI installs them.
- **Read-only on the device.** Nothing may ever write to the device or the
  app's files.
- **No build step for the UI.** `index.html` is plain HTML/CSS/JS served as-is.

## Setup

```sh
git clone https://github.com/ramanmmm/flutter-db-viewer
cd flutter-db-viewer
python3 -m unittest discover -s tests -v
python3 -m flutter_db_viewer --demo     # the UI against the demo app
```

No device is needed for the tests: `DirSource` stands in for the phone, and a
WAL-mode database held open by a writer reproduces what a running app does.
To try against a real device, run `python3 -m flutter_db_viewer` from any
Flutter project with a debug build installed.

## Pull requests

1. Open an issue first for anything bigger than a small fix, so we can agree on the approach.
2. Add or update tests — every behaviour change should be covered in `tests/`.
3. Run the suite on the oldest supported Python if you can (`python3.9 -m unittest discover -s tests`).
4. Update `CHANGELOG.md` under an *Unreleased* heading and the README if user-facing behaviour changes.
5. Keep the style of the surrounding code: standard library, type hints, short comments that explain *why*.

## Releasing (maintainers)

1. Bump `__version__` in `flutter_db_viewer/__init__.py` and move *Unreleased* in `CHANGELOG.md` to the new version.
2. Commit, then `git tag vX.Y.Z && git push --tags`.
3. `gh release create vX.Y.Z --notes-from-tag` (or paste the changelog section).

#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Live view of this app's on-device SQLite database (flutterdb).
# https://github.com/ramanmmm/flutter-db-viewer
#
#   ./checkLiveDb.sh          # open the live page (localhost:8765, or next free port)
#   ./checkLiveDb.sh stop     # stop this app's viewer
#   ./checkLiveDb.sh --once   # print tables / rows / columns in the terminal
#
# Drop this file in your Flutter project root. The first run installs
# flutterdb into ~/.local; nothing is added to the project itself.
# Needs adb and the app installed as a debug build on a connected device.
# ---------------------------------------------------------------------------
set -euo pipefail

# Leave empty to read applicationId from android/app/build.gradle(.kts).
PACKAGE=""
# Pin a release for reproducible team setups, e.g. v1.1.1 (or main).
FLUTTERDB_REF="${FLUTTERDB_REF:-v1.1.1}"

cd "$(dirname "${BASH_SOURCE[0]}")"
export PATH="$HOME/.local/bin:$PATH"

install_flutterdb() {
  curl -fsSL "https://raw.githubusercontent.com/ramanmmm/flutter-db-viewer/$FLUTTERDB_REF/install.sh" \
    | FLUTTERDB_REF="$FLUTTERDB_REF" sh
}

if ! command -v flutterdb >/dev/null 2>&1; then
  echo "flutterdb not installed — installing $FLUTTERDB_REF…"
  install_flutterdb
elif [[ "$FLUTTERDB_REF" == v* ]] && [[ "$(flutterdb --version 2>/dev/null)" != "flutterdb ${FLUTTERDB_REF#v}" ]]; then
  echo "Updating flutterdb to $FLUTTERDB_REF…"
  install_flutterdb
fi

args=()
[[ -n "$PACKAGE" ]] && args=(--package "$PACKAGE")

if [[ "${1:-}" == "stop" ]]; then
  shift
  exec flutterdb stop ${args[@]+"${args[@]}"} "$@"
fi

exec flutterdb ${args[@]+"${args[@]}"} "$@"

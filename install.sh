#!/bin/sh
# ---------------------------------------------------------------------------
# flutterdb installer — no pip, no virtualenv, standard library only.
#
#   curl -fsSL https://raw.githubusercontent.com/ramanmmm/flutter-db-viewer/main/install.sh | sh
#   curl -fsSL .../install.sh | FLUTTERDB_REF=v1.0.0 sh     # a specific release
#   sh install.sh --local                                   # from a git clone
#   sh install.sh --uninstall
#
# Installs the package to ~/.local/share/flutterdb and a `flutterdb` launcher
# to ~/.local/bin. Override with FLUTTERDB_HOME / FLUTTERDB_BIN.
# ---------------------------------------------------------------------------
set -eu

REPO="ramanmmm/flutter-db-viewer"
REF="${FLUTTERDB_REF:-main}"
HOME_DIR="${FLUTTERDB_HOME:-$HOME/.local/share/flutterdb}"
BIN_DIR="${FLUTTERDB_BIN:-$HOME/.local/bin}"

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  rm -rf "$HOME_DIR"
  rm -f "$BIN_DIR/flutterdb"
  say "Removed flutterdb."
  exit 0
fi

command -v python3 >/dev/null 2>&1 || die "python3 is required (3.9 or newer)."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
  || die "python3 is $(python3 -V 2>&1 | cut -d' ' -f2); flutterdb needs 3.9 or newer."

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [ "${1:-}" = "--local" ]; then
  SRC="$(cd "$(dirname "$0")" && pwd)"
  [ -f "$SRC/flutter_db_viewer/viewer.py" ] || die "--local must be run from a flutter-db-viewer checkout."
  say "Installing from $SRC"
else
  URL="https://codeload.github.com/$REPO/tar.gz/$REF"
  say "Downloading $REPO@$REF"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$TMP/src.tar.gz" || die "download failed: $URL"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$TMP/src.tar.gz" "$URL" || die "download failed: $URL"
  else
    die "curl or wget is required."
  fi
  tar -xzf "$TMP/src.tar.gz" -C "$TMP"
  SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  [ -f "$SRC/flutter_db_viewer/viewer.py" ] || die "unexpected archive layout."
fi

mkdir -p "$HOME_DIR" "$BIN_DIR"
rm -rf "$HOME_DIR/flutter_db_viewer"
cp -R "$SRC/flutter_db_viewer" "$HOME_DIR/flutter_db_viewer"
find "$HOME_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +

cat > "$BIN_DIR/flutterdb" <<EOF
#!/bin/sh
# flutterdb launcher — https://github.com/$REPO
PYTHONPATH="$HOME_DIR\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m flutter_db_viewer "\$@"
EOF
chmod +x "$BIN_DIR/flutterdb"

VERSION="$("$BIN_DIR/flutterdb" --version 2>/dev/null || true)"
[ -n "$VERSION" ] || die "installed, but '$BIN_DIR/flutterdb --version' failed."
say "Installed $VERSION → $BIN_DIR/flutterdb"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "Add $BIN_DIR to your PATH:  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc" ;;
esac
command -v adb >/dev/null 2>&1 \
  || say "adb not found on PATH — install Android platform-tools to read a device (or try: flutterdb --demo)."
say "Try it:  cd <your-flutter-app> && flutterdb     (or: flutterdb --demo)"

#!/data/data/com.termux/files/usr/bin/bash
# ---------------------------------------------------------------------------
# Uninstall the colab-mcp stack installed by install.sh.
#
# Removes:
#   - python packages recorded by install.sh (uninstall manifest); for
#     installs made before the manifest existed, a conservative curated
#     list of packages unique to this stack
#   - launchers and the install dir
#
# Keeps by default:
#   - OAuth token + session (~/.config/colab-exec)  -> --purge to remove
#   - shared Termux packages (python-rpds-py, python-cryptography)
#   - build toolchain (rust, clang, cmake, maturin) -> --toolchain to remove
#
# Usage:
#   bash uninstall.sh               # standard uninstall
#   bash uninstall.sh --purge       # also delete ~/.config/colab-exec
#   bash uninstall.sh --toolchain   # also remove rust/clang/cmake/maturin
#   bash uninstall.sh --purge --toolchain
# ---------------------------------------------------------------------------
set -euo pipefail

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }

PX="${PREFIX:-/data/data/com.termux/files/usr}"
INSTALL_DIR="${COLAB_MCP_DIR:-$HOME/.local/share/colab-mcp}"
MANIFEST="$INSTALL_DIR/installed-packages.txt"

PURGE=0
TOOLCHAIN=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --toolchain) TOOLCHAIN=1 ;;
    *) warn "Unknown option: $arg (ignored)" ;;
  esac
done

[ -x "$PX/bin/pkg" ] || { echo "This script targets Termux (PREFIX=$PX not found)." >&2; exit 1; }

# --- 1. python packages -----------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  warn "uv not found; cannot remove python packages (pkg install uv)."
elif [ -f "$MANIFEST" ]; then
  COUNT="$(wc -l < "$MANIFEST")"
  say "Removing $COUNT package(s) recorded at install time"
  while read -r pkg; do
    [ -z "$pkg" ] && continue
    uv pip uninstall --system "$pkg" >/dev/null 2>&1 \
      || warn "not installed (skipped): $pkg"
  done < "$MANIFEST"
else
  say "No install manifest found - using the curated package list"
  for pkg in mcp-server-colab-exec mcp mcp-types pydantic pydantic-core \
             pydantic-settings annotated-types annotated-doc \
             typing-inspection sse-starlette python-multipart \
             websocket-client google-auth google-auth-oauthlib pyasn1 \
             pyasn1-modules pyjwt requests-oauthlib oauthlib starlette \
             uvicorn httpx-sse truststore; do
    uv pip uninstall --system "$pkg" >/dev/null 2>&1 || true
  done
  warn "Packages shared with other tools were kept (anyio, httpx, h11,"
  warn "httpcore, rich, typer, jsonschema, requests, ...). Remove those"
  warn "manually if nothing else on the system uses them."
fi

# --- 2. launchers + manifest -------------------------------------------------
say "Removing launchers from $INSTALL_DIR"
rm -rf "$INSTALL_DIR"

# --- 3. credentials (optional) ----------------------------------------------
if [ "$PURGE" = 1 ]; then
  say "Removing ~/.config/colab-exec (token, session, env snapshot)"
  rm -rf "$HOME/.config/colab-exec"
elif [ -e "$HOME/.config/colab-exec" ]; then
  warn "Kept ~/.config/colab-exec (cached OAuth token - avoids re-consent)."
  warn "Re-run with --purge to delete it."
fi

# --- 4. toolchain (optional) -------------------------------------------------
if [ "$TOOLCHAIN" = 1 ]; then
  say "Removing build toolchain (rust, clang, cmake, make, binutils, maturin)"
  pkg uninstall -y rust clang cmake make binutils >/dev/null 2>&1 || true
  rm -f "$HOME/.cargo/bin/maturin"
  rmdir "$HOME/.cargo/bin" "$HOME/.cargo" 2>/dev/null || true
fi

printf '\n\033[1;32mUninstall complete.\033[0m\n'
warn "Kept shared Termux packages: python-rpds-py, python-cryptography (pkg)."

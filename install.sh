#!/data/data/com.termux/files/usr/bin/bash
# ---------------------------------------------------------------------------
# Google Colab MCP server on Termux (Android).
#
# Installs everything needed to run the colab-exec MCP server on a phone:
#   base packages -> pydantic-core (prebuilt wheel when available, else
#   source build via maturin) -> MCP server (mcp 1.x) -> launchers.
#
# Usage:
#   bash install.sh                              # wheel fast path, else source
#   COLAB_MCP_LOCAL_WHEEL=x.whl bash install.sh  # force a specific local wheel
#   PDC_VERSION=2.46.5 bash install.sh           # pin pydantic-core (source)
#   CARGO_BUILD_JOBS=1 bash install.sh           # lower RAM for small phones
#   COLAB_MCP_WHEEL_OUT=dir bash install.sh      # keep the built wheel
#
# Prebuilt wheels for Termux aarch64 are produced by CI
# (.github/workflows/termux-wheels.yml) and published to GitHub Releases;
# install.sh downloads the one matching your Python and pydantic version.
#
# Tested on Termux, aarch64, Python 3.14. No root required.
# ---------------------------------------------------------------------------
set -euo pipefail

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

PX="${PREFIX:-/data/data/com.termux/files/usr}"
INSTALL_DIR="${COLAB_MCP_DIR:-$HOME/.local/share/colab-mcp}"
JOBS="${CARGO_BUILD_JOBS:-2}"
REPO="${COLAB_MCP_REPO:-bd-loser/colab-mcp-termux}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# --- sanity ---------------------------------------------------------------
[ -x "$PX/bin/pkg" ] || die "This script targets Termux (PREFIX=$PX not found)."
command -v python3 >/dev/null || die "python3 missing; run: pkg install python"

# --- 1. base system packages ----------------------------------------------
say "Installing base packages (python, uv, rpds-py, cryptography)"
pkg update -y >/dev/null
pkg install -y python python-pip uv python-rpds-py python-cryptography \
  curl git unzip tar >/dev/null
command -v uv >/dev/null || die "uv not found after install (pkg install uv)"

# --- 2. pydantic-core (Rust) matching the current pydantic -----------------
# Fast path: prebuilt Termux wheel (COLAB_MCP_LOCAL_WHEEL, ./dist/, or the
# latest GitHub release of this repo). Slow path: source build — rust +
# maturin + pydantic-core, 15-40 minutes on a phone.
if [ -z "${PDC_VERSION:-}" ] || [ -z "${PYDANTIC_VERSION:-}" ]; then
  _VERSIONS="$(curl -fsSL https://pypi.org/pypi/pydantic/json | python3 -c \
    'import json,sys
d = json.load(sys.stdin)
line = next(r for r in d["info"]["requires_dist"] if r.startswith("pydantic-core=="))
print(d["info"]["version"], line.split("==")[1].split(";")[0].strip())')"
  if [ -z "${PYDANTIC_VERSION:-}" ]; then PYDANTIC_VERSION="${_VERSIONS%% *}"; fi
  if [ -z "${PDC_VERSION:-}" ]; then PDC_VERSION="${_VERSIONS##* }"; fi
fi
say "pydantic $PYDANTIC_VERSION requires pydantic-core $PDC_VERSION"

PY_TAG="$(python3 -c 'import sys; v=sys.version_info; print(f"cp{v.major}{v.minor}")' 2>/dev/null || true)"

wheel_matches() {
  case "$1" in
    pydantic_core-"$PDC_VERSION"-"$PY_TAG"-"$PY_TAG"-android*_arm64_v8a.whl) return 0 ;;
    *) return 1 ;;
  esac
}

if python3 -c "import pydantic_core,sys; sys.exit(0 if pydantic_core.__version__=='$PDC_VERSION' else 1)" 2>/dev/null; then
  say "pydantic-core $PDC_VERSION already installed - skipping"
else
  WHEEL=""
  for cand in ${COLAB_MCP_LOCAL_WHEEL:-} dist/pydantic_core-*.whl; do
    if [ -f "$cand" ] && wheel_matches "$(basename "$cand")"; then
      WHEEL="$cand"; break
    fi
  done
  if [ -z "$WHEEL" ] && [ -n "$PY_TAG" ]; then
    WHEEL_URL="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null | python3 -c '
      import json, sys
      want, tag = sys.argv[1], sys.argv[2]
      try:
          assets = json.load(sys.stdin).get("assets", [])
      except Exception:
          assets = []
      for a in assets:
          n = a["name"]
          if (n.startswith("pydantic_core-" + want + "-")
                  and ("-" + tag + "-" + tag + "-") in n
                  and "android" in n and "arm64_v8a" in n):
              print(a["browser_download_url"])
              break
    ' "$PDC_VERSION" "$PY_TAG" || true)"
    if [ -n "$WHEEL_URL" ]; then
      DL="$(mktemp -d)"
      if curl -fsSL "$WHEEL_URL" -o "$DL/pydantic_core.whl" \
         && wheel_matches "$(basename "$WHEEL_URL")"; then
        WHEEL="$DL/pydantic_core.whl"
        say "Downloaded prebuilt wheel: $(basename "$WHEEL_URL")"
      fi
    fi
  fi

  if [ -n "$WHEEL" ]; then
    say "Installing prebuilt pydantic-core wheel (fast path)"
    uv pip install --system "$WHEEL"
  else
    say "No prebuilt wheel for pydantic-core $PDC_VERSION ($PY_TAG) - building from source"
    say "Installing build toolchain (rust, clang, cmake)"
    pkg install -y rust clang cmake make binutils >/dev/null

    MATURIN="$(command -v maturin || true)"
    [ -z "$MATURIN" ] && [ -x "$HOME/.cargo/bin/maturin" ] && MATURIN="$HOME/.cargo/bin/maturin"
    if [ -z "$MATURIN" ]; then
      say "Building maturin from source (this is the long step: ~5-20 min)"
      CARGO_BUILD_JOBS="$JOBS" cargo install maturin --locked
      MATURIN="$HOME/.cargo/bin/maturin"
    fi
    say "maturin: $("$MATURIN" --version)"

    WORK="$(mktemp -d)"
    trap 'rm -rf "$WORK"' EXIT
    SRC_URL="$(curl -fsSL "https://pypi.org/pypi/pydantic-core/$PDC_VERSION/json" | python3 -c \
      'import json,sys; d=json.load(sys.stdin); print(next(u["url"] for u in d["urls"] if u["packagetype"]=="sdist"))')"
    say "Downloading sdist: $SRC_URL"
    curl -fsSL "$SRC_URL" -o "$WORK/pydantic_core.tar.gz"
    tar xzf "$WORK/pydantic_core.tar.gz" -C "$WORK"
    (
      cd "$WORK/pydantic-core-$PDC_VERSION"
      # pydantic-core's release profile uses fat LTO + codegen-units=1, which
      # exhausts RAM on phones. Thin LTO + more units trades a little speed
      # for a build that actually completes.
      export CARGO_BUILD_JOBS="$JOBS"
      export CARGO_PROFILE_RELEASE_LTO=thin
      export CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16
      export CARGO_PROFILE_RELEASE_STRIP=true
      say "Compiling pydantic-core (thin LTO, ~10-25 min)"
      "$MATURIN" build --release --out "$WORK/dist" -i python3
    )
    uv pip install --system "$WORK"/dist/pydantic_core-*.whl
    if [ -n "${COLAB_MCP_WHEEL_OUT:-}" ]; then
      mkdir -p "$COLAB_MCP_WHEEL_OUT"
      cp "$WORK"/dist/pydantic_core-*.whl "$COLAB_MCP_WHEEL_OUT"/
    fi
    trap - EXIT
    rm -rf "$WORK"
  fi
  python3 -c "import pydantic_core; print('pydantic-core', pydantic_core.__version__)"
fi

[ -n "$PYDANTIC_VERSION" ] || die "could not determine pydantic version"

# --- 3. the MCP server (must use the mcp 1.x FastMCP API) -------------------
say "Installing mcp-server-colab-exec with mcp<2"
uv pip install --system mcp-server-colab-exec "mcp[cli]<2" "pydantic==$PYDANTIC_VERSION"

# --- 4. launchers ------------------------------------------------------------
say "Installing launchers to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/scripts/colab_mcp_dns.py" "$INSTALL_DIR/colab_mcp_dns.py"
cp "$SCRIPT_DIR/scripts/colab_persistent.py" "$INSTALL_DIR/colab_persistent.py"
chmod +x "$INSTALL_DIR/colab_mcp_dns.py" "$INSTALL_DIR/colab_persistent.py"

cat <<EOF

\033[1;32mInstall complete.\033[0m

Next steps
----------
1) Authenticate with Google once (opens a browser for Colab consent):

   python3 -c "from mcp_server_colab_exec.colab_runtime import get_credentials as g; g()"

   The token is cached at ~/.config/colab-exec/token.json.

2) Point your MCP client at the persistent launcher (recommended):

   command:   python3
   args:      $INSTALL_DIR/colab_persistent.py

   This keeps one Colab runtime + kernel warm across tool calls and
   exposes 23 tools (see docs/TOOLS.md). For the plain DNS-patched
   upstream server (3 tools, runtime released after every call) use
   $INSTALL_DIR/colab_mcp_dns.py instead.

   Example opencode config: examples/opencode.mcp.json
   Example result:          scripts/verify.sh  (allocates a free T4 and prints the GPU)
   Launcher tests:          python3 tests/test_colab_persistent.py

Docs: docs/TOOLS.md, docs/ARCHITECTURE.md, docs/TROUBLESHOOTING.md, docs/BUILD-FROM-SOURCE.md
EOF

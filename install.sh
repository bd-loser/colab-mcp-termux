#!/data/data/com.termux/files/usr/bin/bash
# ---------------------------------------------------------------------------
# Google Colab MCP server on Termux (Android) - complete source build.
#
# Installs everything needed to run mcp-server-colab-exec on a phone:
#   system packages -> maturin (Rust) -> pydantic-core (Rust) ->
#   MCP server (mcp 1.x) -> DNS-over-HTTPS wrapper.
#
# Usage:
#   bash install.sh                  # full install
#   PDC_VERSION=2.46.5 bash install.sh   # pin pydantic-core version
#   CARGO_BUILD_JOBS=1 bash install.sh   # lower RAM for very small phones
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
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# --- sanity ---------------------------------------------------------------
[ -x "$PX/bin/pkg" ] || die "This script targets Termux (PREFIX=$PX not found)."
command -v python3 >/dev/null || die "python3 missing; run: pkg install python"

# --- 1. system packages ---------------------------------------------------
say "Installing system packages (python, uv, rust, clang, cmake, rpds-py, cryptography)"
pkg update -y >/dev/null
pkg install -y python python-pip uv rust clang cmake make binutils \
  python-rpds-py python-cryptography curl git unzip tar >/dev/null

command -v uv >/dev/null || die "uv not found after install (pkg install uv)"

# --- 2. maturin (Rust build tool for Python wheels) -----------------------
MATURIN="$(command -v maturin || true)"
[ -z "$MATURIN" ] && [ -x "$HOME/.cargo/bin/maturin" ] && MATURIN="$HOME/.cargo/bin/maturin"
if [ -z "$MATURIN" ]; then
  say "Building maturin from source (this is the long step: ~5-20 min)"
  CARGO_BUILD_JOBS="$JOBS" cargo install maturin --locked
  MATURIN="$HOME/.cargo/bin/maturin"
fi
say "maturin: $("$MATURIN" --version)"

# --- 3. pydantic-core (Rust) matching the current pydantic ----------------
if [ -z "${PDC_VERSION:-}" ]; then
  PDC_VERSION="$(curl -fsSL https://pypi.org/pypi/pydantic/json | python3 -c \
    'import json,sys; d=json.load(sys.stdin); print(next(r.split("==")[1] for r in d["info"]["requires_dist"] if r.startswith("pydantic-core==")))')"
fi
say "pydantic-core version required by pydantic: $PDC_VERSION"

if python3 -c "import pydantic_core,sys; sys.exit(0 if pydantic_core.__version__=='$PDC_VERSION' else 1)" 2>/dev/null; then
  say "pydantic-core $PDC_VERSION already installed - skipping build"
else
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
  trap - EXIT
  rm -rf "$WORK"
  python3 -c "import pydantic_core; print('pydantic-core', pydantic_core.__version__)"
fi

# --- 4. the MCP server (must use the mcp 1.x FastMCP API) -----------------
say "Installing mcp-server-colab-exec with mcp<2"
uv pip install --system mcp-server-colab-exec "mcp[cli]<2"

# --- 5. launchers (DNS wrapper + persistent warm kernel) -------------------
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

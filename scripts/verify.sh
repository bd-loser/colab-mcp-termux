#!/data/data/com.termux/files/usr/bin/bash
# Allocate a free Colab T4 and print its GPU name. Proves the whole chain:
# OAuth token -> runtime allocation -> Jupyter kernel -> code execution.
set -euo pipefail

INSTALL_DIR="${COLAB_MCP_DIR:-$HOME/.local/share/colab-mcp}"
WRAPPER="$INSTALL_DIR/colab_mcp_dns.py"
[ -f "$WRAPPER" ] || WRAPPER="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/colab_mcp_dns.py"

python3 - "$WRAPPER" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("dns_wrap", sys.argv[1])
dns = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dns)
dns.install()

from mcp_server_colab_exec.server import colab_execute
code = (
    "import sys, torch\n"
    "print('python', sys.version.split()[0])\n"
    "print('torch', torch.__version__)\n"
    "print('cuda', torch.cuda.is_available())\n"
    "print('gpu', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none')"
)
print(colab_execute(code, accelerator="T4", timeout=300))
PY

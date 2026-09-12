#!/data/data/com.termux/files/usr/bin/bash
# Interactive Google OAuth for Colab. Google's flow prints an authorization
# URL; on a phone, pasting it is painful, so we open it directly in the
# browser and wait for the redirect to localhost to complete.
set -euo pipefail

LOG="$(mktemp)"
cleanup() { rm -f "$LOG"; }
trap cleanup EXIT

open_url() {
  local url="$1"
  if command -v termux-open-url >/dev/null 2>&1; then
    termux-open-url "$url"
  elif command -v am >/dev/null 2>&1; then
    am start -a android.intent.action.VIEW -d "$url" >/dev/null 2>&1 || true
  fi
}

# BROWSER=echo makes Python's webbrowser "open" the URL by echoing it, so we
# can capture it instead of relying on a desktop browser launcher.
BROWSER=echo python3 -c \
  "from mcp_server_colab_exec.colab_runtime import get_credentials; get_credentials()" \
  >"$LOG" 2>&1 &
PID=$!

URL=""
for _ in $(seq 1 10); do
  sleep 1
  URL="$(grep -oE 'https://accounts\.google\.com[^[:space:]]+' "$LOG" | head -1 || true)"
  [ -n "$URL" ] && break
done

if [ -n "$URL" ]; then
  echo "Opening consent page in your browser..."
  open_url "$URL"
  echo "If it did not open, paste this URL manually:"
  echo "$URL"
else
  echo "Waiting for OAuth (no URL captured yet)..."
fi

wait "$PID"
echo "Authentication complete. Token cached at ~/.config/colab-exec/token.json"

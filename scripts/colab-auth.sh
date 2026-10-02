#!/data/data/com.termux/files/usr/bin/bash
# Google OAuth for Colab.
#
# Uses the standard loopback flow (run_local_server). Android's browser shares
# the device loopback interface, so the 127.0.0.1:<port> callback does reach
# Termux. The only thing that has to be fixed is URL delivery: webbrowser.open()
# finds no desktop browser on Termux, so the consent page is never shown and the
# flow just hangs. We patch it to hand the URL to the Android browser.
#
# Note: google-auth-oauthlib >= 1.0 removed run_console(), and Google blocked
# the OOB flow anyway, so loopback is the right mechanism here.
set -euo pipefail

python3 - <<'PY'
import subprocess
import webbrowser


def _open(url):
    """Hand the consent URL to the Android browser. Never block on it."""
    print(f"\nOpen this URL in your browser:\n{url}\n")
    for cmd in (["termux-open-url", url],
                ["am", "start", "-a", "android.intent.action.VIEW", "-d", url]):
        try:
            subprocess.run(cmd, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            break
        except (FileNotFoundError, OSError):
            continue
    return True


webbrowser.open = _open
webbrowser.open_new = _open
webbrowser.open_new_tab = _open

from mcp_server_colab_exec.colab_runtime import get_credentials

creds = get_credentials()

print("\nAuthentication complete. Token cached at ~/.config/colab-exec/token.json")
print(f"Valid: {creds.valid}  Expires: {getattr(creds, 'expiry', 'unknown')}")
PY

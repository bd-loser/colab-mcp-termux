#!/data/data/com.termux/files/usr/bin/python3
"""colab_cli_bionic — full CLI for the Colab MCP stack (no MCP client needed).

  auth login [--select|--email a@b.c]   OAuth with explicit account chooser
  auth whoami | auth status | auth logout
  runtime status | runtime release | runtime restart
  run "code" | run -f file.py [--cpu]
  det start -f job.py [-n name] [--respawn local::/remote,..] [--max 6]
  det status | det stop
  push LOCAL /remote   |   pull /remote LOCAL
  events [N] | kernels | info | busy
  raw TOOL_NAME '{"json":"args"}'

Reuses the persistent warm-session client (colab_persistent) and the same
token cache (~/.config/colab-exec/token.json) as the MCP server.
"""
import argparse
import base64
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TOKEN_PATH = os.path.expanduser("~/.config/colab-exec/token.json")


def _boot():
    """Install the persistent server patches and return the TOOLS registry."""
    import colab_mcp_dns
    import colab_persistent
    colab_mcp_dns.install()
    colab_persistent.install()
    return colab_persistent.TOOLS


def _p(obj):
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except Exception:
            pass
    print(json.dumps(obj, indent=2) if not isinstance(obj, str) else obj)


# ── auth ─────────────────────────────────────────────────────────────────────

def _load_creds():
    from google.oauth2.credentials import Credentials
    if not os.path.exists(TOKEN_PATH):
        raise SystemExit("not signed in — run: colab-cli auth login")
    return Credentials.from_authorized_user_file(TOKEN_PATH)


def _email_from_token(tok):
    idt = tok.get("id_token")
    if idt:
        try:
            part = idt.split(".")[1]
            part += "=" * (-len(part) % 4)
            return json.loads(base64.urlsafe_b64decode(part)).get("email")
        except Exception:
            pass
    return None


def _email_from_access(at):
    try:
        r = urllib.request.urlopen(
            "https://oauth2.googleapis.com/tokeninfo?access_token="
            + urllib.parse.quote(at), timeout=15)
        return json.load(r).get("email")
    except Exception:
        return None


def _open_browser(url):
    import subprocess
    import webbrowser

    def _open(u):
        print(f"\nOpen in your browser:\n{u}\n")
        for cmd in (["termux-open-url", u],
                    ["am", "start", "-a", "android.intent.action.VIEW",
                     "-d", u]):
            try:
                subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
                break
            except (FileNotFoundError, OSError):
                continue
        return True
    webbrowser.open = webbrowser.open_new = webbrowser.open_new_tab = _open

    class _Shim:
        def open(self, url, new=0, autoraise=True):
            return _open(url)

    def _get(*a, **k):
        return _Shim()
    webbrowser.get = _get


def cmd_auth(args):
    if args.sub == "status":
        tok = json.load(open(TOKEN_PATH)) if os.path.exists(TOKEN_PATH) else {}
        creds = _load_creds() if tok else None
        _p({"signed_in": bool(tok), "account": _email_from_token(tok),
            "valid": bool(creds and creds.valid),
            "expires": str(getattr(creds, "expiry", None))})
    elif args.sub == "whoami":
        creds = _load_creds()
        if not creds.valid and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
        print(_email_from_access(creds.token) or "unknown")
    elif args.sub == "logout":
        tok = json.load(open(TOKEN_PATH)) if os.path.exists(TOKEN_PATH) else {}
        for key in ("token", "refresh_token"):
            if tok.get(key):
                try:
                    urllib.request.urlopen(urllib.request.Request(
                        "https://oauth2.googleapis.com/revoke?token="
                        + tok[key]), timeout=15)
                except Exception:
                    pass
        for f in (TOKEN_PATH,
                  os.path.expanduser("~/.config/colab-exec/session.json")):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass
        _p({"logged_out": True})
    elif args.sub == "login":
        # Release any runtime held under the CURRENT token first, so switching
        # accounts never leaves a zombie GPU assignment on the old account.
        if not args.no_release:
            try:
                _p(json.loads(_boot()["colab_kernel_reset"]()))
            except SystemExit:
                pass
        _open_browser(None)
        from mcp_server_colab_exec.colab_runtime import CLIENT_CONFIG, SCOPES
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_config(CLIENT_CONFIG, SCOPES)
        kwargs = {}
        if args.email:
            kwargs = {"login_hint": args.email, "email": args.email}
        if args.select or not args.email:
            kwargs["prompt"] = "select_account"   # force the account chooser
        os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
        creds = flow.run_local_server(port=0, **kwargs)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
        print(f"signed in as: "
              f"{_email_from_access(creds.token) or '(unknown — check whoami)'}")


# ── everything else rides on the warm-session tools ─────────────────────────

def main():
    ap = argparse.ArgumentParser(prog="colab-cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auth")
    a.add_argument("sub", choices=["login", "whoami", "status", "logout"])
    a.add_argument("--email")
    a.add_argument("--select", action="store_true")
    a.add_argument("--no-release", action="store_true")

    r = sub.add_parser("runtime")
    r.add_argument("sub", choices=["status", "release", "restart"])

    x = sub.add_parser("run")
    x.add_argument("code", nargs="?")
    x.add_argument("-f", "--file")
    x.add_argument("--cpu", action="store_true")
    x.add_argument("-t", "--timeout", type=int, default=300)

    d = sub.add_parser("det")
    d.add_argument("sub", choices=["start", "status", "stop"])
    d.add_argument("-f", "--file")
    d.add_argument("-n", "--name", default="job")
    d.add_argument("--respawn", default="",
                   help="local::/remote pairs, comma separated")
    d.add_argument("--max", type=int, default=0)
    d.add_argument("--poll", type=int, default=20)
    d.add_argument("--force", action="store_true")
    d.add_argument("--cpu", action="store_true")

    u = sub.add_parser("push")
    u.add_argument("local")
    u.add_argument("remote")

    w = sub.add_parser("pull")
    w.add_argument("remote")
    w.add_argument("local")

    e = sub.add_parser("events")
    e.add_argument("n", nargs="?", type=int, default=20)

    sub.add_parser("kernels")
    sub.add_parser("info")
    sub.add_parser("busy")

    raw = sub.add_parser("raw")
    raw.add_argument("tool")
    raw.add_argument("json_args", nargs="?", default="{}")

    args = ap.parse_args()

    if args.cmd == "auth":
        cmd_auth(args)
        return
    if args.cmd == "det" and args.sub == "stop":
        _p(_boot()["colab_watchdog_stop"]())
        return

    T = _boot()
    acc = "CPU" if getattr(args, "cpu", False) else "T4"
    if args.cmd == "runtime":
        fn = {"status": "colab_kernel_info",
              "release": "colab_kernel_reset",
              "restart": "colab_kernel_restart"}[args.sub]
        _p(T[fn]())
    elif args.cmd == "run":
        code = open(args.file).read() if args.file else args.code
        if not code:
            raise SystemExit("give code or -f file")
        _p(T["colab_execute"](code=code, accelerator=acc,
                              timeout=args.timeout))
    elif args.cmd == "det":
        if args.sub == "start":
            if not args.file:
                raise SystemExit("det start needs -f job.py")
            _p(T["colab_execute_detached"](
                code=open(args.file).read(), job=args.name,
                accelerator=acc, respawn_files=args.respawn,
                respawn_max=args.max, respawn_poll=args.poll,
                force=args.force))
        else:
            _p(T["colab_job_status"]())
    elif args.cmd == "push":
        _p(T["colab_upload"](file_path=args.local, remote_path=args.remote))
    elif args.cmd == "pull":
        _p(T["colab_download"](remote_path=args.remote,
                               file_path=args.local))
    elif args.cmd == "events":
        _p(T["colab_events"](n=args.n))
    elif args.cmd == "kernels":
        _p(T["colab_kernel_list"]())
    elif args.cmd == "info":
        _p(T["colab_kernel_info"]())
    elif args.cmd == "busy":
        _p(T["colab_kernel_busy"]())
    elif args.cmd == "raw":
        _p(T[args.tool](**json.loads(args.json_args)))


if __name__ == "__main__":
    main()

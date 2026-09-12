#!/usr/bin/env python3
"""Persistent-kernel launcher for the Colab MCP server.

Layers, all applied before the stdio server starts:
1. DNS fix from colab_mcp_dns (IPv4 preference + DoH fallback).
2. server._run_on_colab replacement: ONE Colab runtime + named Jupyter
   kernels kept warm across tool calls (no unassign in finally). Session
   persisted to ~/.config/colab-exec/session.json and resumed after a
   process restart. Busy-state preflight, kernel-level recovery ladder.
3. Control tools: execute (warm), kernel_reset/restart/interrupt/info/busy,
   namespace/inspect/check_syntax (kernel protocol), upload/download,
   env_snapshot/env_restore, expose/expose_status (cloudflared),
   execute_detached/job_status (live tail), kernel_new/list/use/close,
   kernels_prune.

Holding the runtime between calls is equivalent to keeping a notebook open
in a browser; free-tier session limits and quotas still apply.
"""
import ast
import base64
import json
import os
import sys
import threading
import time
import uuid

import colab_mcp_dns

SESSION_DIR = os.path.expanduser("~/.config/colab-exec")
SESSION_PATH = os.path.join(SESSION_DIR, "session.json")
ENV_SNAPSHOT_PATH = os.path.join(SESSION_DIR, "env_snapshot.txt")
BUSY_WAIT_DEFAULT = 10
UPLOAD_MAX_BYTES = 25 * 1024 * 1024
UPLOAD_FALLBACK_MAX = 8 * 1024 * 1024
REMOTE_PREFIXES = ("/content", "/tmp")

# Module-level so callers (tests, batteries) can introspect the warm session.
state = {}

JOB_SCRIPT = '''
import json as _json, threading as _threading, traceback as _tb, time as _time
import io as _io, contextlib as _ctx

_job_file = "/content/job.json"
_status = [{"job": %(job)r, "state": "running", "started": _time.time(),
            "ended": None, "stdout_tail": None, "error": None}]
_buf = _io.StringIO()

def _write_status():
    try:
        import os as _os
        with open(_job_file + ".tmp", "w") as _f:
            _json.dump(_status[0], _f)
        _os.replace(_job_file + ".tmp", _job_file)
    except Exception:
        pass

def _run_job():
    _write_status()
    _g = globals()
    try:
        with _ctx.redirect_stdout(_buf), _ctx.redirect_stderr(_buf):
            exec(compile(%(code)r, %(job)r, "exec"), _g, _g)
        _status[0]["state"] = "done"
    except BaseException as _e:
        _status[0]["state"] = "error"
        _status[0]["error"] = "".join(_tb.format_exception_only(type(_e), _e)).strip()
    _status[0]["stdout_tail"] = _buf.getvalue()[-4000:]
    _status[0]["ended"] = _time.time()
    _write_status()

def _monitor(_stop):
    while not _stop.is_set():
        _stop.wait(2)
        if _status[0]["state"] != "running":
            return
        _snap = dict(_status[0])
        _snap["stdout_tail"] = _buf.getvalue()[-4000:]
        _status[0].update(_snap)
        _write_status()

_stop = _threading.Event()
_threading.Thread(target=_monitor, args=(_stop,), daemon=True).start()
_t = _threading.Thread(target=_run_job, name=%(job)r, daemon=True)
_t.start()
print("[detached] started", %(job)r)
'''

STATUS_SCRIPT = '''
import json as _json, os as _os
_p = "/content/job.json"
if _os.path.exists(_p):
    with open(_p) as _f:
        print(_json.dumps(_json.load(_f)))
else:
    print(_json.dumps({"state": "unknown", "error": "no job.json"}))
'''

EXPOSE_SCRIPT = '''
import json, pathlib, re, subprocess, time, urllib.request
_port = %(port)d
_tag = f"tunnel --url http://127.0.0.1:{_port}"
try:
    subprocess.run(["pkill", "-f", _tag], capture_output=True)
except FileNotFoundError:
    pass
_binary = pathlib.Path("/content/cloudflared")
if not _binary.exists():
    urllib.request.urlretrieve(
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
        _binary)
    _binary.chmod(0o755)
_log = open(f"/content/tunnel_{_port}.log", "w")
_p = subprocess.Popen([str(_binary), "tunnel", "--url",
                       f"http://127.0.0.1:{_port}"],
                      stdout=_log, stderr=subprocess.STDOUT)
_url = None
for _ in range(%(wait_cycles)d):
    if _p.poll() is not None:
        raise RuntimeError(f"tunnel died; see /content/tunnel_{_port}.log")
    _txt = pathlib.Path(f"/content/tunnel_{_port}.log").read_text()
    _m = re.search(r"https://[a-z0-9-]+\\.trycloudflare\\.com", _txt)
    if _m:
        _url = _m.group(0)
        break
    time.sleep(2)
if not _url:
    raise RuntimeError(f"tunnel URL not found; see /content/tunnel_{_port}.log")
json.dump({"port": _port, "url": _url, "pid": _p.pid},
          open(f"/content/exposed_{_port}.json", "w"))
print(_url)
'''

EXPOSE_STATUS_SCRIPT = '''
import json, os, signal
_port = %(port)d
_f = f"/content/exposed_{_port}.json"
if not os.path.exists(_f):
    print(json.dumps({"alive": False, "reason": "not exposed"}))
else:
    _d = json.load(open(_f))
    try:
        os.kill(_d["pid"], 0)
        _d["alive"] = True
    except OSError:
        _d["alive"] = False
    print(json.dumps(_d))
'''

FREEZE_SCRIPT = '''
import subprocess, sys
print(sys.version.split()[0])
print(subprocess.run([sys.executable, "-m", "pip", "freeze"],
                     capture_output=True, text=True).stdout)
'''


def _ws_connect(url, headers, timeout):
    import websocket
    return websocket.create_connection(url, header=headers, timeout=timeout)


def install():
    import requests
    from mcp_server_colab_exec import colab_runtime as cr
    from mcp_server_colab_exec import server as srv

    lock = threading.Lock()

    # -- session persistence -------------------------------------------------

    def _save_session():
        try:
            os.makedirs(SESSION_DIR, exist_ok=True)
            data = {
                "endpoint": state.get("endpoint"),
                "proxy_url": state.get("proxy_url"),
                "proxy_token": state.get("proxy_token"),
                "accelerator": state.get("accelerator"),
                "kernels": state.get("kernels", {}),
                "active": state.get("active"),
            }
            tmp = SESSION_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, SESSION_PATH)
            os.chmod(SESSION_PATH, 0o600)
        except Exception as exc:
            print(f"[persist] session save failed: {exc}", file=sys.stderr)

    def _clear_session_file():
        try:
            if os.path.exists(SESSION_PATH):
                os.remove(SESSION_PATH)
        except OSError:
            pass

    def _proxy_headers(token=None):
        headers = {
            "X-Colab-Runtime-Proxy-Token": state["proxy_token"],
            "X-Colab-Client-Agent": "vscode",
        }
        return headers

    def _kernel_model(kernel_id):
        try:
            r = requests.get(f"{state['proxy_url']}/api/kernels/{kernel_id}",
                             headers=_proxy_headers(), timeout=20)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    def _kernel_alive(kernel_id):
        return _kernel_model(kernel_id) is not None

    def _sessions():
        try:
            r = requests.get(f"{state['proxy_url']}/api/sessions",
                             headers=_proxy_headers(), timeout=20)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return []

    def _wait_idle(kernel_id, wait_s):
        """Raise if the kernel is busy with another cell for too long."""
        deadline = time.time() + max(wait_s, 0)
        while True:
            model = _kernel_model(kernel_id)
            if model is None:
                return
            st = model.get("execution_state")
            if st != "busy":
                return  # idle, starting (boot), dead, or unknown: don't block
            if time.time() >= deadline:
                raise RuntimeError(
                    f"kernel busy for over {wait_s}s; another cell is running. "
                    "Use colab_interrupt, colab_kernel_busy, or colab_kernel_new.")
            time.sleep(1)

    def _resume_session():
        try:
            with open(SESSION_PATH) as f:
                saved = json.load(f)
        except Exception:
            return
        if not all(saved.get(k) for k in ("endpoint", "proxy_url", "proxy_token")):
            _clear_session_file()
            return
        kernels = {n: k for n, k in (saved.get("kernels") or {}).items()
                   if _kernel_alive_public(saved, k)}
        if saved.get("active") not in kernels:
            saved["active"] = next(iter(kernels), None)
        try:
            creds = cr.get_credentials()
        except Exception:
            return
        stop_event = cr.start_keepalive(creds.token, saved["endpoint"])
        state.update(
            endpoint=saved["endpoint"], proxy_url=saved["proxy_url"],
            proxy_token=saved["proxy_token"],
            accelerator=saved.get("accelerator", "T4"),
            kernels=kernels, active=saved.get("active"),
            stop_event=stop_event,
        )
        if kernels:
            print(f"[persist] resumed {len(kernels)} kernel(s) on "
                  f"{saved['endpoint']}", file=sys.stderr)
        else:
            # Runtime adopted with no live kernels: the next execute creates
            # a fresh kernel on it (no reallocation).
            print(f"[persist] runtime resumed, kernels dead; new kernel on "
                  f"first call ({saved['endpoint']})", file=sys.stderr)

    def _kernel_alive_public(saved, kernel_id):
        try:
            r = requests.get(
                f"{saved['proxy_url']}/api/kernels/{kernel_id}",
                headers={"X-Colab-Runtime-Proxy-Token": saved["proxy_token"],
                         "X-Colab-Client-Agent": "vscode"},
                timeout=20)
            return r.status_code == 200
        except Exception:
            return False

    # -- state helpers --------------------------------------------------------

    def _drop():
        event = state.pop("stop_event", None)
        if event is not None:
            event.set()
        state.clear()
        _clear_session_file()

    def _release_old(token):
        endpoint = state.get("endpoint")
        _drop()
        if endpoint:
            cr.unassign_runtime(token, endpoint)

    def _snap():
        return {
            "endpoint": state.get("endpoint"),
            "proxy_url": state.get("proxy_url"),
            "proxy_token": state.get("proxy_token"),
            "accelerator": state.get("accelerator"),
            "kernels": dict(state.get("kernels", {})),
            "active": state.get("active"),
        }

    def _active_id():
        return state.get("kernels", {}).get(state.get("active"))

    # -- kernel control API ---------------------------------------------------

    def _kernel_post(kernel_id, action, timeout=30):
        r = requests.post(
            f"{state['proxy_url']}/api/kernels/{kernel_id}/{action}",
            headers=_proxy_headers(), timeout=timeout)
        return r.status_code

    def _create_named_session(label, startup_timeout=180):
        """Create a Jupyter session with a UNIQUE name/path.

        Colab's proxy reuses the same kernel for sessions sharing name+path,
        so every extra kernel must use its own identity.
        """
        headers = {
            "X-Colab-Runtime-Proxy-Token": state["proxy_token"],
            "X-Colab-Client-Agent": "vscode",
            "Content-Type": "application/json",
        }
        body = {
            "kernel": {"name": "python3"},
            "name": f"colab-exec-{label}",
            "path": f"colab-exec-{label}",
            "type": "notebook",
        }
        last_error = None
        deadline = time.time() + startup_timeout
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            try:
                r = requests.post(f"{state['proxy_url']}/api/sessions",
                                  headers=headers, json=body, timeout=30)
                r.raise_for_status()
                return r.json()["kernel"]["id"]
            except Exception as e:
                last_error = e
                time.sleep(3)
        raise RuntimeError(f"timed out creating session {label}: {last_error}")

    def _ws_request(kernel_id, msg_type, content, timeout=60):
        """Send one kernel-protocol request; return its *_reply content."""
        session_id = uuid.uuid4().hex
        ws_url = state["proxy_url"].replace("https://", "wss://") \
                                   .replace("http://", "ws://")
        ws_url = f"{ws_url}/api/kernels/{kernel_id}/channels?session_id={session_id}"
        ws = _ws_connect(ws_url, [
            f"X-Colab-Runtime-Proxy-Token: {state['proxy_token']}",
            "X-Colab-Client-Agent: vscode",
        ], timeout)
        msg_id = uuid.uuid4().hex
        msg = {
            "header": {"msg_id": msg_id, "msg_type": msg_type,
                       "username": "colab-persist", "session": session_id,
                       "version": "5.3"},
            "parent_header": {}, "metadata": {}, "content": content,
            "channel": "shell",
        }
        try:
            ws.send(json.dumps(msg))
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    raw = ws.recv()
                except Exception:
                    continue
                if not raw:
                    time.sleep(0.05)
                    continue
                reply = json.loads(raw)
                if (reply.get("parent_header", {}).get("msg_id") == msg_id
                        and (reply.get("msg_type")
                             or reply.get("header", {}).get("msg_type", ""))
                        .endswith("_reply")):
                    return reply.get("content", {})
            raise RuntimeError(f"timeout waiting for {msg_type} reply")
        finally:
            try:
                ws.close()
            except Exception:
                pass

    # -- file transfer --------------------------------------------------------

    def _remote_ok(remote_path):
        return remote_path.startswith(REMOTE_PREFIXES)

    def _upload_bytes(remote_path, data, timeout=120):
        """Upload bytes; REST contents API first, kernel fallback second."""
        if not _remote_ok(remote_path):
            return {"error": f"remote_path must start with {REMOTE_PREFIXES}"}
        b64 = base64.b64encode(data).decode("ascii")
        if state.get("proxy_url"):
            try:
                r = requests.put(
                    f"{state['proxy_url']}/api/contents/{remote_path}",
                    headers=_proxy_headers(),
                    json={"type": "file", "format": "base64", "content": b64},
                    timeout=timeout)
                if 200 <= r.status_code < 300:
                    return {"method": "rest", "bytes": len(data)}
            except Exception:
                pass
        if len(data) > UPLOAD_FALLBACK_MAX:
            return {"error": f"file exceeds fallback limit "
                             f"({UPLOAD_FALLBACK_MAX} bytes) and REST upload "
                             f"unavailable"}
        code = (f"import base64, pathlib\n"
                f"_p = pathlib.Path({remote_path!r})\n"
                f"_p.parent.mkdir(parents=True, exist_ok=True)\n"
                f"_p.write_bytes(base64.b64decode({b64!r}))\n"
                f"print('wrote', _p.stat().st_size)\n")
        out, err, rc = _run_on_colab(code, state.get("accelerator", "T4"),
                                     timeout)
        if rc != 0:
            return {"error": err[-500:] or "kernel upload failed"}
        return {"method": "kernel", "bytes": len(data)}

    def _download_bytes(remote_path, timeout=120):
        if not _remote_ok(remote_path):
            return {"error": f"remote_path must start with {REMOTE_PREFIXES}"}
        if state.get("proxy_url"):
            try:
                r = requests.get(
                    f"{state['proxy_url']}/api/contents/{remote_path}",
                    headers=_proxy_headers(), params={"content": 1},
                    timeout=timeout)
                if r.status_code == 200:
                    model = r.json()
                    if model.get("format") == "base64" and model.get("content"):
                        return {"method": "rest",
                                "data": base64.b64decode(model["content"])}
            except Exception:
                pass
        code = (f"import base64, os\n"
                f"_p = {remote_path!r}\n"
                f"assert os.path.exists(_p), 'not found: ' + _p\n"
                f"_sz = os.path.getsize(_p)\n"
                f"assert _sz <= {UPLOAD_FALLBACK_MAX}, f'too large: {{_sz}}'\n"
                f"print('DLB64_START')\n"
                f"print(base64.b64encode(open(_p,'rb').read()).decode('ascii'))\n"
                f"print('DLB64_END')\n")
        out, err, rc = _run_on_colab(code, state.get("accelerator", "T4"),
                                     timeout)
        if rc != 0:
            return {"error": err[-500:] or "kernel download failed"}
        start = out.find("DLB64_START")
        end = out.find("DLB64_END")
        if start == -1 or end == -1:
            return {"error": "markers not found in kernel output"}
        b64 = out[start + len("DLB64_START"):end].strip()
        return {"method": "kernel", "data": base64.b64decode(b64)}

    # -- core executor ---------------------------------------------------------

    def _run_on_colab(code, accelerator, timeout, busy_wait=BUSY_WAIT_DEFAULT):
        with lock:
            creds = cr.get_credentials()
            token = creds.token

            if state.get("endpoint") and state.get("accelerator") != accelerator:
                _release_old(token)

            if not state.get("endpoint"):
                # CPU runtimes allocate with an empty accelerator (no variant).
                alloc_acc = "" if accelerator == "CPU" else accelerator
                try:
                    assignment = cr.allocate_runtime(token, alloc_acc)
                except Exception as exc:
                    # Colab allows one runtime per account; a leftover
                    # assignment (e.g. crashed process) causes 412. Discover
                    # it via a variant-less assign, release it, retry once.
                    print(f"[persist] allocate failed ({exc}); trying to "
                          f"discover and release a leftover runtime",
                          file=sys.stderr)
                    try:
                        leftover = cr.allocate_runtime(token, "")
                        cr.unassign_runtime(token, leftover["endpoint"])
                    except Exception:
                        pass
                    assignment = cr.allocate_runtime(token, alloc_acc)
                stop_event = cr.start_keepalive(token, assignment["endpoint"])
                state.update(
                    endpoint=assignment["endpoint"],
                    proxy_url=assignment["proxy_url"],
                    proxy_token=assignment["proxy_token"],
                    accelerator=accelerator, kernels={}, active=None,
                    stop_event=stop_event,
                )

            if _active_id() is None:
                try:
                    kernel_id = cr.create_session(state["proxy_url"],
                                                  state["proxy_token"])
                except Exception:
                    _release_old(token)
                    raise
                state.setdefault("kernels", {})["main"] = kernel_id
                state["active"] = "main"
                _save_session()
                print(f"[persist] warm kernel {kernel_id} on "
                      f"{state['endpoint']}", file=sys.stderr)

            kernel_id = _active_id()
            _wait_idle(kernel_id, busy_wait)
            try:
                return cr.execute_code(
                    state["proxy_url"], state["proxy_token"], kernel_id,
                    code, timeout=timeout, access_token=token,
                    endpoint=state["endpoint"])
            except Exception as exc:
                if _kernel_alive(kernel_id):
                    raise
                print(f"[persist] kernel {state['active']} lost ({exc}); "
                      f"new kernel on same runtime", file=sys.stderr)
                state["kernels"].pop(state["active"], None)
                try:
                    kernel_id = cr.create_session(state["proxy_url"],
                                                  state["proxy_token"])
                except Exception:
                    _release_old(token)
                    raise
                state["kernels"][state["active"] or "main"] = kernel_id
                _save_session()
                return cr.execute_code(
                    state["proxy_url"], state["proxy_token"], kernel_id,
                    code, timeout=timeout, access_token=token,
                    endpoint=state["endpoint"])

    srv._run_on_colab = _run_on_colab

    def _ensure_warm():
        """Make sure a warm runtime + active kernel exist; return snapshot."""
        with lock:
            if not state.get("endpoint") or _active_id() is None:
                pass
            else:
                return _snap()
        _run_on_colab("pass", state.get("accelerator", "T4") if state.get(
            "endpoint") else "T4", 60)
        return _snap()

    # -- tools -----------------------------------------------------------------

    def colab_execute(code: str, accelerator: str = "T4", timeout: int = 300,
                      busy_wait: int = BUSY_WAIT_DEFAULT) -> str:
        """Execute Python code on the persistent warm Colab kernel.

        The runtime and kernel stay warm between calls: variables and loaded
        models persist. Use accelerator="CPU" for non-GPU work to save quota.
        Raises a clear error if the kernel is busy running another cell
        (see colab_interrupt / colab_kernel_busy).
        """
        wrapped, num_cells = srv._wrap_cells(code)
        stdout, stderr, rc = _run_on_colab(wrapped, accelerator, timeout,
                                           busy_wait)
        cells = srv._parse_cell_output(stdout, num_cells)
        errors = [c for c in cells if c["status"] != "ok"] if rc != 0 else []
        return json.dumps(
            {"cells": cells, "errors": errors, "stderr": stderr,
             "exit_code": rc}, indent=2)

    def colab_kernel_reset() -> str:
        """Drop all kernels and release the GPU runtime.

        Frees the GPU immediately. The next call allocates a fresh runtime.
        Use when done with GPU work for a while.
        """
        with lock:
            if not state.get("endpoint"):
                return json.dumps({"reset": False, "reason": "no warm runtime"})
            endpoint = state.get("endpoint")
            creds = cr.get_credentials()
            _drop()
            released = cr.unassign_runtime(creds.token, endpoint)
            return json.dumps(
                {"reset": True, "released": released, "endpoint": endpoint})

    def colab_kernel_restart() -> str:
        """Restart the active kernel in place, keeping the same runtime.

        Namespace and VRAM are cleared (models reload), but pip packages and
        the GPU assignment survive — no reallocation delay.
        """
        with lock:
            kernel_id = _active_id()
            if kernel_id is None:
                return json.dumps({"restart": False, "reason": "no warm kernel"})
            try:
                code = _kernel_post(kernel_id, "restart")
            except Exception as exc:
                return json.dumps({"restart": False, "error": str(exc)})
            if code not in (200, 201, 204):
                return json.dumps(
                    {"restart": False, "error": f"HTTP {code}",
                     "hint": "call colab_kernel_reset to start over"})
            deadline = time.time() + 60
            while time.time() < deadline:
                if _kernel_alive(kernel_id):
                    return json.dumps(
                        {"restart": True, "kernel_id": kernel_id,
                         "endpoint": state["endpoint"]})
                time.sleep(1)
            return json.dumps({"restart": False, "error": "restart timed out"})

    def colab_interrupt() -> str:
        """Interrupt the busy kernel: stop the running cell immediately.

        The kernel stays alive with all state (loaded models kept); the
        running cell raises KeyboardInterrupt. Does NOT release the GPU.
        """
        kernel_id = _active_id()
        if kernel_id is None:
            return json.dumps({"interrupted": False, "reason": "no warm kernel"})
        try:
            code = _kernel_post(kernel_id, "interrupt")
        except Exception as exc:
            return json.dumps({"interrupted": False, "error": str(exc)})
        return json.dumps({"interrupted": 200 <= code < 300, "http": code,
                           "kernel_id": kernel_id})

    def colab_kernel_busy() -> str:
        """Report whether the active kernel is busy running a cell."""
        kernel_id = _active_id()
        if kernel_id is None:
            return json.dumps({"busy": None, "reason": "no warm kernel"})
        model = _kernel_model(kernel_id)
        if model is None:
            return json.dumps({"busy": None, "reason": "kernel unreachable",
                               "kernel_id": kernel_id})
        st = model.get("execution_state", "unknown")
        return json.dumps({"busy": st == "busy",
                           "execution_state": st, "kernel_id": kernel_id})

    def colab_kernel_info() -> str:
        """Probe the warm kernel: liveness, python, GPU name, VRAM usage."""
        kernel_id = _active_id()
        if kernel_id is None:
            return json.dumps({"alive": False, "reason": "no warm kernel"})
        if not _kernel_alive(kernel_id):
            return json.dumps({"alive": False, "endpoint": state.get("endpoint"),
                               "kernel_id": kernel_id})
        probe = (
            "import json\n"
            "info = {'python': __import__('sys').version.split()[0]}\n"
            "try:\n"
            "    import torch\n"
            "    info['gpu'] = torch.cuda.get_device_name(0)\n"
            "    free, total = torch.cuda.mem_get_info()\n"
            "    info['vram_free_gb'] = round(free/2**30, 1)\n"
            "    info['vram_total_gb'] = round(total/2**30, 1)\n"
            "except Exception:\n"
            "    info['gpu'] = 'none'\n"
            "print(json.dumps(info))\n"
        )
        try:
            stdout, stderr, rc = _run_on_colab(
                probe, state.get("accelerator", "T4"), 120)
        except Exception as exc:
            return json.dumps({"alive": True, "probe_error": str(exc),
                               "endpoint": state.get("endpoint")})
        line = next((l for l in stdout.splitlines() if l.startswith("{")), "")
        result = {"alive": True, "endpoint": state.get("endpoint"),
                  "kernel_id": kernel_id}
        try:
            result.update(json.loads(line))
        except Exception:
            result["probe"] = stdout.strip()[:200]
        return json.dumps(result)

    def colab_namespace() -> str:
        """List user variables in the kernel namespace without executing code.

        Returns name + type pairs (silent kernel-protocol probe, nothing is
        run and In[] history is not polluted).
        """
        snap = _ensure_warm()
        kernel_id = snap["kernels"][snap["active"]]
        content = {
            "code": "", "silent": True, "store_history": False,
            "user_expressions": {
                "ns": ("[(n, type(globals()[n]).__name__) for n in dir() "
                       "if not n.startswith('_')]"),
            },
        }
        reply = _ws_request(kernel_id, "execute_request", content)
        expr = (reply.get("user_expressions") or {}).get("ns", {})
        names = []
        if expr.get("status") == "ok":
            text = (expr.get("data") or {}).get("text/plain", "[]")
            try:
                names = ast.literal_eval(text)
            except Exception:
                names = []
        return json.dumps({"count": len(names), "names": names})

    def colab_inspect(name: str) -> str:
        """Inspect a kernel variable: type, value/docstring if any.

        Silent kernel-protocol probe (inspect_request + fallback
        user_expressions); nothing executes.
        """
        snap = _ensure_warm()
        kernel_id = snap["kernels"][snap["active"]]
        reply = _ws_request(kernel_id, "inspect_request",
                            {"code": name, "cursor_pos": len(name),
                             "detail_level": 0})
        if reply.get("status") != "ok" or not reply.get("found"):
            return json.dumps({"found": False, "name": name})
        text = (reply.get("data") or {}).get("text/plain", "")
        if not text.strip():
            # Plain variables get empty inspect text; compose type + repr.
            exprs = {"t": f"type({name}).__name__", "r": f"repr({name})"}
            r2 = _ws_request(kernel_id, "execute_request",
                             {"code": "", "silent": True,
                              "store_history": False,
                              "user_expressions": exprs})
            ue = r2.get("user_expressions") or {}
            if (ue.get("t", {}).get("status") == "ok"
                    and ue.get("r", {}).get("status") == "ok"):
                try:
                    t = ast.literal_eval(
                        (ue["t"].get("data") or {}).get("text/plain", "''"))
                    r = ast.literal_eval(
                        (ue["r"].get("data") or {}).get("text/plain", "''"))
                    text = f"{t} {name} = {r}"
                except Exception:
                    pass
        return json.dumps({"found": True, "name": name, "info": text[:2000]})

    def colab_check_syntax(code: str) -> str:
        """Check Python syntax on the kernel without running the code.

        Returns ok=true or the SyntaxError details.
        """
        snap = _ensure_warm()
        kernel_id = snap["kernels"][snap["active"]]
        expr = f"compile({json.dumps(code)}, '<cell>', 'exec')"
        content = {
            "code": "", "silent": True, "store_history": False,
            "user_expressions": {"check": expr},
        }
        reply = _ws_request(kernel_id, "execute_request", content)
        result = (reply.get("user_expressions") or {}).get("check", {})
        if result.get("status") == "ok":
            return json.dumps({"ok": True})
        return json.dumps({"ok": False,
                           "error": (result.get("evalue")
                                     or str(result.get("traceback", []))[:400])})

    def colab_upload(file_path: str, remote_path: str,
                     timeout: int = 120) -> str:
        """Upload a local file to the Colab runtime (/content or /tmp).

        Tries the Jupyter contents REST API, falls back to a kernel cell.
        Max 25 MB (REST) / 8 MB (fallback).
        """
        file_path = os.path.expanduser(file_path)
        if not os.path.isfile(file_path):
            return json.dumps({"error": f"File not found: {file_path}"})
        size = os.path.getsize(file_path)
        if size > UPLOAD_MAX_BYTES:
            return json.dumps({"error": f"file too large: {size} bytes"})
        with open(file_path, "rb") as f:
            data = f.read()
        result = _upload_bytes(remote_path, data, timeout)
        result["remote_path"] = remote_path
        return json.dumps(result)

    def colab_download(remote_path: str, file_path: str,
                       timeout: int = 120) -> str:
        """Download a file from the Colab runtime to the phone.

        Tries the Jupyter contents REST API, falls back to a kernel cell.
        """
        file_path = os.path.expanduser(file_path)
        result = _download_bytes(remote_path, timeout)
        if "error" in result:
            return json.dumps(result)
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        with open(file_path, "wb") as f:
            f.write(result["data"])
        return json.dumps({"method": result["method"],
                           "bytes": len(result["data"]),
                           "local_path": file_path})

    def colab_env_snapshot() -> str:
        """Snapshot the runtime python version + pip freeze to the phone.

        Saved to ~/.config/colab-exec/env_snapshot.txt; restore later with
        colab_env_restore (e.g. after Colab reclaims the GPU).
        """
        out, err, rc = _run_on_colab(FREEZE_SCRIPT, "T4", 300)
        if rc != 0:
            return json.dumps({"error": err[-500:] or "pip freeze failed"})
        lines = out.strip().splitlines()
        python = lines[0] if lines else "?"
        packages = [l for l in lines[1:] if l.strip()]
        try:
            os.makedirs(SESSION_DIR, exist_ok=True)
            with open(ENV_SNAPSHOT_PATH, "w") as f:
                f.write("\n".join(packages))
            os.chmod(ENV_SNAPSHOT_PATH, 0o600)
        except Exception as exc:
            return json.dumps({"error": f"save failed: {exc}"})
        return json.dumps({"python": python, "packages": len(packages),
                           "saved": ENV_SNAPSHOT_PATH})

    def colab_env_restore(timeout: int = 900) -> str:
        """Reinstall the snapshotted pip environment on the warm runtime.

        Uploads ~/.config/colab-exec/env_snapshot.txt and runs pip install
        -r. Fast when packages are already satisfied.
        """
        if not os.path.isfile(ENV_SNAPSHOT_PATH):
            return json.dumps({"error": "no snapshot; run colab_env_snapshot"})
        with open(ENV_SNAPSHOT_PATH) as f:
            packages = [l.strip() for l in f if l.strip()]
        if not packages:
            return json.dumps({"error": "snapshot is empty"})
        # Local-version pins (torch==2.9.0+cu128) and direct file references
        # (pkg @ file:///...) only resolve on Colab's own setup; skip them.
        skipped = [p for p in packages
                   if ("==" in p and "+" in p.split("==", 1)[1])
                   or "file:///" in p]
        packages = [p for p in packages if p not in skipped]
        if not packages:
            return json.dumps({"error": "snapshot has only local-version pins",
                               "skipped": len(skipped)})
        content = "\n".join(packages).encode()
        up = _upload_bytes("/content/relay_env_snapshot.txt", content)
        if "error" in up:
            return json.dumps(up)
        code = ("import subprocess, sys\n"
                "r = subprocess.run([sys.executable, '-m', 'pip', 'install', "
                "'-q', '-r', '/content/relay_env_snapshot.txt'], "
                "capture_output=True, text=True)\n"
                "print('pip rc', r.returncode)\n"
                "print(r.stderr[-1500:])\n")
        out, err, rc = _run_on_colab(code, "T4", timeout)
        ok = rc == 0 and "pip rc 0" in out
        return json.dumps({"restored": ok, "packages": len(packages),
                           "skipped_local_pins": len(skipped),
                           "upload_method": up.get("method"),
                           "detail": out.strip()[-400:]})

    def colab_expose(port: int, timeout: int = 180) -> str:
        """Publish a port on the Colab runtime as a public HTTPS URL.

        Starts a cloudflared quick tunnel to 127.0.0.1:<port> on the runtime
        and returns the https://*.trycloudflare.com URL. Re-running replaces
        the tunnel on that port. Use to expose the model server.
        """
        if not 1 <= port <= 65535:
            return json.dumps({"error": "port out of range"})
        cycles = max(1, timeout // 2)
        out, err, rc = _run_on_colab(EXPOSE_SCRIPT % {"port": port,
                                                      "wait_cycles": cycles},
                                     state.get("accelerator", "T4"), timeout)
        if rc != 0:
            return json.dumps({"error": err[-500:] or "expose failed"})
        url = next((l.strip() for l in out.splitlines()
                    if "trycloudflare.com" in l), "")
        if not url:
            return json.dumps({"error": "no tunnel URL in output"})
        return json.dumps({"port": port, "url": url})

    def colab_expose_status(port: int) -> str:
        """Check the cloudflared tunnel for a port: alive, URL, pid."""
        out, err, rc = _run_on_colab(
            EXPOSE_STATUS_SCRIPT % {"port": port},
            state.get("accelerator", "T4"), 60)
        line = next((l for l in out.splitlines() if l.startswith("{")), "")
        try:
            return json.dumps(json.loads(line))
        except Exception:
            return json.dumps({"alive": False, "raw": out.strip()[:300]})

    def colab_execute_detached(code: str, job: str = "job",
                               accelerator: str = "T4", timeout: int = 120,
                               busy_wait: int = BUSY_WAIT_DEFAULT) -> str:
        """Start a long-running job on the warm kernel without blocking.

        The code runs in a background thread; this returns once launched.
        Progress (state, live stdout tail) is written to /content/job.json —
        poll with colab_job_status. One job at a time per kernel.
        """
        started = time.time()
        out, err, rc = _run_on_colab(
            JOB_SCRIPT % {"job": job, "code": code}, accelerator, timeout,
            busy_wait)
        result = {"job": job, "launched": rc == 0,
                  "elapsed": round(time.time() - started, 1)}
        if rc != 0:
            result["stderr"] = err[-1000:]
        return json.dumps(result)

    def colab_job_status() -> str:
        """Read the last detached job's status (running/done/error) and its
        live stdout tail (updated every ~2s while running)."""
        snap = _ensure_warm()
        kernel_id = snap["kernels"][snap["active"]]
        expr = ("__import__('json').loads(open('/content/job.json').read()) "
                "if __import__('os').path.exists('/content/job.json') "
                "else {'state': 'missing', 'error': 'no job.json'}")
        reply = _ws_request(kernel_id, "execute_request",
                            {"code": "", "silent": True, "store_history": False,
                             "user_expressions": {"job": expr}})
        ue = (reply.get("user_expressions") or {}).get("job", {})
        if ue.get("status") == "ok":
            try:
                data = ast.literal_eval(
                    (ue.get("data") or {}).get("text/plain", ""))
                return json.dumps(data)
            except Exception:
                pass
        return json.dumps({"state": "unknown",
                           "error": ue.get("evalue", "probe failed")})

    def colab_kernel_new(name: str) -> str:
        """Create a second kernel on the same runtime and switch to it.

        Kernels share the GPU but have independent namespaces — e.g. a
        training kernel and a serving kernel on one T4. Switch with
        colab_kernel_use.
        """
        if not name or not name.isidentifier():
            return json.dumps({"error": "name must be an identifier"})
        with lock:
            if not state.get("endpoint"):
                pass
            else:
                try:
                    kernel_id = _create_named_session(name)
                except Exception as exc:
                    return json.dumps({"error": str(exc)})
                state.setdefault("kernels", {})[name] = kernel_id
                state["active"] = name
                _save_session()
                return json.dumps({"created": name, "kernel_id": kernel_id,
                                   "active": name})
        _run_on_colab("pass", "T4", 60)
        with lock:
            if name in state.get("kernels", {}):
                return json.dumps({"created": name,
                                   "kernel_id": state["kernels"][name],
                                   "active": name})
        return json.dumps({"error": "kernel_new fell back to main; retry"})

    def colab_kernel_list() -> str:
        """List kernels on the warm runtime: ours (named) and any others."""
        snap = _snap()
        if not snap["endpoint"]:
            return json.dumps({"kernels": [], "reason": "no warm runtime"})
        result = []
        seen = set()
        for s in _sessions():
            kid = (s.get("kernel") or {}).get("id")
            if not kid:
                continue
            seen.add(kid)
            ours = next((n for n, k in snap["kernels"].items() if k == kid),
                        None)
            result.append({"name": ours, "kernel_id": kid,
                           "session_id": s.get("id"),
                           "session_name": s.get("name"),
                           "active": ours == snap["active"]})
        for n, k in snap["kernels"].items():
            if k not in seen:
                result.append({"name": n, "kernel_id": k, "session_id": None,
                               "session_name": None,
                               "active": n == snap["active"]})
        return json.dumps({"kernels": result, "active": snap["active"]})

    def colab_kernel_use(name: str) -> str:
        """Switch the active kernel (must exist; see colab_kernel_list)."""
        with lock:
            if name not in state.get("kernels", {}):
                return json.dumps({"error": f"unknown kernel: {name}"})
            state["active"] = name
            _save_session()
            return json.dumps({"active": name,
                               "kernel_id": state["kernels"][name]})

    def colab_kernel_close(name: str = "") -> str:
        """Close a kernel (default: the active one) and free its memory.

        The runtime stays warm. If the active kernel is closed, the next
        execute creates a fresh one.
        """
        with lock:
            kernels = state.get("kernels", {})
            target = name or state.get("active")
            if not target or target not in kernels:
                return json.dumps({"closed": False,
                                   "reason": f"unknown kernel: {target}"})
            kernel_id = kernels.pop(target)
            was_active = state.get("active") == target
            if was_active:
                state["active"] = next(iter(kernels), None)
            _save_session()
        session_id = None
        for s in _sessions():
            if (s.get("kernel") or {}).get("id") == kernel_id:
                session_id = s.get("id")
                break
        deleted = False
        try:
            if session_id:
                r = requests.delete(
                    f"{state['proxy_url']}/api/sessions/{session_id}",
                    headers=_proxy_headers(), timeout=30)
                deleted = 200 <= r.status_code < 300
            if not deleted:
                r = requests.delete(
                    f"{state['proxy_url']}/api/kernels/{kernel_id}",
                    headers=_proxy_headers(), timeout=30)
                deleted = 200 <= r.status_code < 300
        except Exception:
            pass
        return json.dumps({"closed": True, "name": target,
                           "kernel_id": kernel_id, "deleted": deleted,
                           "active": state.get("active")})

    def colab_kernels_prune(all: bool = False) -> str:
        """Delete orphan kernels on the warm runtime.

        Orphans are colab-exec sessions not registered by this launcher
        (e.g. left by a crashed process). Pass all=true to delete every
        colab-exec kernel including ours.
        """
        snap = _snap()
        if not snap["endpoint"]:
            return json.dumps({"pruned": [], "reason": "no warm runtime"})
        pruned = []
        for s in _sessions():
            kid = (s.get("kernel") or {}).get("id")
            ours = kid in snap["kernels"].values()
            if not (s.get("name") or "").startswith("colab-exec"):
                continue
            if ours and not all:
                continue
            try:
                r = requests.delete(
                    f"{state['proxy_url']}/api/sessions/{s.get('id')}",
                    headers=_proxy_headers(), timeout=30)
                if 200 <= r.status_code < 300:
                    pruned.append({"kernel_id": kid, "session_id": s.get("id")})
            except Exception:
                pass
        if all and state.get("kernels"):
            with lock:
                state["kernels"] = {}
                state["active"] = None
                _save_session()
        return json.dumps({"pruned": pruned, "count": len(pruned)})

    def _tool(fn, **hints):
        return srv.mcp.tool(annotations={"readOnlyHint": False, **hints})(fn)

    srv.colab_execute = _tool(colab_execute)
    srv.colab_kernel_reset = _tool(colab_kernel_reset, destructiveHint=True)
    srv.colab_kernel_restart = _tool(colab_kernel_restart, destructiveHint=True)
    srv.colab_interrupt = _tool(colab_interrupt)
    srv.colab_kernel_busy = _tool(colab_kernel_busy, readOnlyHint=True)
    srv.colab_kernel_info = _tool(colab_kernel_info, readOnlyHint=True)
    srv.colab_namespace = _tool(colab_namespace, readOnlyHint=True)
    srv.colab_inspect = _tool(colab_inspect, readOnlyHint=True)
    srv.colab_check_syntax = _tool(colab_check_syntax, readOnlyHint=True)
    srv.colab_upload = _tool(colab_upload)
    srv.colab_download = _tool(colab_download)
    srv.colab_env_snapshot = _tool(colab_env_snapshot, readOnlyHint=True)
    srv.colab_env_restore = _tool(colab_env_restore)
    srv.colab_expose = _tool(colab_expose)
    srv.colab_expose_status = _tool(colab_expose_status, readOnlyHint=True)
    srv.colab_execute_detached = _tool(colab_execute_detached)
    srv.colab_job_status = _tool(colab_job_status, readOnlyHint=True)
    srv.colab_kernel_new = _tool(colab_kernel_new, destructiveHint=True)
    srv.colab_kernel_list = _tool(colab_kernel_list, readOnlyHint=True)
    srv.colab_kernel_use = _tool(colab_kernel_use)
    srv.colab_kernel_close = _tool(colab_kernel_close, destructiveHint=True)
    srv.colab_kernels_prune = _tool(colab_kernels_prune, destructiveHint=True)

    _resume_session()


if __name__ == "__main__":
    colab_mcp_dns.install()
    install()
    from mcp_server_colab_exec.server import main
    main()

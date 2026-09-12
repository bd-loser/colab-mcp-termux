"""Mock tests for the persistent-kernel launcher (colab_persistent).

Run from the repository root or from tests/:  python3 tests/test_colab_persistent.py

Covers: warm reuse, recovery ladder (transient / kernel-dead / runtime-dead),
accelerator switch, busy preflight, session persist+resume, restart,
interrupt, namespace/inspect/syntax via fake WS, upload/download (REST +
fallback), env snapshot/restore, expose, multi-kernel registry, prune,
detached jobs with live tail. All Colab APIs are faked; no network, no GPU.
"""
import base64
import importlib
import json
import os
import sys
import tempfile
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "scripts"))

import colab_persistent

PASS = []


def run(name, fn):
    fn()
    PASS.append(name)
    print(f"ok: {name}")


class FakeResp:
    def __init__(self, code, json_data=None, text=""):
        self.status_code = code
        self._json = json_data
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests as _r
            raise _r.HTTPError(f"{self.status_code}")


class FakeWs:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self):
        if self.replies:
            return json.dumps(self.replies.pop(0))
        return ""

    def close(self):
        pass


def fresh(save_path=None, alive=True):
    if save_path is None:
        save_path = os.path.join(tempfile.mkdtemp(), "session.json")
    importlib.reload(colab_persistent)
    colab_persistent.SESSION_PATH = save_path  # AFTER reload; reload resets it
    colab_persistent.ENV_SNAPSHOT_PATH = os.path.join(
        os.path.dirname(save_path), "env_snapshot.txt")
    from mcp_server_colab_exec import colab_runtime as cr

    calls = {"alloc": 0, "session": 0, "exec": 0, "ka": 0, "unassign": 0,
             "kpost": [], "deleted": [], "put": [], "ws": 0}
    flag = {"fail_exec": False, "fail_session": False, "alive": alive,
            "busy": "idle", "sessions": [], "ws_replies": []}
    kernels = ["k1", "k2", "k3"]
    store = {"state": {}, "contents": {}}

    def fake_kernel_post(action):
        calls["kpost"].append(action)
        return 200

    cr.get_credentials = lambda: SimpleNamespace(token="tok")
    cr.allocate_runtime = lambda t, a: calls.__setitem__("alloc", calls["alloc"] + 1) or {
        "endpoint": f"e{calls['alloc']}", "proxy_url": f"http://p{calls['alloc']}",
        "proxy_token": "pt", "xsrf_token": None, "nbh": "n", "reused": False}
    cr.start_keepalive = lambda t, e: calls.__setitem__("ka", calls["ka"] + 1) or threading.Event()

    def fake_create_session(url, tok, startup_timeout=180):
        if flag["fail_session"]:
            raise RuntimeError("runtime gone")
        calls["session"] += 1
        kid = kernels[min(calls["session"] - 1, len(kernels) - 1)]
        return kid

    cr.create_session = fake_create_session

    def fake_exec(url, tok, kid, code, timeout=300, access_token=None, endpoint=None):
        calls["exec"] += 1
        if flag["fail_exec"]:
            if flag.get("fail_once"):
                flag["fail_exec"] = False  # kernel death is one-shot
            raise OSError("dead runtime")
        if "[detached] started" in code:
            store["state"]["job"] = {"state": "running"}
            return ("[detached] started 'j1'", "", 0)
        if "job.json" in code and "print(_json.dumps(_json.load(_f))" in code:
            return (json.dumps(store["state"].get("job", {"state": "unknown"})), "", 0)
        if "vram_free_gb" in code:
            return (json.dumps({"python": "3.13", "gpu": "Tesla T4",
                                "vram_free_gb": 14.7, "vram_total_gb": 15.0}), "", 0)
        if "'pip', 'install'" in code:
            return ("pip rc 0\n", "", 0)
        if "'pip', 'freeze'" in code or '"pip", "freeze"' in code:
            return ("3.13.15\npkg-a==1.0\npkg-b==2.0\n", "", 0)
        if "pkill" in code:
            return ("https://x-tunnel.trycloudflare.com", "", 0)
        if "write_bytes(base64.b64decode(" in code:
            marker = code.split("base64.b64decode(")[1].split("))")[0].strip("'\"")
            store["state"]["last_upload"] = base64.b64decode(marker)
            return ("wrote 3", "", 0)
        if "DLB64_START" in code:
            data = store["state"].get("last_download", b"hello")
            b64 = base64.b64encode(data).decode()
            return (f"DLB64_START\n{b64}\nDLB64_END", "", 0)
        if "exposed_" in code:
            return (json.dumps({"port": 8099, "url": "https://x.trycloudflare.com",
                                "pid": 123, "alive": True}), "", 0)
        return (f"ran {code[:24]!r} on {kid}", "", 0)

    cr.execute_code = fake_exec
    cr.unassign_runtime = lambda t, e: calls.__setitem__(
        "unassign", calls["unassign"] + 1) or True

    import requests

    def fake_get(url, **kw):
        if "/api/sessions" in url:
            return FakeResp(200, flag["sessions"])
        if "/api/kernels/" in url:
            if not flag["alive"]:
                return FakeResp(404)
            return FakeResp(200, {"id": "k", "execution_state": flag["busy"]})
        if "/api/contents/" in url:
            path = url.split("/api/contents/")[-1]
            if path in store["contents"]:
                return FakeResp(200, {"format": "base64",
                                      "content": store["contents"][path]})
            return FakeResp(404)
        return FakeResp(404)

    def fake_post(url, **kw):
        if url.rstrip("/").endswith("/api/sessions"):
            kid = f"k{calls['session'] + 1}"
            return FakeResp(200, {"kernel": {"id": kid}})
        action = url.rstrip("/").rsplit("/", 1)[-1]
        return FakeResp(fake_kernel_post(action))

    def fake_put(url, **kw):
        path = url.split("/api/contents/")[-1]
        store["contents"][path] = kw.get("json", {}).get("content")
        calls["put"].append(path)
        return FakeResp(200)

    def fake_delete(url, **kw):
        calls["deleted"].append(url)
        return FakeResp(204)

    requests.get = fake_get
    requests.post = fake_post
    requests.put = fake_put
    requests.delete = fake_delete

    def fake_ws(url, headers, timeout):
        calls["ws"] += 1
        return FakeWs(flag["ws_replies"])

    colab_persistent._ws_connect = fake_ws
    colab_persistent.install()
    from mcp_server_colab_exec import server as srv

    return SimpleNamespace(cr=cr, srv=srv, calls=calls, flag=flag,
                           store=store, save_path=save_path)


def _ws_reply(msg_type, content):
    return {"header": {"msg_type": msg_type}, "parent_header": {"msg_id": "X"},
            "content": content}


# NOTE: parent msg ids are random; _ws_request matches parent_header.msg_id.
# The fake replies below use a placeholder replaced at send time via wrapper.
class DynWs(FakeWs):
    def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        for r in self.replies:
            if r["parent_header"]["msg_id"] == "MATCH":
                r["parent_header"]["msg_id"] = msg["header"]["msg_id"]
                break  # one reply patched per request


def with_dyn_ws(t, replies):
    t.flag["ws_replies"] = replies

    class C:
        def __init__(self):
            self.replies = replies
            self.sent = []

        def send(self, raw):
            msg = json.loads(raw)
            self.sent.append(msg)
            for r in self.replies:
                if r["parent_header"].get("msg_id") == "MATCH":
                    r["parent_header"]["msg_id"] = msg["header"]["msg_id"]
                    break

        def recv(self):
            return json.dumps(self.replies.pop(0)) if self.replies else ""

        def close(self):
            pass
    orig = colab_persistent._ws_connect
    colab_persistent._ws_connect = lambda u, h, to: C()
    return orig


# ── core executor ────────────────────────────────────────────────────────────

def test_warm_reuse():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    t.srv._run_on_colab("c2", "T4", 10)
    assert t.calls["alloc"] == 1 and t.calls["session"] == 1
    assert t.calls["exec"] == 2 and t.calls["unassign"] == 0


def test_transient_exec_error_keeps_kernel():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    t.flag["fail_exec"] = True
    try:
        t.srv._run_on_colab("c2", "T4", 10)
        raise AssertionError("expected failure")
    except OSError:
        pass
    t.flag["fail_exec"] = False
    t.srv._run_on_colab("c3", "T4", 10)
    assert t.calls["session"] == 1 and t.calls["alloc"] == 1, t.calls


def test_kernel_dead_new_session_same_runtime():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    t.flag["fail_exec"] = True
    t.flag["fail_once"] = True
    t.flag["alive"] = False  # kernel REST says dead -> new session, same runtime
    t.srv._run_on_colab("c2", "T4", 10)
    assert t.calls["alloc"] == 1 and t.calls["session"] == 2
    assert t.calls["unassign"] == 0, "kernel death must not unassign runtime"


def test_runtime_dead_full_drop():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    t.flag["fail_exec"] = True
    t.flag["alive"] = False
    t.flag["fail_session"] = True
    try:
        t.srv._run_on_colab("c2", "T4", 10)
        raise AssertionError("expected failure")
    except RuntimeError:
        pass
    t.flag["fail_exec"] = False
    t.flag["fail_session"] = False
    t.srv._run_on_colab("c3", "T4", 10)
    assert t.calls["alloc"] == 2, "runtime death must lead to reallocation"


def test_accelerator_switch_releases_old():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    t.srv._run_on_colab("c2", "L4", 10)
    assert t.calls["alloc"] == 2 and t.calls["unassign"] == 1


def test_busy_prefetch_blocks():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    t.flag["busy"] = "busy"
    try:
        t.srv._run_on_colab("quick", "T4", 10, busy_wait=0)
        raise AssertionError("expected busy error")
    except RuntimeError as e:
        assert "busy" in str(e)
    assert t.calls["exec"] == 1, "busy kernel must not accept the cell"


def test_busy_waits_then_runs():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    t.flag["busy"] = "busy"

    def unbusy():
        import time as _t
        _t.sleep(1.5)
        t.flag["busy"] = "idle"
    threading.Thread(target=unbusy).start()
    t.srv._run_on_colab("c2", "T4", 10, busy_wait=5)
    assert t.calls["exec"] == 2


def test_execute_error_keeps_kernel():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    orig = t.cr.execute_code
    t.cr.execute_code = lambda *a, **k: ("", "boom", 1)
    out = t.srv._run_on_colab("bad", "T4", 10)
    t.cr.execute_code = orig
    assert out == ("", "boom", 1)
    t.srv._run_on_colab("good", "T4", 10)
    assert t.calls["alloc"] == 1 and t.calls["session"] == 1


def test_execute_tool_wraps_cells():
    t = fresh()
    result = json.loads(t.srv.colab_execute("x = 1\n\nprint(x)"))
    assert result["exit_code"] == 0 and len(result["cells"]) == 2


# ── kernel control tools ─────────────────────────────────────────────────────

def test_reset_tool():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    result = json.loads(t.srv.colab_kernel_reset())
    assert result["reset"] is True and result["released"] is True
    assert not os.path.exists(colab_persistent.SESSION_PATH)


def test_restart_in_place():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    result = json.loads(t.srv.colab_kernel_restart())
    assert result["restart"] is True and result["kernel_id"] == "k1"
    assert t.calls["session"] == 1 and t.calls["alloc"] == 1
    assert "restart" in t.calls["kpost"]
    t.srv._run_on_colab("c2", "T4", 10)
    assert "k1" in t.srv._run_on_colab("c3", "T4", 10)[0]


def test_interrupt_busy():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    result = json.loads(t.srv.colab_interrupt())
    assert result["interrupted"] is True and "interrupt" in t.calls["kpost"]
    assert t.calls["alloc"] == 1 and t.calls["session"] == 1


def test_interrupt_no_kernel():
    t = fresh()
    result = json.loads(t.srv.colab_interrupt())
    assert result == {"interrupted": False, "reason": "no warm kernel"}


def test_kernel_busy_tool():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    t.flag["busy"] = "busy"
    assert json.loads(t.srv.colab_kernel_busy())["busy"] is True
    t.flag["busy"] = "idle"
    assert json.loads(t.srv.colab_kernel_busy())["busy"] is False


def test_info_probe():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    result = json.loads(t.srv.colab_kernel_info())
    assert result["alive"] is True and result["gpu"] == "Tesla T4"


# ── WS probes: namespace / inspect / syntax ─────────────────────────────────

def test_namespace():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    reply = {"header": {"msg_type": "execute_reply"},
             "parent_header": {"msg_id": "MATCH"},
             "content": {"status": "ok", "user_expressions": {"ns": {
                 "status": "ok",
                 "data": {"text/plain": "[('x', 'int'), ('torch', 'module')]"}}}}}
    orig = with_dyn_ws(t, [reply])
    try:
        result = json.loads(t.srv.colab_namespace())
    finally:
        colab_persistent._ws_connect = orig
    assert result["count"] == 2
    assert ("x", "int") in [tuple(n) for n in result["names"]]


def test_inspect():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    reply = {"header": {"msg_type": "inspect_reply"},
             "parent_header": {"msg_id": "MATCH"},
             "content": {"status": "ok", "found": True,
                         "data": {"text/plain": "int: x = 41"}}}
    orig = with_dyn_ws(t, [reply])
    try:
        result = json.loads(t.srv.colab_inspect("x"))
    finally:
        colab_persistent._ws_connect = orig
    assert result["found"] is True and "int" in result["info"]


def test_inspect_fallback_compose():
    """Plain variables (empty inspect text) get type + repr composed."""
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    replies = [
        {"header": {"msg_type": "inspect_reply"},
         "parent_header": {"msg_id": "MATCH"},
         "content": {"status": "ok", "found": True, "data": {}}},
        {"header": {"msg_type": "execute_reply"},
         "parent_header": {"msg_id": "MATCH"},
         "content": {"status": "ok", "user_expressions": {
             "t": {"status": "ok", "data": {"text/plain": "'int'"}},
             "r": {"status": "ok", "data": {"text/plain": "'41'"}}}}},
    ]
    orig = with_dyn_ws(t, replies)
    try:
        result = json.loads(t.srv.colab_inspect("x"))
    finally:
        colab_persistent._ws_connect = orig
    assert result["found"] is True and result["info"] == "int x = 41"


def test_check_syntax():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    ok_reply = {"header": {"msg_type": "execute_reply"},
                "parent_header": {"msg_id": "MATCH"},
                "content": {"status": "ok", "user_expressions": {"check": {
                    "status": "ok", "data": {"text/plain": "<code>"}}}}}
    orig = with_dyn_ws(t, [ok_reply])
    try:
        good = json.loads(t.srv.colab_check_syntax("x=1"))
    finally:
        colab_persistent._ws_connect = orig
    assert good["ok"] is True


# ── file transfer ────────────────────────────────────────────────────────────

def test_upload_rest():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    path = os.path.join(tempfile.mkdtemp(), "up.bin")
    open(path, "wb").write(b"abc")
    result = json.loads(t.srv.colab_upload(path, "/content/up.bin"))
    assert result["method"] == "rest" and result["bytes"] == 3
    assert t.calls["put"] == ["/content/up.bin"]


def test_download_rest():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    t.store["contents"]["/content/dl.bin"] = base64.b64encode(b"xyz").decode()
    dest = os.path.join(tempfile.mkdtemp(), "dl.bin")
    result = json.loads(t.srv.colab_download("/content/dl.bin", dest))
    assert result["method"] == "rest" and result["bytes"] == 3
    assert open(dest, "rb").read() == b"xyz"


def test_upload_fallback_kernel():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    import requests
    real_put = requests.put
    requests.put = lambda *a, **k: FakeResp(500)
    path = os.path.join(tempfile.mkdtemp(), "up.bin")
    open(path, "wb").write(b"abc")
    try:
        result = json.loads(t.srv.colab_upload(path, "/content/up.bin"))
    finally:
        requests.put = real_put
    assert result["method"] == "kernel"
    assert t.store["state"]["last_upload"] == b"abc"


def test_download_fallback_kernel():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    t.store["state"]["last_download"] = b"payload!"
    dest = os.path.join(tempfile.mkdtemp(), "dl.bin")
    result = json.loads(t.srv.colab_download("/content/dl.bin", dest))
    assert result["method"] == "kernel"
    assert open(dest, "rb").read() == b"payload!"


def test_remote_path_guard():
    t = fresh()
    result = json.loads(t.srv.colab_download("/etc/passwd", "/tmp/x"))
    assert "error" in result


# ── env snapshot / restore ──────────────────────────────────────────────────

def test_env_snapshot_and_restore():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    snap = json.loads(t.srv.colab_env_snapshot())
    assert snap["python"] == "3.13.15" and snap["packages"] == 2
    assert os.path.exists(colab_persistent.ENV_SNAPSHOT_PATH)
    # rewrite with uninstallable lines: local-version pin + file:// reference
    with open(colab_persistent.ENV_SNAPSHOT_PATH, "w") as f:
        f.write("pkg-a==1.0\ntorch==2.9.0+cu128\nweird @ file:///nope\n")
    t.store["contents"]["/content/relay_env_snapshot.txt"] = "zzz"
    result = json.loads(t.srv.colab_env_restore())
    assert result["restored"] is True and result["packages"] == 1
    assert result["skipped_local_pins"] == 2


def test_cpu_mode_allocates_without_variant():
    t = fresh()
    seen = []
    real_alloc = t.cr.allocate_runtime

    def spy_alloc(token, accelerator):
        seen.append(accelerator)
        return real_alloc(token, accelerator if accelerator else "T4")
    t.cr.allocate_runtime = spy_alloc
    t.srv._run_on_colab("c1", "CPU", 10)
    assert seen == [""]  # CPU must allocate with empty accelerator
    # state still records CPU for switch comparison
    import colab_persistent as cp
    assert cp.state["accelerator"] == "CPU"


# ── expose ──────────────────────────────────────────────────────────────────

def test_expose():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    result = json.loads(t.srv.colab_expose(8099))
    assert result["url"] == "https://x-tunnel.trycloudflare.com"


def test_expose_status():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    result = json.loads(t.srv.colab_expose_status(8099))
    assert result["alive"] is True and result["port"] == 8099


# ── detached jobs ────────────────────────────────────────────────────────────

def test_detached_and_status():
    t = fresh()
    t.srv.colab_execute_detached("train()", job="j1")
    assert t.store["state"]["job"]["state"] == "running"
    reply = {"header": {"msg_type": "execute_reply"},
             "parent_header": {"msg_id": "MATCH"},
             "content": {"status": "ok", "user_expressions": {"job": {
                 "status": "ok",
                 "data": {"text/plain":
                          "{'state': 'done', 'stdout_tail': 'acc 0.91'}"}}}}}
    orig = with_dyn_ws(t, [reply])
    try:
        result = json.loads(t.srv.colab_job_status())
    finally:
        colab_persistent._ws_connect = orig
    assert result["state"] == "done" and result["stdout_tail"] == "acc 0.91"


def test_job_script_has_live_tail():
    assert "_monitor" in colab_persistent.JOB_SCRIPT
    assert "stdout_tail" in colab_persistent.JOB_SCRIPT


# ── multi-kernel ────────────────────────────────────────────────────────────

def test_kernel_new_use_close_list():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    created = json.loads(t.srv.colab_kernel_new("train"))
    assert created["created"] == "train" and created["kernel_id"] == "k2"
    used = json.loads(t.srv.colab_kernel_use("main"))
    assert used["kernel_id"] == "k1"
    t.flag["sessions"] = [
        {"id": "s1", "name": "colab-exec", "kernel": {"id": "k1"}},
        {"id": "s2", "name": "colab-exec", "kernel": {"id": "k2"}},
        {"id": "s3", "name": "other", "kernel": {"id": "k9"}},
    ]
    listing = json.loads(t.srv.colab_kernel_list())
    names = {k["name"]: k for k in listing["kernels"]}
    assert set(names) == {"main", "train", None}
    assert listing["active"] == "main"
    closed = json.loads(t.srv.colab_kernel_close("train"))
    assert closed["closed"] is True and closed["deleted"] is True
    assert any("s2" in u for u in t.calls["deleted"])
    listing2 = json.loads(t.srv.colab_kernel_list())
    assert {k["name"] for k in listing2["kernels"]} == {"main", None}


def test_prune_removes_only_orphans():
    t = fresh()
    t.srv._run_on_colab("c1", "T4", 10)
    t.flag["sessions"] = [
        {"id": "s1", "name": "colab-exec", "kernel": {"id": "k1"}},
        {"id": "s2", "name": "colab-exec", "kernel": {"id": "k2"}},
        {"id": "s3", "name": "colab-exec", "kernel": {"id": "k3"}},
        {"id": "s4", "name": "notours", "kernel": {"id": "k4"}},
    ]
    result = json.loads(t.srv.colab_kernels_prune())
    assert result["count"] == 2
    pruned_kernels = {p["kernel_id"] for p in result["pruned"]}
    assert pruned_kernels == {"k2", "k3"}
    listing = json.loads(t.srv.colab_kernel_list())
    ids = [k["kernel_id"] for k in listing["kernels"]]
    assert "k1" in ids and "k4" in ids


# ── session persistence ─────────────────────────────────────────────────────

def test_session_persist_and_resume():
    path = os.path.join(tempfile.mkdtemp(), "session.json")
    t = fresh(save_path=path)
    t.srv._run_on_colab("c1", "T4", 10)
    saved = json.load(open(path))
    assert saved["kernels"] == {"main": "k1"} and saved["active"] == "main"
    t2 = fresh(save_path=path)
    assert t2.calls["alloc"] == 0 and t2.calls["ka"] == 1
    t2.srv._run_on_colab("c2", "T4", 10)
    assert t2.calls["alloc"] == 0 and t2.calls["session"] == 0


def test_resume_dead_kernels_adopts_runtime():
    path = os.path.join(tempfile.mkdtemp(), "session.json")
    json.dump({"endpoint": "e1", "proxy_url": "http://p1", "proxy_token": "pt",
               "accelerator": "T4", "kernels": {"main": "k1"},
               "active": "main"}, open(path, "w"))
    t = fresh(save_path=path, alive=False)
    # kernels dead -> runtime still adopted; session file kept
    assert os.path.exists(path)
    # But our fake create_session still works -> first call makes k1 again
    out = t.srv._run_on_colab("c1", "T4", 10)
    assert t.calls["alloc"] == 0 and t.calls["ka"] == 1
    assert t.calls["session"] == 1 and "k1" in out[0]


def test_resume_dead_runtime_allocates_fresh():
    path = os.path.join(tempfile.mkdtemp(), "session.json")
    json.dump({"endpoint": "e1", "proxy_url": "http://p1", "proxy_token": "pt",
               "accelerator": "T4", "kernels": {"main": "k1"},
               "active": "main"}, open(path, "w"))
    t = fresh(save_path=path, alive=False)
    t.flag["fail_session"] = True  # runtime is dead: session creation fails
    try:
        t.srv._run_on_colab("c1", "T4", 10)
        raise AssertionError("expected failure")
    except RuntimeError:
        pass
    t.flag["fail_session"] = False
    t.srv._run_on_colab("c2", "T4", 10)
    assert t.calls["alloc"] == 1, "dead runtime must be replaced by fresh alloc"


def test_412_leftover_runtime_recovery():
    t = fresh()
    left = {"count": 0}

    def flaky_alloc(token, accelerator):
        left["count"] += 1
        if left["count"] == 1:
            raise RuntimeError("412 Precondition Failed")
        if accelerator == "":  # discovery call returns the leftover
            return {"endpoint": "e-left", "proxy_url": "http://left",
                    "proxy_token": "pt", "xsrf_token": None, "nbh": "n",
                    "reused": True}
        return {"endpoint": "e-new", "proxy_url": "http://new",
                "proxy_token": "pt", "xsrf_token": None, "nbh": "n",
                "reused": False}

    t.cr.allocate_runtime = flaky_alloc
    t.srv._run_on_colab("c1", "T4", 10)
    # first alloc fails -> discovery ("" call) -> unassign e-left -> retry
    assert left["count"] == 3, left
    unassigned = t.calls["unassign"]
    assert unassigned >= 1
    # final endpoint is e-new
    listing = json.loads(t.srv.colab_kernel_list())
    assert any(k["name"] == "main" for k in listing["kernels"])


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            run(name, fn)
    print(f"{len(PASS)}/{len(PASS)} pass")

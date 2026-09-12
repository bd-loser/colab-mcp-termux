# Troubleshooting

Every failure below was hit during a real Termux install; each entry gives the
symptom and the fix.

## 1. `No module named 'pip._internal.operations.install.wheel'`

**When:** any `pip install`, including build isolation for `maturin`/`cffi`.

**Cause:** Termux's patched pip. Its `_prevent_import_hook` raises for its own
submodule during isolated builds.

**Fix:** use `uv` for installs and `pkg` for native packages:

```bash
pkg install -y uv
uv pip install --system <package>
```

## 2. `Failed to build 'rpds-py'` / `Failed to build 'maturin'`

**When:** `pip install mcp-server-colab-exec`, first attempt.

**Cause:** both need Rust builds Termux does not ship.

**Fix:**

```bash
pkg install -y python-rpds-py        # prebuilt, avoids the rpds-py build
CARGO_BUILD_JOBS=2 cargo install maturin --locked
```

## 3. `Failed to build 'pydantic-core'`

**When:** resolving `pydantic` (dependency of `mcp`).

**Cause:** no Termux wheel exists; `pydantic-core` is a Rust extension.

**Fix:** build it manually with `maturin` and a memory-safe profile. See
[BUILD-FROM-SOURCE.md](BUILD-FROM-SOURCE.md). If the compiler is killed or the
device thrashes, set `CARGO_BUILD_JOBS=1`.

## 4. `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`

**When:** running `mcp-server-colab-exec` after install.

```
This is mcp 2.x, where FastMCP was renamed to MCPServer ... pin 'mcp<2'
```

**Cause:** the server targets the mcp **1.x** API; dependency resolution pulled
`mcp` 2.x (`mcp-server-colab-exec` only requires `mcp[cli]>=1.6.0`).

**Fix:**

```bash
uv pip install --system "mcp[cli]<2"
```

## 5. `[Errno 113] No route to host` or `No address associated with hostname`

**When:** allocating a runtime, or any HTTPS request to
`colab.research.google.com`.

**Cause:** the phone's resolver returns **IPv6-only** answers while IPv6 is
unrouted, and/or the hostname is filtered. Other sites (google.com, pypi.org)
work, which is why it looks like a Colab outage.

**Diagnosis:**

```bash
python3 -c "import socket; print([r[4][0] for r in socket.getaddrinfo('colab.research.google.com',443)])"
# ['2404:...::8a', ...]  -> IPv6-only, no route
curl -s "https://dns.google/resolve?name=colab.research.google.com&type=A"
# returns 216.239.3x.180 -> the IPs are reachable
```

**Fix:** launch the MCP through the DNS wrapper, which prefers IPv4 and falls
back to DNS-over-HTTPS:

```json
"command": ["python3", "/path/to/colab_mcp_dns.py"]
```

## 6. OAuth URL is impossible to paste on a phone

**Fix:** use `scripts/colab-auth.sh`, which captures the URL and opens it with
`termux-open-url` (or `am start`). You can also open it via adb:

```bash
adb shell am start -a android.intent.action.VIEW -d '<url>'
```

Make sure the browser runs on the **same device**, because the redirect target
is `http://localhost:<port>` served by the waiting Python process.

## 7. `Failed to build 'cryptography'`

**Fix:** `pkg install -y python-cryptography` (Termux ships a prebuilt
version that satisfies `pyjwt[crypto]`).

## 8. Runtime seems to vanish between calls

Only with the **plain launcher** (`colab_mcp_dns.py`): the upstream server
unassigns the runtime after every call, so each tool call gets a fresh GPU.
The **persistent launcher** (`colab_persistent.py`) keeps the runtime and
kernel warm across calls and resumes the session after restarts; call
`colab_kernel_reset` to release the GPU on demand.

## 8b. "kernel busy" error from `colab_execute`

Another cell is still running on the kernel (possibly from another client
or process). Use `colab_kernel_busy` to check, `colab_interrupt` to stop
the running cell (state is kept), or `colab_kernel_new` to get a fresh
kernel on the same runtime. The `busy_wait` parameter controls how long a
call waits before failing.

## 8c. HTTP 412 (Precondition Failed) when allocating

Colab allows one runtime per account; a leftover assignment (e.g. from a
crashed process) blocks new allocations. The persistent launcher detects
this and automatically discovers, releases, and retries. To clean up
manually, call `colab_kernel_reset` from a working session or run
`scripts/verify.sh` (which triggers the same recovery).

## 8d. Exposed tunnel URL unreachable from the phone

Fresh `trycloudflare.com` hostnames can take a few seconds to propagate in
public DNS. The DNS wrapper resolves them via DoH endpoints (including
bare-IP forms), so retries usually succeed within seconds; the URL also
changes every time `colab_expose` re-runs.

## 9. `Timed out creating kernel session`

The runtime is still booting. Allocation can take 30-90 s; the client retries
for up to 180 s. Retry the call, or request a smaller accelerator.

## 10. Free-tier T4 unavailable / allocation errors

Free GPU capacity is variable and the internal API is unofficial. Retry later;
try `accelerator="L4"` only if the account has premium capacity. Heavy usage
can be rate-limited by Google.

## Quick health check

```bash
python3 -c "import pydantic_core; print(pydantic_core.__version__)"
python3 -c "import mcp; print(mcp.__file__)"
ls -l ~/.config/colab-exec/token.json
bash scripts/verify.sh
```

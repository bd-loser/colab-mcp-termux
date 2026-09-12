# Architecture

This project makes the **Google Colab GPU MCP server** (`mcp-server-colab-exec`)
run on **Termux / Android** by rebuilding its Rust dependencies from source,
fixing mobile-network DNS, and adding a **persistent warm-kernel launcher**
on top of the server.

```
┌──────────────────────────┐        stdio (JSON-RPC)        ┌──────────────────────────────┐
│  MCP client              │  <───────────────────────────> │  colab_persistent.py         │
│  (opencode, Claude Code, │                                │   ├─ colab_mcp_dns import    │
│   Cursor, Gemini CLI...) │                                │   │   (getaddrinfo patch)   │
└──────────────────────────┘                                │   ├─ warm session registry   │
                                                            │   ├─ busy preflight         │
                                                            │   ├─ kernel control (REST)  │
                                                            │   ├─ silent WS probes       │
                                                            │   └─ mcp-server-colab-exec  │
                                                            └──────────────┬───────────────┘
                                                                            │
                                         OAuth token (cached) + internal API│
                                                                            ▼
                                           ┌──────────────────────────────────────────┐
                                           │ colab.research.google.com                 │
                                           │  GET/POST /tun/m/assign                   │
                                           │  /tun/m/{endpoint}/keep-alive             │
                                           └───────────────┬──────────────────────────┘
                                                           │ runtime proxy url + token
                                                           ▼
                                           ┌──────────────────────────────────────────┐
                                           │ Colab runtime (Tesla T4 / L4 / CPU)       │
                                           │  Jupyter REST: /api/sessions, /api/       │
                                           │   kernels/{id}/{interrupt,restart}        │
                                           │  Kernel WebSocket: execute_request,       │
                                           │   inspect_request, user_expressions       │
                                           └──────────────────────────────────────────┘
```

## Components

| Component | Role |
|---|---|
| `scripts/colab_persistent.py` | MCP entry point. Wraps `mcp-server-colab-exec`: keeps one runtime + named kernels warm across tool calls, persists the session, adds 19 tools (kernel lifecycle, introspection, files, environment, tunnels). |
| `scripts/colab_mcp_dns.py` | DNS layer imported by the launcher. Patches `socket.getaddrinfo` (IPv4-prefer + DNS-over-HTTPS with multiple endpoints) then hands off. Can also run standalone as the plain DNS-patched server. |
| `scripts/colab-auth.sh` | One-time Google OAuth; opens the consent page in the phone browser. |
| `scripts/verify.sh` | Allocates a free T4 and prints the GPU name (end-to-end check). |
| `install.sh` | Reproduces the full source build (maturin + pydantic-core) and installs both launchers. |
| `tests/test_colab_persistent.py` | 36 mock tests for the launcher — fakes for every Colab API, no network or GPU needed. |
| `.github/workflows/termux-wheels.yml` | CI: builds the `pydantic-core` wheel in the official Termux container on arm64 runners, verifies a fresh wheel-only install, publishes wheels to Releases (tag builds). |

## Session persistence layer

State kept by the launcher (in-process, plus a durable copy):

```
state = {
  endpoint, proxy_url, proxy_token,   # the Colab runtime
  accelerator,                        # "T4" | "L4" | "CPU"
  kernels: {name: kernel_id},         # "main" + any created via kernel_new
  active: name,                       # kernel that tool calls target
  stop_event,                         # keep-alive thread control
}
```

* **Persisted** to `~/.config/colab-exec/session.json` (atomic write, mode
  600) after every change; deleted on `colab_kernel_reset`.
* **Resumed** at startup: kernels are liveness-checked via the Jupyter REST
  API. Live kernels are adopted as-is; a live runtime with dead kernels is
  adopted and gets a fresh kernel on first use; a fully dead session is
  discarded.
* **Recovery ladder** on execution failure: transient errors are surfaced;
  a dead kernel gets a new kernel on the same runtime; a dead runtime
  triggers reallocation (first discovering and releasing any leftover
  assignment that would block allocation).
* **Busy preflight**: before executing, the kernel's `execution_state` is
  polled. Only `busy` blocks (waits `busy_wait` seconds, then raises with
  guidance); `starting`/`idle`/unknown pass through.

## Kernel control plane

Standard Jupyter REST endpoints through the runtime proxy, authenticated
with the proxy token header:

| Endpoint | Used by |
|---|---|
| `POST /api/sessions` | kernel creation (unique name per kernel — the proxy dedupes identical identities) |
| `GET /api/sessions` | kernel listing, orphan detection |
| `DELETE /api/sessions/{id}` | kernel close / prune |
| `GET /api/kernels/{id}` | liveness + `execution_state` |
| `POST /api/kernels/{id}/interrupt` | `colab_interrupt` |
| `POST /api/kernels/{id}/restart` | `colab_kernel_restart` (in-place; same kernel id) |
| `PUT/GET /api/contents/{path}` | file upload/download (base64, 25 MB cap) |

## Kernel-protocol probes

`colab_namespace`, `colab_inspect`, and `colab_check_syntax` open a
WebSocket to the kernel's channels and send **silent** requests:
`execute_request` with `user_expressions` (evaluated without polluting
`In[]` history) and `inspect_request`. Replies are matched by
`parent_header.msg_id`. This gives read-style introspection without
executing user code.

## The DNS wrapper

Android's resolver on many handsets returns IPv6-only answers while IPv6 is
unrouted, which surfaces as:

```
[Errno 113] No route to host
socket.gaierror: [Errno 7] No address associated with hostname
```

The wrapper:

* returns IPv4 results (`AF_INET`) when the caller did not ask for IPv6;
* falls back to **DNS-over-HTTPS** (multiple public endpoints, including
  bare-IP forms that work when the resolver refuses the DoH hostname
  itself) when the local resolver answers with no usable IPv4;
* never rewrites the hostname, so TLS SNI and certificate validation are
  unaffected.

It is generic: set `COLAB_MCP_TARGET=module:function` to launch any stdio
server with the same fix.

## Security notes

* OAuth scope is `https://www.googleapis.com/auth/colaboratory` (+ `profile`,
  `email`). Tokens live at `~/.config/colab-exec/token.json` (mode 600).
* `session.json` contains the runtime proxy token and is written with mode
  600; upload/download paths are restricted to `/content` and `/tmp`.
* The wrapper only changes name resolution; it does not log or forward traffic.
* The upstream package uses the Google Colab VS Code extension's OAuth client
  and Colab's **unofficial** internal API. See the disclaimer in the README.

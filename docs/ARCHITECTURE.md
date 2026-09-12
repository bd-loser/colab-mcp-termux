# Architecture

This project makes the **Google Colab GPU MCP server** (`mcp-server-colab-exec`)
run on **Termux / Android** by rebuilding its Rust dependencies from source and
fixing mobile-network DNS.

```
┌──────────────────────────┐        stdio (JSON-RPC)        ┌─────────────────────────────┐
│  MCP client              │  <───────────────────────────> │  colab_mcp_dns.py (wrapper) │
│  (opencode, Claude Code, │                                │   ├─ getaddrinfo patch      │
│   Cursor, Gemini CLI...) │                                │   └─ mcp-server-colab-exec  │
└──────────────────────────┘                                └──────────────┬──────────────┘
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
                                          │ Colab runtime (Tesla T4 / L4)             │
                                          │  Jupyter kernel over WebSocket            │
                                          │  execute_request → stream/result/status   │
                                          └──────────────────────────────────────────┘
```

## Components

| Component | Role |
|---|---|
| `mcp-server-colab-exec` | MCP server exposing `colab_execute`, `colab_execute_file`, `colab_execute_notebook` |
| `scripts/colab_mcp_dns.py` | stdio launcher; patches `socket.getaddrinfo` (IPv4-prefer + DNS-over-HTTPS) then runs the server |
| `scripts/colab-auth.sh` | one-time Google OAuth; opens the consent page in the phone browser |
| `scripts/verify.sh` | allocates a free T4 and prints the GPU name (end-to-end check) |
| `install.sh` | reproduces the full source build (maturin + pydantic-core) and install |

## Request lifecycle (per tool call)

1. Load cached OAuth credentials (refresh if expired).
2. `GET /tun/m/assign` to obtain an XSRF token, then `POST` to allocate a GPU runtime.
3. Start a keep-alive thread (`/tun/m/{endpoint}/keep-alive`, every 60 s).
4. Create a Jupyter kernel via the runtime proxy (`POST /api/sessions`).
5. Send one `execute_request` over the kernel WebSocket; collect `stream`,
   `execute_result`, `error`, and `status: idle`.
6. **Release the runtime** (`/tun/m/unassign/{endpoint}`) in a `finally` block.

### Important consequence: runtimes are ephemeral

Because the upstream server unassigns the runtime after **every** call, this
MCP is designed for **one-shot GPU jobs** - training runs, evaluations,
artifact generation. It is **not** a way to host a long-lived server (e.g. an
LLM inference endpoint) between calls. For that, run a notebook/tunnel yourself.

## The DNS wrapper

Android's resolver on many handsets returns IPv6-only answers while IPv6 is
unrouted, which surfaces as:

```
[Errno 113] No route to host
socket.gaierror: [Errno 7] No address associated with hostname
```

The wrapper:

* returns IPv4 results (`AF_INET`) when the caller did not ask for IPv6;
* falls back to **DNS-over-HTTPS** (`https://dns.google/resolve`, type `A`)
  when the local resolver answers with no usable IPv4;
* never rewrites the hostname, so TLS SNI and certificate validation are
  unaffected.

It is generic: set `COLAB_MCP_TARGET=module:function` to launch any stdio
server with the same fix.

## Security notes

* OAuth scope is `https://www.googleapis.com/auth/colaboratory` (+ `profile`,
  `email`). The token lives at `~/.config/colab-exec/token.json` (mode 600).
* The wrapper only changes name resolution; it does not log or forward traffic.
* The upstream package uses the Google Colab VS Code extension's OAuth client
  and Colab's **unofficial** internal API. See the disclaimer in the README.

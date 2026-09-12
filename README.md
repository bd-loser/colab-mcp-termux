# Google Colab MCP on Termux (Android) — persistent warm kernels

> Run **Google Colab GPU runtimes (T4 / L4 / CPU)** from any **MCP** client —
> opencode, Claude Code, Gemini CLI, Cursor, Cline — **directly from an
> Android phone via Termux**. No root, no desktop, no prebuilt wheels
> required. Runtimes stay **warm between calls**, so a model loaded once
> stays in VRAM.

`mcp-server-colab-exec` normally installs in seconds on a Linux desktop. On
**Termux aarch64** it does not: `pydantic-core` has no Android wheel, Termux's
`pip` cannot build it, and mobile DNS often breaks Colab outright. This
project ships a **one-command installer** plus two launchers — a
**DNS-over-HTTPS wrapper** and a **persistent-kernel launcher** exposing
**23 MCP tools** — verified end-to-end on a real Tesla T4.

```
python 3.13.15 | torch 2.11.0+cu128 | cuda True | gpu Tesla T4
```

---

## Why this exists

| Problem on Termux / Android | Fix in this repo |
|---|---|
| `pydantic-core` (Rust) has no Termux wheel | Builds it from source with a **memory-safe Cargo profile** |
| No wheel builder (`maturin`) for aarch64 | Installs `maturin` from crates.io |
| Termux `pip` breaks: `No module named pip._internal.operations.install.wheel` | Uses **`uv`** for all Python installs |
| `mcp 2.x` removed `FastMCP` → server crashes | Pins **`mcp[cli]<2`** |
| Resolver returns IPv6-only → `[Errno 113] No route to host` | **DNS-over-HTTPS + IPv4 preference** wrapper |
| OAuth URL impossible to paste on a phone | Opens the consent page in the browser automatically |
| Upstream server releases the GPU after every call → model reloads each request | **Persistent-kernel launcher** keeps one runtime + kernel warm across calls |

---

## Quick start

```bash
git clone https://github.com/bd-loser/colab-mcp-termux.git
cd colab-mcp-termux
bash install.sh              # installs deps + builds maturin & pydantic-core
bash scripts/colab-auth.sh   # one-time Google sign-in (opens your browser)
bash scripts/verify.sh       # allocates a free T4 and prints the GPU
```

Then point your MCP client at the persistent launcher:

```json
{
  "mcp": {
    "colab-exec": {
      "type": "local",
      "command": [
        "/data/data/com.termux/files/usr/bin/python3",
        "/data/data/com.termux/files/home/.local/share/colab-mcp/colab_persistent.py"
      ],
      "enabled": true
    }
  }
}
```

Full example: [`examples/opencode.mcp.json`](examples/opencode.mcp.json).

> The two Rust builds take **15-40 minutes** on a phone. `install.sh` is
> idempotent — re-running skips completed steps.

---

## What you get

**23 MCP tools** (full reference: [`docs/TOOLS.md`](docs/TOOLS.md)):

| Category | Tools |
|---|---|
| Execution (warm) | `colab_execute`, `colab_execute_file`, `colab_execute_notebook` |
| Background jobs | `colab_execute_detached`, `colab_job_status` |
| Kernel lifecycle | `colab_kernel_busy`, `colab_kernel_info`, `colab_interrupt`, `colab_kernel_restart`, `colab_kernel_reset`, `colab_kernel_new`, `colab_kernel_use`, `colab_kernel_list`, `colab_kernel_close`, `colab_kernels_prune` |
| Introspection | `colab_namespace`, `colab_inspect`, `colab_check_syntax` |
| Files | `colab_upload`, `colab_download` |
| Environment | `colab_env_snapshot`, `colab_env_restore` |
| Networking | `colab_expose`, `colab_expose_status` |

Typical uses: load a model once and serve it for the session, run training
jobs in the background with a live log tail, push datasets and pull
adapters, publish an inference endpoint on a public URL — all from a chat
client on the phone.

---

## Requirements

* Termux (F-Droid or GitHub build) on **aarch64**
* Python 3.10+ (works on 3.14)
* A Google account with Colab access (for the free T4)
* ~2 GB free storage during the build; lower `CARGO_BUILD_JOBS` on small devices

---

## How it works

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

```
MCP client ──stdio──▶ colab_persistent.py ──▶ mcp-server-colab-exec
                      ├─ DNS patch (IPv4 + DoH)
                      ├─ warm session + kernel registry          │
                      └─ kernel control / WS probes               ▼
                                          Colab internal API (/tun/m/assign)
                                                               │
                                                               ▼
                                          T4 runtime · Jupyter kernels · WebSocket
```

### Runtimes are warm by default

The launcher keeps one runtime and its kernels alive across tool calls
(the upstream server releases the GPU after every call). The session is
persisted to `~/.config/colab-exec/session.json` and resumed after client
restarts; `colab_kernel_reset` releases the GPU on demand. Holding the
runtime is equivalent to keeping a notebook open in a browser — free-tier
session limits and quotas still apply.

---

## Documentation

* [Tool reference](docs/TOOLS.md) — all 23 tools, parameters, examples
* [Architecture](docs/ARCHITECTURE.md) — components, session model, DNS wrapper
* [Building the Rust deps from source](docs/BUILD-FROM-SOURCE.md) — `maturin`,
  `pydantic-core`, memory-safe Cargo profile, `uv`
* [Troubleshooting](docs/TROUBLESHOOTING.md) — known failure modes and fixes
* [Tests](tests/test_colab_persistent.py) — 36 mock tests for the launcher
  (no network, no GPU required)

---

## FAQ

**Can I run Google Colab on Termux / Android?**
Yes. This repo runs a Colab GPU MCP server on Termux and executes Python on a
real Tesla T4, with no root access.

**Is there an official Google Colab MCP server?**
Yes — [`googlecolab/colab-mcp`](https://github.com/googlecolab/colab-mcp).
However, it bridges to a **browser-based Colab session** via WebSocket, which
requires a human to open Colab in a browser and click "Connect". On headless
Termux there is no browser tab to bridge to. We use
[`pdwi2020/mcp-server-colab-exec`](https://github.com/pdwi2020/mcp-server-colab-exec)
(PyPI: `mcp-server-colab-exec`) instead — it executes code via Colab's API
directly, no browser needed.

**Does this work on non-rooted phones?**
Yes. Only Termux packages and user-space Python are used.

**Why does it need DNS-over-HTTPS?**
Many Android resolvers return IPv6-only answers for `colab.research.google.com`
while IPv6 is unrouted, producing `No route to host`. The wrapper prefers IPv4
and resolves via public DoH endpoints when needed.

**Can it host a model server?**
Within a session, yes: load the model once, start an HTTP server in a
background thread, and call `colab_expose` to get a public HTTPS URL. The
model stays in VRAM for the life of the runtime; re-running `colab_expose`
replaces a dead tunnel without reloading. This is not a permanent hosting
solution — free-tier runtimes are reclaimed eventually.

**How long does installation take?**
~15-40 minutes, almost entirely compiling `maturin` and `pydantic-core`.

---

## Disclaimer

`mcp-server-colab-exec` drives Google Colab through its **unofficial internal
API** using the Colab VS Code extension's OAuth client. This may be
rate-limited, blocked, or changed by Google at any time, and may be subject to
Colab's Terms of Service. Free GPU capacity is variable. Holding runtimes
between calls is equivalent to keeping a notebook open in a browser. Use
responsibly and at your own risk. This repository provides build tooling and a
network fix only; it does not bundle Google credentials.

## Credits

* [`pdwi2020/mcp-server-colab-exec`](https://github.com/pdwi2020/mcp-server-colab-exec)
  — the MCP server (PyPI: `mcp-server-colab-exec`; author Paritosh Dwivedi).
  This repo packages and fixes it for Termux.
* [`googlecolab/colab-mcp`](https://github.com/googlecolab/colab-mcp) —
  Google's official Colab MCP (browser-bridged, not used here).
* [Astral `uv`](https://github.com/astral-sh/uv) — Python packaging
* [PyO3 / maturin](https://github.com/PyO3/maturin) — Rust Python extensions

## License

MIT — see [LICENSE](LICENSE).

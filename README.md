# Google Colab MCP Server on Termux (Android) — complete source build

> Run **Google Colab GPU runtimes (T4 / L4)** from any **MCP** client —
> opencode, Claude Code, Gemini CLI, Cursor, Cline — **directly from an
> Android phone via Termux**. No root, no desktop, no prebuilt wheels required.

`mcp-server-colab-exec` normally installs in seconds on a Linux desktop. On
**Termux aarch64** it does not: `pydantic-core` has no Android wheel, Termux's
`pip` cannot build it, and mobile DNS often breaks Colab outright. This project
ships a **one-command installer** plus a **DNS-over-HTTPS wrapper** that makes it
work end-to-end — verified by allocating a real Tesla T4.

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

---

## Quick start

```bash
git clone https://github.com/bd-loser/colab-mcp-termux.git
cd colab-mcp-termux
bash install.sh          # installs deps + builds maturin & pydantic-core
bash scripts/colab-auth.sh   # one-time Google sign-in (opens your browser)
bash scripts/verify.sh       # allocates a free T4 and prints the GPU
```

Then point your MCP client at the DNS-patched launcher:

```json
{
  "mcp": {
    "colab-exec": {
      "type": "local",
      "command": [
        "/data/data/com.termux/files/usr/bin/python3",
        "/data/data/com.termux/files/home/.local/share/colab-mcp/colab_mcp_dns.py"
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

Three MCP tools, callable by any MCP-compatible assistant:

| Tool | Description |
|---|---|
| `colab_execute` | Run inline Python on a Colab GPU (T4 / L4) |
| `colab_execute_file` | Run a local `.py` file on a Colab GPU |
| `colab_execute_notebook` | Run code **and download generated artifacts** (models, CSVs, images) to the phone |

Typical uses: `pip install` something on a T4, train or evaluate a model,
generate an artifact, read the GPU name — all from a chat client.

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
MCP client  ──stdio──▶  colab_mcp_dns.py  ──▶  mcp-server-colab-exec
                        (getaddrinfo patch)         │
                                                    ▼
                                 Colab internal API (/tun/m/assign)
                                                    │
                                                    ▼
                                 T4 runtime · Jupyter kernel · WebSocket
```

### Runtimes are ephemeral (by design)

The upstream server **releases the GPU after every call**. This is ideal for
**one-shot GPU jobs** (training, evaluation, artifact generation) and is *not*
a way to host a long-running server between calls. Plan one call per job.

---

## Documentation

* [Architecture](docs/ARCHITECTURE.md) — components, lifecycle, DNS wrapper
* [Building the Rust deps from source](docs/BUILD-FROM-SOURCE.md) — `maturin`,
  `pydantic-core`, memory-safe Cargo profile, `uv`
* [Troubleshooting](docs/TROUBLESHOOTING.md) — every error encountered, with fixes

---

## FAQ

**Can I run Google Colab on Termux / Android?**
Yes. This repo runs a Colab GPU MCP server on Termux and executes Python on a
real Tesla T4, with no root access.

**Is there an official Google Colab MCP server?**
No. `mcp-server-colab-exec` is community software that uses Colab's internal
API. This project only packages and fixes it for Termux.

**Does this work on non-rooted phones?**
Yes. Only Termux packages and user-space Python are used.

**Why does it need DNS-over-HTTPS?**
Many Android resolvers return IPv6-only answers for `colab.research.google.com`
while IPv6 is unrouted, producing `No route to host`. The wrapper prefers IPv4
and resolves via `dns.google` when needed.

**Can it host a model server permanently?**
No. Each tool call allocates and releases the runtime. For persistent hosting,
run a notebook/tunnel yourself.

**How long does installation take?**
~15-40 minutes, almost entirely compiling `maturin` and `pydantic-core`.

---

## Disclaimer

`mcp-server-colab-exec` drives Google Colab through its **unofficial internal
API** using the Colab VS Code extension's OAuth client. This may be
rate-limited, blocked, or changed by Google at any time, and may be subject to
Colab's Terms of Service. Free GPU capacity is variable. Use responsibly and at
your own risk. This repository provides build tooling and a network fix only;
it does not bundle Google credentials.

## Credits

* [`pdwi2020/mcp-server-colab-exec`](https://github.com/pdwi2020/mcp-server-colab-exec) — the MCP server
* [Astral `uv`](https://github.com/astral-sh/uv) — Python packaging
* [PyO3 / maturin](https://github.com/PyO3/maturin) — Rust Python extensions

## License

MIT — see [LICENSE](LICENSE).

# Tool reference

The persistent-kernel launcher (`scripts/colab_persistent.py`) wraps the
upstream `mcp-server-colab-exec` server and exposes **23 MCP tools** for
driving Google Colab from any MCP client. Everything runs over the same
installed server; the launcher adds session management, kernel control,
introspection, file transfer, and tunnel publishing on top.

---

## Session model

| Concept | Behavior |
|---|---|
| **Runtime** | One Colab runtime per Google account (T4, L4, or CPU). Allocated on first use and **kept warm** — not released after each call. |
| **Kernel** | A Jupyter kernel on the runtime. The first one is named `main`; more can be created with `colab_kernel_new`. Kernels on one runtime share the GPU but have independent namespaces. |
| **Warm state** | Variables, imported modules, and loaded models persist across tool calls on the same kernel. A model loaded once stays in VRAM for all later calls. |
| **Session resume** | Runtime + kernel IDs are persisted to `~/.config/colab-exec/session.json` (mode 600). After a client or process restart, the launcher reconnects to the same kernel instead of reallocating. |
| **Busy preflight** | Before executing, the kernel's `execution_state` is checked. If another cell is running, the call waits `busy_wait` seconds, then fails with guidance to use `colab_interrupt` or `colab_kernel_new`. |
| **Recovery ladder** | On failure the launcher distinguishes transient errors (surfaced as-is), a dead kernel (new kernel on the same runtime), and a dead runtime (fresh allocation, including automatic release of leftover assignments that would otherwise block allocation with HTTP 412). |
| **Release** | `colab_kernel_reset` unassigns the runtime and frees the GPU. Free-tier session limits and quotas still apply; holding a runtime between calls is equivalent to keeping a notebook open in a browser. |

Accelerator values: `"T4"` (free GPU, default), `"L4"` (premium GPU),
`"CPU"` (no GPU — saves GPU quota for jobs that need it). Switching
accelerator type releases the old runtime first.

---

## Execution

### `colab_execute`

Run Python code on the warm kernel. Code is split on blank lines into
virtual cells and results are returned per cell.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `code` | string | — | Python code (required) |
| `accelerator` | string | `"T4"` | `"T4"`, `"L4"`, or `"CPU"` |
| `timeout` | int | `300` | Max execution seconds |
| `busy_wait` | int | `10` | Seconds to wait if the kernel is busy |

Returns: `{"cells": [{cell_num, stdout, status}], "errors": [...], "stderr", "exit_code"}`

```python
colab_execute(code="import torch\nx = 41\nprint(torch.cuda.get_device_name(0))")
colab_execute(code="print(x + 1)")   # warm: 41 -> 42, no reload
```

### `colab_execute_file` *(upstream)*

Execute a local `.py` file on the warm kernel. `file_path` must be a `.py`
file inside the workspace. Same parameters plus `file_path`.

### `colab_execute_notebook` *(upstream)*

Execute code on the warm kernel and collect generated artifacts (images,
CSVs, models, …) as a zip into a local `output_dir`. Zip members are
validated before extraction.

---

## Background jobs

### `colab_execute_detached`

Start a long-running job on the warm kernel **without blocking** the call.
The code runs in a background thread; state and progress are written to
`/content/job.json` on the runtime. One job per kernel.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `code` | string | — | Python code (required) |
| `job` | string | `"job"` | Job name (label in status output) |
| `accelerator` | string | `"T4"` | Accelerator type |
| `timeout` | int | `120` | Launch timeout |
| `busy_wait` | int | `10` | Busy preflight wait |

```python
colab_execute_detached(code="train_model()", job="train")
```

### `colab_job_status`

Read the last detached job's state: `running` / `done` / `error`, start and
end times, the last 4 KB of stdout (refreshed every ~2 s while running),
and the error text if it failed.

```python
colab_job_status()   # {"job": "train", "state": "running", "stdout_tail": "epoch 3 loss 0.42", ...}
```

---

## Kernel lifecycle

### `colab_kernel_busy`

Report whether the active kernel is currently running a cell
(`{"busy": true, "execution_state": "busy"}`). Read-only, no side effects.

### `colab_kernel_info`

Probe the active kernel: liveness, endpoint, kernel id, Python version,
GPU name, and free/total VRAM.

```python
colab_kernel_info()   # {"alive": true, "gpu": "Tesla T4", "vram_free_gb": 14.5, ...}
```

### `colab_interrupt`

Interrupt the busy kernel. The running cell raises `KeyboardInterrupt`;
the kernel stays alive with all state (loaded models are kept). Any
`colab_execute` waiting on the kernel returns immediately after. Does not
release the GPU.

### `colab_kernel_restart`

Restart the active kernel **in place**. The namespace and VRAM are cleared
(models must be reloaded) but pip-installed packages and the GPU assignment
survive — no reallocation delay.

### `colab_kernel_reset`

Drop all kernels and **release the runtime** (frees the GPU immediately).
The next call allocates a fresh runtime. Use when done with GPU work.

### `colab_kernel_new` / `colab_kernel_use` / `colab_kernel_list` / `colab_kernel_close`

Multi-kernel support on one runtime:

- `colab_kernel_new(name)` — create an additional kernel with its own
  namespace and switch to it (e.g. a training kernel and a serving kernel
  sharing one T4).
- `colab_kernel_use(name)` — switch the active kernel.
- `colab_kernel_list()` — list all kernels on the runtime, ours (named)
  and any others, with the active one marked.
- `colab_kernel_close(name="")` — close a kernel (default: the active
  one) and free its memory; the runtime stays warm.

### `colab_kernels_prune`

Delete orphan `colab-exec*` sessions on the runtime — kernels left behind
by crashed processes that this launcher did not register. Pass
`all=true` to delete every colab-exec kernel including ours.

---

## Introspection

All three tools use silent kernel-protocol probes: nothing executes,
nothing is added to the kernel's `In[]` history.

### `colab_namespace`

List user variables in the active kernel as `(name, type)` pairs.

```python
colab_namespace()   # {"count": 2, "names": [["x", "int"], ["torch", "module"]]}
```

### `colab_inspect`

Inspect a kernel variable: type, value (for plain objects), or
signature/docstring (for functions and modules).

```python
colab_inspect("x")         # {"found": true, "info": "int x = 41"}
colab_inspect("model")     # signature + docstring of a loaded model
```

### `colab_check_syntax`

Validate Python syntax on the kernel without running the code.

```python
colab_check_syntax("def broken(:")   # {"ok": false, "error": "invalid syntax ..."}
```

---

## File transfer

### `colab_upload`

Upload a local file to the runtime. Paths must be under `/content` or
`/tmp`. Uses the Jupyter contents REST API (up to 25 MB) with a kernel-cell
fallback (up to 8 MB).

| Parameter | Type | Description |
|---|---|---|
| `file_path` | string | Local file (required) |
| `remote_path` | string | Runtime path, e.g. `/content/data.jsonl` (required) |
| `timeout` | int | Transfer timeout (default 120) |

### `colab_download`

Download a file from the runtime to the phone. Same path rules and
transport ladder as upload.

```python
colab_download(remote_path="/content/adapter.safetensors",
               file_path="~/models/adapter.safetensors")
```

---

## Environment

### `colab_env_snapshot`

Capture the runtime's Python version and `pip freeze` to the phone
(`~/.config/colab-exec/env_snapshot.txt`). Use before a session you may
want to reproduce.

### `colab_env_restore`

Reinstall a snapshot on the current runtime (uploads the file and runs
`pip install -r`). Fast when packages are already satisfied. Lines that
only resolve inside Colab's own images (local-version pins like
`torch==2.x+cu128`, direct `file:///` references) are skipped and reported
in `skipped_local_pins`.

---

## Networking

### `colab_expose`

Publish a port on the runtime as a public HTTPS URL via a cloudflared
quick tunnel, and return the `https://*.trycloudflare.com` address.
Re-running replaces the tunnel on that port.

```python
colab_expose(port=8081)   # {"port": 8081, "url": "https://....trycloudflare.com"}
```

### `colab_expose_status`

Check the tunnel for a port: alive, URL, pid.

### Typical serving flow

```python
colab_execute(code=MODEL_LOAD_CELL, timeout=900)      # once; stays in VRAM
colab_execute(code=SERVER_START_CELL)                 # daemon thread, returns
url = colab_expose(port=8081)["url"]                  # public endpoint
# tunnel died? just:
colab_expose(port=8081)                               # model never reloads
```

---

## Limits and semantics

- One detached job per kernel; a second `colab_execute_detached` replaces
  the previous job's status file.
- A cell that outlives its `timeout` keeps the kernel busy; follow with
  `colab_interrupt` (kernel kept) or `colab_kernel_restart` (state lost).
- Quick-tunnel URLs are one-shot and change on re-expose; they are not a
  fixed hostname.
- `store_history` is on for executed cells (`In[]` grows) — use
  `colab_kernel_restart` to clear.
- Free-tier runtimes are still reclaimed by Colab (idle/12 h limits); the
  launcher recovers by reallocating, and Drive-cached model files
  (`HF_HOME` on Drive) make cold reloads fast.

# Building the Rust dependencies from source

`mcp-server-colab-exec` depends on `mcp[cli]`, which depends on `pydantic`,
which depends on **`pydantic-core`** - a Rust extension. Termux has no
prebuilt wheel for `pydantic-core` (manylinux wheels are glibc-based and do
not load against Android's bionic libc), so it must be compiled on the device.
Compiling it needs a wheel builder: **`maturin`**, itself a Rust program.

So two Rust builds are required: `maturin`, then `pydantic-core`.

## 1. `maturin`

```bash
pkg install -y rust clang cmake make binutils
CARGO_BUILD_JOBS=2 cargo install maturin --locked
maturin --version   # 1.15.0 at the time of writing
```

`cargo install` builds the CLI into `~/.cargo/bin/maturin`.

## 2. `pydantic-core`

Find the version pydantic pins, download the sdist, build a wheel, install it:

```bash
# pydantic-core==X.Y.Z  <- from pydantic's metadata
PDC=$(curl -fsSL https://pypi.org/pypi/pydantic/json | python3 -c \
  'import json,sys; d=json.load(sys.stdin); print(next(r.split("==")[1] for r in d["info"]["requires_dist"] if r.startswith("pydantic-core==")))')

SRC=$(curl -fsSL "https://pypi.org/pypi/pydantic-core/$PDC/json" | python3 -c \
  'import json,sys; d=json.load(sys.stdin); print(next(u["url"] for u in d["urls"] if u["packagetype"]=="sdist"))')

curl -fsSL "$SRC" -o pydantic_core.tar.gz
tar xzf pydantic_core.tar.gz
cd "pydantic-core-$PDC"

# Memory-safe profile (see below), then build:
CARGO_BUILD_JOBS=2 \
CARGO_PROFILE_RELEASE_LTO=thin \
CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 \
CARGO_PROFILE_RELEASE_STRIP=true \
maturin build --release --out dist -i python3

uv pip install --system dist/pydantic_core-*.whl
```

The result is a Termux-tagged wheel, e.g.
`pydantic_core-2.46.5-cp314-cp314-android_24_arm64_v8a.whl`.

## Why the profile overrides matter

`pydantic-core`'s own `Cargo.toml` uses:

```toml
[profile.release]
lto = "fat"
codegen-units = 1
strip = true
```

Fat LTO with a single codegen unit is the most memory-hungry configuration
possible. On a phone with ~4 GB RAM it either OOMs or never finishes. Cargo
reads profile values from the environment, so we override **without editing
the source**:

| Variable | Value | Effect |
|---|---|---|
| `CARGO_PROFILE_RELEASE_LTO` | `thin` | far lower peak memory than `fat` |
| `CARGO_PROFILE_RELEASE_CODEGEN_UNITS` | `16` | parallel codegen, less RAM per unit |
| `CARGO_PROFILE_RELEASE_STRIP` | `true` | keep the resulting wheel small |
| `CARGO_BUILD_JOBS` | `2` (or `1`) | cap concurrent rustc processes |

The trade-off is a marginally slower extension at runtime - irrelevant for
orchestration code that runs a few times per session.

## Installing Python packages on Termux

Termux's `pip` is patched and can fail during install or build isolation with:

```
ImportError: No module named 'pip._internal.operations.install.wheel'
```

Use **`uv`** (fast, self-contained, not affected by that patch):

```bash
pkg install -y uv
uv pip install --system mcp-server-colab-exec "mcp[cli]<2"
```

Native Python packages that Termux already provides should come from `pkg`,
not pip, to avoid Rust builds:

```bash
pkg install -y python-rpds-py python-cryptography
```

* `python-rpds-py` -> required by `jsonschema` (otherwise a Rust build).
* `python-cryptography` -> required by `pyjwt[crypto]` (otherwise a Rust build).

## Verify

```bash
python3 -c "import pydantic_core; print(pydantic_core.__version__)"
mcp-server-colab-exec --help 2>&1 | head    # will start stdio server; Ctrl-C
```

Then run `scripts/verify.sh` to allocate a real T4.

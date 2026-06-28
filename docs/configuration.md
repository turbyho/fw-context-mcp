# Configuration

Complete reference for `.fw-context/config.toml` and `.fw-context/local.toml` —
global defaults, shared project settings, local developer overrides, and every
available setting.

## How config works

Three levels of TOML files, merged in order (later overrides earlier):

```
~/.fw-context/config.toml                global defaults (apply to all projects)
        │
        ├── merged with ──→  <project>/.fw-context/config.toml   shared project config (commit to git)
        │
        ├── merged with ──→  <project>/.fw-context/local.toml    local developer overrides (gitignored)
        │
        ▼
    final Config used by fw-context
```

**Why two project files?** `config.toml` holds settings that are the same for
everyone on the project — build parameters, source roots, excludes. Commit it
to git. `local.toml` holds developer-specific settings — which Ollama model you
have installed, where your index database lives, whether you want LLM analysis
enabled. Keep it out of git (add to `.gitignore`).

All files are auto-created with commented-out defaults on first use.

## Settings reference

### `[build]` — Build system

Controls how `fw-context index` generates `compile_commands.json`.
Used only when running without `--no-build`.

> **⚠️ Every `fw-context index` without `--no-build` triggers a full
> re-index** — the build step regenerates ``compile_commands.json``,
> which produces a new config hash, so every translation unit is
> re-parsed from scratch.  For fast incremental indexing (seconds),
> use ``fw-context index --no-build`` after the initial run.

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `system` | *(auto-detect)* | project | Build system: `"mbed-os"`, `"zephyr"`, `"platformio"`. Auto-detected from project markers (`west.yml`, `.mbed`, `platformio.ini`, etc.) |
| `clean` | `true` | project | Always do a clean build before generating. **Recommended** — ensures complete `compile_commands.json`. Set `false` or use `--no-clean` for incremental builds. Note: a clean build forces a full re-index. |
| `command` | *(none)* | project | Full build command override. Bypasses all auto-detection. Example: `"bear -- make -j4"`. |
| `target` | *(auto-detect)* | project | Mbed OS target board. Auto-detected from `.mbed` or `custom_targets.json`. |
| `toolchain` | *(auto-detect)* | project | Mbed OS toolchain. Auto-detected from `.mbed`. |
| `profile` | `"develop"` | project | Mbed OS build profile. `develop` is best for indexing (includes `-g` debug symbols). |
| `app_config` | `"mbed_app.json"` | project | Mbed OS app configuration file. |
| `extra_profiles` | `["lto.json"]` | project | Additional Mbed OS profiles (resolved relative to `mbed-os/tools/profiles/extensions/`). |
| `defines` | `[]` | project | Extra preprocessor macros passed to the compiler (`-D` flags). Example: `["VERSION_FW_MAJOR=4", "DEV"]`. Useful for conditional code paths — ensures `#ifdef DEV` branches are indexed. |
| `board` | *(required)* | project | Zephyr board name. Must be set explicitly for Zephyr projects. |

### `[index]` — Indexer

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `db_dir` | `"~/.fw-context/index"` | global, local | Directory for SQLite index databases. One subdirectory per project. |
| `compile_commands` | `"compile_commands.json"` | project | Path to compilation database. Relative paths resolved from project root. |
| `source_roots` | `[]` *(auto-detect)* | project | Directories to scan for symbols. **Empty = auto-detect** — scans `src`, `lib`, `app`, `include`, `modules`, `drivers` + framework dirs (`zephyr/`, `mbed-os/`) + top-level dirs from `compile_commands.json`. Set explicitly to narrow or extend: `["src", "lib", "/path/to/framework"]`. |
| `exclude_paths` | `["build", "BUILD"]` | project | Directories to skip. Useful for generated code, test fixtures, vendored code. |
| `index_refs` | `true` | project | Build cross-reference / call graph. On by default — enables `find_callers`, `find_call_path`, `find_dead_code`, etc. Set `false` or pass `--no-refs` for faster indexing on very large projects. |
| `index_embeddings` | `true` | project | Generate vector embeddings during indexing. Requires Ollama. Embeddings power semantic search and hybrid FTS5+vector re-ranking. Disable with `false` or `--no-embeddings`. |

### `[llm]` — Ollama

These settings belong in `~/.fw-context/config.toml` (global defaults) or
`<project>/.fw-context/local.toml` (per-project overrides). They should NOT
go in the shared `config.toml` — LLM configuration is developer-specific.

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `enabled` | `true` | global, local | Enable Ollama. When `false`, `smart_search` falls back to word-split FTS5, `explain_symbol` returns source + prompt for the AI assistant. |
| `ollama_url` | `"http://localhost:11434"` | global, local | Ollama API base URL. Change for remote GPU servers. |
| `model` | `"qwen2.5-coder:14b"` | global, local | LLM model tag. Override per-project for different codebases. |
| `embed_model` | `"mxbai-embed-large:latest"` | global, local | Embedding model for vector search. Auto-pulled on first use. |
| `num_ctx` | `16384` | global, local | Context window in tokens. Accommodates full function bodies during analysis generation. |
| `analyze_symbols` | `true` | global, local | Generate per-symbol LLM analysis (summary, inputs, outputs) during indexing. Stored in `llm_analysis` table — symbols become searchable by purpose. |
| `analyze_files` | `true` | global, local | Generate per-file LLM summaries (2–3 sentences) during indexing. Returned by `get_file_analysis` and as `file_summary` in `get_file_map`. |
| `debug_log` | *(none)* | global, local | Path to JSONL debug log for Ollama prompts + responses. Example: `"~/.fw-context/llm-debug.jsonl"`. |

### `[project]` — Metadata

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `name` | *(directory name)* | project | Human-readable project name shown in `fw-context list` and status output. |

## Examples

### Global config (`~/.fw-context/config.toml`)

```toml
[index]
db_dir = "~/.fw-context/index"

[llm]
# enabled = true   # set false to disable Ollama entirely
ollama_url = "http://localhost:11434"
model = "qwen2.5-coder:14b"
num_ctx = 16384
# debug_log = "~/.fw-context/llm-debug.jsonl"
```

### Shared project config (`<project>/.fw-context/config.toml`)

Commit this file to git. It contains settings that are the same for all developers.

#### Zephyr

```toml
[project]
name = "my-zephyr-app"

[build]
# system = "zephyr"                      # auto-detected from west.yml
board = "nrf52840dk_nrf52840"            # required — your board name
# clean = true                           # pristine build (recommended)

[index]
compile_commands = "build/compile_commands.json"
source_roots = []
exclude_paths = ["build", "BUILD"]
```

#### PlatformIO / Arduino

```toml
[project]
name = "my-pio-project"

[build]
# system = "platformio"                  # auto-detected from platformio.ini

[index]
compile_commands = "compile_commands.json"
source_roots = [
    "src",
    "lib",
    "/home/user/.platformio/packages/framework-arduinoespressif32",
]
exclude_paths = [".pio", "build", "BUILD"]
```

#### Mbed OS

```toml
[project]
name = "my-mbed-app"

[build]
# system = "mbed-os"                     # auto-detected from .mbed
# target = "P_ECB_BOARD"                # override auto-detected target
# toolchain = "GCC_ARM"                  # auto-detected from .mbed
# profile = "develop"                    # best for indexing
# app_config = "mbed_app.json"
# extra_profiles = ["lto.json"]
# defines = ["VERSION_FW_MAJOR=4", "DEV"]  # extra -D macros

[index]
compile_commands = "compile_commands.json"
source_roots = []
exclude_paths = ["build", "BUILD"]
```

### Local developer config (`<project>/.fw-context/local.toml`)

Keep this file out of git (add to `.gitignore`). It overrides settings from
`config.toml` and the global config — use it for developer-specific preferences.

```toml
[llm]
# enabled = false                        # set false if you don't use Ollama
# ollama_url = "http://localhost:11434"
# model = "qwen2.5-coder:14b"           # override if you have a different model
# analyze_symbols = true
# analyze_files = true

[index]
# db_dir = "~/.fw-context/index"        # override if you store indexes elsewhere
```

## Source root auto-detection

When `source_roots` is empty (default), the indexer scans:

1. **Common source dirs:** `src`, `lib`, `app`, `include`, `drivers`, `modules`
2. **Framework dirs:** `zephyr`, `mbed-os` (if present at project root)
3. **Top-level dirs from `compile_commands.json`** — any directory containing
   at least one translation unit

The `compile_commands.json` determines *which* translation units are parsed;
the `#include` chain pulls in OS headers automatically. Result: framework
symbols your project actually uses are indexed without manual configuration.

Set `source_roots` explicitly only when you need to narrow the scope or
include directories outside the project root (e.g. PlatformIO frameworks).

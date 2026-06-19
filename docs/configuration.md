# Configuration

Complete reference for `.fw-context/config.toml` — global defaults,
per-project overrides, and every available setting.

## How config works

Two TOML files, merged with **project overrides global**:

```
~/.fw-context/config.toml          global defaults (apply to all projects)
        │
        ├── merged with ──→  <project>/.fw-context/config.toml   project overrides
        │
        ▼
    final Config used by fw-context
```

Both files are auto-created with sensible defaults on first use.

## Settings reference

### `[build]` — Build system

Controls how `fw-context index` generates `compile_commands.json`.
Used only when running without `--no-build`.

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `system` | *(auto-detect)* | project | Build system: `"mbed-os"`, `"zephyr"`, `"platformio"`. Auto-detected from project markers (`west.yml`, `.mbed`, `platformio.ini`, etc.) |
| `clean` | `true` | project | Always do a clean build before generating. **Recommended** — ensures complete `compile_commands.json`. Set `false` or use `--no-clean` for incremental builds. |
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
| `db_dir` | `"~/.fw-context/index"` | global | Directory for SQLite index databases. One subdirectory per project. |
| `compile_commands` | `"compile_commands.json"` | project | Path to compilation database. Relative paths resolved from project root. |
| `source_roots` | `[]` *(auto-detect)* | project | Directories to scan for symbols. **Empty = auto-detect** — scans `src`, `lib`, `app`, `include`, `modules`, `drivers` + framework dirs (`zephyr/`, `mbed-os/`) + top-level dirs from `compile_commands.json`. Set explicitly to narrow or extend: `["src", "lib", "/path/to/framework"]`. |
| `exclude_paths` | `["build", "BUILD"]` | project | Directories to skip. Useful for generated code, test fixtures, vendored code. |
| `index_refs` | `true` | project | Build cross-reference / call graph. On by default — enables `find_callers`, `find_call_path`, `find_dead_code`, etc. Set `false` or pass `--no-refs` for faster indexing on very large projects. |
| `index_embeddings` | `true` | project | Generate vector embeddings during indexing. Requires Ollama. Adds ~1–2 minutes. Disable with `false` or `--no-embeddings`. Embeddings power semantic search and hybrid FTS5+vector re-ranking. |

### `[llm]` — Ollama

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `enabled` | `true` | both | Enable Ollama. When `false`, `smart_search` falls back to word-split FTS5, `explain_symbol` returns source + prompt for the AI assistant. |
| `ollama_url` | `"http://localhost:11434"` | global | Ollama API base URL. Change for remote GPU servers. |
| `model` | `"qwen2.5-coder:14b"` | both | LLM model tag. Override per-project for different codebases. |
| `embed_model` | `"mxbai-embed-large:latest"` | both | Embedding model for vector search. Auto-pulled on first use. |
| `num_ctx` | `8192` | global | Context window in tokens. Keep ≥8192 — factory default (2048) is too small for source code. |
| `debug_log` | *(none)* | both | Path to JSONL debug log for Ollama prompts + responses. Example: `"~/.fw-context/llm-debug.jsonl"`. |

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
num_ctx = 8192
# debug_log = "~/.fw-context/llm-debug.jsonl"
```

### Zephyr

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

[llm]
# enabled = false                        # set false if you don't use Ollama
```

### PlatformIO / Arduino

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

### Mbed OS

```toml
[project]
name = "my-mbed-app"

[build]
# system = "mbed-os"                     # auto-detected from .mbed
# target = "P_ECB_BOARD"                # override auto-detected target
# profile = "develop"                    # best for indexing
# defines = ["VERSION_FW_MAJOR=4", "DEV"]  # extra -D macros

[index]
compile_commands = "compile_commands.json"
source_roots = []
exclude_paths = ["build", "BUILD"]
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

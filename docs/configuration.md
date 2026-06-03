# Configuration

Complete reference for fw-context configuration files — global defaults,
per-project overrides, and all available settings.

## Configuration

Configuration is loaded from two TOML files, merged with **project overrides
global** — values set in the project config take precedence over the global one.
Both files are auto-created with sensible defaults on first use.

```
~/.fw-context/config.toml          global defaults (apply to all projects)
        │
        ├── merged with ──→  <project>/.fw-context/config.toml   project overrides
        │
        ▼
    final Config used by fw-context
```

### Configuration reference

#### `[index]` — Indexer settings

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `db_dir` | `"~/.fw-context/index"` | global | Directory where SQLite index databases are stored. One subdirectory per project (keyed by project ID). |
| `compile_commands` | `"compile_commands.json"` | project | Path to the compilation database. Relative paths are resolved from the project root. Use `bear --output compile_commands.json -- ...` to generate it. |
| `source_roots` | `[]` *(auto-detect)* | project | Directories to scan for symbols. **Empty list = auto-detect** (scans `src`, `lib`, `app`, `include`, `modules` + framework dirs `zephyr`/`mbed-os` + top-level dirs from `compile_commands.json`). Set explicitly to narrow indexing: `["src", "lib"]`. Directories that don't exist are silently skipped. |
| `exclude_paths` | `["build", "BUILD"]` | project | Directories to skip during indexing. Useful for generated code, test fixtures, or third-party vendored code. Paths are relative to project root. |
| `index_refs` | `false` | project | Build the cross-reference / call graph (`find_callers`, `find_references`). Off by default — reference extraction adds indexing time and DB size. Set `true` (or pass `--refs`) and re-index to enable. |

#### `[llm]` — LLM / Ollama settings

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `enabled` | `true` | both | Enable or disable Ollama integration. When `false`, `explain_symbol` returns the source code + prompt for the AI assistant to answer itself, and `smart_search` falls back to direct keyword search. No Ollama connection needed. |
| `ollama_url` | `"http://localhost:11434"` | global | Ollama API base URL. Change if Ollama runs on a different machine (e.g. `"http://192.168.1.50:11434"`). |
| `model` | `"qwen2.5-coder:14b"` | both | Ollama model tag. Override per-project to use a different model for different codebases. See [Choosing an Ollama model](installation.md#choosing-an-ollama-model) for recommendations. |
| `num_ctx` | `8192` | global | Context window size in tokens passed to Ollama. The factory default (2048) is too small for source code — keep at least 8192. |
| `debug_log` | *(none)* | both | Path to a JSONL debug log file. When set, all Ollama prompts and responses are logged for debugging. Example: `"~/.fw-context/llm-debug.jsonl"`. |

#### `[project]` — Project metadata

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `name` | *(directory name)* | project | Human-readable project name used in `fw-context list` and status output. Defaults to the directory name if not set. |

### Example: Global config (`~/.fw-context/config.toml`)

```toml
[index]
db_dir = "~/.fw-context/index"

[llm]
# enabled = true   # set to false to disable Ollama — tools return raw prompts
ollama_url = "http://localhost:11434"
model = "qwen2.5-coder:14b"
num_ctx = 8192
# debug_log = "~/.fw-context/llm-debug.jsonl"   # uncomment to log all LLM calls
```

### Example: Per-project config (`<project>/.fw-context/config.toml`)

```toml
[project]
name = "my-firmware"

[index]
compile_commands = "compile_commands.json"
source_roots = []                # auto-detect (default)
exclude_paths = ["build", "BUILD", "generated"]

[llm]
enabled = false                  # no Ollama — AI assistant handles results
# model = "qwen2.5-coder:14b"   # use a different model just for this project
```

### How `source_roots` auto-detection works

When `source_roots` is empty (default), the indexer scans your project for:

1. **Common source dirs:** `src`, `lib`, `app`, `include`, `modules`
2. **Framework dirs:** `zephyr`, `mbed-os` (if they exist at project root)
3. **Top-level dirs from `compile_commands.json`** — any directory that
   contains at least one translation unit from the compilation database

The `compile_commands.json` determines *which* translation units are parsed;
the `#include` chain determines *which* OS headers are traversed — providing
natural, build-accurate filtering.

Set `source_roots` explicitly only when you need to narrow indexing to a
subset of directories or include directories outside the project root
(e.g. PlatformIO frameworks in `~/.platformio/packages/`).


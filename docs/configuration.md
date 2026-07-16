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
enabled. Keep it out of git.

Run ``fw-context init`` to auto-create the config files and add the
necessary entries to ``.gitignore``. All files are created with commented-out
defaults on first use.

## Settings reference

### `[build]` — Build system

Controls how `fw-context index` generates `compile_commands.json`.
Used only when running with ``--build`` or when ``compile_commands.json``
doesn't exist yet.

> **Incremental is the default.**  ``fw-context index`` reuses an existing
> ``compile_commands.json`` whenever possible.  Use ``fw-context index --build``
> to force a clean build and full re-index when needed.

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
| `vendor_paths` | `[]` | project | Additional vendor/SDK directory patterns (additive to auto-detection). Paths matching these get `is_project=0`. E.g. `["third_party", "generated"]`. |
| `project_paths` | `[]` | project | Manual project directory patterns — overrides auto-detection. Paths matching these get `is_project=1`. Useful for vendored code your team maintains (e.g. `["src/old_hal"]`). For paths outside the project root, use absolute paths (e.g. `["/home/user/esp/components/muj_fork"]`). |
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
| `embed_model` | `"mxbai-embed-large:latest"` | global, local | Embedding model for vector search. Auto-pulled on first use. ``mxbai-embed-large`` runs on CPU; for GPU, use ``qwen3-embedding:8b`` (~4.7 GB VRAM, 4096-dim vectors). |
| `embed_query_prompt` | *(auto-detect)* | global, local | Instruction prepended to query text before embedding. Auto-detected from model prefix: ``mxbai-*`` → ``"Represent this sentence for searching relevant passages: "``, ``qwen3-embedding*`` → code retrieval instruction. Set explicitly to override, or ``""`` to disable. |
| `embed_doc_prompt` | *(empty)* | global, local | Instruction prepended to symbol descriptions during indexing. Most models work best with an empty prompt — only set when the model's training expects a per-document instruction. |
| `num_ctx` | `16384` | global, local | Context window in tokens. Accommodates full function bodies during analysis generation. |
| `keep_alive` | `"10m"` | global, local | How long to keep the model loaded in VRAM after a request (minutes, seconds, or ``-1`` for indefinite). During indexing, prevents per-request model loading (~2–5 s each). |
| `timeout` | `600.0` | global, local | HTTP request timeout in seconds for Ollama API calls. Embed requests use ``timeout × 2``. |
| `analyze_symbols` | `true` | global, local | Generate per-symbol LLM analysis (summary, inputs, outputs) during indexing. Stored in `llm_analysis` table — symbols become searchable by purpose. |
| `debug_log` | *(none)* | global, local | Path to JSONL debug log for Ollama prompts + responses. Example: `"~/.fw-context/llm-debug.jsonl"`. |

### `[project]` — Metadata

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `name` | *(directory name)* | project | Human-readable project name shown in `fw-context list` and status output. |

### `[cache_server]` — Shared LLM Analysis Cache

Configure a remote cache server for sharing `llm_analysis` across developers.
**Optional** — without this section, analysis stays local.

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `url` | *(none)* | global, local | Cache server URL. Example: `"https://fw-cache.example.com"`. |
| `token` | *(none)* | global, local | Bearer token with `can_read` + `can_write` permissions. Created via `fw-cache-admin token create`. |
| `batch_size` | `100` | global, local | Max hashes/entries per HTTP request. |
| `force` | `false` | global, local | When `true`, sends `X-Cache-Overwrite` header — overwrites existing entries. Requires `can_overwrite` token. |

```toml
[cache_server]
url = "https://fw-cache.example.com"
token = "<your-token>"
# batch_size = 100
# force = false
```

See **[Cache Server →](cache-server.md)** for setup, deployment, and management.

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
# vendor_paths = ["third_party"]          # additional vendor dirs (additive to auto-detection)
# project_paths = ["src/old_hal"]        # manual project dirs (overrides auto-detection)
```

#### PlatformIO / Arduino

```toml
[project]
name = "my-pio-project"

[build]
# system = "platformio"                  # auto-detected from platformio.ini

[index]
compile_commands = "compile_commands.json"
# PlatformIO framework packages are auto-detected as vendor (is_project=0).
# For vendored code your team maintains, use project_paths to mark it as project:
# project_paths = ["src/my_customized_framework"]
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
# vendor_paths = ["third_party"]          # additional vendor dirs (additive to auto-detection)
# project_paths = ["src/old_hal"]        # manual project dirs (overrides auto-detection)
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
#
# ── Embedding model ──
# Uncomment for better search quality with a GPU:
# embed_model = "qwen3-embedding:8b"
# embed_query_prompt = "Retrieve C/C++ functions, types, symbols, and implementation code relevant to the query."

[index]
# db_dir = "~/.fw-context/index"        # override if you store indexes elsewhere
```

## Project vs vendor code detection

Every indexed file gets an ``is_project`` flag during indexing, computed from
path patterns in this priority order (first match wins):

1. **``project_paths`` config** → ``is_project=1`` (user says "this is project code")
2. **Outside project root** → ``is_project=0`` (external SDK, toolchain, system headers)
3. **``vendor_paths`` config + auto-detection** → ``is_project=0`` (SDK/vendor code)
4. **Everything else** → ``is_project=1`` (project code)

Query-time ``project_only`` filtering uses this column directly
(``WHERE is_project = 1``), so it always respects your config.

See ``vendor_paths`` and ``project_paths`` in the ``[index]`` section above.

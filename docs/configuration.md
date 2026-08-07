# Configuration

This document is the complete reference for `.fw-context/config.toml` and `.fw-context/local.toml`. This document lists the global defaults, the shared project settings, the local developer overrides, and every available setting.

## How config works

fw-context merges three levels of TOML files, in this order. A later file overrides an earlier file.

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

**Why two project files?** `config.toml` holds settings that are the same for every developer on the project. Examples are build parameters, source roots, and excludes. Commit `config.toml` to git.

`local.toml` holds settings that are specific to each developer. Examples are which Ollama model you have installed, where your index database is, and whether you want LLM analysis enabled. Keep `local.toml` out of git.

Run `fw-context init` to create the config files automatically. This command also adds the necessary entries to `.gitignore`. On first use, fw-context creates all files with commented-out default values.

## Settings reference

### `[build]` — Build system

This section controls how `fw-context index` generates `compile_commands.json`. fw-context uses this section only when you run `fw-context index --build`, or when `compile_commands.json` does not yet exist.

> **Incremental is the default.** `fw-context index` reuses an existing `compile_commands.json` file whenever possible. Use `fw-context index --build` to force a clean build. A clean build also forces a full re-index.

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `system` | *(auto-detect)* | project | The build system: `"mbed-os"`, `"zephyr"`, or `"platformio"`. fw-context detects this automatically from project markers, such as `west.yml`, `.mbed`, or `platformio.ini`. |
| `clean` | `true` | project | Always run a clean build before fw-context generates `compile_commands.json`. **Recommended.** A clean build ensures a complete `compile_commands.json` file. Set this key to `false`, or use `--no-clean`, for incremental builds. Note: a clean build always forces a full re-index. |
| `command` | *(none)* | project | A full override for the build command. This key bypasses all automatic detection. Example: `"bear -- make -j4"`. |
| `python` | *(auto-detect)* | local | The Python interpreter for pip-based build tools, such as `mbed-cli`, `platformio`, `keil2clangd`, or `compiledb`. `fw-context init` detects this automatically. Set this key manually when automatic detection fails. |
| `activate` | *(auto-detect)* | local | A shell script that fw-context sources before the build, for example the Zephyr/NCS toolchain script or the ESP-IDF `export.sh` script. `fw-context init` detects this automatically. Set this key manually when automatic detection fails. |
| `target` | *(auto-detect)* | project | The Mbed OS target board. fw-context detects this automatically from `.mbed` or `custom_targets.json`. |
| `toolchain` | *(auto-detect)* | project | The Mbed OS toolchain. fw-context detects this automatically from `.mbed`. |
| `profile` | `"develop"` | project | The Mbed OS build profile. The `develop` profile works best for indexing, because it includes `-g` debug symbols. |
| `app_config` | `"mbed_app.json"` | project | The Mbed OS application configuration file. |
| `extra_profiles` | `["lto.json"]` | project | Additional Mbed OS profiles. fw-context resolves these paths relative to `mbed-os/tools/profiles/extensions/`. |
| `defines` | `[]` | project | Extra preprocessor macros that fw-context passes to the compiler as `-D` flags. Example: `["VERSION_FW_MAJOR=4", "DEV"]`. This key is useful for conditional code paths, because it makes fw-context index `#ifdef DEV` branches too. |
| `board` | *(required)* | project | The Zephyr board name. You must set this key explicitly for Zephyr projects. |

### `[index]` — Indexer

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `db_dir` | `"~/.fw-context/index"` | global, local | The directory for SQLite index databases. fw-context creates one subdirectory for each project. |
| `compile_commands` | `"compile_commands.json"` | project | The path to the compilation database. fw-context resolves relative paths from the project root. |
| `vendor_paths` | `[]` | project | Additional vendor or SDK directory patterns. fw-context adds these patterns to its automatic detection. A path that matches one of these patterns gets `is_project=0`. Example: `["third_party", "generated"]`. |
| `project_paths` | `[]` | project | Manual project directory patterns. These patterns override automatic detection. A path that matches one of these patterns gets `is_project=1`. Use this key for vendored code that your team maintains, for example `["src/old_hal"]`. For a path outside the project root, use an absolute path, for example `["/home/user/esp/components/muj_fork"]`. |
| `index_refs` | `true` | project | Build the cross-reference and call graph data. This key is on by default, and it enables tools such as `find_callers`, `find_call_path`, and `find_dead_code`. Set this key to `false`, or pass `--no-refs`, for faster indexing on very large projects. |
| `index_embeddings` | `true` | project | Generate vector embeddings during indexing. This key requires Ollama. The embeddings power semantic search and hybrid FTS5+vector re-ranking. Disable this key with `false`, or with `--no-embeddings`. |

### `[llm]` — Ollama

Put these settings in `~/.fw-context/config.toml` for global defaults, or in `<project>/.fw-context/local.toml` for per-project overrides. Do not put these settings in the shared `config.toml` file. LLM configuration is specific to each developer.

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `enabled` | `true` | global, local | Enable Ollama. When this key is `false`, `smart_search` falls back to word-split FTS5. Also, `explain_symbol` returns the source code and a prompt for the AI assistant. |
| `ollama_url` | `"http://localhost:11434"` | global, local | The base URL for the Ollama API. Change this key for a remote GPU server. |
| `model` | `"qwen2.5-coder:14b"` | global, local | The LLM model tag. Override this key for each project, to use a different model for different codebases. |
| `embed_model` | `"mxbai-embed-large:latest"` | global, local | The embedding model for vector search. fw-context pulls this model automatically on first use. `mxbai-embed-large` runs on a CPU. For a GPU, use `qwen3-embedding:8b`, which needs about 4.7 GB of VRAM and creates 4096-dimension vectors. |
| `embed_query_prompt` | *(auto-detect)* | global, local | An instruction that fw-context adds before the query text, before it creates the embedding. fw-context detects this instruction automatically from the model name prefix: for `mxbai-*`, fw-context uses `"Represent this sentence for searching relevant passages: "`; for `qwen3-embedding*`, fw-context uses a code-retrieval instruction. Set this key explicitly to override the default, or set it to `""` to disable it. |
| `embed_doc_prompt` | *(empty)* | global, local | An instruction that fw-context adds before symbol descriptions during indexing. Most models work best with an empty prompt. Set this key only when the model's training expects a per-document instruction. |
| `auto_pull` | `false` | global, local | When `true`, fw-context pulls a model automatically from the Ollama registry when it is not installed. When `false` (default), you must pull each model explicitly. Set this key to `false` for offline or intranet environments. |
| `num_ctx` | `16384` | global, local | The context window, in tokens. This size allows full function bodies during analysis generation. |
| `keep_alive` | `"10m"` | global, local | How long fw-context keeps the model loaded in VRAM after a request. Use minutes, seconds, or `-1` for an indefinite time. During indexing, this setting prevents model loading before each request, which takes about 2 to 5 seconds each time. |
| `timeout` | `600.0` | global, local | The HTTP request timeout, in seconds, for Ollama API calls. Embed requests use `timeout × 2`. |
| `reranker_model` | *(none)* | global, local | A cross-encoder model for search result reranking. Example: `"cross-encoder/ms-marco-MiniLM-L6-v2"`. When you set this key, fw-context rescors each result with the cross-encoder for higher precision. This key requires `sentence-transformers`. Default `None` — no reranking. |
| `analyze_symbols` | `true` | global, local | Generate an LLM analysis for each symbol during indexing. This analysis includes a summary, the inputs, and the outputs. fw-context stores this analysis in the `llm_analysis` table, so you can search for symbols by purpose. |
| `ollama_max_concurrent` | `1` | global, local | The maximum number of parallel Ollama HTTP calls per process. Increase this key to 2–4 for multi-client MCP transports, such as SSE or streamable, where embedding requests (~100 ms) can overlap. Keep this key low for chat requests, which use the GPU more. |
| `chat_api_base` | *(none)* | global, local | A chat API URL for an OpenAI-compatible cloud or proxy endpoint. Examples: DeepSeek, LiteLLM, vLLM, llama.cpp. When you set this key, fw-context sends chat requests to this URL instead of the local Ollama instance. Default `None` — fw-context uses the local Ollama server. |
| `chat_api_key` | *(none)* | global, local | A bearer token for the cloud or proxy chat API. Leave this key empty for endpoints that need no authentication. |
| `chat_api_format` | `"auto"` | global, local | The request format for the chat API. `"auto"` (default) detects the format from the URL. `"ollama"` uses the Ollama-native `/api/chat` format. `"openai"` uses the `/v1/chat/completions` format. |
| `stream` | `false` | global, local | When `true`, fw-context sends `stream: true` and reads SSE chunks for chat requests. This setting keeps the HTTP connection open with a continuous data flow, and prevents reverse-proxy idle timeouts (nginx default: 60 s, Cloudflare: 100 s). When `false`, fw-context uses a non-streaming path. |
| `debug_log` | *(none)* | global, local | The path to a JSONL debug log for Ollama prompts and responses. Example: `"~/.fw-context/llm-debug.jsonl"`. |

#### External chat API examples

When you set `chat_api_base`, fw-context sends chat requests (analysis
generation, `smart_search`, `explain_symbol`) to that URL, with
OpenAI-compatible JSON. The embedding model still uses Ollama.

**OpenAI:**

```toml
[llm]
chat_api_base = "https://api.openai.com/v1"
chat_api_key = "sk-..."
chat_api_format = "openai"
model = "gpt-4.1"
```

**Anthropic (through a proxy):**

Anthropic has no OpenAI-compatible endpoint. Use LiteLLM as a proxy —
it translates OpenAI-format requests to Anthropic-format requests.

```toml
[llm]
# Point at your LiteLLM proxy. Set ANTHROPIC_API_KEY in the proxy env.
chat_api_base = "http://localhost:4000/v1"
chat_api_key = "sk-lite"
chat_api_format = "openai"
model = "claude-sonnet-4-20250514"
```

**DeepSeek:**

```toml
[llm]
chat_api_base = "https://api.deepseek.com/v1"
chat_api_key = "sk-..."
chat_api_format = "openai"
model = "deepseek-v4-flash"
```

**LiteLLM proxy (local):**

LiteLLM translates between provider-specific formats. You run it locally,
and fw-context talks to it as if it were OpenAI.

```toml
[llm]
chat_api_base = "http://localhost:4000/v1"
chat_api_key = "sk-lite"
chat_api_format = "openai"
model = "openai/gpt-4o"       # LiteLLM model routing syntax
# or: model = "deepseek/deepseek-v4-flash"
# or: model = "anthropic/claude-3-5-sonnet-20241022"
```

**llama.cpp server:**

```toml
[llm]
chat_api_base = "http://localhost:8080/v1"
chat_api_key = "not-needed"
chat_api_format = "openai"
model = "qwen2.5-coder:14b"
```

For all external endpoints, set `stream = true` when you use a reverse
proxy, to prevent idle timeouts.

### `[project]` — Metadata

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `name` | *(directory name)* | project | A readable project name. fw-context shows this name in `fw-context list` and in status output. |

### `[cache_server]` — Shared LLM Analysis Cache

Configure a remote cache server, to share `llm_analysis` data across developers. **Optional.** Without this section, the analysis stays local.

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `url` | *(none)* | global, local | The cache server URL. Example: `"https://fw-cache.example.com"`. |
| `token` | *(none)* | global, local | A bearer token with `can_read` and `can_write` permissions. Create this token with the `fw-cache-admin token create` command. |
| `batch_size` | `100` | global, local | The maximum number of hashes or entries in each HTTP request. |
| `force` | `false` | global, local | When this key is `true`, fw-context sends the `X-Cache-Overwrite` header, and overwrites existing entries. This key requires a `can_overwrite` token. |

```toml
[cache_server]
url = "https://fw-cache.example.com"
token = "<your-token>"
# batch_size = 100
# force = false
```

For setup, deployment, and management instructions, see **[Cache Server →](cache-server.md)**.

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

Commit this file to git. This file contains settings that are the same for every developer.

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

Keep this file out of git. Add it to `.gitignore`. This file overrides settings from `config.toml` and from the global config. Use this file for preferences that are specific to each developer.

```toml
# ── Build environment (auto-detected by fw-context init) ──
# Set manually only if auto-detection fails.
[build]
# python = "/home/user/.pyenv/versions/3.11.8/bin/python"  # for mbed-cli, platformio, keil2clangd
# activate = "/home/user/ncs_tools/nordic_minimal_setup.sh"  # for Zephyr/NCS, ESP-IDF

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

During indexing, fw-context gives every indexed file an `is_project` flag. fw-context computes this flag from path patterns, in this priority order. The first match wins:

1. **`project_paths` config** → `is_project=1` (user says "this is project code")
2. **Outside project root** → `is_project=0` (external SDK, toolchain, system headers)
3. **`vendor_paths` config + auto-detection** → `is_project=0` (SDK/vendor code)
4. **Everything else** → `is_project=1` (project code)

At query time, `project_only` filtering uses this column directly, with `WHERE is_project = 1`. This filtering always follows your configuration.

For more information, see `vendor_paths` and `project_paths` in the `[index]` section above.

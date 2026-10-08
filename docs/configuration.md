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

The environment variable `FW_CONTEXT_HOME` moves the global `config.toml`, the local LLM cache `llm_cache.db`, the state of the release check `update_check.json` (see [`[updates]`](#updates--release-check)), and the global files that the cleanup removes (see [Obsolete keys and files](#obsolete-keys-and-files)). Without it, they are in `~/.fw-context`. It does not move the index, the project registry or the clang headers: these have their own variables (`FW_CONTEXT_INDEX_DIR`, `FW_CONTEXT_PROJECTS_DB`, `FW_CONTEXT_CLANG_RESOURCE_DIR`). The test suite of fw-context sets all four, thus a test cannot change the files of the operator.

Run `fw-context init` to create the project config files with commented-out default values. This command also adds the necessary entries to `.gitignore`. On first use, fw-context creates only the global `~/.fw-context/config.toml`. The MCP tools and `fw-context index` create no config file in a project that `init` did not set up, and read a missing project file as empty.

## Settings reference

### `[build]` — Build system

This section controls how `fw-context index` generates `compile_commands.json`. fw-context uses this section only when you run `fw-context index --build`, or when `compile_commands.json` does not yet exist.

> **Incremental is the default.** `fw-context index` reuses an existing `compile_commands.json` file whenever possible. Use `fw-context index --build` to force a clean build. A clean build also forces a full re-index.

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `system` | *(auto-detect)* | project | The build system: `"mbed-os"`, `"zephyr"`, or `"platformio"`. fw-context detects this automatically from project markers, such as `west.yml`, `.mbed`, or `platformio.ini`. |
| `clean` | `true` | project | Always run a clean build before fw-context generates `compile_commands.json`. **Recommended.** A clean build ensures a complete `compile_commands.json` file. Set this key to `false`, or use `--no-clean`, for incremental builds. Note: a clean build always forces a full re-index. |
| `command` | *(none)* | project | A full override for the build command. This key bypasses all automatic detection. Example: `"bear -- make -j4"`. |
| `timeout` | `7200` | project | The build timeout, in seconds. Long first builds (Zephyr, ESP-IDF) must not fail at the previous 600 s limit. |
| `python` | *(auto-detect)* | local | The Python interpreter for pip-based build tools, such as `mbed-cli`, `platformio`, `keil2clangd`, or `compiledb`. `fw-context init` detects this automatically. Set this key manually when automatic detection fails. |
| `activate` | *(auto-detect)* | local | A shell script that fw-context sources before the build, for example the Zephyr/NCS toolchain script or the ESP-IDF `export.sh` script. `fw-context init` detects this automatically. Set this key manually when automatic detection fails. |
| `target` | *(auto-detect)* | project | The Mbed OS target board. fw-context detects this automatically from `.mbed` or `custom_targets.json`. |
| `toolchain` | *(auto-detect)* | project | The Mbed OS toolchain. fw-context detects this automatically from `.mbed`. |
| `profile` | `"develop"` | project | The Mbed OS build profile. The `develop` profile works best for indexing, because it includes `-g` debug symbols. |
| `app_config` | `"mbed_app.json"` | project | The Mbed OS application configuration file. |
| `extra_profiles` | `["lto.json"]` | project | Additional Mbed OS profiles. fw-context resolves these paths relative to `mbed-os/tools/profiles/extensions/`. |
| `defines` | `[]` | project | Extra preprocessor macros that fw-context passes to the compiler as `-D` flags. Example: `["VERSION_FW_MAJOR=4", "DEV"]`. This key is useful for conditional code paths, because it makes fw-context index `#ifdef DEV` branches too. |
| `board` | *(required)* | project | The Zephyr board name. You must set this key explicitly for Zephyr projects. |
| `fqbn` | *(none)* | project | The Arduino fully qualified board name, for example `"arduino:avr:uno"`. `fw-context init` asks for this value when you select the Arduino build system interactively. |
| `keil_project` | *(none)* | project | The path to the Keil MDK `.uvprojx` project file. `keil2clangd` converts this file to `compile_commands.json`. |
| `iar_project` | *(none)* | project | The path to the IAR EWARM `.ewp` project file. `keil2clangd` converts this file to `compile_commands.json`. |
| `source_dirs` | *(none)* | project | The source directories for the manual (bare) mode. `fw-context init` asks for this value when you select the bare mode interactively. |
| `include_dirs` | *(none)* | project | The include directories for the manual (bare) mode. |

#### `[[build.variants]]` — Multi-project and multi-image builds

One workspace can build several boards or images. You declare each build
as a **variant**, with an array-of-tables entry. Each `(variant, image)`
pair becomes one indexed build, with its own `config_hash`. This feature
works for every build system, not only Zephyr.

Shared `[build]` keys for multi-variant projects. `default_image` also
applies to a project without variants whose build makes several images
(ESP-IDF):

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `default_variant` | *(none)* | project | The variant that a query uses when it omits `variant`. Without this key, a query without `variant` fails closed. |
| `default_image` | *(none)* | project | The image that a query gets when it does not name `image` and the build holds more than one image. The key applies to each variant that holds an image of this name. Without the key, the query gets the application that the build system names: an ESP-IDF build names its application, and a Zephyr sysbuild names its default image in `domains.yaml`. A build with one image gives that image. In all other cases, the query must name `image`. `get_active_build` reports the result as `active_image`. |
| `sysbuild` | `false` | project | Use `west build --sysbuild` (Zephyr). |
| `source_dir` | *(none)* | project | The sysbuild input application directory (Zephyr). |
| `environment` | *(none)* | project | The `[env:<name>]` of `platformio.ini` that the build builds (PlatformIO). A variant can override it; its default is the variant name. In a project without `[[build.variants]]`, each of two or more environments becomes a variant, see [Build Configuration](build.md#platformio). |
| `env` | *(none)* | project | Build environment variables, shared by every variant. |

Each `[[build.variants]]` table:

| Key | Default | Description |
|-----|---------|-------------|
| `name` | *(required)* | A unique key. The query tools and the CLI reference this key. |
| `board` | *(none)* | The board, target, or chip label for this variant. This value overrides `[build] board`. |
| `description` | *(none)* | A human-readable description. |
| `build_dir` | — | Retired, here and in `[build]`. Each variant builds into `.fw-context/build/<name>/out`. `fw-context index` removes the key from the config at its start (see [Obsolete keys and files](configuration.md#obsolete-keys-and-files)). When it cannot write the config, the run stops with an error. |
| `env` | *(none)* | Build environment variables. fw-context folds these variables into the `config_hash`. |
| `images` | *(none)* | The sysbuild images (Zephyr only). Each image has `name`, `dir`, `type` (`"project"` or `"sdk"`), and an optional `board`. |
| *(any other `[build]` key)* | — | Overrides the shared `[build]` value for this variant only. |

Variant overrides merge with the shared `[build]` section by type. A
scalar value (such as `board`) replaces the shared value. A list value
(such as `defines`) replaces the shared list. A dict value (such as
`env`) merges into the shared dict.

```toml
[build]
system = "bare"
compiler = "arm-none-eabi-gcc"
source_dirs = ["src", "lib"]
include_dirs = ["include"]
default_variant = "board-a"

[[build.variants]]
name = "board-a"
board = "STM32F407xx"
defines = ["USE_HAL_DRIVER", "STM32F407xx"]

[[build.variants]]
name = "board-b"
board = "STM32F103xx"
defines = ["USE_HAL_DRIVER", "STM32F103xx"]
```

For a Zephyr sysbuild example, see [Build Configuration → Multi-project
and multi-image builds](build.md).

### `[index]` — Indexer

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `db_dir` | `"~/.fw-context/index"` | global, local | The directory for SQLite index databases. fw-context creates one subdirectory for each project. |
| `compile_commands` | `".fw-context/build/compile_commands.json"` | project | The path to the compilation database. fw-context resolves relative paths from the project root. For a build that fw-context runs, the default (and the older value `"compile_commands.json"`) means the database of that build, in `.fw-context/build/<variant>/out`. Set another path for a build that fw-context cannot run (for example an STM32CubeIDE project), or for a database of your own. fw-context then runs no build, makes no variants from the environments of platformio.ini, and the linker scripts and the vector table of the build stay unknown. A missing file is an error, `--build` with such a file is an error, and so is `[[build.variants]]` beside it. |
| `vendor_paths` | `[]` | project | Additional vendor or SDK directory patterns. fw-context adds these patterns to its automatic detection. A path that matches one of these patterns gets `is_project=0`. Example: `["third_party", "generated"]`. |
| `project_paths` | `[]` | project | Manual project directory patterns. These patterns override automatic detection. A path that matches one of these patterns gets `is_project=1`. Use this key for vendored code that your team maintains, for example `["src/old_hal"]`. For a path outside the project root, use an absolute path, for example `["/home/user/esp/components/muj_fork"]`. |
| `index_refs` | `true` | project | Build the cross-reference and call graph data. This key is on by default, and it enables tools such as `find_callers`, `find_call_path`, and `find_dead_code`. Set this key to `false`, or pass `--no-refs`, for faster indexing on very large projects. |
| `index_embeddings` | `true` | project | Generate vector embeddings during indexing. An Ollama `embed_model` requires Ollama; a sentence-transformers or `ft://` model runs locally. The embeddings power semantic search and hybrid FTS5+vector re-ranking. Disable this key with `false`, or with `--no-embeddings`. |
| `max_symbol_body_lines` | `1000` | project | The maximum number of lines of one symbol body. The index stores at most this number of lines, and `get_source` and `get_symbol_context` return at most this number. A body that a cap cut carries `_source_truncated`. |
| `transient_defines` | `["MBED_BUILD_TIMESTAMP", "BUILD_TIMESTAMP", "BUILD_TIME", "BUILD_DATE", "BUILD_ID", "BUILD_NUMBER"]` | global, project, local | The names of the `-D` macros whose value makes each build unique, for example a build time or a build counter. When fw-context compares two builds, it ignores these macros: in the identity of the build (`config_hash`) and in the flags of each translation unit. Thus a new build that changes only such a value parses no file again. Mbed OS writes the time of the build into `MBED_BUILD_TIMESTAMP` on each unit. Write names only (`NAME`, not `-DNAME` or `NAME=1`). A project list replaces the global list, and `[]` makes every macro count. Remove a name that your code reads in an `#if`, because then a new value must change the index. A change of the list can change `config_hash` and start a full reindex. An entry that is not a macro name, or the key in a `[[build.variants]]` table, stops `fw-context index` with an error. |

fw-context asks each GCC compiler that `compile_commands.json` names for its system include directories and its predefined macros, as clangd `--query-driver` does. It runs the compiler with `-E -v` and `-dM -E` on an empty input. This is the compiler that the build of the project runs too, thus no configuration selects it. When a compiler does not answer, its units get the flags of `compile_commands.json`, a target from the compiler name, and toolchain directories that fw-context guesses from the layout of the toolchain.

Earlier releases read the keys `query_driver`, `query_driver_extra` and `query_driver_auto`, and wrote `.fw-context/toolchains.toml`. fw-context ignores these keys and this file now. The next `fw-context index` removes them, see [Obsolete keys and files](#obsolete-keys-and-files). Until then, a warning names the file that still has them.

The parse also needs the clang compiler headers (`stddef.h`, `arm_acle.h`) of the same major version as libclang, because the libclang wheel does not include them. fw-context ships these headers for the libclang version that it pins, and unpacks them to `~/.fw-context/clang-resource/<major>-<hash>/` at the first parse (one directory for each archive, thus two installations of fw-context do not share one copy), or when `fw-context doctor --fix`, `fw-context init` or `make install` runs. This needs no network and no administrator rights, and works on Linux, macOS and Windows. `fw-context doctor` reports the state as the `clang-resource` check.

WHY the query: a GCC cross compiler adds its system headers and its target macros on its own, and `compile_commands.json` does not hold them. Without the query, a target that libclang has no backend for (xtensa, ESP32) was parsed with the headers of the host, and `#if __XTENSA__` read as false.

### `[llm]` — Ollama

Put these settings in `~/.fw-context/config.toml` for global defaults, or in `<project>/.fw-context/local.toml` for per-project overrides. Do not put these settings in the shared `config.toml` file. LLM configuration is specific to each developer.

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `enabled` | `true` | global, local | Enable Ollama. When this key is `false`, `smart_search` falls back to word-split FTS5. Also, `explain_symbol` returns the source code and a prompt for the AI assistant. |
| `ollama_url` | `"http://localhost:11434"` | global, local | The base URL for the Ollama API. Change this key for a remote GPU server. |
| `model` | `"qwen2.5-coder:14b"` | global, local | The LLM model tag. Override this key for each project, to use a different model for different codebases. |
| `embed_model` | `""` *(auto-detect)* | global, local | The embedding model for vector search. Empty: fw-context selects `qwen3-embedding:8b` on a machine with a GPU (about 4.7 GB of VRAM, 4096-dimension vectors), and `qwen3-embedding:0.6b` without one. A name with the prefix `BAAI/`, `ibm-granite/`, `lightonai/`, `cross-encoder/` or `sentence-transformers/` uses sentence-transformers (requires the `st` extra); `ft://` selects a fine-tuned local model; any other name is an Ollama model. When `auto_pull` is `true`, fw-context pulls an Ollama model automatically on first use; otherwise pull it manually. A change of model invalidates the stored embeddings. |
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
| `force` | `false` | global, local | When this key is `true`, fw-context sends the `X-Cache-Overwrite` header, and overwrites existing entries. This key applies to the writes during `fw-context index` and `fw-context analyze`. `fw-context index --force` also turns it on. `fw-context cache push` does not read this key: use `fw-context cache push --overwrite`. This key requires a `can_overwrite` token. |

```toml
[cache_server]
url = "https://fw-cache.example.com"
token = "<your-token>"
# batch_size = 100
# force = false
```

For setup, deployment, and management instructions, see **[Cache Server →](cache-server.md)**.

### `[updates]` — Release check

At start, the MCP server asks PyPI for the newest release of `fw-context-mcp`. The request runs in a background thread, and no tool waits for it. After a request that succeeded, the server asks again only after 24 hours. The server stores the answer in `update_check.json` in the global directory (see `FW_CONTEXT_HOME` above). When the stored release is newer than the installed one, `get_active_build` gives an `update_notice`, and `fw-context version` prints a second line. A failed request writes nothing, and the next server start tries again. An editable install (`pip install -e`, `make install`) sends no request, because its package metadata does not follow the source tree.

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `check` | `true` | global | When this key is `false`, the MCP server sends no request to PyPI, and `get_active_build` gives no `update_notice`. Set it to `false` on a machine without network access, or where a request to PyPI is not permitted. fw-context ignores this key in a project config, and logs a warning when a project config sets it. |

The environment variable `FW_CONTEXT_NO_UPDATE_CHECK` also stops the check. Any value other than an empty string and `0` stops it. The test suite of fw-context sets it.

```toml
[updates]
check = false
```

`get_active_build` also reports a different fw-context that the operator installed while the MCP server runs (an upgrade or a downgrade), independently of this key. The server then does not run the installed code, and `client_restart_required` tells the user to restart the LLM client. This check reads only the local package metadata.

## Obsolete keys and files

fw-context removes the config keys and the files that an older release wrote and that no release reads now. The list is in `src/fw_context_mcp/housekeeping.py`, with the commit that retired each item.

| Item | Where |
|------|-------|
| `[index] query_driver`, `query_driver_extra`, `query_driver_auto` | each config file, also in `[[build.variants]]` |
| `[llm] allow_external_llm` | each config file |
| `[build] build_dir` | each config file, also in `[[build.variants]]` |
| `[index] compile_commands` with the value `compile_commands.json` or `.fw-context/build/compile_commands.json` | `config.toml` and `local.toml` of the project, when two conditions are true. fw-context runs the build of the project: for a stub build (for example STM32CubeIDE), the value names the database of the project. And the files below it give such a value too, or none: else the removal brings up the value of a lower file. The global config keeps the key, because it applies to each project, a stub project too. |
| `.fw-context/toolchains.toml` | the project |
| The build output of the older layout (`.fw-context/autobuild/`, the files directly in `.fw-context/build/`), see [build.md](build.md) | the project. A database that a build of the index reads stays. |
| `platformio-shared.ini` | the global directory |

The cleanup changes only the lines of a removed key. A comment on the same line as the key goes with it. The other comments, the order of the keys and the line ends (LF or CRLF) of the file stay. A config file without an obsolete key is not written. The cleanup writes a new file and renames it over the old one, as the other config writers of fw-context do. Thus a stop of the process leaves the old file or the new one, never a cut file. The new file gets the mode of the old one (a config of mode 0600 that holds a token stays 0600). A symbolic link stays a link: the cleanup writes the target. A file whose mode gives the owner no write permission stays as it is, also when root runs fw-context, and the cleanup reports it. `config.toml` is in git, thus a removed key shows as a change there: commit it.

The report gives each removed key as a full line with its value, for example `[index] query_driver = ["/opt/gcc/*"] in .fw-context/local.toml`. Thus you can write a key back. `local.toml` and the global config are not in git: before the cleanup changes one of them, it writes the old text to `local.toml.bak` or `config.toml.bak` beside the file. A later cleanup replaces that copy. `fw-context init` keeps `local.toml.bak` out of git. You can delete the copy.

`.fw-context/build/` belongs to fw-context. For a project whose build fw-context runs, the cleanup removes `.fw-context/build/compile_commands.json` when no build of the index reads it, also when you made the file. Keep a database of your own in another directory, and name it with `[index] compile_commands`.

These commands do the cleanup:

- `fw-context index`, at the start of each run, and again after a run that ends well
- `fw-context cleanup` (`--dry-run` shows the list and changes nothing)
- `fw-context doctor --fix`, and `fw-context init`, which runs the same fixes. `fw-context doctor` shows the list as the check `obsolete-files`.

The MCP server and the watcher daemon remove nothing. The background runs of the daemon are `fw-context index` runs, thus the cleanup runs in them too. When the cleanup cannot read or write a file, it logs a warning and continues: the index run does not stop for it. When the index cannot be read (for example an index of an older schema), the old build output stays until an index run migrates the index.

## Examples

### Global config (`~/.fw-context/config.toml`)

```toml
[index]
db_dir = "~/.fw-context/index"
transient_defines = ["MBED_BUILD_TIMESTAMP", "BUILD_TIMESTAMP", "BUILD_TIME", "BUILD_DATE", "BUILD_ID", "BUILD_NUMBER"]

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
# compile_commands = "compile_commands.json"   # only for a build that fw-context cannot run
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
# compile_commands = "compile_commands.json"   # only for a build that fw-context cannot run
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
# target = "BOARD_V2_BOARD"                # override auto-detected target
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

Keep this file out of git. `fw-context init` already ignores it: its `.gitignore` rules ignore everything under `.fw-context/` except `config.toml`. This file overrides settings from `config.toml` and from the global config. Use this file for preferences that are specific to each developer.

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

# Configuration

This document is the complete reference for `.fw-context/config.toml` and `.fw-context/local.toml`. This document lists the global defaults, the shared project settings, the local developer overrides, and every available setting. The `[build]` keys of each build system are in [Build Configuration](build.md#configuration-reference).

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

`local.toml` holds settings that are specific to each developer. Examples are the LLM profile of the project, where your index database is, and whether you want LLM analysis enabled. Keep `local.toml` out of git.

The LLM profiles are in the global config only. A profile names the LLM backend: the server, the model and the API key variable. A project only selects a profile, with `[llm] profile`. See [`[llm]`](#llm--llm-and-embeddings).

The environment variable `FW_CONTEXT_HOME` moves the global `config.toml`, the local LLM cache `llm_cache.db`, the calibration of the LLM analysis `llm_calibration.json` (see [The context of the LLM analysis](#the-context-of-the-llm-analysis)), the state of the release check `update_check.json` (see [`[updates]`](#updates--release-check)), and the global files that the cleanup removes (see [Obsolete keys and files](#obsolete-keys-and-files)). Without it, they are in `~/.fw-context`. It does not move the index, the project registry or the clang headers: these have their own variables (`FW_CONTEXT_INDEX_DIR`, `FW_CONTEXT_PROJECTS_DB`, `FW_CONTEXT_CLANG_RESOURCE_DIR`). The test suite of fw-context sets all four, thus a test cannot change the files of the operator.

Run `fw-context init` to create the project config files with commented-out default values. This command also adds the necessary entries to `.gitignore`. On first use, fw-context creates only the global `~/.fw-context/config.toml`. The MCP tools and `fw-context index` create no config file in a project that `init` did not set up, and read a missing project file as empty.

The LLM step of `fw-context init` tells which file selects the LLM backend. The backend keys are `enabled`, `profile`, `embed_profile` and `embed_model` of `[llm]`:

- Only the global config sets them: the step shows the global values, and option 0 "Use the global settings" is the default (Enter). It writes nothing.
- `local.toml` sets them: the step asks "Change? [y/N]". In the menu, option 0 removes these keys from `local.toml`, and the global settings then apply. Enter keeps the keys.
- `config.toml` sets them: the menu has no option 0, because `init` writes only `local.toml`, and a committed setting applies to each developer.

Option 3 "Disable the LLM analysis" writes `[llm] analyze_symbols = false` to `local.toml`. The embeddings and the chat of `smart_search` stay. To turn off the embeddings too, set `[llm] enabled = false`.

Option 1 (local Ollama) writes the chat model to an Ollama profile of the global config. This is the selected profile when it is an Ollama profile, else the profile `local`. The other keys of that profile stay. Then `local.toml` selects the profile with `[llm] profile`, and gets the embedding model in `[llm] embed_model`. The prompts of option 1 give the models that a config file sets as the defaults, thus Enter keeps them. Without such a file, the defaults are the recommended models. The chat model of an `openai_compatible` profile is not a default, because it is not a model of Ollama.

Option 1 then sends a test request to the profile. When the test fails, `local.toml` and the global config get their old text back.

The setup of the LLM services (option 2, and `fw-context llm setup`) does these steps:

1. It tells what the machine has: the local Ollama, the GPU memory and the RAM.
2. It lists the services: local Ollama, Ollama on another computer, Ollama Cloud (through the local Ollama), the ChatGPT plan, OpenRouter, DeepSeek, and another OpenAI-compatible API. A ✓ marks a service with a profile, and a ⚠ marks a service that sends the source code to another host. You select services by numbers, separated by commas.
3. For each selected service, it asks for the values: the model (with a recommendation), the API key (without an echo, into the key store; for OpenRouter and DeepSeek the key of the provider, `provider:<name>`), or the ChatGPT sign-in in the browser. It writes a profile to the global config, and asks before it replaces a profile of the same name.
4. It sends a request without code through each new profile. When a profile does not answer and you do not keep it, the global config gets its earlier text back: a profile that it replaced comes back with its earlier values. A key or a ChatGPT sign-in that no profile uses then goes.
5. It asks for profiles to remove, with a question for each. The removal also removes the own key of the profile and the ChatGPT sign-in.
6. In a project, it asks for the profile of the project: a profile that can run, the global default, or no LLM analysis. A profile without a key or a sign-in is listed with the command that repairs it. When `config.toml` (in git) selects a profile, the global default is not offered, because `local.toml` cannot remove that selection. A selection of a profile or of the global default turns the LLM analysis on again when `local.toml` turned it off.

The local Ollama service writes the profile of the Ollama of this machine: the selected profile only when it is such a profile, else `local`. The setup does not ask for the embedding model: it belongs to the project (option 1 of the LLM step of `init`). Ctrl-C stops the setup; it does not answer the question with its default.

The recommended local model comes from the memory of the machine: `gemma4:26b` with a 12 GB GPU, `gemma4:12b` with an 8 GB GPU, `qwen3-coder:30b` (a MoE model, usable on the CPU) with less GPU memory and 32 GiB of RAM or more, else `qwen2.5-coder:7b`. These values come from a measurement on C and C++ firmware. You select the model.

A cloud model of Ollama (`<model>:cloud`) runs on `ollama.com`, although its server is the local Ollama. fw-context reads the host from the model list of the server (`remote_host`), and tells that the source code leaves the machine.

For another OpenAI-compatible API, the setup asks for four values: a profile name (default `cloud`), the URL of the API, the name of the environment variable that holds the API key, and the model. When you give no variable, it reads the key without an echo and stores it for the profile (see [API keys](#api-keys)). No config file gets the key.

When no file sets `embed_model`, fw-context detects the model from the hardware: `qwen3-embedding:8b` with a GPU, `qwen3-embedding:0.6b` without one. `fw-context init` saves the detected name one time to the global config. Thus the model stays the same when a detection fails one time, which would embed the whole index again with a different dimension. The name goes to the global config and not to `local.toml`, because the hardware is a fact of the machine: each project then uses the global settings, and a later change of the global config applies to it. Earlier versions of `init` saved the name to `local.toml` of each project. Option 0 of the LLM step removes it.

### A config file that fw-context cannot use

fw-context stops with an error when one of the three files has one of these problems:

- fw-context cannot read the file, the file is not UTF-8, or the file is not valid TOML.
- A section other than `[updates]` is not a table, for example `build = "x"`.
- A value of the wrong type in a key that selects the data that fw-context reads or makes, or the place where data goes. For example `db_dir = 5`. These keys are:
  - `[project] id`
  - `[index]` `db_dir`, `compile_commands`, `config_header`, `vendor_paths`, `project_paths` and `transient_defines`
  - each key of `[build]` except `timeout` and `clean`, and each key of a `[[build.variants]]` table
  - `[llm]` `profile`, `embed_profile`, `embed_model`, `embed_dim`, `embed_query_prompt`, `embed_doc_prompt` and `debug_log`
  - `provider`, `model`, `url` and `api_key_env` of an LLM profile (`[llm.profiles.<name>]`)
  - `[cache_server] url` and `[call_graph] dispatch_bridges`
- A `[[build.variants]]` table has no `name`.
- A URL is not valid, for example `url = "http://[::1"` in an LLM profile.
- A problem of the LLM profiles:
  - An old `[llm]` backend key, for example `[llm] ollama_url` or `[llm] model`. See [The old `[llm]` backend keys](#the-old-llm-backend-keys).
  - A table `[llm.profiles]` in `config.toml` or `local.toml` of a project.
  - A profile without `provider` or `model`, or an `openai_compatible` profile without `url`.
  - A key in a profile that the provider of the profile does not know. A typo there can select a different backend.
  - `[llm] profile` or `[llm] embed_profile` names a profile that the global config does not have, or `embed_profile` names a profile that is not an Ollama profile. The error names the file that sets the key.
  - The global config has two or more profiles, and no file sets `[llm] profile`.

The error names the file and the key, but not the value, because a value can be a secret. `fw-context status` gives exit code 1, and the MCP status tools give the status `config_error`. fw-context does not use the default value in place of these values: a default gives answers about other data, or sends the source code to a different place.

A value of the wrong type in a different key gives a warning: for example a timeout, a limit or a switch (`timeout = "x"`, `enabled = "flase"`). In an LLM profile, these keys are `timeout`, `stream`, `num_ctx`, `keep_alive`, `auto_pull` and `max_concurrent`. A section `[updates]` that is not a table gives a warning too. fw-context then uses the value of a lower file, or the default of the key, and the tools work. Each command writes the warning to the log one time (a CLI command to stderr), `fw-context doctor` shows it, and the MCP status tools give it in the list `config_warnings`.

An older form of a value still works: a string that holds a number (`num_ctx = "4096"`), and `"yes"`, `"no"`, `"on"`, `"off"`, `"1"` and `"0"` for a switch. A key of a `[[build.variants]]` table goes to the build as it is, thus it must have the TOML type.

`fw-context doctor` lists each value that fw-context cannot use, in each of the three files. `fw-context doctor --fix --only config` removes each value. Then the value of a lower file applies, for example of `config.toml` for a value of `local.toml`, or else the default of the key. A `fw-context doctor --fix` without `--only` also does the other fixes, and it can pull a missing Ollama model of several GB. `fw-context init` and `doctor --fix --only <checks>` without `config` remove no value. The old `[llm]` backend keys are not removed: `fw-context doctor --fix --only config` and `fw-context init` convert them to LLM profiles (see [The old `[llm]` backend keys](#the-old-llm-backend-keys)). The fix also does not change a profile in a project file, or a profile without a correct `provider`, `model` or necessary `url`. Correct these yourself. Before the change, `local.toml` and the global config get a copy of the old text in `<name>.bak`. When one `doctor --fix` changes a file two times, the copy holds the text before the first change. `config.toml` of a project gets no copy, because git holds its old text. The fix does not remove a `[project] id` of the wrong type: without an ID, the project is not initialized, and the index of the project is lost. Correct the ID yourself. A file that is not valid TOML also needs you: the fix cannot read its values.

## Settings reference

### `[build]` — Build system

This section controls how `fw-context index` generates `compile_commands.json`. fw-context uses this section when a run builds: with `fw-context index --build`, and when a run builds on its own, for example when the build is not there. For the full list of cases, see [Automatic build](build.md#automatic-build).

> **Incremental is the default.** `fw-context index` reuses an existing `compile_commands.json` file whenever possible. Use `fw-context index --build` to force a clean build. A clean build also forces a full re-index.

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `system` | *(auto-detect)* | project | The build system: `"mbed-os"`, `"zephyr"`, `"platformio"`, `"esp-idf"`, `"arduino"`, `"cmake"`, `"keil-mdk"`, `"iar-ewarm"`, `"makefile"`, `"bare"`, `"stm32cubeide"` or `"ti-ccs"`. fw-context detects this automatically from project markers, such as `west.yml`, `.mbed`, or `platformio.ini`. For each build system, see [Build Configuration](build.md#configuration-reference). |
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

Shared `[build]` keys for multi-variant projects:

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
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

On a project of more than one build, each query tool needs the arguments
of one build: `variant`, `image`, or the two, as the error lists them. A
build without an image name needs only `variant`, and a build without a
variant name needs only `image`. See
[A project of several builds](tools.md#mcp-tools). The keys
`[build] default_variant` and `[build] default_image` are retired, and
they have no effect. The config loader ignores them and logs a warning
that they have no effect. It does not change your config file, thus you
can remove the keys yourself.

```toml
[build]
system = "bare"
compiler = "arm-none-eabi-gcc"
source_dirs = ["src", "lib"]
include_dirs = ["include"]

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
| `config_header` | `""` *(none)* | project | The path to a configuration header that the build generates (for example `config.h`), relative to the project root. fw-context adds `-include <file>` to each translation unit. Use this key for a build system that does not put `-include` into `compile_commands.json`. A missing file stops `fw-context index` with an error. |
| `rerank_top_k` | `50` | global, local | The number of rows at the top of each `semantic_search` page that `[llm] reranker_model` scores again. Without `reranker_model`, this key has no effect. |
| `min_dense_count` | `3` | project | The minimum number of embedding results for a `smart_search` ranking from the embeddings only. With fewer results, `smart_search` uses the FTS5 results. A value below 1 counts as 1. |
| `purge_max_missing_percent` | `20` | project | A limit for the removal of files from the index. When more than this percent of the indexed files are missing from disk, or are outside the build, `fw-context index` removes none of them and logs a warning. This limit protects the index from a network mount that is offline, or from a wrong project root. |
| `max_symbol_body_lines` | `1000` | project | The maximum number of lines of one symbol body that `get_source` and `get_symbol_context` return. The index stores the whole body. The value of the project of the query applies. A body that a cap cut carries `_source_truncated`. |
| `transient_defines` | `["MBED_BUILD_TIMESTAMP", "BUILD_TIMESTAMP", "BUILD_TIME", "BUILD_DATE", "BUILD_ID", "BUILD_NUMBER"]` | global, project, local | The names of the `-D` macros whose value makes each build unique, for example a build time or a build counter. When fw-context compares two builds, it ignores these macros: in the identity of the build (`config_hash`) and in the flags of each translation unit. Thus a new build that changes only such a value parses no file again. Mbed OS writes the time of the build into `MBED_BUILD_TIMESTAMP` on each unit. Write names only (`NAME`, not `-DNAME` or `NAME=1`). A project list replaces the global list, and `[]` makes every macro count. Remove a name that your code reads in an `#if`, because then a new value must change the index. A change of the list can change `config_hash` and start a full reindex. An entry that is not a macro name, or the key in a `[[build.variants]]` table, stops `fw-context index` with an error. |

fw-context asks each GCC compiler that `compile_commands.json` names for its system include directories and its predefined macros, as clangd `--query-driver` does. It runs the compiler with `-E -v` and `-dM -E` on an empty input. This is the compiler that the build of the project runs too, thus no configuration selects it. When a compiler does not answer, its units get the flags of `compile_commands.json`, a target from the compiler name, and toolchain directories that fw-context guesses from the layout of the toolchain.

Earlier releases read the keys `query_driver`, `query_driver_extra` and `query_driver_auto`, and wrote `.fw-context/toolchains.toml`. fw-context ignores these keys and this file now. The next `fw-context index` removes them, see [Obsolete keys and files](#obsolete-keys-and-files). Until then, a warning names the file that still has them.

The parse also needs the clang compiler headers (`stddef.h`, `arm_acle.h`) of the same major version as libclang, because the libclang wheel does not include them. fw-context ships these headers for the libclang version that it pins, and unpacks them to `~/.fw-context/clang-resource/<major>-<hash>/` at the first parse (one directory for each archive, thus two installations of fw-context do not share one copy), or when `fw-context doctor --fix`, `fw-context init` or `make install` runs. This needs no network and no administrator rights, and works on Linux, macOS and Windows. `fw-context doctor` reports the state as the `clang-resource` check.

WHY the query: a GCC cross compiler adds its system headers and its target macros on its own, and `compile_commands.json` does not hold them. Without the query, a target that libclang has no backend for (xtensa, ESP32) was parsed with the headers of the host, and `#if __XTENSA__` read as false.

### `[llm]` — LLM and embeddings

The LLM backend is a named **profile**: a table `[llm.profiles.<name>]`. A profile gives the server, the chat model and the API key variable. The profiles are in the global config `~/.fw-context/config.toml` only. `[llm] profile` selects one profile:

- The global config gives the default profile of each project.
- `config.toml` or `local.toml` of a project selects a different profile. The last file wins.

WHY the profiles are in the global config only: a profile names the server that gets the source code. A `config.toml` in git thus cannot send the source code to a host that the repository selects. It can only select a profile of the user. A table `[llm.profiles]` in a project file is an error.

When no file sets `[llm] profile` and the global config has one profile, fw-context uses that profile. With two or more profiles, `[llm] profile` is necessary. Without profiles, fw-context uses the defaults: a local Ollama at `http://localhost:11434`, with the model `qwen2.5-coder:14b`. A `profile` that names no profile of the global config is an error, and the error names the file that selects it.

When the selected profile sends the chat or the embeddings to a host that is not this machine, fw-context prints a notice on stderr when it loads the config of a project. The source code then goes to that host.

Put the other `[llm]` keys in `~/.fw-context/config.toml` for global defaults, or in `<project>/.fw-context/local.toml` for per-project overrides. Do not put them in the shared `config.toml` file. LLM configuration is specific to each developer.

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `enabled` | `true` | global, local | Enable the LLM and the Ollama calls. When this key is `false`, `smart_search` falls back to word-split FTS5. Also, `explain_symbol` returns the source code and a prompt for the AI assistant. `fw-context index` then skips the embeddings and the symbol analysis, also for a sentence-transformers `embed_model`. To keep the embeddings and skip only the analysis, set `analyze_symbols = false`. |
| `profile` | *(the only profile)* | global, project, local | The name of the LLM profile of the chat (analysis, `smart_search`, `explain_symbol`). It must name a profile of the global config. |
| `embed_profile` | *(see description)* | global, project, local | The name of the Ollama profile whose `url` is the server of the embeddings. When the selected profile is an `openai_compatible` profile, fw-context also uses `keep_alive`, `auto_pull` and `max_concurrent` of this profile. Without this key, the embeddings use the server of the selected profile when it is an Ollama profile, else `http://localhost:11434`. A profile with a different provider is an error. |
| `embed_model` | `""` *(auto-detect)* | global, local | The embedding model for vector search. Empty: fw-context selects `qwen3-embedding:8b` on a machine with a GPU (about 4.7 GB of VRAM, 4096-dimension vectors), and `qwen3-embedding:0.6b` without one. A name with the prefix `BAAI/`, `ibm-granite/`, `lightonai/`, `cross-encoder/` or `sentence-transformers/` uses sentence-transformers (requires the `st` extra); `ft://` selects a fine-tuned local model; any other name is an Ollama model. When the selected Ollama profile sets `auto_pull = true`, fw-context pulls an Ollama model automatically on first use; otherwise pull it manually. A change of model invalidates the stored embeddings. |
| `embed_query_prompt` | *(auto-detect)* | global, local | An instruction that fw-context adds before the query text, before it creates the embedding. fw-context detects this instruction automatically from the model name prefix: for `mxbai-*`, fw-context uses `"Represent this sentence for searching relevant passages: "`; for `qwen3-embedding*`, fw-context uses a code-retrieval instruction. Set this key explicitly to override the default, or set it to `""` to disable it. |
| `embed_dim` | *(none)* | global, local | The dimension of the stored vectors, for a sentence-transformers or `ft://` model with Matryoshka training. When the value is smaller than the dimension of the model, fw-context cuts each vector to this size and normalizes it again. An Ollama model ignores this key. A negative value means "not set". For a sentence-transformers or `ft://` model, the next `fw-context index` after a change of this key computes each stored embedding again. |
| `embed_doc_prompt` | *(empty)* | global, local | An instruction that fw-context adds before symbol descriptions during indexing. Most models work best with an empty prompt. Set this key only when the model's training expects a per-document instruction. After a change of this key, the next `fw-context index` computes each stored embedding again. |
| `reranker_model` | *(none)* | global, local | A cross-encoder model for search result reranking. Example: `"cross-encoder/ms-marco-MiniLM-L6-v2"`. When you set this key, fw-context rescores each result with the cross-encoder for higher precision. This key requires `sentence-transformers`. Default `None` — no reranking. |
| `analyze_symbols` | `true` | global, local | Generate an LLM analysis for each symbol during indexing. This analysis includes a summary, the inputs, and the outputs. fw-context stores this analysis in the `llm_analysis` table, so you can search for symbols by purpose. |
| `analyze_vendor` | `false` | global, local | When this key is `true`, the LLM analysis also includes the vendor and SDK code (for example `mbed-os/` or `zephyr/`). This can add hours to an index run. When `false`, the analysis includes only the project code. `fw-context index --analyze-vendor` and `--no-analyze-vendor` override this key. Without `analyze_symbols`, this key has no effect. |
| `debug_log` | *(none)* | global, local | The path to a JSONL debug log for LLM prompts and responses. Example: `"~/.fw-context/llm-debug.jsonl"`. |

#### `[llm.profiles.<name>]` — LLM profiles

Each profile is one LLM backend. The name of a profile has only letters, digits, `_` and `-`. The keys of a profile:

| Key | Default | Provider | Description |
|-----|---------|----------|-------------|
| `provider` | *(required)* | each | `"ollama"` for an Ollama server, `"openai_compatible"` for an API with the OpenAI chat format, `"openrouter"` for OpenRouter, `"deepseek"` for the API of DeepSeek, or `"chatgpt"` for the ChatGPT plan of the user (see [ChatGPT plan](#chatgpt-plan)). For `openrouter` and `deepseek`, fw-context knows the URL, the key variable and the request fields that turn the reasoning of the model off. A `chatgpt` profile has `provider`, `model` and, as each profile, `timeout`; `stream` has no effect, because each request of the plan streams. |
| `model` | *(required)* | each | The chat model. Use a model with 14B parameters or more: a smaller model gives JSON that is not consistent, and poor descriptions of embedded C++ code. |
| `url` | `"http://localhost:11434"` for Ollama, `"https://openrouter.ai/api/v1"` for OpenRouter, `"https://api.deepseek.com"` for DeepSeek | each | The server. Required for `openai_compatible`. fw-context adds `/chat/completions` to a URL that contains `/v1`, and `/v1/chat/completions` to a URL without `/v1`. A URL that ends with `/chat/completions` stays as it is. |
| `timeout` | `600.0` | each | The HTTP request timeout, in seconds. Embed requests use `timeout × 2`. |
| `stream` | `false` | each | When `true`, fw-context sends `stream: true` and reads SSE chunks for chat requests. This setting keeps the HTTP connection open with a continuous data flow, and prevents reverse-proxy idle timeouts (nginx default: 60 s, Cloudflare: 100 s). When `false`, fw-context uses a non-streaming path. |
| `num_ctx` | `16384` | `ollama` | The context window, in tokens. This size allows full function bodies during analysis generation. For the LLM analysis, it is the upper limit of the context that fw-context selects from the measured speed of the model, see [The context of the LLM analysis](#the-context-of-the-llm-analysis). |
| `keep_alive` | `"10m"` | `ollama` | How long Ollama keeps the model loaded in VRAM after a request. Use minutes, seconds, or `-1` for an indefinite time. During indexing, this setting prevents model loading before each request, which takes about 2 to 5 seconds each time. When the chat and the embeddings use one Ollama server (the same URL: `localhost` and `127.0.0.1` are two servers here), fw-context unloads the embedding model before the first request of an LLM analysis (also the count of the tokens), and the chat model before the embeddings: the two share the GPU memory, and a model that this setting keeps leaves the next model only the rest. This applies to `fw-context index`, `fw-context analyze` and `reindex_file`. A run whose analyses all come from the cache unloads nothing, and only a chat model that the same run used is unloaded, never the model of another client. A model that stays loaded (another client can load it again) gives a warning in the log. |
| `auto_pull` | `false` | `ollama` | When `true`, fw-context pulls a model automatically from the Ollama registry when it is not installed. When `false` (default), you must pull each model explicitly. Set this key to `false` for offline or intranet environments. |
| `max_concurrent` | `1` | `ollama` | The maximum number of parallel Ollama HTTP calls per process. Increase this key to 2–4 for multi-client MCP transports, such as SSE or streamable, where embedding requests (~100 ms) can overlap. Keep this key low for chat requests, which use the GPU more. |
| `api_key_env` | *(none)*; `"OPENROUTER_API_KEY"` for OpenRouter, `"DEEPSEEK_API_KEY"` for DeepSeek, when the profile has no `url` of its own | each API | The **name** of the environment variable that holds the API key, for example `"DEEPSEEK_API_KEY"`. Never write the key itself into a config file. When the variable is not set, fw-context uses the stored key of the profile (see [API keys](#api-keys)). |
| `routing` | `{ sort = "price", quantizations = ["int8", "fp8", "mxfp8", "fp16", "bf16", "fp32", "unknown"] }` | `openrouter` | The hosts of OpenRouter that get the request (the `provider` object of the OpenRouter API). `sort`: `"price"`, `"throughput"` or `"latency"`. `quantizations`: the precisions that a host can run, from `int4`, `int8`, `fp4`, `mxfp4`, `nvfp4`, `fp6`, `fp8`, `mxfp8`, `fp16`, `bf16`, `fp32` and `unknown`. `zdr`: `true` for hosts with zero data retention only. `data_collection`: `"deny"` for hosts that do not store or train on the data, or `"allow"`. A key of this table replaces the default of that key only. The default filter keeps out the precisions below 8 bits: measured on 2026-10-20, the `fp4` host of a DeepSeek model gave much weaker analyses than an `fp8` host. It keeps `unknown`, the value of the models of OpenAI. A model that only hosts below 8 bits run needs a filter with their precision, for example `quantizations = ["mxfp4"]`. |

fw-context reads `timeout`, `stream`, `num_ctx`, `keep_alive`, `auto_pull` and `max_concurrent` from the selected profile. Thus these values also apply to the embeddings. When the selected profile is an `openai_compatible` profile, `keep_alive`, `auto_pull` and `max_concurrent` come from the profile of `embed_profile`, because only the embeddings use Ollama. Without `embed_profile`, these keys have their defaults.

A key that the provider of the profile does not know is an error, because a typo can select a different backend. A value of the wrong type in `provider`, `model`, `url` or `api_key_env`, and each problem of `routing`, is an error. In the other keys, it gives a warning, and fw-context uses the default.

```toml
# ~/.fw-context/config.toml
[llm]
profile = "local"                  # the default profile of each project

[llm.profiles.local]
provider = "ollama"
url = "http://localhost:11434"
model = "qwen2.5-coder:14b"
num_ctx = 16384

[llm.profiles.gpu-box]             # an Ollama server on a different machine
provider = "ollama"
url = "http://192.168.1.50:11434"
model = "qwen2.5-coder:32b"
```

```toml
# <project>/.fw-context/local.toml
[llm]
profile = "gpu-box"
```

#### `[llm.models]` and `[llm.embed_models]` — the list of the models of Ollama

The global config holds a list of the models of Ollama that fw-context knows: `[llm.models."<tag>"]` for the LLM analysis, and `[llm.embed_models."<tag>"]` for the embeddings. `fw-context init` and `fw-context doctor --fix` write it, and you can change it. The list gives no setting to a run: a later release uses it to recommend a model.

```toml
[llm.models."gemma4:26b"]
quality = 3.73
quality_source = "fw-context benchmark 2026-10-20: 30 C/C++ symbols, prompt v1, blind grades 1-5"
weights_gb = 18.0
moe = true
remote = false
source = "fw-context 0.35.0"
fingerprint = "b82e286eb5d0770b"
```

| Key | Description |
|-----|-------------|
| `quality` | For the LLM analysis: the blind grade 1–5 of the LLM analysis of C/C++ symbols, from the benchmark of fw-context. It is not the result of a public benchmark: a model can be good at other tasks and weak at the analysis of firmware. For the embeddings: the quality of the search, when a benchmark gives it. |
| `quality_source` | The benchmark of `quality`: its date, the version of the prompt and the number of symbols. |
| `weights_gb` | The size of the model file, in GB (10^9 bytes): the memory of the weights. The context needs more (the KV cache). |
| `moe` | `true` for a mixture-of-experts model: it uses few experts for each token, thus it is usable from the RAM. |
| `dimension` | Embeddings only: the size of a vector. |
| `remote` | `true` for a cloud model of Ollama: the source code goes to another computer. |
| `usd_per_1k_symbols`, `usd_per_1k_symbols_off_peak`, `price_source` | A cloud model only: the price of the LLM analysis of 1000 symbols in USD, from the price list of Ollama and the tokens that the benchmark measured. A thinking model counts its reasoning. The off-peak price applies outside 12:00–18:00 UTC on weekdays, for the models that Ollama gives it. |
| `enabled` | `false` takes a model out of each recommendation. fw-context does not change this key. |
| `source`, `fingerprint` | An entry of fw-context: the release that wrote it, and a fingerprint of its values. |

The tag includes the quantization: `qwen2.5-coder:14b` and `qwen2.5-coder:14b-instruct-q8_0` are two entries. The list of each release holds a model of the LLM analysis when no other model of the same or a smaller size is more than 0.2 better. A model below 3.0 stays for a machine with little memory, and its quality is low. A cloud model needs no memory of this machine: the list holds it when no other cloud model of the same or a lower price is more than 0.2 better. The rule compares the lower of the two prices. A cloud model is never recommended without your consent, because the source code goes out.

When a new release of fw-context has a new list, `fw-context doctor` reports it (the check `llm-models`), and `fw-context doctor --fix` or `fw-context init` applies it:

- An entry that is not in the global config is added.
- An entry of fw-context that you did not change gets the values of the new release. Your `enabled` stays.
- An entry of fw-context that you changed stays. When the new release has other values for it, the report names them.
- An entry without `source` is yours. fw-context never changes it.
- An entry of fw-context that the new release does not have is removed when you did not change it. When you changed it, it stays, and the report names it.

A removed entry of fw-context comes back with the next `doctor --fix`: fw-context cannot tell a removed entry from one that is not added yet. To take a model out, set `enabled = false`. Thus a removal is also the way back to the values of fw-context for an entry that you changed. Each change counts, also a removed key or a value of the wrong type. A value of the wrong type or an unknown key gives a warning, and fw-context does not use that value. A project file cannot hold the list: the warning asks you to move it to the global config. `fw-context doctor --fix` and `fw-context init` write the list only when the result is valid TOML with each entry in its table. For a shape that they cannot change (an inline `[llm]`, a `[llm.models]` that is not a table of entries), they write nothing there and report it.

#### The context of the LLM analysis

With an `ollama` profile, fw-context selects the context of the LLM analysis from the measured speed of the model on this machine. The speed of the analysis follows where the model runs: all in the GPU memory, partly in the RAM, all in the RAM, or in the RAM with swap. The report of Ollama (`/api/ps`) does not tell this for each kind of model, thus fw-context measures it.

- **Measurement.** fw-context loads the model with a context, sends a short request, and then sends one reference request of about 1700 tokens. The speed comes from the times of llama.cpp in the answer.
- **Limits.** The upper limit is `num_ctx`, or the trained context of the model when it is smaller. The lower limit is 4096, or the upper limit when it is smaller.
- **Selection.** fw-context selects the largest context whose speed is at most 10 % below the speed at the lower limit, in steps of 1024 tokens or the upper limit itself.
- **Calibration.** The first manual `fw-context index` or `fw-context analyze` does the full search (about 6 measurements of some seconds each for 16384) and stores the result in `llm_calibration.json` in the global directory (see `FW_CONTEXT_HOME` above). The file belongs to the machine. Do not edit it. A new model file, a new Ollama release, or a change of the memory of this machine makes a new calibration. fw-context reads the GPU memory with `nvidia-smi`: a GPU that it does not tell (AMD, Intel) gives no memory, thus a new card of that kind does not make a new calibration. Then run the search again: remove the file. Each pair of limits has its own calibration, thus two profiles of one model with different `num_ctx` do not replace each other.
- **Manual run.** A manual run with a calibration does one measurement at the stored context. When the model is at most 15 % slower than in the calibration, the run uses that context: the wider margin keeps the noise of one measurement from starting a search. When it is slower, another program uses the memory now: the run searches a smaller context, and the calibration stays. When the lower limit is slower too, the log tells how many times slower the model is. When the model is more than 15 % faster than in the calibration, the run calibrates again.
- **Background reindex and `reindex_file`.** These use the stored context without a measurement, because they usually analyse few symbols. Without a calibration, they use `num_ctx`.
- **Memory.** A load that fails for memory (an out-of-memory error of Ollama, or "unable to load full model on GPU" for a model that is too large for the machine) is a context that does not fit. When the model does not load at the lower limit for memory, the run stops with an error, and a later run continues. The exception is a new calibration that a manual run starts because the model is clearly faster: the check just loaded the model at the stored context, thus the run uses that context. Another error is not a measurement, and the log gives it: in the search, the run uses the largest context that the search found; at the first measurement of a calibration, `num_ctx`; at the check of a manual run, the stored context. Ollama maps the weights of a model from its file: a model larger than the free RAM loads and runs slowly from the disk. The measurement shows that as a slow speed.
- **No selection.** An API profile and a cloud model of Ollama (a model that `/api/tags` lists with `remote_host`) use `num_ctx`: their memory is not on this machine, and a measurement would send the reference request out.

The log of the run gives the selected context and the time of a typical symbol. A prompt that does not fit the selected context goes to the second pass, which has its own larger context.

#### API keys

A config file never holds an API key: a key in a TOML file is a secret in plain text. fw-context gets the key of a profile from the first of these places that holds one:

1. The environment variable that `api_key_env` of the profile names. Set it in the environment of each process that runs fw-context: your shell for the CLI, and the LLM client that starts the MCP server.
2. The keyring of the system: the macOS Keychain, the Windows Credential Manager, or the Secret Service on Linux (for example GNOME Keyring or KWallet). The service is `fw-context`, and the user is the name of the profile.
3. `secrets.toml` in the global directory (`$FW_CONTEXT_HOME`, else `~/.fw-context`), with mode 0600 on Linux and macOS. fw-context writes a key there only when no keyring can be used, for example on Linux without a D-Bus session, in WSL or in a container. On Windows the mode does not keep other users out, but the Credential Manager is always there.

For a profile of `openrouter` or `deepseek` without a key of its own, fw-context then uses the key of the provider, which `fw-context llm key set provider:openrouter` stores. Thus two profiles of one provider, for example two models of OpenRouter, use one stored key. A profile name cannot have a `:`, thus a profile named `deepseek` never gives its key to the provider DeepSeek.

A profile of `openrouter` or `deepseek` with a `url` of its own (for example a proxy) gets neither the default variable nor the key of the provider: the key of OpenRouter must not go to a different host. `api_key_env = ""` turns the default variable off.

These commands store, remove and show a key. No command shows the key itself:

```bash
fw-context llm key set deepseek      # reads the key without an echo, or one line of stdin
fw-context llm key set provider:openrouter   # the key of each profile of OpenRouter
fw-context llm key status            # the place of the key of each API profile
fw-context llm key delete deepseek
```

fw-context gets the key when it sends the first request to the API, not when it reads the config, and keeps a found key in the memory of the process. A key that you store for a profile without a key applies to a running MCP server at the next request. A key that you change applies after the server refuses the old one (HTTP 401 or 403): then fw-context reads the key again. When no place holds a key, fw-context tells it one time, and sends the requests without a key. When the server replies HTTP 401 or 403, the error names the command that stores the key.

A key goes into an HTTP header, thus it can hold only printable ASCII characters without a space. fw-context removes white space at the start and at the end (for example the newline of a variable from a `.env` file). It does not store or use a key with a different character, and the message does not show the key.

#### External chat API examples

A profile with `provider = "openai_compatible"` sends the chat requests (analysis generation, `smart_search`, `explain_symbol`) to its `url`, with OpenAI-compatible JSON. The embeddings still use Ollama, see `embed_profile`.

**OpenAI:**

```toml
[llm.profiles.openai]
provider = "openai_compatible"
url = "https://api.openai.com/v1"
model = "gpt-4.1"
api_key_env = "OPENAI_API_KEY"
```

**Anthropic (through a proxy):**

Anthropic has no OpenAI-compatible endpoint. Use LiteLLM as a proxy —
it translates OpenAI-format requests to Anthropic-format requests.

```toml
[llm.profiles.anthropic]
# Point at your LiteLLM proxy. Set ANTHROPIC_API_KEY in the proxy env.
provider = "openai_compatible"
url = "http://localhost:4000/v1"
model = "claude-sonnet-4-20250514"
api_key_env = "LITELLM_API_KEY"
```

**DeepSeek:**

```toml
[llm.profiles.deepseek]
provider = "deepseek"            # the URL and the key variable have defaults
model = "deepseek-v4-flash"
```

fw-context turns the thinking of the model off (`thinking: {type: disabled}`): the thinking costs output tokens, and did not make the analysis better.

**OpenRouter:**

```toml
[llm.profiles.openrouter]
provider = "openrouter"
model = "deepseek/deepseek-v4.1-flash"
routing = { zdr = true }         # only hosts with zero data retention; sort and quantizations keep their defaults
```

fw-context turns the reasoning of the model off. A model that refuses it (HTTP 400 that names the reasoning) gets the lowest reasoning, without the reasoning text in the answer, and fw-context logs a warning: the reasoning tokens are billed as output. OpenRouter gives the cost of each request: fw-context reads it from the answer.

#### ChatGPT plan

A profile with `provider = "chatgpt"` sends the chat requests to OpenAI and bills them to the ChatGPT plan of the user ("Sign in with ChatGPT"), not to an API key. The usage counts against the limits of the plan: see ChatGPT Settings → Usage. For ChatGPT Plus, the five-hour limit is shared with each other app.

```toml
[llm.profiles.chatgpt]
provider = "chatgpt"
model = "gpt-6-luna"             # 'fw-context llm models chatgpt' lists the models of the plan
```

```bash
fw-context llm login chatgpt     # sign in in the browser (a loopback callback on 127.0.0.1)
fw-context llm models chatgpt
fw-context llm logout chatgpt    # revoke the sign-in and remove the tokens
```

- The sign-in checks the signature of the ID token against the keys of OpenAI, and the plan permission (`chatgpt.tokens.use.direct`). A profile signs in with one account: for another account, sign out first or make another profile.
- The tokens are in `auth/chatgpt-<profile>.json` in the global directory, mode 0600, not in the keyring: the record is larger than the Windows Credential Manager takes. fw-context refreshes the access token (1 hour) with the refresh token (30 days, a new one at each refresh) under a file lock, thus an index run and the MCP server do not refresh at the same time.
- Each request has `store: false`, and the reasoning at `effort: "none"`.
- When the plan reaches a usage limit, or the sign-in is not valid any more, the LLM analysis stops at once. The stored analyses stay, and a later run continues.

**LiteLLM proxy (local):**

LiteLLM translates between provider-specific formats. You run it locally,
and fw-context sends requests to it in the OpenAI format.

```toml
[llm.profiles.litellm]
provider = "openai_compatible"
url = "http://localhost:4000/v1"
api_key_env = "LITELLM_API_KEY"
model = "openai/gpt-4o"       # LiteLLM model routing syntax
# or: model = "deepseek/deepseek-v4-flash"
# or: model = "anthropic/claude-3-5-sonnet-20241022"
```

**llama.cpp server:**

The server needs no key, thus the profile has no `api_key_env`.

```toml
[llm.profiles.llamacpp]
provider = "openai_compatible"
url = "http://localhost:8080/v1"
model = "qwen2.5-coder:14b"
```

To use one of these profiles in a project, set `[llm] profile = "<name>"` in `.fw-context/local.toml`. To use it in each project, set the key in the global config. When you use a reverse proxy, set `stream = true` in the profile, to prevent idle timeouts.

### `[project]` — Metadata

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `name` | *(directory name)* | project | A readable project name. fw-context shows this name in `fw-context list` and in status output. |
| `id` | *(none)* | project | The unique ID of the project (32 hexadecimal characters). `fw-context init` writes it to `config.toml`, thus git shares it with the team. Without an ID, the project is not initialized, and the index of the project is lost. |

### `[call_graph]` — Dispatch bridges

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `dispatch_bridges` | *(built-in list)* | project | A table that maps a registration API to its dispatch entry point, for example `events::EventQueue::call_every` to `events::EventQueue::dispatch_forever`. For each call of the API, the call graph gets an edge from the entry point to the callback that the call gives. The built-in list covers Mbed OS, Zephyr, FreeRTOS, ESP-IDF and the STM32 HAL. An entry can only change the entry point of an API of the built-in list: the indexer records the calls of these APIs only, thus a key that is not in the list gives no edge. fw-context reads this key only from `.fw-context/config.toml` of the project. |

```toml
[call_graph.dispatch_bridges]
# The callbacks of call_every run in the thread of my_app::event_loop.
"events::EventQueue::call_every" = "my_app::event_loop"
```

### `[cache_server]` — Shared LLM Analysis Cache

Configure a remote cache server, to share `llm_analysis` data across developers. **Optional.** Without this section, the analysis stays local.

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `url` | *(none)* | global, local | The cache server URL. Example: `"https://fw-cache.example.com"`. |
| `token` | *(none)* | global, local | A bearer token with `can_read` and `can_write` permissions. Create this token with the `fw-cache-admin token create` command. |
| `batch_size` | `100` | global, local | The maximum number of hashes or entries in each HTTP request. |
| `force` | `false` | global, local | When this key is `true`, fw-context sends the `X-Cache-Overwrite` header, and overwrites existing entries. This key applies to the writes during `fw-context index` and `fw-context analyze`. `fw-context index --force-llm` and `fw-context analyze --force-llm` also turn it on. `fw-context cache push` does not read this key: use `fw-context cache push --overwrite`. This key requires a `can_overwrite` token. |

```toml
[cache_server]
url = "https://fw-cache.example.com"
token = "<your-token>"
# batch_size = 100
# force = false
```

For setup, deployment, and management instructions, see **[Cache Server →](cache-server.md)**.

### `[updates]` — Release check

At start, the MCP server asks PyPI for the newest release of `fw-context-mcp`. The request runs in a background thread, and no tool waits for it. After a request that succeeded, the server asks again only after 24 hours. The server stores the answer in `update_check.json` in the global directory (see `FW_CONTEXT_HOME` above). When the stored release is newer than the installed one, `get_project_status` gives an `update_notice`, and `fw-context version` prints a second line. A failed request writes nothing, and the next server start tries again. An editable install (`pip install -e`, `make install`) sends no request, because its package metadata does not follow the source tree.

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `check` | `true` | global | When this key is `false`, the MCP server sends no request to PyPI, and `get_project_status` gives no `update_notice`. Set it to `false` on a machine without network access, or where a request to PyPI is not permitted. fw-context ignores this key in a project config, and logs a warning when a project config sets it. |

The environment variable `FW_CONTEXT_NO_UPDATE_CHECK` also stops the check. Any value other than an empty string and `0` stops it. The test suite of fw-context sets it.

```toml
[updates]
check = false
```

`get_project_status` also reports a different fw-context that the operator installed while the MCP server runs (an upgrade or a downgrade), independently of this key. The server then does not run the installed code, and `client_restart_required` tells the user to restart the LLM client. This check reads only the local package metadata.

## Obsolete keys and files

fw-context removes the config keys and the files that an older release wrote and that no release reads now. The list is in `src/fw_context_mcp/housekeeping.py`, with the commit that retired each item. The old `[llm]` backend keys are not in this list: fw-context converts them to LLM profiles, see [The old `[llm]` backend keys](#the-old-llm-backend-keys).

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

The MCP server and the watcher daemon remove nothing. The background runs of the daemon are `fw-context index` runs, thus the cleanup runs in them too. When the cleanup cannot read or write a file, it logs a warning and continues: the index run does not stop for it. When fw-context cannot use the config or read the index (for example a config file that is not valid TOML, or an index of an older schema), the old build output stays. It stays until you correct the config file, or until an index run migrates the index.

### The old `[llm]` backend keys

Before the LLM profiles, `[llm]` named the LLM backend directly, in each of the three files. These keys are an error now:

`ollama_url`, `model`, `num_ctx`, `timeout`, `keep_alive`, `stream`, `auto_pull`, `ollama_max_concurrent`, `chat_api_base`, `chat_api_key` and `chat_api_format`

The error tells where the key goes. These commands convert the keys to LLM profiles:

- `fw-context init`, before it loads the config for the first time. With `--dry-run`, it shows the changes, writes nothing, and stops there, because the next steps read the converted config.
- `fw-context doctor --fix --only config`. `fw-context doctor` shows the changes in the check `config`.

The conversion makes these changes:

- **The global config.** Its old keys become a profile, and `[llm] profile` selects it when the key is not set. The name of a new profile is `local` for an Ollama server on this machine, `remote` for an Ollama server on a different machine, and `api` for an OpenAI-compatible API. When the name is in use, the conversion adds `-2`, `-3` and so on. When a profile with the same values exists, the conversion uses that profile.
- **A project.** The old keys of `config.toml` and `local.toml` apply over the profile that the project selects. For example, a project with only `num_ctx` keeps the API and the embeddings server of the global config. When the result is the same profile, the conversion removes the keys. Else it adds a new profile to the global config, with the name of the project directory, and `local.toml` selects it. When `local.toml` is missing, the conversion makes it. Before it adds a profile, the conversion sets `[llm] profile` of the global config when it is not set: else `load` selects the only profile for each project. A global config without a profile gets the profile `local` (Ollama on this machine), the backend of the old defaults. A global config that is missing is made from the template first. The new profile is a copy: a later change of the profile below it does not change the project.
- **The embeddings.** When the old keys send the embeddings to a server that the chat profile does not give them, the conversion also adds an Ollama profile `embeddings` with the URL of `ollama_url`. With a chat on an API, the profile also gets `keep_alive`, `auto_pull` and `ollama_max_concurrent` (as `max_concurrent`). `[llm] embed_profile` selects it, in the file of the old keys: the global config, or `local.toml` of the project.
- **The comments.** The conversion removes the commented lines of the old templates from `[llm]`, for example `# ollama_url = ...`. `local.toml` of a project gets a commented `profile` line when the conversion changes the file for a different reason. `config.toml` gets no new line.

The conversion selects the provider from `chat_api_format` and `chat_api_base`, with the same rules as the old releases. A `chat_api_format` of the global config applies to a `chat_api_base` of the project, as in the old releases. An `openai_compatible` profile has no `num_ctx`, because the old releases did not send it to such an API: the report says so. When the old keys sent the chat in the Ollama format, the result is an `ollama` profile. Else the result is an `openai_compatible` profile.

The conversion moves `chat_api_key` to the key store of the profile (see [API keys](#api-keys)), and removes it from the file. A key of a project gets a profile of the project, because the key applied to that project only. A new API profile of a project gets a copy of the key of the API profile below it, because the old releases gave the key of the global config to the project. When a key cannot be stored, the conversion changes no config file, and a second run stores the keys again. A key of an Ollama backend goes away without a copy, because the old releases did not send it to Ollama. When the project selects a profile with the same values already, its key goes to that profile. The `.bak` copy holds a mark in the place of the key. When `config.toml` of a project held the key, git keeps it in the history of the file: replace the key at the provider.

The conversion first makes and checks each new text, and makes sure that it can write each file. When it cannot write a file, it changes no file. Then it writes the global config, `local.toml` and `config.toml`, in this order, and stops at the first write that fails. Before the conversion changes `local.toml` or the global config, it writes the old text to `<name>.bak` beside the file. `config.toml` of a project gets no copy, because git holds its old text: commit the change.

## Examples

### Global config (`~/.fw-context/config.toml`)

```toml
[index]
db_dir = "~/.fw-context/index"
transient_defines = ["MBED_BUILD_TIMESTAMP", "BUILD_TIMESTAMP", "BUILD_TIME", "BUILD_DATE", "BUILD_ID", "BUILD_NUMBER"]

[llm]
# enabled = true   # set false to disable the LLM entirely
profile = "local"  # the default LLM profile of each project
# embed_profile = "local"   # the Ollama profile of the embeddings
# debug_log = "~/.fw-context/llm-debug.jsonl"
analyze_symbols = true

[llm.profiles.local]
provider = "ollama"
url = "http://localhost:11434"
model = "qwen2.5-coder:14b"
num_ctx = 16384
# keep_alive = "10m"
# timeout = 600.0

# An OpenAI-compatible API (the source code goes to its host):
# [llm.profiles.deepseek]
# provider = "openai_compatible"
# url = "https://api.deepseek.com"
# model = "deepseek-chat"
# api_key_env = "DEEPSEEK_API_KEY"
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
# compile_commands = "compile_commands.json"   # only for a build that fw-context cannot run
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
# activate = "/home/user/ncs-v3.2.3-env.sh"  # for Zephyr/NCS, ESP-IDF

[llm]
# profile = "local"                      # the LLM profile of this project: a profile of the global config
# enabled = false                        # set false if you do not use an LLM
# embed_profile = "local"                # the Ollama profile of the embeddings
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

# Installation

This is the complete installation guide. This guide covers prerequisites, setup, Ollama, the LLM setup, and AI assistant integration.

## Prerequisites

### Linux

```bash
# Ubuntu / Debian
sudo apt install python3 bear

# Arch / Manjaro
sudo pacman -S python bear

# Fedora / RHEL
sudo dnf install python3 bear

# uv (all distros)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### macOS

```bash
# Homebrew (install from https://brew.sh if not present)
brew install python@3.12 uv bear llvm

# Add Homebrew Python to PATH
echo 'export PATH="/opt/homebrew/opt/python@3.12/libexec/bin:$PATH"' >> ~/.zshrc
```

### Windows

Install these programs, and make sure that each one is on `PATH`:

- Python 3.11 or later, from [python.org](https://www.python.org/downloads/windows/).
- [Git for Windows](https://git-scm.com/download/win). fw-context uses `git` to find new source files.
- The compiler toolchain of your project, for example the
  [Arm GNU Toolchain](https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads).
- [LLVM](https://github.com/llvm/llvm-project/releases), when the project has assembly files (`.S`):
  fw-context runs `clang -E` to preprocess them. Without `clang` on `PATH`, the index has no symbols
  of the assembly files.

What was tested on Windows 11 (Python 3.11):

- The full test suite.
- A project in `bare` mode with `arm-none-eabi-gcc` 12.2 and 14.2, in a directory with a space
  and characters above 127 in its name: `fw-context init`, `fw-context index`, `fw-context search`,
  and the MCP server through an MCP client over stdio.

The other build systems (Zephyr and NCS, ESP-IDF, PlatformIO, CMake, Makefile, Mbed OS, Arduino,
Keil, IAR) were not tried on Windows with a real project. The tests cover their Windows code: the
activate scripts (`.bat`, `.cmd`, `.ps1`), the lookup of the tools through `PATHEXT`, and the
command lines of Windows.

A GCC older than version 13 on Windows cannot open a file whose path has a character outside the
ANSI code page of the system (measured with GCC 12.2: "ě" became "e", and GCC did not find the
file). GCC 13 and later declare the code page UTF-8 in their manifest
([GCC PR108865](https://gcc.gnu.org/bugzilla/show_bug.cgi?id=108865); measured with 14.2). Use
GCC 13 or later, or keep such a project in a path without these characters.
`bear` is not available on Windows.

| What | Why |
|------|-----|
| Python 3.11+ | Runtime |
| [`uv`](https://docs.astral.sh/uv/) *(optional)* | Fast package installer. Without a working `uv`, `make install` uses `python -m venv` and `pip`. |
| Compiler toolchain (for example ARM GCC or the Zephyr SDK) | libclang needs system headers to parse cross-compiled code |
| [`bear`](https://github.com/rizsotto/Bear) | Intercepts build commands → `compile_commands.json` |
| [Ollama](https://ollama.com) *(optional)* | Computes the vector embeddings (`semantic_search`, `smart_search` re-rank) and the symbol analysis (`explain_symbol`). The core index does not need it. See [Ollama (optional)](#ollama-optional). |

## Install

### pip (recommended)

```bash
pip install fw-context-mcp
# or via uv:
uv pip install fw-context-mcp
```

This command installs the latest release from PyPI with all dependencies
(libclang-ng, pysqlite3, sqlite-vec, watchfiles, httpx, mcp, tomli-w, tomlkit,
keyring, pyjwt, packaging, PyYAML).  The `libclang-ng` wheel holds the
libclang library and its Python binding, thus no system libclang package is
necessary.

The `fw-context` binary lands in your active Python environment's `bin/`
directory — typically `~/.local/bin` for user installs, or the virtual
environment's `bin/` if you use one. Ensure that directory is on your
`PATH`.

### From source

```bash
# Clone
git clone git@github.com:turbyho/fw-context-mcp.git ~/.fw-context/src

# Install (creates venv, installs package, symlinks binaries into ~/.local/bin)
cd ~/.fw-context/src && make install
```

`make install` uses `uv` when `uv --version` runs. When `uv` is missing,
or is a shim that cannot run (pyenv, asdf), it uses `python3 -m venv` and
`pip` in the venv `~/.fw-context/.venv`. The package is installed in
editable mode.

**File watcher (auto-reindex on save):**

The MCP server starts a background watcher daemon automatically. When you save a C/C++ file, this daemon waits 2 s for more changes. Then it starts `fw-context index --background`, which updates the index. A change of the checkout (another branch, a tag, or a commit on a detached HEAD) also starts such a run. You do not need separate configuration.

Only an MCP server starts the daemon. `fw-context index` does not start it. Each MCP server of the project sends a ping to the daemon every 15 s, and the daemon stops when it gets no ping for 60 s.

## Configure your project

After you install fw-context, run `fw-context init` in your firmware project. It creates `<project>/.fw-context/config.toml` and `local.toml` with default values and a project ID. The MCP tools and `fw-context index` create no config file in a project that `init` did not set up.

Two project-level files exist:

| File | Purpose | Git |
|------|---------|-----|
| `.fw-context/config.toml` | Shared settings (build, index, project name) | Commit |
| `.fw-context/local.toml` | Private overrides (LLM profile, db path, analysis toggles) | Gitignore |

The LLM profiles (server, model, API key variable) are in the global config `~/.fw-context/config.toml`. A project file only selects a profile.

A config file never holds an API key. fw-context gets the key of a profile from the environment variable that `api_key_env` names, then from the keyring of the system, then from `secrets.toml` in `~/.fw-context`. To store a key, run `fw-context llm key set <profile>`. See [API keys](configuration.md#api-keys).

For the full reference, see **[Configuration →](configuration.md)**.

### Zephyr

```toml
[build]
board = "nrf52840dk_nrf52840"    # required
# clean = true                   # pristine build (recommended)
```

### PlatformIO / Arduino

```toml
[build]
# nothing required — auto-detected from platformio.ini
# clean = true

[index]
# PlatformIO framework packages are auto-detected as vendor (is_project=0).
# For vendored code your team maintains, use project_paths to mark it as project:
# project_paths = ["src/my_customized_framework"]
```

### Mbed OS

```toml
[project]
name = "my-mbed-app"

[build]
system = "mbed-os"               # auto-detected from .mbed
target = "NUCLEO_F429ZI"         # auto-detected from .mbed / custom_targets.json
toolchain = "GCC_ARM"            # auto-detected from .mbed
profile = "develop"              # best for indexing (includes -g debug symbols)
app_config = "mbed_app.json"
# extra_profiles = ["lto.json"]   # uncomment if you use LTO
defines = [
    "DEBUG",
    "ENABLE_LOGGING",
]

[index]
# vendor_paths = ["third_party"]          # additional vendor dirs (additive to auto-detection)
# project_paths = ["src/old_hal"]        # manual project dirs (overrides auto-detection)
```

> fw-context passes **`defines`** to the compiler as `-D` flags. Use `defines` for version
> numbers and feature toggles. This makes fw-context index `#ifdef DEV` and
> `#if VERSION_FW_MAJOR` branches correctly.

## Dependencies by Build System

Install only the tools that your build system needs. `pip install fw-context-mcp`
installs the core Python packages automatically: `libclang-ng`, `pysqlite3`,
`sqlite-vec`, `watchfiles`, `httpx`, `mcp`, `tomli-w`, `tomlkit`, `keyring`,
`pyjwt`, `packaging`, and `PyYAML`.

### Core (All Projects)

| Tool | Why |
|------|-----|
| Python 3.11+ | Runtime |
| `clang` (system binary) | fw-context uses this tool to resolve expanded macro values, with `clang -dM -E`. It also preprocesses assembly units (`.S`) with `clang -E -C`. Without `clang`, fw-context does not index the symbols, the call edges and the vector tables of assembly units. |

```bash
# Arch / Manjaro
sudo pacman -S clang
# Ubuntu / Debian
sudo apt install clang
# Fedora / RHEL
sudo dnf install clang
# macOS
xcode-select --install   # ships clang as part of Command Line Tools
```

### Zephyr RTOS

| Tool | Why | Install |
|------|-----|---------|
| **west** | Build orchestration | Included with the Zephyr SDK. See [Getting Started](https://docs.zephyrproject.org/latest/develop/getting_started/index.html) |
| Zephyr SDK | Toolchain + cmake + host tools | Follow the Zephyr install guide |

`fw-context` runs `west build -b <board>`. `fw-context` enables `CMAKE_EXPORT_COMPILE_COMMANDS=ON` automatically.

> **Nordic NCS users:** `fw-context init` lists the NCS versions that
> `nrfutil sdk-manager list` gives, and you select one. It then writes an
> activation script for that toolchain, `.fw-context/ncs-env.sh`, from
> `nrfutil sdk-manager toolchain env` and `ZEPHYR_BASE`, and writes its path
> to `.fw-context/local.toml`, as `activate`. For plain Zephyr,
> `fw-context init` uses `ZEPHYR_SDK_INSTALL_DIR`, the `ZEPHYR_BASE` of your
> shell (`zephyr-env.sh`) or `~/zephyr-sdk-*/` (`environment-setup-*`).
> Then it asks for the script that sets up the environment of the project:
> Enter keeps the script that it found, and a path selects the script of
> your project. Each build, and each lookup of `west`, runs after
> `source <activate>`.
> See [Build Configuration](build.md#13-build-environment--activation-script-for-zephyrncs).

### PlatformIO

| Tool | Why | Install |
|------|-----|---------|
| **pio** / **platformio** | Build + compile_commands.json | `pip install platformio` |

`fw-context` runs `pio run --target compiledb` plus a real build for `.d` files.

> **`fw-context init`** detects PlatformIO automatically, from
> `~/.platformio/penv/bin/python` (the bundled venv) or from `pio` on PATH.
> When automatic detection fails, set `python = "/path/to/python"` in
> `.fw-context/local.toml`.

### Mbed OS

| Tool | Why | Install |
|------|-----|---------|
| **bear** | Intercepts build → compile_commands.json | `sudo pacman -S bear` / `sudo apt install bear` / `sudo dnf install bear` / `brew install bear` |
| **mbed** CLI | `mbed compile` | `pip install mbed-cli` (requires Python 3.11 or older — use pyenv or venv) |
| ARM GCC | Cross-compiler toolchain | [ARM GNU Toolchain](https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads) |

`fw-context` runs `bear --output compile_commands.json -- mbed compile -t GCC_ARM -m <target>`.

> **`fw-context init`** detects the Python interpreter for `mbed-cli` automatically,
> from pyenv versions (`~/.pyenv/versions/*/bin/mbed`), common venv paths
> (`~/mbed_venv`), and `~/.local/bin/mbed`. When `fw-context init` detects the
> interpreter, it writes the path to `.fw-context/local.toml`. For details, see
> **[Build Configuration →](build.md#12-build-environment--custom-python-for-mbed-cli)**.

### Arduino CLI

| Tool | Why | Install |
|------|-----|---------|
| **arduino-cli** | Compile + compile_commands.json | `curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh \| sh` |

This build system requires `fqbn` (fully qualified board name) in
`.fw-context/config.toml`. Example: `fqbn = "arduino:avr:uno"`.

`fw-context` runs `arduino-cli compile --export-compile-commands` in two passes:
first for the database only, then for a real compile that creates object files.
Header dependency tracking uses `manifest.json`, which fw-context builds from
the libclang token stream. This build system does not need `.d` files.

### ESP-IDF

| Tool | Why | Install |
|------|-----|---------|
| **idf.py** | Build wrapper | Bundled with [ESP-IDF](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/get-started/) |
| cmake | Build system | Bundled with ESP-IDF |

`fw-context` runs `idf.py build`. This command requires the ESP-IDF environment:
`. ./export.sh`, or `idf.py` on PATH.

> **`fw-context init`** detects the ESP-IDF export script automatically, from
> `$IDF_PATH/export.sh`, `~/esp/esp-idf/export.sh`, or `idf.py` on PATH. When
> `fw-context init` detects the script, it writes `activate` to
> `.fw-context/local.toml`. Then builds work without you sourcing the
> environment manually.

### Generic CMake

| Tool | Why | Install |
|------|-----|---------|
| **cmake** | Build + compile_commands.json | `sudo pacman -S cmake` / `sudo apt install cmake` / `sudo dnf install cmake` / `brew install cmake` |

Use this build system for any CMake project that is not Zephyr or ESP-IDF.
`fw-context` runs `cmake -B <out> -DCMAKE_EXPORT_COMPILE_COMMANDS=ON && cmake --build <out>`.
`<out>` is `.fw-context/build/<variant>/out`. With `[build] cmake_generator`,
the configure step also gets `-G <generator>`.

### Makefile

| Tool | Why | Install |
|------|-----|---------|
| **make** | Build tool | `sudo pacman -S make` / `sudo apt install make` / `sudo dnf install make` / macOS: bundled with Xcode CLT |
| **compiledb** | Generate compile_commands.json from make output | `pip install compiledb` |

`fw-context` runs `compiledb -n -o compile_commands.json make` (dry-run by default).

### Keil MDK

| Tool | Why | Install |
|------|-----|---------|
| **keil2clangd** | Converts `.uvprojx` → compile_commands.json | `pip install keil2clangd` |

**You do not need a Keil installation.** `keil2clangd` parses the XML project file statically.

### IAR EWARM

| Tool | Why | Install |
|------|-----|---------|
| **keil2clangd** | Converts `.ewp` → compile_commands.json | `pip install keil2clangd` |

**You do not need an IAR installation.** `keil2clangd` parses the XML project file statically.

### Manual (Bare) Mode

| Tool | Why | Install |
|------|-----|---------|
| **gcc** (or any C/C++ compiler) | Syntax-only compilation for `.d` files | `sudo pacman -S gcc` / `sudo apt install gcc` / `sudo dnf install gcc` / `brew install gcc` (or Xcode CLT clang) |

This mode needs no build system. Configure `source_dirs`, `include_dirs`, and
`defines` in `.fw-context/config.toml`. `fw-context` scans the sources and
generates `compile_commands.json`. The `.d` files go to
`.fw-context/build/default/out/deps`, not next to the sources.

### STM32CubeIDE (Manual Setup)

This setup needs no CLI tools. Enable the built-in JSON Compilation Database generator:

**Project Properties → C/C++ Build → Builder Settings → ✓ Generate JSON Compilation Database**

Then run `fw-context index compile_commands.json` with the generated file.

### TI Code Composer Studio (Manual Setup)

This setup needs no CLI tools. Enable JSON Compilation Database in Project Settings. Or use `bear`:

```bash
bear -- eclipse -nosplash -application com.ti.ccstudio.apps.projectBuild ...
```

### Summary Table

| Build System | CLI Tools | Python Extras | IDE/Toolchain Required? |
|---|---|---|---|
| **Zephyr** | `west` | — | Zephyr SDK |
| **PlatformIO** | `pio` | — | no |
| **Mbed OS** | `bear`, `mbed` | `mbed-cli` | ARM GCC |
| **Arduino** | `arduino-cli` | — | Arduino CLI |
| **ESP-IDF** | `idf.py` | — | ESP-IDF |
| **CMake (generic)** | `cmake` | — | no |
| **Makefile** | `make` | `compiledb` | no |
| **Keil MDK** | — | `keil2clangd` | **no** (static parse) |
| **IAR EWARM** | — | `keil2clangd` | **no** (static parse) |
| **Manual (bare)** | `gcc` (any compiler) | — | any C/C++ compiler |
| **STM32CubeIDE** | — | — | STM32CubeIDE (manual) |
| **TI CCS** | — | — | TI CCS (manual) |

## Update

```bash
cd ~/.fw-context/src && make update
```

`make update` runs `git pull`, and then the same editable install as
`make install`.

An update from a release that used the `libclang` package leaves that
package installed. Run `fw-context doctor --fix`: it removes the old
package and installs `libclang-ng` again. See [libclang](#libclang).

An update can change the format of the rows in the index. Then
`get_project_status` reports `row_format_mismatch`. Run `fw-context index`
once in each project to write the rows again.

An update can also rename or remove an MCP tool. The instruction block
that an older `fw-context init` wrote into `CLAUDE.md` or `AGENTS.md`
keeps the old tool names. Run `fw-context init` again in each project to
write the new block. For a global install, run
`fw-context init --scope global` again.

An older release can leave files and config keys that no release reads
now. `fw-context index` and `fw-context doctor --fix` remove them. To see
the list first, run `fw-context cleanup --dry-run`. To remove them now,
run `fw-context cleanup`. See
[`fw-context cleanup`](tools.md#fw-context-cleanup).

### Update from 0.35 or older

Do these steps in this order:

1. Run `fw-context init` in one project, or
   `fw-context doctor --fix --only config`, one time. The command converts
   the old `[llm]` backend keys of the global config (`ollama_url`,
   `model`, `num_ctx` and the other keys) to LLM profiles. Until then, the
   other commands stop with a config error. See
   [The old `[llm]` backend keys](configuration.md#the-old-llm-backend-keys).
2. In each project, run `fw-context init` before `fw-context index`. It
   converts the old `[llm]` keys of the project (`config.toml` and
   `local.toml`), and it writes the new instruction block for the LLM.
3. Run `fw-context index` in each project. The first run of each build
   writes each row again, as a first index run does: the row format and the
   config hash changed. The LLM analysis of each global variable with a
   definition also runs again one time (see
   [`fw-context analyze`](tools.md#fw-context-analyze)). On a paid LLM
   profile, this run costs more than a usual run.

## Verify

```bash
fw-context --help
fw-context status
fw-context doctor                       # audit the dependencies, the index and the config
fw-context doctor --fix                 # audit, and repair what it can
fw-context doctor --only clang-resource # run only these checks
```

For each check of `fw-context doctor`, see
[`fw-context doctor`](tools.md#fw-context-doctor).

## Ollama (optional)

**The core index does not need Ollama.** Symbols, full-text search, the
call graph, and source retrieval come from libclang and SQLite only.

With the default configuration, Ollama does three jobs:

| Job | Used by | Without Ollama |
|-----|---------|----------------|
| Vector embeddings (`[index] index_embeddings = true`, default) | `semantic_search`, the re-rank step of `smart_search` | `fw-context index` logs the error, stores no vectors, and marks the build `"partial"`. The status tools then ask the LLM to tell you. `semantic_search` falls back to `search_code`, with a `warning`. When you use no embedding model and no LLM, set `[llm] enabled = false`. |
| Symbol analysis (`[llm] analyze_symbols = true`, default) | `explain_symbol`, symbol descriptions in search results | `fw-context index` logs a warning and skips the analysis. |
| Search query generation | `smart_search` | `smart_search` uses FTS5 only. |

Alternatives:

- **Chat through an API.** Select an LLM profile of the provider
  `openrouter`, `deepseek`, `openai_compatible` or `chatgpt` (the ChatGPT
  plan). Symbol analysis and query generation then use that service, and
  the source code goes to its host. See
  [External chat API examples](configuration.md#external-chat-api-examples)
  and [ChatGPT plan](configuration.md#chatgpt-plan).
- **Embeddings through an API.** Set `[llm] embed_profile` to a profile of
  the provider `openrouter` or `openai_compatible`, and `[llm] embed_model`
  to a model of that API. See
  [Embeddings through an API](configuration.md#embeddings-through-an-api).
- **Embeddings without Ollama.** Install the `st` extra
  (`pip install "fw-context-mcp[st]"`) and set `embed_model` to a
  sentence-transformers model, for example `"BAAI/bge-small-en-v1.5"`.
  `fw-context index` then stores the vectors without Ollama, and
  `semantic_search` embeds the query locally too.
- **No LLM.** Set `enabled = false` in `[llm]`. The LLM-calling tools then
  return raw prompts, and the AI assistant processes the results with its
  own model. `fw-context index` then skips the embeddings and the symbol
  analysis. This is also true for a sentence-transformers `embed_model`.
- **Embeddings without the analysis.** Set `[llm] analyze_symbols = false`.
  `fw-context index` then stores the embeddings and skips the symbol
  analysis. `[index] index_embeddings = false` skips only the embeddings.

### Install Ollama

```bash
# Linux / macOS (all distros)
curl -fsSL https://ollama.com/install.sh | sh

# Arch / Manjaro
sudo pacman -S ollama
# or: yay -S ollama

# Fedora / RHEL
sudo dnf install ollama

# Verify
ollama --version
```

Ollama runs a daemon on `http://localhost:11434`. This daemon must run when
you run `fw-context index` (embeddings, symbol analysis), and when you call
`semantic_search`, `smart_search`, or `explain_symbol`.

```bash
ollama serve &   # start daemon if not running as a service
```

fw-context needs Ollama 0.30.1 or later. An older server cannot give the
count of the tokens of a prompt to the LLM analysis, and it can cut a long
prompt or a long embedding input without an error. `fw-context doctor`
reports an older server (the checks `ollama` and `chat-model`).

### Pull models

The LLM step of `fw-context init` and `fw-context llm setup` recommend a
chat model and an embedding model for this machine, and offer to pull a
local model. `auto_pull` of an LLM profile is `false` by default, thus
fw-context pulls no model at an index run. To pull a model yourself:

```bash
ollama pull <model>
```

With an empty `embed_model` (default), fw-context selects
`qwen3-embedding:8b` on a machine with a GPU, and `qwen3-embedding:0.6b`
without one. `qwen3-embedding:4b` has the same quality as 8b, and uses
less VRAM.

### Model recommendations

The global config holds the list of the models of Ollama that fw-context
knows, with the measured quality and the size of each model:
`[llm.models]` for the LLM analysis and `[llm.embed_models]` for the
embeddings. `fw-context init` and `fw-context doctor --fix` copy the list
of the installed release into the global config. To get the
recommendation for this machine, run:

```bash
fw-context llm recommend           # measure the installed models, then recommend
fw-context llm recommend --again   # measure each installed model again
```

On a machine without a GPU, or with little memory, a cloud model or an API
is the only practical way. See
[`[llm.models]` and `[llm.embed_models]`](configuration.md#llmmodels-and-llmembed_models--the-list-of-the-models-of-ollama)
and [The recommendation of a model](configuration.md#the-recommendation-of-a-model).

### Configure

The LLM step of `fw-context init` writes an LLM profile for you. To write
it yourself, edit the global config `~/.fw-context/config.toml`. Each
profile is one LLM backend, and `[llm] profile` selects one:

```toml
[llm]
enabled = true
profile = "local"
# embed_model = ""                 # empty = auto: qwen3-embedding:8b (GPU) / :0.6b (CPU)

[llm.profiles.local]
provider = "ollama"
url      = "http://localhost:11434"
model    = "qwen2.5-coder:14b"
num_ctx  = 16384
```

> **Remote Ollama:** Add a profile with `url = "http://192.168.1.50:11434"`
> when Ollama runs on another machine. To make it the Ollama profile of this
> machine, run `fw-context llm default <name> --ollama`.
> **One Ollama profile for each machine:** Each project that uses Ollama
> uses the Ollama profile of this machine. A project cannot select another
> Ollama profile. A project can select an external profile (an API, the
> ChatGPT plan, a cloud model of Ollama) in
> `<project>/.fw-context/local.toml`: `[llm] profile = "<name>"`. For one
> run with another model, use `--llm <name>`. See
> [One Ollama profile for each machine](configuration.md#one-ollama-profile-for-each-machine).
> A profile in a project file is an error. For each key, see
> [Configuration](configuration.md#llm--llm-and-embeddings).

## LLM setup

The LLM step of `fw-context init` sets up the LLM services: a local
Ollama, an Ollama on another computer, Ollama Cloud, the ChatGPT plan,
OpenRouter, DeepSeek, or another OpenAI-compatible API. To change the
setup later, run `fw-context llm setup`. It has the same steps, and one
more step that removes the profiles that you do not use. See
[How config works](configuration.md#how-config-works).

```bash
fw-context llm setup                       # set up the LLM services (needs a terminal)
fw-context llm list                        # each profile, and where it sends the source code
fw-context llm test <profile>              # send a request without code through a profile
fw-context llm default <profile>           # the profile of this project (local.toml)
fw-context llm default <profile> --global  # the profile of each project that selects none
fw-context llm default <profile> --ollama  # the Ollama profile of this machine
fw-context llm key set <profile>           # store the API key of a profile
fw-context llm login <profile>             # sign a chatgpt profile in with ChatGPT
fw-context llm recommend                   # recommend a local chat and embedding model
fw-context llm remove <profile>            # remove a profile, its own key and its sign-in
```

- `[llm] ollama_profile` of the global config names the Ollama profile of
  this machine. `fw-context llm default <profile> --ollama` writes it. See
  [One Ollama profile for each machine](configuration.md#one-ollama-profile-for-each-machine).
- A profile of the provider `chatgpt` uses the ChatGPT plan of the user.
  To sign it in, run `fw-context llm login <profile>`. See
  [ChatGPT plan](configuration.md#chatgpt-plan).
- `--llm <profile>` of `fw-context index` and `fw-context analyze` selects
  the profile of the LLM analysis for one run. `--force-llm` makes the LLM
  analysis of each symbol again. See
  [The LLM profile of one run](tools.md#the-llm-profile-of-one-run).

For each command, see
[`fw-context llm`](tools.md#fw-context-llm-setup-list-test-default-remove-recommend),
[`fw-context llm key`](tools.md#fw-context-llm-key) and
[`fw-context llm login`](tools.md#fw-context-llm-login-logout-models).

## AI assistant setup

```bash
fw-context init                    # detect & configure current project (default)
fw-context init --list-tools       # show what's supported and detected
fw-context init --dry-run          # preview without writing
fw-context init --tool claude-code # specific tool only
fw-context init --scope global     # global install (all projects)
fw-context init --scope all        # both global and project
fw-context init --quick            # skip AI tool registration only
fw-context init --skip-doctor      # skip the dependency audit
fw-context init --skip-build       # skip compile_commands.json generation
fw-context init --non-interactive  # disable prompts (CI/pipe)
fw-context quickstart              # alias for `init --quick`
```

**Provisioning:** `fw-context init` now does more than AI-tool setup. It
runs these steps, in order:

1. Audit the dependencies, and auto-fix what it can. This step runs each
   fix of `fw-context doctor --fix` (for example the missing pip packages,
   the clang headers and the obsolete files) except two: the model pulls
   (`ollama pull`), and the fix of `config`, which removes values from the
   config files. `--skip-doctor` skips this step. This step comes first,
   because the next steps write config files.
2. Convert the old `[llm]` backend keys (`ollama_url`, `model`,
   `chat_api_base` and the other keys) to LLM profiles of the global
   config (see
   [Configuration](configuration.md#the-old-llm-backend-keys)).
3. Copy the list of the models of Ollama of this release into the global
   config (`[llm.models]` and `[llm.embed_models]`, see
   [Model recommendations](#model-recommendations)). An entry that you
   changed stays.
4. Detect the build system and its parameters (for example the board).
5. Generate the project ID when the config has none, and register the
   project in the global project registry.
6. Detect the build environment of this machine (`python`, `activate`,
   `idf_path`), and write it to `local.toml`.
7. Create a missing config file from its template, and add the missing
   keys to an existing one: the global config, `.fw-context/config.toml`
   and `.fw-context/local.toml`.
8. Generate `compile_commands.json` when the project is buildable.
   `--skip-build` skips this step, and so does a project of several variants.
9. Register the AI tools (skipped by `--quick`).
10. Update `.gitignore`, and install the skills and the agents (the two
    skipped by `--quick`).
11. Set up the LLM services (the LLM step). The step has two parts: the
    services that you use, then the service of the LLM analysis and the
    service of the embeddings, in two different lists. In a project, each
    choice asks for its scope: this project only (`local.toml`), or each
    project (the global config). `fw-context llm setup` runs the same step
    later. See [LLM setup](#llm-setup) and
    [Configuration](configuration.md#how-config-works).
    In a pipe, or with `--non-interactive`, this step writes nothing.
12. Save the detected embedding model one time to the global config. This
    step writes nothing when a config file sets `[llm] embed_model`, or
    when the LLM is off.
13. Print a checklist of the remaining manual steps.

When build-system detection fails, `fw-context init` asks for the missing
value interactively (build system, board, FQBN, and so on). In a pipe, or
with `--non-interactive`, the missing value goes to the checklist instead.

**What `fw-context init` does:**

By default, with `--scope project`, `fw-context init` writes the instructions
only into the current project. It detects each AI tool on the system: a
binary on `PATH`, or a global config directory. `fw-context init --list-tools`
shows the result.

| Tool (`--tool`) | Detection | Instructions in the project |
|------|-----------|--------|
| **Claude Code** (`claude-code`) | `claude` on `PATH`, or `~/.claude` | Adds `<!-- fw-context -->` to `CLAUDE.md`. Installs the `code-explorer` and `general-purpose` agents. Installs the `fw-review` skill. |
| **OpenCode** (`opencode`) | `~/.config/opencode` | Adds a marked section to `AGENTS.md`. Installs the skill. |
| **Kilo Code** (`kilocode`) | `~/.kilocode` | Reads the instructions of OpenCode (`AGENTS.md`). |
| **Codex** (`codex`) | `~/.codex` | Writes `.codex/rules/fw-context.md`. |
| **Cursor** (`cursor`) | `~/.cursor` or `~/.config/Cursor` | Writes `.cursor/rules/fw-context.mdc`. |

**Project agents:** `fw-context init` creates two subagent definitions in `.claude/agents/`:
- `code-explorer`: this subagent includes a `CRITICAL — C/C++ source access` block. This block enforces fw-context for all C/C++ code reading
- `general-purpose`: this subagent has the same enforcement, for any general task that touches C/C++ source

Both agents are registered as `mode: subagent`, so in OpenCode they are only
invoked by `@`-mention or delegated by a primary agent — they do not appear
in the primary-agent (Tab) selector.

For an existing agent file, `fw-context init` adds only the CRITICAL block.
`fw-context init` does not change the rest of the file, so the file keeps its
custom domain knowledge.

**Project skill:** `fw-context init` installs the `fw-review` skill in
the project's `.claude/skills/` directory, alongside the global copy.

**If `fw-context init` detects no AI tool on the system**, and you give no
`--tool`, it configures Claude Code:

```
No AI assistants detected — falling back to Claude Code configuration.
```

To configure a different tool, give `--tool <id>`.

**Global install**, with `--scope global` or `--scope all`, is also available.
Global install injects instructions into `~/.claude/CLAUDE.md` and similar
files, the same way as before.

The command is **idempotent**. You can safely re-run the command after
updates. Collision detection warns you before the command overwrites
existing content.

## libclang

The `libclang-ng` package gives the libclang library and its Python
binding, in one exact version that fw-context pins. If you get
`clang.cindex` errors, run `fw-context doctor`: the `libclang-python` and
`libclang-so` checks tell the cause and the command that repairs it.

An upgrade from a fw-context that used the old `libclang` package leaves
that package installed. The two packages install the same `clang` package,
thus remove the old one and reinstall `libclang-ng` (`fw-context doctor
--fix` does both):

```bash
pip uninstall -y libclang
pip install --force-reinstall "libclang-ng==22.1.4.2"
```

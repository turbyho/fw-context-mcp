# Installation

This is the complete installation guide. This guide covers prerequisites, setup, Ollama, and AI assistant integration.

## Prerequisites

### Linux

```bash
# Ubuntu / Debian
sudo apt install python3 bear libclang-dev

# Arch / Manjaro
sudo pacman -S python bear libclang

# Fedora / RHEL
sudo dnf install python3 bear libclang-devel

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

| What | Why |
|------|-----|
| Python 3.11+ | Runtime |
| [`uv`](https://docs.astral.sh/uv/) | Fast package installer |
| Compiler toolchain (for example ARM GCC or the Zephyr SDK) | libclang needs system headers to parse cross-compiled code |
| [`bear`](https://github.com/rizsotto/Bear) | Intercepts build commands → `compile_commands.json` |
| [Ollama](https://ollama.com) *(optional)* | Powers `smart_search` and `explain_symbol`. Disable with `[llm] enabled = false` to let the AI assistant handle results. |

## Install

### pip (recommended)

```bash
pip install fw-context-mcp
# or via uv:
uv pip install fw-context-mcp
```

This command installs the latest release from PyPI with all dependencies
(libclang, pysqlite3, sqlite-vec, watchfiles, httpx, mcp, tomli-w).

The `fw-context` binary lands in your active Python environment's `bin/`
directory — typically `~/.local/bin` for user installs, or the virtual
environment's `bin/` if you use one. Ensure that directory is on your
`PATH`.

### From source

```bash
# Clone
git clone git@github.com:turbyho/fw-context-mcp.git ~/.fw-context/src
# or: git clone git@git.montyho.com:turbyho/fw-context-mcp.git ~/.fw-context/src

# Install (creates venv, installs package, symlinks binaries into ~/.local/bin)
cd ~/.fw-context/src && make install
```

**File watcher (auto-reindex on save):**

The MCP server starts a background watcher daemon automatically. This daemon reindexes an edited source file within 500 ms after you save the file. This daemon regenerates the LLM analysis after 60 s of inactivity. You do not need separate configuration.

## Configure your project

After you install fw-context, create `<project>/.fw-context/config.toml` in your firmware project. Or let `fw-context index` create this file automatically, with default values.

Two project-level files exist:

| File | Purpose | Git |
|------|---------|-----|
| `.fw-context/config.toml` | Shared settings (build, index, project name) | Commit |
| `.fw-context/local.toml` | Private overrides (LLM model, db path, analysis toggles) | Gitignore |

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
installs the core Python packages automatically: `libclang`, `pysqlite3`,
`sqlite-vec`, `watchfiles`, `httpx`, `mcp`, and `tomli-w`.

### Core (All Projects)

| Tool | Why |
|------|-----|
| Python 3.11+ | Runtime |
| `clang` (system binary) | fw-context uses this tool to resolve expanded macro values, with `clang -dM -E` |

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

> **Nordic NCS users:** `fw-context init` detects the NCS toolchain setup script
> automatically, at `~/ncs_tools/nordic_minimal_setup.sh`. `fw-context init`
> writes this script path to `.fw-context/local.toml`, as `activate`. For plain
> Zephyr, `fw-context init` checks `west config zephyr.base` and
> `~/zephyr-sdk-*/` for the environment setup script.

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
`fw-context` runs `cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON && cmake --build build`.

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
generates `compile_commands.json`.

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

## Verify

```bash
fw-context --help
fw-context status
```

## Ollama (optional)

Ollama powers natural-language search and symbol explanations.
**Ollama is optional.** Set `enabled = false` in `[llm]`. Then the AI
assistant processes the results with its own model.

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
you call `smart_search` or `explain_symbol`.

```bash
ollama serve &   # start daemon if not running as a service
```

### Pull models

```bash
# LLM — required for smart_search + explain_symbol (min. 14B parameters)
ollama pull qwen2.5-coder:14b        # recommended (~12 GB VRAM)

# Cloud alternative — no GPU needed (requires ollama signin)
ollama pull deepseek-v4-flash:cloud  # 284B MoE, 13B active, great value
ollama pull qwen3-coder:480b-cloud   # coding-focused, top quality

# Embedding model — required for vector search
ollama pull mxbai-embed-large:latest

# Verify
ollama run qwen2.5-coder:14b "Explain: void uart_init(int baudrate)"
```

### Model recommendations

**With GPU:**

| VRAM | Model | Tag | Notes |
|------|-------|-----|-------|
| 12 GB | **Qwen2.5-Coder 14B** | `qwen2.5-coder:14b` | Minimum recommended. Produces robust JSON, with good code comprehension |
| 16 GB | Qwen2.5-Coder 14B Q8 | `qwen2.5-coder:14b-q8_0` | Higher precision, slightly better quality |
| 24 GB+ | Qwen2.5-Coder 32B | `qwen2.5-coder:32b` | Best local quality for embedded C++ |

> **Do you have less than 12 GB of VRAM?** Use the cloud models below. A smaller
> local model produces inconsistent JSON, and writes poor descriptions for
> embedded C++ code.

**Without GPU (cloud models, requires `ollama signin`):**

| Model | Tag | Notes |
|-------|-----|-------|
| **DeepSeek V4 Flash** | `deepseek-v4-flash:cloud` | 284B MoE (13B active), great value |
| **Qwen3-Coder 480B** | `qwen3-coder:480b-cloud` | Coding-focused, best analysis quality |
| Gemini 3 Flash | `gemini-3-flash-preview:cloud` | Fast, affordable |

### Configure

Edit `~/.fw-context/config.toml` (global) or
`<project>/.fw-context/local.toml` (per-project override):

```toml
[llm]
enabled      = true
model        = "qwen2.5-coder:14b"
embed_model  = "mxbai-embed-large:latest"
ollama_url   = "http://localhost:11434"
num_ctx      = 16384
```

> **Remote Ollama:** Set `ollama_url = "http://192.168.1.50:11434"` if
> Ollama runs on another machine.
> **Per-project model:** Use `<project>/.fw-context/local.toml` to override
> the model for a specific project, for example a 32B model for a large codebase.

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

1. Generate the project ID and register the project globally.
2. Audit the dependencies, and auto-fix what it can (missing pip packages).
   Model pulls (`ollama pull`) never run here.
3. Detect the build system and generate `compile_commands.json` when the
   project is buildable.
4. Register the AI tools (steps skipped by `--quick`).
5. Print a checklist of the remaining manual steps.

When build-system detection fails, `fw-context init` asks for the missing
value interactively (build system, board, FQBN, and so on). In a pipe, or
with `--non-interactive`, the missing value goes to the checklist instead.

**What `fw-context init` does:**

By default, with `--scope project`, `fw-context init` configures only the current project:

| Tool | Detection | Action |
|------|-----------|--------|
| **Claude Code** | `.claude/` dir in project | Adds `<!-- fw-context -->` to `CLAUDE.md`. Installs the `code-explorer` and `general-purpose` agents. Installs the `fw-review` skill. |
| **OpenCode** | `.opencode/` dir in project | Writes the rules file. Installs the skill. |

**Project agents:** `fw-context init` creates two agent definitions in `.claude/agents/`:
- `code-explorer`: this agent includes a `CRITICAL — C/C++ source access` block. This block enforces fw-context for all C/C++ code reading
- `general-purpose`: this agent has the same enforcement, for any general task that touches C/C++ source

For an existing agent file, `fw-context init` adds only the CRITICAL block.
`fw-context init` does not change the rest of the file, so the file keeps its
custom domain knowledge.

**Project skill:** `fw-context init` installs the `fw-review` skill in
the project's `.claude/skills/` directory, alongside the global copy.

**If `fw-context init` detects no AI tool in the project** (no `.claude/` or
`.opencode/` directory, for example), `fw-context init` prints instructions.
`fw-context init` does not fall back to a global install:

```
No AI assistant detected in this project.

Run an AI assistant (Claude Code, OpenCode, etc.) in this project
directory first — it will create its config directory. Then re-run
'fw-context init'.

Alternatively, use --scope global to install fw-context for all
projects, or --tool to target a specific assistant.
```

**Global install**, with `--scope global` or `--scope all`, is also available.
Global install injects instructions into `~/.claude/CLAUDE.md` and similar
files, the same way as before.

The command is **idempotent**. You can safely re-run the command after
updates. Collision detection warns you before the command overwrites
existing content.

## libclang

If you get `clang.cindex` errors:

```bash
# Arch / Manjaro
sudo pacman -S libclang

# Ubuntu / Debian
sudo apt install libclang-dev

# Fedora / RHEL
sudo dnf install libclang-devel

# macOS
brew install llvm
echo 'export PATH="/opt/homebrew/opt/llvm/bin:$PATH"' >> ~/.zshrc
```

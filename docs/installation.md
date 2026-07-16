# Installation

Complete installation guide — prerequisites, setup, Ollama, AI assistant integration.

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
| Compiler toolchain (ARM GCC / Zephyr SDK / …) | libclang needs system headers to parse cross-compiled code |
| [`bear`](https://github.com/rizsotto/Bear) | Intercepts build commands → `compile_commands.json` |
| [Ollama](https://ollama.com) *(optional)* | Powers `smart_search` and `explain_symbol`. Disable with `[llm] enabled = false` to let the AI assistant handle results. |

## Install

```bash
# Clone
git clone git@github.com:turbyho/fw-context-mcp.git ~/.fw-context/src
# or: git clone git@git.montyho.com:turbyho/fw-context-mcp.git ~/.fw-context/src

# Install (creates venv, installs package, symlinks binaries into ~/.local/bin)
cd ~/.fw-context/src && make install
```

**File watcher (auto-reindex on save):**

The MCP server starts a background watcher daemon automatically — edited
source files are reindexed within 500 ms of saving, and LLM analysis
regenerates after 60 s of inactivity. No separate configuration needed.

## Configure your project

After install, create `<project>/.fw-context/config.toml` in your firmware
project — or let `fw-context index` auto-create it with defaults.

Two project-level files exist:

| File | Purpose | Git |
|------|---------|-----|
| `.fw-context/config.toml` | Shared settings (build, index, project name) | Commit |
| `.fw-context/local.toml` | Private overrides (LLM model, db path, analysis toggles) | Gitignore |

Full reference: **[Configuration →](configuration.md)**.

### Zephyr

```toml
[build]
board = "nrf52840dk_nrf52840"    # required
# clean = true                   # pristine build (recommended)
```

### PlatformIO / Arduino

```toml
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

> **`defines`** are passed as `-D` flags to the compiler. Use them for version
> numbers and feature toggles — ensures `#ifdef DEV` / `#if VERSION_FW_MAJOR`
> branches are indexed correctly.

## Dependencies by Build System

Install only what your build system needs. Core Python packages (`libclang`,
`pysqlite3`, `sqlite-vec`, `watchfiles`, `httpx`, `mcp`) are installed
automatically with `pip install fw-context-mcp`.

### Core (All Projects)

| Tool | Why |
|------|-----|
| Python 3.11+ | Runtime |
| `clang` (system binary) | Resolves expanded macro values (`clang -dM -E`) |

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
| **west** | Build orchestration | Bundled with Zephyr SDK — [Getting Started](https://docs.zephyrproject.org/latest/develop/getting_started/index.html) |
| Zephyr SDK | Toolchain + cmake + host tools | Follow Zephyr install guide |

`fw-context` runs `west build -b <board>`. `CMAKE_EXPORT_COMPILE_COMMANDS=ON`
is enabled automatically.

### PlatformIO

| Tool | Why | Install |
|------|-----|---------|
| **pio** / **platformio** | Build + compile_commands.json | `pip install platformio` |

`fw-context` runs `pio run --target compiledb` plus a real build for `.d` files.

### Mbed OS

| Tool | Why | Install |
|------|-----|---------|
| **bear** | Intercepts build → compile_commands.json | `sudo pacman -S bear` / `sudo apt install bear` / `sudo dnf install bear` / `brew install bear` |
| **mbed** CLI | `mbed compile` | `pip install mbed-cli` |
| ARM GCC | Cross-compiler toolchain | [ARM GNU Toolchain](https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads) |

`fw-context` runs `bear --output compile_commands.json -- mbed compile -t GCC_ARM -m <target>`.

### Arduino CLI

| Tool | Why | Install |
|------|-----|---------|
| **arduino-cli** | Compile + compile_commands.json | `curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh \| sh` |

Requires `fqbn` (fully qualified board name) in `.fw-context/config.toml`:
`fqbn = "arduino:avr:uno"`.

`fw-context` runs `arduino-cli compile --export-compile-commands` (two passes:
database only, then real compile for object files).  Header dependency tracking
uses ``manifest.json`` (built from libclang token stream) — no ``.d`` files needed.

### ESP-IDF

| Tool | Why | Install |
|------|-----|---------|
| **idf.py** | Build wrapper | Bundled with [ESP-IDF](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/get-started/) |
| cmake | Build system | Bundled with ESP-IDF |

`fw-context` runs `idf.py build`. Requires the ESP-IDF environment
(`. ./export.sh` or `idf.py` on PATH).

### Generic CMake

| Tool | Why | Install |
|------|-----|---------|
| **cmake** | Build + compile_commands.json | `sudo pacman -S cmake` / `sudo apt install cmake` / `sudo dnf install cmake` / `brew install cmake` |

For any CMake project that is not Zephyr or ESP-IDF. `fw-context` runs
`cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON && cmake --build build`.

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

**No Keil installation needed** — `keil2clangd` parses the XML project file statically.

### IAR EWARM

| Tool | Why | Install |
|------|-----|---------|
| **keil2clangd** | Converts `.ewp` → compile_commands.json | `pip install keil2clangd` |

**No IAR installation needed** — `keil2clangd` parses the XML project file statically.

### Manual (Bare) Mode

| Tool | Why | Install |
|------|-----|---------|
| **gcc** (or any C/C++ compiler) | Syntax-only compilation for `.d` files | `sudo pacman -S gcc` / `sudo apt install gcc` / `sudo dnf install gcc` / `brew install gcc` (or Xcode CLT clang) |

No build system required. Configure `source_dirs`, `include_dirs`, and `defines`
in `.fw-context/config.toml`. `fw-context` scans sources and generates
`compile_commands.json`.

### STM32CubeIDE (Manual Setup)

No CLI tools. Enable the built-in JSON Compilation Database generator:

**Project Properties → C/C++ Build → Builder Settings → ✓ Generate JSON Compilation Database**

Then run `fw-context index compile_commands.json` with the generated file.

### TI Code Composer Studio (Manual Setup)

No CLI tools. Enable JSON Compilation Database in Project Settings, or use `bear`:

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
**It is optional** — set `enabled = false` in `[llm]` and the AI assistant
processes results with its own model.

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

Ollama runs a daemon on `http://localhost:11434`. It must be running when
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
| 12 GB | **Qwen2.5-Coder 14B** | `qwen2.5-coder:14b` | Minimum recommended — robust JSON, good code comprehension |
| 16 GB | Qwen2.5-Coder 14B Q8 | `qwen2.5-coder:14b-q8_0` | Higher precision, slightly better quality |
| 24 GB+ | Qwen2.5-Coder 32B | `qwen2.5-coder:32b` | Best local quality for embedded C++ |

> **Less than 12 GB VRAM?** Use cloud models (below) — smaller local models
> produce inconsistent JSON and poor-quality descriptions for C++ embedded code.

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
> the model for a specific project (e.g. a 32B model for a large codebase).

## AI assistant setup

```bash
fw-context init                    # detect & configure current project (default)
fw-context init --list-tools       # show what's supported and detected
fw-context init --dry-run          # preview without writing
fw-context init --tool claude-code # specific tool only
fw-context init --scope global     # global install (all projects)
fw-context init --scope all        # both global and project
```

**What `fw-context init` does:**

By default (`--scope project`), it configures ONLY the current project:

| Tool | Detection | Action |
|------|-----------|--------|
| **Claude Code** | `.claude/` dir in project | Injects `<!-- fw-context -->` into `CLAUDE.md`, installs agents (`code-explorer`, `general-purpose`), installs `fw-review` skill |
| **OpenCode** | `.opencode/` dir in project | Writes rules file, installs skill |

**Project agents:** Two agent definitions are created in `.claude/agents/`:
- `code-explorer` — includes a `CRITICAL — C/C++ source access` block that
  enforces fw-context for all C/C++ code reading
- `general-purpose` — same enforcement for any general task that touches
  C/C++ source

For existing agents, the CRITICAL block is injected without touching the
rest of the file — custom domain knowledge is preserved.

**Project skill:** The `fw-review` skill is installed in
the project's `.claude/skills/` directory alongside the global copy.

**If no AI tool is detected in the project** (no `.claude/`, `.opencode/`,
etc. directory), `fw-context init` prints instructions instead of falling
back to a global install:

```
No AI assistant detected in this project.

Run an AI assistant (Claude Code, OpenCode, etc.) in this project
directory first — it will create its config directory. Then re-run
'fw-context init'.

Alternatively, use --scope global to install fw-context for all
projects, or --tool to target a specific assistant.
```

**Global install** (`--scope global` or `--scope all`) is still available
and works as before — injects instructions into `~/.claude/CLAUDE.md` etc.

The command is **idempotent** — safe to re-run after updates.
Collision detection warns before overwriting existing content.

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

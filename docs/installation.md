# Installation

Complete installation guide — prerequisites, setup, Ollama, AI assistant integration.

## Prerequisites

### Linux

```bash
# Ubuntu / Debian
sudo apt install python3 bear libclang-dev

# Arch / Manjaro
sudo pacman -S python bear libclang

# uv
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

The MCP server starts a background watcher daemon automatically. Edited
source files are reindexed on save, and LLM analysis regenerates after a
period of inactivity.

## Configure your project

After install, create `<project>/.fw-context/config.toml` in your firmware
project — or let `fw-context index` auto-create it with defaults.
Full reference: **[Configuration →](configuration.md)**.

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
# PlatformIO frameworks live outside the project root:
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
source_roots = []
exclude_paths = ["build", "BUILD"]
```

> **`defines`** are passed as `-D` flags to the compiler. Use them for version
> numbers and feature toggles — ensures `#ifdef DEV` / `#if VERSION_FW_MAJOR`
> branches are indexed correctly.

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
# Linux / macOS
curl -fsSL https://ollama.com/install.sh | sh

# Arch / Manjaro
yay -S ollama

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

Edit `~/.fw-context/config.toml`:

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

## AI assistant setup

```bash
fw-context init                    # all detected assistants
fw-context init --list-tools       # show what's supported and detected
fw-context init --dry-run          # preview without writing
fw-context init --tool claude-code # specific tool only
```

**What `fw-context init` does:**

| Tool | Action |
|------|--------|
| **Claude Code** | Registers MCP server (`claude mcp add`), injects instructions into `~/.claude/CLAUDE.md` |
| **OpenCode** | Writes `~/.config/opencode/rules/fw-context.md` |
| **Kilo Code** | Inherits from Claude Code automatically |
| **Codex** | Writes `~/.codex/rules/fw-context.md` |
| **Cursor** | Writes `.cursor/rules/fw-context.mdc` (project-scoped) |

The command is **idempotent** — safe to re-run after updates.
Collision detection warns before overwriting existing content.

## libclang

If you get `clang.cindex` errors:

```bash
# Ubuntu / Debian
sudo apt install libclang-dev

# macOS
brew install llvm
echo 'export PATH="/opt/homebrew/opt/llvm/bin:$PATH"' >> ~/.zshrc
```

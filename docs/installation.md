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

### Linux 🐧

```bash
# Clone
git clone git@github.com:turbyho/fw-context-mcp.git ~/.fw-context/src
# or: git clone git@git.montyho.com:turbyho/fw-context-mcp.git ~/.fw-context/src

# Create venv + install
uv venv ~/.fw-context/.venv --python 3.12
uv pip install --python ~/.fw-context/.venv/bin/python ~/.fw-context/src/

# Add to PATH
echo 'export PATH="$HOME/.fw-context/.venv/bin:$PATH"' >> ~/.zshrc
```

### macOS 🍎

```bash
# Clone
git clone git@github.com:turbyho/fw-context-mcp.git ~/.fw-context/src
# or: git clone git@git.montyho.com:turbyho/fw-context-mcp.git ~/.fw-context/src

# Create venv + install
uv venv ~/.fw-context/.venv --python 3.12
uv pip install --python ~/.fw-context/.venv/bin/python ~/.fw-context/src/

# Add to PATH
echo 'export PATH="$HOME/.fw-context/.venv/bin:$PATH"' >> ~/.zshrc
```

**Optional dependencies:**

```bash
# File watcher for fw-context watch
uv pip install --python ~/.fw-context/.venv/bin/python "fw-context-mcp[watch]"
```

## Update

### Linux 🐧

```bash
cd ~/.fw-context/src
git pull
uv pip install --reinstall --python ~/.fw-context/.venv/bin/python ~/.fw-context/src/
```

### macOS 🍎

```bash
cd ~/.fw-context/src
git pull
uv pip install --reinstall --python ~/.fw-context/.venv/bin/python ~/.fw-context/src/
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
# LLM — required for smart_search + explain_symbol
ollama pull qwen2.5-coder:14b        # recommended (~9 GB VRAM)
ollama pull qwen2.5-coder:7b         # smaller (~4 GB VRAM)

# Embedding model — required for vector search
ollama pull mxbai-embed-large:latest

# Verify
ollama run qwen2.5-coder:14b "Explain: void uart_init(int baudrate)"
```

### Model recommendations

**With GPU:**

| VRAM | Model | Tag |
|------|-------|-----|
| 8 GB | Qwen2.5-Coder 7B | `qwen2.5-coder:7b` |
| 12 GB | **Qwen2.5-Coder 14B** | `qwen2.5-coder:14b` |
| 16 GB | Qwen2.5-Coder 14B Q8 | `qwen2.5-coder:14b-q8_0` |
| 24 GB+ | Qwen2.5-Coder 32B | `qwen2.5-coder:32b` |

**Without GPU (cloud models, requires `ollama signin`):**

| Model | Tag | Notes |
|-------|-----|-------|
| **Nemotron-3 Nano** | `nemotron-3-nano:cloud` | 4B, fast, free tier |
| RnJ-1 | `rnj-1:cloud` | 8B, code/STEM optimized |

### Configure

Edit `~/.fw-context/config.toml`:

```toml
[llm]
enabled      = true
model        = "qwen2.5-coder:14b"
embed_model  = "mxbai-embed-large:latest"
ollama_url   = "http://localhost:11434"
num_ctx      = 8192
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

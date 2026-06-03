# Installation

Complete installation guide for fw-context — prerequisites, setup, Ollama
integration, and AI assistant configuration.

## Prerequisites

| What | Why |
|------|-----|
| Python 3.11+ | Runtime |
| [`uv`](https://docs.astral.sh/uv/) | Fast package installer |
| Compiler toolchain (ARM GCC / Zephyr SDK / PlatformIO) | libclang needs system headers to parse cross-compiled code |
| [`bear`](https://github.com/rizsotto/Bear) | Intercepts build commands to produce `compile_commands.json` |
| [Ollama](https://ollama.com) *(optional)* | Powers `explain_symbol` and `smart_search`. Not required —                  when disabled, the AI assistant processes the results itself. |


## Installation

### Clone from repository

```bash
# Clone to ~/.fw-context/src (or any location you prefer)
git clone git@github.com:turbyho/fw-context-mcp.git ~/.fw-context/src
# or from the primary server:
# git clone https://git.montyho.com/turbyho/fw-context-mcp.git ~/.fw-context/src

# Create a dedicated virtual environment
uv venv ~/.fw-context/.venv --python 3.12

# Install from the cloned source
uv pip install --python ~/.fw-context/.venv/bin/python ~/.fw-context/src/

# Make fw-context available in your shell
export PATH="$HOME/.fw-context/.venv/bin:$PATH"
# Add to ~/.bashrc or ~/.zshrc for persistence
```

### Upgrade

Pull the latest source and re-install:

```bash
cd ~/.fw-context/src
git pull
uv pip install --python ~/.fw-context/.venv/bin/python ~/.fw-context/src/
```

### Install from local path

If you already have the source elsewhere:

```bash
uv pip install --python ~/.fw-context/.venv/bin/python /path/to/fw-context-mcp/
```


## Installing Ollama (optional)

Ollama powers `smart_search` (natural-language → FTS5 keywords, Czech/non-ASCII
query translation) and `explain_symbol` (plain-English function explanations).
It is **optional** — set `enabled = false` in `[llm]` config and the AI
assistant processes results with its own LLM instead.

### Install Ollama

```bash
# Linux / macOS
curl -fsSL https://ollama.com/install.sh | sh

# Or via package manager on Arch/Manjaro:
# yay -S ollama
# pacman -S ollama

# Verify it runs
ollama --version
```

Ollama starts a local daemon on `http://localhost:11434`. The daemon must be
running whenever fw-context calls `smart_search` or `explain_symbol`.

```bash
# Start the daemon (if not started automatically as a service)
ollama serve &
```

### Pull a model

Pick one model based on available VRAM. The default config expects
`qwen2.5-coder:14b` (proven in testing), but any code-oriented model works.

```bash
# Recommended: good balance of quality and speed (~9 GB VRAM)
ollama pull qwen2.5-coder:14b

# Smaller option (~4 GB VRAM)
ollama pull qwen2.5-coder:7b

# Semantic embedding model (~500 MB) — used for similarity search
ollama pull mxbai-embed-large:latest

# If you have no local GPU — use a cloud model (requires ollama signin)
ollama signin
ollama pull nemotron-3-nano:cloud   # free tier, 4B
```

### Verify the model works

```bash
# Quick smoke test — should return a short explanation
ollama run qwen2.5-coder:14b "In one sentence: what does void uart_init(int baud) do?"

# Or use fw-context's built-in check:
fw-context status         # shows Ollama availability and configured model
```

### Configure fw-context to use the model

Edit `~/.fw-context/config.toml` (global) or `.fw-context/config.toml` (project):

```toml
[llm]
enabled      = true
model        = "qwen2.5-coder:14b"          # LLM for translation/search/explain
embed_model  = "mxbai-embed-large:latest"   # embedding model for semantic search
ollama_url   = "http://localhost:11434"
num_ctx      = 8192   # keep ≥ 8192 — factory default (2048) is too small
```

> **Remote Ollama:** If Ollama runs on another machine (e.g. a GPU server),
> set `ollama_url = "http://192.168.1.50:11434"`. Everything else stays the same.

See [Choosing an Ollama model](#choosing-an-ollama-model) for a full comparison
of local and cloud model options.


## Choosing an Ollama model

`explain_symbol` and `smart_search` use a local Ollama model by default.
**Ollama is optional** — if you don't have it, set `enabled = false` in config
and the AI assistant will process the results with its own LLM.

The tasks are lightweight — generating FTS5 search terms and writing 2–4
sentence symbol explanations. An 8B code-optimized model is plenty; you don't
need a 24B+ model for this.

All models are accessed through the same Ollama API — the config only changes
the model name. Cloud models require `ollama signin` first.

### Local models (with GPU)

For subagent tasks the priority is low latency, not maximum quality. Start with
the smallest model that fits your VRAM and upgrade only if the results are poor.

| VRAM | Model | Ollama tag | Notes |
|------|-------|-----------|-------|
| 8 GB | Qwen2.5-Coder 7B | `qwen2.5-coder:7b-instruct-q8_0` | Good baseline |
| 12 GB | **Qwen2.5-Coder 14B** | `qwen2.5-coder:14b-q4_K_M` | **Recommended for RTX 4070 12 GB** |
| 12 GB | DeepSeek-Coder-V2 Lite 16B | `deepseek-coder-v2:16b-lite-q4_K_M` | Alternative |
| 16 GB | Qwen2.5-Coder 14B Q8 | `qwen2.5-coder:14b-q8_0` | Higher precision |
| 24 GB+ | Qwen2.5-Coder 32B | `qwen2.5-coder:32b-q4_K_M` | Maximum quality |

### Cloud models (no GPU)

Ollama v0.12+ supports cloud-hosted models. They run on ollama.com
infrastructure but are accessed through your local Ollama daemon.

Browse the full catalog at [ollama.com/search?c=cloud](https://ollama.com/search?c=cloud).
Requires: `ollama signin`.

For fw-context, start with the smallest model — generating search terms and
short explanations needs little reasoning depth. Sorted smallest → largest:

| Model | Ollama tag | Size | Notes |
|-------|-----------|------|-------|
| **Nemotron-3 Nano** | `nemotron-3-nano:cloud` | 4B | **Recommended smallest** — agentic, tools-enabled, efficient |
| **RnJ-1** | `rnj-1:cloud` | 8B | Code/STEM optimized, great for search + explanations |
| Qwen3-Coder-Next | `qwen3-coder-next:cloud` | — | Coding-focused, agentic workflows |
| DeepSeek V4 Flash | `deepseek-v4-flash:cloud` | MoE (13B active) | Fast, good price/performance |
| Qwen3.5 | `qwen3.5:cloud` | 9B tag available | General-purpose, strong tool use |
| Devstral Small 2 | `devstral-small-2:cloud` | 24B | Code exploration and multi-file editing |

> **Tip:** Use `ollama pull <tag>` to download, then test with
> `ollama run <tag> "explain: void uart_init(int baudrate)"` to gauge
> latency and quality before switching.

### Context window

The default `num_ctx` is **8192**. Ollama's factory default (2048) is too small
for source code context — keep 8192 or higher in your config:

```toml
[llm]
ollama_url = "http://localhost:11434"      # default, change for remote Ollama
model = "nemotron-3-nano:cloud"
num_ctx = 8192
```

### Recommendation

| Setup | Model | Why |
|-------|-------|-----|
| GPU, 8–12 GB VRAM | `qwen2.5-coder:14b-q4_K_M` | Best quality for local subagent tasks |
| No GPU — smallest | `nemotron-3-nano:cloud` | 4B, agentic, fast — plenty for search terms + short explanations |
| No GPU — code focused | `rnj-1:cloud` | 8B, code/STEM optimized |
| Need deeper code analysis | `devstral-small-2:cloud` | 24B, better for complex reasoning about code |

## AI assistant setup

Run `fw-context init` for automatic registration (Claude Code + OpenCode).
For manual setup or other assistants, see **[README-MCP.md](../README-MCP.md#integration)**.


## Setting up AI assistant integration

Run this once:

```bash
fw-context init
```

It registers the MCP server globally in Claude Code, inserts usage instructions
into `~/.claude/CLAUDE.md`, and writes rules for OpenCode. The command is
idempotent — safe to re-run after updates.

For manual setup or other assistants, see [AI assistant setup](#ai-assistant-setup).


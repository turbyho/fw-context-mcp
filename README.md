# fw-context

Build-aware code intelligence for embedded firmware. Lets your AI assistant
(Claude Code, OpenCode) understand C/C++ codebases built with **Mbed OS**,
**Zephyr**, or **PlatformIO** — without grep, without reading entire files,
without hallucinating symbols that don't exist.

It works in two parts:

1. **Indexer** — parses your `compile_commands.json` via libclang, extracts every
   function, class, method, enum, and typedef, stores them in a local SQLite
   database with full-text search.
2. **MCP server** — exposes the index as tools that AI assistants call to
   look up definitions, search for symbols, and even ask a local LLM to explain
   what a function does.

## Quick start

```bash
# 1. Clone and install
git clone git@git.montyho.com:turbyho/fw-context-mcp.git ~/.fw-context/src
uv venv ~/.fw-context/.venv --python 3.12
uv pip install --python ~/.fw-context/.venv/bin/python ~/.fw-context/src/
echo 'export PATH="$HOME/.fw-context/.venv/bin:$PATH"' >> ~/.bashrc

# 2. Register with your AI assistant
fw-context init

# 3. Generate compile_commands.json, then index
cd your-firmware-project
bear -- python3 build_app.py --profile release     # Mbed OS
fw-context index

# Done. Restart your AI assistant and start asking it about your code.
```

## Prerequisites

| What | Why |
|------|-----|
| Python 3.11+ | Runtime |
| [`uv`](https://docs.astral.sh/uv/) | Fast package installer |
| SSH key for `git.montyho.com` | To clone the repository |
| Compiler toolchain (ARM GCC / Zephyr SDK / PlatformIO) | libclang needs system headers to parse cross-compiled code |
| [`bear`](https://github.com/rizsotto/Bear) | Intercepts build commands to produce `compile_commands.json` |
| [Ollama](https://ollama.com) *(optional)* | Powers `explain_symbol` and `smart_search` |

## Installation

### Clone from repository

```bash
# Clone to ~/.fw-context/src (or any location you prefer)
git clone git@git.montyho.com:turbyho/fw-context-mcp.git ~/.fw-context/src

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

## Setting up AI assistant integration

Run this once:

```bash
fw-context init
```

It registers the MCP server globally in Claude Code, inserts usage instructions
into `~/.claude/CLAUDE.md`, and writes rules for OpenCode. The command is
idempotent — safe to re-run after updates.

For manual setup or other assistants, see [AI assistant setup](#ai-assistant-setup).

## Day-to-day workflow

```
compile_commands.json  ──→  fw-context index  ──→  AI assistant tools
        ↑                                              │
   bear / west / pio                                   │
        │                                      lookup · search · explain
   your build system
```

### 1. Generate `compile_commands.json`

**Mbed OS:**
```bash
bear -- python3 build_app.py --profile release --type DEV
```

**Zephyr:**
```bash
west build -b <board> -- -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
fw-context index build/compile_commands.json
```

**PlatformIO:**
```bash
pio run --target compiledb
```

### 2. Index the project

Run from the project root:

```bash
fw-context index
```

First run takes a few seconds to minutes depending on project size (8&hairsp;500
symbols across 950 files takes ~15&hairsp;s). Subsequent runs are
**incremental** — only files with changed modification time are re-parsed.

```bash
# Custom source roots (override .fw-context/config.toml)
fw-context index --source-roots src lib vendor

# Index a different project
fw-context index --project /path/to/other/project

# Verbose output — shows per-file progress
fw-context index -v
```

### 3. Use with your AI assistant

Once indexed, your assistant can:

- *"What does `modem_parser_oob_init` do?"* → `lookup_symbol` + `explain_symbol`
- *"Find functions related to BLE advertising"* → `search_code` or `smart_search`
- *"Is the index up to date?"* → `get_active_build`
- *"I changed `main.cpp`, re-index it"* → `reindex_file`

### 4. Keep the index current

After editing source files, the index stays accurate in two ways:

- **Automatic staleness detection** — if `compile_commands.json` or individual
  source files have changed since the last index, tools return a warning.
- **On-demand re-index** — run `fw-context index` (incremental, fast) or use
  `reindex_file("src/main.cpp")` via the MCP tool.

```bash
# Check if the index is stale
fw-context status

# Re-index changed files only
fw-context index

# Fresh start (after toolchain change, compiler upgrade, etc.)
fw-context reset
fw-context index
```

## CLI reference

### `fw-context index`

Build or update the symbol index.

```
fw-context index [compile_commands.json] [--project DIR] [--source-roots DIR ...] [--name NAME] [-v]
```

| Option | Default | Description |
|--------|---------|-------------|
| `compile_commands.json` | from config | Path to the compilation database |
| `--project DIR` | `.` | Project root directory |
| `--source-roots DIR...` | `src lib` | Directories to index symbols from |
| `--name NAME` | directory name | Project name override |
| `-v` | off | Verbose progress output |

### `fw-context search`

Full-text search over indexed symbols. Supports FTS5 syntax.

```bash
fw-context search "modem_init"
fw-context search "set_key" --limit 5
fw-context search "spi*"              # prefix match
fw-context search '"spi init"'        # phrase match
```

### `fw-context status`

Show index freshness and statistics for the current project.

```bash
$ fw-context status
Project : /home/user/firmware
Symbols : 8586  files=952
Indexed : 2026-05-12 09:35:18
DB      : ~/.fw-context/index/452361ffbf84f774/index.db
```

If `compile_commands.json` changed since the last index, you'll see `[STALE]`.

### `fw-context list`

List all indexed projects.

```bash
$ fw-context list
zbox-ecb-fw  /home/user/zbox-ecb-fw
  symbols=8586  files=952  indexed=2026-05-12 09:35:18
```

### `fw-context reset`

Delete the index for a project (asks for confirmation).

```bash
fw-context reset                  # interactive confirmation
fw-context reset -y               # skip confirmation
fw-context reset --project /path  # specific project
```

### `fw-context init`

Register fw-context with Claude Code and OpenCode (see [Setup](#setting-up-ai-assistant-integration)).

## MCP tools

These tools are called by your AI assistant. See **[README-MCP.md](README-MCP.md)**
for the full JSON-RPC protocol reference, input/output schemas, error handling,
and integration details.

| Category | Tool | Purpose |
|----------|------|---------|
| Search | `search_code` | Keyword search (FTS5) |
| Search | `lookup_symbol` | Find symbol by name |
| Search | `smart_search` | Natural language → search (Ollama) |
| Understanding | `explain_symbol` | LLM explanation of a symbol (10–30 s) |
| Understanding | `get_active_build` | Check index freshness |
| Maintenance | `reindex_file` | Re-index one file |
| Maintenance | `reset_index` | Delete and rebuild index |
| Maintenance | `list_projects` | List all indexed projects |
| Maintenance | `check_ollama` | Verify Ollama availability |

## Configuration

### Global defaults (`~/.fw-context/config.toml`)

Auto-created on first run:

```toml
[index]
db_dir = "~/.fw-context/index"

[llm]
ollama_url = "http://localhost:11434"
model = "codestral:latest"
num_ctx = 8192
```

Change `ollama_url` if Ollama runs on a different machine:

```toml
[llm]
ollama_url = "http://192.168.1.50:11434"   # remote Ollama instance
```

### Per-project config (`.fw-context/config.toml`)

Place in your project root to override defaults:

```toml
[project]
name = "my-firmware"

[index]
compile_commands = "compile_commands.json"
source_roots = ["src", "lib"]
exclude_paths = ["lib/zcbor/generation", "build"]

[llm]
model = "deepseek-coder:6.7b"   # use a different model for this project
```

`source_roots` controls which files' symbols get indexed — only symbols whose
defining file lives under one of these directories. `exclude_paths` filters out
generated code, third-party sources, etc.

## Choosing an Ollama model

`explain_symbol` and `smart_search` use a local Ollama model. The tasks are
lightweight — generating FTS5 search terms and writing 2–4 sentence symbol
explanations. An 8B code-optimized model is plenty; you don't need a 24B+ model
for this.

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
For manual setup or other assistants, see **[README-MCP.md](README-MCP.md#integration)**.

## Troubleshooting

### "No index found"

Run `fw-context index` from the project root first. The index is per-project and
stored under `~/.fw-context/index/`.

### Index is stale

Run `fw-context index` — it's incremental and picks up only changed files.

### Symbols missing or incomplete

Check your `.fw-context/config.toml` — `source_roots` must include the
directory where the symbol's file lives. Also make sure the file is listed in
`compile_commands.json`.

### "Cannot connect to Ollama"

Ollama is optional. Tools that need it (`explain_symbol`, `smart_search`) will
return a warning and fall back to direct search. Install from
[ollama.com](https://ollama.com) and pull a model:

```bash
ollama pull codestral:latest
```

Then verify:
```bash
fw-context check_ollama
```

### clang.cindex error / libclang not found

```bash
# Ubuntu/Debian
sudo apt install libclang-dev

# macOS
brew install llvm
echo 'export PATH="/opt/homebrew/opt/llvm/bin:$PATH"' >> ~/.zshrc
```

### "No module named fw_context_mcp" after install

Make sure `~/.fw-context/.venv/bin` is in your `PATH`:

```bash
export PATH="$HOME/.fw-context/.venv/bin:$PATH"
```

### Reset after toolchain upgrade

Delete the old index and rebuild:

```bash
fw-context reset -y
fw-context index
```

## How it works

```
compile_commands.json
        │
        ▼
  ┌─────────────┐     ┌──────────────┐
  │ config_hash  │     │ libclang     │
  │ (detects     │     │ per-TU parse │
  │  changes)    │     └──────┬───────┘
  └─────────────┘            │
        │              Symbol records
        ▼                    │
  ┌──────────────────────────▼─────┐
  │         SQLite + FTS5          │
  │  ~/.fw-context/index/<id>/     │
  │        index.db                │
  └────────────┬───────────────────┘
               │
     ┌─────────▼──────────┐
     │   MCP server (stdio)│
     │   search_code       │
     │   lookup_symbol     │
     │   ...               │
     └─────────┬───────────┘
               │
     ┌─────────▼──────────┐
     │   AI assistant      │
     │   (Claude / OpenCode)│
     └────────────────────┘
```

## Directory layout

```
~/.fw-context/
├── config.toml          # global defaults (LLM model, db path)
├── .venv/               # Python virtual environment
│   └── bin/
│       ├── fw-context       # CLI entry point
│       └── fw-context-mcp   # MCP server entry point
└── index/
    └── <project-id>/
        └── index.db     # SQLite database with FTS5 index
```

Project-level:

```
your-firmware/
├── .fw-context/
│   └── config.toml      # per-project overrides (source roots, excludes)
└── compile_commands.json
```

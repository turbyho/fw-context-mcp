# fw-context-mcp

Local build-aware code intelligence MCP server for embedded projects (Mbed OS, Zephyr).

Indexes only the code that is actually compiled for a given build configuration —
no false positives from inactive `#ifdef` branches, wrong board targets, or
irrelevant platform code.

## How it works

```
local build script (Bear + mbed/west)
  → compile_commands.json
  → indexer (libclang, SQLite/FTS5)
  → ~/.fw-context/index/<project_id>/<config_hash>/
  → MCP server
  → Claude Code / OpenCode
```

## Requirements

- Python 3.11+
- libclang (matching your system clang version)
- Ollama (optional, for LLM subagent)
- Bear (for Mbed OS compile_commands.json generation)

## Installation

```bash
pip install -e .
```

## Configuration

**Global** (`~/.fw-context/config.toml`):
```toml
[llm]
model = "devstral-small-2:cloud"
base_url = "http://localhost:11434/v1"
api_key = ""
```

**Project** (`.fw-context/config.toml` in your firmware repo):
```toml
framework = "mbed"
target = "CUSTOM_NRF52840"
toolchain = "GCC_ARM"
profile = "release"
build_dir = "build/mbed"
```

## Usage

```bash
# Index your project (first time or after build config change)
fw-context index

# Start MCP server
fw-context-mcp
```

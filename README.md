# fw-context
<!-- mcp-name: io.github.turbyho/fw-context-mcp -->
mcp-name: io.github.turbyho/fw-context-mcp

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![MCP](https://img.shields.io/badge/MCP-2024--11--05-green.svg)](https://modelcontextprotocol.io)
[![License](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-135%20passed-brightgreen.svg)](tests/)
[![Glama](https://glama.ai/mcp/servers/turbyho/fw-context-mcp/badges/score.svg)](https://glama.ai/mcp/servers/turbyho/fw-context-mcp)

**MCP server for embedded C/C++ firmware** — gives AI assistants (Claude Code,
Cursor, OpenCode, etc.) real understanding of your codebase. Parses your actual build
with [libclang](https://clang.llvm.org/), extracts every symbol, and builds a
persistent index with full-text search, call graph, and vector embeddings.

No hallucination. No grepping. No reading thousands of framework headers into context.

## What it does

Your AI assistant goes from guessing to knowing:

> *"What does `uart_init` do and who calls it?"*
> → `get_symbol_context("uart_init")` — body, callers, callees in one call.
>
> *"Find all BLE advertising functions and how they're connected."*
> → `search_code("ble advertising", kind="function")` → `find_call_path("gap_init", "start_advertising")`
>
> *"Show me the implementation of `adc_read` — not the declaration."*
> → `get_source("adc_read")` — exact body via libclang, no file reading.
>
> *"What would break if I change `spi_transfer`?"*
> → `find_all_callers_recursive("spi_transfer")` — every caller, direct and indirect.
>
> *"Give me a map of `modem_msg.cpp` before I read it."*
> → `get_file_map("src/modem_msg.cpp")` — 426 symbols grouped by kind.

**21 MCP tools** — symbol search, source reading, call-graph traversal, hotspot
analysis, dead code detection, vector search. All backed by real compiler flags
from `compile_commands.json` — `#ifdef`-aware, not grep.

## Quick start

### 1. Install

#### Prerequisites

| | Linux (apt) | macOS (brew) |
|---|---|---|
| Python 3.11+ | `sudo apt install python3` | `brew install python@3.12` |
| uv | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `brew install uv` |
| bear | `sudo apt install bear` | `brew install bear` |
| libclang | `sudo apt install libclang-dev` | `brew install llvm` |
| Ollama *(optional)* | `curl -fsSL https://ollama.com/install.sh \| sh` | `brew install ollama` |

#### Linux

```bash
git clone git@github.com:turbyho/fw-context-mcp.git ~/.fw-context/src
cd ~/.fw-context/src && make install
```

#### macOS

```bash
git clone git@github.com:turbyho/fw-context-mcp.git ~/.fw-context/src
cd ~/.fw-context/src && make install
```

### 2. Ollama (optional)

Powers `smart_search` (natural-language search) and `explain_symbol`.
Works without Ollama too — the AI assistant processes results on its own.

```bash
# Install
curl -fsSL https://ollama.com/install.sh | sh   # Linux
brew install ollama                               # macOS

# Pull models
ollama pull qwen2.5-coder:14b          # LLM (~9 GB VRAM, or :7b for 4 GB)
ollama pull mxbai-embed-large:latest   # embedding model for vector search

# Start daemon
ollama serve &
```

### 3. Register with your AI assistant

```bash
fw-context init
```

### 4. Update

```bash
cd ~/.fw-context/src && make update
```

### 5. Index your firmware project

```bash
cd your-firmware-project

# Zephyr:
west build -b <board> -- -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
fw-context index build/compile_commands.json

# PlatformIO:
pio run --target compiledb
fw-context index

# Mbed OS:
bear -- mbed compile --profile release
fw-context index

# CMake / Make:
bear -- make
# or: bear -- cmake --build build

fw-context index
```

### 6. Restart your assistant and start asking about your code

For detailed prerequisites, Ollama setup, and AI assistant integration:
**[Installation guide →](docs/installation.md)**

## Why not just use LSP?

LSP servers (clangd, ccls) are excellent for interactive editing.
But they have limitations for AI-assisted exploration:

| Limitation | fw-context solution |
|-----------|---------------------|
| No full-text search across the codebase | FTS5 over 6 columns — find "all functions related to modem init" |
| Index dies with the server — rebuild from scratch | Persistent SQLite file — survives reboots, reads in milliseconds |
| Editor protocol, not AI protocol | MCP tools purpose-built for AI assistant workflow |
| Blind to which `#ifdef` branch is active | Uses real compiler flags from `compile_commands.json` |

Use **clangd for editing**, **fw-context for AI-assisted exploration**.

## Architecture

### Data flow

```
   BUILD                          INDEX                          QUERY
   =====                          =====                          =====
   bear / west / pio    libclang parses each TU          AI assistant calls
   cmake / make         extracts symbols + refs          MCP tools over
        │               generates embeddings            JSON-RPC (stdio)
        ▼                       │                              │
   compile_commands      SQLite db on disk               lookup_symbol(…)
   .json                 ~/.fw-context/index/            search_code(…)
                         │            │                  find_callers(…)
                         ▼            ▼                  explain_symbol(…)
                    symbols + refs   vec0                 get_symbol_context(…)
                    (FTS5 index)   (vector KNN)                 │
                                                          ▼
                                                    AI assistant answers
                                                    your question about
                                                    the code
```

### Components

```
   CLI (fw-context)            MCP server (fw-context-mcp)          Ollama (optional)
   ================            ===========================          ==================
   fw-context index            exposes 21 tools over               local LLM runtime
   fw-context export           JSON-RPC (stdio)                    HTTP :11434
   fw-context watch                  │                                  │
   fw-context status           search_code ───────────── lookup   smart_search ──▶ translates NL → FTS5 terms
   fw-context reset            lookup_symbol ─────────── prefix   explain_symbol ─▶ explains function
   fw-context init             smart_search ──────────── NL       embeddings ────▶ mxbai-embed-large
   fw-context search           get_file_map ──────────── file structure by kind
                               get_source ────────────── body
                               get_symbol_context ────── body+callers+callees
                               find_callers ──────────── direct callers
                               find_references ───────── all uses
                               find_call_path ────────── BFS in call graph
                               find_all_callers_recursive  transitive callers
                               find_callees_recursive ── transitive callees
                               find_dead_code ────────── never called
                               find_hotspots ─────────── most-called
                               find_wrapper_callers ──── wrapper→driver
                               trace_data_flow ───────── data flow paths
                               get_active_build ──────── index health
                               reindex_file ──────────── re-parse one file
                               reset_index ───────────── delete + rebuild
                               list_projects ─────────── all indexed projects
                               check_ollama ──────────── verify LLM
```

| Component | Runs as | Purpose |
|-----------|---------|---------|
| **CLI** (`fw-context`) | User command | Index, export, watch, status, reset, init, search |
| **Indexer** | Called by CLI | libclang parses every TU, stores in SQLite + FTS5 + vec0 |
| **MCP server** (`fw-context-mcp`) | Subprocess (AI assistant) | 21 tools over JSON-RPC — search, graph, source, maintenance |
| **Ollama** *(optional)* | Local daemon | NL search, symbol explanation, embedding generation |

## Key capabilities

- **Fast lookups** — FTS5 full-text search, prefix/exact symbol lookup, call-graph traversal
- **Natural-language search** — *"how does the modem connect?"* → finds `network_registration`, `modem_attach`, … (Ollama, optional)
- **Vector search** — semantic similarity via `sqlite-vec` + Ollama embeddings, hybrid FTS5+vector re-ranking
- **Graph analytics** — call paths, transitive callers/callees, dead code detection, hotspot analysis
- **Indirect call detection** — resolves function-pointer arguments at direct call sites, uncovering call-graph edges that grep/cscope miss
- **Incremental indexing** — only changed files re-parsed; auto-reindex on query detects and fixes staleness
- **Offline-first** — index is a file on disk at `~/.fw-context/index/`. No daemon, no cloud, no network.
- **`#ifdef`-aware** — uses real compiler flags; sees exactly what your compiler sees

## Supported ecosystems

Works with **any build system** that produces `compile_commands.json`:

| Ecosystem | Auto-detection |
|-----------|---------------|
| **Zephyr RTOS** | `west.yml` or `prj.conf` |
| **PlatformIO** | `platformio.ini` |
| **Mbed OS** | `mbed-os/` directory or `mbed_app.json` |
| **Bare-metal / FreeRTOS** | Any build with `bear` |
| **Custom toolchain** | Any build with `bear` |

Subsequent runs are **incremental** — seconds for a few changed files.

## Documentation

| Document | Covers |
|----------|--------|
| **[Installation](docs/installation.md)** | Prerequisites, install, upgrade, Ollama setup, AI assistant integration |
| **[Tools Reference](docs/tools.md)** | All 21 MCP tools, 9 CLI commands, internal workings, search pipeline |
| **[Configuration](docs/configuration.md)** | `.fw-context/config.toml` — global defaults, per-project overrides, every setting |
| **[MCP Server](README-MCP.md)** | JSON-RPC protocol, tool schemas, error handling, debugging |

## Directory layout

```
~/.fw-context/
├── config.toml              # global defaults
├── .venv/                   # Python virtual environment
│   └── bin/
│       ├── fw-context       # CLI
│       └── fw-context-mcp   # MCP server
└── index/
    └── <project-id>/
        └── index.db         # SQLite + FTS5 + vec0 + refs

your-firmware/
├── .fw-context/
│   └── config.toml          # per-project overrides
└── compile_commands.json
```

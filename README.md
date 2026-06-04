# fw-context

Build-aware code intelligence for embedded firmware — gives AI assistants
**real C/C++ understanding** of your codebase without hallucination.

## What it does

fw-context parses your **actual build** (`compile_commands.json`) with
[libclang](https://clang.llvm.org/), extracts every C/C++ symbol with its
qualified name, signature, and location, and stores them in a persistent
**SQLite + FTS5** database. An **MCP server** exposes this index as tools
that AI assistants call directly — sub-millisecond lookups, zero hallucination,
real `#ifdef`-aware understanding.

Once indexed, your assistant can answer:

> *"What does `uart_init` do and who calls it?"*
>
> *"Find all BLE advertising functions and how they're connected."*
>
> *"Show me the implementation of `adc_read` — not the declaration."*
>
> *"What would break if I change `spi_transfer`?"*
>
> *"Is the index up to date after my last edit?"*

**No grepping. No hallucination. No reading 8000 Mbed OS headers into context.**

## Quick start

```bash
# 1. Install
git clone git@github.com:turbyho/fw-context-mcp.git ~/.fw-context/src
uv venv ~/.fw-context/.venv --python 3.12
uv pip install --python ~/.fw-context/.venv/bin/python ~/.fw-context/src/
echo 'export PATH="$HOME/.fw-context/.venv/bin:$PATH"' >> ~/.zshrc

# 2. Register with your AI assistant
fw-context init

# 3. Generate compile_commands.json and index
cd your-firmware-project

# Mbed OS:
bear -- mbed compile --profile release

# Zephyr:
west build -b <board> -- -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
fw-context index build/compile_commands.json

# PlatformIO:
pio run --target compiledb
fw-context index

# CMake / Make:
bear -- make
# or: bear -- cmake --build build

fw-context index

# 4. Restart your assistant and start asking about your code
```

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
   fw-context index            exposes 17 tools over               local LLM runtime
   fw-context export           JSON-RPC (stdio)                    HTTP :11434
   fw-context watch                  │                                  │
   fw-context status           search_code ───────────── lookup   smart_search ──▶ translates NL → FTS5 terms
   fw-context reset            lookup_symbol ─────────── prefix   explain_symbol ─▶ explains function
   fw-context init             smart_search ──────────── NL       embeddings ────▶ mxbai-embed-large
   fw-context search           get_source ────────────── body
                               get_symbol_context ────── body+callers+callees
                               find_callers ──────────── direct callers
                               find_references ───────── all uses
                               find_call_path ────────── BFS in call graph
                               find_all_callers_recursive  transitive callers
                               find_callees_recursive ── transitive callees
                               find_dead_code ────────── never called
                               find_hotspots ─────────── most-called
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
| **MCP server** (`fw-context-mcp`) | Subprocess (AI assistant) | 17 tools over JSON-RPC — search, graph, source, maintenance |
| **Ollama** *(optional)* | Local daemon | NL search, symbol explanation, embedding generation |

## Key capabilities

- **Sub-millisecond lookups** — FTS5 full-text search, prefix/exact symbol lookup, call-graph traversal
- **Natural-language search** — *"how does the modem connect?"* → finds `network_registration`, `modem_attach`, … (Ollama, optional)
- **Vector search** — semantic similarity via `sqlite-vec` + Ollama embeddings, hybrid FTS5+vector re-ranking
- **Graph analytics** — call paths, transitive callers/callees, dead code detection, hotspot analysis
- **Incremental indexing** — only changed files re-parsed; auto-reindex on query detects and fixes staleness
- **Offline-first** — index is a file on disk at `~/.fw-context/index/`. No daemon, no cloud, no network.
- **`#ifdef`-aware** — uses real compiler flags; sees exactly what your compiler sees

## Supported ecosystems

Works with **any build system** that produces `compile_commands.json`:

| Ecosystem | Auto-detection | Typical scope | First index |
|-----------|---------------|---------------|-------------|
| **Mbed OS** | `mbed-os/`, `mbed_app.json` | 2 000–9 000 files, 20 000–80 000 symbols | 5–20 min |
| **Zephyr RTOS** | `west.yml`, `prj.conf` | 1 000–15 000 files, 10 000–100 000+ symbols | 3–20 min |
| **PlatformIO** | `platformio.ini` | 500–10 000 files, 5 000–80 000 symbols | 1–15 min |
| **Bare-metal / FreeRTOS** | Any build with `bear` | 50–2 000 files, 500–20 000 symbols | 5 s–3 min |
| **Custom toolchain** | Any build with `bear` | Scales to 100 000+ symbols | 10–30 min |

Subsequent runs are **incremental** — seconds for a few changed files.

## Documentation

| Document | Covers |
|----------|--------|
| **[Installation](docs/installation.md)** | Prerequisites, install, upgrade, Ollama setup, AI assistant integration |
| **[Tools Reference](docs/tools.md)** | All 17 MCP tools, 8 CLI commands, internal workings, search pipeline |
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

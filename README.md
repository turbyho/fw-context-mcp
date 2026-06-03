# fw-context

Build-aware code intelligence for embedded firmware. **fw-context** parses your
actual build (`compile_commands.json`) with [libclang](https://clang.llvm.org/)
and stores every C/C++ symbol — functions, classes, methods, enums, typedefs,
variables — in a full-text-searchable **SQLite + FTS5** database. AI assistants
(Claude Code, OpenCode) query it through an **MCP server** with sub-millisecond
latency and zero hallucination.

Works with **any embedded build system** that produces `compile_commands.json`:
**Mbed OS**, **Zephyr RTOS**, **PlatformIO** (Arduino, ESP-IDF, STM32Cube),
**FreeRTOS**, **bare-metal ARM**, and anything else compiled with GCC or Clang.
No LSP server required — it uses the real compiler flags, so it sees what your
compiler sees, `#ifdef`s and all.

First indexing of a typical project (500–1 000 files, 5 000–10 000 symbols)
takes **10–30 seconds**. Subsequent runs are incremental — only changed files
are re-parsed. The index lives on disk; your AI assistant reads it directly,
no daemon, no background process.

## Supported ecosystems

fw-context auto-detects your build system and source roots from the project
structure. The only hard requirement is `compile_commands.json` — if your
toolchain can produce one, fw-context can index it.

| Ecosystem | Auto-detection | Project scope | Framework scope | Notes |
|-----------|---------------|---------------|-----------------|-------|
| **Mbed OS** | `mbed-os/` directory, `mbed_app.json` | 50–500 files<br>500–5 000 project symbols | ~8 000 C++ files<br>~50 000+ symbols | ARM Mbed OS 5/6; `bear -- python3 build_app.py` |
| **Zephyr RTOS** | `west.yml`, `prj.conf` | 30–300 files<br>300–3 000 project symbols | ~15 000 files<br>~100 000+ symbols | Zephyr 3.x; `west build -- -DCMAKE_EXPORT_COMPILE_COMMANDS=ON` |
| **PlatformIO** | `platformio.ini` | 20–500 files<br>200–5 000 project symbols | Arduino ~500 files<br>ESP-IDF ~10 000 files | Arduino, ESP-IDF, STM32Cube, Teensy…; `pio run --target compiledb` |
| **FreeRTOS / Azure RTOS / ChibiOS / bare-metal** | Any build with `bear` | 10–500 files<br>100–5 000 symbols | Depends on RTOS | Any GCC/Clang-based toolchain; `bear -- make` / `bear -- cmake --build .` |
| **Custom / in-house RTOS** | Any build with `bear` | Scales to 2 000+ files<br>20 000+ symbols | Included in build | Proprietary toolchains that emit compile_commands.json work too |

The indexer auto-detects source directories (`src`, `lib`, `app`, `include`,
`drivers`, `modules`, `zephyr`, `mbed-os`) from your project structure and
`compile_commands.json` entries. Framework symbols your code actually
`#include`s are indexed automatically — no manual configuration needed.

## What problem it solves

AI coding assistants are great at Python, JavaScript, and Go — languages with
mature LSP servers, static analysis, and training data. Embedded firmware is
different:

- **Proprietary codebases** the model has never seen — no training data.
- **Massive dependency trees** — Mbed OS ships ~8 000 C++ files, Zephyr workspace
  contains 15 000+ source files across subsystems and drivers, and ESP-IDF
  adds ~10 000 more. Too large to read into context.
- **Build-time macros and `#ifdef`s** that change which code is actually compiled.
- **Custom build systems** (`mbed compile`, `west build`, `pio run`) whose
  include paths, defines, and compiler flags are opaque until you run them.

Without an index, the assistant resorts to grep — slow, imprecise, and blind to
which translation units are actually part of the build. It hallucinates function
names, misses overloads, and can't tell a definition from a declaration.

**fw-context solves this by parsing your actual build.** It reads
`compile_commands.json` (the exact same compilation database your build system
produces), parses every translation unit with **libclang** (the same parser as
your IDE), and stores the extracted symbols in a **SQLite + FTS5** database on
disk. Your AI assistant then queries this database through an **MCP server** —
sub-millisecond lookups, zero hallucination, real C/C++ understanding.

## How it works, at a glance

```
  1. Your build system         2. fw-context index            3. AI assistant
  ┌──────────────────┐        ┌─────────────────────┐        ┌──────────────────┐
  │ bear / west / pio │  ──→  │ libclang parses      │  ──→  │ lookup_symbol(…) │
  │ compile_commands  │        │ every .c/.cpp in TU │        │ search_code(…)   │
  │ .json             │        │ SQLite + FTS5 db    │        │ explain_symbol(…)│
  └──────────────────┘        └─────────────────────┘        └──────────────────┘
```

### Architecture (3 components)

| Component | Runs as | Built with | Purpose |
|-----------|---------|------------|---------|
| **CLI** (`fw-context`) | User command | Python + Click-like argparse | Index build, status checks, project management |
| **Indexer** | Called by CLI | **libclang** (C API via `libclang` Python bindings), SQLite FTS5 | Parses C/C++; extracts functions, classes, methods, enums, typedefs, variables; stores in full-text-searchable database |
| **MCP server** (`fw-context-mcp`) | Subprocess started by AI assistant | **MCP SDK** (stdlib JSON-RPC 2.0), SQLite, **httpx** (Ollama HTTP) | Exposes 12 tools over stdin/stdout; optionally calls **Ollama** for natural-language search and symbol explanation |

### Key technologies

| Tool | Role |
|------|------|
| **[libclang](https://clang.llvm.org/doxygen/group__CINDEX.html)** | C/C++ parser — traverses AST for each translation unit, extracts symbols with their qualified names, signatures, docstrings, and location. Uses the *exact* compiler flags from `compile_commands.json` (include paths, defines, standards) so it sees what the compiler sees. |
| **[SQLite](https://sqlite.org) + [FTS5](https://sqlite.org/fts5.html)** | Storage and full-text search. The `symbols` table stores name, kind, signature, file/line, docstring, and definition-vs-declaration flag. The FTS5 index covers 6 columns: `name` (original C++ name), `qualified_name` (full `namespace::class::method`), `signature` (parameter types), `docstring` (documentation comments), `file_path` (relative path from project root, for module context), and `name_tokens` (camelCase/snake_case split for sub-token search — e.g. `onConnectionComplete` → `on connection complete`). FTS5 enables fast prefix/phrase/keyword queries without loading entire files. |
| **[MCP SDK](https://github.com/modelcontextprotocol/python-sdk)** | JSON-RPC 2.0 server framework. Handles protocol initialization, message framing, and tool registration. The server is stateless between calls — each tool invocation opens the DB, runs the query, and closes. |
| **[httpx](https://www.python-httpx.org/)** | Async HTTP client for calling Ollama's REST API (`/api/chat`, `/api/tags`). Used by `smart_search` and `explain_symbol`. |
| **[Ollama](https://ollama.com)** *(optional)* | Local or cloud LLM. Powers natural-language search (translates "how does the modem connect?" → FTS5 keywords `modem connect`, `modem attach`) and generates plain-English explanations of C/C++ functions. When disabled, the AI assistant processes results with its own LLM — no Ollama required. |
| **[`bear`](https://github.com/rizsotto/Bear)** | LD_PRELOAD-based build interception. Wraps your build command (`bear -- python3 build_app.py`) to produce `compile_commands.json`. Required once per build config change. |

### What gets indexed

The indexer extracts every **definition and declaration** from the translation
units in `compile_commands.json`:

| Symbol kind | Example |
|-------------|---------|
| Function | `void uart_init(int baudrate)` |
| Method | `bool Modem::connect(const char *apn)` |
| Constructor / Destructor | `BleManager::BleManager()` |
| Class / Struct | `class BoxManager { … }` |
| Enum | `enum class State { IDLE, ACTIVE }` |
| Enum constant | `State::IDLE` |
| Typedef / Using | `using Callback = void(*)(int)`; `typedef uint32_t tick_t` |
| Variable / Field | `int _counter`; `static constexpr size_t BUFFER_SIZE` |
| Namespace | `namespace zbox { … }` |

All have **qualified names** (e.g. `zbox::BleManager::start_advertising`),
**signatures**, and **file + line** locations.

Each symbol is stored in the FTS5 index with 6 searchable columns:

| FTS5 column | Content |
|-------------|---------|
| `name` | Original C++ name |
| `qualified_name` | Full `namespace::class::method` |
| `signature` | Parameter types |
| `docstring` | Documentation comments |
| `file_path` | Relative path from project root (module context) |
| `name_tokens` | camelCase/snake_case split — `onConnectionComplete` → `on connection complete` |

Source roots are auto-detected from your project structure (`src`, `lib`, `app`,
`include`, `modules`, `zephyr`, `mbed-os`) and `compile_commands.json` entries —
OS and framework symbols your project actually `#include`s are indexed
automatically. No manual configuration needed.

## Quick start

```bash
# 1. Clone and install
git clone git@github.com:turbyho/fw-context-mcp.git ~/.fw-context/src
# or from the primary server:
# git clone git@git.montyho.com:turbyho/fw-context-mcp.git ~/.fw-context/src
uv venv ~/.fw-context/.venv --python 3.12
uv pip install --python ~/.fw-context/.venv/bin/python ~/.fw-context/src/
echo 'export PATH="$HOME/.fw-context/.venv/bin:$PATH"' >> ~/.bashrc

# 2. Register with your AI assistant
fw-context init

# 3. Generate compile_commands.json, then index
cd your-firmware-project
bear --output .fw-context/compile_commands.json -- python3 build_app.py --profile release     # Mbed OS
fw-context index .fw-context/compile_commands.json

# Set as default for the project:
echo 'compile_commands = ".fw-context/compile_commands.json"' >> .fw-context/config.toml

# Done. Restart your AI assistant and start asking it about your code.
```

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
enabled   = true
model     = "qwen2.5-coder:14b"   # must match the pulled tag exactly
ollama_url = "http://localhost:11434"
num_ctx   = 8192                   # keep ≥ 8192 — factory default (2048) is too small
```

> **Remote Ollama:** If Ollama runs on another machine (e.g. a GPU server),
> set `ollama_url = "http://192.168.1.50:11434"`. Everything else stays the same.

See [Choosing an Ollama model](#choosing-an-ollama-model) for a full comparison
of local and cloud model options.

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

**Source roots are auto-detected** by default. The indexer scans your project
for common source directories (`src`, `lib`, `app`, `include`, `modules`) and
OS/framework directories (`zephyr`, `mbed-os`), then supplements with
directories discovered from `compile_commands.json`. This means OS symbols
your project actually uses are indexed automatically — no manual config needed.

```bash
# Custom source roots (override auto-detection)
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

After editing source files, the index stays accurate in three ways:

- **Automatic re-index** — `search_code` and `lookup_symbol` detect stale files
  in their results and re-index them on the fly (up to 5 files, 30 s timeout),
  then re-run the query. No manual steps needed for typical edit→search workflows.
- **Automatic staleness detection** — if `compile_commands.json` itself changed,
  or auto-reindex failed, tools return a clear warning with next steps.
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
| `--source-roots DIR...` | auto-detected | Directories to index symbols from |
| `--name NAME` | directory name | Project name override |
| `--refs` | off | Build cross-reference / call graph (`find_callers`, `find_references`) |
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
Also creates `.fw-context/config.toml` in the current directory, so you can
customize `source_roots`, `exclude_paths`, and LLM settings per-project before
the first `fw-context index`.

## MCP tools

These tools are called by your AI assistant. See **[README-MCP.md](README-MCP.md)**
for the full JSON-RPC protocol reference, input/output schemas, error handling,
and integration details.

| Category | Tool | Purpose |
|----------|------|---------|
| Search | `search_code` | Keyword search (FTS5) |
| Search | `lookup_symbol` | Find symbol by name |
| Search | `smart_search` | Natural language → search (Ollama) |
| Understanding | `get_source` | Read a symbol's full definition body (no LLM, fast) |
| Understanding | `explain_symbol` | LLM explanation of a symbol (10–30 s) |
| Understanding | `get_active_build` | Check index freshness |
| Call graph | `find_callers` | Who calls a function (needs `index_refs`) |
| Call graph | `find_references` | All uses of a symbol — calls/reads/member access (needs `index_refs`) |
| Maintenance | `reindex_file` | Re-index one file |
| Maintenance | `reset_index` | Delete and rebuild index |
| Maintenance | `list_projects` | List all indexed projects |
| Maintenance | `check_ollama` | Verify Ollama availability |

## How each tool works internally

This section explains what happens under the hood when your AI assistant calls
each tool — which ones talk to Ollama, which ones are pure SQLite lookups, and
the exact data flow for each.

### Tools that DO NOT call Ollama (pure index, sub-millisecond)

These tools work entirely from the SQLite + FTS5 database on disk. No HTTP
calls, no GPU, no latency beyond a DB query.

| Tool | Mechanism | What happens |
|------|-----------|--------------|
| `lookup_symbol` | SQLite prefix/exact match on `symbols` table | Opens the DB, runs a `SELECT` with `WHERE name LIKE 'prefix%'`, returns the first N rows. Definitions sort before declarations. That's it — a single SQL query. |
| `search_code` | FTS5 full-text search | Opens the DB, runs an FTS5 `MATCH` query against 6 columns (`name`, `qualified_name`, `signature`, `docstring`, `file_path`, `name_tokens`). Results are ranked by a weighted scoring function (name match = 3 pts, qualified name = 2 pts, file path = 1 pt, project-local bonus = +1, function/method/class bonus = +2). A single FTS5 query, no iteration. |
| `get_source` | Disk read | Looks up the symbol's `file`, `line`, and `end_line` from the index, then reads that exact range from the source file on disk. `end_line` (stored at index time from libclang's AST extent) means brace-matching is never needed — the body is always perfectly bounded. |
| `get_active_build` | SQLite metadata read | Reads the `build_info` and `files` tables to compute index freshness. Compares stored `mtime` of every indexed file against the current on-disk `mtime`. Also checks whether `compile_commands.json` itself changed. |
| `find_callers` | SQLite cross-reference lookup (needs `--refs`) | Queries the `refs` table for rows where `target = '<symbol>' AND ref_kind = 'call'`. Returns each call site with its file:line and the enclosing function's qualified name. |
| `find_references` | SQLite cross-reference lookup (needs `--refs`) | Same as `find_callers` but broader — matches `ref_kind IN ('call', 'ref', 'member')` to capture every use of the symbol. |
| `reindex_file` | Single-file libclang re-parse | Looks up the translation unit in `compile_commands.json` that covers the given file, runs libclang's `parse` + `visit` on just that TU, and atomically replaces the old symbols in the database. |
| `list_projects` | Filesystem scan | Scans `~/.fw-context/index/` for subdirectories, reads each `index.db`'s `build_info` table, and returns project metadata. |
| `reset_index` | File deletion | Deletes the SQLite database file for the project after confirmation. |
| `check_ollama` | HTTP health check | Calls Ollama's `/api/tags` endpoint to list installed models, then checks if the configured model is among them. The only non-Ollama tool that makes an HTTP call — but it's a fast metadata query, not an LLM inference. |

### Tools that DO call Ollama (LLM inference, 1–40 seconds)

Only two tools use Ollama. Both are **optional** — when Ollama is disabled
(`[llm] enabled = false`), these tools return raw prompts and source code for
the AI assistant to process with its own LLM.

#### `smart_search` — natural language → FTS5 keywords (2–10 s)

A two-phase search that translates human language into precise FTS5 queries:

```
                         Phase 1: Rough search
┌──────────────┐    ┌─────────────────────────────┐
│ User query:  │    │ 1. Split into words         │
│ "uart serial │───▶│ 2. FTS5 search with stems   │───▶ rough results
│  port read   │    │    (uart* serial* port*…)    │    (~20–50 symbols)
│  and write   │    └─────────────────────────────┘
│  data"       │
└──────────────┘
                         Phase 2: Ollama refinement
┌─────────────────────────────────────────────────┐
│ 3. Take rough result symbol names               │
│    → PalUartReadData, uarte_irq_handler,        │
│      nordic_nrf5_uart0_handler, SerialBase …    │
│                                                  │
│ 4. Send to Ollama with prompt:                  │
│    "Study these real symbols from the project.   │
│     Learn the naming conventions. Generate       │
│     3–5 FTS5 keyword search terms matching       │
│     these patterns."                             │
│                                                  │
│ 5. Ollama returns JSON:                         │
│    ["uart*", "Uart*", "serial*", "Serial*",     │
│     "read_data*", "ReadData*", "write_data*"]    │
└─────────────────────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────┐
│ 6. Run FTS5 with refined terms                  │
│ 7. Merge & deduplicate with rough results        │
│ 8. Rank by weighted score                       │
│ 9. Return final results (max 100)               │
└─────────────────────────────────────────────────┘
```

**Why two phases?** The rough search gathers real symbol names from the
project so Ollama can learn the naming conventions. If you just sent the
query "uart serial port read and write data" to FTS5 directly, you'd miss
camelCase symbols like `PalUartReadData` (single token in FTS5 — prefix
`Uart*` won't match `uart*`). Ollama sees both patterns and generates
matching prefixes for each.

**Non-ASCII (Czech) queries** get an extra pre-processing step: the query
is sent to Ollama for translation to English before entering Phase 1. This
means `"jak se modem připojuje"` becomes `"how does the modem connect"`
first, then proceeds through the two-phase search.

**When Ollama is disabled:** `smart_search` falls back to Phase 1 only —
word-split + FTS5 with stems. Results are still scored and ranked, just
without the naming-convention refinement.

#### `explain_symbol` — plain-English function explanation (10–40 s)

Reads a symbol's source code context and asks Ollama to explain it:

```
┌──────────────────────────────────────────────────────┐
│ 1. Look up symbol in index                           │
│    (file, line, end_line from SQLite)                │
│                                                      │
│ 2. Read source file from disk                        │
│    → symbol's definition body (line..end_line)       │
│    → plus context_lines above and below (default 40) │
│                                                      │
│ 3. Build prompt with:                                │
│    - "You are a C/C++ embedded firmware expert."     │
│    - Symbol name, file:line, signature               │
│    - Source context (the actual code)                │
│    - "Cover: (1) what it does and why,               │
│              (2) key mechanism or logic,             │
│              (3) when/how it fits in the system."    │
│                                                      │
│ 4. POST to Ollama /api/chat                          │
│    model: qwen2.5-coder:14b (configurable)           │
│    context window: 8192 tokens                       │
│    typical response: 300–600 tokens                  │
│    latency: 10–40 s (model + prompt dependent)       │
│                                                      │
│ 5. Return explanation + symbol metadata              │
└──────────────────────────────────────────────────────┘
```

**What the prompt includes:** The symbol's source code with surrounding
context (configurable via `context_lines`, default 40), so the model sees
not just the function body but also nearby functions, comments, and
`#ifdef`s. This gives it enough context to explain how the function fits
into the larger driver or module.

**When Ollama is disabled:** `explain_symbol` returns the source code and
the exact prompt that would have been sent, plus a `warning` field. The AI
assistant then processes this with its own LLM — same quality, but the
assistant's model answers instead of Ollama.

### Auto-reindex on stale files

`search_code` and `lookup_symbol` check whether any files in their result
set have been modified since the last index (by comparing stored `mtime` vs
on-disk `mtime`). If stale files are found:

```
┌────────────────────────────────────────────────────┐
│ 1. Results contain symbols from stale files        │
│                                                    │
│ 2. For each stale file (max 5, 30 s timeout):     │
│    → Find its translation unit in                  │
│      compile_commands.json                         │
│    → Re-parse with libclang                        │
│    → Atomically replace old symbols in DB          │
│                                                    │
│ 3. Re-run the original query against the           │
│    fresh index                                     │
│                                                    │
│ 4. Return results (with a warning if               │
│    auto-reindex failed or compile_commands.json    │
│    itself is stale)                                │
└────────────────────────────────────────────────────┘
```

This means the typical edit→search workflow requires no manual re-index
step — you edit a file, the tool detects staleness on the next query, and
re-indexes it on the fly.

### Staleness detection (`get_active_build`)

The index tracks two kinds of staleness:

| Staleness source | How detected | Severity |
|------------------|-------------|----------|
| Source file edited | `stored_mtime < on_disk_mtime` | Low — auto-reindex fixes it |
| `compile_commands.json` changed | `stored_hash != current_hash` | High — requires manual `fw-context index` |

When `compile_commands.json` changes (new files added, build config
changed), auto-reindex cannot help because the set of translation units
may be different. The tool returns `stale: true` with a clear message to
run `fw-context index`.

### Index building in detail

```
┌──────────────────────────────────────────────────────┐
│ 1. Read compile_commands.json                        │
│    → Extract all translation units (file + args)     │
│                                                      │
│ 2. Auto-detect source_roots                          │
│    → Scan for src/, lib/, app/, include/, modules/   │
│    → Add framework dirs (mbed-os/, zephyr/)          │
│    → Add dirs from compile_commands.json entries     │
│                                                      │
│ 3. For each translation unit (in parallel):          │
│    → Parse with libclang (cxindex.parse)             │
│      using the EXACT compiler flags from the         │
│      compile_commands.json entry:                    │
│      - Include paths (-I…)                           │
│      - Preprocessor defines (-D…)                    │
│      - Language standard (-std=…)                    │
│      - Target triple (--target=…)                    │
│    → Traverse AST (cursor.visit)                     │
│    → Extract symbols where cursor location           │
│      is within a source_root directory               │
│    → Category: function, method, class, enum,        │
│      typedef, variable, field, namespace, etc.       │
│                                                      │
│ 4. Write to SQLite (atomic transaction)              │
│    → Delete old symbols for this TU                  │
│    → Insert new symbols                              │
│    → Populate FTS5 index (6 columns)                 │
│    → If --refs: extract & store cross-references     │
│                                                      │
│ 5. Write build metadata                              │
│    → compile_commands.json hash                      │
│    → mtime of every indexed source file              │
│    → symbol count, file count                        │
│    → indexed_at timestamp                            │
└──────────────────────────────────────────────────────┘
```

### Database schema

```sql
-- Core symbol table
CREATE TABLE symbols (
    id INTEGER PRIMARY KEY,
    name TEXT,                  -- short name (e.g. "serial_putc")
    qualified_name TEXT,        -- full path (e.g. "serial_putc")
    kind TEXT,                  -- function, method, class, enum, …
    file TEXT,                  -- absolute path
    line INTEGER,               -- definition line (1-based)
    end_line INTEGER,           -- end of definition body
    is_definition INTEGER,      -- 1 = definition, 0 = declaration
    signature TEXT,             -- "void serial_putc(serial_t *, int)"
    docstring TEXT,             -- doxygen/comment block
    tu_file TEXT                -- translation unit from compile_commands
);

-- FTS5 full-text search index (6 columns)
CREATE VIRTUAL TABLE fts USING fts5(
    name,
    qualified_name,
    signature,
    docstring,
    file_path,
    name_tokens,                -- camelCase/snake_case split
    content='symbols',
    content_rowid='id'
);

-- Cross-references (only when --refs is enabled)
CREATE TABLE refs (
    source_file TEXT,           -- file containing the reference
    source_line INTEGER,        -- line of the reference
    ref_kind TEXT,              -- 'call', 'ref', 'member'
    caller TEXT,                -- qualified name of enclosing function
    caller_kind TEXT,           -- kind of the enclosing function
    target TEXT                 -- qualified name of referenced symbol
);

-- Build metadata
CREATE TABLE build_info (
    config_hash TEXT,           -- sha256 of compile_commands.json
    compile_commands TEXT,      -- path to compile_commands.json
    symbol_count INTEGER,
    file_count INTEGER,
    reference_count INTEGER,    -- only when --refs
    indexed_at TEXT             -- ISO 8601
);

CREATE TABLE files (
    path TEXT PRIMARY KEY,      -- absolute path
    mtime REAL                  -- stored modification time
);
```

### Call graph (cross-references)

`find_callers` and `find_references` answer "who calls `modem_init`?" and "where
is `ZCfgDataManager` used?". Both accept short names (`reset_slot_error_lock`)
or qualified names (`zbox::ZRTDATA::reset_slot_error_lock`). The reference
graph is **opt-in** — it adds indexing
time and database size, so it is off by default. Enable it and re-index:

```bash
fw-context index --refs
# or set in .fw-context/config.toml:  [index] index_refs = true
```

Only project-internal references are stored (both the call site and the target
definition under your source roots), which keeps the graph bounded — references
into system/framework headers are dropped. `find_callers` returns each call site
with its file:line and the enclosing caller's qualified name.

### Source reading (`get_source`)

`get_source` returns a symbol's full definition body straight from disk — no
LLM, no waiting. It reads the **exact line range** stored during indexing
(`line .. end_line` from libclang's AST extent), so the body is always
correctly bounded even for multi-line signatures, templates, and braces inside
strings or comments. For indexes built without `end_line` (before the column
was added), it falls back to brace-matching.

### Index health (`get_active_build`)

`get_active_build` now reports two additional fields beyond symbol/file counts:

- `reference_count` — number of indexed cross-references (0 when `index_refs`
  was not enabled). Only populated after indexing with `--refs`.
- `modified_files_count` — indexed source files whose on-disk modification time
  is newer than the stored one. A non-zero count means source files have been
  edited since the last index and a reindex would pick up the changes.

The top-level `stale` flag is now `True` when either `compile_commands.json` or
**any individual source file** has changed since the last index.

### Search behaviour

**Auto-prefix expansion:** bare words passed to `search_code` and `smart_search`
are automatically expanded to prefix queries, so `modem init` becomes
`modem* init*`. This means a query for `uart` also matches `uart_init`,
`uart_write`, etc. Use quoted phrases (`"spi init"`) to suppress expansion and
match the exact token sequence.

**Non-ASCII queries:** if the query contains non-ASCII characters (e.g. Czech),
`smart_search` passes it through Ollama for translation to English before
generating FTS5 terms — no manual transliteration needed.

### Search quality

`smart_search` ranks results by a weighted score across FTS5 columns:

| Match | Points |
|-------|--------|
| `name` / `name_tokens` (camelCase split) | 3 |
| `qualified_name` | 2 |
| `file_path` (module context, e.g. `spi* write*` hits `write` in `spi_driver.cpp`) | 1 |
| Project-local symbol (`src/`, `lib/`, not `mbed-os/`) | +1 bonus |
| Kind: function / method / class | +2 bonus |
| Kind: enum constant | +1 bonus |
| Kind: variable / field | 0 bonus |

Results are sorted by score descending. When Ollama is disabled, `smart_search`
falls back to `search_code` with a plain keyword split — scoring still applies.

## Configuration

Configuration is loaded from two TOML files, merged with **project overrides
global** — values set in the project config take precedence over the global one.
Both files are auto-created with sensible defaults on first use.

```
~/.fw-context/config.toml          global defaults (apply to all projects)
        │
        ├── merged with ──→  <project>/.fw-context/config.toml   project overrides
        │
        ▼
    final Config used by fw-context
```

### Configuration reference

#### `[index]` — Indexer settings

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `db_dir` | `"~/.fw-context/index"` | global | Directory where SQLite index databases are stored. One subdirectory per project (keyed by project ID). |
| `compile_commands` | `"compile_commands.json"` | project | Path to the compilation database. Relative paths are resolved from the project root. Recommended: `.fw-context/compile_commands.json` to keep the project root clean. Use `bear --output .fw-context/compile_commands.json -- ...` to generate it there. |
| `source_roots` | `[]` *(auto-detect)* | project | Directories to scan for symbols. **Empty list = auto-detect** (scans `src`, `lib`, `app`, `include`, `modules` + framework dirs `zephyr`/`mbed-os` + top-level dirs from `compile_commands.json`). Set explicitly to narrow indexing: `["src", "lib"]`. Directories that don't exist are silently skipped. |
| `exclude_paths` | `["build", "BUILD"]` | project | Directories to skip during indexing. Useful for generated code, test fixtures, or third-party vendored code. Paths are relative to project root. |
| `index_refs` | `false` | project | Build the cross-reference / call graph (`find_callers`, `find_references`). Off by default — reference extraction adds indexing time and DB size. Set `true` (or pass `--refs`) and re-index to enable. |

#### `[llm]` — LLM / Ollama settings

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `enabled` | `true` | both | Enable or disable Ollama integration. When `false`, `explain_symbol` returns the source code + prompt for the AI assistant to answer itself, and `smart_search` falls back to direct keyword search. No Ollama connection needed. |
| `ollama_url` | `"http://localhost:11434"` | global | Ollama API base URL. Change if Ollama runs on a different machine (e.g. `"http://192.168.1.50:11434"`). |
| `model` | `"qwen2.5-coder:14b"` | both | Ollama model tag. Override per-project to use a different model for different codebases. See [Choosing an Ollama model](#choosing-an-ollama-model) for recommendations. |
| `num_ctx` | `8192` | global | Context window size in tokens passed to Ollama. The factory default (2048) is too small for source code — keep at least 8192. |
| `debug_log` | *(none)* | both | Path to a JSONL debug log file. When set, all Ollama prompts and responses are logged for debugging. Example: `"~/.fw-context/llm-debug.jsonl"`. |

#### `[project]` — Project metadata

| Key | Default | Scope | Description |
|-----|---------|-------|-------------|
| `name` | *(directory name)* | project | Human-readable project name used in `fw-context list` and status output. Defaults to the directory name if not set. |

### Example: Global config (`~/.fw-context/config.toml`)

```toml
[index]
db_dir = "~/.fw-context/index"

[llm]
# enabled = true   # set to false to disable Ollama — tools return raw prompts
ollama_url = "http://localhost:11434"
model = "qwen2.5-coder:14b"
num_ctx = 8192
# debug_log = "~/.fw-context/llm-debug.jsonl"   # uncomment to log all LLM calls
```

### Example: Per-project config (`<project>/.fw-context/config.toml`)

```toml
[project]
name = "my-firmware"

[index]
compile_commands = "compile_commands.json"
source_roots = []                # auto-detect (default)
exclude_paths = ["build", "BUILD", "generated"]

[llm]
enabled = false                  # no Ollama — AI assistant handles results
# model = "qwen2.5-coder:14b"   # use a different model just for this project
```

### How `source_roots` auto-detection works

When `source_roots` is empty (default), the indexer scans your project for:

1. **Common source dirs:** `src`, `lib`, `app`, `include`, `modules`
2. **Framework dirs:** `zephyr`, `mbed-os` (if they exist at project root)
3. **Top-level dirs from `compile_commands.json`** — any directory that
   contains at least one translation unit from the compilation database

The `compile_commands.json` determines *which* translation units are parsed;
the `#include` chain determines *which* OS headers are traversed — providing
natural, build-accurate filtering.

Set `source_roots` explicitly only when you need to narrow indexing to a
subset of directories or include directories outside the project root
(e.g. PlatformIO frameworks in `~/.platformio/packages/`).

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
For manual setup or other assistants, see **[README-MCP.md](README-MCP.md#integration)**.

## Troubleshooting

### "No index found"

Run `fw-context index` from the project root first. The index is per-project and
stored under `~/.fw-context/index/`.

### Index is stale

Run `fw-context index` — it's incremental and picks up only changed files.

### Symbols missing or incomplete

By default source roots are auto-detected from your project structure and
`compile_commands.json`. If symbols from certain directories are missing:

1. Check `.fw-context/config.toml` — explicit `source_roots` overrides
   auto-detection. Remove it to restore auto-detection, or add the missing
   directories.
2. Make sure the file is listed in `compile_commands.json`.
3. For PlatformIO frameworks (Arduino, ESP-IDF) whose sources live outside
   the project root (e.g. `~/.platformio/packages/`), add them explicitly:
   ```toml
   source_roots = ["src", "lib", "/home/user/.platformio/packages/framework-arduinoespressif32"]
   ```

### "Cannot connect to Ollama"

Ollama is optional. If you don't have it (no GPU, no cloud account), disable
it in config:

```toml
# ~/.fw-context/config.toml
[llm]
enabled = false
```

Then `explain_symbol` returns the source code and prompt for the AI assistant
to answer itself, and `smart_search` falls back to direct keyword search —
no Ollama connection needed.

To use Ollama instead, install it from [ollama.com](https://ollama.com) and
pull a model:

```bash
ollama pull qwen2.5-coder:14b
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

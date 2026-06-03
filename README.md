# fw-context

Build-aware code intelligence for embedded firmware — a tool that lets AI
assistants **understand your C/C++ codebase** without hallucinating function
names, missing overloads, or guessing which `#ifdef` branch is active.

## What it does

**fw-context** parses your actual build (`compile_commands.json`) with
[libclang](https://clang.llvm.org/) — the same parser your IDE uses — and
stores every C/C++ symbol in a full-text-searchable **SQLite + FTS5** database
on disk. An **MCP server** then exposes this index as tools that AI assistants
(Claude Code, OpenCode) can call directly: sub-millisecond lookups, zero
hallucination, real C/C++ understanding.

Once indexed, your AI assistant can answer questions like:

> *"What does `modem_parser_oob_init` do and who calls it?"*
>
> *"Find all functions related to BLE pairing failure handling."*
>
> *"Show me the definition of `ZCfgDataManager` — not the declaration, the actual implementation."*
>
> *"Is the index up to date, or did I forget to re-index after my last edit?"*
>
> *"Where is `reset_slot_error_lock` used across the entire project?"*

The assistant resolves these by querying the index — no grepping through files,
no guessing from training data, no reading 8000 Mbed OS headers into context.

### Why not just use LSP?

LSP servers (clangd, ccls) are designed for interactive editing — go-to-definition,
autocomplete, hover info. They're excellent at that. But they have limitations
for AI-assisted code exploration:

- **No full-text search across the codebase.** LSP can find a symbol by name,
  but not "all functions related to modem initialization."
- **No persistent index.** The LSP index lives in memory; restart the server
  and it rebuilds from scratch. fw-context's index is a SQLite file — it
  survives reboots, and your AI assistant reads it in milliseconds.
- **No AI-oriented interface.** LSP speaks a protocol designed for editors,
  not for AI agents. fw-context's MCP tools are purpose-built for the
  assistant's workflow: lookup, search, explain, re-index.
- **Grep is blind to build context.** `grep -r` doesn't know which translation
  units are actually compiled, which `#ifdef` branch is active, or whether a
  match is a definition or just a forward declaration. fw-context knows all
  three.

fw-context doesn't replace LSP — it complements it. Use clangd for editing,
fw-context for AI-assisted exploration.

### Three components

| Component | Runs as | Purpose |
|-----------|---------|---------|
| **CLI** (`fw-context`) | User command | Index build, status checks, project management |
| **Indexer** | Called by CLI | libclang parses every translation unit from `compile_commands.json`, extracts symbols with qualified names, signatures, and locations, stores them in SQLite + FTS5 |
| **MCP server** (`fw-context-mcp`) | Subprocess started by AI assistant | Exposes 12 tools over JSON-RPC 2.0 (stdin/stdout); optionally calls a local **Ollama** model for natural-language search and symbol explanation |

### Key characteristics

- **Zero daemon.** No background process — the index is a file on disk that
  the MCP server reads on-demand. Each tool call opens the DB, runs a query,
  and closes.
- **Ollama is optional.** `explain_symbol` and `smart_search` can use a local
  LLM for natural-language queries, but when Ollama is disabled, the AI
  assistant processes results with its own model. Everything else works
  without any LLM at all.
- **Incremental by default.** After the first index, only files with changed
  modification time are re-parsed. Editing one file and running `fw-context index`
  takes under a second.
- **Auto-reindex on query.** `search_code` and `lookup_symbol` detect stale
  files in their results and re-index them on the fly — so the typical
  edit→search workflow requires no manual steps.
- **Offline-first.** The index lives at `~/.fw-context/index/`. No cloud
  account needed. Ollama runs locally if you choose to use it.

### Supported ecosystems

Works with **any embedded build system** that produces `compile_commands.json`:
**Mbed OS**, **Zephyr RTOS**, **PlatformIO** (Arduino, ESP-IDF, STM32Cube),
**FreeRTOS**, **bare-metal ARM**, and anything else compiled with GCC or Clang.
No LSP server, no `c_cpp_properties.json`, no manual configuration — it uses
the real compiler flags, so it sees what your compiler sees, `#ifdef`s and all.

First indexing depends on project size — from **seconds** for small bare-metal
projects (50–200 files) to **15–20 minutes** for large Mbed OS codebases with
thousands of framework headers pulled in through `#include`. Subsequent runs
are **incremental** — only changed files are re-parsed, so they finish in
seconds.

## Supported ecosystems

fw-context auto-detects your build system and source roots from the project
structure. The only hard requirement is `compile_commands.json` — if your
toolchain can produce one, fw-context can index it.

| Ecosystem | Auto-detection | Typical indexed scope | Index time (first run) | Notes |
|-----------|---------------|----------------------|------------------------|-------|
| **Mbed OS** | `mbed-os/`, `mbed_app.json` | 2 000–9 000 files<br>20 000–80 000 symbols | 5–20 min | Framework + your code; `#include` chain pulls in large parts of mbed-os |
| **Zephyr RTOS** | `west.yml`, `prj.conf` | 1 000–15 000 files<br>10 000–100 000+ symbols | 3–20 min | West workspace includes modules, drivers, subsystems |
| **PlatformIO** | `platformio.ini` | 500–10 000 files<br>5 000–80 000 symbols | 1–15 min | Arduino (~500 files), ESP-IDF (~10 000), STM32Cube, Teensy… |
| **Bare-metal / FreeRTOS** | Any build with `bear` | 50–2 000 files<br>500–20 000 symbols | 5 s–3 min | No OS framework overhead; only your code + RTOS kernel headers |
| **Custom / in-house RTOS** | Any build with `bear` | Scales to 10 000+ files<br>100 000+ symbols | 10–30 min | Proprietary toolchains that emit compile_commands.json work too |

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

## Installation & setup

For detailed prerequisites, installation steps, Ollama setup, and AI assistant
integration, see **[Installation guide](docs/installation.md)**.

The short version:

```bash
git clone git@github.com:turbyho/fw-context-mcp.git ~/.fw-context/src
uv venv ~/.fw-context/.venv --python 3.12
uv pip install --python ~/.fw-context/.venv/bin/python ~/.fw-context/src/
echo 'export PATH="$HOME/.fw-context/.venv/bin:$PATH"' >> ~/.zshrc
fw-context init
```

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

First run depends on project size — small projects finish in seconds, large
Mbed OS codebases with 50 000+ symbols can take 15–20 minutes. Subsequent runs are
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


## CLI & MCP tools

Full CLI reference, MCP tool catalog, and a detailed walkthrough of how each
tool works internally: see **[Tools Reference](docs/tools.md)**.

## Configuration

Complete configuration reference — global defaults, per-project overrides,
source root auto-detection, and all `[index]` / `[llm]` / `[project]` settings:
see **[Configuration](docs/configuration.md)**.

## Ollama & AI assistant setup

For Ollama installation, model selection (local and cloud), and AI assistant
integration (Claude Code, OpenCode): see **[Installation guide](docs/installation.md)**.

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

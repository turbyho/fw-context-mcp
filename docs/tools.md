# Tools Reference

Complete reference for all fw-context CLI commands and MCP server tools.

## CLI commands

### `fw-context index`

Build or update the symbol index from `compile_commands.json`.

```bash
# Default: everything on (refs + embeddings)
fw-context index

# Explicit path, verbose
fw-context index compile_commands.json -v

# Skip cross-references for faster indexing on large projects
fw-context index --no-refs

# Skip embeddings (no Ollama available)
fw-context index --no-embeddings

# Custom source roots
fw-context index --source-roots src lib drivers
```

| Option | Default | Description |
|--------|---------|-------------|
| `compile_commands.json` | from config | Path to the compilation database |
| `--project DIR` | `.` | Project root directory |
| `--source-roots DIR…` | auto-detected | Directories to index symbols from |
| `--name NAME` | directory name | Project name override |
| `--no-refs` | off | Skip cross-reference / call graph indexing |
| `--no-embeddings` | off | Skip embedding generation |
| `-v` | off | Verbose progress output |

**Generating `compile_commands.json`:**

| Build system | Command |
|-------------|---------|
| **Mbed OS** | `bear -- mbed compile --profile release` |
| **Zephyr** | `west build -b <board> -- -DCMAKE_EXPORT_COMPILE_COMMANDS=ON` |
| **PlatformIO** | `pio run --target compiledb` |
| **CMake** | `bear -- cmake --build build` or `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON` |
| **Make** | `bear -- make` |
| **Custom** | `bear -- <your-build-command>` |

### `fw-context search`

Full-text search over indexed symbols from the command line.

```bash
fw-context search "uart_init"
fw-context search "spi transfer" --limit 10
fw-context search "ble_*"                # prefix wildcard
fw-context search '"i2c read"'           # exact phrase
```

### `fw-context status`

Show index freshness and statistics.

```bash
$ fw-context status
Project : /home/user/firmware
Symbols : 8586  files=952  refs=12430
Indexed : 2026-06-05 09:35:18
DB      : ~/.fw-context/index/a1b2c3d4/index.db
Status  : fresh
```

### `fw-context list`

List all indexed projects.

```bash
$ fw-context list
my-zephyr-app   /home/user/zephyr-app     symbols=12430  files=1502  indexed=2026-06-05
my-mbed-app     /home/user/mbed-app       symbols=8586   files=952   indexed=2026-06-04
```

### `fw-context reset`

Delete the index for a project.

```bash
fw-context reset                  # interactive confirmation
fw-context reset -y               # skip confirmation
fw-context reset --project /path  # specific project
```

### `fw-context init`

Register fw-context with AI assistants and inject usage instructions.

```bash
fw-context init                         # all detected assistants
fw-context init --tool claude-code      # specific tool only
fw-context init --dry-run               # preview without writing
fw-context init --force                 # overwrite collisions
fw-context init --list-tools            # show supported tools
```

| Tool | ID | Scope |
|------|----|-------|
| Claude Code | `claude-code` | global (`~/.claude/`) |
| OpenCode | `opencode` | global (`~/.config/opencode/`) |
| Kilo Code | `kilocode` | inherits from Claude Code |
| Codex | `codex` | global (`~/.codex/`) |
| Cursor | `cursor` | project (`.cursor/`) |

### `fw-context export`

Export the symbol index as portable JSON.

```bash
fw-context export                     # stdout
fw-context export -o index.json       # file
fw-context export --no-refs           # symbols only
```

The format is `fw-context-export/1` — suitable for sharing between machines,
debugging, or as input to other tools.

### `fw-context watch`

Watch project files and auto-reindex on changes.

```bash
fw-context watch                      # watch current project
fw-context watch --debounce 3000      # 3-second debounce (default: 2000 ms)
```

Monitors `.c`, `.cpp`, `.h`, `.hpp` files. Uses `watchfiles` for efficient
filesystem events. Press `Ctrl+C` to stop.

```bash
pip install "fw-context-mcp[watch]"   # install watch dependency
```

---

## MCP tools

These 17 tools are called by your AI assistant over JSON-RPC. Each tool
opens the database, runs its query, and closes — no persistent connections.

### Search & lookup

#### `search_code`

Full-text search with FTS5 syntax.

```
Input:  {"query": "uart init", "kind?": "function", "limit?": 20}
Output: [{"name": "uart_init", "qualified_name": "drv::uart_init", "kind": "function",
          "file": "/path/src/uart.c", "line": 42, "is_definition": true,
          "signature": "void uart_init(int baudrate)", "docstring": "Initialize UART"}, …]
```

**FTS5 syntax:**
- `uart*` — prefix wildcard
- `"spi transfer"` — exact phrase match
- `modem init` — both terms (AND). Underscore is a word separator — `modem_init` is treated as `modem AND init`

**Kind filter:** `function`, `method`, `constructor`, `destructor`, `class`, `struct`, `enum`, `enum_constant`, `typedef`, `variable`, `field`, `namespace`

#### `lookup_symbol`

Find a symbol by name (exact or prefix match).

```
Input:  {"name": "uart_init", "exact?": true}
Output: [{"name": "uart_init", "qualified_name": "drv::uart_init", "kind": "function",
          "file": "/path/src/uart.c", "line": 42, "is_definition": true,
          "signature": "void uart_init(int baudrate)", "docstring": "Initialize UART"}]
```

Definitions are sorted before declarations. Use `exact: true` for exact name
match; the default is prefix match (`uart` → `uart_init`, `uart_write`, …).

#### `smart_search`

Natural language → FTS5 keywords via Ollama (optional).

```
Input:  {"query": "how does the modem connect to the network?", "limit?": 20}
Output: [
  {"_generated_queries": ["network_reg*", "modem_attach*", "pdp_context*"]},
  {"_rough_queries": ["modem", "connect", "network"]},
  …symbol results…
]
```

Multi-phase pipeline: translate → rough search → LLM query generation
→ FTS5 search → refine → vector re-rank → deduplicate → format.

When Ollama is disabled (`[llm] enabled = false`), falls back to word-split
FTS5 search. Non-English queries are auto-translated in Phase 0.

### Understanding

#### `get_source`

Read a symbol's definition body — no LLM, sub-millisecond.

```
Input:  {"name": "adc_read"}
Output: {"name": "adc_read", "kind": "function", "file": "/path/src/adc.c",
         "line": 55, "signature": "uint16_t adc_read(uint8_t channel)",
         "source": "  55  uint16_t adc_read(uint8_t channel) {\n  …\n  70  }"}
```

Uses libclang's `end_line` for exact body boundaries. Falls back to
brace-matching for older indexes.

#### `explain_symbol`

Look up a symbol and get a plain-English explanation via Ollama.

```
Input:  {"name": "spi_transfer", "context_lines?": 40}
Output: {"name": "spi_transfer", "kind": "function", "file": "/path/src/spi.c",
         "line": 120, "signature": "int spi_transfer(const uint8_t* tx, uint8_t* rx, size_t n)",
         "explanation": "This function performs a full-duplex SPI transfer…"}
```

Takes 10–30 seconds with Ollama. When disabled, returns `source` + `explain_prompt`
for the AI assistant to process with its own model.

#### `get_symbol_context`

Rich LLM context — body, callers, and callees in one response.

```
Input:  {"name": "modem_connect"}
Output: {"name": "modem_connect", "kind": "function",
         "file": "/path/src/modem.c", "line": 210,
         "signature": "int modem_connect(const char* apn)",
         "source": " 210  int modem_connect(const char* apn) {\n  …\n 245  }",
         "callers": [
           {"name": "network_init", "file": "/path/src/net.c", "line": 80, "kind": "function"},
           {"name": "on_registration_timeout", "file": "/path/src/modem.c", "line": 310, "kind": "function"}
         ],
         "callees": [
           {"name": "send_at_command", "kind": "function", "file": "/path/src/modem.c"},
           {"name": "wait_for_urc", "kind": "function", "file": "/path/src/modem.c"},
           {"name": "pdp_activate", "kind": "function", "file": "/path/src/net.c"}
         ]}
```

Designed as one-shot LLM context — answers "what does this do and how does it fit?"
in a single call. Limited to 5 callers and 5 callees.

### Call graph

All graph tools use the cross-reference index. Enabled by default —
disable with `fw-context index --no-refs` or `[index] index_refs = false`
if you don't need them.

#### `find_callers`

Who calls this function? (direct callers only)

```
Input:  {"name": "uart_write", "limit?": 50}
Output: [{"file": "/path/src/main.c", "line": 35, "ref_kind": "call",
          "caller": "main", "caller_kind": "function"}, …]
```

#### `find_references`

All uses of a symbol — calls, reads, member access.

```
Input:  {"name": "g_sensor_data", "limit?": 50}
Output: [{"file": "/path/src/sensor.c", "line": 12, "ref_kind": "ref",
          "caller": "sensor_task", "caller_kind": "function"}, …]
```

#### `find_call_path`

Find paths between two functions via BFS in the call graph.

```
Input:  {"from_name": "main", "to_name": "uart_send_byte", "max_depth?": 10}
Output: [{"depth": 3, "chain": "main → app_init → uart_write → uart_send_byte"}]
```

Returns up to 5 shortest paths. Requires both symbols to be in the index.

#### `find_all_callers_recursive`

All transitive callers — who calls this, directly or indirectly?

```
Input:  {"name": "gpio_set", "max_depth?": 5}
Output: [{"name": "led_toggle", "qualified_name": "led_toggle", "kind": "function",
          "file": "/path/src/led.c", "depth": 1}, … (2 steps away), … (3 steps away)]
```

Deduplicated — each caller appears once at its shortest distance.

#### `find_callees_recursive`

What does this call, directly or indirectly?

```
Input:  {"name": "main", "max_depth?": 5}
Output: [{"name": "spi_init", "kind": "function", "file": "/path/src/spi.c", "depth": 1},
         {"name": "spi_transfer", "kind": "function", "file": "/path/src/spi.c", "depth": 2}, …]
```

#### `find_dead_code`

Functions defined but never called.

```
Input:  {"limit?": 100}
Output: [{"name": "unused_helper", "kind": "function", "file": "/path/src/utils.c",
          "signature": "void unused_helper(int x)", "line": 200}, …]
```

Entry points (`main`, interrupt handlers) will appear here — filter them out
manually. Only definitions with `kind IN ('function', 'method', 'constructor', 'destructor')` are checked.

#### `find_hotspots`

Most-called functions ranked by caller count.

```
Input:  {"limit?": 20}
Output: [{"name": "log_debug", "kind": "function", "caller_count": 147, …},
         {"name": "millis", "kind": "function", "caller_count": 89, …}, …]
```

### Index maintenance

#### `get_active_build`

Check index health — call at session start.

```
Input:  {"project_root?": "/path/to/project"}
Output: {"config_hash": "a1b2…", "project_id": "c3d4…", "project_root": "/path/to/project",
         "build_system": "zephyr", "compile_commands": "/path/build/compile_commands.json",
         "indexed_at": "2026-06-05T09:35:18", "symbol_count": 12430, "file_count": 1502,
         "reference_count": 8900, "modified_files_count": 3, "stale": true}
```

`stale: true` when `compile_commands.json` changed or any indexed source file
has a newer on-disk mtime.

#### `reindex_file`

Re-index a single file after editing.

```
Input:  {"file_path": "/abs/path/to/src/main.c"}
Output: {"file": "/abs/path/to/src/main.c", "translation_units": 1,
         "symbols_updated": 28, "elapsed_s": 2.5}
```

**Limitation:** The file must appear in `compile_commands.json`. Header-only
files without a corresponding `.c`/`.cpp` entry cannot be re-indexed this way —
run `fw-context index` instead.

#### `reset_index`

Delete the index. Always dry-run first, then `confirm: true`.

```
Input:  {"confirm": false}     → {"action": "dry_run", "symbol_count": 8586, …}
Input:  {"confirm": true}      → {"action": "deleted", "message": "…"}
```

#### `list_projects`

List all indexed firmware projects.

```
Input:  {}
Output: [{"project_id": "a1b2…", "name": "my-zephyr-app", "root_path": "/path",
          "build_system": "zephyr", "symbol_count": 12430, "file_count": 1502,
          "indexed_at": "2026-06-05T09:35:18", "stale": false, "db": "…"}, …]
```

#### `check_ollama`

Verify Ollama availability before using `smart_search` or `explain_symbol`.

```
Input:  {}
Output: {"status": "ok", "ollama_running": true, "ollama_enabled": true,
         "configured_model": "qwen2.5-coder:14b", "num_ctx": 8192,
         "installed_models": ["qwen2.5-coder:14b", "mxbai-embed-large:latest"], …}
```

Returns `status: "disabled"` when `[llm] enabled = false` — no Ollama needed.

---

## How the search pipeline works

### `smart_search` — multi-phase natural-language search

```
Phase 0: Translate (LLM)
┌────────────────────────────────────────────────────┐
│ Non-English queries → English.                     │
│ "SPI komunikace s displayem" → "SPI communication  │
│ with display".  English queries pass through.      │
└────────────────────────────────────────────────────┘
                    │
                    ▼
Phase 1: Rough search (FTS5)
┌────────────────────────────────────────────────────┐
│ Word-pair + single-word FTS5 queries.              │
│ "modem connect network" → samples from the index   │
│ (12–20 symbols with their naming conventions).     │
└────────────────────────────────────────────────────┘
                    │
                    ▼
Phase 2a: LLM query generation
┌────────────────────────────────────────────────────┐
│ Ollama sees the query + sample symbols.            │
│ Learns naming style (snake_case vs camelCase).     │
│ UNDERSTANDING: <subsystem, intent>                 │
│ QUERIES: ["network_reg*", "modem_attach*", …]      │
└────────────────────────────────────────────────────┘
                    │
                    ▼
Phase 2b: LLM refinement (feedback loop)
┌────────────────────────────────────────────────────┐
│ First-round queries executed.                      │
│ LLM sees top results → course-corrects if needed.  │
│ Better queries OR [] if already correct.            │
└────────────────────────────────────────────────────┘
                    │
                    ▼
Phase 3: FTS5 search + merge
┌────────────────────────────────────────────────────┐
│ All generated queries run via OR.                  │
│ Deduplicated by (name, file_path).                 │
│ Scored: name match=3, qualified_name=2,            │
│ file_path=1, project-local=+1, kind=+0..2          │
└────────────────────────────────────────────────────┘
                    │
                    ▼
Phase 4: Vector re-rank (sqlite-vec)
┌────────────────────────────────────────────────────┐
│ FTS5 candidates re-ranked by cosine distance.      │
│ Query embedded via mxbai-embed-large.              │
│ Hybrid: text recall + vector precision.            │
└────────────────────────────────────────────────────┘
                    │
                    ▼
Phase 5: Deduplicate + format
┌────────────────────────────────────────────────────┐
│ Merge FTS5 + vector results.                       │
│ Prefer definitions.  Score-sort.  Limit to N.      │
│ Add metadata entries (_generated_queries, …).      │
└────────────────────────────────────────────────────┘
```

### Search quality scoring

| Match location | Points |
|---------------|--------|
| `name` / `name_tokens` (camelCase split) | 3 |
| `qualified_name` | 2 |
| `file_path` (module context) | 1 |
| Project-local code (`src/`, `lib/`, not OS framework) | +1 |
| Kind: function / method / class / struct / enum / typedef | +2 |
| Kind: enum constant / namespace | +1 |
| Kind: variable / field | 0 |

### Auto-reindex on query

`search_code` and `lookup_symbol` detect stale files in their results
(on-disk mtime > stored mtime) and re-index them automatically (up to 5
files, 30 s timeout), then re-run the query. The typical edit→search
workflow requires no manual steps.

### Index building (detailed)

```
1. Read compile_commands.json → extract translation units (file + compiler args)

2. Auto-detect source_roots:
   Scan for src/, lib/, app/, include/, modules/
   Add framework dirs (mbed-os/, zephyr/)
   Add top-level dirs from compile_commands.json entries

3. For each translation unit (parallel, ThreadPoolExecutor):
   Parse with libclang using exact compiler flags (-I, -D, -std, --target)
   Traverse AST → extract symbols within source_roots
   Category: function, method, class, enum, typedef, variable, field, …
   If --refs: extract cross-references within source_roots

4. Write to SQLite (atomic per-TU transaction):
   Delete old symbols for this TU
   Insert new symbols + FTS5 triggers
   If --refs: insert references
   If --embeddings: generate + store vector embeddings

5. Write build metadata:
   compile_commands.json hash, file mtimes, symbol/file/ref counts
```

### Vector search

When embeddings are generated during indexing (`fw-context index --embeddings`),
symbols are stored in two tables:

| Table | Storage | Query method |
|-------|---------|-------------|
| `embeddings` | BLOB (4096 bytes per 1024-dim vector) | Legacy brute-force (Python) |
| `vec_symbols` (vec0) | sqlite-vec virtual table | KNN via `MATCH` (C implementation) |

The `EmbeddingPhase` prefers `vec0` when available (index built after this
feature), falls back to BLOB brute-force for older indexes, and operates as
a **hybrid re-rank** when FTS5 results already exist — avoiding duplicate
searches and expensive merging.

---

## Auto-detection of build system

| Ecosystem | Detected by | compile_commands.json location |
|-----------|------------|-------------------------------|
| **Mbed OS** | `mbed-os/` directory or `mbed_app.json` | Project root |
| **Zephyr** | `west.yml` or `prj.conf` | `build/compile_commands.json` |
| **PlatformIO** | `platformio.ini` | Project root |
| **CMake** | `CMakeLists.txt` + `compile_commands.json` | `build/` |
| **Bare-metal** | Any build with `bear` | Project root |

---

## Search tips

- **Use short queries.** 1–3 words works best. FTS5 is a token-based engine —
  longer phrases narrow results too aggressively.
- **Omit underscores.** FTS5 treats `_` as a word separator. `modem_init` →
  `modem AND init`. Write `modem init` instead.
- **Trailing `*` for prefix match.** `uart_*` finds `uart_init`, `uart_write`,
  `uart_read`, …
- **Quotes for exact phrases.** `"spi transfer"` matches that exact token sequence,
  not `transfer over spi`.
- **Kind filter for precision.** Narrow `search_code` to `kind=function` when
  you know you're looking for a function — eliminates variables, fields, enums.

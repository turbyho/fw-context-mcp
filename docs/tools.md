# Tools Reference

CLI and MCP tools reference — command-line interface, MCP server tools,
and a detailed walkthrough of how each tool works internally.

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

Register fw-context with Claude Code and OpenCode (see [Setup](installation.md#setting-up-ai-assistant-integration)).
Also creates `.fw-context/config.toml` in the current directory, so you can
customize `source_roots`, `exclude_paths`, and LLM settings per-project before
the first `fw-context index`.

## MCP tools

These tools are called by your AI assistant. See **[README-MCP.md](../README-MCP.md)**
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


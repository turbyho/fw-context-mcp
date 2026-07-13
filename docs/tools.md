# Tools Reference

Complete reference for all fw-context CLI commands and MCP server tools.

## CLI commands

### `fw-context index`

Build or update the symbol index from `compile_commands.json`.

> **Incremental by default.**  An existing `compile_commands.json` is reused
> — only changed files are re-parsed.  Use `--build` to force a clean build
> and full re-index when needed (e.g. after SDK update or build system changes).
> Use `--force` to bypass mtime checks and force re-index of all files,
> embeddings, LLM analysis, overrides, and caches without rebuilding
> (e.g. after schema changes or tool updates).

```bash
# Incremental (default) — reuse existing compile_commands.json, fast:
fw-context index

# Force clean build + full re-index:
fw-context index --build

# Force full re-index of all files, embeddings, analysis, and caches
# (skips mtime checks — use after schema changes or tool updates):
fw-context index --force

# Explicit path, verbose
fw-context index compile_commands.json -v

# Skip cross-references for faster indexing on large projects
fw-context index --no-refs

# Skip embeddings (no Ollama available)
fw-context index --no-embeddings

# Skip LLM symbol analysis
fw-context index --no-analyze

# Custom source roots
fw-context index --source-roots src lib drivers
```

| Option | Default | Description |
|--------|---------|-------------|
| `compile_commands.json` | from config | Path to the compilation database (skips build) |
| `--project DIR` | `.` | Project root directory |
| `--build` | off | Force a clean build and regenerate `compile_commands.json` |
| `--no-clean` | off | With `--build`: skip clean, do incremental build |
| `--source-roots DIR…` | auto-detected | Directories to index symbols from |
| `--name NAME` | directory name | Project name override |
| `--no-refs` | off | Skip cross-reference / call graph indexing |
| `--no-embeddings` | off | Skip embedding generation |
| `--no-analyze` | off | Skip LLM symbol analysis |
| `--analyze` | on | Force LLM symbol analysis (negates --no-analyze) |
| `--force` | off | Force re-index of all files, embeddings, LLM analysis, overrides, PageRank, and hotspot cache (bypasses mtime checks) |
| `-v` | off | Verbose progress output |

**Generating `compile_commands.json`:**

| Build system | Command |
|-------------|---------|
| **Zephyr** | `west build -b <board> -- -DCMAKE_EXPORT_COMPILE_COMMANDS=ON` |
| **PlatformIO** | `pio run --target compiledb` |
| **Mbed OS** | `bear -- mbed compile --profile release` |
| **CMake** | `bear -- cmake --build build` or `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON` |
| **Make** | `bear -- make` |
| **Custom** | `bear -- <your-build-command>` |

### `fw-context project-init`

Initialize or verify project-level configuration.

```bash
fw-context project-init                     # create configs, update .gitignore
fw-context project-init --project /path     # specific project
```

What it does:
1. Creates `.fw-context/config.toml` and `.fw-context/local.toml` with defaults (idempotent)
2. Adds `compile_commands.json` and `.fw-context/local.toml` to `.gitignore`
3. Auto-detects the build system
4. Checks `compile_commands.json` for completeness

Run after `fw-context init` or when setting up a new clone.

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
Symbols : 8586  files=952
Indexed : 2026-06-05 09:35:18
DB      : ~/.fw-context/index/a1b2c3d4/index.db
```

### `fw-context list`

List all indexed projects.

```bash
$ fw-context list
my-zephyr-app   /home/user/zephyr-app     symbols=12430  files=1502  indexed=2026-06-05 09:35:18
my-platformio-app /home/user/pio-app      symbols=8586   files=952   indexed=2026-06-04 16:20:45
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

### `fw-context export`

Export the symbol index as portable JSON.

```bash
fw-context export                     # stdout
fw-context export -o index.json       # file
fw-context export --no-refs           # symbols only
```

The format is `fw-context-export/1` — suitable for sharing between machines,
debugging, or as input to other tools.

### `fw-context analyze`

Run LLM symbol analysis on an already-indexed project. Useful when the
index was built with `--no-analyze` and you want to add analysis later,
or when you want to re-generate analysis for all symbols.

```bash
fw-context analyze                        # analyze current project
fw-context analyze --project /path        # specific project
```

Analysis is generated per function/method using the full function body
(via libclang extent) and callee names as supplementary context. Results
are stored in the `llm_analysis` table and denormalized into the FTS5
index — symbols become searchable by their purpose, not just their name.

Configure with `[llm] analyze_symbols`, `[llm] analysis_model`, and
`[llm] num_ctx` in `~/.fw-context/config.toml`.

### `fw-context cache stats`

Show cache statistics for one or both tiers. The ``--remote`` flag queries
the server in real-time and shows a per-model breakdown with percentages.
When run inside a project directory with an existing index, it also shows
how many of that project's symbols are cached on the server.

```bash
fw-context cache stats                    # both tiers
fw-context cache stats --remote           # Tier 2 only (remote server)
```

Output:
```
$ fw-context cache stats --remote
Remote cache (Tier 2): https://fw-cache.montyho.com
  Total entries: 14486
  Newest entry:  2026-07-06 11:32:23
  Models:
    qwen2.5-coder:14b: 14476 (100%)
  Project cache: 3138/3138 cached (452361ffbf84f774)

$ fw-context cache stats
Local cache (Tier 1): 3160 entries  (/home/user/.fw-context/llm_cache.db)
Remote cache (Tier 2): https://fw-cache.montyho.com
  Total entries: 14486
  ...
  Project cache: 3138/3138 cached (452361ffbf84f774)
```

### `fw-context cache clear`

Delete cache entries for one or both tiers.

```bash
fw-context cache clear                    # local cache (Tier 1) only
fw-context cache clear --remote           # project's entries from server (Tier 2)
fw-context cache clear --all              # both tiers
fw-context cache clear --remote -y        # skip confirmation prompt
```

The `--remote` flag reads all content hashes from the project's `llm_analysis_cache`
table and sends them to the server's `POST /cache/clear` endpoint. Requires
`[cache_server]` configured in `.fw-context/local.toml` and a token with
`can_write` permission.

All clear operations are safe — cache entries are rebuilt automatically
on the next `fw-context index --analyze` or `fw-context analyze`.

### `fw-context cache push`

Push all local cache entries to the remote cache server. Uses overwrite mode
(``X-Cache-Overwrite``) by default — newer local entries replace older remote ones.

```bash
fw-context cache push                       # push all, batch size from config (100)
fw-context cache push --batch 500           # larger batches for faster transfer
```

Requires ``[cache_server]`` configured with ``can_write`` and ``can_overwrite``
permissions. Progress is reported in batches.

### `fw-context cache remote-init`

Interactive wizard to configure the remote cache server connection.
Prompts for URL, token, verifies connectivity, and writes ``[cache_server]``
to ``.fw-context/local.toml``.

```bash
fw-context cache remote-init                 # configure for current project
fw-context cache remote-init --project DIR   # configure for specific project
```

The wizard:

1. Shows the currently configured URL (if any)
2. Prompts for the server URL (press Enter to keep current or accept default)
3. Prompts for the authentication token (required — paste your read or read+write token)
4. Verifies the connection: calls ``/health`` then ``/cache/stats`` with the token
5. Writes the ``[cache_server]`` section to ``local.toml`` (idempotent)

Typical output:

```
No remote cache configured.

Cache server URL [https://fw-cache.example.com]: https://fw-cache.montyho.com

Token (paste your read or read+write token): <token>

Verifying connection to https://fw-cache.montyho.com ...
  Connected. Server has 14486 cached entries.

Remote cache configured: https://fw-cache.montyho.com
Config written to: /path/to/project/.fw-context/local.toml
Run 'fw-context cache stats --remote' to verify.
```

### `fw-context watch`

Manage the background watcher daemon that auto-reindexes changed source files.

```bash
fw-context watch status              # show daemon status
fw-context watch restart             # restart the daemon
fw-context watch restart --project /path  # specific project
```

#### `fw-context watch status`

Show the watcher daemon status for the current project — whether it's running,
its PID and uptime, the modified file count, and whether a background reindex
is in progress.

```bash
$ fw-context watch status
Project:    my-zephyr-app
Path:       /home/user/zephyr-app
DB dir:     /home/user/.fw-context/index/a1b2c3d4
Daemon:     running (pid 12345, uptime 3600s)
Socket:     /home/user/.fw-context/index/a1b2c3d4/daemon.sock (active)
Modified:   3 file(s)
Index:      idle
Last index: [45/45] main.cpp: unchanged
```

#### `fw-context watch restart`

Stop the current watcher daemon (SIGTERM, falls back to SIGKILL after 3 s),
clean up leftover socket/pid/lock files, and spawn a fresh daemon. Verifies
the new daemon responds to a ping before returning.

```bash
$ fw-context watch restart
Stopping daemon (pid 12345)...
Old daemon stopped.
Starting new daemon...
Daemon restarted successfully.
```

### `fw-context version`

Show version information.

```bash
$ fw-context version
fw-context-mcp <version>
```

---

## MCP tools

These tools are called by your AI assistant over JSON-RPC. Each tool
opens the database, runs its query, and closes — no persistent connections.

All tools accept an optional `project_root` parameter (default: auto-detected
from the current working directory). It is shown in every input block below
but typically omitted — the auto-detection handles the common case.

### Search & lookup

#### `search_code`

Full-text search with FTS5 syntax.

```
Input:  {"query": "uart init", "project_root?": "/path/to/project", "kind?": "function", "limit?": 20}
Output: [{"name": "uart_init", "qualified_name": "drv::uart_init", "kind": "function",
          "file": "/path/src/uart.c", "line": 42, "is_definition": true,
          "signature": "void uart_init(int baudrate)", "docstring": "Initialize UART",
          "is_template": false, "is_virtual": false, "is_pure_virtual": false,
          "summary": "Initialize the UART peripheral…", "inputs": "baudrate…",
          "outputs": "…", "enum_value": null, "_fallback": "fts5+kind"}, …]
```

Enum constants include `enum_value` (the integer value) when non-None.
Results include `is_template`, `is_virtual`, `is_pure_virtual` flags
(boolean, always present). When the symbol is a template instantiation,
`template_usr` references the template definition. When the symbol is
a member (method/field/nested type), `parent_usr` references the parent class.
When LLM analysis has been generated (`fw-context index --analyze`), results
include `summary`, `inputs`, and `outputs` fields with plain-English
descriptions.

**Progressive relaxation:** when the initial FTS5 search returns nothing, the
tool automatically broadens the search in up to six steps:

1. *FTS5 with kind filter* — the original query with the user-provided `kind`
   constraint.
2. *FTS5 without kind filter* — drops the `kind` constraint (users often guess
   the wrong kind for a symbol).
3. *`name_tokens` substring match* — searches the pre-computed CamelCase/
   snake_case token column (e.g. `BuildType` is indexed as `"build type"`).
   Requires at least N‑1 of N query terms to match.
4. *Single-term docstring LIKE* — when only one query term was given and the
   token-based steps found nothing, does a raw LIKE over the docstring column
   to catch terms the FTS5 tokeniser may have missed.
5. *Individual term FTS5* — searches each query word separately and merges
   the results.
6. *Macro FTS5 fallback* — searches the ``macros_fts`` table for matching
   ``#define`` names and values (``kind="macro"``, ``_fallback="macros_fts"``).

Results from fallback steps carry `_fallback` indicating which method
succeeded: `"fts5"`, `"name_tokens_like"`, `"docstring_like"`,
`"individual_terms"`, or `"macros_fts"`. Results from the primary FTS5 path have
`"_fallback": "fts5+kind"` or omit the field.

**FTS5 syntax:**
- `uart*` — prefix wildcard
- `"spi transfer"` — exact phrase match
- `modem init` — both terms (AND for `search_code`). Underscore is a word separator — `modem_init` is treated as `modem AND init`
- **For `search_bodies` and `search_content`:** bare multi-word queries are OR-joined (each term prefix-wildcarded) — prefer single-word queries

**Kind filter:** `function`, `method`, `constructor`, `destructor`, `class`, `struct`, `union`, `enum`, `enum_constant`, `typedef`, `variable`, `field`, `namespace`

#### `search_bodies`

Find patterns in C/C++ function **bodies** — the implementation code inside `{ }`.

Searches ONLY the text between `{` and `}` of function/method definitions.
Does NOT search file-scope constructs (`extern "C"`, `#include`, `#define`,
type declarations in headers).

```
Input:  {"query": "attach", "project_root?": "/path/to/project", "kind?": "function", "limit?": 20, "project_only?": true}
Output: [{"name": "setup", "qualified_name": "setup", "kind": "function",
          "file": "/path/src/main.cpp", "line": 55, "is_definition": true,
          "signature": "void setup()",
          "_match_snippet": "…_timeout.<b>attach</b>(callback(&led_blink, 1000))…",
          "source": "… (function body, truncated at 2000 chars)"}]
```

- **When to use `search_bodies` vs `search_code` vs `search_content`:**
  - `search_bodies` — patterns in function BODIES (what the code DOES):
    `.attach(`, `NVIC_SetVector(`, `.rise(`, `.fall(`, `callback(&`
  - `search_content` — patterns anywhere in FILES:
    `extern "C"`, `InterruptIn`, `#define`, type declarations
  - `search_code` — find symbols by NAME:
    `modem init`, `interrupt handler`

**FTS5 query tips for `search_bodies`:**
Bare multi-word queries are OR-joined (each term prefix-wildcarded):
`"attach callback"` → `attach* OR callback*`. Prefer single-word queries
for broad matching — `"attach"` finds all `.attach(...)` patterns across
the codebase.

Results include `_match_snippet` — a highlighted excerpt showing each
match in context with `<b>…</b>` tags. Project code sorts before vendor
code. Set `project_only=True` to filter to application code only.

#### `search_content`

Find patterns in **full file content** — file-scope + function bodies, not
limited to function bodies.

Searches **ifdef-filtered** file text — only code that actually compiles
for the current build configuration. Inactive `#ifdef` branches are replaced
with blank lines (preserving original line numbers).

```
Input:  {"query": "InterruptIn", "project_root?": "/path/to/project", "limit?": 20, "project_only?": false}
Output: [{"file": "/path/src/main.cpp", "language": "cpp",
          "mtime": "2026-06-05T09:35:18", "_match_snippet": "…InterruptIn…"}]
```

Covers file-scope constructs that `search_bodies` cannot see:
`extern "C"`, type declarations in headers, `#include`, `#define`,
global variables, namespace blocks. Results are file-level (one entry
per matching file) — use `search_bodies` for per-function granularity.

When `files_fts` is missing (legacy index), falls back to LIKE search
on `files.content` — results include `_fallback: "like"` and no snippet
highlighting. Run `fw-context index` to upgrade.

**FTS5 query tips for `search_content`:** Bare multi-word queries are
OR-joined (prefix-wildcarded). Prefer single-word queries.

#### `lookup_symbol`

Find a symbol by name (exact or prefix match). Searches functions, methods,
classes, enums, typedefs, variables, fields, and **macros**.

```
Input:  {"name": "uart_init", "project_root?": "/path/to/project", "exact?": true, "limit?": 50}
Output: [{"name": "uart_init", "qualified_name": "drv::uart_init", "kind": "function",
          "file": "/path/src/uart.c", "line": 42, "is_definition": true,
          "signature": "void uart_init(int baudrate)", "docstring": "Initialize UART"}]
```

Macro example:
```
Input:  {"name": "CONFIG_UART_BAUDRATE", "project_root?": "/path/to/project", "exact?": true}
Output: [{"name": "CONFIG_UART_BAUDRATE", "kind": "macro",
          "file": "/path/include/config.h", "line": 15,
          "value": "115200", "expanded_value": "115200"}]
```

Definitions are sorted before declarations. Use `exact: true` for exact name
match; the default is prefix match (`uart` → `uart_init`, `uart_write`, …).

Macros are extracted via `clang -dM -E` with the compiler flags from
`compile_commands.json`, so `#ifdef`-conditional macros resolve correctly
for the indexed build configuration. Enum constants include `enum_value`
when non-None.

#### `smart_search`

Natural language → FTS5 keywords via Ollama (optional).

```
Input:  {"query": "how does the modem connect to the network?", "project_root?": "/path/to/project", "limit?": 20}
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

#### `semantic_search`

Concept search using pre-computed symbol embeddings. Finds symbols conceptually
related to a natural-language query, even when the query words don't appear
literally in the code.

```
Input:  {"query": "parcel locker state machine", "project_root?": "/path/to/project", "threshold?": 0.60, "limit?": 20}
Output: [{"name": "set_shipment", "qualified_name": "Locker::set_shipment",
          "_similarity": 0.72, "_method": "embedding", …}, …]
```

Uses cosine similarity over variable-dimension embeddings (generated during
`fw-context index --embeddings`). Models: mxbai-embed-large → 1024-dim,
qwen3-embedding → 4096-dim. **When to prefer over `search_code`:**
conceptual queries ("power consumption" → `get_load_power`) where keywords
don't match. **When to prefer `search_code`:** known keywords or symbol names
(`"fram_write"`, `"cbor encode"`).

Results include `_similarity` (cosine similarity score, 0–1) and `_method`
(`"embedding"` or `"search_code_fallback"`). Source-aware ranking boosts
project code (1.2×) over vendored SDK paths (0.85×).

**Threshold guidance** (mxbai-embed-large):
- `0.50` — exploratory, more results
- `0.55` — balanced, ~1000 results
- `0.60` — high precision (default)
- `0.65` — strict, may miss relevant symbols

Requires Ollama with an embedding model. Falls back to `search_code` if
Ollama is unavailable or disabled.

### Understanding

#### `get_file_map`

Structural overview of a file — all symbols grouped by kind. Fast table of
contents before reading a chapter.

```
Input:  {"file_path": "src/modem_msg.cpp", "project_root?": "/path/to/project", "signatures?": false, "max_per_kind?": 30}
Output: {"file": "src/modem_msg.cpp", "total_symbols": 426,
         "symbols": {
           "method":    [{"name": "_is_socket_ok", "line": 140, "signature": "bool _is_socket_ok()"}, …],
           "variable":  [{"name": "_buffer_msg", "line": 105}, …],
           "constructor": [{"name": "ModemMsg", "line": 130, "signature": "void ModemMsg(…)"}, …],
           "function":  [{"name": "memset", "line": 94}, …],
           "struct":    [{"name": "buffer", "line": 3561}, …],
           "enum_constant": {
             "count": 8,
             "subgroups": [
               {"name": "StatusCode", "count": 5,
                "constants": [
                  {"name": "OPERATION_SUCCESSFUL", "enum_value": 1, "line": 21},
                  {"name": "TOKEN_INVALID", "enum_value": -2, "line": 23}, …]},
               {"name": "State", "count": 3,
                "constants": [{"name": "Idle", "enum_value": 0, "line": 10}, …]}
             ]
           }
         }}
```

Enum constants are grouped into `subgroups` by parent enum. Each subgroup has
`name` (parent enum), `count` (real total, even when `max_per_kind` limits
`constants`), and a `constants` list with `name`, `qualified_name`, `line`,
and `enum_value` (integer value, when available).

Pass a relative path (`src/main.cpp`) or just the filename (`main.cpp` — suffix
match). Use this instead of `Read` on large files — symbols are organized by
kind in a single response.

#### `get_source`

Read a symbol's definition body — no LLM, fast.

```
Input:  {"name": "adc_read", "project_root?": "/path/to/project"}
Output: {"name": "adc_read", "kind": "function", "file": "/path/src/adc.c",
         "line": 55, "signature": "uint16_t adc_read(uint8_t channel)",
         "source": "  55  uint16_t adc_read(uint8_t channel) {\n  …\n  70  }"}
```

Uses libclang's `end_line` for exact body boundaries. Falls back to
brace-matching for older indexes.

For enum constants, the result includes `enum_value` (the integer value).
For enums, the result includes a `constants` array listing all member
constants with their names and values:

```
Input:  {"name": "BleCmd::StatusCode", "project_root?": "/path/to/project"}
Output: {"name": "StatusCode", "kind": "enum", "file": "/path/src/ble_cmd.h",
         "line": 20, "signature": "",
         "constants": [
           {"name": "OPERATION_SUCCESSFUL", "enum_value": 1},
           {"name": "TOKEN_INVALID", "enum_value": -2}
         ],
         "source": "  20  enum StatusCode {\n  …\n  24  }"}
```

#### `explain_symbol`

Look up a symbol and get a plain-English explanation of its purpose, inputs,
outputs, and side effects. Falls back to **macro explanation** when the name
matches a macro definition — returns `kind: "macro"` with the expanded value.

```
Input:  {"name": "spi_transfer", "project_root?": "/path/to/project", "context_lines?": 40}
Output: {"name": "spi_transfer", "kind": "function", "file": "/path/src/spi.c",
         "line": 120, "signature": "int spi_transfer(const uint8_t* tx, uint8_t* rx, size_t n)",
         "explanation": "This function performs a full-duplex SPI transfer…\n\nInputs: …\nOutputs: …",
         "llm_analysis": {"summary": "…", "inputs": "…", "outputs": "…",
                          "model": "qwen2.5-coder:14b", "analyzed_at": "2026-06-21T17:51:14"}}
```

Macro fallback:
```
Input:  {"name": "CONFIG_UART_BAUDRATE", "project_root?": "/path/to/project"}
Output: {"name": "CONFIG_UART_BAUDRATE", "kind": "macro",
         "file": "/path/include/config.h", "line": 15,
         "signature": "#define CONFIG_UART_BAUDRATE",
         "value": "115200", "expanded_value": "115200",
         "source": "#define CONFIG_UART_BAUDRATE 115200"}
```

**Pre-computed (instant):** When the index was built with `--analyze` (default),
symbols have pre-generated descriptions stored in the `llm_analysis` table.
`explain_symbol` returns these instantly — no Ollama call, no waiting.

**On-demand fallback (10–30 s):** When no pre-computed analysis exists (index
built with `--no-analyze`, or symbol re-indexed without re-analysis), falls
back to calling Ollama directly. When Ollama is disabled, returns `source` +
`explain_prompt` for the AI assistant to answer with its own model.

The analysis is generated during indexing using the full function body (via
libclang extent) and callee names from the reference index as context.
Re-indexing a file (`reindex_file`) auto-regenerates its analysis.

#### `get_symbol_context`

Rich LLM context — body, callers, and callees in one response.

```
Input:  {"name": "modem_connect", "project_root?": "/path/to/project", "project_only?": true}
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
         ],
         "indirect_call_sites": [],
         "resolution": null}
```

For function pointer fields the output includes call sites and resolution:

```
Input:  {"name": "onData", "project_root?": "/path/to/project"}
Output: {"name": "onData", "kind": "field",
         "indirect_call_sites": [
           {"file": "/path/src/main.c", "line": 16,
            "expr_text": "drv . onData",
            "fn_ptr_type": "void (*)(unsigned char *, int)",
            "caller": "test_assign"}
         ],
         "resolution": {
           "assignments_found": 1,
           "call_sites_found": 1,
           "resolved": true,
           "note": "1 function(s) assigned; 1 call site(s); fully resolved"
         }}
```

Designed as one-shot LLM context — answers "what does this do and how does it fit?"
in a single call. Returns all direct callers and callees (no artificial limit).

For field and variable symbols that have function pointer type, also includes
a ``resolution`` block: ``{assignments_found, call_sites_found, resolved, note}``
indicating whether assignments and call sites are linked (Phase 3).
``resolved: false`` with an explanatory note when data is incomplete — LLM
can detect uncertainty from this signal.

For enums, includes a ``constants`` array with all member constants and their
values (same shape as `get_source`). Enum constants include `enum_value`.

#### `read_file`

Read a complete source file with ifdef-filtered content — only code that
actually compiles for the current build configuration. Inactive `#ifdef`
branches are replaced with blank lines (preserving original line numbers).

```
Input:  {"file_path": "src/modem.c", "project_root?": "/path/to/project"}
Output: {"file": "/path/src/modem.c", "language": "c", "mtime": 1748534400.0,
         "lines": 512, "content": "/* Modem driver */\n\n#include \"modem.h\"\n…"}
```

Unlike generic file readers, this returns build-accurate content — code
gated behind `#ifdef BOARD_V2` is visible only when `BOARD_V2` is defined
for this build.

The path can be relative to the project root (`src/main.cpp`) or just the
filename (`main.cpp`). Falls back to raw disk content (with a `warning`)
when the indexed `files.content` column is empty — run
`fw-context index` to populate ifdef-filtered content.

### Call graph

All graph tools use the cross-reference index. Enabled by default —
disable with `fw-context index --no-refs` or `[index] index_refs = false`
if you don't need them.

#### `find_callers`

Who calls this function? Direct callers and indirect calls via function pointers
detected in call arguments, assignments, variable initializers, and struct/array
init lists. Automatically falls back to **macro lookup** when the symbol is not
found as a function/method.

```
Input:  {"name": "uart_write", "project_root?": "/path/to/project", "limit?": 50}
Output: [{"file": "/path/src/main.c", "line": 35, "ref_kind": "call",
          "caller": "main", "caller_kind": "function"},
         {"file": "/path/src/setup.c", "line": 12, "ref_kind": "indirect",
          "caller": "setup", "caller_kind": "function"}]
```

Indirect edges (``ref_kind: "indirect"``) appear when a function pointer
references a function through any of these patterns:
``callback(&Class::method, this)``, ``driver.onData = &handleData``,
``void (*fp)(int) = &handler``, or ``{.on_data = &handler}``.

For the invocation side — where the stored pointer is actually called — use
``find_indirect_call_sites``. For linking assignments to call sites — which
specific functions can run at a given call site — use
``find_indirect_targets``.

#### `find_references`

All uses of a symbol — calls, reads, member access, indirect references.
When the symbol is not found, automatically falls back to **macro lookup**
(returns `ref_kind: "macro_use"` for files that use the macro).

```
Input:  {"name": "g_sensor_data", "project_root?": "/path/to/project", "limit?": 50}
Output: [{"file": "/path/src/sensor.c", "line": 12, "ref_kind": "ref",
          "caller": "sensor_task", "caller_kind": "function"}, …]
```

Macro fallback:
```
Input:  {"name": "CONFIG_BUFFER_SIZE", "project_root?": "/path/to/project"}
Output: [{"kind": "macro", "name": "CONFIG_BUFFER_SIZE", "file": "…", "line": 42, …},
         {"file": "…", "ref_kind": "macro_use", "_match_snippet": "…CONFIG_BUFFER_SIZE…"}, …]
```

``ref_kind`` values: ``"call"`` (direct call), ``"ref"`` (variable read/write),
``"member"`` (member access), ``"indirect"`` (function pointer reference in
arguments, assignments, initializers, or init lists), ``"template_ref"``,
``"macro_use"`` (macro usage in file).

#### `find_call_path`

Find paths between two functions via BFS in the call graph.

```
Input:  {"from_name": "main", "to_name": "uart_send_byte", "project_root?": "/path/to/project", "max_depth?": 10}
Output: [{"depth": 3, "chain": "main → app_init → uart_write → uart_send_byte"}]
```

Returns up to 5 shortest paths. Requires both symbols to be in the index.

#### `find_all_callers_recursive`

All transitive callers — who calls this, directly or indirectly?

```
Input:  {"name": "gpio_set", "project_root?": "/path/to/project", "max_depth?": 5, "limit?": 50}
Output: [{"name": "led_toggle", "qualified_name": "led_toggle", "kind": "function",
          "file": "/path/src/led.c", "depth": 1}, … (2 steps away), … (3 steps away)]
```

Deduplicated — each caller appears once at its shortest distance.

#### `find_callees_recursive`

What does this call, directly or indirectly?

```
Input:  {"name": "main", "project_root?": "/path/to/project", "max_depth?": 5, "limit?": 50}
Output: [{"name": "spi_init", "kind": "function", "file": "/path/src/spi.c", "depth": 1},
         {"name": "spi_transfer", "kind": "function", "file": "/path/src/spi.c", "depth": 2}, …]
```

#### `find_dead_code`

Functions defined but never called. Returns two categories:

```
Input:  {"project_root?": "/path/to/project", "limit?": 100, "exclude_paths?": ["zephyr/%", "mbed-os/%"], "project_only?": true}
Output: [
  {"name": "orphan_fn", "kind": "function", "file": "/path/src/utils.c",
   "signature": "void orphan_fn()", "line": 200,
   "status": "dead", "reason": "no references found — likely unused"},
  {"name": "handler_timeout", "kind": "function", "file": "/path/src/main.c",
   "signature": "void handler_timeout()", "line": 25,
   "status": "possibly_dead",
   "reason": "assigned as function pointer but call sites unresolved",
   "indirect_refs": "src/main.c:60"},
  …
]
```

**`"dead"`** — no references at all (neither calls nor function pointer
assignments). Likely unused.

**`"possibly_dead"`** — the function is assigned to a function pointer
(``ref_kind="indirect"``) but no call site through that pointer was resolved.
This means the function MIGHT be called through unindexed code or a
type-erased API. Treat this as uncertain, not as confirmed dead code.
Verify each hit with ``find_indirect_targets`` before deleting.

Expect additional false positives from entry points (`main`), ISRs,
virtual method overrides, constructors called via factories, and
weak-aliased symbols. Only definitions with
`kind IN ('function', 'method', 'constructor', 'destructor')` are checked.
Use `exclude_paths` to skip vendor SDK code (LIKE patterns — `%` matches
any suffix).

#### `find_hotspots`

Most-called functions ranked by caller count.

```
Input:  {"project_root?": "/path/to/project", "limit?": 20, "project_only?": true, "exclude_paths?": ["lib/%"]}
Output: [{"name": "log_debug", "kind": "function", "caller_count": 147, …},
         {"name": "millis", "kind": "function", "caller_count": 89, …}, …]
```

#### `find_wrapper_callers`

Find wrapper classes that call methods of a driver class. Useful for
understanding adapter/wrapper architecture.

```
Input:  {"class_name": "UART_DRIVER", "project_root?": "/path/to/project", "limit?": 50}
Output: [{"wrapper_class": "UART", "wrapper_method": "send",
          "driver_method": "UART_DRIVER::send", "file": "/path/src/uart.cpp",
          "line": 45, "ref_kind": "call"}, …]
```

Pass a fully-qualified class name (`hal::UART_DRIVER`) or just the bare name
(`UART_DRIVER`). Results are grouped by wrapper class.

#### `find_indirect_call_sites`

Find indirect call sites where a function pointer field or variable is invoked.

```
Input:  {"name": "onData", "project_root?": "/path/to/project", "limit?": 50}
Output: [{"file": "/path/src/main.c", "line": 36, "expr_text": "drv . onData",
          "target_usr": "c:@S@Driver@FI@onData", "target_name": "onData",
          "fn_ptr_type": "void (*)(unsigned char *, int)",
          "caller": "test_assign", "caller_kind": "function"}]
```

Returns locations where a function pointer is called through a field access
(``driver.onData(buf, len)``) or variable (``stored_callback(42)``). Use
this to answer *"where is this function pointer invoked?"* — complement with
``find_indirect_targets`` for *"which functions are assigned to this field?"*

Uses three-tier name resolution: exact name, exact qualified, suffix LIKE.

#### `find_indirect_targets`

Find functions assigned to a function pointer field, variable, or parameter.

```
Input:  {"name": "onData", "project_root?": "/path/to/project", "limit?": 50}
Output: [{"rhs_name": "handler_data", "rhs_qname": "handler_data",
          "fn_ptr_type": "void (*)(unsigned char *, int)",
          "method": "assignment",
          "assign_file": "/path/src/main.c", "assign_line": 14,
          "assign_caller": "test_assign",
          "call_file": "/path/src/main.c", "call_line": 16,
          "call_expr_text": "drv . onData"}]
```

Links assignment sites (``driver.onData = &handler``) to call sites
(``driver.onData(buf, len)``) via the field's USR. Shows the execution
flow: *handler is assigned to onData at line 14, and onData is called at
line 16 → handler may run at that call site.*

When a function is assigned but no call site is found, ``call_file`` and
``call_line`` are ``null`` — the assignment exists but the invocation may be
in unindexed code. The ``method`` field indicates how the assignment was
detected: ``"assignment"`` (direct field assignment), ``"call_arg"``
(passed as callback argument), ``"var_init"`` (variable initializer), or
``"init_list"`` (struct/array designated initializer).

For the reverse query — where is this field called — use
``find_indirect_call_sites``.

#### `trace_data_flow`

Trace how data of a given type flows to a target function. **Experimental.**

```
Input:  {"type_name": "SensorData", "to_symbol": "uart_send", "project_root?": "/path/to/project", "max_depth?": 8, "limit?": 15}
Output: [{"source_function": "sensor_read", "source_type": "SensorData",
          "file": "/path/src/sensor.cpp", "line": 120,
          "call_path": "sensor_read → pack_payload → uart_send"}, …]
```

Finds functions whose signature mentions `type_name`, then looks for call paths
from those functions to `to_symbol`. Does NOT resolve type transformations
(e.g. CBOR encoding). Best used together with `find_call_path` to verify
specific paths.

### Class analysis

#### `get_inheritance_chain`

Return the C++ inheritance hierarchy for a class or struct — direct bases
(what this inherits from) and direct derived classes (what inherits from
this), with access level and virtual flag.

```
Input:  {"class_name": "UART_DRIVER", "project_root?": "/path/to/project", "transitive?": false, "max_depth?": 10}
Output: {"name": "UART_DRIVER", "qualified_name": "hal::UART_DRIVER",
         "kind": "class", "file": "/path/src/UART_DRIVER.h", "line": 45,
         "bases": [{"name": "SerialBase", "usr": "c:@...", "access": "public",
                     "is_virtual": false, "file": "/path/src/SerialBase.h"}],
         "derived": [{"name": "UART", "usr": "c:@...", "access": "public",
                       "is_virtual": false, "file": "/path/src/UART.h"}]}
```

When `transitive: true`, adds `all_bases` (ancestors BFS) and `all_derived`
(descendants BFS) with `depth` and cycle detection for diamond inheritance.

The `class_name` can be a bare name (`UART_DRIVER`) or qualified
(`hal::UART_DRIVER`). Only classes and structs are valid targets.

#### `get_class_members`

Return all methods, fields, and nested types of a class/struct grouped by kind.

```
Input:  {"class_name": "ModemManager", "project_root?": "/path/to/project"}
Output: {"name": "ModemManager", "qualified_name": "ns::ModemManager",
         "kind": "class", "file": "/path/src/modem.h", "line": 120,
         "members": {
             "method": [{"name": "send", "qualified_name": "ns::ModemManager::send",
                          "signature": "int send(const uint8_t*,size_t)",
                          "is_virtual": false, "is_pure_virtual": false, "line": 150}, …],
             "field": [{"name": "_baudrate", …}, …],
             "constructor": [{"name": "ModemManager", …}],
             "enum": [{"name": "State", …}]
         },
         "member_count": 12}
```

Members are ordered by kind then name. Shows the full API surface without
opening the header file. Works for C structs too — they just won't have
methods. Returns `member_count: 0` for indexes predating this feature.

#### `get_template_instances`

Find concrete instantiations of a class or function template.

```
Input:  {"template_name": "std::vector", "project_root?": "/path/to/project", "limit?": 50}
Output: {"template_name": "std::vector", "template_usr": "c:@...",
         "instance_count": 3,
         "instances": [{"name": "vector", "qualified_name": "std::vector<int>",
                         "kind": "class", "file": "/path/src/main.cpp", "line": 42,
                         "signature": "class vector<int>", "is_definition": true}, …]}
```

Returns empty `instances` when the template is declared but never instanciated
in project code.  Works for both class templates (`CXCursor_CLASS_TEMPLATE`)
and function templates (`CXCursor_FUNCTION_TEMPLATE`).  The template is
resolved by name (exact or qualified) just like `lookup_symbol`.

#### `get_method_overrides`

Show which virtual methods override which base-class methods, and which
derived-class methods override this one.

```
Input:  {"method_name": "UART_DRIVER::send", "project_root?": "/path/to/project"}
Output: {"method": "send", "qualified_name": "hal::UART_DRIVER::send",
         "kind": "method", "file": "/path/src/UART_DRIVER.cpp", "line": 88,
         "is_virtual": true, "is_pure_virtual": false,
         "overrides": [{"name": "send", "qualified_name": "SerialBase::send",
                         "usr": "c:@...", "file": "/path/src/SerialBase.h"}],
         "overridden_by": []}
```

Uses the `overrides` table built during indexing.  Parameter-type comparison
filters out accidental name collisions (overloads, not overrides).  For a
non-virtual method or a method whose class has no ancestors, both
`overrides` and `overridden_by` are empty lists.

### Index maintenance

#### `get_active_build`

Check index health — call at session start.

```
Input:  {"project_root?": "/path/to/project"}
Output: {"config_hash": "a1b2…", "project_id": "c3d4…", "project_root": "/path/to/project",
         "build_system": "zephyr", "compile_commands": "/path/build/compile_commands.json",
         "indexed_at": "2026-06-05T09:35:18", "symbol_count": 12430, "file_count": 1502,
         "reference_count": 8900, "modified_files_count": 3,
         "header_affected_tus": 0, "deps_verification": "full",
         "schema_version": 84935291, "current_schema": 84935291,
         "analyzed_symbols": 8450, "unanalyzed_symbols": 120,
         "analysis_model": "qwen2.5-coder:14b",
         "bg_reindex_running": false, "reindex_progress": null,
         "status": "ready", "reindex_needed": false, "reindex_reasons": [],
         "index_message": "Index is fully up to date (12430 symbols)"}
```

**Read-only.** No side effects — does not spawn background tasks.
Background reindex is managed by the server startup thread and the file
watcher. ``bg_reindex_running`` and ``reindex_progress`` report whether
a background reindex is active and its last log line.

**``status`` field (use for decision-making):**

| Status | Meaning | What to do |
|--------|---------|------------|
| ``"ready"`` | Fully up to date, no issues | Continue normally |
| ``"reindexing"`` | Background reindex in progress | Index is still usable — continue normally |
| ``"reindex_needed"`` | compile_commands.json changed or schema mismatch | Queries still work, but schedule ``fw-context index`` |
| ``"no_index"`` | No build config indexed | Use other tools |
| ``"error"`` | DB corruption or access error | Use other tools |

``modified_files_count`` is cached for 30 seconds (calls within that window
return the same value). ``header_affected_tus`` reports how many translation
units have stale header dependencies — non-zero when headers changed since the
last index with ``deps_verification: "full"``. ``deps_verification`` is
``"full"`` (``.d`` dependency files available — header staleness is tracked),
``"partial"`` (some ``.d`` files exist but not all), or ``"none"``
(no dependency tracking — header changes cannot be detected).
``unanalyzed_symbols`` counts definition symbols that still need LLM analysis
(zero when analysis is disabled, empty when all symbols are analyzed).

#### `reindex_file`

Re-index a single file after editing.

```
Input:  {"file_path": "/abs/path/to/src/main.c", "project_root?": "/path/to/project"}
Output: {"file": "/abs/path/to/src/main.c", "translation_units": 1,
         "symbols_updated": 28, "elapsed_s": 2.5}
```

**Limitation:** The file must appear in `compile_commands.json`. Headers are
re-indexed via the translation unit that includes them — a single header
included by multiple TUs may need a full `fw-context index` for completeness.

#### `reindex_file_impl`

Shared implementation used by ``reindex_file`` (public tool, full analysis)
and background auto-reindex (fast path, no LLM). Prefer ``reindex_file``
for interactive use; use this only when you need to control
`with_analysis` explicitly.

```
Input:  {"file_path": "/abs/path/to/src/main.c", "project_root?": "/path/to/project", "with_analysis?": true}
Output: {"file": "/abs/path/to/src/main.c", "translation_units": 1,
         "symbols_updated": 28, "elapsed_s": 2.5,
         "analysis_updated": 12, "analysis_warning": null}
```

When ``with_analysis=True`` (default), also regenerates LLM symbol analysis
and method override relationships — slower but produces a fully up-to-date
index. Set ``False`` for a fast symbol-only update. Requires an existing
index; the file must appear in ``compile_commands.json``.

#### `reset_index`

Delete the index. Always dry-run first, then `confirm: true`.

```
Input:  {"project_root?": "/path/to/project", "confirm": false}     → {"action": "dry_run", "symbol_count": 8586, …}
Input:  {"project_root?": "/path/to/project", "confirm": true}      → {"action": "deleted", "message": "…"}
```

#### `list_projects`

List all indexed firmware projects.

```
Input:  {"project_root?": "/path/to/project"}
Output: [{"project_id": "a1b2…", "name": "my-zephyr-app", "root_path": "/path",
          "build_system": "zephyr", "symbol_count": 12430, "file_count": 1502,
          "indexed_at": "2026-06-05T09:35:18", "stale": false, "db": "…"}, …]
```

#### `get_project_info`

Look up project metadata from the global project registry.

```
Input:  {"project_id": "a1b2c3d4…"}
Output: {"project_id": "a1b2c3d4…", "name": "my-zephyr-app",
         "project_type": "zephyr", "root_path": "/path/to/project",
         "created_at": "2026-06-01T12:00:00", "updated_at": "2026-06-05T09:35:18"}
```

Looks up the global project registry at ``~/.fw-context/projects.db``. Use this
to identify a project from its UUID4 — find out what build system it uses, its
name, and where it was last indexed. Returns an error when the `project_id` is
not registered.

#### `check_ollama`

Verify Ollama availability. Call before `smart_search`, `semantic_search`,
or when `explain_symbol` needs on-demand analysis (no pre-computed analysis
available).

```
Input:  {"project_root?": "/path/to/project"}
Output: {"status": "ok", "ollama_running": true, "ollama_enabled": true,
         "configured_model": "qwen2.5-coder:14b", "num_ctx": 16384,
         "installed_models": ["qwen2.5-coder:14b", "mxbai-embed-large:latest"], …}
```

Returns `status: "disabled"` when `[llm] enabled = false` — no Ollama needed.

Note: `explain_symbol` with pre-computed analysis (default) returns instantly
and does not require Ollama at query time. `num_ctx` is 16384 by default to
accommodate full function bodies during analysis generation.

### MCP Resources

Resources are read-only URI-addressable endpoints that return structured
content (Markdown or JSON). Unlike tools, they take no JSON-RPC parameters
(except `symbols/{name}` which takes a name path segment).

#### `fw-context://stats`

Markdown summary of all indexed projects — symbol counts, file counts,
freshness, and index timestamps.

```
URI:    fw-context://stats
Output: # fw-context — 3 project(s)

        - **my-zephyr-app** (a1b2…) — 12430 symbols, 1502 files, indexed 2026-06-05T09:35:18, ✓ fresh
        - **my-pio-app** (c3d4…) — 8586 symbols, 952 files, indexed 2026-06-04T16:20:45, ⚠ stale
```

Aggregates across all project databases under `~/.fw-context/index/`.
Projects with errors are shown with an ERROR marker.

#### `fw-context://projects`

Same data as [`list_projects`](#list_projects), serialized as indented JSON.

```
URI:    fw-context://projects
Output: [{"project_id": "a1b2…", "name": "my-zephyr-app", "symbol_count": 12430, …}, …]
```

#### `fw-context://symbols/{name}`

Definition source of a symbol rendered as a Markdown document. Uses the same
lookup as [`get_source`](#get_source).

```
URI:    fw-context://symbols/uart_init
Output: # uart_init

        - **qualified:** `drv::uart_init`
        - **kind:** function
        - **file:** `/path/src/uart.c:42`
        - **signature:** `void uart_init(int baudrate)`

        ```cpp
        void uart_init(int baudrate) {
          …
        }
        ```
```

When the symbol is not found, returns a JSON error object.

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

### Staleness recovery on query

`search_code`, `lookup_symbol`, and `semantic_search` detect stale files in
their results (on-disk mtime > stored mtime).  When stale files are found,
the tools append a warning to the response and the server's background
reindex subprocess picks up the work — no query blocks waiting for
reindex.  The file watcher independently handles recently edited files
within 500 ms of save, so stale results in practice are rare.

For manual recovery, use `reindex_file` or `fw-context index`.

### Index building (detailed)

```
1. Read compile_commands.json → extract translation units (file + compiler args)

2. Auto-detect source_roots:
   Scan for src/, lib/, app/, include/, modules/
   Add framework dirs (zephyr/, mbed-os/)
   Add top-level dirs from compile_commands.json entries

3. For each translation unit (sequential, per-TU write lock):
   Parse with libclang using exact compiler flags (-I, -D, -std, --target)
   Traverse AST → extract symbols within source_roots
   Category: function, method, class, enum, typedef, variable, field, …
   Extract cross-references within source_roots (on by default; skip with --no-refs)
   Extract macros via clang -dM -E (preprocessor dump; stored in macro_defs table)
   Release write lock between TUs — manual operations (``reindex_file``)
   can interleave via the pause marker mechanism without blocking.

4. Write to SQLite (atomic per-TU transaction):
   Delete old symbols for this TU
   Insert new symbols + FTS5 triggers
   Insert references (on by default; skip with --no-refs)
   Insert macros with expanded values
   Generate + store vector embeddings (on by default; skip with --no-embeddings)

5. Write build metadata:
   compile_commands.json hash, file mtimes, symbol/file/ref/macro counts
```

### Vector search

When embeddings are generated during indexing (`fw-context index --embeddings`),
symbols are stored in two tables:

| Table | Storage | Query method |
|-------|---------|-------------|
| `embeddings` | BLOB (4 bytes × dim floats — 4096 for mxbai 1024-dim, 16384 for qwen3 4096-dim) | Legacy brute-force (Python) |
| `vec_symbols` (vec0) | sqlite-vec virtual table | KNN via `MATCH` (C implementation) |

The `EmbeddingPhase` prefers `vec0` when available (index built after this
feature), falls back to BLOB brute-force for older indexes, and operates as
a **hybrid re-rank** when FTS5 results already exist — avoiding duplicate
searches and expensive merging.

---

## Auto-detection of build system

| Ecosystem | Detected by | compile_commands.json location |
|-----------|------------|-------------------------------|
| **Zephyr** | `west.yml` or `prj.conf` | `build/compile_commands.json` |
| **PlatformIO** | `platformio.ini` | Project root |
| **Mbed OS** | `mbed-os/` directory or `mbed_app.json` | Project root |
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

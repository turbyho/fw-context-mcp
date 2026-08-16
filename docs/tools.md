# Tools Reference

This is the complete reference for all fw-context CLI commands and MCP server tools.

## CLI commands

### `fw-context index`

Build or update the symbol index from `compile_commands.json`.

> **Incremental by default.** `fw-context` reuses an existing
> `compile_commands.json`, and re-parses only the changed files. Use
> `--build` to force a clean build and a full re-index when needed, for
> example after an SDK update or a build system change. Use `--force` to
> bypass the mtime checks and force a re-index of all files, embeddings,
> LLM analysis, overrides, and caches, without rebuilding, for example
> after a schema change or a tool update.

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
| `--no-clean` | off | With `--build`: skip the clean step, and do an incremental build |
| `--source-roots DIR…` | auto-detected | Directories to index symbols from |
| `--name NAME` | directory name | Project name override |
| `--no-refs` | off | Skip cross-reference / call graph indexing |
| `--no-embeddings` | off | Skip embedding generation |
| `--no-analyze` | off | Skip LLM symbol analysis |
| `--analyze` | on | Force LLM symbol analysis (negates --no-analyze) |
| `--force` | off | Force re-index of all files, embeddings, LLM analysis, overrides, PageRank, and hotspot cache (bypasses mtime checks) |
| `--variant NAME` | all variants | Restrict indexing to one build variant (name from `[[build.variants]]`) |
| `--variants A,B` | all variants | Comma-separated list of build variants to index |
| `--image NAME` | all images | Restrict indexing to one sysbuild image |
| `--exclude-image NAME` | none | Exclude a sysbuild image from indexing (repeatable) |
| `-v` | off | Verbose progress output |

For a multi-variant project, `fw-context index --build` builds and indexes
every variant. Use `--variant`, `--variants`, `--image`, or
`--exclude-image` to narrow the run. See
[Build Configuration → Multi-project and multi-image builds](build.md).

**Generating `compile_commands.json`:**

| Build system | Command |
|-------------|---------|
| **Zephyr** | `west build -b <board> -- -DCMAKE_EXPORT_COMPILE_COMMANDS=ON` |
| **PlatformIO** | `pio run --target compiledb` |
| **Mbed OS** | `bear -- mbed compile --profile release` |
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

### `fw-context db`

Manage the index database.

```bash
fw-context db list                   # list all builds
fw-context db stats <hash>           # show statistics for a build
fw-context db delete <hash>          # delete a specific build
fw-context db delete --all           # delete the entire index
fw-context db cleanup                # remove orphaned compile_commands artifacts
```

#### `fw-context db list`

List all builds for a project, with per-build statistics. The active build
shows with a `*` marker.

```bash
$ fw-context db list
Project: my-zephyr-app  path=/home/user/zephyr-app

*a1b2c3d4e5f6  main: add UART driver
  First indexed:  2026-06-01 12:00:00      Last indexed:    2026-06-05 09:35:18
  Symbols:        12,430                   Files:           1,502  Refs:    8,900

7f8e9d0c1b2a  feature/spi: WIP SPI rewrite
  First indexed:  2026-06-03 14:22:10      Last indexed:    2026-06-04 18:10:05
  Symbols:        12,318                   Files:           1,498  Refs:    8,756

* = active build (a1b2c3d4e5f6...)
```

#### `fw-context db stats`

Show detailed statistics for a specific build: symbols by kind, LLM
analysis coverage, embedding count, reference count, PageRank coverage,
and more.

```bash
$ fw-context db stats a1b2c3d4e5f6
Build:          a1b2c3d4e5f6...
  Description:  main: add UART driver
  First indexed: 2026-06-01 12:00:00
  Last indexed: 2026-06-05 09:35:18
  ...

Symbols by kind:
  function                  5 212
  method                    3 108
  class                       482
  ...
  TOTAL                    12 430
  Definitions (is_definition=1): 8 450

LLM analysis:   8 450 / 8 450 definitions analyzed
  Coverage:     100%
  Unanalyzed:   0
Embeddings:     12 430 symbols with embeddings
Files:          1 502
References:     8 900
Macros:         1 234
Indirect calls: 156
FP assignments: 89
Overrides:      42
Hotspot cache:  20
PageRank cov:   12 430 / 12 430 symbols
```

#### `fw-context db delete`

Delete a specific build, by its config hash. You can resolve a hash with
the first 12 or more characters.

```bash
fw-context db delete a1b2c3d4e5f6       # delete a build (interactive confirmation)
fw-context db delete a1b2c3d4e5f6 -y    # skip confirmation
fw-context db delete --all              # delete the entire index
fw-context db delete --all -y           # delete all without confirmation
```

You cannot delete the active build without `--force`. You cannot delete
the only build: use `--all` instead.

#### `fw-context db cleanup`

Remove orphaned `compile_commands.<hash>.json` artifacts that have no
matching build in the index.

```bash
fw-context db cleanup                  # clean up for current project
fw-context db cleanup --project /path  # clean up for specific project
```

### `fw-context init`

Provision a project in one command: audit and fix dependencies, generate
`compile_commands.json` when possible, register AI assistants, and print
a checklist of the remaining manual steps.

```bash
fw-context init                         # full provisioning (all detected assistants)
fw-context init --quick                 # skip AI tool registration only
fw-context init --tool claude-code      # specific tool only
fw-context init --dry-run               # preview without writing
fw-context init --force                 # overwrite collisions
fw-context init --list-tools            # show supported tools
fw-context init --skip-doctor           # skip the dependency audit
fw-context init --skip-build            # skip compile_commands.json generation
fw-context init --non-interactive       # disable prompts (CI/pipe)
fw-context init --name NAME             # project name in the global registry
```

`fw-context init` runs these steps, in order:

1. **Project ID** — generate the ID and register the project globally.
2. **Dependencies** — audit and auto-fix fixable issues, such as missing
   pip packages. Model pulls (`ollama pull`) never run in this step.
3. **Build** — detect the build system and generate
   `compile_commands.json` when the project is buildable.
4. **AI tools** — register the MCP server and inject instructions, skills,
   and agents.
5. **Checklist** — print the remaining manual steps, grouped by category.

**Interactive fallback.** When build-system detection fails (no project
markers, no board, no FQBN), `fw-context init` asks for the missing value.
Each prompt has a default; press Enter to accept it. The answers are
written to `.fw-context/config.toml`. In a pipe, or with
`--non-interactive`, the missing value goes to the checklist instead.

The command is idempotent. Re-running it asks `Change? [y/N]` for each
configured value, so you can press Enter to keep the current setup.

`--quick` skips only step 4 (AI tool registration). It still creates the
project ID, the config files, the `.gitignore` entries, and runs the
dependency audit, the build, and the checklist.

| Tool | ID | Scope |
|------|----|-------|
| Claude Code | `claude-code` | global (`~/.claude/`) |
| OpenCode | `opencode` | global (`~/.config/opencode/`) |

### `fw-context quickstart`

Alias for `fw-context init --quick`. Provision the project, but skip AI
tool registration only. Use this command for CI, or when the AI tools
already work.

```bash
fw-context quickstart                  # deps + build + config + checklist
```

This command shares the `--project`, `--dry-run`, `--skip-doctor`,
`--skip-build`, `--non-interactive`, and `--name` flags with
`fw-context init`.

### `fw-context init-variants`

Manage `[[build.variants]]` in `.fw-context/config.toml`, for a project
that is already initialized. This command adds, removes, or lists build
variants, without re-running the agent and dependency provisioning.

```bash
fw-context init-variants list                          # list declared variants
fw-context init-variants add --name <name> --board <board>
fw-context init-variants add --name <name> --board <board> --description "text"
fw-context init-variants add --name <name> --build-dir build/<name>
fw-context init-variants add --name <name> --env ZBOX_ENV=DEV
fw-context init-variants remove <name>
```

| Subcommand | Description |
|-----------|-------------|
| `list` | Read `[[build.variants]]` from `config.toml` (what the config declares) |
| `add` | Append one variant to `config.toml` |
| `remove` | Remove one variant from `config.toml` |

`list` shows what the config declares. It does not show what fw-context
has indexed. After you add or remove a variant, run `fw-context index
--build` to index the new set. The MCP `list_variants` tool shows what is
actually indexed.

### `fw-context export`

Export the symbol index as portable JSON.

```bash
fw-context export                     # stdout
fw-context export -o index.json       # file
fw-context export --no-refs           # symbols only
```

The format is `fw-context-export/1`. This format is suitable for sharing
between machines, for debugging, or as input to other tools.

### `fw-context analyze`

Run LLM symbol analysis on an already-indexed project. This command is
useful when the index was built with `--no-analyze` and you want to add
analysis later. This command is also useful when you want to regenerate
the analysis for all symbols.

```bash
fw-context analyze                        # analyze current project
fw-context analyze --project /path        # specific project
```

fw-context generates the analysis for each function or method. fw-context
uses the full function body (through the libclang extent) and the callee
names, as supplementary context. fw-context stores the results in the
`llm_analysis` table, and denormalizes the results into the FTS5 index. So
symbols become searchable by their purpose, not only by their name.

Configure with `[llm] analyze_symbols`, `[llm] analysis_model`, and
`[llm] num_ctx` in `~/.fw-context/config.toml`.

### `fw-context cache stats`

Show cache statistics for one or both tiers. The `--remote` flag queries
the server in real time, and shows a per-model breakdown with percentages.
When you run this command inside a project directory that has an existing
index, this flag also shows how many of that project's symbols the server
has cached.

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

The `--remote` flag reads all the content hashes from the project's
`llm_analysis_cache` table, and sends the hashes to the server's
`POST /cache/clear` endpoint. This flag requires `[cache_server]` in
`.fw-context/local.toml`, and a token with `can_write` permission.

All clear operations are safe. fw-context rebuilds the cache entries
automatically, on the next `fw-context index --analyze` or
`fw-context analyze` run.

### `fw-context cache push`

Push all local cache entries to the remote cache server. By default, this
command uses overwrite mode, with `X-Cache-Overwrite`. So a newer local
entry replaces an older remote entry.

```bash
fw-context cache push                       # push all, batch size from config (100)
fw-context cache push --batch 500           # larger batches for faster transfer
```

This command requires `[cache_server]`, configured with `can_write` and
`can_overwrite` permissions. This command reports progress in batches.

### `fw-context cache remote-init`

Interactive wizard to configure the remote cache server connection. This
wizard prompts you for the URL and the token. This wizard verifies
connectivity, and writes `[cache_server]` to `.fw-context/local.toml`.

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

### `fw-context finetune`

Fine-tune the embedding model on project code. This command generates
synthetic training pairs from the symbol index, and trains the embedding
model to produce better vector representations of your codebase.

```bash
fw-context finetune                          # fine-tune on current project
fw-context finetune --project /path          # fine-tune on specific project
fw-context finetune --sample-limit 500       # limit training pairs
fw-context finetune --epochs 5               # number of training epochs
fw-context finetune --batch-size 32          # training batch size
```

This command requires the `st` extra: `pip install fw-context-mcp[st]`.
You can control the number of synthetic pairs with `--sample-limit`
(default 2000). More epochs and pairs give better quality, but take
more time.

### `fw-context doctor`

Audit the installation: check dependencies, file permissions, database
integrity, and configuration. This command reports issues, and can
attempt automatic repairs.

```bash
fw-context doctor                    # audit, report issues
fw-context doctor --fix              # audit and attempt repair
fw-context doctor --json             # machine-readable JSON output
fw-context doctor --project /path    # audit specific project
```

`fw-context doctor` checks:

- The Python version and required packages
- The `tomli-w` package, which writes the config files
- The Ollama installation and model availability
- The libclang shared library and its version
- The SQLite version and FTS5 support
- Index database integrity and schema compatibility
- Configuration file syntax and key validity
- File permissions for the index directory and config files

When you run this command with `--fix`, it attempts to repair fixable
issues automatically. The command reports issues that it cannot fix, and
tells you how to fix them manually.

### `fw-context watch`

Manage the background watcher daemon that auto-reindexes changed source files.

```bash
fw-context watch status              # show daemon status
fw-context watch restart             # restart the daemon
fw-context watch restart --project /path  # specific project
```

#### `fw-context watch status`

Show the watcher daemon status for the current project: whether the daemon
is running, its PID and uptime, the modified file count, and whether a
background reindex is in progress.

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

Stop the current watcher daemon, with `SIGTERM`. If the daemon does not
stop, this command falls back to `SIGKILL` after 3 seconds. This command
cleans up leftover socket, pid, and lock files, and spawns a fresh daemon.
This command verifies that the new daemon responds to a ping, before it
returns.

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

Your AI assistant calls these tools over JSON-RPC. Each tool opens the
database, runs its query, and closes the database. These tools use no
persistent connections.

All tools accept an optional `project_root` parameter, which defaults to a
value that fw-context detects automatically from the current working
directory. Every input block below shows this parameter, but you typically
omit it, because the automatic detection handles the common case.

**Multi-variant projects.** For a project with `[[build.variants]]`, every
query tool also accepts `variant` and `image` parameters. When you omit
`variant`, fw-context uses `[build] default_variant`, or returns an error
that lists the valid names. Set `variant` to `"*"` to query every variant.
A multi-variant result annotates each record with its `variant` and
`image`. Call `get_active_build` to see the variants and images that
fw-context has indexed.

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
When fw-context generates LLM analysis (with `fw-context index --analyze`),
results include `summary`, `inputs`, and `outputs` fields, with
plain-English descriptions.

**Progressive relaxation:** when the initial FTS5 search returns nothing, the
tool automatically broadens the search in up to six steps:

1. *FTS5 with kind filter* — the original query with the user-provided `kind`
   constraint.
2. *FTS5 without kind filter* — drops the `kind` constraint (users often guess
   the wrong kind for a symbol).
3. *`name_tokens` substring match* — searches the pre-computed CamelCase/
   snake_case token column (for example, fw-context indexes `BuildType` as
   `"build type"`). Requires at least N-1 of N query terms to match.
4. *Single-term docstring LIKE* — when you give only one query term, and the
   token-based steps find nothing, does a raw LIKE search over the
   docstring column, to catch terms that the FTS5 tokenizer did not match.
5. *Individual term FTS5* — searches each query word separately and merges
   the results.
6. *Macro FTS5 fallback* — searches the `macros_fts` table for matching
   `#define` names and values (`kind="macro"`, `_fallback="macros_fts"`).

Results from fallback steps carry `_fallback` indicating which method
succeeded: `"fts5"`, `"name_tokens_like"`, `"docstring_like"`,
`"individual_terms"`, or `"macros_fts"`. Results from the primary FTS5 path have
`"_fallback": "fts5+kind"` or omit the field.

**FTS5 syntax:**
- `uart*` — prefix wildcard
- `"spi transfer"` — exact phrase match
- `modem init` — both terms (AND for `search_code`). fw-context treats an
  underscore as a word separator, so `modem_init` means `modem AND init`
- **For `search_bodies` and `search_content`:** fw-context OR-joins bare
  multi-word queries, with each term prefix-wildcarded. Prefer single-word
  queries

**Kind filter:** `function`, `method`, `constructor`, `destructor`, `class`, `struct`, `union`, `enum`, `enum_constant`, `typedef`, `variable`, `field`, `namespace`

#### `search_bodies`

Find patterns in C/C++ function **bodies** — the implementation code inside `{ }`.

Searches **only** the text between `{` and `}` of function and method
definitions. Does **not** search file-scope constructs, such as
`extern "C"`, `#include`, `#define`, or type declarations in headers.

```
Input:  {"query": "attach", "project_root?": "/path/to/project", "kind?": "function", "limit?": 20, "project_only?": true}
Output: [{"name": "setup", "qualified_name": "setup", "kind": "function",
          "file": "/path/src/main.cpp", "line": 55, "is_definition": true,
          "signature": "void setup()",
          "_match_snippet": "…_timeout.<b>attach</b>(callback(&led_blink, 1000))…",
          "source": "… (function body, truncated at 2000 chars)"}]
```

- **When to use `search_bodies` vs. `search_code` vs. `search_content`:**
  - `search_bodies` — patterns in function **bodies** (what the code does):
    `.attach(`, `NVIC_SetVector(`, `.rise(`, `.fall(`, `callback(&`
  - `search_content` — patterns anywhere in **files**:
    `extern "C"`, `InterruptIn`, `#define`, type declarations
  - `search_code` — find symbols by **name**:
    `modem init`, `interrupt handler`

**FTS5 query tips for `search_bodies`:** fw-context OR-joins bare
multi-word queries, with each term prefix-wildcarded: `"attach callback"`
becomes `attach* OR callback*`. Prefer single-word queries for broad
matching. For example, `"attach"` finds all `.attach(...)` patterns
across the codebase.

Results include `_match_snippet` — a highlighted excerpt showing each
match in context with `<b>…</b>` tags. Project code sorts before vendor
code. Set `project_only=True` to filter to application code only.

#### `search_content`

Find patterns in **full file content** — file-scope + function bodies, not
limited to function bodies.

Searches **ifdef-filtered** file text: only the code that actually
compiles for the current build configuration. fw-context replaces an
inactive `#ifdef` branch with blank lines, and keeps the original line
numbers.

```
Input:  {"query": "InterruptIn", "project_root?": "/path/to/project", "limit?": 20, "project_only?": false}
Output: [{"file": "/path/src/main.cpp", "language": "cpp",
          "mtime": "2026-06-05T09:35:18", "_match_snippet": "…InterruptIn…"}]
```

Covers file-scope constructs that `search_bodies` cannot see: `extern "C"`,
type declarations in headers, `#include`, `#define`, global variables, and
namespace blocks. Results are file-level, with one entry for each matching
file. Use `search_bodies` for per-function detail.

When `files_fts` is missing, in a legacy index, this tool falls back to a
LIKE search on `files.content`. The results include `_fallback: "like"`,
and no snippet highlighting. Run `fw-context index` to upgrade.

**FTS5 query tips for `search_content`:** fw-context OR-joins bare
multi-word queries, with a prefix wildcard on each term. Prefer
single-word queries.

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

fw-context sorts definitions before declarations. Use `exact: true` for an
exact name match. The default is a prefix match: `uart` matches
`uart_init`, `uart_write`, and other names with that prefix.

fw-context extracts macros with `clang -dM -E`, using the compiler flags
from `compile_commands.json`. So a macro with an `#ifdef` condition
resolves correctly for the indexed build configuration. Enum constants
include `enum_value` when the value is not `None`.

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

When you disable Ollama (`[llm] enabled = false`), this tool falls back to
a word-split FTS5 search. Phase 0 auto-translates a non-English query.

#### `semantic_search`

Concept search using pre-computed symbol embeddings. Finds symbols that
are conceptually related to a natural-language query, even when the
query words do not appear literally in the code.

```
Input:  {"query": "parcel locker state machine", "project_root?": "/path/to/project", "threshold?": 0.60, "limit?": 20}
Output: [{"name": "set_shipment", "qualified_name": "Locker::set_shipment",
          "_similarity": 0.72, "_method": "embedding", …}, …]
```

Uses cosine similarity over variable-dimension embeddings (generated during
`fw-context index --embeddings`). Models: mxbai-embed-large → 1024-dim,
qwen3-embedding → 4096-dim. **When to prefer over `search_code`:**
conceptual queries ("power consumption" → `get_load_power`) where keywords
do not match. **When to prefer `search_code`:** known keywords or symbol
names (`"fram_write"`, `"cbor encode"`).

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

#### `find_variables`

Find C/C++ variables by name or prefix, and trace who reads or writes them
through the call graph. Splits variables into global (`varglobal`: file,
namespace, or class scope) and local (`varlocal`: inside a function body).

```
Input:  {"name": "g_", "project_root?": "/path/to/project", "kind?": "varglobal", "limit?": 20}
Output: [{"name": "g_debug_level", "qualified_name": "g_debug_level",
          "kind": "varglobal", "file": "/path/src/logging.c", "line": 15,
          "signature": "int g_debug_level",
          "enclosing_function": "<file scope>", "enclosing_class": "",
          "references": [
            {"function": "log_init", "file": "/path/src/logging.c", "line": 42, "ref_kind": "ref"},
            {"function": "log_write", "file": "/path/src/logging.c", "line": 68, "ref_kind": "ref"}
          ]}, …]
```

Each result includes a type signature, for example `"bool timeSet"` or
`"const IPAddress modbus_ip"`. Each result also includes the enclosing
function for a local variable (`"<file scope>"` for a global variable),
and the enclosing class for a static member. Each result includes a
`references` list, showing every function that reads or writes the
variable. This list uses the same `ref_kind` values as `find_references`:
`"call"`, `"ref"`, `"member"`.

Use `find_variables` when you need to:
- understand shared state
- find who modifies a global variable
- trace side effects
- distinguish important globals from loop counters

For a general symbol search, use `search_code` or `lookup_symbol`. For all
references to a specific variable, including reads in expressions, use
`find_references`.

The `kind` parameter accepts `"varglobal"`, `"varlocal"`, or `None` for
both. Results include legacy indexes that still use `kind="variable"`,
from before the split. Reindex the project to get the full benefit of
the split.

### Understanding

#### `get_file_map`

Structural overview of a file: all symbols grouped by kind. This tool
works like a fast table of contents, before you read the whole file.

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

fw-context groups enum constants into `subgroups`, by the parent enum.
Each subgroup has a `name` (the parent enum), a `count` (the real total,
even when `max_per_kind` limits the `constants` list), and a `constants`
list with `name`, `qualified_name`, `line`, and `enum_value` (the integer
value, when available).

Pass a relative path, such as `src/main.cpp`, or just the filename, such
as `main.cpp` (fw-context matches the suffix). Use this tool instead of
`Read` on large files. fw-context organizes the symbols by kind, in a
single response.

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
For enums, the result includes a `constants` array that lists all the
member constants, with their names and values:

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
outputs, and side effects. Falls back to a **macro explanation** when the
name matches a macro definition. In that case, this tool returns
`kind: "macro"`, with the expanded value.

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

**Pre-computed (instant):** When you build the index with `--analyze` (the
default), symbols have pre-generated descriptions, stored in the
`llm_analysis` table. `explain_symbol` returns these descriptions
instantly. This tool makes no Ollama call, so there is no waiting.

**On-demand fallback (10–30 s):** When no pre-computed analysis exists, for
example when you built the index with `--no-analyze`, or when fw-context
re-indexed the symbol without re-analysis, this tool falls back to calling
Ollama directly. When Ollama is disabled, this tool returns `source` and
`explain_prompt`, so the AI assistant can answer with its own model.

fw-context generates the analysis during indexing. fw-context uses the
full function body (through the libclang extent) and the callee names
from the reference index, as context. When you re-index a file, with
`reindex_file`, fw-context regenerates the analysis automatically.

#### `get_symbol_context`

Rich LLM context — body, callers, and callees in one response. Returns
**all** callers and callees, including vendor and SDK code. The call
graph naturally spans the project and vendor boundaries.

```
Input:  {"name": "modem_connect", "project_root?": "/path/to/project"}
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

This tool is designed as one-shot LLM context. This tool answers "what
does this do, and how does it fit?" in a single call. This tool returns
all the direct callers and callees, with no artificial limit.

For a field or variable symbol that has a function pointer type, the
result also includes a `resolution` block:
`{assignments_found, call_sites_found, resolved, note}`. This block
indicates whether the assignments and the call sites are linked (Phase 3).
When the data is incomplete, `resolved` is `false`, with an explanatory
note. The LLM can detect the uncertainty from this signal.

For enums, the result includes a `constants` array, with all the member
constants and their values, in the same shape as `get_source`. Enum
constants include `enum_value`.

#### `read_file`

Read a complete source file with ifdef-filtered content — only code that
actually compiles for the current build configuration. fw-context replaces
an inactive `#ifdef` branch with blank lines, and keeps the original line
numbers.

```
Input:  {"file_path": "src/modem.c", "project_root?": "/path/to/project"}
Output: {"file": "/path/src/modem.c", "language": "c", "mtime": 1748534400.0,
         "lines": 512, "content": "/* Modem driver */\n\n#include \"modem.h\"\n…"}
```

Unlike a generic file reader, this tool returns build-accurate content.
Code that is behind `#ifdef BOARD_V2` is visible only when the build
defines `BOARD_V2`.

The path can be relative to the project root, such as `src/main.cpp`, or
just the filename, such as `main.cpp`. This tool falls back to the raw
disk content, with a `warning`, when the indexed `files.content` column is
empty. Run `fw-context index` to populate the ifdef-filtered content.

### Call graph

All graph tools use the cross-reference index. fw-context enables this
index by default. Disable this index with `fw-context index --no-refs`,
or with `[index] index_refs = false`, if you do not need these tools.

#### `find_callers`

Who calls this function? This tool finds direct callers, and indirect
calls through function pointers that fw-context detects in call
arguments, assignments, variable initializers, and struct or array
initializer lists. This tool automatically falls back to a **macro
lookup** when fw-context does not find the symbol as a function or method.

```
Input:  {"name": "uart_write", "project_root?": "/path/to/project", "limit?": 50}
Output: [{"file": "/path/src/main.c", "line": 35, "ref_kind": "call",
          "caller": "main", "caller_kind": "function"},
         {"file": "/path/src/setup.c", "line": 12, "ref_kind": "indirect",
          "caller": "setup", "caller_kind": "function"}]
```

Indirect edges (`ref_kind: "indirect"`) appear when a function pointer
references a function through any of these patterns:
`callback(&Class::method, this)`, `driver.onData = &handleData`,
`void (*fp)(int) = &handler`, or `{.on_data = &handler}`.

For the invocation side — where fw-context actually calls the stored
pointer — use `find_indirect_call_sites`. For linking assignments to call
sites — which specific functions can run at a given call site — use
`find_indirect_targets`.

#### `find_references`

All uses of a symbol — calls, reads, member access, indirect references.
When fw-context does not find the symbol, this tool automatically falls
back to a **macro lookup**. In that case, this tool returns
`ref_kind: "macro_use"` for a file that uses the macro.

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

`ref_kind` values: `"call"` (direct call), `"ref"` (variable read/write),
`"member"` (member access), `"indirect"` (function pointer reference in
arguments, assignments, initializers, or init lists), `"template_ref"`,
`"macro_use"` (macro usage in file).

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

fw-context deduplicates the results. Each caller appears once, at its
shortest distance.

#### `find_callees_recursive`

What does this call, directly or indirectly?

```
Input:  {"name": "main", "project_root?": "/path/to/project", "max_depth?": 5, "limit?": 50}
Output: [{"name": "spi_init", "kind": "function", "file": "/path/src/spi.c", "depth": 1},
         {"name": "spi_transfer", "kind": "function", "file": "/path/src/spi.c", "depth": 2}, …]
```

#### `find_dead_code`

Finds functions that have a definition, but no caller. Returns two categories:

```
Input:  {"project_root?": "/path/to/project", "limit?": 100, "project_only?": true, "exclude_paths?": ["lib/%"]}
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

**`"possibly_dead"`**: code assigns the function to a function pointer
(`ref_kind="indirect"`), but fw-context did not resolve a call site
through that pointer. This means the function **might** run through
unindexed code, or through a type-erased API. Treat this result as
uncertain, not as confirmed dead code. Verify each result with
`find_indirect_targets`, before you delete the function.

Expect additional false positives from entry points (`main`), ISRs,
virtual method overrides, constructors called via factories, and
weak-aliased symbols. fw-context checks only definitions with
`kind IN ('function', 'method', 'constructor', 'destructor')`.

By default, fw-context excludes SDK and vendor paths automatically, with
the `is_project` column, which respects the project's `vendor_paths` and
`project_paths` configuration. Use `exclude_paths` for additional LIKE
patterns on top of this. A `%` matches any suffix, for example `lib/%`.

#### `find_hotspots`

Most-called functions ranked by caller count.

```
Input:  {"project_root?": "/path/to/project", "limit?": 20, "project_only?": true, "exclude_paths?": ["lib/%"]}
Output: [{"name": "log_debug", "kind": "function", "caller_count": 147, …},
         {"name": "millis", "kind": "function", "caller_count": 89, …}, …]
```

#### `find_wrapper_callers`

Find wrapper classes that call methods of a driver class. This tool is
useful for understanding an adapter or wrapper architecture.

```
Input:  {"class_name": "UART_DRIVER", "project_root?": "/path/to/project", "limit?": 50}
Output: [{"wrapper_class": "UART", "wrapper_method": "send",
          "driver_method": "UART_DRIVER::send", "file": "/path/src/uart.cpp",
          "line": 45, "ref_kind": "call"}, …]
```

Pass a fully-qualified class name (`hal::UART_DRIVER`) or just the bare
name (`UART_DRIVER`). fw-context groups the results by wrapper class.

#### `find_indirect_call_sites`

Find indirect call sites where a function pointer field or variable is invoked.

```
Input:  {"name": "onData", "project_root?": "/path/to/project", "limit?": 50}
Output: [{"file": "/path/src/main.c", "line": 36, "expr_text": "drv . onData",
          "target_usr": "c:@S@Driver@FI@onData", "target_name": "onData",
          "fn_ptr_type": "void (*)(unsigned char *, int)",
          "caller": "test_assign", "caller_kind": "function"}]
```

Returns locations where code calls a function pointer, through a field
access (`driver.onData(buf, len)`) or a variable (`stored_callback(42)`).
Use this tool to answer *"where does code invoke this function pointer?"*
Combine this tool with `find_indirect_targets`, to answer *"which
functions are assigned to this field?"*

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

Links assignment sites (`driver.onData = &handler`) to call sites
(`driver.onData(buf, len)`) via the field's USR. Shows the execution
flow: *code assigns handler to onData at line 14, and code calls onData
at line 16, so handler might run at that call site.*

When code assigns a function, but fw-context finds no call site,
`call_file` and `call_line` are `null`. The assignment exists, but the
invocation might be in unindexed code. The `method` field indicates how
fw-context detected the assignment: `"assignment"` for a direct field
assignment, `"call_arg"` for a value passed as a callback argument,
`"var_init"` for a variable initializer, or `"init_list"` for a struct or
array designated initializer.

For the reverse query — where does code call this field — use
`find_indirect_call_sites`.

#### `trace_data_flow`

Trace how data of a given type flows to a target function. **Experimental.**

```
Input:  {"type_name": "SensorData", "to_symbol": "uart_send", "project_root?": "/path/to/project", "max_depth?": 8, "limit?": 15}
Output: [{"source_function": "sensor_read", "source_type": "SensorData",
          "file": "/path/src/sensor.cpp", "line": 120,
          "call_path": "sensor_read → pack_payload → uart_send"}, …]
```

Finds the functions whose signature mentions `type_name`, then looks for
call paths from those functions to `to_symbol`. Does **not** resolve type
transformations, for example CBOR encoding. Use this tool together with
`find_call_path`, to verify specific paths.

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

fw-context orders the members by kind, then by name. This tool shows the
full API surface, without opening the header file. This tool also works
for C structs, but a struct will not have methods. This tool returns
`member_count: 0` for an index that predates this feature.

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

Returns an empty `instances` list when the template is declared but
fw-context never finds an instantiation in the project code. Works for
both class templates (`CXCursor_CLASS_TEMPLATE`) and function templates
(`CXCursor_FUNCTION_TEMPLATE`). fw-context resolves the template by name,
exact or qualified, in the same way as `lookup_symbol`.

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

Uses the `overrides` table, which fw-context builds during indexing. A
parameter-type comparison filters out accidental name collisions, such as
overloads that are not overrides. For a non-virtual method, or a method
whose class has no ancestors, both `overrides` and `overridden_by` are
empty lists.

### Index maintenance

#### `get_environment_status`

Return the complete project environment status in one call. This tool
aggregates the dependency audit, the detected build system, the
compilation database, the index state, and the LLM backend.

```
Input:  {"project_root?": "/path/to/project"}
Output: {"init_status": "initialized",
         "deps": [{"name": "libclang-so", "status": "missing",
                    "action": {"message": "libclang shared library not found.",
                               "command": "apt install libclang-18-dev"}}],
         "build_system": "zephyr",
         "compile_db": {"exists": true, "path": "/path/build/compile_commands.json",
                        "entry_count": 312},
         "index": {"status": "reindex_needed", "index_message": "…", …},
         "llm": {"enabled": true, "ollama_running": true,
                 "chat_model": "qwen2.5-coder:14b", "embed_model": "qwen3-embedding:8b"}}
```

Each problem field (`deps[*]`, `compile_db`, `llm`) may carry an `action`
with a human-readable `message` and an exact shell `command`. Pass both to
the user without interpretation. The `index` field is the full
`get_active_build()` result, unchanged; its `index_message` carries the
action.

This tool is read-only. Use it at session start, to see everything that
needs fixing before answering the user. Pass `project_root` explicitly
when the project is not the server's working directory.

#### `check_dependencies`

Run the full dependency audit. Read-only. Returns structured results, one
entry per check.

```
Input:  {"project_root?": "/path/to/project"}
Output: [{"name": "libclang-so", "status": "missing",
          "message": "…", "fix_cmd": "apt install libclang-18-dev",
          "instructions": "…", "critical": true}, …]
```

Read `status`, `fix_cmd`, and `instructions` per issue. A `status` of
`"skipped"` means a prerequisite check is missing. This tool is the MCP
wrapper for `fw-context doctor` without `--fix`; it repairs nothing. For a
single-call overview of the whole environment, use
`get_environment_status`.

#### `get_active_build`

Check index health — call at session start.

```
Input:  {"project_root?": "/path/to/project"}
Output: {"config_hash": "a1b2…", "project_id": "c3d4…", "project_root": "/path/to/project",
         "build_system": "zephyr", "compile_commands": "/path/build/compile_commands.json",
         "indexed_at": "2026-06-05T09:35:18", "symbol_count": 12430, "file_count": 1502,
         "reference_count": 8900, "modified_files_count": 3,
         "header_affected_tus": 0, "manifest_verification": "full",
         "schema_version": 84935291, "current_schema": 84935291,
         "analyzed_symbols": 8450, "unanalyzed_symbols": 120,
         "analysis_model": "qwen2.5-coder:14b",
         "vendor_paths": [], "project_paths": [],
         "bg_reindex_running": false, "reindex_progress": null,
         "status": "ready", "reindex_needed": false, "reindex_reasons": [],
         "index_message": "Index is fully up to date (12430 symbols)"}
```

**Read-only.** This tool has no side effects, and does not spawn background
tasks. The server startup thread and the file watcher manage the
background reindex. `bg_reindex_running` and `reindex_progress` report
whether a background reindex is active, and its last log line.

**`status` field (use for decision-making):**

| Status | Meaning | What to do |
|--------|---------|------------|
| ``"ready"`` | Fully up to date, no issues | Continue normally |
| ``"reindexing"`` | Background reindex in progress | Index is still usable — continue normally |
| ``"reindex_needed"`` | compile_commands.json changed or schema mismatch | Queries still work, but schedule ``fw-context index`` |
| ``"no_index"`` | No build config indexed | Use other tools |
| ``"error"`` | DB corruption or access error | Use other tools |

`modified_files_count` is cached for 30 seconds, so calls within that
window return the same value. `header_affected_tus` reports how many
translation units have stale header dependencies. This count is non-zero
when headers changed since the last index with
`manifest_verification: "full"`.

`manifest_verification` is `"full"` when `manifest.json` is available. In
that case, fw-context tracks header staleness with SHA-256 hashes that
fw-context collects during indexing. `manifest_verification` is `"none"`
when no manifest exists. In that case, fw-context cannot detect header
changes. Run `fw-context index --build` to regenerate the index with full
dependency tracking.

`unanalyzed_symbols` counts the definition symbols that still need LLM
analysis. This count is zero when analysis is disabled, and empty when
fw-context has analyzed all symbols.

**Multi-variant output.** For a project with `[[build.variants]]`, the
result adds these fields:

- `multi` — `true` for a multi-variant project
- `variants` — a list of `{name, description, board}`
- `images` — a list of `{name, description, dir, type, board?}`
- `variant_images` — a map of variant name to its image names
- `active_variant` — the `[build] default_variant` value
- `active_image` — the `[build] default_image` value

#### `list_variants`

List every indexed build, with its `(variant, image, board)` identity.
This tool shows what fw-context actually indexed, not what the config
declares. Each `builds` entry is one `(variant, image)` build, with its
own `config_hash` and symbol count.

```
Input:  {"project_root?": "/path/to/project"}
Output: {"multi": true, "builds": [
           {"variant": "nrf52840-dev", "image": "app", "board": "nrf52840dk/nrf52840",
            "config_hash": "a1b2…", "symbol_count": 12430, "file_count": 1502,
            "manifest_verification": "full"},
           {"variant": "nrf52840-dev", "image": "mcuboot", "board": "nrf52840dk/nrf52840",
            "config_hash": "c3d4…", "symbol_count": 3120, "file_count": 480,
            "manifest_verification": "full"}]}
```

For a single-project index, this tool returns one `builds` entry, with an
empty `variant` and an empty `image`. Use `get_active_build` for the
human-readable variant and image discovery.

#### `reindex_file`

Re-index a single file after editing.

```
Input:  {"file_path": "/abs/path/to/src/main.c", "project_root?": "/path/to/project"}
Output: {"file": "/abs/path/to/src/main.c", "translation_units": 1,
         "symbols_updated": 28, "elapsed_s": 2.5}
```

**Limitation:** The file must appear in `compile_commands.json`. fw-context
re-indexes a header through the translation unit that includes the
header. When multiple translation units include a single header, you
might need a full `fw-context index` run for completeness.

#### `reindex_file_impl`

The shared implementation that `reindex_file` uses (the public tool, with
full analysis) and that the background auto-reindex uses (the fast path,
with no LLM). Prefer `reindex_file` for interactive use. Use this tool
only when you need to control `with_analysis` explicitly.

```
Input:  {"file_path": "/abs/path/to/src/main.c", "project_root?": "/path/to/project", "with_analysis?": true}
Output: {"file": "/abs/path/to/src/main.c", "translation_units": 1,
         "symbols_updated": 28, "elapsed_s": 2.5,
         "analysis_updated": 12, "analysis_warning": null}
```

When `with_analysis=True` (the default), this tool also regenerates the
LLM symbol analysis and the method override relationships. This is
slower, but produces a fully up-to-date index. Set `with_analysis=False`
for a fast, symbol-only update. This tool requires an existing index. The
file must appear in `compile_commands.json`.

#### `reset_index`

Delete the index. Always run a dry run first, then pass `confirm: true`.

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

Looks up the global project registry at `~/.fw-context/projects.db`. Use
this tool to identify a project from its UUID4: find out which build
system the project uses, the project's name, and when fw-context last
indexed the project. Returns an error when the registry does not have the
`project_id`.

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

Returns `status: "disabled"` when `[llm] enabled = false`. In that case,
this tool needs no Ollama.

Note: `explain_symbol`, with pre-computed analysis (the default), returns
instantly, and does not require Ollama at query time. `num_ctx` is 16384
by default, to allow full function bodies during analysis generation.

#### `configure_llm`

Write per-developer LLM settings to `.fw-context/local.toml` (gitignored).
This tool writes nothing else. After writing, it tests the configuration
with a simple API call.

```
Input:  {"chat_api_base?": "https://api.deepseek.com/v1", "chat_api_key?": "sk-…",
         "model?": "deepseek-chat", "auto_pull?": false, "stream?": false}
Output: {"status": "ok", "model": "deepseek-chat", "test_latency_s": 1.2, …}
```

Pass `chat_api_base` for a cloud or proxy API. When it is empty, this
tool uses the local Ollama instance. When `chat_api_base` points to an
external host, source code in chat prompts is sent to that host. Verify
this complies with your data security policy. Prefer the local Ollama
instance.

### MCP Resources

Resources are read-only, URI-addressable endpoints that return structured
content, in Markdown or JSON. Unlike tools, resources take no JSON-RPC
parameters, except `symbols/{name}`, which takes a name path segment.

#### `fw-context://stats`

Markdown summary of all indexed projects — symbol counts, file counts,
freshness, and index timestamps.

```
URI:    fw-context://stats
Output: # fw-context — 3 project(s)

        - **my-zephyr-app** (a1b2…) — 12430 symbols, 1502 files, indexed 2026-06-05T09:35:18, ✓ fresh
        - **my-pio-app** (c3d4…) — 8586 symbols, 952 files, indexed 2026-06-04T16:20:45, ⚠ stale
```

Aggregates data across all project databases under `~/.fw-context/index/`.
This resource marks a project that has errors with an **ERROR** marker.

#### `fw-context://projects`

Same data as [`list_projects`](#list_projects), serialized as indented JSON.

```
URI:    fw-context://projects
Output: [{"project_id": "a1b2…", "name": "my-zephyr-app", "symbol_count": 12430, …}, …]
```

#### `fw-context://symbols/{name}`

Definition source of a symbol rendered as a Markdown document. Uses the
same lookup as [`get_source`](#get_source).

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

When fw-context does not find the symbol, this resource returns a JSON
error object.

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

`search_code`, `lookup_symbol`, and `semantic_search` detect a stale file
in their results, when the on-disk `mtime` is greater than the stored
`mtime`. When fw-context finds a stale file, these tools append a warning
to the response. The server's background reindex subprocess picks up the
work, so no query has to wait for the reindex to finish. The file watcher
independently handles a recently edited file within 500 ms after the
save, so a stale result is rare in practice.

For manual recovery, use `reindex_file` or `fw-context index`.

### Index building (detailed)

```
1. Read compile_commands.json → extract translation units (file + compiler args)

2. For each translation unit (sequential, per-TU write lock):
   Parse with libclang using exact compiler flags (-I, -D, -std, --target)
   Traverse AST → extract all symbols (no source filtering: fw-context
   indexes everything from compile_commands.json and the includes)
   Category: function, method, class, enum, typedef, variable, field, and other kinds
   Compute is_project per-symbol (project code = 1, vendor/SDK = 0)
   Extract cross-references including vendor/SDK (on by default; skip with --no-refs)
   Extract macros via clang -dM -E (preprocessor dump; stored in macro_defs table)
   Release the write lock between translation units. A manual operation,
   such as `reindex_file`, can interleave through the pause marker
   mechanism, without blocking.

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

When fw-context generates embeddings during indexing
(`fw-context index --embeddings`), fw-context stores the symbols in two tables:

| Table | Storage | Query method |
|-------|---------|-------------|
| `embeddings` | BLOB (4 bytes × dim floats — 4096 for mxbai 1024-dim, 16384 for qwen3 4096-dim) | Legacy brute-force (Python) |
| `vec_symbols` (vec0) | sqlite-vec virtual table | KNN via `MATCH` (C implementation) |

The `EmbeddingPhase` prefers `vec0` when available, in an index built
after fw-context added this feature. The `EmbeddingPhase` falls back to
BLOB brute-force search for an older index. The `EmbeddingPhase` also
operates as a **hybrid re-rank** when FTS5 results already exist. This
avoids duplicate searches and expensive merging.

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

- **Use short queries.** A query with 1 to 3 words works best. FTS5 is a
  token-based engine, so a longer phrase narrows the results too
  aggressively.
- **Omit underscores.** FTS5 treats `_` as a word separator. `modem_init` →
  `modem AND init`. Write `modem init` instead.
- **Trailing `*` for a prefix match.** `uart_*` finds `uart_init`,
  `uart_write`, `uart_read`, and other names with that prefix.
- **Quotes for exact phrases.** `"spi transfer"` matches that exact token sequence,
  not `transfer over spi`.
- **Kind filter for precision.** Narrow `search_code` to `kind=function`
  when you know that you are looking for a function. This filter
  eliminates variables, fields, and enums.

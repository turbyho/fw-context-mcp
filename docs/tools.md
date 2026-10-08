# Tools Reference

This is the complete reference for all fw-context CLI commands and MCP server tools.

## CLI commands

### `fw-context index`

Build or update the symbol index from `compile_commands.json`.

> **Incremental by default.** `fw-context` reuses an existing
> `compile_commands.json`, and re-parses only the changed files. Use
> `--build` to force a clean build and a full re-index when needed, for
> example after an SDK update or a build system change. Use `--force` to
> skip the hash checks and force a re-index of all files, embeddings,
> LLM analysis, overrides, and caches, without rebuilding, for example
> after a schema change or a tool update.

A file is changed when its content, the content of a header that it
includes, or its compiler flags changed. fw-context compares hashes and no
file time: `git checkout` and `touch` start no parse, and a copy that keeps
an old time (`cp -p`) does. The `-D` macros of `[index] transient_defines`
(a build time, a build counter) do not count, see
[Configuration](configuration.md).

A run that stops before its end (a watchdog, `Ctrl+C`, a manual
`reindex_file`) keeps the files that it parsed: the run writes its manifest
each minute. The next run parses the other files, and also the files whose
call-graph dispatch edges (event-loop callbacks, thread starts) the stop
lost. It expands the macros again, which the stopped run did not do.

```bash
# Incremental (default) — reuse existing compile_commands.json, fast:
fw-context index

# Force clean build + full re-index:
fw-context index --build

# Force full re-index of all files, embeddings, analysis, and caches
# (skips the hash checks — use after schema changes or tool updates):
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
| `--force` | off | Force re-index of all files, embeddings, LLM analysis, overrides, PageRank, and hotspot cache (skips the hash checks) |
| `--variant NAME` | all variants | Restrict indexing to one build variant (name from `[[build.variants]]`) |
| `--variants A,B` | all variants | Comma-separated list of build variants to index |
| `--image NAME` | all images | Index only this image of the build (Zephyr sysbuild, ESP-IDF). The build stays complete. |
| `--exclude-image NAME` | none | Do not index this image of the build (repeatable) |
| `--no-prune` | off | Keep the global-registry rows that nothing on disk confirms |
| `--takeover` | off | Terminate another running foreground index run of this project instead of refusing |
| `-v` | off | Verbose progress output |

**Automatic build:**

Without `--build`, a run can build by itself.

A run that you start builds as `--build` does when the build is not there:
`compile_commands.json` does not exist, or a `directory` of its entries does
not exist. fw-context asks the compiler of each unit in that directory.
When fw-context cannot run the build (STM32CubeIDE, TI CCS, a
`[build] command` in a background run, or a background run of a backend
that compiles in your tree), the run stops with an error and does not
index. A run with an explicit `compile_commands.json`, or a project with
`[[build.variants]]`, does not do this check.

A run also builds when an index exists and one of these conditions is true:

- The build is not there (a background run).
- A source file on disk is not in `compile_commands.json`. Such a file has
  no translation unit, thus a reindex skips it.
- The tree is on a different git branch than the index.
  `compile_commands.json` belongs to the old branch.

The run builds only when the backend can build on its own (see
[Build Configuration](build.md#automatic-build)). Each build, this one and
one that you start, goes to `.fw-context/build/<variant>/out`, or
`.fw-context/build/default/out` for a project without variants.
`--build` and `--background` are mutually exclusive, except for this
automatic build.

When a run stops without an index (the build is not there, or the run
failed), fw-context writes the reason to `build_problem.json` next to the
index. Each answer of a query tool of the project carries it: a list gets
a leading `warning` row, a dict gets `build_warning`. The maintenance tools
do not carry it. `get_active_build` gives it as a reason, with the status
`reindex_needed`. The next run that ends well with the build there removes
the file. `get_active_build` removes a reason that said the build is not
there when it finds the build again. A failed automatic build is not
paused: the next background run builds again.

**When another index run is in progress:**

Only one index run can own a project's index at a time. A manual run takes
the index before it builds anything, so a refused run never cleans the build
directory or rewrites `compile_commands.json`. This holds for a single build
and for `[[build.variants]]`.

- **A background run** (the file-watcher daemon starts these): a manual run
  always takes over. It sends `SIGTERM`, and the background run stops with
  exit code 75 (superseded); the daemon retries it later. A run that does
  not stop within 10 seconds gets `SIGKILL`.
- **Another foreground run**: the new run is refused with exit code 69, and
  the message names the PID of that run. Add `--takeover` to terminate it
  the same way and index anyway.
- **A `--background` run** never takes over from another run.

Before it sends `SIGTERM`, the run that takes over writes
`reindex.takeover` in the index directory, with its own PID and the PID of
the run that it stops. Thus the stopped run knows why it stopped:

| Cause of the `SIGTERM` | Exit code | Message | Daemon retries |
|---|---|---|---|
| Another index run takes over | 75 | `Superseded: …` | Yes |
| Anything else (a CI timeout, `kill`, the shutdown of the daemon) | 143 | `Terminated: …` | No |

A run that already released the index when the `SIGTERM` of a takeover
arrives ignores that signal. The other run only wanted the index, and it
already has it.

Each build runs in its own process group. When a run stops, it stops the
whole group, the compilers of `make -j`, `bear` and `pio` included, before
the other run builds in the same directory. A run that `SIGKILL` stops
cannot do that. It records its build group in `reindex.build`, and the next
run that owns the index stops that group first.

The build is not in the session of the terminal, thus it does not get the
`SIGHUP` of a closed terminal. The index run gets it, and stops as with a
foreign `SIGTERM`: `Terminated`, exit code 143, and the build group stops
too.

**The global registry:**

`fw-context index` and `fw-context init` write this project into the global
registry at `~/.fw-context/projects.db`. Then they remove the rows that
nothing on disk confirms. The registry only grew before this, and two rows
of one name make the `project="<name>"` tool selector fail with an
ambiguity error.

A row stays when one of these conditions is true:

- An index database of that project ID is in the index directory.
- The project root holds a `.fw-context/config.toml` that declares that
  same project ID.

The command names each row that it removes, up to five names. To keep the
rows, use `--no-prune`. A live project can look stale: this happens when
its filesystem is not mounted, and its index is not in the default index
directory. If such a row goes, the next `init` or `index` of that project
writes it again.

A `--background` run removes no row. The file-watcher daemon starts those
runs, no flag can reach them, and their output goes to `reindex.log`.

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
fw-context db cleanup                # remove orphaned per-build artifacts
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
fw-context init --no-prune              # keep the stale global-registry rows
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

A separate instruction file that fw-context owns (`.codex`, `.cursor`)
carries section markers. A later run updates the text between the markers
in place. A file that a version before 0.29.2 wrote has no markers, and
`fw-context init` skips it. Run `fw-context init --force` once to replace
such a file.

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
fw-context init-variants add --name <name> --env BOARD_ENV=DEV
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

Configure with `[llm] analyze_symbols`, `[llm] model`, `[llm] analyze_vendor`,
and `[llm] num_ctx` in `~/.fw-context/config.toml`.

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

When the server gives no statistics, the line under the URL names the
cause, with the same causes as for `fw-context cache push`. For example:
`The server rejected the token (401/403) — check [cache_server] token`. The
exit status is 0, because this command only shows a status.

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

Upload the local cache entries that the remote cache server does not have
yet. The server keeps each entry that it already has. To replace those
entries with the local ones, add `--overwrite`.

```bash
fw-context cache push                       # missing entries only, batch size from config (100)
fw-context cache push --batch 500           # larger batches for faster transfer
fw-context cache push --overwrite           # also replace the server's entries
```

The client never sends more than 1000 entries in one request, the most that
the server accepts; a larger `--batch` is capped at 1000. A `--batch` below
1 is a usage error. A `[cache_server] batch_size` below 1 stops the command
with exit status 1 and the message
`error: [cache_server] batch_size must be a positive integer, not <value>`.

This command requires `[cache_server]` and a token with `can_write`.
`--overwrite` also requires `can_overwrite`. The command reports progress in
batches, and ends with the number of entries inserted and already on the
server. It exits with status 1 when the server does not give its
statistics, or when the server refuses a write. A refused write stops the
push; no further batch goes. When the statistics fail, the message names
the cause:

| Cause | Message says |
|---|---|
| The server rejects the token (401/403) | check `[cache_server] token` |
| 429: the token failed too often | check the token, then wait and try again |
| The body is not a JSON object | a proxy can be in front of the server |
| No connection | check the URL and the network |

A local entry that breaks a limit of the server stays local. The command
does not send it, and lists it in a warning (the first five, then a count).
The other entries go. See [Cache Server](cache-server.md).

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
- The files, directories and config keys of an older fw-context
  (`obsolete-files`). `--fix` removes them.

When you run this command with `--fix`, it attempts to repair fixable
issues automatically. The command reports issues that it cannot fix, and
tells you how to fix them manually.

### `fw-context cleanup`

Remove the files, directories and config keys that an older fw-context
wrote and that no release reads now. The list is in
[Obsolete keys and files](configuration.md#obsolete-keys-and-files).
`fw-context index` and `fw-context doctor --fix` do the same cleanup, thus
you need this command only to see the list, or to clean now.

```bash
fw-context cleanup --dry-run         # show the list, change nothing
fw-context cleanup                   # remove the items in the list
fw-context cleanup --project /path   # clean a specific project
```

The command gives one line for each item. Exit code 1 means that a path
could not be read or changed.

`fw-context db cleanup` is a different command: it removes the per-build
files of the index that no build uses.

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
Last index: 09:35:18 INFO [45/45] main.cpp: unchanged
```

`Last index:` shows the newest line of `reindex.log` that begins a
fw-context record: a log record, a `fw-context: error:` line, or the
`Project:` header. The output of the build tools in the same file is
skipped. After a failed build, the line is thus the `fw-context: error:`
line, not the quoted tool output below it.

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

**A question about a different project.** Every tool answers about one
project. To ask about a project that is not the project of the current
directory, give the `project` parameter — the `name` or the `project_id`
that `list_projects` shows. It is an alternative to `project_root`, which
takes the root path. Give one of the two, not both. The `project`
parameter must go on each call, `get_active_build` included, because
fw-context keeps no session state.

```
list_projects   →  {"name": "boot-loader", "project_id": "<32 hex chars>", "root_path": "/…/boot-loader"}
lookup_symbol   →  {"name": "main", "project": "boot-loader"}
```

A parameter name that no tool declares causes an error that names it.
Earlier releases dropped such an argument without a message, and the tool
then answered about the project of the current directory.

A project name that matches more than one project in the registry causes
an error. The error lists each candidate with its `project_id` and root
path. Repeat the call with the `project_id`.

**Multi-variant projects.** One query answers for one build. Every tool
that answers about code accepts `variant` and `image` parameters, except
`smart_search` and `semantic_search`. These two take neither, and answer
for the active build. Both selectors fail closed:

- When the project has variants, name a `variant`. When you omit it,
  fw-context uses `[build] default_variant`, or returns an error that lists
  the valid names.
- When the build holds more than one image, name an `image`. Each image
  is a separate program, for example a bootloader and the application. When
  you omit it, fw-context uses `[build] default_image` if the build holds
  that image. Else the build gives the application that its build system
  names (ESP-IDF, Zephyr sysbuild). Else a build with one image gives that
  image. Else the query returns an error that lists the images.
- `variant="*"` is refused. To learn whether a symbol is in two builds, ask
  once for each build.
- A project with one build and no variants refuses `variant`. It refuses
  `image` too, unless the build makes more than one program (ESP-IDF, a
  Zephyr sysbuild).

The valid variants are the declared variants (`[[build.variants]]`) and
the variants in the index. Call `get_active_build` or `list_variants` to
see the variants and images that fw-context has indexed.

**Paging.** Each tool that can give a long list gives it in pages, and
says how much there is. The answer has one of these forms:

- A list answer starts with a page notice, and the tool takes an
  `offset`. These tools give a list answer: `lookup_symbol`,
  `search_code`, `search_bodies`, `search_content`, `smart_search`,
  `semantic_search`, `find_callers`, `find_references`,
  `find_all_callers_recursive`, `find_callees_recursive`,
  `find_indirect_call_sites`, `find_indirect_targets`, `find_variables`,
  `find_dead_code`, `find_hotspots`, `find_wrapper_callers`,
  `trace_data_flow`, `get_template_instances`, and `get_vector_table`.
- A dict answer holds the page notice under the key `page`. These tools
  give a dict answer: `get_class_members`, `get_file_map` with `kind`, and
  `read_file`. `read_file` pages lines, and names the next page with
  `start_line`, not with `offset`.
- `get_inheritance_chain` with `transitive: true` holds two page notices:
  `all_bases_page` and `all_derived_page`.
- `get_symbol_context` and `get_source` cut each list at a maximum.
  `<list>_total` counts the whole list, and `<list>_hint` names the tool
  that pages a cut list.
- `find_call_path` has no pages, because fw-context cannot count the
  paths. A trailing `info` row says when the answer can leave out paths.

When the answer holds a row, the page notice is there, also when one
page holds the whole answer:

```
{"total": 137, "offset": 0, "shown": 20, "more": true, "hint": "…pass offset=20…"}
```

- `total` counts every row that the query matches, not only the page.
- Find the notice by its keys, not by its position. A stale-index
  `warning` row, or the row that reports a query FTS5 cannot parse, can
  come first.
- When `more` is true, call again with `offset` = `offset` + `shown`. The
  `hint` names that call. It is absent on the last page.
- The `hint` repeats each argument that changes the answer and that is
  not at its default: a filter such as `kind`, `project_only` or `exact`,
  and the selection of the project and the build (`project`, `variant`,
  `image`). A call with `project_root` gets `project=` with the same
  value, because `project` takes a path, a name or a `project_id`.
  Without them, the next page came from the project of the current
  directory.
- The order of each tool is stable, thus two pages never overlap and never
  skip a row.
- An `info` row that names an offset means that the offset is past the
  end. The row also gives the total.
- `smart_search`: an `offset` of 0, or no `offset`, starts a new search.
  A larger `offset` gives a page of the stored answer of the last search
  of the same query.
- `semantic_search`: `total_capped: true` in the notice means that
  `total` is a lower bound. One search reaches 4096 vector rows at most.

**Parameter validation.** The schema refuses a bad value before the tool
runs, with an error that names the parameter:

| Parameter | Valid values |
|---|---|
| `limit`, `max_depth`, `timeout_ms` | 1 or more |
| `offset`, `context_lines`, `max_per_kind`, `start_line`, `end_line` | 0 or more |
| `threshold` | 0.0 to 1.0 |
| A required string (for example `name`, `query`) | not empty |

Only a value that is too large is clamped to the maximum of the tool.

### Search & lookup

#### `search_code`

Full-text search with FTS5 syntax.

```
Input:  {"query": "uart init", "project_root?": "/path/to/project", "kind?": "function", "limit?": 20, "offset?": 0}
Output: [{"total": 3, "offset": 0, "shown": 3, "more": false},
         {"name": "uart_init", "qualified_name": "drv::uart_init", "kind": "function",
          "file": "/path/src/uart.c", "line": 42, "is_definition": true,
          "signature": "void uart_init(int baudrate)", "docstring": "Initialize UART",
          "is_template": false, "is_virtual": false, "is_pure_virtual": false,
          "llm_analysis": {"summary": "Initialize the UART peripheral…",
                           "inputs": "baudrate…", "outputs": "…"},
          "enum_value": null}, …]
```

Enum constants include `enum_value` (the integer value) when non-None.
Results include `is_template`, `is_virtual`, `is_pure_virtual` flags
(boolean, always present). When the symbol is a template instantiation,
`template_usr` references the template definition. When the symbol is
a member (method/field/nested type), `parent_usr` references the parent class.
When fw-context generates LLM analysis (with `fw-context index --analyze`),
a result carries `llm_analysis` — `{summary, inputs, outputs}`, with
plain-English descriptions.

A model wrote that text, and the code did not. It is nested for that
reason: a flat key would sit beside `signature` and `docstring`, which
come from the source, with nothing to separate a description from a
guess. Use `llm_analysis` to find a symbol. Quote `signature`,
`docstring`, or the `source` of `get_source`.
`get_active_build().analysis.model` names the model — one for each index.

**Local variables are out:** FTS5 indexes the qualified name, thus a local
variable matches through the name of its parent. A query for `sensor`
would also return `V`, `ret`, and `tmp_value` from inside
`read_sensor_value`. Thus `search_code` leaves out the `varlocal` kind and
the legacy `variable` kind, in every relaxation step. `varglobal` stays.
To find a local variable, pass `kind="varlocal"`, or use `find_variables`.

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
   `#define` names, parameter lists and values (`kind="macro"`,
   `_fallback="macros_fts"`).

Results from fallback steps carry `_fallback` indicating which method
succeeded: `"fts5"` (step 2, the retry without `kind`),
`"name_tokens_like"`, `"docstring_like"`, `"individual_terms"`, or
`"macros_fts"`. Results from the primary FTS5 path (step 1) have no
`_fallback` field. One step owns the whole answer, thus all pages of one
query come from the same step. Steps 3, 4, and 5 read at most 32 terms of
the query.

When FTS5 cannot parse the query and no step finds a row, the result is
one row with `warning` and `hint`. The hint names the repair: an unbalanced
double quote, or a bare operator (`AND`, `OR`, `NOT`, `NEAR`). Search one
word, or write an explicit phrase.

**FTS5 syntax:**
- `uart*` — prefix wildcard
- `"spi transfer"` — exact phrase match
- fw-context treats an underscore as a word separator, so `modem_init`
  asks for the two tokens next to each other and misses
  `modem_parser_oob_init`
- **`search_code` and `search_content`** give every bare term a trailing
  `*` and OR-join them: `modem init` becomes `modem* OR init*`. Prefer
  single-word queries
- **`search_bodies`** sends the query as written: a space is an AND of two
  exact tokens, and no wildcard is added
- Punctuation is not searchable in any of them. FTS5 cannot parse
  `.attach(` as a term, so the query is repaired into the phrase
  `".attach("` — and the tokenizer drops punctuation inside a phrase too,
  so what runs is the word `attach`. In `search_bodies` such a result
  carries `_fallback: "sanitized"` and `_query_used`; a query FTS5 accepts
  (`NEAR(a b)`, `^term`, a column filter) always runs untouched

**Kind filter:** `function`, `method`, `constructor`, `destructor`, `class`, `struct`, `union`, `enum`, `enum_constant`, `typedef`, `varglobal`, `varlocal`, `variable`, `field`, `namespace`

#### `search_bodies`

Find patterns in the **text of a definition** — the code inside its extent.

Searches the stored text of every definition, and a definition is not only
a callable. Measured on one project of 60,877 symbols, the text covers:

- Callables — `function`, `method`, `constructor`, `destructor`.
- Types — `class`, `struct`, `union`, `enum`, `namespace`. An enum
  constant, a bit field, and a member declaration such as
  `InterruptIn _pin;` are inside the body of the type that holds them.
- Definitions of data — `varglobal`, `varlocal`, `typedef`. A table with
  a multi-line initializer is found by its content.

A match on a type reports the type as the result. A query for one enum
constant thus answers with the enum, and `match_lines` gives the line of
the constant itself.

**Only the text matches.** The query is bound to the stored body: a hit
in the name, the signature, the docstring or the `llm_analysis` of a
symbol is not a hit here. Measured on one project, `sensor` used to give
36 results of which 22 matched only through a summary that a model wrote
— untrusted text that cannot be cited, and a `_match_snippet` with no
match in it. Use `search_code` to reach a name or a concept. A column
filter you write yourself (`summary : sensor`) overrides the binding.

Text that belongs to **no** definition is out of reach: `#define`,
`#include`, `#ifdef`, `extern "C"`, and a comment or declaration at file
scope. Use `search_content` for those.

**The stored text is ifdef-filtered.** A line of an inactive `#if` branch
is blank in the stored body, and line numbers do not move. An empty
result can thus mean that the pattern is only in a dead branch: the active
build does not compile it.

```
Input:  {"query": "attach", "project_root?": "/path/to/project", "kind?": "function", "limit?": 20, "offset?": 0, "project_only?": true}
Output: [{"total": 1, "offset": 0, "shown": 1, "more": false},
         {"name": "setup", "qualified_name": "setup", "kind": "function",
          "file": "/path/src/main.cpp", "line": 55, "is_definition": true,
          "signature": "void setup()",
          "_match_snippet": "…_timeout.<b>attach</b>(callback(&led_blink, 1000))…",
          "match_lines": [58],
          "source": "… (the text of the definition)",
          "_source_truncated": true}]
```

`line` is the first line of the definition, which in a long function is
far from the match. Cite from `match_lines`, never from `line`. The name
carries no leading underscore for a reason: a field the caller must cite
is an answer, while an `_`-prefixed field (`_match_snippet`, `_fallback`,
`_source_truncated`) says where the answer came from.

`_source_truncated` says that `source` is cut. A callable keeps 2000
characters, any other kind 500 — the body of a type is mostly members
that the match has nothing to do with, and `_match_snippet` already
carries the match in context. `get_source` gives the whole text.

- **When to use `search_bodies` vs. `search_code` vs. `search_content`:**
  - `search_bodies` — which **definition** holds the pattern (what the
    code does or declares): `attach`, `SELF_TEST`, `InterruptIn`
  - `search_content` — which **files** a topic touches, and the
    preprocessor and file scope: `extern "C"`, `#define`, `#include`
  - `search_code` — find symbols by **name**:
    `modem init`, `interrupt handler`

  `search_content` is the complement of `search_bodies`, not its
  fallback: it covers the same definitions plus the text between them,
  and it widens the query. Measured on one project, `SELF_TEST` gave 6
  files there and 5 definitions here — the extra file held the comment
  `Self tester`, which the literal query cannot reach. For the footprint
  of one feature, run both.

**FTS5 query tips for `search_bodies`:** the query goes to FTS5 as
written. A space is an AND of two exact tokens and no wildcard is added,
so `SELF_TEST` misses `Self tester` while `SELF_TEST*` finds it. A single
word is the broadest form: `"attach"` reaches every `.attach(...)`
pattern in the codebase.

A query the engine refuses is repaired and re-run once, and the results
then carry `_fallback: "sanitized"` with `_query_used`. Only what FTS5
rejects is touched, so `NEAR(a b)`, `^term` and a column filter keep
their exact meaning. When the repair cannot help either — an unbalanced
quote, a bare operator — the answer is a `warning` with a `hint`, never
an empty list.

Results include `_match_snippet` — a highlighted excerpt showing each
match in context with `<b>…</b>` tags. Project code sorts before vendor
code. Set `project_only=True` to filter to application code only.

#### `search_content`

Find patterns in **full file content** — the whole file, and not only the
text that belongs to a definition.

Searches **ifdef-filtered** file text: only the code that actually
compiles for the current build configuration. fw-context replaces an
inactive `#ifdef` branch with blank lines, and keeps the original line
numbers. Comments and preprocessor directives stay: an include guard, a
`#define`, and a block comment with its closing marker. A blank line is
thus an inactive line, or a line that is blank on disk.

```
Input:  {"query": "InterruptIn", "project_root?": "/path/to/project", "limit?": 20, "offset?": 0, "project_only?": false}
Output: [{"total": 1, "offset": 0, "shown": 1, "more": false},
         {"file": "/path/src/main.cpp", "language": "cpp",
          "mtime": "2026-06-05T09:35:18", "_match_snippet": "…InterruptIn…",
          "match_lines": [29, 105]}]
```

Covers the text that belongs to no definition, which is what
`search_bodies` cannot see: `#define`, `#include`, `#ifdef`, `extern "C"`,
and a comment at file scope. Results are file-level, with one entry for
each matching file. Use `search_bodies` when you want the symbol that
holds the match.

`match_lines` gives the lines of the file that hold a query term, up to
20 of them — the line numbers of the file itself, because an inactive
`#ifdef` branch is a blank line and the count never shifts. The field is
absent when FTS5 matched a variant of the token that the term is not a
substring of: `SELF_TEST` matches a file that writes `Self tester`, and
no line there holds `self_test`. Read `_match_snippet` in that case.

When `files_fts` is missing, in a legacy index, this tool falls back to a
LIKE search on `files.content`. The results include `_fallback: "like"`,
and no snippet highlighting. Run `fw-context index` to upgrade. A query
that FTS5 refuses to parse takes the same path and adds a `warning` with
a `hint` at the head of the list.

**FTS5 query tips for `search_content`:** fw-context gives every bare
term a trailing `*` and OR-joins them. Prefer single-word queries.

#### `lookup_symbol`

Find a symbol by name (exact or prefix match). Searches functions, methods,
classes, enums, typedefs, variables, fields, and **macros**.

```
Input:  {"name": "uart_init", "project_root?": "/path/to/project", "exact?": true, "limit?": 50, "offset?": 0}
Output: [{"total": 1, "offset": 0, "shown": 1, "more": false},
         {"name": "uart_init", "qualified_name": "drv::uart_init", "kind": "function",
          "file": "/path/src/uart.c", "line": 42, "is_definition": true,
          "signature": "void uart_init(int baudrate)", "docstring": "Initialize UART"}]
```

A member carries `class`: the class, struct, or union that declares it.
A free function has no `class`. This field tells two same-name methods
apart.

When no symbol matches, a trailing dict can hold `_did_you_mean` with
suggested names. The symbols of the first suggestion that matches exactly
then come back, each with `_fallback: true`. Their name is NOT the name
that you asked for. With no match and no suggestion, the list is empty and
has no page notice.

Macro example:
```
Input:  {"name": "CONFIG_UART_BAUDRATE", "project_root?": "/path/to/project", "exact?": true}
Output: [{"name": "CONFIG_UART_BAUDRATE", "kind": "macro",
          "file": "/path/include/config.h", "line": 15,
          "signature": "#define CONFIG_UART_BAUDRATE",
          "is_function_like": false,
          "value": "115200", "expanded_value": "115200"}]
```

A function-like macro shows its parameters in `signature`, and `value` holds
the replacement text alone:
```
Output: [{"name": "MIN", "kind": "macro", "signature": "#define MIN(a, b)",
          "is_function_like": true, "value": "((a) < (b) ? (a) : (b))"}]
```

`signature` is `#define NAME` for an object-like macro, `#define NAME()` for
a function-like macro that takes no argument, and `#define NAME(a, b)`
otherwise. `is_function_like` is what separates the first two: both have an
empty parameter list. `get_source`, `explain_symbol` and the macro step of
`search_code` write the same spelling.

The order is stable: definitions before declarations, then by line, then
by file and USR. Thus two pages never overlap. Use `exact: true` for an
exact name match. The default is a prefix match: `uart` matches
`uart_init`, `uart_write`, and other names with that prefix.

fw-context extracts macros with `clang -dM -E`, using the compiler flags
from `compile_commands.json`. So a macro with an `#ifdef` condition
resolves correctly for the indexed build configuration. Enum constants
include `enum_value` when the value is not `None`.

#### `smart_search`

Natural language → FTS5 keywords via Ollama (optional).

```
Input:  {"query": "how does the modem connect to the network?", "project_root?": "/path/to/project", "limit?": 20, "offset?": 0}
Output: [
  {"_generated_queries": ["network_reg*", "modem_attach*", "pdp_context*"]},
  {"_rough_queries": ["modem", "connect", "network"]},
  {"total": 64, "offset": 0, "shown": 20, "more": true, "hint": "…offset=20…"},
  …symbol results…
]
```

Multi-phase pipeline: translate → rough search → LLM query generation
→ FTS5 search → refine → vector re-rank → adaptive fusion → deduplicate
→ expand context → format.

When no symbol matches, the result holds the metadata entries and a dict
with `info`. One dict with `error` means that the query failed.

**Pages.** `limit` is the size of one page, from 1 to 100. The search
ranks up to 100 symbols, and `total` counts them. An LLM writes the
queries of this tool, thus two runs of one question can find different
symbols. For this reason, all pages of one answer come from one run:

- An `offset` of 0, or no `offset`, starts a new search. Its answer
  replaces the stored answer.
- A larger `offset` gives a page of the stored answer, when that answer
  is of the same query on the same build.
- When the stored answer is of another query, another project or another
  build, or when the server holds no stored answer, the tool runs the
  search first. Then it stores the answer and gives the page.

The server stores one answer only. No other tool changes it, and it has
no time limit. An index run that keeps the build keeps the stored answer.
The metadata entries come with each page. A page after the last symbol
gives one `info` dict that names the total.

The whole pipeline has a time limit: `[llm] timeout` of the project
(default 600 s). When the pipeline passes it, a leading dict holds
`_partial: true`, a `warning` with the timeout, and a `hint`. The symbols
that follow are the ones that the steps before the timeout found, without
the later re-rank steps; there can be none. This answer has no page
notice, and the server does not store it. Thus a next page runs the
search again. A timeout at `offset` 0 also removes the older stored
answer.

When you disable Ollama (`[llm] enabled = false`), this tool falls back to
a word-split FTS5 search. Phase 0 auto-translates a non-English query.

#### `semantic_search`

Concept search using pre-computed symbol embeddings. Finds symbols that
are conceptually related to a natural-language query, even when the
query words do not appear literally in the code.

```
Input:  {"query": "parcel locker state machine", "project_root?": "/path/to/project", "threshold?": 0.60, "limit?": 20, "offset?": 0}
Output: [{"total": 175, "offset": 0, "shown": 20, "more": true, "hint": "…offset=20…"},
         {"name": "set_shipment", "qualified_name": "Locker::set_shipment",
          "kind": "method", "file": "/path/src/locker.cpp", "line": 118,
          "is_definition": true, "signature": "void set_shipment(int id)",
          "docstring": "", "_method": "embedding", "_similarity": 0.8123}, …]
```

Uses cosine similarity over variable-dimension embeddings (generated during
`fw-context index --embeddings`). Models: mxbai-embed-large → 1024-dim,
qwen3-embedding → 4096-dim. **When to prefer over `search_code`:**
conceptual queries ("power consumption" → `get_load_power`) where keywords
do not match. **When to prefer `search_code`:** known keywords or symbol
names (`"fram_write"`, `"cbor encode"`).

Each symbol holds `name`, `qualified_name`, `kind`, `file`, `line`,
`is_definition`, `signature`, `docstring`, and `_method` (`"embedding"` or
`"search_code_fallback"`). An `"embedding"` result also holds
`_similarity`, the raw cosine similarity. A symbol also holds `summary`,
`inputs`, and `outputs` when `fw-context index --analyze` wrote them (model
text, not a fact), and `_rerank_score` when `llm.reranker_model` is set.
Source-aware ranking multiplies the similarity of project code by 1.2 and
of all other code by 0.85. The multiplied score sets the order, and
`_similarity` does not show it. `threshold` applies to the raw cosine
similarity.

**Pages.** The tool ranks every symbol above `threshold`, as far as one
KNN query reaches: 4096 vector rows. A symbol of several chunks uses
several rows. A page is a slice of that order: windows of 200 raw ranks,
and inside a window the multiplied score, then the symbol id. Thus the
multiplier moves a symbol only inside its window, and the first page is
the one that the tool gave before it paged. `total` counts the whole set.
When the KNN query stops at
4096 rows and its last row is still above `threshold`, the notice holds
`total_capped: true`, and `total` is a lower bound. `limit` is the size of
one page, from 1 to 100. The hint repeats a `threshold` that is not the
default. The reranker changes the order of the first symbols of a page
only, and it moves no symbol to another page. A page after the last
symbol gives one `info` dict that names the total.

**Relevance floor.** When the best raw cosine similarity of all matches is
below 0.68, the result is the page notice and one dict: a `warning` that
the matches are likely unrelated, `_fallback_suggestion: "search_code"`,
`_best_similarity`, and the matches of the page in `_results`.

When the embedding search finds no match above `threshold`, the tool runs
the lexical fallback below, with a `warning` that suggests a lower
threshold. When a phase of the search failed, that `warning` gives the
error of the phase. One dict with `error` means that the query failed.
When compile_commands.json changed after the last index run, a trailing
dict holds a `warning` and a `hint`.

**Threshold guidance** (mxbai-embed-large):
- `0.50` — exploratory, more results
- `0.55` — balanced, ~1000 results
- `0.60` — high precision (default)
- `0.65` — strict, may miss relevant symbols

Requires `[llm] enabled = true`, embeddings in the index, and, for an
Ollama embedding model, a running Ollama server. A sentence-transformers or
`ft://` model embeds the query locally and needs no Ollama. When one of
them is missing, when the embedding model fails, or when no match is above
`threshold`, this tool runs ONE plain FTS5 symbol search (the first step of
`search_code`, with no kind filter and no relaxation). The fallback gives
one page at `offset`, with the page notice of `semantic_search`. A
leading dict holds a `warning` with the reason, the page notice follows,
and each symbol holds `_method: "search_code_fallback"`. When the lexical
search also finds nothing, the result is one `warning`.

#### `find_variables`

Find C/C++ variables by name or part of it, and trace who reads or writes them
through the call graph. Splits variables into global (`varglobal`: file,
namespace, or class scope) and local (`varlocal`: inside a function body).

```
Input:  {"name": "g_", "project_root?": "/path/to/project", "kind?": "varglobal", "limit?": 20, "offset?": 0}
Output: [{"total": 7, "offset": 0, "shown": 7, "more": false},
         {"name": "g_debug_level", "qualified_name": "g_debug_level",
          "kind": "varglobal", "file": "/path/src/logging.c", "line": 15,
          "signature": "int g_debug_level",
          "enclosing_function": "<file scope>", "enclosing_class": "",
          "references": [
            {"function": "log_init", "file": "/path/src/logging.c", "line": 42, "ref_kind": "ref"},
            {"function": "log_write", "file": "/path/src/logging.c", "line": 68, "ref_kind": "ref"}
          ],
          "references_total": 2}, …]
```

Each result includes a type signature, for example `"bool timeSet"` or
`"const IPAddress modbus_ip"`. Each result also includes the enclosing
function for a local variable (`"<file scope>"` for a global variable),
and the enclosing class for a static member. Each result includes a
`references` list, showing the functions that read or write the
variable. This list uses the same `ref_kind` values as `find_references`:
`"call"`, `"ref"`, `"member"`. The list is capped at 30 references per
variable, and holds only the references of the selected build (variant and
image). `references_total` counts every reference of the variable. When
the cap cuts the list, `references_hint` names the `find_references` call
that pages every reference.

`name` is a substring match on the name and on the qualified name: `g_`
finds `g_debug_level` and also `msg_count`. The result holds only
definitions, globals first, then by name. No match gives `[]`.

The page notice comes before the variables, and `total` counts every
variable that matches. `limit` (max 100) is the size of one page. The
hint repeats `kind`. A page after the last variable gives one `info` dict
that names the total.

Use `find_variables` when you need to:
- understand shared state
- find who modifies a global variable
- trace side effects
- distinguish important globals from loop counters

For a general symbol search, use `search_code` or `lookup_symbol`. For all
references to a specific variable, including reads in expressions, use
`find_references`.

The `kind` parameter accepts `"varglobal"`, `"varlocal"`, `"field"`, or
`None` for all of them. It also accepts the legacy `"variable"`, for an
index made before the split. Another value causes an error. Results
include legacy rows that still use `kind="variable"`. Reindex the project
to get the full benefit of the split.

### Understanding

#### `get_file_map`

Structural overview of a file: all symbols grouped by kind. This tool
works like a fast table of contents, before you read the whole file.

```
Input:  {"file_path": "src/net_msg.cpp", "project_root?": "/path/to/project", "signatures?": false, "max_per_kind?": 30, "kind?": null, "offset?": 0}
Output: {"file": "src/net_msg.cpp", "total_symbols": 426,
         "symbols": {
           "method": {"count": 45, "items": [
             {"name": "_is_socket_ok", "qualified_name": "ModemMsg::_is_socket_ok",
              "line": 140, "end_line": 152}, …],
             "hint": "get_file_map('src/net_msg.cpp', kind='method', offset=30) reads the next page."},
           "varglobal": {"count": 3, "items": [
             {"name": "_buffer_msg", "qualified_name": "_buffer_msg", "line": 105}, …]},
           "constructor": {"count": 1, "items": [
             {"name": "ModemMsg", "qualified_name": "ModemMsg::ModemMsg",
              "line": 130, "end_line": 138}]},
           "struct": {"count": 2, "items": [
             {"name": "buffer", "qualified_name": "buffer", "line": 3561, "end_line": 3570}, …]},
           "enum_constant": {
             "count": 8,
             "items": [],
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

Each kind maps to `{count, items}`. `count` is the real total of that
kind. `items` holds the first `max_per_kind` symbols (`0` = no limit).
A group that `max_per_kind` cut also holds `hint`: the `get_file_map`
call with `kind` that pages the items of that kind. For `enum_constant`,
the hint starts at the first constant, because fw-context cuts each
subgroup separately.

**One kind.** With `kind`, the answer is one flat page of the items of
that kind, enum constants too:

```
Input:  {"file_path": "src/net_msg.cpp", "kind": "method", "max_per_kind": 30, "offset": 30}
Output: {"file": "src/net_msg.cpp", "kind": "method", "count": 45,
         "items": [{"name": "send", "qualified_name": "ModemMsg::send",
                    "line": 410, "end_line": 432}, …],
         "page": {"total": 45, "offset": 30, "shown": 15, "more": false}}
```

`max_per_kind` is then the size of the page (`0` = every item from
`offset` on), and `offset` skips that many items. `page` holds the page
notice. The hint repeats `signatures: true`. A kind that the file does
not hold gives one `info` dict that names the kinds of the file. A page
after the last item gives one `info` dict that names the total.

fw-context groups enum constants into `subgroups`, by the parent enum.
For `enum_constant`, `items` is empty.
Each subgroup has a `name` (the parent enum), a `count` (the real total,
even when `max_per_kind` limits the `constants` list), and a `constants`
list with `name`, `qualified_name`, `line`, and `enum_value` (the integer
value, when available).

Each item holds `name`, `qualified_name`, and `line`. A definition also
holds `end_line`, thus `file:line-end_line` is the citation. A declaration
has no extent and gets no `end_line`. An item holds `signature` only when
you pass `signatures: true`.

Pass a relative path, such as `src/main.cpp`, or just the filename, such
as `main.cpp`. Use this tool instead of `Read` on large files. fw-context
organizes the symbols by kind, in a single response.

**Path matching.** fw-context tries the exact path first. Then it matches
the path as whole trailing path segments: `config.h` matches
`src/config.h`, but not `hw_config.h`. When more than one file matches,
a project file wins over a vendor file, and then the shorter path wins.
`read_file` uses the same rules.

**The index decides access.** Any file of the indexed build is readable,
also an SDK header outside the project tree. A path that the index does
not hold is refused with an `error`.

#### `get_source`

Read a symbol's definition body — no LLM, fast.

```
Input:  {"name": "adc_read", "project_root?": "/path/to/project"}
Output: {"name": "adc_read", "kind": "function", "file": "/path/src/adc.c",
         "line": 55, "signature": "uint16_t adc_read(uint8_t channel)",
         "source": "  55  uint16_t adc_read(uint8_t channel) {\n  …\n  70  }"}
```

Uses libclang's `end_line` for exact body boundaries. Falls back to
brace-matching for older indexes. The result also carries `end_line`, thus
`file:line-end_line` is the citation — no counting.

Every line of `source` starts with its line number in the file: four
columns, right-aligned, then two spaces. The `source` of `search_bodies`
is bare, and so is the `content` of `read_file` until you ask for
`line_numbers=True`.

The body is **ifdef-filtered**. A line of an inactive `#if` branch comes
back blank, and the line numbers do not move. `source_origin` says where
the text came from:

| `source_origin` | Meaning |
|-----------------|---------|
| `"index"` | The filtered copy that the index holds. |
| `"disk"` | The file on disk. It holds every `#if` branch. |

The body comes from the disk when the file changed after the last index
run and the symbol did not move. The result then also holds `stale: true`
and a `stale_warning`. Read both before you cite such a body. When the
symbol moved, the body comes from the index, with `stale: true` and a
`stale_warning`, and its line numbers can be out of date. A symbol that
the index holds no body for, such as a declaration, also reads from the
disk.

`_source_truncated: true` marks a body that a cap cut. `[index]
max_symbol_body_lines` limits the number of lines, and a second cap limits
the characters (8000). The character cut can stop in the middle of a line.
Read the rest with `read_file` and a line range.

When the name matches more than one symbol, the body belongs to one of
them, and the result adds:

- `ambiguous_warning` — one sentence that names the symbol this answer is
  about.
- `candidates` — up to 20 of the most referenced matches. Each row holds
  `qualified_name`, `class`, `kind`, `file`, `line`, `signature`, and
  `in_project_root`.
- `candidates_total` — how many symbols the name matches.

To get another symbol, ask again with a `qualified_name` from
`candidates`. For the full list, page through
`lookup_symbol(name, exact=True, offset=…)`.

The symbol comes from the index, thus a body in a file outside the project
root, such as an SDK header, is returned and not refused.
`in_project_root` on each candidate tells project code from vendor code.

For enum constants, the result includes `enum_value` (the integer value).
For enums, the result includes a `constants` array that lists the member
constants, with their names and values, in the order of the source:

```
Input:  {"name": "BleCmd::StatusCode", "project_root?": "/path/to/project"}
Output: {"name": "StatusCode", "kind": "enum", "file": "/path/src/radio_cmd.h",
         "line": 20, "signature": "",
         "constants": [
           {"name": "OPERATION_SUCCESSFUL", "enum_value": 1},
           {"name": "TOKEN_INVALID", "enum_value": -2}
         ],
         "constants_total": 2,
         "source": "  20  enum StatusCode {\n  …\n  24  }"}
```

`constants` holds at most 200 constants. `constants_total` counts all of
them. When the list is cut, `constants_hint` names the
`lookup_symbol('<enum>::')` call that pages every constant.

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
         "is_function_like": false,
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
Ollama directly. When the LLM is disabled (`[llm] enabled = false`), this
tool returns `explain_prompt` and no `source`. The source window is part
of the prompt text. When the Ollama call fails (timeout, missing model,
or another error), the result holds a `warning`, `source`, and
`explain_prompt`. In both cases, the AI assistant can answer with its own
model.

fw-context generates the analysis during indexing. fw-context uses the
ifdef-filtered body that the index holds and the callee names from the
reference index, as context. Code in an inactive `#if` branch does not
reach the model. When every line of a body is inactive, fw-context skips
the symbol and calls no model, because the build compiles none of its
code. When you re-index a file, with `reindex_file`, fw-context
regenerates the analysis automatically.

The on-demand fallback reads a window of `context_lines` around the
symbol from the disk. That window is not ifdef-filtered.

When the name matches more than one symbol, the result adds
`ambiguous_warning`, `candidates`, and `candidates_total`, in the same
shape as `get_source`. When the file changed after the last index run,
the result adds `stale: true` and a `stale_warning`.

#### `get_symbol_context`

Rich LLM context — body, callers, and callees in one response. Returns
the direct callers and callees, including vendor and SDK code. The call
graph naturally spans the project and vendor boundaries. Each list has a
maximum, and a count of the whole list.

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
         "callers_total": 2, "callees_total": 3, "indirect_call_sites_total": 0,
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
does this do, and how does it fit?" in a single call. Each list stops at
a maximum:

| List | Maximum | Tool that pages it |
|------|---------|--------------------|
| `callers` | 50 `call` and `indirect` references, each with its line | `find_callers` |
| `callees` | 100 rows, one for each callee and kind of reference | `find_callees_recursive` with `max_depth=1` |
| `indirect_call_sites` | 200 entries | `find_indirect_call_sites`, for a field or a variable |

`callers_total`, `callees_total`, and `indirect_call_sites_total` count
each whole list. When the maximum cuts a list, `callers_hint`,
`callees_hint`, or `indirect_call_sites_hint` names the tool that pages
it. `indirect_call_sites_hint` is there only for a field or a variable.
The hint names no offset: the paged tool counts a wider set, thus its
pages do not continue the list row for row.

For a field or variable symbol that has a function pointer type, the
result also includes a `resolution` block:
`{assignments_found, call_sites_found, resolved, note}`. This block
indicates whether the assignments and the call sites are linked (Phase 3).
When the data is incomplete, `resolved` is `false`, with an explanatory
note. The LLM can detect the uncertainty from this signal.

For enums, the result includes a `constants` array, with the member
constants and their values, in the same shape as `get_source`: at most
200 constants, `constants_total`, and `constants_hint` when the list is
cut. Enum constants include `enum_value`.

The body follows the same rules as in `get_source`: it is ifdef-filtered,
`source_origin` says `"index"` or `"disk"`, `_source_truncated` marks a
cut body, and an ambiguous name adds `ambiguous_warning`, `candidates`,
and `candidates_total`. Every part of the answer — body, callers, and
callees — is about the one symbol that `ambiguous_warning` names. When
the file changed after the last index run, the result adds `stale: true`
and a `stale_warning`. The callers and callees always come from the
index, thus a stale result can hold an incomplete list.

#### `read_file`

Read a source file, one page at a time, with ifdef-filtered content —
only code that actually compiles for the current build configuration.
fw-context replaces an inactive `#ifdef` branch with blank lines, and
keeps the original line numbers.

```
Input:  {"file_path": "src/modem.c", "project_root?": "/path/to/project", "start_line?": 0}
Output: {"file": "/path/src/modem.c", "language": "c", "mtime": 1748534400.0,
         "lines": 512, "content": "/* Modem driver */\n\n#include \"modem.h\"\n…",
         "page": {"total": 512, "offset": 0, "shown": 512, "more": false}}
```

Unlike a generic file reader, this tool returns build-accurate content.
Code that is behind `#ifdef BOARD_V2` is visible only when the build
defines `BOARD_V2`.

The path can be relative to the project root, such as `src/main.cpp`, or
just the filename, such as `main.cpp`. The path matching and the access
rules are the same as in `get_file_map`. This tool falls back to the raw
disk content, with a `warning`, when the indexed `files.content` column is
empty. Run `fw-context index` to populate the ifdef-filtered content.

Comments and preprocessor directives stay in the content: an include
guard, a `#define`, and a block comment with its closing marker. Thus a
blank line is an inactive line, or a line that is blank on disk.

When every line of the file is inactive, the result holds
`all_lines_inactive: true` and a `warning`. Such a file holds code, and
the active build compiles none of it. It is NOT an empty file.

When the file changed after the last index run, the result holds
`stale: true` and a `stale_warning`: the content comes from the index, not
from the disk.

`line_numbers=True` prefixes every line with its number, in the format
that `get_source` uses. `start_line` and `end_line` cut a window out of
the file — 1-based, both ends inclusive. A `start_line` of 0 starts at
line 1. An `end_line` of 0 gives one page of at most 2000 lines. Reading
around a known line costs a fraction of the whole file.

**Pages.** Without `end_line`, the answer is one page of at most 2000
lines from `start_line` on. `page` holds the page notice, in lines:

- `total` is the number of lines of the file.
- `offset` is the number of lines before the page.
- `shown` is the number of lines on the page.

When `more` is true, the `hint` names the `read_file` call with the next
`start_line`, for example
`read_file('src/stm32f4xx.h', start_line=2001) reads the next page.`
The hint repeats `line_numbers=True`. A page that is not the whole file
also holds `start_line` and `end_line`. An explicit
`end_line` gives the whole range that it names, and the answer then has
no `page`.

```
Input:  {"file_path": "src/command.h", "start_line": 25, "end_line": 31, "line_numbers": true}
Output: {"file": "/path/src/command.h", "language": "c", "mtime": 1748534400.0,
         "lines": 140, "start_line": 25, "end_line": 31,
         "content": "  25      CLEAR_SD     = 11,\n  …"}
```

`lines` stays the length of the whole file. `start_line` and `end_line`
in the result say which lines the `content` really holds, after the end
was clamped to the length of the file. A negative bound, an `end_line`
before `start_line`, or a `start_line` past the end of the file gives an
`error`.

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
Input:  {"name": "uart_write", "project_root?": "/path/to/project", "limit?": 50, "offset?": 0}
Output: [{"total": 2, "offset": 0, "shown": 2, "more": false},
         {"file": "/path/src/main.c", "line": 35, "ref_kind": "call",
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

**Ambiguous name.** When the name matches more than one symbol, such as
two classes with a method of the same name, the answer holds the call
sites of all of them. A leading `warning` row names the symbols, and each
row carries `target_qualified_name`, the symbol that the row belongs to.
Give the full qualified name to ask about one symbol only. The same
applies to `find_references`, `find_call_path`,
`find_all_callers_recursive`, and `find_callees_recursive`.

**Virtual methods.** A virtual method with no call site of its own
answers with the call sites of its base method and of the sibling
overrides of that base. A `warning` row leads such an answer, and each
row carries two more fields:

- `recorded_against` — the symbol that the index records the call
  against: the base method, or a sibling override.
- `reaches_this_symbol` — `true` when the row is recorded against the base
  method. Such a call reaches this override when the object has this
  type. A row with `false` is recorded against a sibling override, and it
  reaches that sibling and not this method.

Read both fields before you report a caller count. `find_references`
behaves in the same way.

#### `find_references`

All uses of a symbol — calls, reads, member access, indirect references.
When fw-context does not find the symbol, this tool automatically falls
back to a **macro lookup**. In that case, this tool returns
`ref_kind: "macro_use"` for a file that uses the macro.

```
Input:  {"name": "g_sensor_data", "project_root?": "/path/to/project", "limit?": 50, "offset?": 0}
Output: [{"total": 74, "offset": 0, "shown": 50, "more": true,
          "hint": "find_references('g_sensor_data', offset=50) reads the next page."},
         {"file": "/path/src/sensor.c", "line": 12, "ref_kind": "ref",
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
arguments, assignments, initializers, or init lists),
`"implicit_construct"` (implicit constructor call from a global or static
object, or from member-field initialization), `"dispatch"` (a synthetic
edge from a dispatch bridge, such as `EventQueue::call_every`), `"macro_use"` (macro usage
in file), `"vector"` (a slot of an assembly vector table names the
symbol; `file` and `line` are the table entry), `"vector_data"` (a slot
holds an address computed from the symbol, such as the initial stack
pointer), `"runtime_vector"` (a `NVIC_SetVector` call installs the symbol
into a slot at run time), `"alias"` (`.thumb_set`: another assembly name
for the symbol).

#### `find_call_path`

Find paths between two functions via BFS in the call graph.

```
Input:  {"from_name": "main", "to_name": "uart_send_byte", "project_root?": "/path/to/project", "max_depth?": 10}
Output: [{"depth": 3, "chain": "main → app_init → uart_write → uart_send_byte",
          "target_usr": "c:@F@uart_send_byte"}]
```

Returns up to 5 distinct paths, in the order that the BFS finds them. The
order is fixed: the same query gives the same paths. The tool does not
check that a path is the shortest. The search runs from both ends, and
each end goes up to `max_depth` hops. Thus a path can hold up to
2 × `max_depth` edges. When the search finds no path within the depth,
the result is one `info` dict. Requires both symbols to be in the index.
Each path carries `target_usr`, the USR of the symbol
that the path ends at. It tells two overloads apart. A chain that the
search finds at several meeting points is reported once.

When `to_name` matches more than one symbol, the search reaches all of
them, and each path also carries `target_qualified_name`. The answer can
then hold two direct paths to two targets of the same name.

**No pages.** This tool takes no `offset` and gives no page notice,
because fw-context cannot count the paths. A trailing `info` row says
when the answer can leave out paths:

- The search found 5 paths and stopped. More paths can exist.
- The search used its budget of node expansions before it reached each
  node within the depth. More paths can exist.

When the search used its budget and found no path, the answer is one
`info` row. That row says that a path can still exist, and NOT "no path".
Then name a start or a target that is nearer, or use `find_callers`.

#### `find_all_callers_recursive`

All transitive callers — who calls this, directly or indirectly?

```
Input:  {"name": "gpio_set", "project_root?": "/path/to/project", "max_depth?": 5, "limit?": 50, "offset?": 0}
Output: [{"total": 12, "offset": 0, "shown": 12, "more": false},
         {"name": "led_toggle", "qualified_name": "led_toggle", "kind": "function",
          "file": "/path/src/led.c", "depth": 1}, … (2 steps away), … (3 steps away)]
```

fw-context deduplicates the results. Each caller appears once, at its
shortest distance.

The page notice comes before the rows, and after the `warning` row of an
ambiguous name. `total` counts every caller within `max_depth`. The hint
repeats `max_depth` when it is not the default. A page after the last row
gives one `info` dict that names the total. `find_callees_recursive` pages
in the same way.

Each row holds `name`, `qualified_name`, `kind`, `signature`, `depth`, and
`file` (absolute). There is no `line`, because one caller can hold several
call sites. For the line of each call, use `find_callers` on the name
that this tool reports. `find_callees_recursive` gives the same fields.

#### `find_callees_recursive`

What does this call, directly or indirectly?

```
Input:  {"name": "main", "project_root?": "/path/to/project", "max_depth?": 5, "limit?": 50, "offset?": 0}
Output: [{"total": 87, "offset": 0, "shown": 50, "more": true, "hint": "…offset=50…"},
         {"name": "spi_init", "kind": "function", "file": "/path/src/spi.c", "depth": 1},
         {"name": "spi_transfer", "kind": "function", "file": "/path/src/spi.c", "depth": 2}, …]
```

#### `find_dead_code`

Finds functions that have a definition, but no caller. Returns two categories:

```
Input:  {"project_root?": "/path/to/project", "limit?": 100, "offset?": 0, "project_only?": true, "exclude_paths?": ["lib/%"]}
Output: [
  {"total": 12, "offset": 0, "shown": 12, "more": false},
  {"name": "orphan_fn", "kind": "function", "file": "/path/src/utils.c",
   "signature": "void orphan_fn()", "line": 200,
   "status": "dead", "reason": "no references found — likely unused"},
  {"name": "handler_timeout", "kind": "function", "file": "/path/src/main.c",
   "signature": "void handler_timeout()", "line": 25,
   "status": "possibly_dead",
   "reason": "assigned as function pointer but call sites unresolved",
   "indirect_refs": "/path/src/main.c:60"},
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

Each row also holds `qualified_name`. `file` is absolute, and `line` is
the line of the definition.

Expect additional false positives from entry points (`main`), virtual
method overrides, constructors called via factories, and weak-aliased
symbols. fw-context checks only definitions with
`kind IN ('function', 'method', 'constructor', 'destructor')`.

ISRs: a handler that a readable vector table names carries a reference,
thus it is not reported as `"dead"`. An assembly table of `.word` entries
gives a `"vector"` reference. A table that the build generates in C gives
an `"indirect"` reference, thus such a handler can appear as
`"possibly_dead"`. A handler that a `NVIC_SetVector` call installs gives a
`"runtime_vector"` reference. Expect ISRs as `"dead"` false positives
where the architecture builds its table from branch instructions (arm64,
Xtensa, MIPS), and where a table slot holds an address without a name.
Use `get_vector_table` to see the table.

By default, fw-context excludes SDK and vendor paths automatically, with
the `is_project` column, which respects the project's `vendor_paths` and
`project_paths` configuration. Use `exclude_paths` for additional LIKE
patterns on top of this. A `%` matches any suffix, for example `lib/%`.

#### `find_hotspots`

Most-called functions ranked by caller count.

```
Input:  {"project_root?": "/path/to/project", "limit?": 20, "offset?": 0, "project_only?": true, "exclude_paths?": ["lib/%"]}
Output: [{"total": 212, "offset": 0, "shown": 20, "more": true,
          "hint": "find_hotspots(offset=20) reads the next page."},
         {"name": "log_debug", "kind": "function", "caller_count": 147, …},
         {"name": "millis", "kind": "function", "caller_count": 89, …}, …]
```

Each row holds `name`, `qualified_name`, `kind`, `signature`, `file`
(absolute), `line`, and `caller_count`. For the call sites of one hotspot,
use `find_callers`.

#### `find_wrapper_callers`

Find wrapper classes that call methods of a driver class. This tool is
useful for understanding an adapter or wrapper architecture.

```
Input:  {"class_name": "UART_DRIVER", "project_root?": "/path/to/project", "limit?": 20, "offset?": 0}
Output: [{"total": 3, "offset": 0, "shown": 3, "more": false},
         {"wrapper_class": "UART", "method_count": 2,
          "methods": [
            {"method": "send", "qualified_name": "UART::send", "kind": "method",
             "file": "/path/src/uart.cpp",
             "calls": [{"driver_method": "send", "line": 45}]},
            {"method": "init", "qualified_name": "UART::init", "kind": "method",
             "file": "/path/src/uart.cpp",
             "calls": [{"driver_method": "configure", "line": 30},
                       {"driver_method": "enable", "line": 31}]}]}, …]
```

Pass a fully-qualified class name (`hal::UART_DRIVER`) or just the bare
name (`UART_DRIVER`). The name is case-sensitive. When no top-level class
has a bare name, the name matches the class of that name in each
namespace, but not a class whose name only ends with it: `Gap` does not
match `PalGap`. The methods of a class nested in the driver class count as
driver methods.

fw-context groups the results by wrapper class. A
free function goes into `wrapper_class: "(global)"`. `file` is on each
method, not on the class, because one class can span several files.
`driver_method` is the bare name of the driver method.

A page is a slice of the wrapper classes, in the order of the class name.
`limit` is the number of classes on one page (default 20, max 100). Each
class comes with all of its methods and all of its calls into the driver,
thus a class is never split over two pages. `total` counts every wrapper
class. A page after the last class gives one `info` dict that names the
total. No match gives one `info` dict.

#### `find_indirect_call_sites`

Find indirect call sites where a function pointer field or variable is invoked.

```
Input:  {"name": "onData", "project_root?": "/path/to/project", "limit?": 50, "offset?": 0}
Output: [{"total": 1, "offset": 0, "shown": 1, "more": false},
         {"file": "/path/src/main.c", "line": 36, "expr_text": "drv . onData",
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

`limit` (max 200) is the size of one page. `total` counts every call site
that matches. A page after the last call site gives one `info` dict that
names the total. `find_indirect_targets` pages in the same way.

#### `find_indirect_targets`

Find functions assigned to a function pointer field, variable, or parameter.

```
Input:  {"name": "onData", "project_root?": "/path/to/project", "limit?": 50, "offset?": 0}
Output: [{"total": 1, "offset": 0, "shown": 1, "more": false},
         {"rhs_name": "handler_data", "rhs_qname": "handler_data",
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

**Name matching.** `name` matches the pointer by exact name, exact
qualified name, or `::name` suffix, and a parameter by its name. `name`
also matches the ASSIGNED function, by exact name or substring. Thus a
row can be an assignment of a function whose name holds `name`, to a
pointer with another name. Read `rhs_name` and the call site before you
act on such a row. `_` and `%` in `name` match themselves, not any
character.

**Inferred call sites.** When the pointer has no call site of its own,
fw-context tries fallbacks. An entry that one of them filled carries
`_note`, or `call_expr_text: "<inferred via class hierarchy>"`. The
`fn_ptr_type` fallback gives a call through another pointer of the same
type: a candidate, not a proof.

For the reverse query — where does code call this field — use
`find_indirect_call_sites`.

#### `trace_data_flow`

Trace how data of a given type flows to a target function. **Experimental.**

```
Input:  {"type_name": "SensorData", "to_symbol": "uart_send", "project_root?": "/path/to/project", "max_depth?": 8, "limit?": 15, "offset?": 0}
Output: [{"total": 2, "offset": 0, "shown": 2, "more": false},
         {"_summary": "1/2 source functions on this page reach 'uart_send' within depth 8",
          "_type": "SensorData", "_target": "uart_send"},
         {"source_name": "sensor_read", "source_qualified_name": "sensor_read",
          "source_kind": "function", "source_file": "/path/src/sensor.cpp",
          "source_line": 120, "caller_count": 4, "reachable": true,
          "paths": [{"depth": 2, "chain": "sensor_read → pack_payload → uart_send",
                     "target_usr": "c:@F@uart_send"}]},
         {"source_name": "sensor_log", "source_qualified_name": "sensor_log",
          "source_kind": "function", "source_file": "/path/src/log.cpp",
          "source_line": 40, "caller_count": 1, "reachable": false}]
```

Finds the definitions whose signature mentions `type_name` (substring
match, any symbol kind, most-called first), then looks for call paths from
those definitions to `to_symbol`. The page notice comes first, and
`total` counts every source function that matches. A `_summary` row
follows, and it describes the source functions of this page. `limit`
(max 15) is the number of source functions on one page. The hint repeats
`max_depth` when it is not the default.

Each source row holds up to 3 `paths`, in the shape that `find_call_path`
gives. An unreachable source has no `paths` key. When `to_symbol` matches
more than one symbol, a source row also holds the `warning` that names
them, and each path its `target_qualified_name`. `timed_out: true` means
that the time budget (`timeout_ms`) ran out before the search for that
source began: its `reachable: false` means "not checked". Such a source
counts as shown, thus the hint goes past it. To do a check of it, call
again with the same `offset`: each page gets the whole time budget.

A page after the last source function gives one `info` dict that names
the total. When nothing matches, the result is one `info` dict. Does
**not** resolve type transformations, for example CBOR encoding. Use this
tool together with `find_call_path`, to verify specific paths.

#### `get_vector_table`

Read the interrupt vector table, and say what services each interrupt.
Nothing calls a handler — the hardware reads a slot and jumps — thus the
other graph tools show a handler as unreferenced. This tool reads the
table itself.

```
Input:  {"project_root?": "/path/to/project", "unhandled_only?": false, "limit?": 400, "offset?": 0}
Output: [{"total": 98, "offset": 0, "shown": 98, "more": false},
         {"slot": 0, "name": "__StackTop", "file": "/path/.link_script.ld", "line": 148,
          "status": "linker", "source": "assembly",
          "table_file": "/path/startup_stm32f429xx.S", "table_line": 60},
         {"slot": 44, "name": "TIM2_IRQHandler", "file": "/path/src/timer.c", "line": 31,
          "status": "c", "source": "assembly",
          "table_file": "/path/startup_stm32f429xx.S", "table_line": 104,
          "overridden": {"file": "/path/startup_stm32f429xx.S", "line": 412}},
         {"slot": 45, "name": "TIM3_IRQHandler", "file": "/path/startup_stm32f429xx.S", "line": 413,
          "status": "unhandled", "source": "assembly",
          "table_file": "/path/startup_stm32f429xx.S", "table_line": 105,
          "aliases": {"name": "Default_Handler", "file": "/path/startup_stm32f429xx.S", "line": 380}},
         …,
         {"coverage": "…"}, {"interrupts": "…"}]
```

**`slot` is the position inside its own table.** What the position means
belongs to the architecture. On Cortex-M, slots 0 to 15 are the system
exceptions, and slot 16 + n is external interrupt n. fw-context does not
renumber slots across tables: two tables both start at 0. Read `slot`
together with `table_name` and `source`.

**Paths are absolute**, as in the other graph tools. This applies to
`file`, `table_file`, and the paths in `overridden`, `aliases`, and
`installed` (also `at`, a `file:line`). A `"build"` row without a single
definition has an empty `file`.

`status` says what services the interrupt:

| Status | Meaning |
|--------|---------|
| `"c"` | A definition outside assembly. Code runs. `overridden` holds the weak definition that it replaced, when the index holds one. |
| `"assembly"` | An assembly definition that is not an alias of another symbol. Assembly services the interrupt. |
| `"unhandled"` | An assembly name that is an alias (`.thumb_set`) of another symbol, and nothing overrode it. fw-context reads this from the alias edge only, not from weakness. `aliases` names the target, usually `Default_Handler`, an infinite loop. |
| `"runtime"` | The image holds that alias, but a `NVIC_SetVector` call installs a real handler at run time (a target with `CMSIS_VECTAB_VIRTUAL`). The interrupt is serviced only after the registering code has run. |
| `"data"` | The slot holds an address built from the symbol, such as `.word sym + CONST`. On Cortex-M this is slot 0, the initial stack pointer. It is not code. |
| `"linker"` | The linker script gives the address, and no compiled file defines the name. Slot 0 of a CMSIS table looks like this. It is not code. |
| `"dispatcher"` | The target holds more than one slot of the table AND calls through a pointer, such as Zephyr's `_isr_wrapper`. It decides at run time where to go. Follow it with `get_symbol_context`. |

`source` says where a row came from:

| Source | Meaning |
|--------|---------|
| `"assembly"` | A table of `.word` or `.long` address words, as a CMSIS startup file writes it. |
| `"c"` | A C array whose elements are function addresses, as a build that generates its table writes it. The row also holds `table_name` and `table_usr`. |
| `"build"` | A registration that the build recorded, for a slot that the other two sources cannot name. The row can hold `argument`, the symbol that the build passes to the handler. It is an argument, not a second handler. |

fw-context recognizes a C table by its shape, not by its name. Thus any
array of function addresses is reported, and `table_name` tells a table
of handlers from, for example, a table of state machine steps.

An assembly row can hold `installed`: one entry per `NVIC_SetVector` call
site, with `name`, `file`, `line`, and `at` (where the registration
happens). A row with a real static definition keeps its own status and
still carries `installed`.

The page notice comes before the slots, and `total` counts every slot of
every table. The slots come in the order of source, then table, then
slot. `limit` (max 1000) is the size of one page. When `unhandled_only`
is true, the hint repeats it. A page after the last slot gives one `info`
dict that names the total, and then the rows that follow. An answer with
no slot row has no page notice.

Rows other than slots follow the slots of each page. The page does not
apply to them, thus they are on every page. They describe the whole
table:

- `coverage` — one row per C table that has slots with no function name.
  Such a slot holds a zero, an address that the linker resolved, or data.
  In a table of handlers that is an unused vector. In Zephyr's
  `_sw_isr_table` it is the opposite: the unnamed slots are the
  interrupts in use.
- `interrupts` — which interrupts the build connected and which it did
  not, where the build recorded its registrations. When the build enables
  `CONFIG_DYNAMIC_INTERRUPTS`, the text says that some interrupts can be
  connected at run time.

`unhandled_only=true` returns only the `"unhandled"` slots, and the
`interrupts` row. A build that generates its table writes no alias, thus
it gives no `"unhandled"` slot. There, the `interrupts` row is the answer.

The answer is never empty. One `info` row says when the build has no
vector table that this tool can read. An architecture that builds its
table from branch instructions (arm64, Xtensa, MIPS) gives this answer.
Use `find_references` on a handler name instead. Requires the reference
index.

### Class analysis

#### `get_inheritance_chain`

Return the C++ inheritance hierarchy for a class or struct — direct bases
(what this inherits from) and direct derived classes (what inherits from
this), with access level and virtual flag.

```
Input:  {"class_name": "UART_DRIVER", "project_root?": "/path/to/project", "transitive?": false, "max_depth?": 10, "limit?": 100, "offset?": 0}
Output: {"name": "UART_DRIVER", "qualified_name": "hal::UART_DRIVER",
         "kind": "class", "file": "/path/src/UART_DRIVER.h", "line": 45,
         "bases": [{"name": "SerialBase", "usr": "c:@...", "access": "public",
                     "is_virtual": false, "file": "/path/src/SerialBase.h"}],
         "derived": [{"name": "UART", "usr": "c:@...", "access": "public",
                       "is_virtual": false, "file": "/path/src/UART.h"}]}
```

When `transitive: true`, adds `all_bases` (ancestors BFS) and `all_derived`
(descendants BFS) with `depth` and cycle detection for diamond inheritance.

Each of the two lists is one page, in the order of depth, then name.
`limit` is the size of the page (default 100, max 500). `offset` skips
that many classes, in the two lists together. `all_bases_page` and
`all_derived_page` hold the page notice of each list. `limit` and
`offset` apply only with `transitive: true`. The hint repeats `max_depth`
when it is not the default. A hierarchy below a root class of a framework
can hold thousands of classes.

The `class_name` can be a bare name (`UART_DRIVER`) or qualified
(`hal::UART_DRIVER`). Only classes and structs are valid targets.

#### `get_class_members`

Return all methods, fields, and nested types of a class/struct grouped by kind.

```
Input:  {"class_name": "ModemManager", "project_root?": "/path/to/project", "limit?": 200, "offset?": 0}
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
         "member_count": 12,
         "page": {"total": 12, "offset": 0, "shown": 12, "more": false}}
```

A page holds the members in one order: kind, name, and line. fw-context
groups the members of a page by kind after the cut, thus one kind can
continue on the next page. `limit` is the size of one page (default 200,
max 500), and `offset` skips that many members. `member_count` counts
every member, not only the page. `page` holds the page notice. A page
after the last member gives one `info` dict that names the total.

This tool shows the full API surface, without opening the header file.
This tool also works for C structs, but a struct will not have methods. This tool returns
`member_count: 0` for an index that predates this feature.

#### `get_template_instances`

Find concrete instantiations of a class or function template.

```
Input:  {"template_name": "std::vector", "project_root?": "/path/to/project", "limit?": 50, "offset?": 0}
Output: [{"total": 3, "offset": 0, "shown": 3, "more": false},
         {"name": "vector", "qualified_name": "std::vector", "kind": "class",
          "file": "/usr/include/c++/12/bits/stl_vector.h", "line": 428,
          "is_definition": true, "signature": "",
          "instances": [{"name": "vector", "qualified_name": "std::vector<int>",
                          "kind": "class", "file": "/path/src/main.cpp", "line": 42,
                          "signature": "class vector<int>", "is_definition": true}, …],
          "instance_count": 3}]
```

The result is a list: the page notice, then one dict. That dict describes
the template itself and holds the `instances` list, which is one page of
the instances. `limit` (max 200) is the size of the page, and `offset`
skips that many instances. `instance_count` counts all instances: it is
the same number as `total`. A page after the last instance gives one
`info` dict that names the total. A dict with `error` means
that the name was not found, is not a template, or the query failed. When
a file of the answer changed after the last index run, a `warning` dict
comes first.

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
Output: {"name": "send", "qualified_name": "hal::UART_DRIVER::send",
         "kind": "method", "file": "/path/src/UART_DRIVER.cpp", "line": 88,
         "signature": "int send(const uint8_t *, size_t)",
         "overrides": [{"usr": "c:@...", "name": "send", "qualified_name": "SerialBase::send",
                         "kind": "method", "file": "/path/src/SerialBase.h", "line": 30}],
         "overridden_by": []}
```

The result has no virtual flags. For `is_virtual` and `is_pure_virtual`,
use `get_class_members` or `lookup_symbol`. A related `file` can be
`null` when the index has no path for it. When a file of the answer
changed after the last index run, the dict adds `stale: true` and a
`stale_warning`. On failure, the dict holds `error`.

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

This tool is read-only, and it creates no `.fw-context` file in a project
that is not initialized. Use it at session start, to see everything that
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
         "indexed_at": "2026-06-05 09:35:18", "symbol_count": 12430, "file_count": 1502,
         "reference_count": 8900, "modified_files_count": 3,
         "header_affected_tus": 0, "manifest_verification": "full",
         "schema_version": 84935291, "current_schema": 84935291,
         "analysis": {"model": "qwen2.5-coder:14b", "analyze_vendor": false,
                      "project": {"analyzed": 8450, "skipped": 6, "total": 8570},
                      "vendor": {"analyzed": 0, "skipped": 0, "total": 42330},
                      "complete": false},
         "vendor_paths": [], "project_paths": [], "effective_vendor_patterns": ["zephyr/%", …],
         "bg_reindex_running": false,
         "status": "ready", "reindex_needed": false, "reindex_reasons": [],
         "index_message": "Index is fully up to date (12430 symbols) | LLM analysis: project 8450/8570 (vendor skipped — analyze_vendor=false)",
         "entry_point": "__start",
         "memory": [{"name": "FLASH", "attributes": "rx", "origin": "0x0", "length": "0x100000",
                     "origin_value": 0, "length_value": 1048576,
                     "file_path": "build/zephyr/linker.cmd", "line": 12}, …],
         "defines": {"__ZEPHYR__": "1", …}, "defines_varying": 4}
```

**Read-only.** It creates no `.fw-context` file in a project that is not
initialized. This tool does not spawn background tasks. The server startup thread and the file watcher manage the
background reindex. `bg_reindex_running` reports whether an index run is
active. Only while a run is active, `reindex_progress` holds the newest
progress or error line that fw-context wrote for that run. Output of the
build tools in the same log is skipped.

**`status` field (use for decision-making):**

| Status | Meaning | What to do |
|--------|---------|------------|
| ``"ready"`` | Fully up to date, no issues | Continue normally |
| ``"reindexing"`` | Background reindex in progress, or fw-context is about to build a new source file by itself | Index is still usable — continue normally |
| ``"reindex_needed"`` | A structural cause — see `reindex_reasons` below | Queries still work, but read `reindex_reasons` for the command |
| ``"no_index"`` | Initialized, but `index.db` does not exist | Run ``fw-context index`` |
| ``"not_initialized"`` | ``fw-context init`` was not run | Run ``fw-context init`` |

A failure sets no `status`. On DB corruption, an access error, or a
database that holds no build config, the result is a dict that holds only
`error`. Read that key first.

`"reindex_needed"` outranks `"reindexing"`: the answers of this moment
come from the last finished run. When `bg_reindex_running` is `true` at
the same time, `index_message` says to wait for the running index run
instead of naming a command.

**`reindex_reasons`** names each cause of `"reindex_needed"`:

- Schema mismatch (`schema_mismatch: <old> < <new>`).
- Older row format (`row_format_mismatch: <old> != <new> — <effect>`).
  The stored text has an older meaning. For example, an index written
  before `fw-context-rows/1` keeps every inactive `#if` branch, thus
  answers can hold dead code.
- compile_commands.json changed.
- A source file on disk is missing from compile_commands.json, and
  fw-context will not build it by itself. The reason says why.
- The tree is on another git branch than the index.
- Another build of the project (`build <variant>/<image>: …`) has a changed
  or missing database.

A missing source file and a branch change need `fw-context index --build`,
because only a build regenerates compile_commands.json. The others need
`fw-context index`.
Read the reason text: it names the command.

**`stale_builds`** lists the other builds of the project — each
`(variant, image)` — that are stale, as
`{variant, image, reindex_needed, reasons}`. A query names one build, thus
another build can be stale while the build of a query without `variant`
and `image` is current: a change in a file that only the bootloader
compiles. A changed or missing database of such a build sets
`reindex_needed`. Rows of an older row format set it too, because the builds
of one project are not always indexed together. The reason is then
`row format <old> != <current>`. Modified files and changed headers make it
stale, and set `stale`.

One reason does not set `"reindex_needed"`: a new source file that is
missing from compile_commands.json, when fw-context will build it by
itself. That reason is in `reindex_reasons` while `reindex_needed` is
`false` and `status` is `"reindexing"`.

**`client_restart_required`.** When the index holds a NEWER row format
than this server process reads, the result holds
`client_restart_required: true` and `client_restart_reason`, and
`index_message` opens with it. `status` stays `"ready"`, and every query
keeps working. Do NOT reindex: the indexer writes the same new format
again. Tell the user to restart the LLM client (Claude Code, opencode, or
the client in use). The MCP server is a child process of that client.

`modified_files_count` counts the files whose content no longer matches
the index. Both modes count it. `fast` controls only the header check:
`fast=true` (the default) reuses the cached manifest hashes, and
`fast=false` recomputes them, which is much slower.
`header_affected_tus` reports how many source files have stale header
dependencies. A file that `compile_commands.json` lists more than once
counts once. This count is non-zero when headers changed since the
last index with `manifest_verification: "full"`.

`effective_vendor_patterns` holds the SQL LIKE patterns that this build
really used to mark vendor code. `vendor_paths` and `project_paths` hold
what the config asks for. The list is empty when the manifest cannot be
read.

**Build facts.** `entry_point` is the `ENTRY()` of the linker script of
the build that `config_hash` names. `memory` lists the `MEMORY` regions of
that script: `{name, attributes, origin, length, origin_value,
length_value, file_path, line}`. `origin` and `length` are the expression
that the script writes. `origin_value` and `length_value` are numbers, or
`null` for an expression that names a symbol. An empty value means "not
recorded", never "no memory". A build system such as Keil or IAR records
no linker script. A PlatformIO project records its link only in
`fw-context index --build`, thus an empty value there means that no build
recorded the link yet.
`defines` holds the `-D` flags that every translation unit of the build
carries with one value. `defines_varying` counts the names that are left
out because they are not on every unit or have different values. In a
multi-variant project with no `[build] default_variant`, the four fields
are empty. Use `list_variants` for the map of every build.

`manifest_verification` is `"full"` when `manifest.json` is available. In
that case, fw-context tracks header staleness with SHA-256 hashes that
fw-context collects during indexing. `manifest_verification` is `"none"`
when no manifest exists. In that case, fw-context cannot detect header
changes. Run `fw-context index --build` to regenerate the index with full
dependency tracking.

`analysis` splits LLM-analysis coverage by project vs vendor symbols so
the reader can tell intentionally-skipped vendor symbols apart from
project symbols that still need analysis:

- `project.analyzed` / `project.total` — project definition symbols.
- `vendor.analyzed` / `vendor.total` — vendor/SDK definition symbols.
- `project.skipped` / `vendor.skipped` — symbols that the pipeline tried
  and cannot analyze. The body is larger than the model context, or the
  model gave an unparseable answer. fw-context stores a `skip:` sentinel
  for these symbols and does not try them again.
- `analyze_vendor` — whether vendor analysis was enabled at index time.
- `complete` — `true` when the pipeline has no more work: every project
  symbol is analyzed or skipped (and, when `analyze_vendor` is true, every
  vendor symbol too).

`index_message` also gives the analysis coverage, split into project and
vendor symbols. An unanalyzed symbol is not a reindex reason.

When `analyze_vendor` is `false`, vendor symbols are skipped by design —
a large unanalyzed vendor count is normal, not a defect.

`indexed_at` and `first_indexed_at` are UTC, in `"YYYY-MM-DD HH:MM:SS"`
format. File modification times are local time. Do not compare the two
directly. In UTC+2, a file that fw-context indexed correctly looks 2 hours
newer than `indexed_at`. To find modified files, use `modified_files_count`.

**Multi-variant output.** The result holds these fields:

- `multi` — `true` for a multi-variant project: the config declares
  variants, or the index holds a build with a variant name
- `variants` — a list of `{name, description, board}`: the declared
  variants, plus the indexed variants that the config does not declare
- `images` — a list of `{name, description, dir, type, board?}`
- `variant_images` — a map of variant name to its image names. It is empty
  for a project without variants. The images of its build are in `images`
- `active_variant` — the `[build] default_variant` value
- `active_image` — the image that a query without `image` gets, in the
  default variant: the `[build] default_image` value when the build holds
  it, or the application that the build names. `config_hash` and the other
  fields describe the build of a query without `variant` and `image`, and
  `smart_search` and `semantic_search` answer for that build. A project
  with several variants and no `default_variant` has no such build: then
  the two tools answer for the newest build
- `default_build_error` — only when no build answers a query without
  `image`, for example while the bootloader is indexed and the application
  is not. The other fields then describe the newest build. Name `image`
  until the application is indexed

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

This tool is read-only, with one exception: the config load creates
default `.fw-context/config.toml` and `local.toml` when they are missing.

Each `builds` entry also holds `entry_point` and `memory`, in the same
shape as in `get_active_build`. This is the memory map of every build;
`get_active_build` gives it for one build only.

#### `reindex_file`

Re-index a single file after editing.

```
Input:  {"file_path": "/abs/path/to/src/main.c", "project_root?": "/path/to/project"}
Output: {"file": "/abs/path/to/src/main.c", "translation_units": 1,
         "symbols_updated": 28, "elapsed_s": 2.5}
```

**Limitation:** A source file must appear in `compile_commands.json`.
compile_commands.json lists no headers, thus fw-context re-parses a header
through ONE translation unit that includes it, from the manifest: the unit
with the lowest path in sort order. The result then holds a `warning`
("Header re-indexed via one TU…"). Other units can include the header
with other `#define` values and still hold stale symbols. Run a full
`fw-context index` for full accuracy. A file that no unit builds or
includes gives an `error`.

#### `reindex_file_impl`

The shared implementation that `reindex_file` uses (the public tool, with
full analysis) and that the background auto-reindex uses (the fast path,
with no LLM). Prefer `reindex_file` for interactive use. Use this tool
only when you need to control `with_analysis` explicitly.

```
Input:  {"file_path": "/abs/path/to/src/main.c", "project_root?": "/path/to/project", "with_analysis?": true}
Output: {"file": "/abs/path/to/src/main.c", "translation_units": 1,
         "symbols_updated": 28, "elapsed_s": 2.5,
         "analysis_updated": 12, "embeddings_updated": 12}
```

Optional keys are present only when they apply. They are absent
otherwise, never `null`:

- `skipped_tus` and `skipped_count` — translation units that failed to
  parse.
- `warning` — a header went through one translation unit.
- `analysis_updated` and `embeddings_updated` — counts for the whole
  build, not only for this file.
- `analysis_warning`, `overrides_warning`, `pagerank_warning`,
  `embedding_warning` — a phase failed.

A file that is gone from disk but is still in the index has its records
removed. The result is then `{"file": …, "symbols_removed": 28,
"action": "deleted"}`. On failure, the dict holds `error`, and can also
hold `skipped_tus` or `action`. `reindex_file` gives the same shape.

When `with_analysis=True` (the default), this tool also regenerates the
LLM symbol analysis and the method override relationships. This is
slower, but produces a fully up-to-date index. Set `with_analysis=False`
for a fast, symbol-only update. This tool requires an existing index. A
source file must appear in `compile_commands.json`. A header goes through
one including translation unit, with the same `warning` as in
`reindex_file`.

#### `reset_index`

Delete the index. Always run a dry run first, then pass `confirm: true`.

```
Input:  {"project_root?": "/path/to/project", "confirm": false}     → {"action": "dry_run", "symbol_count": 8586, …}
Input:  {"project_root?": "/path/to/project", "confirm": true}      → {"action": "deleted", "message": "…"}
```

The delete removes the database with its `-wal`, `-shm`, and `-journal`
files. It also removes the automatic-build markers `build_problem.json` and
`excluded_sources.json` next to the database, because they describe the
index that is gone.

#### `list_projects`

List all indexed firmware projects.

```
Input:  {"project_root?": "/path/to/project"}
Output: [{"project_id": "a1b2…", "name": "my-zephyr-app", "root_path": "/path",
          "build_system": "zephyr", "symbol_count": 12430, "file_count": 1502,
          "indexed_at": "2026-06-05 09:35:18", "description": "branch: main",
          "first_indexed_at": "2026-06-01 11:02:44",
          "schema_version": 84935291, "current_schema": 84935291,
          "reindex_needed": false, "status": "ready", "db": "…",
          "variant_count": 2, "image_count": 5,
          "analysis": {"project": {"analyzed": 8450, "skipped": 6, "total": 8570},
                       "vendor": {"analyzed": 0, "skipped": 0, "total": 42330}}}, …]
```

`indexed_at` and `first_indexed_at` are UTC, in `"YYYY-MM-DD HH:MM:SS"`
format. `analysis` holds the `project` and `vendor` counts only, and is
`null` when the project has no indexed build. For the `model`,
`analyze_vendor`, and `complete` fields, call `get_active_build` for that
project.

`reindex_needed` is `true`, and `status` is `"reindex_needed"`, when one of
these is true:

- The schema of the database is older than the schema of this version.
- The `compile_commands.json` of a build changed after the index, or it is
  missing.
- A build of the project holds rows of an older row format.

The two build checks read the newest build of each `(variant, image)`, as
`get_active_build` does, because the builds of one project are not always
indexed together.

A newer row format does not set it. In that case, this process is the old
reader, and `get_active_build` asks for a restart of the LLM client.

When no project has an index, the result is one entry with an `info` key.
When fw-context cannot read a database, the result holds one entry with `db`
and `error` keys for that file.

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
         "installed_models": ["qwen2.5-coder:14b", "mxbai-embed-large:latest"],
         "chat_api": {"configured": false, "model": "qwen2.5-coder:14b"}, …}
```

Returns `status: "disabled"` when `[llm] enabled = false`. In that case,
this tool needs no Ollama.

`chat_api` says whether an external chat API replaces Ollama for chat,
and which model it names. When it is configured, `chat_api` also holds
`endpoint` and `format`, and a `compliance_warning` for a host that is not
local.

`embedding_backend` is `"ollama"` for an Ollama embedding model, and
`"local"` for a sentence-transformers or `ft://` model. A local model needs
no Ollama: it counts as installed, and with chat on an external API the
status is `"ok"` even when Ollama does not run. `"embedding_unavailable"`
thus means an Ollama embedding model and a stopped Ollama.

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

Every parameter you omit keeps its current value. `auto_pull` and
`chat_api_format` are written only when you pass them and they differ from
the current value; `chat_api_format: "auto"` returns to the detection from
the URL. A call that changes nothing returns `status: "error"` and writes
nothing.

When the test call fails, this tool restores the previous
`.fw-context/local.toml` (or removes the file if it did not exist). The
result then holds `status: "error"`, and the message says that nothing
was changed.

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
Phase 4: Vector search (sqlite-vec)
┌────────────────────────────────────────────────────┐
│ Separate KNN query with the query embedding        │
│ (embed_model), independent of the FTS5 results.    │
│ Cosine similarity threshold 0.5.                   │
└────────────────────────────────────────────────────┘
                    │
                    ▼
Phase 5: Adaptive fusion
┌────────────────────────────────────────────────────┐
│ Prefer the vector results.  When they are fewer    │
│ than [index] min_dense_count, use the FTS5 results │
│ instead.  No rank fusion of the two lists.         │
└────────────────────────────────────────────────────┘
                    │
                    ▼
Phase 6: Deduplicate
┌────────────────────────────────────────────────────┐
│ Remove duplicates.  Prefer definitions.            │
└────────────────────────────────────────────────────┘
                    │
                    ▼
Phase 7: Expand context (call graph)
┌────────────────────────────────────────────────────┐
│ Walk callers and callees of the top 10 results.    │
│ Insert up to 5 project definitions at positions    │
│ 11-15.                                             │
└────────────────────────────────────────────────────┘
                    │
                    ▼
Phase 8: Format
┌────────────────────────────────────────────────────┐
│ Limit to N.  Add metadata entries                  │
│ (_generated_queries, …).                           │
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

The query tools check each file that their result names against the
index. The content decides: fw-context compares the hash of the file on
disk with `files.source_hash`. A `git checkout`, `pull`, or `stash pop`
that only changes the `mtime` thus gives no warning. Only for a row with
no stored hash does the `mtime` decide.

These tools do the check: `search_code`, `search_bodies`,
`search_content`, `lookup_symbol`, `find_variables`, `get_file_map`, the
call-graph tools, and the class-analysis tools. The result shape decides
where the warning goes:

- A list gets a leading `{"warning": …}` record.
- A dict gets `stale_warning` and `stale: true`.

An empty result names no file, thus it gets its own message. It says that
indexed files changed after the last index run, thus the empty result is
not proof that the code does not exist. When source files are not in
`compile_commands.json`, the message says so instead, and names
`fw-context index --build`.

`get_source` and `get_symbol_context` add `source_origin`: `"index"` or
`"disk"`. When the file changed and the symbol did not move, the body
comes from the disk, with every `#ifdef` branch. When an edit moved the
symbol, the tool gives the indexed body with `stale: true`, because the
stored line number no longer points at the symbol. `explain_symbol` and
`read_file` also set `stale` and `stale_warning` when the file changed.

The search tools start the watcher daemon in the background when they find
a changed file, so no query has to wait for a reindex. The file watcher
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
   Read the preprocessor skipped ranges (clang_getAllSkippedRanges,
   indexer/skipped_ranges.py) → blank each inactive #if line in
   files.content and symbols.source; line numbers do not move;
   comments and preprocessor directives stay
   Release the write lock between translation units. A manual operation,
   such as `reindex_file`, can interleave through the pause marker
   mechanism, without blocking.

3. Write to SQLite (atomic per-TU transaction):
   Delete old symbols for this TU
   Insert new symbols + FTS5 triggers
   Insert references (on by default; skip with --no-refs)
   Insert macros with expanded values
   Generate + store vector embeddings (on by default; skip with --no-embeddings)

4. Linker scripts (after the C units, before the assembly; indexer/linker_script.py):
   Read the linker scripts that the build names
   Store symbol definitions (NAME = expr; PROVIDE), ENTRY, and MEMORY
   A symbol that a compiled file defines keeps that definition

5. Assembly units (.S, after every other unit; indexer/asm.py):
   Preprocess with clang -E -C (#if resolves, C macros expand, comments stay)
   Expand assembler macros (.macro, .rept, .irp)
   Store labels, .global/.weak symbols, .equ/.set aliases
   Link each vector-table slot to its handler (ref_kind "vector")

6. Write build metadata:
   compile_commands.json hash, file mtimes, symbol/file/ref/macro counts
   Stamp the row format at the end of the run (get_active_build compares it)
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

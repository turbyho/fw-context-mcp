# CLAUDE.md — fw-context-mcp

Project instructions for all AI tooling. Dynamic project memory belongs in
`MEMORY.md`.

## Agent language

- Expect input primarily in Czech, alternatively English
- Respond in Czech; common technical terms and identifiers may stay in English
- Write documentation, commit messages, tool descriptions, and release notes in
  English
- Write plans in Czech unless specified otherwise
- Always use UTF-8 without BOM encoding for Czech text

## Project identification

- Python MCP server — build-aware code intelligence for embedded C/C++
  firmware
- Targets Mbed OS, Zephyr, and PlatformIO projects via
  `compile_commands.json` + libclang
- 31 MCP tools: symbol lookup, full-text search, call-graph traversal, vector
  search, source code retrieval
- Serves AI assistants (Claude Code, Cursor, OpenCode) as an MCP server
- Python 3.11+, distributed as a PyPI package + GitHub release
- SQLite + FTS5 for indexing, libclang for parsing, optional Ollama for
  semantic search

## Workflow rules (MANDATORY)

These rules apply to the main session and all subagents.

### 1. Plan

A `plans/` plan is required for non-trivial changes:
- New features (new MCP tool, new search phase, new indexer capability)
- Refactoring that touches multiple modules
- Schema changes in the SQLite index
- API changes that affect the public MCP tool interface

A plan must contain:
- Change goal
- Analysis with specific file and line references
- List of changes without implementation
- Risks and verification method
- Status `Návrh — čeká na schválení`

Trivial changes (typos, single-line fixes, docs-only) and changes to
`.claude/` configuration do not require a plan.

### 2. Approval

- New features, schema changes, and public API changes require explicit user
  approval before implementation
- Bug fixes, tests, docs, and AI tooling configuration are fine with a normal
  direct user instruction unless destructive

### 3. No speculation

- Only state what follows from the code or available artifacts
- When making claims about behavior, reference specific `path:line`
- If you're unsure, verify in the repo — don't fill in assumptions
- MCP tools are documented in `docs/tools.md` and in their function docstrings;
  when in doubt about tool behavior, read the source

### 4. Preserve structure

Preserve the existing namespace layout:

```
src/fw_context_mcp/
  cli.py              — CLI entrypoint (argparse)
  utils.py            — shared helpers (resolve_project_root, abs_path)
  config/             — configuration loading (TOML, Settings dataclass)
  indexer/            — libclang parsing, SQLite index, build detection
  llm/                — Ollama client
  mcp/                — FastMCP server, tool handlers
  search/             — search pipeline (phases/, pipeline.py, scoring.py)
```

Don't introduce new top-level modules without a plan.

### 5. Git and commit conventions

- Commit messages are in English
- Conventional Commits prefix: `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`,
  `test:`, `perf:`, `ci:`, `build:`
- **UTF-8 without BOM** — no byte order mark at the start of files
- **LF** (`\n`) — Unix line endings, never CRLF
- **Unicode normalization NFC** — normalize Unicode characters to NFC form

Rules for files:
- Create new files always in UTF-8 without BOM with LF line endings
- Before committing, verify NFC normalization (`iconv -f UTF-8 -t UTF-8`
  preserves NFC)
- Convert existing files with CRLF or BOM to LF / UTF-8 without BOM

### 6. Release

Release workflow is documented in `.claude/skills/release.md`. Key rules:
- English only — no Czech in commits, tags, or release notes
- No emoji, no AI attribution footer
- Both remotes always: `origin` (git.montyho.com) and `github`
- Python explicitly via `~/.pyenv/versions/3.11.8/bin/python3` for build/twine

## Build and test commands

### Environment

- Python 3.11 via pyenv (`~/.pyenv/versions/3.11.8/bin/python3`)
- Package manager: `uv` (preferred) or `pip`
- Lint: `ruff check src/ tests/`
- Type check: `mypy src/`
- Format: `ruff format src/ tests/` (line length 120)

### Install (development)

```bash
# Create venv with pip (needed by IDE tooling):
uv venv --seed
# Install project in editable mode:
uv pip install -e ".[dev]"
```

### Test

```bash
# All tests
python3 -m pytest tests/ -x -q

# Single test file
python3 -m pytest tests/test_db.py -x -q

# Single test
python3 -m pytest tests/test_db.py::test_<name> -x -q

# With verbose output
python3 -m pytest tests/ -v

# Skip slow tests (quality tests over real projects)
python3 -m pytest tests/ -x -q -k "not comprehensive"
```

### Lint and type check

```bash
ruff check src/ tests/
mypy src/
```

### Run the MCP server locally

```bash
fw-context-mcp
# or directly:
python3 -m fw_context_mcp.mcp.server
```

### Run the CLI

```bash
fw-context index          # index current project
fw-context index --build  # auto-detect build system and index
fw-context index --refs   # index with call-graph references
```

## Architecture

### Entry point

- `src/fw_context_mcp/cli.py` — `main()` dispatches subcommands (`index`,
  `init`). `cmd_index()` orchestrates build detection → compile_commands →
  libclang parse → SQLite write.
- `src/fw_context_mcp/mcp/server.py` — `main()` starts the FastMCP stdio
  server. All 31 tools are registered as decorated async functions.

### Index pipeline

```
compile_commands.json
  → indexer/compile_commands.py  (parse JSON, resolve paths)
  → indexer/symbols.py           (libclang parse — AST → Symbol dataclass)
  → indexer/ops.py               (upsert into SQLite + FTS5)
  → indexer/db.py                (schema, queries, migrations)
```

- `indexer/build.py` — `BuildConfig` dataclass and build system auto-detection
  (Mbed OS, Zephyr, PlatformIO)
- `indexer/runner.py` — top-level orchestration of a full index run
- `indexer/config_hash.py` — content-addressable config fingerprint for
  staleness detection

### Search pipeline

`search/pipeline.py` runs a multi-phase pipeline. Each phase in
`search/phases/` is called in sequence:

1. `translate.py` — non-English → English
2. `rough_search.py` — gather sample symbols
3. `llm_query.py` — Ollama generates FTS5 terms
4. `fts5_search.py` — execute FTS5 query
5. `refine.py` — Ollama checks results, course-corrects
6. `embedding.py` — vector similarity re-rank
7. `deduplicate.py` — remove duplicates
8. `format.py` — format results

For simple `search_code` (no Ollama), only `fts5_search.py` runs.

### Configuration

Hierarchy (later overrides earlier):
1. Built-in defaults
2. `~/.fw-context/config.toml` (global, per-user)
3. `<project>/.fw-context/config.toml` (project, shared — commit to git)
4. `<project>/.fw-context/local.toml` (project, local — gitignore)

Loaded by `config/settings.py` into a `Config` dataclass.

### Database

- SQLite with FTS5 for full-text search
- `sqlite-vec` extension for vector embeddings (cosine similarity)
- Schema lives in `indexer/db.py` — `init_db()`, `ensure_schema()`
- Reference index (`refs` table) is built when `--refs` is passed (enabled by
  default)

## Coding conventions

### Python

- Python 3.11+ with `from __future__ import annotations`
- Ruff for linting: E, F, W, I (isort), B (bugbear), UP (pyupgrade)
- Line length 120, target version py311
- mypy with `ignore_missing_imports = true`, `warn_unused_ignores = true`,
  `warn_redundant_casts = true`
- First-party import: `fw_context_mcp`
- Dataclasses over plain dicts for structured data
- Pathlib over os.path / string paths
- Type hints on public functions, `Annotated` for MCP tool parameters

### Project structure

- `from __future__ import annotations` in every file
- Imports sorted: stdlib → third-party → first-party (`fw_context_mcp`)
- Lazy imports for heavy modules (libclang, ollama) — import inside the
  function that needs them
- `Path` objects for all filesystem paths, `str` only at I/O boundaries
- Context managers for resource lifecycle (DB connections, file handles)

### Tool definitions (MCP)

- Each tool is an async function decorated with `@mcp.tool()`
- Parameters use `Annotated[type, Field(description=...)]` or plain types with
  docstring `:param name: description:`
- Docstring first line is the short description shown in tool listings
- Read-only tools must state "Read-only. No side effects." in docstring
- Mutable tools must state the side effect clearly

### Error handling

- `DatabaseCorruptionError` — the caller should tell user to `reset_index()`
  then re-index
- `OllamaError` / `OllamaModelNotFoundError` — graceful fallback to FTS5-only
  search
- CLI exits with `sys.exit(1)` on user-facing errors, prints to stderr
- MCP server returns structured error responses, never crashes

### Testing

- pytest with fixtures in `tests/conftest.py`
- Test database: use `:memory:` SQLite or temporary directories
- Test comprehensiveness: `comprehensive_quality_check.py` runs against real
  indexed projects

## Safe and dangerous actions

### Safe without asking

- Read-only work in `src/`, `tests/`, `docs/`, `.claude/`
- `ruff check`, `mypy`, `pytest`
- `git status`, `git diff`, `git log`
- Creating files in `plans/`

### Requires confirmation before writing or executing

- Changes to `indexer/db.py` (schema changes — may break existing indexes)
- Changes to public MCP tool signatures
- Changes to `pyproject.toml` dependencies
- Destructive git operations (`commit --amend`, `rebase`, `reset --hard`)
- `git push`
- `twine upload` (PyPI publish)
- `reset_index(confirm=True)` — destructive to user's index

### Sudo

Don't run `sudo`. When needed, print the command for the user and stop.

## MEMORY.md — Persistent Project Memory

`MEMORY.md` lives in the project root. It holds dynamically discovered
knowledge of lasting value — decisions, patterns, gotchas, and user feedback.
`CLAUDE.md` holds static instructions; `MEMORY.md` holds things you learn while
working.

### Decision heuristic

When unsure whether to store, ask: *would a fresh Claude instance without this
conversation benefit from knowing this?*

### When to store

#### Explicit triggers (EN)

The user says any variation of "store / remember / save / note / keep":
- `remember this`, `remember that`, `remember for next time`
- `for future reference`, `for next time`, `keep this in mind`
- `save this`, `store this`, `make a note`, `take a note`
- `add to memory`, `don't forget`, `keep in mind for later`
- `note this down`, `write this down`

#### Explicit triggers (CZ)

The user says any variation of "ulož / zapamatuj / pamatuj si":
- `zapamatuj si`, `zapamatuj si to`, `pamatuj si`, `ulož si to`, `ulož do paměti`
- `pro příště`, `příště si vzpomeň`, `ať víš do budoucna`
- `tohle si pamatuj`, `chci aby sis to pamatoval`, `přidej do paměti`
- `zaznamenej si`, `poznamenej si`, `zapiš si`

#### Implicit situations

Even without an explicit trigger, store information that will be useful in
future sessions:

| Situation | What to store |
|-----------|---------------|
| Architectural decision | What was chosen, why, what alternatives were rejected |
| Non-trivial bug fix | Cause, symptom, why it wasn't obvious |
| User feedback on working style | What the user prefers, what they don't want |
| Pattern / convention not in docs | Where and how it is used |
| Configuration hard to discover | What, where, why |
| Gotcha / unexpected behavior | Trigger, effect, fix |
| Important discovery in code | What, where (`path:line`), consequence |

### When NOT to store

- Routine operations (build passed, test ran)
- Trivial facts (file exists, function is named X)
- Every single conversation message
- Things already in `CLAUDE.md` or git history
- Transient work-in-progress state ("I'm working on X right now")

### Where to store

- Project memory: `$PROJECT_ROOT/MEMORY.md` (index) + files in `memory/`
- Format: each entry as a separate `.md` file in the `memory/` directory
- Frontmatter: `name` (kebab-case slug), `description` (one-line summary),
  `metadata` (type: `user` / `feedback` / `project` / `reference`)
- `MEMORY.md` contains only a one-line index: `- [Title](file.md) — hook`
- Link related entries via `[[name]]` references

### Updating existing entries

- Before saving, check if an existing file already covers the fact — update
  that file rather than creating a duplicate
- Delete memories that turn out to be wrong
- When a decision evolves, edit the original entry and note the change

## Response style

- Concise and technical.
- Use `path/file.py:line_number` references when appropriate.
- Communicate with the user in Czech. Technical terms, command names, package
  names, and file names stay in original.

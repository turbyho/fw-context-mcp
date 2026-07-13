# AGENTS.md — fw-context-mcp

OpenCode-specific instructions. See `CLAUDE.md` for the full project reference.

## Quick start

```bash
uv venv --seed && uv pip install -e ".[dev]"
```

## Dev commands

| Purpose | Command |
|---------|---------|
| Lint | `ruff check src/ tests/` |
| Type check | `mypy src/` |
| Format | `ruff format src/ tests/` |
| Run all tests | `python3 -m pytest tests/ -x -q` |
| Single test | `python3 -m pytest tests/test_db.py::test_<name> -x -q` |
| Skip slow tests | `python3 -m pytest tests/ -x -q -k "not comprehensive"` |
| Run server locally | `python3 -m fw_context_mcp.mcp.server` |

## Architecture

```
src/fw_context_mcp/
  cli.py              — CLI entrypoint (fw-context index, init)
  mcp/server.py       — FastMCP stdio server, 32 tools
  indexer/            — libclang parse → SQLite + FTS5 + vec
  search/             — multi-phase search pipeline (phases/, pipeline.py, scoring.py)
  config/             — TOML config loading (settings.py)
  llm/                — Ollama client
```

## Hard constraints

- **Plan required** for: new tools, schema changes (`indexer/db.py`), multi-module refactors, public API changes. Write to `plans/`. Status: `Návrh — čeká na schválení`.
- **Approval required** for: schema changes, public tool signatures, new dependencies in `pyproject.toml`.
- **Never** introduce new top-level modules without a plan.
- **Schema changes** in `indexer/db.py` break existing indexes — handle with care.

## Language

- Respond to user in **Czech** (technical terms stay English)
- Commits, docs, tool descriptions, release notes in **English**
- Write plans in Czech unless specified otherwise

## Python conventions

- `from __future__ import annotations` in **every** file
- Imports sorted: stdlib → third-party → first-party (`fw_context_mcp`)
- **Lazy imports** for heavy deps (libclang, ollama) — import inside the function that uses them
- `pathlib.Path` for filesystem paths, `str` only at I/O boundaries
- Dataclasses over dicts; type hints on public functions; `Annotated` for MCP tool params
- Line length 120, target py311

## Testing

- Markers: `slow`, `ollama`, `libclang` — auto-skipped when deps missing
- Results written to `tests/results/` (JSON summary per run)
- Test DB uses `:memory:` SQLite or temp directories

## File rules

- **UTF-8 without BOM**, **LF** line endings, **NFC** Unicode normalization
- Conventional Commits: `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`, `perf:`, `ci:`, `build:`

## Release

- Use the `release` OpenCode skill (`.opencode/skills/release/SKILL.md`)
- Python explicitly via `~/.pyenv/versions/3.11.8/bin/python3` for build/twine
- Push to both remotes: `origin` (git.montyho.com) and `github`
- No emoji, no AI attribution footer

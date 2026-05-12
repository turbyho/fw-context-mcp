# fw-context-mcp

Build-aware code intelligence MCP server for embedded firmware projects (Mbed OS, Zephyr, PlatformIO).

Indexes symbols from `compile_commands.json` using libclang and exposes them as MCP tools. AI assistants can look up function definitions, search symbols, and detect the build system — without grep, without reading entire files.

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- [`bear`](https://github.com/rizsotto/Bear) — to generate `compile_commands.json` from a build
- ARM GCC toolchain (for Mbed OS / Zephyr projects) — libclang needs the system includes

## Installation

```bash
# Create a dedicated venv
uv venv ~/.fw-context/.venv --python 3.12

# Install from source
uv pip install --python ~/.fw-context/.venv/bin/python /path/to/fw-context-mcp/

# Add to PATH (add to ~/.zshrc or ~/.bashrc)
export PATH="$HOME/.fw-context/.venv/bin:$PATH"
```

After a source update:

```bash
uv pip install --python ~/.fw-context/.venv/bin/python /path/to/fw-context-mcp/
```

## Setup — AI assistant integration

Run once after installation to register the MCP server and install usage instructions
into Claude Code and OpenCode:

```bash
fw-context init
```

This command:
- Registers `fw-context-mcp` in Claude Code (`claude mcp add --scope user`)
- Inserts a usage instructions block into `~/.claude/CLAUDE.md`
- Writes `~/.config/opencode/rules/fw-context.md`

The command is idempotent — safe to re-run after updates.

## Generating compile_commands.json

For **Mbed OS** projects:

```bash
bear -- python3 build_app.py --profile release --type DEV
```

For **Zephyr** projects:

```bash
west build -b <board> -- -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
# compile_commands.json is in build/
```

For **PlatformIO**:

```bash
pio run --target compiledb
```

## Indexing a project

Run from the project root (where `compile_commands.json` is located):

```bash
fw-context index
```

The index is stored in `~/.fw-context/index/<project-id>/index.db`. Re-running is incremental — only files with changed `mtime` are re-parsed.

Options:

```
fw-context index [compile_commands.json] [--project DIR] [--name NAME]
```

Search the index from the CLI:

```bash
fw-context search "modem_init"
fw-context search "set_key" --limit 5
```

## Project configuration

Add `.fw-context/config.toml` to the project root:

```toml
[project]
name = "my-firmware"

[index]
compile_commands = "compile_commands.json"
source_roots = ["src", "lib"]
exclude_paths = ["lib/zcbor/generation"]  # exclude generated code
```

Global defaults live in `~/.fw-context/config.toml` (auto-created on first run).

## MCP server

The MCP server exposes three tools:

### `get_active_build`

Returns metadata about the indexed build:

```json
{
  "config_hash": "d8448ca5",
  "build_system": "mbed-os",
  "symbol_count": 8586,
  "file_count": 952,
  "indexed_at": "2026-05-12 08:19:59",
  "stale": false
}
```

`build_system` is auto-detected: `mbed-os`, `zephyr`, `platformio`, or `unknown`.  
`stale` is `true` when `compile_commands.json` is newer than the index.

### `search_code`

Full-text search over indexed symbols:

```
search_code(query, project_root?, kind?, limit?)
```

Supports FTS5 syntax: prefix `init*`, phrase `"spi init"`.  
`kind` filter: `function`, `method`, `class`, `struct`, `enum`, `typedef`, `variable`, `field`, `enum_constant`, `namespace`.

### `lookup_symbol`

Look up a symbol by name (exact or prefix):

```
lookup_symbol(name, project_root?, exact?)
```

Returns declarations and definitions with `file`, `line`, `signature`, `is_definition`.

## Integrating with AI assistants

### Claude Code — global (all projects)

```bash
claude mcp add --scope user fw-context ~/.fw-context/.venv/bin/fw-context-mcp
```

### Claude Code — per project

```bash
claude mcp add --scope project fw-context ~/.fw-context/.venv/bin/fw-context-mcp
# creates .mcp.json in project root
```

### OpenCode — global

Add to `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "fw-context": {
      "command": ["/home/<user>/.fw-context/.venv/bin/fw-context-mcp"],
      "enabled": true,
      "type": "local"
    }
  }
}
```

## Re-indexing workflow

The index needs to be rebuilt after:

- **Source file changes** — `fw-context index` (incremental, only changed files, fast)
- **New files / changed build flags** — `compile_commands.json` changes → `fw-context index` (incremental)
- **Toolchain or compiler changes** — delete the DB and re-index from scratch:

```bash
rm ~/.fw-context/index/<project-id>/index.db
fw-context index
```

The `stale` field in `get_active_build` signals when `compile_commands.json` has changed since the last index.

## Directory layout

```
~/.fw-context/
├── config.toml          # global defaults
├── .venv/               # tool installation
│   └── bin/
│       ├── fw-context       # CLI
│       └── fw-context-mcp   # MCP server (stdio)
└── index/
    └── <project-id>/
        └── index.db     # SQLite + FTS5 symbol index
```

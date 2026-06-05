# fw-context-mcp — MCP Server

MCP (Model Context Protocol) server that exposes the fw-context symbol index
as tools for AI assistants. Communicates over **JSON-RPC 2.0 via stdio**.

## What it is

A persistent subprocess started by your AI assistant (Claude Code, OpenCode, …)
that provides **18 tools** for navigating embedded firmware codebases:

| Category | Tools |
|----------|-------|
| **Search & lookup** | `search_code`, `lookup_symbol`, `smart_search` |
| **Understanding** | `get_source`, `explain_symbol`, `get_symbol_context` |
| **Call graph** | `find_callers`, `find_references`, `find_call_path`, `find_all_callers_recursive`, `find_callees_recursive`, `find_dead_code`, `find_hotspots` |
| **Maintenance** | `get_active_build`, `reindex_file`, `reset_index`, `list_projects` |
| **Setup** | `check_ollama` |

Full tool reference with schemas and examples: **[Tools Reference](docs/tools.md)**

## Protocol

JSON-RPC 2.0 over stdin/stdout. One JSON object per line, terminated by `\n`.

### Initialization

```
Client → Server:
  {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05",…}}

Server → Client:
  {"jsonrpc":"2.0","id":1,"result":{…}}

Client → Server:
  {"jsonrpc":"2.0","method":"notifications/initialized"}
```

### Calling a tool

```
Client → Server:
  {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search_code","arguments":{"query":"uart init"}}}

Server → Client:
  {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"<json>"}],"isError":false}}
```

The `text` field is a JSON-encoded string — parse it to get the return value.

### Listing tools

```
Client → Server:  {"jsonrpc":"2.0","id":99,"method":"tools/list","params":{}}
Server → Client:  {"jsonrpc":"2.0","id":99,"result":{"tools":[{…},…]}}
```

## Integration

### Claude Code

```bash
claude mcp add --scope user fw-context ~/.fw-context/.venv/bin/fw-context-mcp
```

Run `fw-context init` to do this automatically and inject usage instructions
into `~/.claude/CLAUDE.md`.

### OpenCode

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

### Other MCP clients

Start the server as a subprocess:

```bash
/path/to/.fw-context/.venv/bin/fw-context-mcp
```

Send JSON-RPC on stdin, read responses from stdout. The server has no CLI
interface — it only speaks MCP.

## Error handling

**JSON-RPC errors** (protocol level):
```json
{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"Invalid request parameters"}}
```

**Tool-level errors** (tool ran but couldn't complete):
```json
{"result":{"content":[{"type":"text","text":"{\"error\": \"No index found for …\"}"}],"isError":false}}
```

Tool errors use `isError: false` — the tool call succeeded, but returned an
error field. Only JSON-RPC protocol errors use the top-level `error` key.

### Staleness and auto-reindex

`search_code` and `lookup_symbol` automatically detect stale files in results,
re-index them (up to 5 files, 30 s timeout), and re-run the query. When
auto-reindex fails or `compile_commands.json` itself is stale, a warning
entry is included:

```json
[
  {"warning": "Auto-reindex failed for 1 file(s): …"},
  {"name": "spi_init", …}
]
```

### Database corruption

The server runs `PRAGMA integrity_check` on every database open. If corruption
is detected, tools return a structured error:

```json
{
  "error": "Database corruption detected: …",
  "action": "reset_index",
  "hint": "Run reset_index() then fw-context index to rebuild."
}
```

Call `reset_index(confirm=true)` to delete the database, then `fw-context index`
to rebuild.

## Environment

- **CWD:** The server resolves project root from `$PWD` when `project_root`
  is not provided. Launch it from the firmware project root.
- **No network required** for core tools (search, lookup, source, graph).
  `smart_search` and `explain_symbol` optionally use local Ollama.
- **Stateless** between calls — each tool opens the DB, queries, closes.

## Debugging

```bash
# Manual tool listing
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | /path/to/.fw-context/.venv/bin/fw-context-mcp

# Verbose logging
FW_CONTEXT_LOG_LEVEL=DEBUG /path/to/.fw-context/.venv/bin/fw-context-mcp

# Ollama debug log (when debug_log is set in config)
tail -f ~/.fw-context/llm-debug.jsonl
```

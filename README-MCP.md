# fw-context-mcp — MCP Server

fw-context-mcp is an MCP (Model Context Protocol) server. fw-context-mcp exposes the fw-context symbol index as tools for AI assistants. fw-context-mcp communicates over **JSON-RPC 2.0**, through stdio.

## What it is

Your AI assistant (for example, Claude Code or OpenCode) starts fw-context-mcp as a persistent subprocess. fw-context-mcp provides **34 tools** to navigate embedded firmware codebases:

| Category | Tools |
|----------|-------|
| **Search & lookup** | `search_code`, `lookup_symbol`, `smart_search`, `semantic_search`, `search_bodies`, `search_content`, `find_variables` |
| **Understanding** | `get_file_map`, `get_source`, `explain_symbol`, `get_symbol_context`, `read_file` |
| **Call graph** | `find_callers`, `find_references`, `find_call_path`, `find_all_callers_recursive`, `find_callees_recursive`, `find_dead_code`, `find_hotspots`, `find_wrapper_callers`, `find_indirect_call_sites`, `find_indirect_targets`, `trace_data_flow` |
| **Class analysis** | `get_inheritance_chain`, `get_class_members`, `get_template_instances`, `get_method_overrides` |
| **Index maintenance** | `get_active_build`, `reindex_file`, `reindex_file_impl`, `reset_index`, `list_projects`, `check_ollama` |

For the full tool reference, with schemas and examples, see **[Tools Reference](docs/tools.md)**.

## Protocol

fw-context-mcp uses JSON-RPC 2.0 over stdin and stdout. Each line contains one JSON object. A newline character (`\n`) ends each line.

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

The `text` field is a JSON-encoded string. Parse the string to get the return value.

### Listing tools

```
Client → Server:  {"jsonrpc":"2.0","id":99,"method":"tools/list","params":{}}
Server → Client:  {"jsonrpc":"2.0","id":99,"result":{"tools":[{…},…]}}
```

## Integration

### Claude Code

```bash
# pip install (binary in ~/.local/bin or venv bin/)
claude mcp add --scope user fw-context fw-context-mcp

# source install
claude mcp add --scope user fw-context ~/.fw-context/.venv/bin/fw-context-mcp
```

Run `fw-context init` to do this automatically. This command also adds usage instructions to `~/.claude/CLAUDE.md`.

### OpenCode

Add to `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "fw-context": {
      "command": ["fw-context-mcp"],
      "enabled": true,
      "type": "local"
    }
  }
}
```
If `fw-context-mcp` is not on your PATH, use the full path:
`"command": ["/home/<user>/.local/bin/fw-context-mcp"]` (pip) or
`"command": ["/home/<user>/.fw-context/.venv/bin/fw-context-mcp"]` (source).

### Other MCP clients

Start the server as a subprocess:

```bash
fw-context-mcp
```

If the binary is not on your PATH, use the full path. For pip installs
the binary is typically at `~/.local/bin/fw-context-mcp`. For source
installs it is at `~/.fw-context/.venv/bin/fw-context-mcp`. Run
`which fw-context-mcp` to find the exact path on your system.

## Error handling

**JSON-RPC errors** (protocol level):
```json
{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"Invalid request parameters"}}
```

**Tool-level errors** (the tool ran, but did not complete):
```json
{"result":{"content":[{"type":"text","text":"{\"error\": \"No index found for …\"}"}],"isError":false}}
```

Tool errors use `isError: false`. The tool call succeeded, but the result contains an error field. Only JSON-RPC protocol errors use the top-level `error` key.

### Staleness and auto-reindex

`search_code` and `lookup_symbol` detect stale files in the results automatically. These tools re-index up to 5 stale files, with a 30-second timeout. Then these tools run the query again. When auto-reindex fails, or when `compile_commands.json` itself is stale, the result includes a warning entry:

```json
[
  {"warning": "Auto-reindex failed for 1 file(s): …"},
  {"name": "spi_init", …}
]
```

#### Ghost-record purge after file deletion

When a source file is deleted from disk (e.g. after a git branch switch),
the next `fw-context index` run automatically removes its symbols, references,
and edges from the index. The query-time staleness check now detects deleted
files and triggers a background reindex, which in turn runs the purge.

A safety guard prevents accidental mass deletion: the automatic purge aborts
when more than 20 percent of indexed files are missing from disk. This guards
against offline network mounts or a wrong project root.

Configure the threshold in `.fw-context/config.toml`:

```toml
[index]
purge_max_missing_percent = 20   # default; set to 100 to disable the guard
```

The log contains a warning when the guard blocks a purge:

```
Ghost-file purge aborted: 150/200 files (75.0%) missing from disk (threshold 20%).
Possible offline mount or wrong project root.
```

### Database corruption

The server runs `PRAGMA integrity_check` on every database open. If the check finds corruption, the tools return a structured error:

```json
{
  "error": "Database corruption detected: …",
  "action": "reset_index",
  "hint": "Run reset_index() then fw-context index to rebuild."
}
```

Call `reset_index(confirm=true)` to delete the database. Then run `fw-context index` to rebuild the database.

## Environment

- **CWD:** The server resolves the project root from `$PWD` when the request does not provide `project_root`. Launch the server from the firmware project root.
- **The core tools need no network connection** (search, lookup, source, graph). `smart_search` and `explain_symbol` can optionally use a local Ollama server.
- **The server keeps no state between calls.** For each call, a tool opens the database, runs the query, and closes the database.

## Debugging

```bash
# Manual tool listing
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | fw-context-mcp

# Verbose logging
FW_CONTEXT_LOG_LEVEL=DEBUG fw-context-mcp

# Ollama debug log (when debug_log is set in config)
tail -f ~/.fw-context/llm-debug.jsonl
```

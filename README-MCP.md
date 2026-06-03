# fw-context-mcp — MCP Server

MCP (Model Context Protocol) server that exposes the fw-context symbol index as
tools for AI assistants. Communicates over **JSON-RPC 2.0 via STDIO**.

## What it is

A persistent subprocess started by your AI assistant (Claude Code, OpenCode, …)
that provides 9 tools for navigating embedded firmware codebases:

- **Look up** any C/C++ symbol definition with file + line + signature
- **Search** symbols by keyword or natural language
- **Explain** what a function does (local Ollama LLM, or falls back to returning code + prompt for the AI assistant)
- **Manage** the symbol index (check staleness, re-index files, reset)

## Protocol

The server speaks [JSON-RPC 2.0](https://www.jsonrpc.org/specification) over
stdin/stdout. Each message is one JSON object per line, terminated by `\n`.

### Initialization

```
Client → Server:
  {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"…"}}}

Server → Client:
  {"jsonrpc":"2.0","id":1,"result":{…}}

Client → Server:
  {"jsonrpc":"2.0","method":"notifications/initialized"}
```

### Calling a tool

```
Client → Server:
  {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"<tool>","arguments":{…}}}

Server → Client:
  {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"<json>"}],"isError":false}}
```

The result `text` field contains a JSON-encoded string — parse it to get the
actual return value.

### Listing tools

```
Client → Server:
  {"jsonrpc":"2.0","id":99,"method":"tools/list","params":{}}

Server → Client:
  {"jsonrpc":"2.0","id":99,"result":{"tools":[{…},…]}}
```

## Tools

### `get_active_build`

Check index health. Call at the start of every session.

```
Input:  {"project_root?": "/path/to/project"}
Output: {"config_hash": "…", "symbol_count": 8586, "stale": false, …}
```

### `search_code`

Full-text search with FTS5 syntax.

```
Input:  {"query": "modem init", "kind?": "method", "limit?": 20}
Output: [{"name": "modem_init", "qualified_name": "ns::modem_init", …}, …]
```

**FTS5 syntax:**
- `init*` — prefix wildcard (trailing only)
- `"spi init"` — exact phrase
- `modem init` — AND of both terms (underscore `_` is treated as word separator)

**Kind filter values:** `function`, `method`, `constructor`, `destructor`,
`class`, `struct`, `enum`, `enum_constant`, `typedef`, `variable`, `field`,
`namespace`.

### `lookup_symbol`

Look up by name. Default is prefix match; set `exact: true` for exact.

```
Input:  {"name": "BoxManager", "exact?": true}
Output: [{"name": "BoxManager", "kind": "class", "file": "…", "line": 21}, …]
```

### `smart_search`

Natural language → FTS5 keywords → search. Three-phase process:
(1) rough word-split search to gather real symbol names, (2) Ollama generates
refined FTS5 terms from those names, (3) OR search with client-side scoring.
Non-ASCII queries are auto-translated to English before the rough search phase.

```
Input:  {"query": "how does the modem connect?", "limit?": 20}
Output: [{"_generated_queries": ["modem connect*", …]}, {"_rough_queries": ["modem", "connect"]}, …symbols…]
```

Falls back to direct FTS5 word-split search when Ollama is unavailable. The
first entries are always `_generated_queries` and `_rough_queries`. When the
query was translated, `_translated_from` and `_translated_to` are also included.

### `explain_symbol`

Look up a symbol and explain what it does. **Takes 10–30 seconds with Ollama.**

**Name matching:** Exact match on short name (e.g. `ModemMsgManager`) or
qualified name (e.g. `zbox::ModemMsgManager`). Both are supported.

```
Input:  {"name": "modem_parser_oob_init", "context_lines?": 40}
Output: {"name": "…", "signature": "…", "explanation": "This function initializes…"}
```

When Ollama is unavailable or disabled (`enabled = false`), returns `source`
(source code snippet) and `explain_prompt` (the LLM prompt) instead of
`explanation`. The AI assistant should use its own LLM to answer the prompt.
Call `check_ollama` first.

### `check_ollama`

Verify Ollama connectivity before using `explain_symbol` or `smart_search`.

```
Input:  {}
Output: {"status": "ok", "ollama_running": true, "ollama_enabled": true, "configured_model": "qwen2.5-coder:14b", "num_ctx": 8192, "installed_models": […], …}
```

Returns `status: "disabled"` when `enabled = false` in config —
no Ollama needed, the AI assistant handles the results.

### `list_projects`

List all indexed projects.

```
Input:  {}
Output: [{"project_id": "…", "name": "zbox-ecb-fw", "symbol_count": 8586, "stale": false}, …]
```

### `reset_index`

Delete the index. **Always dry-run first**, then `confirm: true`.

```
Input:  {"confirm": false}          # dry-run
Output: {"action": "dry_run", "symbol_count": 8586, "message": "Would delete …"}

Input:  {"confirm": true}           # execute
Output: {"action": "deleted", "message": "Index deleted. Run 'fw-context index' to rebuild."}
```

### `reindex_file`

Re-index a single file after editing it.

```
Input:  {"file_path": "/abs/path/to/main.cpp"}
Output: {"file": "…", "translation_units": 1, "symbols_updated": 28, "elapsed_s": 2.5}
```

**Limitations:**
- File must appear in `compile_commands.json`
- Header files are re-indexed through the first matching translation unit only;
  other TUs including the same header may stay stale
- Use `fw-context index` (incremental) for full accuracy after editing headers

## Integration

### Claude Code

```bash
claude mcp add --scope user fw-context ~/.fw-context/.venv/bin/fw-context-mcp
```

Run `fw-context init` to do this automatically. The command also inserts
usage instructions into `~/.claude/CLAUDE.md`.

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

`fw-context init` writes the usage instructions to
`~/.config/opencode/rules/fw-context.md`.

### Custom / other assistants

Any MCP-compatible client can use the server. Start it as a subprocess:

```
/path/to/.fw-context/.venv/bin/fw-context-mcp
```

Send JSON-RPC messages on stdin, read responses from stdout. The server
has no CLI interface — it only speaks the MCP protocol.

## Error handling

Errors are returned in two forms:

**JSON-RPC errors** (protocol level — server not running, invalid message):
```json
{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"Invalid request parameters"}}
```

**Tool-level errors** (tool ran but couldn't complete — no index, file not found, …):
```json
{"result":{"content":[{"type":"text","text":"{\"error\": \"No index found for …\"}"}],"isError":false}}
```

Note: tool-level errors use `isError: false` (they are successful tool
invocations with an error field in the return value). Only JSON-RPC protocol
errors use the `error` key at the top level.

### Staleness warnings and auto-reindex

When the index is outdated, `search_code` and `lookup_symbol` **automatically
re-index** stale files found in the results (up to 5 files, 30 s timeout), then
re-run the query with fresh data. The caller gets clean results without stale
markers — no manual intervention needed.

When auto-reindex fails (e.g. for header-only files) or compile_commands.json
itself is stale, a `warning` entry is included:

```json
[
  {"warning": "Auto-reindex failed for 1 file(s): …"},
  {"name": "BoxManager", …}
]
```

### Database corruption

The server runs `PRAGMA integrity_check` on every database open. If corruption
is detected, tools return a structured error with a clear action hint:

```json
{
  "error": "Database corruption detected: …",
  "action": "reset_index",
  "hint": "Run reset_index() then fw-context index to rebuild."
}
```

Call `reset_index(confirm=True)` to delete the corrupt database, then run
`fw-context index` in the project root to rebuild from scratch.

## Environment

- **CWD:** The server resolves project root from the current working directory
  when `project_root` is not provided. Set the assistant to launch the server
  from the firmware project root.
- **No network required** for core tools (search, lookup, list).
  `explain_symbol` and `smart_search` support both local Ollama and a fallback
  mode where the AI assistant processes results itself — no LLM required on
  the server side.
- **Stateless** between calls — each tool call opens the database, runs the
  query, and closes. No persistent connections.

## Debugging

Start the server manually and pipe JSON-RPC messages:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | /path/to/.fw-context/.venv/bin/fw-context-mcp
```

Turn on verbose logging by setting the environment variable:

```bash
FW_CONTEXT_LOG_LEVEL=DEBUG /path/to/.fw-context/.venv/bin/fw-context-mcp
```

Ollama debug log (prompts + responses) is written to
`~/.fw-context/llm-debug.jsonl` when `debug_log` is set in config.

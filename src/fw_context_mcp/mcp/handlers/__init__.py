"""MCP tool handlers — one module per responsibility area.

Each module defines plain functions (no ``@mcp.tool()`` decorator).
Registration happens in ``server.py`` via ``mcp.tool()(handler_fn)``.

Tool function signatures carry Pydantic ``Annotated[str, Field(...)]``
type hints.  FastMCP introspects these at registration time to generate
the JSON Schema for each tool's input.  The ``Field(description=...)``
strings become the tool-description text that AI assistants see — they
must be AS precise and unambiguous as possible because assistants use
them to choose which tool to call and with which arguments.

Each handler module owns exactly one responsibility area:
- ``_lookup.py`` — symbol lookup by exact or prefix name
- ``_search.py`` — concept/keyword search (FTS5 over symbol names)
- ``_search_fallbacks.py`` — fallback strategies when primary search fails
- ``callgraph.py`` — callers, callees, call paths, indirect calls
- ``inheritance.py`` — C++ inheritance chains, class members, overrides
- ``source.py`` — function body reading, file reading, symbol context
- ``variables.py`` — global/local variable search with reference tracing
- ``_base.py`` — shared DB connection lifecycle and executor coordination
- ``maintenance.py`` — index health, reindex, LLM status
- ``search.py`` — high-level search orchestration (FTS5, embeddings, smart)
"""

__all__: list[str] = []

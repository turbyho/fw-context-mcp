"""MCP server package — FastMCP stdio server with 34 tools and 3 resources.

Serves build-aware code intelligence for embedded C/C++ firmware projects.
Tools are registered as async functions on a FastMCP instance; resources
expose project stats and symbol source as MCP resource URIs.

The MCP protocol uses JSON-RPC over stdin/stdout — no HTTP server, no
port, no network surface.  This is deliberate: embedded firmware tools
often run on developer laptops with NDAs and proprietary SDKs.  A local
stdio transport keeps code on the machine.

Package layout follows a strict architecture:

``mcp/`` — MCP server lifecycle and background services
``mcp/handlers/`` — one module per tool responsibility area (lookup,
  search, call graph, inheritance, variables).  Each defines plain
  functions — no ``@mcp.tool()`` decorator — so they are testable
  without FastMCP wiring.
``mcp/shared/`` — cross-cutting concerns: database connection pooling,
  stale detection, PID file coordination, brace matching for body
  extraction, readiness checks, and fallback search paths.

Registration happens in ``server.py``, the single integration point
between tools and FastMCP.  This separation means tool logic does not
depend on the MCP framework — it can be reused in CLI, test, or
headless contexts.
"""

"""MCP tool handlers — one module per responsibility area.

Each module defines plain functions (no ``@mcp.tool()`` decorator).
Registration happens in ``server.py`` via ``mcp.tool()(handler_fn)``.
"""

__all__: list[str] = []

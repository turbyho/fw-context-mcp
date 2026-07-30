"""Service layer for MCP handlers.

Each service class encapsulates database access logic for a specific
domain (search, symbols, callgraph, etc.).  Handlers delegate to these
services instead of writing raw SQL directly.

Usage::

    from fw_context_mcp.mcp.services import SearchService

    svc = SearchService(db_path)
    results = svc.lookup_symbol("uart_init")
"""

from __future__ import annotations

from pathlib import Path

from fw_context_mcp.mcp.shared.context import _open_db_safe


class _BaseService:
    """Shared DB connection management for all services."""

    def __init__(self, db_path: Path):
        self._db_path = db_path

    def _open(self):
        return _open_db_safe(self._db_path)


class SearchService(_BaseService):
    """Search operations — lookup_symbol, search_code, search_bodies."""

    def lookup_symbol(self, name: str, exact: bool = False, limit: int = 50):
        """Look up a symbol by name or prefix."""
        conn = self._open()
        try:
            from fw_context_mcp.indexer.db import lookup_symbol as _lookup
            return _lookup(conn, name, exact=exact, limit=limit)
        finally:
            conn.close()


class SymbolService(_BaseService):
    """Symbol detail operations — get_source, explain_symbol, get_symbol_context."""

    pass  # Implement as handlers are migrated


class CallGraphService(_BaseService):
    """Call graph operations — find_callers, find_call_path, etc."""

    pass


class MaintenanceService(_BaseService):
    """Maintenance operations — reindex_file, reset_index."""

    pass

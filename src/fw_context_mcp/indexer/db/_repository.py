"""Repository pattern for common index-db queries.

Provides ``SymbolRepository`` — a thin wrapper around a connection +
config_hash pair. Eliminates the boilerplate of passing config_hash to
every query and gives handlers a single call-site for data access.

Usage::

    repo = SymbolRepository(conn, config_hash)
    symbols = repo.search_fts5("uart_init")
"""

from __future__ import annotations

import sqlite3
from typing import Any


class SymbolRepository:
    """Thin wrapper around a sqlite3 connection + config_hash pair.

    Each method delegates to the canonical ``indexer.db`` function,
    injecting ``config_hash`` automatically so callers don't repeat it.
    """

    __slots__ = ("conn", "config_hash")

    def __init__(self, conn: sqlite3.Connection, config_hash: str) -> None:
        self.conn = conn
        self.config_hash = config_hash

    # ── symbols ────────────────────────────────────────────────────────

    def search_fts5(
        self,
        query: str,
        kind: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Full-text search over symbol names and signatures."""
        from ._symbols import search_symbols

        return search_symbols(self.conn, query, self.config_hash, kind=kind, limit=limit)

    def find_by_usr(self, usr: str) -> dict[str, Any] | None:
        """Look up a single symbol by its libclang USR."""
        row = self.conn.execute(
            "SELECT * FROM symbols WHERE usr = ? AND config_hash = ?",
            (usr, self.config_hash),
        ).fetchone()
        return dict(row) if row else None

    def find_by_name(
        self,
        name: str,
        exact: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Look up symbols by name — exact or prefix match."""
        if exact:
            rows = self.conn.execute(
                "SELECT * FROM symbols WHERE name = ? AND config_hash = ? LIMIT ?",
                (name, self.config_hash, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM symbols WHERE name LIKE ? AND config_hash = ? LIMIT ?",
                (name + "%", self.config_hash, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_symbols(self) -> int:
        """Return total symbol count for this build."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE config_hash = ?",
            (self.config_hash,),
        ).fetchone()
        return row[0] if row else 0

    def get_by_kind(self, kind: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return symbols filtered by kind."""
        rows = self.conn.execute(
            "SELECT * FROM symbols WHERE kind = ? AND config_hash = ? LIMIT ?",
            (kind, self.config_hash, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_definitions(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return definition symbols (is_definition=1)."""
        rows = self.conn.execute(
            "SELECT * FROM symbols WHERE is_definition = 1 AND config_hash = ? LIMIT ?",
            (self.config_hash, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── embeddings ─────────────────────────────────────────────────────

    def get_embeddings(self, limit: int = 0) -> list[dict[str, Any]]:
        """Return symbols with embeddings, ordered by USR."""
        from ._embeddings import get_embeddings

        return get_embeddings(self.conn, self.config_hash, limit=limit)

    def count_embeddings(self) -> int:
        """Return the number of symbols with embeddings."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE config_hash = ?",
            (self.config_hash,),
        ).fetchone()
        return row[0] if row else 0

    # ── refs ───────────────────────────────────────────────────────────

    def find_refs(self, usr: str) -> list[dict[str, Any]]:
        """Return all references to a symbol by USR."""
        from ._refs import find_refs

        return find_refs(self.conn, usr, self.config_hash)

    def count_refs(self) -> int:
        """Return total reference count for this build."""
        from ._refs import count_refs

        return count_refs(self.conn, self.config_hash)

    # ── files ──────────────────────────────────────────────────────────

    def get_file_map(self, file_path: str) -> dict[str, Any]:
        """Return symbols in a file grouped by kind."""
        from ._files import get_file_map

        return get_file_map(self.conn, file_path, self.config_hash)

    def get_file_hashes(self) -> list[dict[str, Any]]:
        """Return file hash records for staleness checks."""
        from ._files import get_file_hashes

        return get_file_hashes(self.conn, self.config_hash)

    # ── LLM analysis ───────────────────────────────────────────────────

    def get_llm_analysis(self, content_hash: str) -> dict[str, Any] | None:
        """Return cached LLM analysis for a content hash."""
        from ._llm import get_llm_analysis_for_symbol

        return get_llm_analysis_for_symbol(self.conn, content_hash)

    def count_llm_analysis(self) -> int:
        """Return the number of LLM analysis cache entries."""
        from ._llm import count_llm_analysis

        return count_llm_analysis(self.conn, self.config_hash)

    # ── callgraph ──────────────────────────────────────────────────────

    def find_callers(self, name: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return direct callers of a symbol."""
        from ._refs import find_refs

        return find_refs(self.conn, name, self.config_hash, limit=limit)

    def find_hotspots(self, limit: int = 20, project_only: bool = True) -> list[dict[str, Any]]:
        """Return most-called functions."""
        from ._callgraph import find_hotspots

        return find_hotspots(self.conn, self.config_hash, limit=limit, project_only=project_only)

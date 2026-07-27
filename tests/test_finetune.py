"""Tests for the self-supervised fine-tuning pipeline."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fw_context_mcp.indexer import finetune


@pytest.fixture
def sample_db() -> sqlite3.Connection:
    """In-memory DB with build_configs and symbol rows for mining."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE build_configs (
            id INTEGER PRIMARY KEY,
            project_id TEXT,
            config_hash TEXT,
            indexed_at TEXT
        );
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY,
            config_hash TEXT,
            file_path TEXT,
            name TEXT,
            kind TEXT,
            is_definition INTEGER,
            signature TEXT,
            docstring TEXT,
            summary TEXT,
            is_project INTEGER DEFAULT 1
        );
    """)
    conn.execute("INSERT INTO build_configs VALUES (1, 'test', 'hash1', '2026-01-01')")
    for i, (name, kind, summary) in enumerate([
        ("uart_init", "function", "Initialize UART hardware"),
        ("dma_irq", "function", "Handle DMA transfer completion"),
        ("i2c_read", "method", "Read bytes from I2C bus"),
    ], start=1):
        conn.execute(
            """INSERT INTO symbols (id, config_hash, name, kind, is_definition,
               signature, summary, is_project, file_path)
               VALUES (?, 'hash1', ?, ?, 1, 'void f()', ?, 1, 'src/drv/'||?)""",
            (i, name, kind, summary, name),
        )
    conn.commit()
    return conn


class TestGenerateQueries:
    def test_generates_queries_from_summaries(self, sample_db: sqlite3.Connection) -> None:
        queries = finetune._generate_queries(sample_db, "hash1")
        assert len(queries) > 0
        assert all(isinstance(q, tuple) and len(q) == 2 for q in queries)
        text, sid = queries[0]
        assert isinstance(text, str) and len(text) >= 8
        assert isinstance(sid, int) and sid > 0

    def test_skips_symbols_without_summary(self, sample_db: sqlite3.Connection) -> None:
        sample_db.execute("DELETE FROM symbols")
        sample_db.execute(
            "INSERT INTO symbols VALUES (1, 'hash1', 'f.c', 'noop', 'function', "
            "1, 'void f()', NULL, NULL, 1)"
        )
        sample_db.commit()
        queries = finetune._generate_queries(sample_db, "hash1")
        assert len(queries) == 0

    def test_query_templates_contain_symbol_fields(self, sample_db: sqlite3.Connection) -> None:
        queries = finetune._generate_queries(sample_db, "hash1")
        texts = {q[0].lower() for q in queries}
        assert any("initialize" in t or "uart" in t for t in texts)
        assert any("dma" in t or "handle" in t for t in texts)

    def test_template_uses_kind(self, sample_db: sqlite3.Connection) -> None:
        queries = finetune._generate_queries(sample_db, "hash1")
        texts = {q[0] for q in queries}
        assert any(
            "method" in t.lower() or "function" in t.lower()
            for t in texts
        )


class TestMiningResult:
    def test_default_result(self) -> None:
        r = finetune.MiningResult()
        assert r.triples == []
        assert r.queries_generated == 0
        assert r.disagreements_found == 0
        assert r.elapsed_s == 0.0

    def test_result_with_data(self) -> None:
        r = finetune.MiningResult(
            triples=[{"query": "test", "positive_id": 1, "negative_id": 2}],
            queries_generated=10,
            disagreements_found=3,
            elapsed_s=1.5,
        )
        assert r.queries_generated == 10
        assert r.disagreements_found == 3
        assert len(r.triples) == 1


class TestModelPath:
    def test_ft_models_dir(self) -> None:
        assert finetune.FT_MODELS_DIR == Path.home() / ".fw-context" / "models"


class TestTripleLoading:
    def test_loads_symbol_bodies(self, sample_db: sqlite3.Connection) -> None:
        bodies = finetune._load_triple_bodies(sample_db, "hash1", {1, 2, 3})
        assert len(bodies) == 3
        assert all(isinstance(v, str) for v in bodies.values())
        assert "Initialize" in bodies[1] or "UART" in bodies[1]
        assert "dma" in bodies[2].lower()


class TestDisagreementMiningEmpty:
    def test_empty_db_returns_clean_result(self) -> None:
        """Mining on a nonexistent DB returns empty MiningResult without crash."""
        from fw_context_mcp.config.settings import Config

        cfg = Config()
        result = finetune.mine_disagreements(
            cfg, Path("/nonexistent.db"),
            None,  # type: ignore[arg-type]
            sample_limit=10,
        )
        assert isinstance(result, finetune.MiningResult)
        assert result.triples == []
        assert result.disagreements_found == 0

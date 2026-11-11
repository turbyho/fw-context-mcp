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
        import random

        random.seed(42)  # deterministic template selection
        queries = finetune._generate_queries(sample_db, "hash1")
        texts = {q[0].lower() for q in queries}
        assert any("initialize" in t or "uart" in t or "dma" in t or "handle" in t or "i2c" in t for t in texts)

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
    def test_empty_db_returns_clean_result(self, tmp_path: Path) -> None:
        """Mining on a nonexistent DB returns an empty MiningResult, and makes no database.

        The mining only reads.  ``sqlite3.connect`` made the file, and the
        mining then failed on a missing table.  The path ``/nonexistent.db``
        of this test hid it on Linux, where a user cannot write to ``/``:
        on Windows the file appeared in ``C:\\``.
        """
        from fw_context_mcp.config.settings import Config

        missing = tmp_path / "nonexistent.db"
        result = finetune.mine_disagreements(
            Config(), missing,
            None,  # type: ignore[arg-type]  # the mining stops before it uses the embedder
            sample_limit=10,
        )
        assert isinstance(result, finetune.MiningResult)
        assert result.triples == []
        assert result.disagreements_found == 0
        assert not missing.exists()

    def test_an_index_without_sqlite_vec_stops_before_the_embedding(self, tmp_path: Path, monkeypatch) -> None:
        """The dense search needs sqlite-vec: without it the mining must stop at once.

        It embedded each query first (up to 2000), and the first dense
        search then stopped with "no such module: vec0".
        """
        from fw_context_mcp.config.settings import Config
        from fw_context_mcp.indexer.db import (
            _connection,
            insert_symbols_batch,
            open_db,
            split_tokens,
            transaction,
            upsert_build_config,
            upsert_file,
            upsert_project,
        )

        # One built index with one documented function: the mining makes a
        # query of it, thus it reaches the embedding.
        db_path = tmp_path / "index.db"
        conn = open_db(db_path)
        try:
            with transaction(conn):
                upsert_project(conn, "p" * 32, "mining", str(tmp_path))
                upsert_build_config(conn, "h", "p" * 32, str(tmp_path / "compile_commands.json"))
                file_id = upsert_file(conn, "h", "src/uart.c", "c")
                insert_symbols_batch(conn, [
                    ("h", file_id, "src/uart.c", split_tokens("uart_init", "uart_init"),
                     "c:@F@uart_init", "uart_init", "uart_init", "function",
                     1, 1, 3, 1, "void uart_init(void)", "Start the UART.", None, 0, 0, "", 0, "", 1, 0.0, "", 0),
                ])
        finally:
            conn.close()
        monkeypatch.setattr(_connection, "_load_sqlite_vec", lambda conn: None)

        class _NoEmbedding:
            def embed_queries(self, texts):
                raise AssertionError("the mining embedded without sqlite-vec")

        result = finetune.mine_disagreements(
            Config(), db_path,
            _NoEmbedding(),  # type: ignore[arg-type]  # only embed_queries can be called
            sample_limit=10,
        )
        assert result.triples == []


class TestTrainStepTypeCheck:
    """Regression for F4 — train_step rejects non-ST embedders."""

    def test_rejects_ollama_embedder(self, tmp_path: Path) -> None:
        """train_step with OllamaEmbedder must return None."""
        from fw_context_mcp.config.settings import LLMConfig
        from fw_context_mcp.indexer.finetune import train_step

        cfg = LLMConfig(embed_model="mxbai-embed-large:latest")
        from fw_context_mcp.llm.ollama import OllamaEmbedder
        ollama = OllamaEmbedder(cfg)

        result = train_step(
            triples=[{"query": "test", "positive_id": 1, "negative_id": 2}],
            db_path=tmp_path / "test.db",
            embedder=ollama,
            out_dir=tmp_path / "out",
        )
        assert result is None  # rejected

    def test_rejects_unknown_embedder(self, tmp_path: Path) -> None:
        from fw_context_mcp.indexer.finetune import train_step
        from fw_context_mcp.llm.embedder import Embedder

        class PlainEmbedder(Embedder):
            def embed_documents(self, texts): return [[0.0]]
            def embed_queries(self, texts): return [[0.0]]
            @property
            def name(self): return "plain"
            @property
            def dim(self): return 1
            @property
            def max_tokens(self): return 512

        plain = PlainEmbedder()
        result = train_step(
            triples=[{"query": "test", "positive_id": 1, "negative_id": 2}],
            db_path=tmp_path / "test.db",
            embedder=plain,
            out_dir=tmp_path / "out",
        )
        assert result is None  # rejected — not an ST embedder

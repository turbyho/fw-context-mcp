"""Unit tests for SyncQueryExecutor — the single-connection query executor.

Covers:
- creation, query execution, lock serialization under concurrent threads
- reindex detection: pid-file TRANSITION (present→absent) triggers a
  reconnect, while the steady state (pid never exists) must NOT reconnect
  — regression test: reindex.pid exists only while a reindex runs, so a
  naive "pid missing" check would reconnect on every query
- DB file identity change (manual reindex, no pid file) triggers reconnect
- interrupt() cancels a running query with OperationalError('interrupted')
- regression: 'interrupted' is NOT fatal — no reconnect+retry after a
  cancelled query (retrying would re-run the just-cancelled query)
- regression: config_hash is passed per call, never stored on the executor
- health check reconnects a dead connection (ported from the removed
  connection-pool tests)
- invalidate_executor / shutdown semantics
"""

from __future__ import annotations

import fw_context_mcp  # noqa: F401 — must import FIRST: it redirects sqlite3 → pysqlite3

import sqlite3  # noqa: E402 — resolves to pysqlite3 after the redirect above
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from fw_context_mcp.mcp.shared.executor import (  # noqa: E402
    SyncQueryExecutor,
    get_executor,
    interrupt_all,
    invalidate_executor,
)


def _make_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "index.db"
    _make_db(path)
    return path


@pytest.fixture
def executor(db_path: Path):
    ex = SyncQueryExecutor(str(db_path.resolve()), db_path)
    yield ex
    ex.shutdown()


class TestExecution:
    def test_basic_query(self, executor):
        rows = executor.execute_sync(
            lambda conn, h: conn.execute("SELECT x FROM t").fetchall(), "hash1"
        )
        assert [tuple(r) for r in rows] == [(1,)]

    def test_config_hash_passed_per_call(self, executor):
        """N1 regression: the executor never stores config_hash — each call
        must receive the value passed to THAT call."""
        seen: list[str] = []
        executor.execute_sync(lambda conn, h: seen.append(h), "hash_a")
        executor.execute_sync(lambda conn, h: seen.append(h), "hash_b")
        assert seen == ["hash_a", "hash_b"]

    def test_query_error_propagates_without_reconnect(self, executor):
        """Query errors (missing table) are caller bugs — no reconnect."""
        conn_before = executor._conn
        with pytest.raises(sqlite3.OperationalError):
            executor.execute_sync(
                lambda conn, h: conn.execute("SELECT * FROM nope").fetchall(), "h"
            )
        assert executor._conn is conn_before

    def test_concurrent_queries_serialize(self, executor):
        """N threads submit queries — the lock must serialize execution."""
        active = 0
        max_active = 0
        guard = threading.Lock()

        def q(conn, h, _i):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.01)
            with guard:
                active -= 1
            return conn.execute("SELECT x FROM t").fetchall()

        threads = [
            threading.Thread(target=executor.execute_sync, args=(q, "h", i))
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert max_active == 1


class TestReindexDetection:
    def test_steady_state_no_reconnect(self, executor, db_path):
        """C1 regression: reindex.pid never existing must NOT reconnect."""
        conn_before = executor._conn
        executor.execute_sync(lambda conn, h: conn.execute("SELECT 1"), "h")
        executor.execute_sync(lambda conn, h: conn.execute("SELECT 1"), "h")
        assert executor._conn is conn_before

    def test_pid_transition_triggers_reconnect(self, executor, db_path):
        pid = db_path.parent / "reindex.pid"
        pid.write_text("12345")
        # First check: pid exists → mark running, no reconnect yet
        executor.execute_sync(lambda conn, h: conn.execute("SELECT 1"), "h")
        conn_running = executor._conn
        # Reindex "finishes": pid disappears → transition → reconnect
        pid.unlink()
        executor.execute_sync(lambda conn, h: conn.execute("SELECT 1"), "h")
        assert executor._conn is not conn_running

    def test_identity_change_triggers_reconnect(self, executor, db_path, tmp_path):
        """Manual 'fw-context index' leaves no pid file — the (st_ino,
        st_mtime) identity change must be detected instead."""
        conn_before = executor._conn
        replacement = tmp_path / "replacement.db"
        _make_db(replacement)
        replacement.replace(db_path)
        executor.execute_sync(lambda conn, h: conn.execute("SELECT 1"), "h")
        assert executor._conn is not conn_before


class TestInterrupt:
    def test_interrupt_stops_long_query(self, executor):
        """interrupt() from another thread cancels the running query."""
        started = threading.Event()
        errors: list[Exception] = []

        def long_query(conn, h):
            started.set()
            # Expensive recursive CTE — runs long enough to be interrupted
            return conn.execute(
                "WITH RECURSIVE cnt(x) AS ("
                "SELECT 1 UNION ALL SELECT x+1 FROM cnt WHERE x < 100000000"
                ") SELECT COUNT(*) FROM cnt"
            ).fetchall()

        def run():
            try:
                executor.execute_sync(long_query, "h")
            except sqlite3.Error as e:
                errors.append(e)

        t = threading.Thread(target=run)
        t.start()
        started.wait(timeout=5)
        time.sleep(0.1)  # let the query actually start executing
        executor.interrupt()
        t.join(timeout=10)

        assert not t.is_alive(), "interrupted query must not hang"
        assert len(errors) == 1
        assert "interrupted" in str(errors[0]).lower()

    def test_interrupted_is_not_retried(self, executor):
        """N2 regression: OperationalError('interrupted') must NOT trigger
        reconnect+retry — the query would run twice and block the lock."""
        calls = 0

        def q(conn, h):
            nonlocal calls
            calls += 1
            raise sqlite3.OperationalError("interrupted")

        conn_before = executor._conn
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            executor.execute_sync(q, "h")
        assert calls == 1, "interrupted query must not be retried"
        assert executor._conn is conn_before, "interrupted must not reconnect"


class TestHealthCheck:
    def test_dead_connection_reconnects(self, executor):
        """A broken connection is detected by the health check and replaced
        (coverage ported from the removed connection-pool tests)."""
        executor._health_interval = 0.0  # force health check on every call
        executor._last_health = 0.0
        conn_before = executor._conn
        conn_before.close()  # simulate a dead connection
        rows = executor.execute_sync(
            lambda conn, h: conn.execute("SELECT x FROM t").fetchall(), "h"
        )
        assert [tuple(r) for r in rows] == [(1,)]
        assert executor._conn is not conn_before

    def test_fatal_error_reconnects_and_retries_once(self, executor):
        calls = 0

        def q(conn, h):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.DatabaseError("database disk image is malformed")
            return conn.execute("SELECT x FROM t").fetchall()

        rows = executor.execute_sync(q, "h")
        assert [tuple(r) for r in rows] == [(1,)]
        assert calls == 2  # one retry after reconnect


class TestRegistry:
    def test_get_executor_singleton(self, db_path):
        ex1 = get_executor(db_path)
        ex2 = get_executor(db_path)
        assert ex1 is ex2
        invalidate_executor(str(db_path.resolve()))

    def test_invalidate_executor_removes_and_shuts_down(self, db_path):
        ex = get_executor(db_path)
        key = str(db_path.resolve())
        invalidate_executor(key)
        assert get_executor(db_path) is not ex
        invalidate_executor(key)

    def test_invalidate_all(self, db_path):
        get_executor(db_path)
        invalidate_executor(None)  # must not raise
        ex = get_executor(db_path)
        invalidate_executor(None)
        ex.shutdown()

    def test_interrupt_all_no_executors(self):
        interrupt_all()  # empty registry — must not raise

    def test_shutdown_idempotent(self, executor):
        executor.shutdown()
        executor.shutdown()

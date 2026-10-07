"""Edge case tests for KeywordCache TTL cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.search.cache import KeywordCache


class TestKeywordCache:
    def test_get_missing_key_returns_none(self):
        c = KeywordCache()
        assert c.get(("missing", "key")) is None

    def test_set_and_get(self):
        c = KeywordCache()
        c.set(("query", "hash"), ["term1", "term2"])
        queries, understanding = c.get(("query", "hash"))
        assert queries == ["term1", "term2"]
        assert understanding == ""

    def test_overwrite_existing_key(self):
        c = KeywordCache()
        c.set(("q", "h"), ["old"])
        c.set(("q", "h"), ["new"], "understood")
        queries, understanding = c.get(("q", "h"))
        assert queries == ["new"]
        assert understanding == "understood"

    def test_different_keys_independent(self):
        c = KeywordCache()
        c.set(("q1", "h"), ["a"])
        c.set(("q2", "h"), ["b"])
        assert c.get(("q1", "h"))[0] == ["a"]
        assert c.get(("q2", "h"))[0] == ["b"]

    def test_clear_removes_all(self):
        c = KeywordCache()
        c.set(("q1", "h"), ["a"])
        c.set(("q2", "h"), ["b"])
        c.clear()
        assert c.get(("q1", "h")) is None
        assert c.get(("q2", "h")) is None
        assert len(c) == 0

    def test_len_tracks_entries(self):
        c = KeywordCache()
        assert len(c) == 0
        c.set(("a", "b"), ["x"])
        assert len(c) == 1
        c.set(("a", "b"), ["y"])  # overwrite
        assert len(c) == 1
        c.set(("c", "d"), ["z"])
        assert len(c) == 2

    def test_zero_ttl_always_expired(self):
        c = KeywordCache(ttl_s=0)
        c.set(("q", "h"), ["test"])
        assert c.get(("q", "h")) is None

    def test_negative_ttl_always_expired(self):
        c = KeywordCache(ttl_s=-1)
        c.set(("q", "h"), ["test"])
        assert c.get(("q", "h")) is None

    def test_max_entries_one_evicts_previous(self):
        c = KeywordCache(max_entries=1)
        c.set(("a", "h"), ["first"])
        c.set(("b", "h"), ["second"])
        # Only second should remain (oldest evicted)
        assert c.get(("a", "h")) is None
        assert c.get(("b", "h"))[0] == ["second"]

    def test_max_entries_zero_raises_on_set(self):
        c = KeywordCache(max_entries=0)
        # max_entries=0 means store is always "at capacity".
        # popitem from empty OrderedDict raises KeyError.
        with pytest.raises(KeyError):
            c.set(("a", "h"), ["test"])

    def test_eviction_order_fifo_by_timestamp(self):
        c = KeywordCache(max_entries=3)
        c.set(("a", "h"), ["1"])
        c.set(("b", "h"), ["2"])
        c.set(("c", "h"), ["3"])
        c.set(("d", "h"), ["4"])
        # Oldest ("a") evicted
        assert c.get(("a", "h")) is None
        assert c.get(("b", "h"))[0] == ["2"]
        assert c.get(("c", "h"))[0] == ["3"]
        assert c.get(("d", "h"))[0] == ["4"]

    def test_get_expired_entry_removes_it(self):
        c = KeywordCache(ttl_s=0)
        c.set(("q", "h"), ["test"])
        c.get(("q", "h"))  # triggers expiry and removal
        assert len(c) == 0

    def test_eviction_when_at_capacity(self):
        c = KeywordCache(max_entries=2)
        c.set(("a", "h"), ["1"])
        c.set(("b", "h"), ["2"])
        # Overwrite doesn't trigger eviction
        c.set(("a", "h"), ["1v2"])
        assert len(c) == 2
        # New key at capacity evicts oldest
        c.set(("c", "h"), ["3"])
        assert len(c) == 2

    def test_tuple_key_identity(self):
        c = KeywordCache()
        k1 = ("query", "hash_value")
        k2 = ("query", "hash_value")
        c.set(k1, ["result"])
        assert c.get(k2)[0] == ["result"]
    def test_large_cache_no_crash(self):
        c = KeywordCache(max_entries=100)
        for i in range(100):
            c.set((f"q{i}", "h"), [f"term{i}"])
        assert len(c) == 100
        # Adding one more evicts first inserted
        c.set(("q100", "h"), ["term100"])
        assert len(c) == 100
        assert c.get(("q0", "h")) is None


class TestKeywordCacheThreadSafety:
    """Regression: KeywordCache must be thread-safe after lock addition."""

    def test_concurrent_set_and_get_no_crash(self) -> None:
        """Concurrent set/get should not crash or corrupt."""
        import threading

        c = KeywordCache(max_entries=200)
        errors: list[Exception] = []

        def writer(start: int) -> None:
            try:
                for i in range(start, start + 50):
                    c.set((f"q{i}", "h"), [f"term{i}"])
            except Exception as e:
                errors.append(e)

        def reader() -> None:
            try:
                for _ in range(100):
                    c.get(("q0", "h"))
                    len(c)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=(i * 50,))
            for i in range(3)
        ] + [threading.Thread(target=reader) for _ in range(2)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread safety errors: {errors}"
        assert len(c) > 0

    def test_concurrent_clear_and_get_no_crash(self) -> None:
        """Concurrent clear + get should not crash."""
        import threading

        c = KeywordCache()
        for i in range(50):
            c.set((f"q{i}", "h"), [f"term{i}"])

        errors: list[Exception] = []

        def clearer() -> None:
            try:
                c.clear()
            except Exception as e:
                errors.append(e)

        def getter() -> None:
            try:
                for i in range(50):
                    c.get((f"q{i}", "h"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=clearer)] + [
            threading.Thread(target=getter) for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread safety errors: {errors}"


class TestLocalCacheDbReadonly:
    """Regression for F20 — get_local_cache_db readonly path must not
    run PRAGMAs or CREATE TABLE on the read-only connection."""

    def test_readonly_does_not_crash_on_fresh_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_local_cache_db(readonly=True) on a nonexistent DB creates it first."""
        import fw_context_mcp.cache_client as cc_mod

        monkeypatch.setenv("FW_CONTEXT_HOME", str(tmp_path))
        conn = cc_mod.get_local_cache_db(readonly=True)
        assert conn is not None
        conn.close()
        assert (tmp_path / "llm_cache.db").exists()

    def test_readonly_on_existing_db_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After a write path initializes the DB, readonly opens cleanly."""
        import fw_context_mcp.cache_client as cc_mod

        monkeypatch.setenv("FW_CONTEXT_HOME", str(tmp_path))
        conn_rw = cc_mod.get_local_cache_db(readonly=False)
        conn_rw.close()

        conn_ro = cc_mod.get_local_cache_db(readonly=True)
        count = conn_ro.execute(
            "SELECT COUNT(*) FROM llm_analysis_cache"
        ).fetchone()[0]
        assert count == 0
        conn_ro.close()


class TestLocalCachePath:
    """The local LLM cache is in ``$FW_CONTEXT_HOME``, read at each call.

    The path was a module constant from ``Path.home()``, set at import.
    The test session sets ``FW_CONTEXT_HOME``, but a test that wrote to the
    cache wrote to ``~/.fw-context/llm_cache.db`` of the operator: rows with
    the model ``test`` were found there, next to real analyses.  An indexer
    run reads that cache, thus such a row can give a false analysis to a
    real symbol with the same content.
    """

    def test_cache_follows_fw_context_home_set_after_import(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A change of ``FW_CONTEXT_HOME`` after the import moves the cache."""
        import fw_context_mcp.cache_client as cc_mod

        monkeypatch.setenv("FW_CONTEXT_HOME", str(tmp_path))
        conn = cc_mod.get_local_cache_db()
        conn.close()

        assert (tmp_path / "llm_cache.db").is_file()
        assert cc_mod.local_cache_stats()["path"] == str(tmp_path / "llm_cache.db")
        assert cc_mod.local_cache_clear() == 0
        assert not (tmp_path / "llm_cache.db").exists()

    def test_relative_fw_context_home_opens_the_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relative ``FW_CONTEXT_HOME`` is relative to the current directory.

        SQLite reads ``file://relhome/llm_cache.db`` as a URI with the
        authority ``relhome`` and stops with ``invalid uri authority``.
        """
        import fw_context_mcp.cache_client as cc_mod

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FW_CONTEXT_HOME", "relhome")
        conn = cc_mod.get_local_cache_db()
        conn.close()

        assert (tmp_path / "relhome" / "llm_cache.db").is_file()

    def test_session_cache_is_not_in_the_home_of_the_operator(self) -> None:
        """The session isolation of ``conftest.py`` covers the cache too."""
        import os

        import fw_context_mcp.cache_client as cc_mod

        cache_path = Path(cc_mod.local_cache_stats()["path"])
        assert cache_path.parent == Path(os.environ["FW_CONTEXT_HOME"])
        assert cache_path.parent != Path.home() / ".fw-context"

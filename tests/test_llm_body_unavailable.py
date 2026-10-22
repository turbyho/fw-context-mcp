"""A symbol whose body cannot be read must not reach the model.

Observed defect: a background reindex logged

    ERROR [function] empty body for the Mbed project::img_parse_tlvs at
    .../src/img_parse.cpp:56-216 — path resolution or extent mismatch

The cause was benign — an editor shortened the file while the reindex read
it, thus ``end_line`` was past the end of the file and ``_read_body``
returned an empty string.  The handling was not benign: ``_enrich_batch``
only logged, and the loop sent the symbol to Ollama with ``body=""``.  The
answer then went into the project database, into the local cache
(``~/.fw-context/llm_cache.db``) and to the remote cache server, under the
hash of an EMPTY body.

An analysis without the body is worthless, and a content-addressable cache
must never hold one.  The repair marks the symbol with a ``skip:nobody``
sentinel in the per-build table only.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from fw_context_mcp.indexer._llm_analysis import _build_llm_analysis, _enrich_batch
from fw_context_mcp.indexer.db import (
    compute_analysis_coverage,
    make_analysis_summary,
    transaction,
    upsert_file,
)
from fw_context_mcp.indexer.db._symbols import insert_symbols_batch

CONFIG_HASH = "hash-deadbeef"


def _source_file(tmp_path: Path, lines: int) -> Path:
    """Write a C++ file with *lines* numbered lines and return its path."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    f = src / "img_parse.cpp"
    f.write_text("".join(f"// line {i}\n" for i in range(1, lines + 1)), encoding="utf-8")
    return f


def _row(
    *,
    file_path: str,
    kind: str = "function",
    line: int = 5,
    end_line: int = 40,
    usr: str = "",
) -> dict:
    """Build the minimal row that ``_enrich_batch`` reads."""
    return {
        "id": 1,
        "name": "img_parse_tlvs",
        "qualified_name": "the Mbed project::img_parse_tlvs",
        "kind": kind,
        "file_path": file_path,
        "line": line,
        "end_line": end_line,
        "usr": usr,
        "signature": "int img_parse_tlvs(const uint8_t*, size_t)",
        "docstring": "",
    }


# ── _enrich_batch must report the failure, not hide it ───────────────────────


class TestEnrichBatchFlag:
    """``body_unavailable`` must be True exactly when a body is missing."""

    def test_an_extent_past_the_end_of_the_file(self, tmp_path: Path):
        """The reported case: the file shrank while the reindex read it."""
        f = _source_file(tmp_path, lines=30)
        # conn stays None: an empty usr keeps _enrich_batch away from the DB.
        out = _enrich_batch(
            None, [_row(file_path=str(f), line=5, end_line=40)], CONFIG_HASH,
            project_root=tmp_path,
        )

        assert out[0]["body"] == ""
        assert out[0]["body_unavailable"] is True

    def test_a_missing_file(self, tmp_path: Path):
        out = _enrich_batch(
            None,
            [_row(file_path=str(tmp_path / "src" / "gone.cpp"))],
            CONFIG_HASH,
            project_root=tmp_path,
        )

        assert out[0]["body"] == ""
        assert out[0]["body_unavailable"] is True

    def test_a_body_that_reads_correctly(self, tmp_path: Path):
        f = _source_file(tmp_path, lines=60)
        out = _enrich_batch(
            None, [_row(file_path=str(f), line=5, end_line=40)], CONFIG_HASH,
            project_root=tmp_path,
        )

        assert out[0]["body"].startswith("// line 5")
        assert out[0]["body_unavailable"] is False

    def test_a_kind_that_needs_no_body(self, tmp_path: Path):
        """A kind that the analysis does not select has no body, thus nothing is unavailable."""
        f = _source_file(tmp_path, lines=30)
        out = _enrich_batch(
            None,
            [_row(file_path=str(f), kind="field", line=5, end_line=40)],
            CONFIG_HASH,
            project_root=tmp_path,
        )

        assert out[0]["body"] == ""
        assert out[0]["body_unavailable"] is False

    def test_a_global_variable_gets_its_definition_as_the_body(self, tmp_path: Path):
        """The initializer and the comment of the line tell what a constant means.

        Before 2026-10-22 the model got no body for a varglobal and guessed
        the meaning from the name: "compressor" for COND, a threshold for an
        alarm register whose comment gives its states.
        """
        f = _source_file(tmp_path, lines=30)
        definition = "constexpr uint16_t LOW_VOLTAGE_ALARM = 4306;  // 0=Ok;1=Warning;2=Alarm\n"
        row = _row(file_path=str(f), kind="varglobal", line=5, end_line=5)
        row["source"] = definition

        out = _enrich_batch(None, [row], CONFIG_HASH, project_root=tmp_path)

        assert out[0]["body"] == definition
        assert out[0]["body_unavailable"] is False

    def test_a_global_variable_in_an_inactive_branch_is_unavailable(self, tmp_path: Path):
        """The same rule as for a function: a blank stored body never reaches the model."""
        f = _source_file(tmp_path, lines=30)
        row = _row(file_path=str(f), kind="varglobal", line=5, end_line=5)
        row["source"] = "\n"

        out = _enrich_batch(None, [row], CONFIG_HASH, project_root=tmp_path)

        assert out[0]["body"] == ""
        assert out[0]["body_unavailable"] is True

    def test_the_stored_body_wins_over_the_disk(self, tmp_path: Path):
        """The model must read the ifdef-filtered text, not the raw file.

        ``symbols.source`` blanks the lines of an inactive ``#if`` branch.
        The file on disk holds them, thus a body read from it would make the
        model describe code that the build drops.  The same text also feeds
        the hash that decides whether the model runs again — two different
        texts there make every symbol with a dead branch look changed.
        """
        f = _source_file(tmp_path, lines=60)
        filtered = "// line 5\n\n\n// line 8\n"
        row = _row(file_path=str(f), line=5, end_line=8)
        row["source"] = filtered

        out = _enrich_batch(None, [row], CONFIG_HASH, project_root=tmp_path)

        assert out[0]["body"] == filtered
        assert "// line 6" not in out[0]["body"], (
            "line 6 is blank in the stored body — reading the disk would "
            "bring it back"
        )
        assert out[0]["body_unavailable"] is False

    def test_a_stored_body_of_only_blank_lines_is_unavailable(self, tmp_path: Path):
        """A body filtered down to nothing must never reach the model.

        Every line of the symbol is inside an inactive branch, thus the
        stored body holds newlines and no text.  Such a string is truthy,
        so it used to pass as a real body.

        That is unsafe for the caches, not only wasteful: the hash strips
        the body before it hashes, thus the hash of this body is the hash
        of an EMPTY body.  The answer of the model would go into the
        content-addressable caches under that hash, and every later symbol
        with the same name, signature and docstring would read it back.
        """
        f = _source_file(tmp_path, lines=60)
        row = _row(file_path=str(f), line=5, end_line=8)
        row["source"] = "\n\n\n\n"

        out = _enrich_batch(None, [row], CONFIG_HASH, project_root=tmp_path)

        assert out[0]["body_unavailable"] is True, (
            "the stored body holds no text, thus the model has nothing to "
            "read and the sentinel path must take over"
        )
        assert "// line 5" not in out[0]["body"], (
            "the disk holds every branch — falling back to it would give the "
            "model the dead code that the filter removed"
        )

    def test_a_row_from_before_the_source_column_still_reads_the_disk(
        self, tmp_path: Path
    ):
        """An empty column is a legacy row, and the disk must answer for it.

        The blank-body check must not catch this row: nothing was ever
        stored here, thus the disk is the only source, and it is correct.
        """
        f = _source_file(tmp_path, lines=60)
        row = _row(file_path=str(f), line=5, end_line=8)
        row["source"] = ""

        out = _enrich_batch(None, [row], CONFIG_HASH, project_root=tmp_path)

        assert out[0]["body"].startswith("// line 5")
        assert out[0]["body_unavailable"] is False


# ── The analysis loop must skip such a symbol ────────────────────────────────


def _add_symbol(
    conn, file_id: int, file_path: str, *, line: int, end_line: int, source: str = "", kind: str = "function"
) -> int:
    """Insert one analyzable definition symbol and return its row id.

    *source* is empty by default because that is what the indexer stores for
    the extent these tests use.  ``_store_symbol_rows`` calls ``_read_body``,
    which gives an empty string when ``end_line`` is past the end of the
    file — the very condition this module is about.  A stored body next to
    an extent that cannot be read is a state the indexer never writes, and a
    fixture that holds one tests nothing real: ``_enrich_batch`` prefers the
    stored body, thus the symbol would never look body-less.
    """
    insert_symbols_batch(
        conn,
        [
            (CONFIG_HASH, file_id, file_path, "img_parse_tlvs", "usr-img", "img_parse_tlvs",
             "the Mbed project::img_parse_tlvs", kind, line, 1, end_line, 1,
             "int img_parse_tlvs(const uint8_t*, size_t)",
             "", None, 0, 0, "", 0, "", 1, 0.0, source, 0),
        ],
    )
    return conn.execute("SELECT id FROM symbols WHERE usr = 'usr-img'").fetchone()["id"]


class _Recorder:
    """Stands in for the LLM and the caches, and records every call."""

    def __init__(self) -> None:
        self.ollama_calls: list[str] = []
        self.cache_writes: list[list] = []

    def install(self, monkeypatch, *, response: str | None = None) -> None:
        from fw_context_mcp.indexer import _llm_analysis as mod
        from fw_context_mcp.llm.client import ChatResult

        calls = self.ollama_calls

        class _Client:
            """Stands in for the chat client: each prompt has 100 tokens."""

            counts_tokens = True

            def generate(self, prompt, **kw):
                calls.append(prompt)
                if response is None:
                    raise AssertionError("the model must not be called for a body-less symbol")
                return ChatResult(response)

            def count_tokens(self, prompt, num_ctx):
                return 100

        monkeypatch.setattr(mod, "_analysis_contexts", lambda client, cfg: (16384, 32768))
        monkeypatch.setattr(mod, "get_chat_client", lambda cfg: _Client())
        # The loop closes the cache handle at the end, thus a bare None fails.
        monkeypatch.setattr(mod, "get_local_cache_db", lambda: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr(mod, "local_cache_lookup", lambda db, hashes: {})
        monkeypatch.setattr(mod, "local_cache_upsert", lambda db, rows, **_: self.cache_writes.append(rows))


def _analysis_rows(conn) -> list:
    return conn.execute("SELECT symbol_id, model, content_hash FROM llm_analysis").fetchall()


class TestAnalysisLoopSkipsBodylessSymbols:
    """No model call, no cache write, and no analysis that stays incomplete."""

    @staticmethod
    def _run(conn, tmp_path: Path) -> None:
        _build_llm_analysis(
            conn,
            CONFIG_HASH,
            SimpleNamespace(model="test-model"),
            tmp_path,
            project_only=True,
            project_root=tmp_path,
            write_lock_held=True,
            cache_client=None,
        )

    def test_it_stores_a_sentinel_and_calls_nothing(
        self, populated_db, monkeypatch, tmp_path: Path
    ):
        f = _source_file(tmp_path, lines=30)
        with transaction(populated_db):
            file_id = upsert_file(populated_db, CONFIG_HASH, str(f), "cpp")
            symbol_id = _add_symbol(populated_db, file_id, str(f), line=5, end_line=40)

        rec = _Recorder()
        rec.install(monkeypatch)  # any Ollama call raises AssertionError
        self._run(populated_db, tmp_path)

        rows = _analysis_rows(populated_db)
        assert len(rows) == 1
        assert rows[0]["symbol_id"] == symbol_id
        assert rows[0]["model"] == "skip:nobody"
        assert rec.ollama_calls == []
        assert rec.cache_writes == [], "a body-less answer must never enter the cache"

    def test_the_sentinel_does_not_keep_the_analysis_incomplete(
        self, populated_db, monkeypatch, tmp_path: Path
    ):
        """A file that a build step removed must not keep the analysis incomplete.

        ``skip:*`` counts as done, thus the coverage report is complete.
        """
        f = _source_file(tmp_path, lines=30)
        with transaction(populated_db):
            file_id = upsert_file(populated_db, CONFIG_HASH, str(f), "cpp")
            _add_symbol(populated_db, file_id, str(f), line=5, end_line=40)

        _Recorder().install(monkeypatch)
        self._run(populated_db, tmp_path)

        coverage = compute_analysis_coverage(populated_db, CONFIG_HASH)
        assert coverage["project"]["skipped"] == 1
        assert make_analysis_summary(coverage, False, None)["complete"] is True
        assert coverage["project"]["analyzed"] == 0

    def test_the_next_run_repairs_the_symbol(self, populated_db, monkeypatch, tmp_path: Path):
        """The sentinel holds the hash of an EMPTY body.

        A body that reads correctly gives a different hash, thus the symbol
        goes to the model again without any sentinel-clearing rule.
        """
        f = _source_file(tmp_path, lines=30)
        with transaction(populated_db):
            file_id = upsert_file(populated_db, CONFIG_HASH, str(f), "cpp")
            _add_symbol(populated_db, file_id, str(f), line=5, end_line=40)

        _Recorder().install(monkeypatch)
        self._run(populated_db, tmp_path)
        sentinel_hash = _analysis_rows(populated_db)[0]["content_hash"]

        # The editor finishes its write — the file is long enough again.
        _source_file(tmp_path, lines=60)
        rec = _Recorder()
        rec.install(monkeypatch, response="not json")
        self._run(populated_db, tmp_path)

        assert len(rec.ollama_calls) == 1, "the repaired symbol must reach the model"
        assert "// line 5" in rec.ollama_calls[0], "the prompt must carry the real body"
        rows = _analysis_rows(populated_db)
        assert rows[0]["content_hash"] != sentinel_hash
        assert rows[0]["model"] == "skip:unparseable:test-model"

    def test_a_readable_body_still_reaches_the_model(
        self, populated_db, monkeypatch, tmp_path: Path
    ):
        """Guard: the skip must not catch symbols that are perfectly fine."""
        f = _source_file(tmp_path, lines=60)
        with transaction(populated_db):
            file_id = upsert_file(populated_db, CONFIG_HASH, str(f), "cpp")
            _add_symbol(populated_db, file_id, str(f), line=5, end_line=40)

        rec = _Recorder()
        rec.install(monkeypatch, response="not json")
        self._run(populated_db, tmp_path)

        assert len(rec.ollama_calls) == 1
        assert _analysis_rows(populated_db)[0]["model"] == "skip:unparseable:test-model"


class TestGlobalVariableBody:
    """A global variable gets its definition as the body (2026-10-22), in the loop too."""

    _run = staticmethod(TestAnalysisLoopSkipsBodylessSymbols._run)

    def test_an_earlier_analysis_is_made_again_one_time_then_it_stays(
        self, populated_db, monkeypatch, tmp_path: Path
    ):
        """The stored hash of an analysis without the definition matches no more.

        Before, the model got no body for a global variable, thus the stored
        hash is the hash of an empty body.  The first run after the change
        sends the definition one time; the next run matches the new hash.
        """
        from fw_context_mcp.indexer.db import upsert_llm_analysis_batch
        from fw_context_mcp.utils import compute_content_hash

        f = _source_file(tmp_path, lines=30)
        definition = "constexpr uint16_t LOW_VOLTAGE_ALARM = 4306;  // 0=Ok;1=Warning;2=Alarm\n"
        with transaction(populated_db):
            file_id = upsert_file(populated_db, CONFIG_HASH, str(f), "cpp")
            symbol_id = _add_symbol(populated_db, file_id, str(f), line=5, end_line=5, source=definition,
                                    kind="varglobal")
            old_hash = compute_content_hash("", "the Mbed project::img_parse_tlvs",
                                            "int img_parse_tlvs(const uint8_t*, size_t)", "")
            upsert_llm_analysis_batch(populated_db, [(symbol_id, "old", "i", "o", "old-model", old_hash)])
        answer = f'[{{"id": {symbol_id}, "summary": "s", "inputs": "i", "outputs": "o"}}]'

        rec = _Recorder()
        rec.install(monkeypatch, response=answer)
        self._run(populated_db, tmp_path)
        self._run(populated_db, tmp_path)

        assert len(rec.ollama_calls) == 1, "one new analysis, then the hash matches"
        assert "4306" in rec.ollama_calls[0] and "0=Ok;1=Warning;2=Alarm" in rec.ollama_calls[0]
        assert _analysis_rows(populated_db)[0]["content_hash"] != old_hash

    def test_a_definition_that_cannot_be_read_gets_the_sentinel(
        self, populated_db, monkeypatch, tmp_path: Path
    ):
        """No stored text and an extent past the end of the file: no model call, no cache write.

        Before 2026-10-22 a global variable went to the model without a body
        in this case too, because its kind had no body.
        """
        f = _source_file(tmp_path, lines=30)
        with transaction(populated_db):
            file_id = upsert_file(populated_db, CONFIG_HASH, str(f), "cpp")
            _add_symbol(populated_db, file_id, str(f), line=40, end_line=40, kind="varglobal")

        rec = _Recorder()
        rec.install(monkeypatch)  # any model call raises AssertionError
        self._run(populated_db, tmp_path)

        assert _analysis_rows(populated_db)[0]["model"] == "skip:nobody"
        assert rec.ollama_calls == [] and rec.cache_writes == []


def test_a_prompt_that_the_api_refuses_for_the_context_gets_the_sentinel_at_once(
    populated_db, monkeypatch, tmp_path: Path
):
    """An API counts no tokens for the client, and refuses a prompt that does not fit (HTTP 400).

    Before, the refusal was a plain failure: three requests, a failed
    count, and the same three requests at each later run.  Now one
    request, and ``skip:toolarge`` as for Ollama; no cache write.
    """
    from fw_context_mcp.indexer import _llm_analysis as mod
    from fw_context_mcp.llm.errors import ChatPromptTooLong

    f = _source_file(tmp_path, lines=60)
    with transaction(populated_db):
        file_id = upsert_file(populated_db, CONFIG_HASH, str(f), "cpp")
        _add_symbol(populated_db, file_id, str(f), line=5, end_line=40, kind="varglobal")
    calls: list[str] = []

    class _Api:
        counts_tokens = False

        def generate(self, prompt, **kw):
            calls.append(prompt)
            raise ChatPromptTooLong("Chat API HTTP 400: the prompt and the answer budget do not fit the context",
                                    status_code=400)

    rec = _Recorder()
    rec.install(monkeypatch)
    monkeypatch.setattr(mod, "_analysis_contexts", lambda client, cfg: (16384, 16384))
    monkeypatch.setattr(mod, "get_chat_client", lambda cfg: _Api())

    def _run() -> bool:
        return _build_llm_analysis(populated_db, CONFIG_HASH, SimpleNamespace(model="test-model"), tmp_path,
                                   project_only=True, project_root=tmp_path, write_lock_held=True,
                                   cache_client=None)

    # The sentinel counts as done: the run is complete.
    assert _run() is True
    assert len(calls) == 1, "the same request fails again: no retry"
    assert _analysis_rows(populated_db)[0]["model"] == "skip:toolarge:16384"
    assert rec.cache_writes == []

    calls.clear()
    assert _run() is True
    assert calls == [], "the next run does not send the prompt again"


@pytest.mark.parametrize("sentinel", ["skip:nobody", "skip:toolarge:4096", "skip:unparseable:m"])
def test_every_sentinel_counts_as_skipped(populated_db, sentinel, tmp_path: Path):
    """``compute_analysis_coverage`` matches ``skip:%``, thus a new sentinel
    needs no change to the coverage query."""
    from fw_context_mcp.indexer.db import upsert_llm_analysis_batch

    f = _source_file(tmp_path, lines=30)
    with transaction(populated_db):
        file_id = upsert_file(populated_db, CONFIG_HASH, str(f), "cpp")
        symbol_id = _add_symbol(populated_db, file_id, str(f), line=5, end_line=40)
        upsert_llm_analysis_batch(populated_db, [(symbol_id, "", "", "", sentinel, "h")])

    coverage = compute_analysis_coverage(populated_db, CONFIG_HASH)
    assert coverage["project"]["skipped"] == 1
    assert make_analysis_summary(coverage, False, None)["complete"] is True

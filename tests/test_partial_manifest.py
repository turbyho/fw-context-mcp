"""A run whose embeddings fail is stamped "partial"; a failed full-text index stops the run.

`_run_postprocess` copied the failed critical steps into its context only
after the loop of the steps, and `finalize_manifest` is a step of that
loop: the stamp always saw no failure and wrote "full".  The embedding step
also returned without an error when Ollama was not reachable or batches
failed.  `semantic_search` and `smart_search` then missed symbols, and
nothing told the user.

A "partial" build is current: its symbols, references and headers are
complete.  The status tools ask the caller to tell the user, and no query
answer carries a state for it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _run(tmp_path: Path, monkeypatch, *, fail_step: str | None, source: str = "") -> tuple[Path, str]:
    """Index one file; *fail_step* names a postprocess step that raises.  Returns ``(db_path, verification)``.

    *source* is the text of ``main.c``; the default holds one function.
    """
    from fw_context_mcp.indexer import _postprocess
    from fw_context_mcp.indexer.db import open_db
    from fw_context_mcp.indexer.runner import run

    if fail_step:
        def _fails(conn, ctx):
            raise RuntimeError(f"{fail_step} failed")

        # No guard for the step that fails: the embeddings need a model.
        monkeypatch.setattr(_postprocess, "_STEPS", [
            (name, _fails, None) if name == fail_step else (name, fn, guard)
            for name, fn, guard in _postprocess._STEPS
        ])
    root = tmp_path / "proj"
    root.mkdir()
    (root / "main.c").write_text(source or "int main_value(void) { return 1; }\n", encoding="utf-8")
    cc = root / "compile_commands.json"
    cc.write_text(json.dumps([{"directory": str(root), "file": "main.c", "arguments": ["cc", "-c", "main.c"]}]),
                  encoding="utf-8")
    db_path = tmp_path / "index" / "pid" / "index.db"
    run(compile_commands=cc, db_path=db_path, project_root=root, project_id="pid",
        index_refs=False, index_embeddings=False, analyze_symbols=False, analyze_overrides=False)
    conn = open_db(db_path, skip_integrity_check=True)
    try:
        return db_path, conn.execute("SELECT manifest_verification FROM build_configs").fetchone()[0]
    finally:
        conn.close()


def test_failed_embeddings_stamp_partial(tmp_path, monkeypatch):
    """The stamp read the failed steps before the loop copied them in, and wrote "full"."""
    _, verification = _run(tmp_path, monkeypatch, fail_step="embeddings")

    assert verification == "partial"


def test_a_run_without_a_failure_stamps_full(tmp_path, monkeypatch):
    _, verification = _run(tmp_path, monkeypatch, fail_step=None)

    assert verification == "full"


@pytest.fixture
def partial_build(tmp_path):
    """A project of one build with manifest verification "partial" (`tests/test_index_state.py`)."""
    from tests.test_index_state import _project, _set_manifest

    root, db_dir = _project(tmp_path, builds=[("h-a", "a", "")])
    _set_manifest(db_dir, "partial")
    return root


def test_the_status_checks_the_headers_of_a_partial_build(partial_build, monkeypatch):
    """The manifest with the headers is there: only the embeddings failed."""
    from fw_context_mcp.mcp.handlers import maintenance

    calls = []
    monkeypatch.setattr(maintenance, "_check_header_staleness", lambda *a, **k: calls.append(a) or (0, []))

    maintenance.get_build_info(project_root=str(partial_build))

    assert calls, "the header check ran"


def test_the_warning_of_a_partial_build_asks_to_tell_the_user(partial_build):
    """A STOP of each analysis was wrong, and a plain run with the same model fails again."""
    from fw_context_mcp.mcp.handlers.maintenance import get_build_info

    result = get_build_info(project_root=str(partial_build))

    assert "STOP ALL" not in result["_warning"], result["_warning"]
    assert "semantic_search and smart_search can miss symbols" in result["_warning"]
    assert "Tell the user" in result["_warning"]
    assert "'fw-context doctor --fix'" in result["_warning"], "auto_pull is off: a plain index run installs no model"
    assert "index_embeddings" not in result["_warning"], "that hides incomplete vectors"


def test_the_message_of_a_partial_build_says_it_once(partial_build):
    from fw_context_mcp.mcp.handlers.maintenance import get_build_info

    message = get_build_info(project_root=str(partial_build))["index_message"]

    assert message.count("embeddings are not complete") == 1, message


def test_a_partial_build_is_current(partial_build):
    """Its symbols, references and headers are complete: no reason to wait, and no state on a query."""
    from fw_context_mcp.mcp.handlers.maintenance import get_build_info
    from fw_context_mcp.mcp.shared.index_state import annotate_index_state

    result = get_build_info(project_root=str(partial_build))

    assert result["status"] == "ready" and result["reindex_state"] is None, result
    assert annotate_index_state([{"name": "x"}], {"project_root": str(partial_build)}) == [{"name": "x"}]


def test_a_failed_full_text_index_stops_the_run(tmp_path, monkeypatch):
    """A text search of the build misses each symbol, and the integrity check does not see a missing table."""
    with pytest.raises(RuntimeError, match="fts5 failed"):
        _run(tmp_path, monkeypatch, fail_step="fts5")


def test_an_ollama_that_is_not_reachable_is_reported(tmp_path, monkeypatch):
    """The step logged a warning and returned: the build was stamped complete without embeddings."""
    import httpx

    from fw_context_mcp.config.settings import LLMConfig
    from fw_context_mcp.indexer import _embedding
    from fw_context_mcp.llm.ollama import OllamaEmbedder

    monkeypatch.setattr(_embedding, "get_embedder", lambda cfg: OllamaEmbedder.__new__(OllamaEmbedder))

    def _refused(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(_embedding.httpx, "get", _refused)

    with pytest.raises(_embedding.EmbeddingsIncomplete, match="is not reachable"):
        _embedding._build_embeddings(None, "h", LLMConfig(), tmp_path)


def test_a_run_with_ollama_down_ends_and_stamps_partial(tmp_path, monkeypatch):
    """The refused connection is no OSError: it left the step, and the whole index run stopped."""
    import httpx

    from fw_context_mcp.config.settings import LLMConfig
    from fw_context_mcp.indexer import _embedding
    from fw_context_mcp.indexer.db import open_db
    from fw_context_mcp.indexer.runner import run
    from fw_context_mcp.llm.ollama import OllamaEmbedder

    monkeypatch.setattr(_embedding, "get_embedder", lambda cfg: OllamaEmbedder.__new__(OllamaEmbedder))

    def _refused(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(_embedding.httpx, "get", _refused)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "main.c").write_text("int main_value(void) { return 1; }\n", encoding="utf-8")
    cc = root / "compile_commands.json"
    cc.write_text(json.dumps([{"directory": str(root), "file": "main.c", "arguments": ["cc", "-c", "main.c"]}]),
                  encoding="utf-8")
    db_path = tmp_path / "index" / "pid" / "index.db"
    llm = LLMConfig()
    llm.enabled = True

    run(compile_commands=cc, db_path=db_path, project_root=root, project_id="pid", index_refs=False,
        index_embeddings=True, analyze_symbols=False, analyze_overrides=False, llm_config=llm)

    conn = open_db(db_path, skip_integrity_check=True)
    try:
        assert conn.execute("SELECT manifest_verification FROM build_configs").fetchone()[0] == "partial"
    finally:
        conn.close()


def test_failed_batches_are_reported_after_the_other_batches(tmp_path, monkeypatch):
    """A batch that failed after its retries was only logged: the build looked complete.

    Twelve functions make two batches (ten symbols at most in one).  The
    batch with ``fn_11`` fails; the other batch must be in the database.
    """
    from fw_context_mcp.config.settings import LLMConfig
    from fw_context_mcp.indexer import _embedding
    from fw_context_mcp.indexer.db import open_db

    source = "".join(f"int fn_{i:02d}(void) {{ return {i}; }}\n" for i in range(12))
    db_path, _ = _run(tmp_path, monkeypatch, fail_step=None, source=source)

    class _FailsOneBatch:
        name = "fake-embedder"
        max_tokens = 512

        def embed_documents(self, texts):
            if any("fn_11" in text for text in texts):
                raise RuntimeError("model gone")
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(_embedding, "get_embedder", lambda cfg: _FailsOneBatch())
    monkeypatch.setattr(_embedding.time, "sleep", lambda seconds: None)
    conn = open_db(db_path, skip_integrity_check=True)
    try:
        config_hash = conn.execute("SELECT config_hash FROM build_configs").fetchone()[0]
        with pytest.raises(_embedding.EmbeddingsIncomplete, match=r"1 embedding batch\(es\) failed \(model gone\)"):
            _embedding._build_embeddings(conn, config_hash, LLMConfig(), db_path.parent)
        names = {row[0] for row in conn.execute(
            "SELECT s.name FROM embeddings e JOIN symbols s ON s.id = e.symbol_id")}
    finally:
        conn.close()

    assert "fn_11" not in names, names
    assert names, "the batch that did not fail is written"


def test_the_project_names_each_kind_of_build_apart(tmp_path):
    """The text of a partial build could take the place of the text of a build without a manifest."""
    from fw_context_mcp.mcp.handlers.maintenance import get_project_status
    from tests.test_index_state import _project, _set_manifest

    root, db_dir = _project(tmp_path)
    _set_manifest(db_dir, "partial", "a")
    _set_manifest(db_dir, "none", "b")

    warning = get_project_status(project_root=str(root))["_warning"]

    assert "Builds without full header tracking: b." in warning, warning
    assert "STOP ALL" in warning, "the text of the build without a manifest stays"
    assert "Builds whose embeddings are not complete: a." in warning


def test_the_message_of_a_project_names_a_partial_build(tmp_path):
    """A partial build is current: "fully up to date" stood next to a `_warning` about it."""
    from fw_context_mcp.mcp.handlers.maintenance import get_project_status
    from tests.test_index_state import _project, _set_manifest

    root, db_dir = _project(tmp_path)
    _set_manifest(db_dir, "partial", "a")

    state = get_project_status(project_root=str(root))

    assert "fully up to date" not in state["index_message"], state["index_message"]
    assert "The embeddings of a are not complete: read _warning." in state["index_message"]
    assert "_warning" in state

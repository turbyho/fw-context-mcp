"""An LLM analysis that is not complete gets advice, and only when the config asks for the analysis.

Decision of the user (2026-10-18): only advice, no reason to reindex and no
run of the daemon, because the LLM can need hours.  Before, the status gave
``complete: false`` and the counts, but no action.  The analysis can be off
in the config: then nothing asks for it.
"""

from __future__ import annotations

import pytest

from tests.test_index_state import _project, _schedule

COVERAGE = {
    "project": {"total": 10, "analyzed": 7, "skipped": 1},
    "vendor": {"total": 50, "analyzed": 0, "skipped": 0},
}


def _message(tmp_path, monkeypatch, llm: str, *, scheduled: bool = False, row_format: str = "") -> str:
    from fw_context_mcp.indexer.db import open_db
    from fw_context_mcp.mcp.handlers import maintenance

    root, db_dir = _project(tmp_path)
    (root / ".fw-context" / "local.toml").write_text(llm, encoding="utf-8")
    monkeypatch.setattr(maintenance, "compute_analysis_coverage", lambda conn, config_hash: COVERAGE)
    if row_format:
        conn = open_db(db_dir / "index.db")
        try:
            conn.execute("UPDATE build_configs SET row_format = ? WHERE variant = 'a'", (row_format,))
            conn.commit()
        finally:
            conn.close()
    if scheduled:
        _schedule(db_dir)
    return maintenance.get_build_info(project_root=str(root), variant="a")["index_message"]


def test_a_waiting_analysis_names_the_command(tmp_path, monkeypatch):
    message = _message(tmp_path, monkeypatch, "[llm]\nenabled = true\nanalyze_symbols = true\n")

    assert "2 symbol(s) wait for the analysis: run `fw-context index` with the approval of the user" in message, message


@pytest.mark.parametrize("llm", [
    "[llm]\nenabled = true\nanalyze_symbols = false\n",
    "[llm]\nenabled = false\nanalyze_symbols = true\n",
])
def test_no_advice_when_the_config_turns_the_analysis_off(tmp_path, monkeypatch, llm):
    message = _message(tmp_path, monkeypatch, llm)

    assert "wait for the analysis" not in message, message


def test_an_index_run_does_the_analysis(tmp_path, monkeypatch):
    message = _message(tmp_path, monkeypatch, "[llm]\nenabled = true\nanalyze_symbols = true\n", scheduled=True)

    assert "the index run in progress or scheduled does it" in message, message
    assert "wait for the analysis: run" not in message, message


def test_vendor_symbols_count_only_with_analyze_vendor(tmp_path, monkeypatch):
    """With analyze_vendor false the 50 vendor symbols are skipped by design."""
    message = _message(tmp_path, monkeypatch, "[llm]\nenabled = true\nanalyze_symbols = true\n")

    assert "52 symbol(s)" not in message and "2 symbol(s)" in message, message


ON = "[llm]\nenabled = true\nanalyze_symbols = true\n"


def test_the_advice_names_check_ollama(tmp_path, monkeypatch):
    """Without a running LLM the run skips the analysis, and the same advice came after each run."""
    assert "when the number does not go down after the run, call check_ollama" in _message(tmp_path, monkeypatch, ON)


def test_a_reindex_reason_names_its_run(tmp_path, monkeypatch):
    """A bare `fw-context index` stood next to the action of a reason."""
    message = _message(tmp_path, monkeypatch, ON, row_format="fw-context-rows/1")

    assert "the index run that the action above names does it" in message, message
    assert "wait for the analysis: run `fw-context index`" not in message, message


def test_a_newer_row_format_gives_no_advice(tmp_path, monkeypatch):
    """The client must restart, and no run helps."""
    message = _message(tmp_path, monkeypatch, ON, row_format="fw-context-rows/999")

    assert "wait for the analysis" not in message, message


def test_the_vendor_flag_of_the_config_counts(tmp_path, monkeypatch):
    """The run reads analyze_vendor of the config: with it the 50 vendor symbols wait too."""
    message = _message(tmp_path, monkeypatch, ON + "analyze_vendor = true\n")

    assert "52 symbol(s) (with the vendor symbols: [llm] analyze_vendor is now true) wait" in message, message


def _project_message(tmp_path, monkeypatch, llm: str, *, scheduled: bool = False) -> str:
    """The ``index_message`` of get_project_status for the project of the two builds ``a`` and ``b``."""
    from fw_context_mcp.mcp.handlers import maintenance

    root, db_dir = _project(tmp_path)
    (root / ".fw-context" / "local.toml").write_text(llm, encoding="utf-8")
    monkeypatch.setattr(maintenance, "compute_analysis_coverage", lambda conn, config_hash: COVERAGE)
    if scheduled:
        _schedule(db_dir)
    return maintenance.get_project_status(project_root=str(root))["index_message"]


def test_a_project_of_several_builds_names_each_waiting_build(tmp_path, monkeypatch):
    """Before: the status of a project of several builds gave no advice, only "fully up to date".

    No sum over the builds: a file that two builds compile is in the index
    of each, thus the count of each build stands alone.
    """
    message = _project_message(tmp_path, monkeypatch, ON)

    assert (
        "Symbols wait for the LLM analysis in 2 of 2 builds (a: 2, b: 2): run `fw-context index` "
        "with the approval of the user" in message
    ), message
    assert "when the numbers do not go down after the run, call check_ollama" in message, message


def test_a_project_of_several_builds_without_the_analysis_gets_no_advice(tmp_path, monkeypatch):
    message = _project_message(tmp_path, monkeypatch, "[llm]\nenabled = true\nanalyze_symbols = false\n")

    assert "wait for the LLM analysis" not in message, message


def test_a_project_of_several_builds_waits_for_its_run(tmp_path, monkeypatch):
    message = _project_message(tmp_path, monkeypatch, ON, scheduled=True)

    assert "in 2 of 2 builds (a: 2, b: 2), and the index run in progress or scheduled does it" in message, message
    assert "wait for the LLM analysis in 2 of 2 builds (a: 2, b: 2): run" not in message, message


def test_a_reason_of_another_build_names_its_run(tmp_path, monkeypatch):
    """Build ``b`` has a reason and no waiting symbol, build ``a`` waits.

    One index run indexes each build, thus the advice for ``a`` names the
    run of the reason.  Before: a bare `fw-context index` stood next to the
    action of the reason of ``b``.
    """
    from fw_context_mcp.indexer.db import open_db
    from fw_context_mcp.mcp.handlers import maintenance

    root, db_dir = _project(tmp_path)
    (root / ".fw-context" / "local.toml").write_text(ON, encoding="utf-8")
    done = {"project": {"total": 10, "analyzed": 10, "skipped": 0}, "vendor": {"total": 0, "analyzed": 0, "skipped": 0}}
    monkeypatch.setattr(
        maintenance, "compute_analysis_coverage",
        lambda conn, config_hash: COVERAGE if config_hash == "h-a" else done,
    )
    conn = open_db(db_dir / "index.db")
    try:
        conn.execute("UPDATE build_configs SET row_format = ? WHERE variant = 'b'", ("fw-context-rows/1",))
        conn.commit()
    finally:
        conn.close()

    message = maintenance.get_project_status(project_root=str(root))["index_message"]

    assert "in 1 of 2 builds (a: 2), and the index run that the action above names does it" in message, message
    assert "wait for the LLM analysis in 1 of 2 builds (a: 2): run" not in message, message


def test_partial_embeddings_are_no_other_run(tmp_path, monkeypatch):
    """The warning of "partial" is advice: the message named no run, and the analysis advice said "the action above"."""
    from fw_context_mcp.mcp.handlers import maintenance
    from tests.test_index_state import _set_manifest

    root, db_dir = _project(tmp_path)
    (root / ".fw-context" / "local.toml").write_text(ON, encoding="utf-8")
    monkeypatch.setattr(maintenance, "compute_analysis_coverage", lambda conn, config_hash: COVERAGE)
    _set_manifest(db_dir, "partial")

    message = maintenance.get_build_info(project_root=str(root), variant="a")["index_message"]

    assert "the action above" not in message, message
    assert "wait for the analysis: run `fw-context index`" in message, message

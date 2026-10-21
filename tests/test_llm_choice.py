"""The LLM profile of one run (``--llm``, the question at the start), the usage of the run, and the models of the analyses.

No request leaves the test: the Ollama lists are replaced, and no chat
request is sent.  The keyring of the session fails each call (conftest).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from fw_context_mcp.cli import _llm_choice as choice
from fw_context_mcp.config import settings
from fw_context_mcp.llm import _diag, secrets
from fw_context_mcp.llm.client import Usage

GLOBAL = (
    '[llm]\nprofile = "local"\nenabled = true\nembed_model = "e:1b"\n\n'
    '[llm.profiles.local]\nprovider = "ollama"\nurl = "http://localhost:11434"\nmodel = "chat:1b"\n\n'
    '[llm.profiles.remote]\nprovider = "ollama"\nurl = "http://gpu-box:11434"\nmodel = "big:30b"\n\n'
    '[llm.profiles.or]\nprovider = "openrouter"\nmodel = "m"\n'
)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch):
    from fw_context_mcp.config import load

    path = tmp_path / "config.toml"
    path.write_text(GLOBAL, encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", path)
    monkeypatch.setenv("FW_CONTEXT_HOME", str(tmp_path))
    monkeypatch.setattr(secrets, "_found", {})
    monkeypatch.setattr(secrets, "_told", set())
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: {})
    settings._config_cache.clear()
    return load(None)


def _terminal(monkeypatch, answer: str | None) -> list[str]:
    """A terminal on stdin and stdout; *answer* for the question (None: no question may come)."""
    asked: list[str] = []

    def _input(prompt):
        if answer is None:
            pytest.fail("no question may come")
        asked.append(prompt)
        return answer

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(choice, "_in_foreground", lambda: True)
    monkeypatch.setattr(choice, "_ask", _input)
    return asked


def test_llm_selects_the_chat_of_the_run_and_keeps_the_embeddings(cfg, monkeypatch):
    _terminal(monkeypatch, None)

    run = choice.llm_for_run(argparse.Namespace(llm="remote"), cfg, background=False, analyses=True)

    assert (run.llm.profile, run.llm.chat_api_base) == ("remote", "http://gpu-box:11434")
    assert (run.llm.ollama_url, run.llm.embed_model) == (cfg.llm.ollama_url, cfg.llm.embed_model)
    assert cfg.llm.profile == "local", "the config of load() is shared: the run gets a copy"


def test_llm_with_an_unknown_profile_stops_the_run(cfg):
    with pytest.raises(choice.RunProfileError, match="profiles: local, remote, or"):
        choice.llm_for_run(argparse.Namespace(llm="nope"), cfg, background=False, analyses=True)


def test_llm_with_a_profile_without_a_key_stops_the_run(cfg):
    """No silent switch to another profile: the source code would go elsewhere."""
    with pytest.raises(choice.RunProfileError, match="fw-context llm key set or"):
        choice.llm_for_run(argparse.Namespace(llm="or"), cfg, background=True, analyses=True)


def test_the_question_offers_the_profiles_that_can_run_and_a_number_selects_one(cfg, monkeypatch, capsys):
    asked = _terminal(monkeypatch, "2")

    run = choice.llm_for_run(argparse.Namespace(llm=None), cfg, background=False, analyses=True, count="7 definitions")

    out = capsys.readouterr().out
    assert asked and "7 definitions" in out and "1. local: ollama chat:1b — Enter" in out, out
    assert "or:" not in out, "a profile without a key is not offered"
    assert run.llm.profile == "remote"


def test_enter_keeps_the_profile_of_the_project(cfg, monkeypatch):
    _terminal(monkeypatch, "")

    assert choice.llm_for_run(argparse.Namespace(llm=None), cfg, background=False, analyses=True) is cfg


@pytest.mark.parametrize(("background", "analyses", "tty"), [
    (True, True, True),    # the daemon: its stdin can be a terminal
    (False, False, True),  # a run without the LLM analysis
    (False, True, False),  # a script or an LLM agent
])
def test_no_question_without_a_reason(cfg, monkeypatch, background, analyses, tty):
    _terminal(monkeypatch, None)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty)

    assert choice.llm_for_run(argparse.Namespace(llm=None), cfg, background=background, analyses=analyses) is cfg


def test_no_question_with_one_profile_that_can_run(cfg, monkeypatch):
    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL.split("\n[llm.profiles.remote]")[0], encoding="utf-8")
    _terminal(monkeypatch, None)

    assert choice.llm_for_run(argparse.Namespace(llm=None), cfg, background=False, analyses=True) is cfg


def test_the_count_line_has_only_facts_of_the_index(monkeypatch):
    from fw_context_mcp.indexer import _llm_analysis

    monkeypatch.setattr(_llm_analysis, "count_analysis_candidates", lambda conn, config_hash, project_only: (3, 2))

    assert choice.analysis_count_line(None, [{"config_hash": "a"}], project_only=True, force=False) == (
        "3 definitions, 2 of them without an analysis")
    assert "up to 3 definitions" in choice.analysis_count_line(None, [{"config_hash": "a"}], project_only=True,
                                                               force=True)


def test_the_count_of_the_index_matches_the_rows_of_the_run(populated_db, tmp_path):
    """COUNT and the rows of the run use one filter; COUNT does not read the bodies."""
    from fw_context_mcp.indexer._llm_analysis import _select_unanalyzed_symbols, count_analysis_candidates
    from fw_context_mcp.indexer.db import transaction, upsert_file
    from fw_context_mcp.indexer.db._symbols import insert_symbols_batch

    config_hash = "hash-deadbeef"  # the build of the fixture
    source = tmp_path / "work.c"
    source.write_text("// file\n", encoding="utf-8")
    with transaction(populated_db):
        file_id = upsert_file(populated_db, config_hash, str(source), "c")
        insert_symbols_batch(populated_db, [
            (config_hash, file_id, str(source), name, f"usr-{name}", name, name, "function", 1, 1, 3, 1,
             f"void {name}(void)", "", None, 0, 0, "", 0, "", 1, 0.0, "{}", 0)
            for name in ("c1", "c2", "c3")
        ])
    rows = _select_unanalyzed_symbols(populated_db, config_hash, True)

    assert count_analysis_candidates(populated_db, config_hash, True) == (
        len(rows), sum(1 for row in rows if row["existing_hash"] is None))


@pytest.mark.parametrize(("analyze_symbols", "flag", "expected"), [
    (True, None, True),
    (False, "force_llm", True),   # the main use of another profile
    (False, "analyze", True),
    (True, "no_analyze", False),
])
def test_an_index_run_asks_when_it_analyses(cfg, analyze_symbols, flag, expected):
    from dataclasses import replace

    run_cfg = replace(cfg, llm=replace(cfg.llm, analyze_symbols=analyze_symbols))
    args = argparse.Namespace(**{"force_llm": False, "analyze": False, "no_analyze": False, **({flag: True} if flag else {})})

    assert choice.index_analyses(args, run_cfg) is expected


def test_llm_without_an_analysis_has_no_effect(cfg, capsys):
    """A profile without a key stopped a run that sends nothing."""
    run = choice.llm_for_run(argparse.Namespace(llm="or"), cfg, background=False, analyses=False)

    assert run is cfg
    assert "has no effect" in capsys.readouterr().err


def test_a_value_of_a_profile_that_cannot_be_used_stops_the_run_with_a_sentence(cfg):
    """load skips a wrong value of a profile that it does not select: before, the switch raised a ValueError."""
    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL.replace('model = "big:30b"', 'model = "big:30b"\ntimeout = "slow"'),
                                            encoding="utf-8")

    with pytest.raises(choice.RunProfileError, match="fw-context doctor"):
        choice.llm_for_run(argparse.Namespace(llm="remote"), cfg, background=False, analyses=True)


def test_a_background_job_of_the_shell_is_not_asked(cfg, monkeypatch):
    """A run started with & stopped at the read of the terminal (SIGTTIN)."""
    _terminal(monkeypatch, None)
    monkeypatch.setattr(choice, "_in_foreground", lambda: False)

    assert choice.llm_for_run(argparse.Namespace(llm=None), cfg, background=False, analyses=True) is cfg


def test_no_answer_keeps_the_profile_of_the_project(cfg, monkeypatch, capsys):
    """An LLM agent in a pseudo terminal never answers: the run goes on after the timeout."""
    import select

    real_ask = choice._ask
    _terminal(monkeypatch, None)
    monkeypatch.setattr(choice, "_ask", real_ask)
    monkeypatch.setattr(select, "select", lambda r, w, x, timeout: ([], [], []))

    assert choice.llm_for_run(argparse.Namespace(llm=None), cfg, background=False, analyses=True) is cfg
    assert "No answer in 60 s" in capsys.readouterr().out


# ── The usage of the run ──


def test_the_usage_line_tells_only_what_the_provider_reports():
    from fw_context_mcp.indexer._llm_analysis import _Spent

    spent = _Spent()
    spent.add(Usage(prompt_tokens=100, cached_tokens=40, completion_tokens=20, cost=0.002))
    spent.add(Usage(prompt_tokens=50, completion_tokens=10))

    line = spent.line(12.4)
    assert line == ("2 requests, 150 input tokens (40 from the cache of the provider), 30 output tokens, "
                    "cost 0.0020 USD (reported by the provider), 12 s")
    no_cost = _Spent()
    no_cost.add(Usage(prompt_tokens=5, completion_tokens=1))
    assert "cost" not in no_cost.line(1.0), "no estimate when the provider reports no cost"
    streamed = _Spent()
    streamed.add(Usage())
    assert streamed.line(1.0) == "1 requests, usage not reported, 1 s", "no zero for a value that was not reported"


# ── The models of the analyses ──


def test_the_status_counts_the_analyses_of_each_model(populated_db, tmp_path):
    from fw_context_mcp.indexer.db import analysis_models, make_analysis_summary, transaction, upsert_file
    from fw_context_mcp.indexer.db._symbols import insert_symbols_batch

    config_hash = "hash-deadbeef"  # the build of the fixture
    source = tmp_path / "work.c"
    source.write_text("// file\n", encoding="utf-8")
    with transaction(populated_db):
        file_id = upsert_file(populated_db, config_hash, str(source), "c")
        insert_symbols_batch(populated_db, [
            (config_hash, file_id, str(source), name, f"usr-{name}", name, name, "function", 1, 1, 3, 1,
             f"void {name}(void)", "", None, 0, 0, "", 0, "", 1, 0.0, "{}", 0)
            for name in ("f1", "f2", "f3", "f4")
        ])
    rows = populated_db.execute("SELECT id FROM symbols WHERE config_hash = ? ORDER BY name", (config_hash,)).fetchall()
    for row, model in zip(rows, ["a:1b", "a:1b", "b:2b", "skip:unparseable:a:1b"], strict=True):
        populated_db.execute(
            "INSERT OR REPLACE INTO llm_analysis (symbol_id, summary, inputs, outputs, model, content_hash) "
            "VALUES (?, 's', 'i', 'o', ?, 'h')", (row["id"], model))

    models = analysis_models(populated_db, config_hash)

    assert models == {"a:1b": 2, "b:2b": 1}
    summary = make_analysis_summary({"project": {"total": 0, "analyzed": 0, "skipped": 0},
                                     "vendor": {"total": 0, "analyzed": 0, "skipped": 0}}, False, models)
    assert (summary["model"], summary["models"]) == ("a:1b", {"a:1b": 2, "b:2b": 1})

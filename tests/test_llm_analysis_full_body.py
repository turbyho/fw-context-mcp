"""The LLM analysis sends the full body, counts the tokens, and has --force-llm.

Before:

* A body over 5000 characters went to the model as its first 60 lines.
  The summary told what those lines do: for a ``main`` of 42000 characters
  the start-up, and not the main loop.
* The budget came from ``len(prompt) / 2.5`` and from the trained context
  of ``/api/tags``, while each request had ``num_ctx`` of the config, and
  the server cut a longer prompt without an error.
* ``fw-context index --force`` deleted the analyses of the build, and the
  run read each one back from the local cache: no analysis was made again.

No Ollama: the model, the token count and the contexts are stand-ins.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fw_context_mcp.cache_client import get_local_cache_db, local_cache_lookup, local_cache_upsert
from fw_context_mcp.indexer import _llm_analysis as mod
from fw_context_mcp.indexer._llm_analysis import _build_llm_analysis
from fw_context_mcp.indexer.db import transaction, upsert_file
from fw_context_mcp.indexer.db._symbols import insert_symbols_batch
from fw_context_mcp.llm.client import ChatResult
from fw_context_mcp.utils import write_text_atomic

CONFIG_HASH = "hash-deadbeef"
RUN_CTX, LARGEST_CTX = 3000, 16000


class _Model:
    """Stands in for the chat client: one token for each 10 characters of a prompt.

    A test changes ``answer`` (the text of a request) or ``count`` (the
    token count) to make a request or a count fail.
    """

    counts_tokens = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None, int | None]] = []
        self.answer = self.reply
        self.count = lambda prompt, num_ctx: self.tokens(prompt)

    @staticmethod
    def tokens(prompt: str) -> int:
        return len(prompt) // 10

    def reply(self, prompt, *, temperature=0.0, max_tokens=None, num_ctx=None):
        self.calls.append((prompt, num_ctx, max_tokens))
        symbol_id = int(prompt.split("(id: ")[1].split(")")[0])
        return json.dumps([{"id": symbol_id, "summary": f"summary {len(self.calls)}", "inputs": "x", "outputs": "y"}])

    def generate(self, prompt, *, temperature=0.0, max_tokens=None, num_ctx=None) -> ChatResult:
        return ChatResult(self.answer(prompt, temperature=temperature, max_tokens=max_tokens, num_ctx=num_ctx))

    def count_tokens(self, prompt, num_ctx):
        return self.count(prompt, num_ctx)

    def trained_context(self):
        return LARGEST_CTX

    def pull_model(self):
        raise AssertionError("the run must not pull the model")

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "_analysis_contexts", lambda client, cfg: (RUN_CTX, LARGEST_CTX))
        monkeypatch.setattr(mod, "get_chat_client", lambda cfg: self)


class _Server:
    """Stands in for an Ollama chat client for ``_analysis_contexts``: its trained contexts, in order."""

    counts_tokens = True

    def __init__(self, *trained: int | None) -> None:
        self.trained = list(trained)
        self.pulled = 0

    def trained_context(self):
        return self.trained.pop(0)

    def pull_model(self):
        self.pulled += 1


class _Config:
    model = "test-model"


def _body(name: str, lines: int) -> str:
    return f"void {name}(void) {{\n" + "".join(f"    step_{i}();\n" for i in range(lines)) + f"    last_{name}();\n}}\n"


def _seed(conn, tmp_path: Path, bodies: dict[str, str]) -> dict[str, int]:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    f = src / "work.c"
    f.write_text("// file\n", encoding="utf-8")
    with transaction(conn):
        file_id = upsert_file(conn, CONFIG_HASH, str(f), "c")
        insert_symbols_batch(conn, [
            (CONFIG_HASH, file_id, str(f), name, f"usr-{name}", name, name, "function", 1, 1,
             body.count("\n"), 1, f"void {name}(void)", "", None, 0, 0, "", 0, "", 1, 0.0, body, 0)
            for name, body in bodies.items()
        ])
    return {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM symbols WHERE config_hash = ?",
                                                     (CONFIG_HASH,))}


def _run(conn, tmp_path: Path, **kwargs) -> None:
    _build_llm_analysis(conn, CONFIG_HASH, _Config(), tmp_path, project_root=tmp_path,
                        write_lock_held=True, **kwargs)


def _models(conn) -> dict[str, str]:
    return {r["name"]: r["model"] for r in conn.execute(
        "SELECT s.name, a.model FROM llm_analysis a JOIN symbols s ON s.id = a.symbol_id")}


def test_a_long_body_goes_to_the_model_in_full(populated_db, monkeypatch, tmp_path: Path):
    model = _Model()
    model.install(monkeypatch)
    _seed(populated_db, tmp_path, {"long_one": _body("long_one", 400)})  # about 6000 characters

    _run(populated_db, tmp_path)

    prompt = model.calls[0][0]
    assert "last_long_one();" in prompt, "the end of the body must reach the model"
    assert "truncated for analysis" not in prompt


def test_a_prompt_that_does_not_fit_waits_for_one_larger_context(populated_db, monkeypatch, tmp_path: Path):
    model = _Model()
    model.install(monkeypatch)
    _seed(populated_db, tmp_path, {
        "middle": _body("middle", 1500),  # about 2500 tokens: no fit with the answer of 1024
        "short": _body("short", 5),
        "large": _body("large", 4000),  # about 6500 tokens
    })

    _run(populated_db, tmp_path)

    order = [p.split("] ")[1].split(" ")[0] for p, _, _ in model.calls]
    assert order == ["short", "middle", "large"], "the long prompts go last, the shortest first"
    contexts = [ctx for _, ctx, _ in model.calls]
    large_ctx = model.tokens(model.calls[2][0]) + mod._ANSWER_TOKENS
    assert contexts == [RUN_CTX, large_ctx, large_ctx], "one context for the second pass: one load of the model"
    # Before, the largest prompt of the second pass had 300 tokens for its answer.
    assert [max_tokens for _, _, max_tokens in model.calls] == [mod._ANSWER_TOKENS] * 3


def test_a_prompt_larger_than_the_model_gets_the_sentinel(populated_db, monkeypatch, tmp_path: Path):
    model = _Model()
    model.install(monkeypatch)
    _seed(populated_db, tmp_path, {"huge": _body("huge", 10000)})  # about 16000 tokens

    _run(populated_db, tmp_path)

    assert model.calls == []
    assert _models(populated_db) == {"huge": f"skip:toolarge:{LARGEST_CTX}"}


def _cached_summaries(hashes: list[str]) -> dict[str, str]:
    db = get_local_cache_db()
    try:
        return {h: entry["summary"] for h, entry in local_cache_lookup(db, hashes).items() if entry}
    finally:
        db.close()


def test_force_llm_makes_each_analysis_again_and_replaces_the_cache(populated_db, monkeypatch, tmp_path: Path):
    model = _Model()
    model.install(monkeypatch)
    # A name of its own: the local cache is one for the session of the tests.
    _seed(populated_db, tmp_path, {"force_again": _body("force_again", 5)})
    _run(populated_db, tmp_path)
    _run(populated_db, tmp_path)
    assert len(model.calls) == 1, "the precondition: an unchanged symbol is not analysed again"
    stored_hash = populated_db.execute("SELECT content_hash FROM llm_analysis").fetchone()[0]

    _run(populated_db, tmp_path, force_since="9999-01-01 00:00:00")

    assert len(model.calls) == 2
    assert _cached_summaries([stored_hash]) == {stored_hash: "summary 2"}, "the new analysis replaces the entry"


def test_force_llm_takes_an_analysis_of_the_same_command(populated_db, monkeypatch, tmp_path: Path):
    """A symbol of two builds gets one analysis in one command."""
    model = _Model()
    model.install(monkeypatch)
    _seed(populated_db, tmp_path, {"force_same_command": _body("force_same_command", 5)})
    _run(populated_db, tmp_path)

    _run(populated_db, tmp_path, force_since="2000-01-01 00:00:00")

    assert len(model.calls) == 1


def test_local_cache_upsert_keeps_the_first_entry_unless_it_overwrites():
    db = get_local_cache_db()
    try:
        entry = {"hash": "h-overwrite", "summary": "old", "inputs": "", "outputs": "", "model": "m"}
        local_cache_upsert(db, [entry])
        local_cache_upsert(db, [{**entry, "summary": "new"}])
        assert local_cache_lookup(db, ["h-overwrite"])["h-overwrite"]["summary"] == "old"
        local_cache_upsert(db, [{**entry, "summary": "new"}], overwrite=True)
        assert local_cache_lookup(db, ["h-overwrite"])["h-overwrite"]["summary"] == "new"
    finally:
        db.close()


# ── The contexts of the run ──


def test_the_run_has_the_context_of_the_config_and_the_trained_one_as_the_limit(monkeypatch):
    from fw_context_mcp.config.settings import LLMConfig

    assert mod._analysis_contexts(_Server(32768), LLMConfig(num_ctx=16384)) == (16384, 32768)
    assert mod._analysis_contexts(_Server(32768), LLMConfig(num_ctx=65536)) == (32768, 32768)


def test_a_server_that_does_not_answer_gives_no_analysis():
    from fw_context_mcp.config.settings import LLMConfig

    assert mod._analysis_contexts(_Server(None), LLMConfig()) is None


def test_an_openai_endpoint_has_the_context_of_the_config():
    from fw_context_mcp.config.settings import LLMConfig

    assert mod._analysis_contexts(SimpleNamespace(counts_tokens=False), LLMConfig(num_ctx=8192)) == (8192, 8192)


# ── The CLI ──


def _index_args(**flags: bool) -> SimpleNamespace:
    """The arguments of ``fw-context index`` that the code under test reads."""
    return SimpleNamespace(**{"force": False, "force_llm": False, "no_analyze": False, "analyze": False,
                              "name": None, "no_refs": False, "verbose": False, **flags})


def test_force_llm_and_no_analyze_stop_the_command(capsys):
    from fw_context_mcp.cli._index import cmd_index

    assert cmd_index(_index_args(force_llm=True, no_analyze=True)) == 2
    assert "--force-llm" in capsys.readouterr().err


@pytest.mark.parametrize(("flags", "analyses"), [({"force": True}, False), ({"force_llm": True}, True)])
def test_force_llm_turns_on_the_analysis(flags, analyses, tmp_path: Path):
    from fw_context_mcp.cli._index import _build_run_kwargs
    from fw_context_mcp.config.settings import Config

    cfg = Config()
    cfg.llm.analyze_symbols = False
    kwargs = _build_run_kwargs(_index_args(**flags), cfg, tmp_path, "p", [], [], None)
    assert kwargs["analyze_symbols"] is analyses


@pytest.mark.parametrize(("flags", "overwrite"), [({"force": True}, False), ({"force_llm": True}, True)])
def test_only_force_llm_overwrites_the_cache_server(flags, overwrite):
    from fw_context_mcp.cli._index import _cache_server_config
    from fw_context_mcp.config.settings import CacheServerConfig, Config

    cfg = Config()
    cfg.cache_server = CacheServerConfig(url="https://cache.example.test", token="t")
    assert _cache_server_config(_index_args(**flags), cfg).force is overwrite


def test_a_stopped_force_llm_continues_from_the_time_of_the_first_command(tmp_path: Path):
    from fw_context_mcp.indexer import _force_llm_marker as marker

    first, continues = marker.since_for(tmp_path, "model-a")
    assert not continues and (tmp_path / marker.MARKER).is_file()
    assert marker.since_for(tmp_path, "model-a") == (first, True), "the next command keeps the time"
    assert marker.finish(tmp_path) is True
    assert not (tmp_path / marker.MARKER).exists()


def test_a_marker_of_another_model_does_not_continue(tmp_path: Path):
    """The content hash does not hold the model: a continued command took the analyses of the old one."""
    from fw_context_mcp.indexer import _force_llm_marker as marker

    write_text_atomic(tmp_path / marker.MARKER, json.dumps({"since": "2000-01-01 00:00:00", "model": "a"}))
    since, continues = marker.since_for(tmp_path, "b")
    assert not continues and since != "2000-01-01 00:00:00"


def test_an_unfinished_step_keeps_the_marker_for_the_next_command(tmp_path: Path):
    from fw_context_mcp.indexer import _force_llm_marker as marker

    since, _ = marker.since_for(tmp_path, "m")
    marker.mark_unfinished(tmp_path)
    assert marker.finish(tmp_path) is False, "Ollama stopped during the command: the work so far must stay"
    assert marker.since_for(tmp_path, "m") == (since, True)
    assert marker.finish(tmp_path) is True, "the next command starts with the flag cleared"


def test_the_step_marks_a_forced_command_unfinished(populated_db, monkeypatch, tmp_path: Path):
    from fw_context_mcp.indexer import _force_llm_marker as marker
    from fw_context_mcp.indexer import _postprocess

    marker.since_for(tmp_path, "m")
    monkeypatch.setattr(_postprocess, "_build_llm_analysis", lambda *a, **kw: False)
    ctx = {"config_hash": CONFIG_HASH, "llm_config": _Config(), "cache_server_config": None, "force": False,
           "force_llm_since": "2026-10-19 12:00:00", "db_dir": tmp_path, "project_root": tmp_path,
           "analyze_vendor": False}

    _postprocess._step_llm_analysis(populated_db, ctx)

    assert marker.finish(tmp_path) is False


def test_a_symbol_without_an_answer_makes_the_analysis_unfinished(populated_db, monkeypatch, tmp_path: Path):
    from fw_context_mcp.llm.ollama import OllamaError

    model = _Model()
    model.install(monkeypatch)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)

    def down(*a, **kw):
        raise OllamaError("Ollama stopped")

    model.answer = down
    _seed(populated_db, tmp_path, {"no_answer": _body("no_answer", 5)})

    assert _build_llm_analysis(populated_db, CONFIG_HASH, _Config(), tmp_path, project_root=tmp_path,
                               write_lock_held=True) is False


def test_a_forced_run_takes_no_cache_entry_of_another_model(populated_db, monkeypatch, tmp_path: Path):
    model = _Model()
    model.install(monkeypatch)
    _seed(populated_db, tmp_path, {"other_model": _body("other_model", 5)})
    _run(populated_db, tmp_path)
    stored_hash = populated_db.execute("SELECT content_hash FROM llm_analysis").fetchone()[0]
    db = get_local_cache_db()
    try:
        local_cache_upsert(db, [{"hash": stored_hash, "summary": "of model b", "inputs": "", "outputs": "",
                                 "model": "model-b"}], overwrite=True)
    finally:
        db.close()

    _run(populated_db, tmp_path, force_since="2000-01-01 00:00:00")

    assert len(model.calls) == 2, "the entry of another model must not stand for a new analysis"


def test_a_count_that_fails_three_times_leaves_the_symbol_for_the_next_run(populated_db, monkeypatch,
                                                                          tmp_path: Path):
    from fw_context_mcp.llm.ollama import OllamaError

    model = _Model()
    model.install(monkeypatch)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    calls = []

    def count(prompt, num_ctx):
        calls.append(prompt)
        raise OllamaError("timeout")

    model.count = count
    _seed(populated_db, tmp_path, {"never_counted": _body("never_counted", 5)})

    assert _build_llm_analysis(populated_db, CONFIG_HASH, _Config(), tmp_path, project_root=tmp_path,
                               write_lock_held=True) is False
    assert len(calls) == 3 and model.calls == []


@pytest.mark.parametrize("force_llm_since", [None, "2026-10-19 12:00:00"])
def test_the_step_deletes_no_analysis_first(populated_db, monkeypatch, tmp_path: Path, force_llm_since):
    """Before, a stopped --force-llm left the build without the analyses that it did not reach."""
    from fw_context_mcp.indexer import _postprocess
    from fw_context_mcp.indexer.db import upsert_llm_analysis_batch

    ids = _seed(populated_db, tmp_path, {"kept_or_not": _body("kept_or_not", 5)})
    with transaction(populated_db):
        upsert_llm_analysis_batch(populated_db, [(ids["kept_or_not"], "s", "i", "o", "m", "h")])
    monkeypatch.setattr(_postprocess, "_build_llm_analysis", lambda *a, **kw: None)
    ctx = {"config_hash": CONFIG_HASH, "llm_config": _Config(), "cache_server_config": None, "force": True,
           "force_llm_since": force_llm_since, "db_dir": tmp_path, "project_root": tmp_path,
           "analyze_vendor": False}

    _postprocess._step_llm_analysis(populated_db, ctx)

    assert _models(populated_db) == {"kept_or_not": "m"}


def test_an_openai_endpoint_gets_no_count_and_the_answer_budget(populated_db, monkeypatch, tmp_path: Path):
    model = _Model()
    model.install(monkeypatch)
    model.counts_tokens = False
    model.count = lambda *a: pytest.fail("no count for an OpenAI endpoint")
    _seed(populated_db, tmp_path, {"openai_symbol": _body("openai_symbol", 1500)})

    _run(populated_db, tmp_path)

    assert [max_tokens for _, _, max_tokens in model.calls] == [mod._ANSWER_TOKENS]


def test_a_prompt_that_the_server_refuses_goes_to_the_second_pass(populated_db, monkeypatch, tmp_path: Path):
    from fw_context_mcp.llm.ollama import OllamaPromptTooLong

    model = _Model()
    model.install(monkeypatch)
    model.count = lambda prompt, num_ctx: 10  # a wrong count
    real_call = model.reply

    def call(prompt, **kwargs):
        if kwargs["num_ctx"] == RUN_CTX:
            raise OllamaPromptTooLong(5000, RUN_CTX)
        return real_call(prompt, **kwargs)

    model.answer = call
    _seed(populated_db, tmp_path, {"refused_symbol": _body("refused_symbol", 5)})

    _run(populated_db, tmp_path)

    assert [ctx for _, ctx, _ in model.calls] == [5000 + mod._ANSWER_TOKENS]


def test_a_failed_count_goes_again(populated_db, monkeypatch, tmp_path: Path):
    from fw_context_mcp.llm.ollama import OllamaError

    model = _Model()
    model.install(monkeypatch)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    failures = [OllamaError("timeout"), OllamaError("timeout")]

    def count(prompt, num_ctx):
        if failures:
            raise failures.pop()
        return 10

    model.count = count
    _seed(populated_db, tmp_path, {"recount_symbol": _body("recount_symbol", 5)})

    _run(populated_db, tmp_path)

    assert len(model.calls) == 1


def test_a_forced_run_does_not_read_the_cache_server(populated_db, monkeypatch, tmp_path: Path):
    model = _Model()
    model.install(monkeypatch)
    _seed(populated_db, tmp_path, {"remote_symbol": _body("remote_symbol", 5)})

    class _Server:
        writes: list = []

        def batch_get(self, hashes):
            raise AssertionError("a forced run must not read the cache server")

        def batch_put(self, entries):
            self.writes.extend(entries)

    server = _Server()
    _run(populated_db, tmp_path, cache_client=server, force_since="9999-01-01 00:00:00")

    assert len(model.calls) == 1
    assert len(server.writes) == 1


def test_a_missing_model_is_pulled_when_auto_pull_is_on():
    from fw_context_mcp.config.settings import LLMConfig

    server = _Server(None, 32768)

    assert mod._analysis_contexts(server, LLMConfig(num_ctx=16384, auto_pull=True)) == (16384, 32768)
    assert server.pulled == 1


def test_a_refusal_of_each_request_stops_the_run_after_one_symbol(populated_db, monkeypatch, tmp_path: Path):
    """A usage limit of a plan: each next symbol failed the same way, three times, with a pause of 2 s each."""
    from fw_context_mcp.llm.errors import LLMStopRun

    model = _Model()

    def _limit(prompt, **kwargs):
        model.calls.append((prompt, None, None))
        raise LLMStopRun("usage limit")

    model.answer = _limit
    model.install(monkeypatch)
    _seed(populated_db, tmp_path, {f"fn_{i}": _body(f"fn_{i}", 3) for i in range(5)})

    assert _build_llm_analysis(populated_db, CONFIG_HASH, _Config(), tmp_path, project_root=tmp_path,
                               write_lock_held=True) is False

    assert len(model.calls) == 1, "the run must stop at the first refusal, without a retry"
    assert _models(populated_db) == {}, "no symbol gets a sentinel: a later run continues"


def test_the_embedding_model_is_released_once_before_the_first_request(populated_db, monkeypatch, tmp_path: Path):
    """Lazy: a run whose analyses come from the cache keeps the embedding model for the next query."""
    from fw_context_mcp.llm import ollama

    phases: list[str] = []
    monkeypatch.setattr(ollama, "release_other_model", lambda cfg, phase: phases.append(phase))
    model = _Model()
    model.install(monkeypatch)
    _seed(populated_db, tmp_path, {f"fn_{i}": _body(f"fn_{i}", 3) for i in range(3)})

    calls: list[str] = []
    original = model.reply

    def _reply(prompt, **kwargs):
        calls.append("generate")
        return original(prompt, **kwargs)

    model.answer = _reply
    count = model.count
    model.count = lambda prompt, num_ctx: calls.append("count") or count(prompt, num_ctx)
    monkeypatch.setattr(ollama, "release_other_model", lambda cfg, phase: calls.append(f"release:{phase}"))

    _run(populated_db, tmp_path)
    assert calls[0] == "release:analysis" and calls.count("release:analysis") == 1, calls

    calls.clear()
    _run(populated_db, tmp_path)  # each analysis is current: no request
    assert calls == []


@pytest.fixture
def choice_on(monkeypatch):
    """The session turns the choice off (conftest): these tests turn it on, with a client that selects 6144."""
    from fw_context_mcp.llm import ollama
    from fw_context_mcp.llm.client import ContextFit

    monkeypatch.delenv(ollama.NO_RELEASE_ENV, raising=False)
    monkeypatch.setattr(ollama, "release_other_model", lambda cfg, phase: None)
    model = _Model()
    model.asked = []
    model.fit_context = lambda lower, upper: model.asked.append((lower, upper)) or ContextFit(6144, 1.0, "context 6144")
    model.counted = []
    count = model.count
    model.count = lambda prompt, num_ctx: model.counted.append(num_ctx) or count(prompt, num_ctx)
    model.install(monkeypatch)
    # A context of the run above the lower bound of 4096: the search goes from 4096.
    monkeypatch.setattr(mod, "_analysis_contexts", lambda client, cfg: (8000, LARGEST_CTX))
    return model


def _seed_new(conn, tmp_path: Path, prefix: str) -> None:
    """Symbols of their own: the LLM cache of the session holds the analyses of the names of other tests."""
    _seed(conn, tmp_path, {f"{prefix}_{i}": _body(f"{prefix}_{i}", 3) for i in range(3)})


def test_the_first_pass_uses_the_context_that_the_free_gpu_memory_selects(populated_db, tmp_path: Path, choice_on):
    _seed_new(populated_db, tmp_path, "fit_first")

    _run(populated_db, tmp_path, context_choice=mod.ContextChoice())

    assert choice_on.asked == [(4096, 8000)], "one choice for the run, from the lower bound to num_ctx"
    assert {num_ctx for _prompt, num_ctx, _max in choice_on.calls} == {6144}
    assert set(choice_on.counted) == {6144}, "the count of the tokens uses the selected context too"


def test_the_builds_of_one_run_share_one_choice(populated_db, tmp_path: Path, choice_on):
    """`analyze` and an index run call the analysis for each build: nothing that the search measures changes."""
    choice = mod.ContextChoice()
    _seed_new(populated_db, tmp_path, "fit_app")
    _run(populated_db, tmp_path, context_choice=choice)
    _seed_new(populated_db, tmp_path, "fit_boot")

    _run(populated_db, tmp_path, context_choice=choice)

    assert choice_on.asked == [(4096, 8000)]
    assert {num_ctx for _prompt, num_ctx, _max in choice_on.calls} == {6144}


def test_a_run_whose_analyses_come_from_the_cache_selects_nothing(populated_db, tmp_path: Path, choice_on):
    _seed_new(populated_db, tmp_path, "fit_cached")
    _run(populated_db, tmp_path, context_choice=mod.ContextChoice())
    choice_on.asked.clear()

    _run(populated_db, tmp_path, context_choice=mod.ContextChoice())  # each analysis is current: no request

    assert choice_on.asked == []


def test_a_run_without_a_choice_uses_num_ctx(populated_db, tmp_path: Path, choice_on):
    """The background reindex and reindex_file: few symbols, thus no search of 2 to 6 loads."""
    _seed_new(populated_db, tmp_path, "fit_none")

    _run(populated_db, tmp_path)

    assert choice_on.asked == []
    assert {num_ctx for _prompt, num_ctx, _max in choice_on.calls} == {8000}

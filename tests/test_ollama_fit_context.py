"""The context of an LLM analysis from the free GPU memory (``llm.ollama.fit_context``).

A fake Ollama: ``/api/tags`` gives the weights, ``/api/generate`` loads
the model with a context, and ``/api/ps`` gives ``size`` and
``size_vram`` for the loaded context.  No test reaches a real Ollama.
"""

from __future__ import annotations

import httpx
import pytest

from fw_context_mcp.llm import ollama

URL = "http://localhost:11434"
GIB = 1024 ** 3


def _ollama(monkeypatch, *, weights: int | None, report, show: dict | None = None,
            name: str = "m:1b") -> list[dict]:
    """A fake server; *report* maps a context to (size, size_vram), or to None for no entry in /api/ps.

    Returns the body of each load request.
    """
    loads: list[dict] = []

    def _get(url, timeout):
        request = httpx.Request("GET", url)
        if url.endswith("/api/tags"):
            models = [] if weights is None else [{"name": name, "size": weights}]
            return httpx.Response(200, json={"models": models}, request=request)
        found = report(loads[-1]["options"]["num_ctx"])
        models = [] if found is None else [{"name": name, "size": found[0], "size_vram": found[1]}]
        return httpx.Response(200, json={"models": models}, request=request)

    def _post(url, json, timeout):
        request = httpx.Request("POST", url)
        if url.endswith("/api/show"):
            return httpx.Response(200, json=show or {}, request=request)
        loads.append({**json, "timeout": timeout})
        return httpx.Response(200, json={"done": True}, request=request)

    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(httpx, "post", _post)
    return loads


def _contexts(loads: list[dict]) -> list[int]:
    return [load["options"]["num_ctx"] for load in loads]


def _fit(lower: int = 4096, upper: int = 16384, model: str = "m:1b"):
    return ollama.fit_context(URL, model, lower=lower, upper=upper, keep_alive="1m", timeout=77.0)


def _dense(fits_up_to: int, fail_at: int | None = None):
    """A dense model of 8 GiB: all on the GPU up to *fits_up_to*, partly above it; no report at *fail_at*."""
    def report(ctx: int) -> tuple[int, int] | None:
        if ctx == fail_at:
            return None
        size = 8 * GIB + ctx * 100_000
        return (size, size) if ctx <= fits_up_to else (size, int(size * 0.9))
    return report


def test_the_upper_bound_when_it_fits(monkeypatch):
    loads = _ollama(monkeypatch, weights=8 * GIB, report=_dense(16384))

    fit = _fit()

    assert (fit.num_ctx, fit.gpu_share) == (16384, 1.0) and _contexts(loads) == [4096, 16384]


def test_each_load_has_the_keep_alive_and_the_timeout_of_the_profile(monkeypatch):
    loads = _ollama(monkeypatch, weights=8 * GIB, report=_dense(16384))

    _fit()

    assert {(load["keep_alive"], load["timeout"]) for load in loads} == {("1m", 77.0)}


def test_the_bisection_finds_the_largest_context_on_the_gpu(monkeypatch):
    loads = _ollama(monkeypatch, weights=8 * GIB, report=_dense(9216))

    fit = _fit()

    assert (fit.num_ctx, fit.gpu_share) == (9216, 1.0)
    assert len(loads) <= 7 and all(ctx % 1024 == 0 for ctx in _contexts(loads))


@pytest.mark.parametrize(("upper", "fits_up_to", "expected"), [
    (10000, 9500, 9216),  # the review: the search stopped at 8192
    (5500, 5200, 5120),   # the review: the search stopped at 4096
    (10000, 10000, 10000),
], ids=["step below upper", "one step above lower", "upper itself"])
def test_an_upper_bound_that_is_not_a_step_keeps_each_step_below_it(monkeypatch, upper, fits_up_to, expected):
    """A trained context below num_ctx (10000) is the upper bound: each multiple of 1024 below it is a candidate."""
    _ollama(monkeypatch, weights=8 * GIB, report=_dense(fits_up_to))

    assert _fit(upper=upper).num_ctx == expected


@pytest.mark.parametrize(("fail_at", "expected"), [(16384, 4096), (13312, 10240)],
                         ids=["at upper", "in the bisection"])
def test_a_failed_load_stops_at_the_largest_context_that_fits(monkeypatch, fail_at, expected):
    """A failure is not a measurement: it does not count as "does not fit", and the log says what happened."""
    _ollama(monkeypatch, weights=8 * GIB, report=_dense(12288, fail_at=fail_at))

    fit = _fit()

    assert (fit.num_ctx, fit.gpu_share) == (expected, 1.0)
    assert f"the load at context {fail_at} failed" in fit.reason


def test_a_model_that_does_not_fit_at_the_lower_bound_runs_partly_from_ram(monkeypatch):
    """Decision of the user: memory is not a wall."""
    _ollama(monkeypatch, weights=8 * GIB, report=lambda ctx: (10 * GIB, 8 * GIB))

    fit = _fit()

    assert (fit.num_ctx, fit.gpu_share) == (4096, 0.8)
    assert "runs from RAM" in fit.reason


@pytest.mark.parametrize(("show", "kind"), [
    ({"capabilities": ["completion", "vision"]}, "a multimodal model"),
    ({"model_info": {"qwen3moe.expert_count": 128}}, "a MoE model"),
    ({"model_info": ["not", "a", "table"]}, "a model of another kind"),
])
def test_a_report_smaller_than_the_weights_keeps_the_upper_bound(monkeypatch, show, kind):
    """Ollama 0.40.7: a multimodal model or the RAM experts of a MoE model are not in size."""
    loads = _ollama(monkeypatch, weights=17 * GIB, report=lambda ctx: (8 * GIB, 8 * GIB), show=show)

    fit = _fit()

    assert (fit.num_ctx, fit.gpu_share) == (16384, None) and _contexts(loads) == [4096]
    assert kind in fit.reason


def test_a_model_name_without_a_tag_is_the_latest_tag(monkeypatch):
    """Ollama lists `m` as `m:latest` in /api/tags and /api/ps."""
    _ollama(monkeypatch, weights=8 * GIB, report=_dense(16384), name="m:latest")

    assert _fit(model="m").num_ctx == 16384


def test_no_weights_load_nothing(monkeypatch):
    """Without the weights the report cannot be used: a load would only cost time."""
    loads = _ollama(monkeypatch, weights=None, report=_dense(16384))

    fit = _fit()

    assert (fit.num_ctx, fit.gpu_share) == (16384, None) and loads == []


def test_a_server_that_does_not_answer_keeps_the_upper_bound(monkeypatch):
    def _down(*args, **kwargs):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", _down)
    monkeypatch.setattr(httpx, "post", _down)

    fit = _fit()

    assert (fit.num_ctx, fit.gpu_share) == (16384, None)


def test_no_search_when_the_upper_bound_is_the_lower_one(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("no load"))

    assert _fit(lower=4096, upper=4096).num_ctx == 4096


def test_an_api_client_selects_no_context():
    from fw_context_mcp.config.settings import LLMConfig
    from fw_context_mcp.llm.chat_router import OpenAIChatClient

    client = OpenAIChatClient(LLMConfig(model="m", chat_api_base="https://api.example.test/v1",
                                        chat_api_format="openai"), "https://api.example.test/v1/chat/completions")

    assert client.fit_context(4096, 16384) is None


def test_a_model_with_no_part_on_a_gpu_keeps_the_upper_bound(monkeypatch):
    """A machine without a GPU: the model runs from RAM at each context, thus a smaller one is not faster."""
    loads = _ollama(monkeypatch, weights=8 * GIB, report=lambda ctx: (9 * GIB, 0))

    fit = _fit()

    assert (fit.num_ctx, fit.gpu_share) == (16384, 0.0) and _contexts(loads) == [4096]
    assert "no part of the model is on a GPU" in fit.reason


def test_the_client_gives_the_keep_alive_and_the_timeout_of_its_profile(monkeypatch):
    from fw_context_mcp.config.settings import LLMConfig

    seen: list[dict] = []
    monkeypatch.setattr(ollama, "note_chat_use", lambda url, model: None)
    monkeypatch.setattr(ollama, "fit_context", lambda url, model, **kwargs: seen.append(kwargs))
    client = ollama.OllamaClient(LLMConfig(model="m:1b", keep_alive="5m", timeout=1234),
                                 URL + "/api/generate", URL)

    client.fit_context(4096, 16384)

    assert seen == [{"lower": 4096, "upper": 16384, "keep_alive": "5m", "timeout": 1234}]


def test_each_load_goes_through_the_guard_of_the_requests(monkeypatch):
    """max_concurrent: a load is a request of Ollama as each request of the analysis."""
    from contextlib import contextmanager

    held: list[bool] = []
    inside = False

    @contextmanager
    def _guard():
        nonlocal inside
        inside = True
        try:
            yield
        finally:
            inside = False

    _ollama(monkeypatch, weights=8 * GIB, report=_dense(9216))
    fake_post = httpx.post
    monkeypatch.setattr(httpx, "post", lambda url, json, timeout: held.append(inside) or fake_post(url, json, timeout))
    monkeypatch.setattr(ollama, "ollama_guard", _guard)

    _fit()

    assert held and all(held)

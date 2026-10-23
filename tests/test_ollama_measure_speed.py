"""The measurement of the speed of a model and the key of its calibration (``llm.ollama``).

A fake Ollama: ``/api/generate`` answers with the times of llama.cpp,
``/api/tags`` and ``/api/version`` describe the server.  No test reaches a
real Ollama.
"""

from __future__ import annotations

from contextlib import contextmanager

import httpx
import pytest

from fw_context_mcp.config.settings import LLMConfig
from fw_context_mcp.llm import hardware, ollama
from fw_context_mcp.llm.calibration import Key

URL = "http://localhost:11434"
TIMES = {"prompt_eval_count": 1700, "prompt_eval_duration": 1_000_000_000, "eval_count": 64,
         "eval_duration": 2_000_000_000}


def _server(monkeypatch, *, answer=lambda request: (200, TIMES), tags=None, version="0.40.7") -> list[dict]:
    """A fake server; *answer* maps the body of a generate request to (status, JSON or bytes)."""
    sent: list[dict] = []

    def _post(url, json, timeout):
        sent.append({**json, "timeout": timeout})
        status, body = answer(json)
        request = httpx.Request("POST", url)
        if isinstance(body, bytes):
            return httpx.Response(status, content=body, request=request)
        return httpx.Response(status, json=body, request=request)

    def _get(url, timeout):
        request = httpx.Request("GET", url)
        if url.endswith("/api/version"):
            return httpx.Response(200, json={"version": version}, request=request)
        models = tags if tags is not None else [{"name": "m:1b", "digest": "abc"}]
        return httpx.Response(200, json={"models": models}, request=request)

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(httpx, "get", _get)
    return sent


def _measure(num_ctx: int = 8192, stream: bool = False):
    return ollama.measure_speed(URL, "m:1b", num_ctx, keep_alive="5m", timeout=77.0, stream=stream)


def test_the_speed_comes_from_the_times_of_llama_cpp(monkeypatch):
    _server(monkeypatch)

    speed = _measure().speed

    assert (speed.prompt_tokens, speed.prompt_tps, speed.eval_tps) == (1700, 1700.0, 32.0)


def test_a_request_of_the_same_prompt_loads_the_model_before_the_reference_request(monkeypatch):
    """Measured 2026-10-21: the first request after a load was 8 times slower; a short one misses MoE experts."""
    sent = _server(monkeypatch)

    _measure(8192)

    assert [request["options"]["num_predict"] for request in sent] == [1, ollama._REFERENCE_ANSWER_TOKENS]
    assert all(request["prompt"].endswith(ollama._REFERENCE_PROMPT) for request in sent)
    assert {request["options"]["num_ctx"] for request in sent} == {8192}
    assert {(request["keep_alive"], request["timeout"], request["truncate"], request["think"])
            for request in sent} == {("5m", 77.0, False, False)}


def test_each_reference_request_has_a_new_first_line(monkeypatch):
    """The prompt cache of the runner would else make the prompt of a second measurement free."""
    sent = _server(monkeypatch)

    _measure()
    _measure()

    first_lines = {request["prompt"].split("\n", 1)[0] for request in sent}
    assert len(first_lines) == 4


@pytest.mark.parametrize("body", [{"error": "cudaMalloc failed: out of memory"},
                                  b"<html>CUDA error: out of memory</html>",
                                  {"error": "unable to load full model on GPU"}],
                         ids=["json", "not json", "too large for the machine"])
def test_an_error_of_memory_is_a_measurement(monkeypatch, body):
    _server(monkeypatch, answer=lambda request: (500, body))

    result = _measure()

    assert result.speed is None and result.out_of_memory


def test_another_error_is_a_failure(monkeypatch):
    _server(monkeypatch, answer=lambda request: (500, {"error": "llama runner process has terminated: signal: killed"}))

    result = _measure()

    assert result.speed is None and not result.out_of_memory and "runner process has terminated" in result.error


def test_a_streamed_answer_gives_the_times_of_its_last_line(monkeypatch):
    """The stream of the profile: a proxy can cut a slow answer that does not stream."""
    import json

    lines = b"\n".join(json.dumps(part).encode() for part in ({"response": "{"}, {"done": True, **TIMES}))
    sent = _server(monkeypatch, answer=lambda request: (200, lines if request["stream"] else TIMES))

    assert _measure(stream=True).speed.prompt_tokens == 1700 and {r["stream"] for r in sent} == {True}


def test_an_answer_without_times_is_a_failure(monkeypatch):
    _server(monkeypatch, answer=lambda request: (200, {"response": "OK"}))

    assert _measure().error == "Ollama gives no times of the request"


def test_a_server_that_does_not_answer_is_a_failure(monkeypatch):
    def _down(*args, **kwargs):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "post", _down)

    result = _measure()

    assert result.speed is None and not result.out_of_memory and "ConnectError" in result.error


def test_each_request_goes_through_the_guard_of_the_requests(monkeypatch):
    """max_concurrent: a measurement is a request of Ollama as each request of the analysis."""
    inside: list[bool] = []
    state = {"held": False}

    @contextmanager
    def _guard():
        state["held"] = True
        try:
            yield
        finally:
            state["held"] = False

    _server(monkeypatch, answer=lambda request: inside.append(state["held"]) or (200, TIMES))
    monkeypatch.setattr(ollama, "ollama_guard", _guard)

    _measure()

    assert inside == [True, True]


# ── The key of a calibration ────────────────────────────────────────────


def test_the_key_names_the_model_file_ollama_and_the_memory_of_this_machine(monkeypatch):
    _server(monkeypatch)
    monkeypatch.setattr(hardware, "machine_memory", lambda: hardware.Memory(gpu_mib=12282, ram_mib=63488))

    assert ollama.calibration_key(URL, "m:1b", 4096, 16384) == Key(
        server=URL, model="m:1b", digest="abc", ollama="0.40.7", gpu_mib=12282, ram_mib=63488, lower=4096,
        upper=16384)


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434", "http://[::1]:11434"])
def test_the_loopback_addresses_are_this_machine(monkeypatch, url):
    _server(monkeypatch)
    monkeypatch.setattr(hardware, "machine_memory", lambda: hardware.Memory(gpu_mib=12282, ram_mib=63488))

    assert ollama.calibration_key(url, "m:1b", 4096, 16384).gpu_mib == 12282


def test_the_memory_of_this_machine_is_not_part_of_the_key_of_another_server(monkeypatch):
    _server(monkeypatch)
    monkeypatch.setattr(hardware, "machine_memory", lambda: pytest.fail("not this machine"))

    key = ollama.calibration_key("http://gpu-box:11434", "m:1b", 4096, 16384)

    assert (key.gpu_mib, key.ram_mib) == (None, None)


def test_a_cloud_model_has_no_calibration(monkeypatch):
    """It runs on another computer, and a measurement would send the reference request out."""
    _server(monkeypatch, tags=[{"name": "m:1b", "digest": "abc", "remote_host": "https://ollama.com"}])

    assert ollama.calibration_key(URL, "m:1b", 4096, 16384) is None


def test_a_model_without_a_tag_is_the_latest_tag(monkeypatch):
    _server(monkeypatch, tags=[{"name": "m:latest", "digest": "abc"}])
    monkeypatch.setattr(hardware, "machine_memory", lambda: hardware.Memory(gpu_mib=None, ram_mib=None))

    assert ollama.calibration_key(URL, "m", 4096, 16384).model == "m:latest"


def test_a_model_that_the_server_does_not_list_has_no_calibration(monkeypatch):
    _server(monkeypatch, tags=[])

    assert ollama.calibration_key(URL, "m:1b", 4096, 16384) is None


# ── The clients ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("check", [True, False])
def test_the_ollama_client_measures_with_its_profile(monkeypatch, check):
    seen: list[tuple] = []
    key = Key(URL, "m:1b", "abc", "0.40.7", None, None, 4096, 16384)
    monkeypatch.setattr(ollama, "calibration_key", lambda url, model, lower, upper: key)
    monkeypatch.setattr(ollama, "note_chat_use", lambda url, model: seen.append(("used", model)))
    monkeypatch.setattr(ollama, "measure_speed", lambda url, model, ctx, **kw: seen.append(("measure", ctx, kw)))

    def _select(measure, k, *, check):
        measure(k.lower)
        seen.append(("select", k, check))

    monkeypatch.setattr(ollama, "select_context", _select)
    client = ollama.OllamaClient(LLMConfig(model="m:1b", keep_alive="5m", timeout=1234, stream=True),
                                 URL + "/api/generate", URL)

    client.fit_context(4096, 16384, check=check)

    assert ("measure", 4096, {"keep_alive": "5m", "timeout": 1234, "stream": True}) in seen
    assert ("select", key, check) in seen
    assert (("used", "m:1b") in seen) is check, "only a run that loads the model unloads it later"


def test_the_ollama_client_without_a_key_selects_nothing(monkeypatch):
    monkeypatch.setattr(ollama, "calibration_key", lambda url, model, lower, upper: None)
    monkeypatch.setattr(ollama, "select_context", lambda *a, **k: pytest.fail("no selection"))
    client = ollama.OllamaClient(LLMConfig(model="m:1b"), URL + "/api/generate", URL)

    assert client.fit_context(4096, 16384, check=True) is None


def test_an_api_client_selects_no_context():
    from fw_context_mcp.llm.chat_router import OpenAIChatClient

    client = OpenAIChatClient(LLMConfig(model="m", chat_api_base="https://api.example.test/v1",
                                        chat_api_format="openai"), "https://api.example.test/v1/chat/completions")

    assert client.fit_context(4096, 16384, check=True) is None


def test_no_key_when_the_upper_bound_is_not_above_the_lower_one(monkeypatch):
    """The run uses the upper bound: two requests and nvidia-smi would cost time for nothing."""
    monkeypatch.setattr(ollama, "calibration_key", lambda *a: pytest.fail("no key"))
    client = ollama.OllamaClient(LLMConfig(model="m:1b"), URL + "/api/generate", URL)

    assert client.fit_context(4096, 4096, check=False) is None


def test_a_stream_that_ends_with_an_error_gives_the_error(monkeypatch):
    """A stream that fails after its first part: HTTP 200 and an error line."""
    import json

    lines = b"\n".join(json.dumps(part).encode() for part in ({"response": "{"}, {"error": "out of memory"}))
    _server(monkeypatch, answer=lambda request: (200, lines))

    result = _measure(stream=True)

    assert result.speed is None and result.error == "Ollama: out of memory" and result.out_of_memory

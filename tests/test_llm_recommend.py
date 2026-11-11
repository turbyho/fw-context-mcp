"""The recommendation of a local model (``llm.recommend``), its stored speeds, the check and the table.

The speeds of the rule tests are the speeds of this machine of the
benchmark (Ryzen 9 9900X, RTX 4070 SUPER 12 GB, measured 2026-10-21 at a
context of 4096).  No test reaches a real Ollama: the list of the
installed models, the key and the measurements are replaced.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import httpx
import pytest

from fw_context_mcp import net
from fw_context_mcp.config import settings
from fw_context_mcp.llm import _diag, calibration, ollama, recommend
from fw_context_mcp.llm.calibration import EmbedMeasure, Key, Measure, Speed
from fw_context_mcp.llm.hardware import Memory
from fw_context_mcp.llm.recommend import CHAT, EMBEDDING, Candidate, Fit

URL = "http://localhost:11434"
MACHINE = Memory(gpu_mib=12282, ram_mib=63896)


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch) -> Path:
    """Each test has its own global config and calibration file."""
    path = tmp_path / "config.toml"
    path.write_text("[llm]\n", encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", path)
    monkeypatch.setenv("FW_CONTEXT_HOME", str(tmp_path))
    settings._config_cache.clear()
    return tmp_path


def _measured(tag: str, quality: float, seconds: float) -> Candidate:
    return Candidate(tag, quality, True, seconds_per_symbol=seconds)


#: The chat models of the list with their time on this machine (2026-10-21).
MEASURED = [
    _measured("gemma4:31b", 3.80, 70.35), _measured("gemma4:26b", 3.73, 6.27),
    _measured("qwen3.5:27b", 3.70, 54.36), _measured("phi4:14b", 3.67, 12.88),
    _measured("qwen3-coder:30b", 3.63, 7.88), _measured("qwen2.5-coder:14b", 3.53, 12.35),
    _measured("gemma4:12b", 3.50, 3.74), _measured("qwen3:8b", 3.00, 4.04),
    _measured("gemma4:e2b", 2.80, 1.10),
]


# ── The rule of two bands ──


def test_the_measured_speeds_of_this_machine_give_gemma4_26b():
    """The band of the quality (>= 3.60): 31b, 26b, 27b, phi4, qwen3-coder; 26b is the fastest, and the best in its band."""
    assert recommend.choose(MEASURED, CHAT).tag == "gemma4:26b"


def test_a_model_of_the_same_quality_and_much_faster_wins():
    assert recommend.choose([_measured("slow", 3.80, 40.0), _measured("fast", 3.65, 5.0)], CHAT).tag == "fast"


def test_a_better_model_of_the_same_speed_wins():
    """A model that is 20 % slower is as fast: the quality decides."""
    assert recommend.choose([_measured("fast", 3.55, 5.0), _measured("better", 3.70, 6.25)], CHAT).tag == "better"


def test_a_model_just_outside_the_band_of_the_speed_loses():
    assert recommend.choose([_measured("fast", 3.55, 5.0), _measured("better", 3.70, 6.3)], CHAT).tag == "fast"


def test_the_edge_of_the_band_of_the_quality_is_in_the_band():
    """3.73 - 0.2 is 3.5300000000000002 in binary: a quality of 3.53 is still in the band."""
    assert recommend.choose([_measured("best", 3.73, 20.0), _measured("edge", 3.53, 5.0)], CHAT).tag == "edge"


def test_the_tolerance_of_the_embeddings_is_smaller():
    """0.691 against 0.619 is a difference for the embeddings (0.03), not the same quality."""
    models = [_measured("qwen3-embedding:4b", 0.691, 0.045), _measured("qwen3-embedding:0.6b", 0.619, 0.011),
              _measured("qwen3-embedding:8b", 0.682, 0.072)]

    assert recommend.choose(models, EMBEDDING).tag == "qwen3-embedding:4b"


def test_no_candidate_with_a_cost_gives_none():
    assert recommend.choose([Candidate("x", 3.0, False)], CHAT) is None


def test_an_estimate_prefers_the_gpu_then_moe_then_the_weights():
    candidates = [
        Candidate("dense", 3.80, False, fit=Fit.DENSE_IN_RAM, weights_gb=19.0),
        Candidate("moe", 3.73, False, fit=Fit.MOE_IN_RAM, weights_gb=18.0),
        Candidate("small", 3.50, False, fit=Fit.GPU, weights_gb=8.0),
    ]

    assert recommend.choose(candidates, CHAT).tag == "moe"
    assert recommend.choose([*candidates, Candidate("gpu", 3.65, False, fit=Fit.GPU, weights_gb=9.0)],
                            CHAT).tag == "gpu"


# ── The estimate ──


@pytest.mark.parametrize(("weights", "moe", "memory", "fit"), [
    (8.0, False, MACHINE, Fit.GPU),
    (18.0, True, MACHINE, Fit.MOE_IN_RAM),
    (19.0, False, MACHINE, Fit.DENSE_IN_RAM),
    (80.0, True, MACHINE, None),
    (5.2, False, Memory(gpu_mib=None, ram_mib=16 * 1024), Fit.DENSE_IN_RAM),
    (9.0, False, Memory(gpu_mib=None, ram_mib=16 * 1024), None),
    (8.0, False, Memory(gpu_mib=None, ram_mib=None), None),
    (18.0, True, Memory(gpu_mib=21845, ram_mib=32768, unified=True), Fit.GPU),
    (30.0, True, Memory(gpu_mib=21845, ram_mib=32768, unified=True), None),
    (None, False, MACHINE, None),
    (8.0, False, Memory(gpu_mib=12282, ram_mib=None), Fit.GPU),
    (18.0, True, Memory(gpu_mib=12282, ram_mib=None), None),
], ids=["fits the GPU", "MoE in RAM", "dense in RAM", "too large", "CPU 16 GiB", "CPU too small",
        "memory not known", "unified", "unified too large", "no weights", "GPU without RAM",
        "too large without RAM"])
def test_the_estimate_follows_the_memory(weights, moe, memory, fit):
    assert recommend.estimate(weights, moe, memory) == fit


@pytest.mark.parametrize(("gpu", "ram", "model"), [
    (12282, 64 * 1024, "gemma4:26b"),  # the driver reports a 12 GB card as 12282 MiB
    (8188, 32 * 1024, "gemma4:26b"),
    (4096, 64 * 1024, "gemma4:26b"),
    (None, 16 * 1024, "gemma4:12b"),  # 7629 MiB in 16 GiB less 8 GiB
    (None, None, None),
])
def test_the_estimate_of_the_list_of_this_release_follows_the_memory(monkeypatch, gpu, ram, model):
    """A machine without an installed model: the estimate from the weights of the list of this release.

    On 12 GB, the estimate gives the model that the measurement gives
    (``test_the_measured_speeds_of_this_machine_give_gemma4_26b``).
    """
    monkeypatch.setattr(_diag, "installed_models", lambda url: [])

    result = recommend.gather(CHAT, URL, Memory(gpu, ram), measure=False)

    assert (result.model.tag if result.model else None) == model


# ── The measured choice against the estimate ──


def test_the_measured_choice_beats_an_estimate_of_the_same_quality():
    candidates = [_measured("installed", 3.60, 5.0), Candidate("other", 3.70, False, fit=Fit.GPU, weights_gb=9.0)]

    result = recommend.recommend(CHAT, candidates)

    assert (result.model.tag, result.estimated) == ("installed", False)


def test_an_estimate_that_is_clearly_better_is_recommended():
    candidates = [_measured("installed", 2.80, 2.0), Candidate("other", 3.70, False, fit=Fit.GPU, weights_gb=9.0)]

    result = recommend.recommend(CHAT, candidates)

    assert (result.model.tag, result.estimated) == ("other", True)


def test_without_a_measurement_the_estimate_is_recommended():
    result = recommend.recommend(CHAT, [Candidate("other", 3.70, False, fit=Fit.GPU, weights_gb=9.0)])

    assert (result.model.tag, result.estimated) == ("other", True)


# ── verdict: the model of the config against the recommendation ──


def _result(candidates):
    return recommend.recommend(CHAT, candidates)


def test_a_better_installed_model_is_reported():
    better, text = recommend.verdict(_result(MEASURED), "qwen2.5-coder:14b")

    assert better and text.startswith("gemma4:26b (3.73, 6.3 s/symbol) is better than qwen2.5-coder:14b")


def test_a_model_in_the_two_bands_is_as_good():
    """The same quality, 7.5 s against 6.27 s (within 20 %)."""
    assert recommend.verdict(_result([*MEASURED, _measured("close:30b", 3.63, 7.5)]), "close:30b")[0] is False


def test_a_model_just_slower_than_the_band_is_not_as_good():
    """qwen3-coder:30b on this machine: 7.88 s is 0.5 % outside 6.27 s / 0.8."""
    assert recommend.verdict(_result(MEASURED), "qwen3-coder:30b")[0] is True


def test_the_verdict_uses_the_bands_of_the_rule_and_not_of_the_choice():
    """Q* 3.73, F 3.60 at 4.0 s, the choice P at 5.0 s: 6.2 s is in 20 % of P, but not of F.

    Before, the verdict compared with the choice: C was "as good as P"
    although the rule puts C outside the band of the speed.
    """
    result = _result([_measured("q", 3.73, 10.0), _measured("f", 3.60, 4.0), _measured("p", 3.70, 5.0),
                      _measured("c", 3.73, 6.2)])

    assert result.model.tag == "p"
    assert recommend.verdict(result, "c")[0] is True


def test_the_band_of_the_speed_has_an_epsilon():
    """2.4 / 0.8 is 2.9999999999999996 in binary: 3.0 is in the band."""
    found = recommend.bands([_measured("f", 3.0, 2.4), _measured("s", 3.0, 3.0)], CHAT)

    assert found.as_fast(_measured("s", 3.0, 3.0))


def test_the_recommended_model_is_named():
    assert recommend.verdict(_result(MEASURED), "gemma4:26b") == (
        False, "gemma4:26b is the recommended chat model (3.73, 6.3 s/symbol)")


def test_a_model_outside_the_list_is_not_compared():
    better, text = recommend.verdict(_result(MEASURED), "my-model:1b")

    assert not better and "my-model:1b is not a local model of the list" in text


def test_a_model_that_did_not_load_is_not_sent_to_a_new_measurement():
    candidates = [*MEASURED, Candidate("mine:2b", 3.00, True, note="does not fit in the memory: HTTP 500")]

    better, text = recommend.verdict(_result(candidates), "mine:2b")

    assert not better and "has no time (does not fit in the memory" in text and "llm recommend" not in text


def test_a_model_without_a_measurement_is_not_compared():
    candidates = [*MEASURED, Candidate("mine:2b", 3.00, True, fit=Fit.GPU, weights_gb=2.0, can_measure=True)]

    better, text = recommend.verdict(_result(candidates), "mine:2b")

    assert not better and "not measured" in text


def test_the_tag_latest_is_the_same_model():
    result = _result([_measured("nomic-embed-text:latest", 3.0, 1.0)])

    assert result.candidate("nomic-embed-text") is not None


# ── The stored speeds ──


def _key(model: str = "m:1b", lower: int = 4096, upper: int = 4096, **change) -> Key:
    values = {"server": URL, "model": model, "digest": "d1", "ollama": "0.40.7", "gpu_mib": 12282,
              "ram_mib": 63896, "lower": lower, "upper": upper, **change}
    return Key(**values)


def test_a_stored_speed_comes_back_for_its_role_only():
    calibration.store_speed("embedding", _key(lower=0, upper=0), 0.05)

    assert calibration.load_speed("embedding", _key(lower=0, upper=0)) == 0.05
    assert calibration.load_speed("chat", _key(lower=0, upper=0)) is None


def test_a_new_file_of_the_model_needs_a_new_measurement():
    calibration.store_speed("chat", _key(), 6.0)

    assert calibration.load_speed("chat", _key(digest="d2")) is None


def test_a_calibration_gives_the_speed_at_its_lower_bound():
    """The best speed of a calibration is its speed at 4096: no second measurement of the model of a profile."""
    best = Speed(prompt_tokens=1700, prompt_tps=1700.0, eval_tps=300.0)
    calibration.store(calibration.Calibration(_key(upper=16384), best, 8192, "2026-10-21T10:00:00", "0.0"))

    assert calibration.load_speed("chat", _key()) == pytest.approx(2.0)


def test_a_write_of_one_section_keeps_the_other(home):
    best = Speed(prompt_tokens=1700, prompt_tps=1700.0, eval_tps=300.0)
    calibration.store(calibration.Calibration(_key(upper=16384), best, 8192, "2026-10-21T10:00:00", "0.0"))
    calibration.store_speed("chat", _key(model="other:1b"), 9.0)
    calibration.store(calibration.Calibration(_key(model="x:1b", upper=16384), best, 4096, "2026-10-21", "0.0"))

    data = json.loads((home / "llm_calibration.json").read_text(encoding="utf-8"))

    assert [entry["key"]["model"] for entry in data["calibrations"]] == ["m:1b", "x:1b"]
    assert [entry["key"]["model"] for entry in data["speeds"]] == ["other:1b"]


def test_a_file_of_an_older_release_has_no_speeds(home):
    (home / "llm_calibration.json").write_text('{"format": 1, "calibrations": []}\n', encoding="utf-8")

    assert calibration.load_speed("embedding", _key(lower=0, upper=0)) is None


@pytest.mark.parametrize("value", [0, -1.0, True, "6", float("inf")])
def test_a_stored_speed_that_is_not_a_measurement_is_left_out(home, value):
    entry = {"role": "chat", "key": dataclasses.asdict(_key()), "seconds_per_symbol": value,
             "created": "2026-10-21", "fw_context": "0.0"}
    text = json.dumps({"format": 1, "calibrations": [], "speeds": [entry]}).replace("Infinity", "1e999")
    (home / "llm_calibration.json").write_text(text, encoding="utf-8")

    assert calibration.load_speed("chat", _key()) is None


# ── gather: the candidates of the list on a fake Ollama ──


class _Ollama:
    """The fake Ollama of ``gather``: installed models, a key for each, measurements, and the releases."""

    def __init__(self, monkeypatch, installed: list[str], seconds: dict[str, float | str] | None = None,
                 held: list[str] | None = None) -> None:
        self.measured: list[str] = []
        self.released: list[str] = []
        self.seconds = seconds or {}
        monkeypatch.setattr(_diag, "installed_models", lambda url: installed)
        monkeypatch.setattr(ollama, "calibration_key",
                            lambda url, model, lower, upper: _key(model=ollama._with_tag(model), lower=lower,
                                                                  upper=upper))
        self.held = list(held or [])
        monkeypatch.setattr(ollama, "loaded_models", lambda url: list(self.held))
        monkeypatch.setattr(ollama, "release_model", lambda url, model: self.released.append(model))
        monkeypatch.setattr(ollama, "measure_speed", self._chat)
        monkeypatch.setattr(ollama, "measure_embed_speed", self._embed)

    def _value(self, model: str):
        self.measured.append(model)
        value = self.seconds.get(model, 5.0)
        if isinstance(value, BaseException):
            raise value
        return value

    def _chat(self, url, model, num_ctx, *, keep_alive, timeout, stream=False):
        assert num_ctx == calibration.LOWEST_CONTEXT
        value = self._value(model)
        if value == "oom":
            return Measure(None, "HTTP 500: failed to allocate CUDA0 buffer", out_of_memory=True)
        if value == "fail":
            return Measure(None, "Ollama does not answer")
        return Measure(Speed(prompt_tokens=1, prompt_tps=1e12, eval_tps=calibration.TYPICAL_ANSWER_TOKENS / value))

    def _embed(self, url, model, *, keep_alive, timeout):
        value = self._value(model)
        if value == "oom":
            return EmbedMeasure(None, "HTTP 500: cudaMalloc failed: out of memory", out_of_memory=True)
        return EmbedMeasure(value)


LIST = '''[llm.models."big:30b"]
quality = 3.8
weights_gb = 18.0
moe = true

[llm.models."small:8b"]
quality = 3.0
weights_gb = 5.0

[llm.models."off:14b"]
quality = 3.9
weights_gb = 9.0
enabled = false

[llm.models."cloud:cloud"]
quality = 4.5
remote = true

[llm.models."new:12b"]
quality = 3.7
weights_gb = 8.0

[llm.embed_models."e:4b"]
quality = 0.69
weights_gb = 2.5
dimension = 2560
'''


@pytest.fixture
def listed(home):
    settings._GLOBAL_CONFIG_PATH.write_text("[llm]\n" + LIST, encoding="utf-8")


def test_the_candidates_leave_out_a_disabled_model_and_a_cloud_model(monkeypatch, listed):
    _Ollama(monkeypatch, ["big:30b", "small:8b"])

    result = recommend.gather(CHAT, URL, MACHINE, measure=False)

    assert sorted(c.tag for c in result.candidates) == ["big:30b", "new:12b", "small:8b"]


def test_without_a_measurement_no_model_loads(monkeypatch, listed):
    fake = _Ollama(monkeypatch, ["big:30b", "small:8b"])

    result = recommend.gather(CHAT, URL, MACHINE, measure=False)

    assert fake.measured == [] and fake.released == []
    assert {c.tag for c in result.candidates if c.can_measure} == {"big:30b", "small:8b"}


def test_a_measurement_is_stored_and_the_next_run_reads_it(monkeypatch, listed):
    fake = _Ollama(monkeypatch, ["big:30b", "small:8b"], {"big:30b": 6.0, "small:8b": 4.0})

    first = recommend.gather(CHAT, URL, MACHINE, measure=True)
    second = recommend.gather(CHAT, URL, MACHINE, measure=True)

    assert fake.measured == ["big:30b", "small:8b"]
    assert first.candidate("big:30b").seconds_per_symbol == pytest.approx(6.0)
    assert second.candidate("big:30b").seconds_per_symbol == pytest.approx(6.0)


def test_again_measures_each_model_again(monkeypatch, listed):
    fake = _Ollama(monkeypatch, ["big:30b"])
    recommend.gather(CHAT, URL, MACHINE, measure=True)

    recommend.gather(CHAT, URL, MACHINE, measure=True, again=True)

    assert fake.measured == ["big:30b", "big:30b"]


def test_a_model_that_the_server_held_before_stays_loaded(monkeypatch, listed):
    fake = _Ollama(monkeypatch, ["big:30b", "small:8b"], held=["small:8b"])

    recommend.gather(CHAT, URL, MACHINE, measure=True)

    assert fake.released == ["big:30b"]


def test_a_model_that_does_not_fit_gets_no_estimate(monkeypatch, listed):
    _Ollama(monkeypatch, ["big:30b"], {"big:30b": "oom"})

    candidate = recommend.gather(CHAT, URL, MACHINE, measure=True).candidate("big:30b")

    assert (candidate.seconds_per_symbol, candidate.fit, candidate.cost()) == (None, None, None)
    assert candidate.note.startswith("does not fit in the memory")


def test_a_failed_measurement_gives_no_estimate(monkeypatch, listed):
    """Else the estimate recommends the model that just failed to load (a runner that ended, a timeout)."""
    _Ollama(monkeypatch, ["big:30b", "small:8b"], {"big:30b": "fail", "small:8b": 4.0})

    result = recommend.gather(CHAT, URL, MACHINE, measure=True)

    assert result.candidate("big:30b").cost() is None
    assert result.candidate("big:30b").note.startswith("the measurement failed")
    assert result.model.tag != "big:30b"


def test_an_embedding_model_that_does_not_fit_gets_no_estimate(monkeypatch, listed):
    _Ollama(monkeypatch, ["e:4b"], {"e:4b": "oom"})

    candidate = recommend.gather(EMBEDDING, URL, MACHINE, measure=True).candidate("e:4b")

    assert candidate.cost() is None and candidate.note.startswith("does not fit in the memory")


def test_a_server_on_another_computer_is_not_measured(monkeypatch, listed):
    """A measurement there can push out the models of other users."""
    fake = _Ollama(monkeypatch, ["big:30b"])

    result = recommend.gather(CHAT, "http://gpu-box:11434", None, measure=True)

    assert fake.measured == [] and any("not on this machine" in note for note in result.notes)


def test_a_model_of_another_computer_is_not_offered_for_a_measurement(monkeypatch, listed):
    """Before: the setup asked "Measure ... now?", and a yes measured nothing."""
    _Ollama(monkeypatch, ["big:30b"])

    candidate = recommend.gather(CHAT, "http://gpu-box:11434", None, measure=False).candidate("big:30b")

    assert not candidate.can_measure and "another computer" in candidate.note


def test_again_on_another_computer_keeps_the_stored_time(monkeypatch, listed):
    """Before: --again threw the stored time away, and nothing measured a new one."""
    fake = _Ollama(monkeypatch, ["big:30b"])
    monkeypatch.setattr(ollama, "calibration_key", lambda url, model, lower, upper: _key(
        server="http://gpu-box:11434", model=model, lower=lower, upper=upper))
    calibration.store_speed("chat", _key(server="http://gpu-box:11434", model="big:30b"), 6.0)

    result = recommend.gather(CHAT, "http://gpu-box:11434", None, measure=True, again=True)

    assert fake.measured == [] and result.candidate("big:30b").seconds_per_symbol == pytest.approx(6.0)


def test_a_model_that_another_client_loaded_during_the_loop_stays_loaded(monkeypatch, listed):
    fake = _Ollama(monkeypatch, ["big:30b", "small:8b"])
    measure = fake._chat

    def _chat(url, model, num_ctx, **kwargs):
        result = measure(url, model, num_ctx, **kwargs)
        fake.held.append("small:8b")  # a background run loads it now
        return result

    monkeypatch.setattr(ollama, "measure_speed", _chat)

    recommend.gather(CHAT, URL, MACHINE, measure=True)

    assert fake.released == ["big:30b"]


def test_ctrl_c_during_a_measurement_releases_the_model(monkeypatch, listed):
    fake = _Ollama(monkeypatch, ["big:30b"], {"big:30b": KeyboardInterrupt()})

    with pytest.raises(KeyboardInterrupt):
        recommend.gather(CHAT, URL, MACHINE, measure=True)

    assert fake.released == ["big:30b"]


def test_a_model_that_is_not_installed_is_an_estimate(monkeypatch, listed):
    _Ollama(monkeypatch, ["small:8b"], {"small:8b": 2.0})

    result = recommend.gather(CHAT, URL, MACHINE, measure=True)

    assert result.candidate("new:12b").fit == Fit.GPU and not result.candidate("new:12b").installed
    # 3.7 against 3.0: the estimate is better by more than 0.2.
    assert (result.model.tag, result.estimated) == ("new:12b", True)


def test_the_embeddings_measure_with_their_own_measurement(monkeypatch, listed):
    fake = _Ollama(monkeypatch, ["e:4b"], {"e:4b": 0.05})

    result = recommend.gather(EMBEDDING, URL, MACHINE, measure=True)

    assert fake.measured == ["e:4b"] and result.model.tag == "e:4b"
    assert calibration.load_speed("embedding", _key(model="e:4b", lower=0, upper=0)) == pytest.approx(0.05)


def test_a_global_config_without_the_list_uses_the_list_of_the_release(monkeypatch):
    _Ollama(monkeypatch, [])

    result = recommend.gather(CHAT, URL, MACHINE, measure=False)

    assert result.candidate("gemma4:26b") is not None
    assert any("doctor --fix --only llm-models" in note for note in result.notes)


def test_no_ollama_gives_estimates_only(monkeypatch, listed):
    _Ollama(monkeypatch, None)

    result = recommend.gather(CHAT, URL, MACHINE, measure=True)

    assert result.estimated and all(c.seconds_per_symbol is None for c in result.candidates)
    assert any("no Ollama" in note for note in result.notes)


# ── measure_embed_speed on a fake server ──


def _embed_server(monkeypatch, body: dict, status: int = 200) -> list[dict]:
    sent: list[dict] = []

    def _post(url, json, timeout):
        sent.append(json)
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(net, "post", _post)
    return sent


def test_the_time_of_an_embedding_comes_from_the_times_of_ollama(monkeypatch):
    sent = _embed_server(monkeypatch, {"embeddings": [[0.1]], "total_duration": 1_500_000_000,
                                       "load_duration": 500_000_000})

    result = ollama.measure_embed_speed(URL, "e:4b", keep_alive="5m", timeout=60.0)

    assert result.seconds_per_symbol == pytest.approx(0.1)
    assert len(sent) == 2 and len(sent[1]["input"]) == 10
    # A new first line for each batch: a cache of the server does not make the second batch free.
    assert sent[0]["input"][0] != sent[1]["input"][0]


@pytest.mark.parametrize(("status", "body", "error"), [
    (404, {"error": "model 'e:4b' not found"}, "HTTP 404"),
    (200, {"embeddings": [[0.1]]}, "no times"),
])
def test_a_failed_embedding_measurement_says_why(monkeypatch, status, body, error):
    _embed_server(monkeypatch, body, status)

    result = ollama.measure_embed_speed(URL, "e:4b", keep_alive="5m", timeout=60.0)

    assert result.seconds_per_symbol is None and error in result.error


# ── The check of doctor ──


def _cfg(**values):
    from fw_context_mcp.config.settings import LLMConfig

    return LLMConfig(**{"enabled": True, "provider": "ollama", "ollama_url": URL, "model": "small:8b",
                        "embed_model": "e:4b", **values})


def test_the_check_of_the_mcp_server_loads_no_model(monkeypatch, listed):
    from fw_context_mcp.deps._checks import check_llm_recommend

    fake = _Ollama(monkeypatch, ["big:30b", "small:8b", "e:4b"])
    monkeypatch.setattr("fw_context_mcp.llm.hardware.machine_memory", lambda: MACHINE)

    result = check_llm_recommend(_cfg())

    # The estimate compares the models: new:12b fits the GPU and is better by more than 0.2.
    assert fake.measured == [] and result.status == "degraded"
    assert "new:12b (3.70, estimate: on the GPU), to install, is better than small:8b (3.00" in result.message


def test_the_check_of_doctor_measures_and_names_a_better_model(monkeypatch, listed):
    from fw_context_mcp.deps._checks import check_llm_recommend

    fake = _Ollama(monkeypatch, ["big:30b", "small:8b", "new:12b", "e:4b"],
                   {"big:30b": 6.0, "small:8b": 4.0, "new:12b": 3.0, "e:4b": 0.05})
    monkeypatch.setattr("fw_context_mcp.llm.hardware.machine_memory", lambda: MACHINE)
    steps: list[str] = []

    result = check_llm_recommend(_cfg(), steps.append)

    assert sorted(fake.measured) == ["big:30b", "e:4b", "new:12b", "small:8b"] and len(steps) == 4
    assert result.status == "degraded" and result.fix_cmd is None and not result.critical
    assert "new:12b (3.70, 3.0 s/symbol) is better than small:8b" in result.message
    assert "fw-context llm setup" in result.message


def test_a_check_without_the_llm_analysis_measures_no_chat_model(monkeypatch, listed):
    from fw_context_mcp.deps._checks import check_llm_recommend

    fake = _Ollama(monkeypatch, ["big:30b", "e:4b"])
    monkeypatch.setattr("fw_context_mcp.llm.hardware.machine_memory", lambda: MACHINE)

    result = check_llm_recommend(_cfg(analyze_symbols=False), lambda text: None)

    assert fake.measured == ["e:4b"] and "chat:" not in result.message


def test_the_recheck_after_a_fix_of_the_config_knows_the_check(monkeypatch, listed, tmp_path):
    """Before: "unknown check: llm-recommend" and exit code 1 after doctor --fix repaired the config."""
    from fw_context_mcp import deps

    fake = _Ollama(monkeypatch, ["big:30b"])
    monkeypatch.setattr("fw_context_mcp.llm.hardware.machine_memory", lambda: MACHINE)
    monkeypatch.setattr(deps, "load_project_config", lambda root: type("Cfg", (), {"llm": _cfg()})())

    result = deps._recheck("llm-recommend", tmp_path)

    assert result.name == "llm-recommend" and result.status in ("ok", "degraded") and fake.measured == []


def test_llm_recommend_measures_on_the_server_of_a_local_profile(monkeypatch, listed):
    """The key of a speed holds the URL as written: the calibration of the profile then counts."""
    import argparse

    from fw_context_mcp.cli import _llm, _recommend

    servers: list[str] = []
    monkeypatch.setattr(_llm, "_project_and_config", lambda args: (None, type("Cfg", (), {
        "llm": _cfg(ollama_url="http://127.0.0.1:11434")})()))
    monkeypatch.setattr(recommend, "gather", lambda role, server, memory, **kwargs: (
        servers.append(server) or recommend.Recommendation(role)))
    monkeypatch.setattr("fw_context_mcp.llm.hardware.machine_memory", lambda: MACHINE)

    _recommend.cmd_llm_recommend(argparse.Namespace(again=False, project=None))

    assert servers == ["http://127.0.0.1:11434", "http://127.0.0.1:11434"]


# ── The table ──


def test_the_table_marks_the_recommendation_the_current_model_and_a_weak_model():
    from fw_context_mcp.cli._recommend import table_lines

    result = _result(MEASURED)

    lines = table_lines(result, "qwen3:8b")

    assert lines[1].startswith("    gemma4:31b") and "s/symbol" in lines[1]
    assert "← recommended" in next(line for line in lines if "gemma4:26b" in line)
    assert "(current)" in next(line for line in lines if "qwen3:8b" in line)
    assert "low quality" in next(line for line in lines if "gemma4:e2b" in line)

"""The context of the LLM analysis from the measured speed (``llm.calibration``).

A fake measurement: a map from the context to the seconds of a typical
symbol, a lack of memory, or a failure.  No test reaches Ollama.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fw_context_mcp.llm import calibration
from fw_context_mcp.llm.calibration import Key, Measure, Speed
from fw_context_mcp.llm.errors import LLMStopRun, ModelDoesNotFit

KEY = Key(server="http://localhost:11434", model="m:1b", digest="d1", ollama="0.40.7", gpu_mib=12282,
          ram_mib=63488, lower=4096, upper=16384)
OOM = "HTTP 500: failed to allocate CUDA0 buffer"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch) -> Path:
    """Each test has its own calibration file."""
    monkeypatch.setenv("FW_CONTEXT_HOME", str(tmp_path))
    return tmp_path


def _speed(seconds: float) -> Speed:
    """A speed whose typical symbol takes *seconds*: all of it in the answer."""
    return Speed(prompt_tokens=1, prompt_tps=1e12, eval_tps=calibration.TYPICAL_ANSWER_TOKENS / seconds)


class _Machine:
    """The fake measurement: *seconds* maps a context to its time (or OOM / a failure), else *default*."""

    def __init__(self, seconds, default: float | str = 4.0) -> None:
        self.seconds, self.default, self.asked = seconds, default, []

    def __call__(self, num_ctx: int) -> Measure:
        self.asked.append(num_ctx)
        value = self.seconds(num_ctx) if callable(self.seconds) else self.seconds.get(num_ctx, self.default)
        if value == "oom":
            return Measure(None, OOM, out_of_memory=True)
        if value == "fail":
            return Measure(None, "Ollama does not answer (ConnectError: down)")
        return Measure(_speed(value))


def _dense(fast_up_to: int):
    """4 s/symbol up to *fast_up_to*, then 30 % slower: the model leaves the GPU."""
    return lambda ctx: 4.0 if ctx <= fast_up_to else 5.2


def _select(machine, *, key: Key = KEY, check: bool = True):
    return calibration.select_context(machine, key, check=check)


def _key(**change) -> Key:
    return Key(**{**KEY.__dict__, **change})


def test_a_model_as_fast_at_the_upper_bound_gets_it_and_a_calibration():
    machine = _Machine(_dense(16384))

    fit = _select(machine)

    assert fit.num_ctx == 16384 and machine.asked == [4096, 16384]
    stored = calibration.load(KEY)
    assert (stored.context, stored.best) == (16384, _speed(4.0))


def test_the_search_finds_the_largest_context_as_fast_as_the_lower_bound():
    machine = _Machine(_dense(9216))

    fit = _select(machine)

    assert fit.num_ctx == 9216 and len(machine.asked) <= 7 and all(c % 1024 == 0 for c in machine.asked)


def test_a_slower_context_within_the_tolerance_counts_as_as_fast():
    """The MoE model of the probe: 5 % slower at 16k with its KV cache."""
    assert _select(_Machine(lambda ctx: 4.0 if ctx == 4096 else 4.2)).num_ctx == 16384


@pytest.mark.parametrize(("upper", "fast_up_to", "expected"), [
    (10000, 9500, 9216), (5500, 5200, 5120), (10000, 10000, 10000),
], ids=["step below upper", "one step above lower", "upper itself"])
def test_an_upper_bound_that_is_not_a_step_keeps_each_step_below_it(upper, fast_up_to, expected):
    key = _key(upper=upper)

    assert _select(_Machine(_dense(fast_up_to)), key=key).num_ctx == expected


def test_an_out_of_memory_load_is_a_context_that_does_not_fit():
    """A measurement: the search goes on below it."""
    fit = _select(_Machine(lambda ctx: "oom" if ctx > 12288 else 4.0))

    assert fit.num_ctx == 12288


def test_a_failure_stops_the_search_and_stores_nothing():
    """A failure is not a measurement: no calibration from a search that did not end."""
    fit = _select(_Machine(lambda ctx: "fail" if ctx == 16384 else 4.0))

    assert fit.num_ctx == 4096 and "the search stopped at context 16384" in fit.reason
    assert calibration.load(KEY) is None


def test_a_model_that_does_not_load_at_the_lower_bound_stops_the_run():
    with pytest.raises(ModelDoesNotFit, match="failed to allocate") as stop:
        _select(_Machine({}, default="oom"))

    assert isinstance(stop.value, LLMStopRun)


def test_a_failure_at_the_lower_bound_keeps_the_upper_bound():
    fit = _select(_Machine({}, default="fail"))

    assert fit.num_ctx == 16384 and "the measurement failed" in fit.reason


def test_no_search_when_the_upper_bound_is_the_lower_one():
    machine = _Machine({})

    assert _select(machine, key=_key(upper=4096)).num_ctx == 4096 and machine.asked == []


# ── A run with a calibration ────────────────────────────────────────────


def _calibrated(context: int = 12288, best: float = 4.0, key: Key = KEY) -> None:
    calibration.store(calibration.Calibration(key, _speed(best), context, "2026-10-21T12:00:00+00:00", "test"))


def test_a_manual_run_measures_one_request_at_the_calibrated_context():
    _calibrated()
    machine = _Machine({12288: 4.1})

    fit = _select(machine)

    assert fit.num_ctx == 12288 and machine.asked == [12288]


def test_a_slow_check_searches_below_the_calibrated_context_and_keeps_the_calibration():
    """Another program uses the memory now: the calibration is the speed of the machine, not of this moment."""
    _calibrated()
    machine = _Machine(lambda ctx: 4.0 if ctx <= 8192 else 6.0)

    fit = _select(machine)

    assert fit.num_ctx == 8192 and "another program uses the memory now" in fit.reason
    assert calibration.load(KEY).context == 12288


def test_a_lower_bound_slower_than_the_calibration_tells_the_factor():
    _calibrated()

    fit = _select(_Machine({}, default=12.0))

    assert fit.num_ctx == 4096 and "3.0× slower than its calibration" in fit.reason


def test_a_check_clearly_faster_than_the_calibration_calibrates_again():
    """Another program used the memory during the calibration: its context can be too small."""
    _calibrated(context=8192, best=8.0)
    machine = _Machine(_dense(16384))

    fit = _select(machine)

    assert fit.num_ctx == 16384 and calibration.load(KEY).best == _speed(4.0)


def test_a_failed_check_keeps_the_calibrated_context():
    _calibrated()

    fit = _select(_Machine({}, default="fail"))

    assert fit.num_ctx == 12288 and "the check failed" in fit.reason


def test_a_background_run_takes_the_calibration_without_a_measurement():
    _calibrated()
    machine = _Machine({})

    assert _select(machine, check=False).num_ctx == 12288 and machine.asked == []


def test_a_background_run_without_a_calibration_keeps_num_ctx():
    assert _select(_Machine({}), check=False) is None


@pytest.mark.parametrize("change", [{"digest": "d2"}, {"ollama": "0.41.0"}, {"gpu_mib": 16384}, {"upper": 32768},
                                    {"lower": 2048}],
                         ids=["new model file", "new Ollama", "new GPU", "new num_ctx", "new lower bound"])
def test_a_calibration_of_another_key_does_not_apply(change):
    _calibrated()
    other = _key(**change)

    assert calibration.load(other) is None


def test_a_new_calibration_replaces_the_one_of_the_same_model(home: Path):
    _calibrated()
    calibration.store(calibration.Calibration(_key(ollama="0.41.0"), _speed(3.0), 8192,
                                              "2026-10-22T12:00:00+00:00", "test"))

    assert len(json.loads((home / "llm_calibration.json").read_text())["calibrations"]) == 1


@pytest.mark.parametrize("text", ["{not json", '{"format": 99, "calibrations": []}', '{"format": 1}'])
def test_a_file_that_is_not_valid_gives_no_calibration_and_a_new_one_replaces_it(home: Path, text):
    (home / "llm_calibration.json").write_text(text, encoding="utf-8")

    assert calibration.load(KEY) is None
    _calibrated()
    assert calibration.load(KEY).context == 12288


def test_a_check_within_its_wider_tolerance_keeps_the_context_without_a_search():
    """The MoE model of the probe is 7.8 % slower at 16k: 2 % of noise must not start a search."""
    _calibrated(context=16384)
    machine = _Machine({16384: 4.0 * 1.12})

    assert _select(machine).num_ctx == 16384 and machine.asked == [16384]


def test_two_upper_bounds_of_one_model_keep_their_own_calibration():
    """Two profiles of one model with another num_ctx: each manual run would else search again."""
    _calibrated(context=12288)
    _calibrated(context=24576, key=_key(upper=32768))

    assert (calibration.load(KEY).context, calibration.load(_key(upper=32768)).context) == (12288, 24576)


def test_a_slow_check_does_not_measure_the_checked_context_again():
    _calibrated()
    machine = _Machine(lambda ctx: 4.0 if ctx <= 8192 else 6.0)

    _select(machine)

    assert machine.asked.count(12288) == 1


def test_a_calibrated_lower_bound_that_is_slow_is_measured_one_time():
    _calibrated(context=4096)
    machine = _Machine({}, default=12.0)

    fit = _select(machine)

    assert fit.num_ctx == 4096 and machine.asked == [4096]


def test_an_out_of_memory_check_searches_below_the_calibrated_context():
    _calibrated()
    machine = _Machine(lambda ctx: "oom" if ctx > 8192 else 4.0)

    assert _select(machine).num_ctx == 8192


def test_an_out_of_memory_lower_bound_on_the_check_stops_the_run():
    _calibrated()

    with pytest.raises(ModelDoesNotFit, match="m:1b"):
        _select(_Machine({}, default="oom"))


def test_a_failed_new_calibration_keeps_the_context_that_the_check_measured():
    """The check measured the stored context as working: a failure at the lower bound must not give the upper bound."""
    _calibrated(context=8192, best=8.0)
    machine = _Machine(lambda ctx: "fail" if ctx == 4096 else 4.0)

    fit = _select(machine)

    assert fit.num_ctx == 8192 and "a new calibration failed" in fit.reason


@pytest.mark.parametrize("field", ["prompt_tps", "eval_tps"])
def test_a_stored_speed_that_is_not_positive_is_no_calibration(home: Path, field):
    """A speed of 0 divided by zero in the run."""
    _calibrated()
    path = home / "llm_calibration.json"
    data = json.loads(path.read_text())
    data["calibrations"][0]["best"][field] = 0
    path.write_text(json.dumps(data))

    assert calibration.load(KEY) is None


def test_a_stored_context_outside_the_bounds_is_no_calibration(home: Path):
    _calibrated(context=0)

    assert calibration.load(KEY) is None


def test_a_memory_that_the_machine_does_not_tell_now_keeps_the_calibration():
    """nvidia-smi can fail for one call: that is not a new GPU."""
    _calibrated()

    assert calibration.load(_key(gpu_mib=None)).context == 12288


def test_a_store_that_fails_gives_a_warning_and_the_run_goes_on(home: Path, monkeypatch, caplog):
    def _fail(path, text, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(calibration, "write_text_atomic", _fail)

    fit = _select(_Machine(_dense(16384)))

    assert fit.num_ctx == 16384 and "cannot store the calibration" in caplog.text


def test_a_stopped_new_calibration_keeps_the_context_that_the_check_measured_as_fast():
    """The search stops on a timeout at 16384: 8192, which the check just measured, still counts."""
    _calibrated(context=8192, best=8.0)
    machine = _Machine(lambda ctx: "fail" if ctx == 16384 else 4.0)

    fit = _select(machine)

    assert fit.num_ctx == 8192 and machine.asked.count(8192) == 1


def test_a_calibration_made_without_the_memory_matches_a_key_with_it():
    """nvidia-smi failed during the calibration: the next run must not search again."""
    _calibrated(key=_key(gpu_mib=None))

    assert calibration.load(KEY).context == 12288


@pytest.mark.parametrize(("field", "value"), [("key.lower", "4096"), ("best.eval_tps", float("inf")),
                                              ("context", 1.5), ("key.gpu_mib", "12 GB")])
def test_an_entry_that_a_person_changed_breaks_no_other_entry(home: Path, field, value):
    """The file is text: one bad entry of any model must not break the load and the store of the others."""
    other = _key(model="other:1b")
    _calibrated(key=other)
    _calibrated()
    path = home / "llm_calibration.json"
    data = json.loads(path.read_text())
    entry = data["calibrations"][0]
    part, _, name = field.rpartition(".")
    (entry[part] if part else entry)[name] = value
    path.write_text(json.dumps(data))

    assert calibration.load(other) is None and calibration.load(KEY).context == 12288
    _calibrated(key=other)
    assert calibration.load(other).context == 12288

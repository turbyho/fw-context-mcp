"""The context of the LLM analysis from the measured speed of the model on this machine.

The speed of the analysis follows where the model runs: all in the GPU
memory, partly in RAM, all in RAM, or in RAM with swap.  The report of
Ollama (``/api/ps``) does not tell it: measured 2026-10-21 with Ollama
0.40.7, it gives 1.09 GB for gemma4:12b at each context, leaves out the
RAM experts of a MoE model, and reports more on the GPU at 8k than at 4k
for qwen2.5-coder:14b.  The speed of one reference request tells it for
each kind of model (``plans/eval/llm-models-2026-10-20/speed_ctx_0.40.7.jsonl``):

=====================  =========  ========  ========  ========
answer tokens/s        4k         8k        12k       16k
=====================  =========  ========  ========  ========
qwen2.5-coder:14b      37         27.7      22.3      20.5
gemma4:12b             138        137       138       136
qwen3-coder:30b (MoE)  64.4       62.7      61.5      61.0
=====================  =========  ========  ========  ========

The speed falls with the context in one direction, and the noise is 2 to
3 %.  Thus the context of a run is the largest one whose speed is at
least ``1 - SPEED_TOLERANCE`` of the speed at the lower bound.  WHY 10 %:
the MoE model loses 5 % to 16k with its KV cache, and a dense model that
leaves the GPU loses 25 % at once.

The hardware changes seldom, the use of the memory by other programs
often (decision of the user, 2026-10-21).  Thus a calibration measures
the full search one time and keeps the result in
``~/.fw-context/llm_calibration.json``; a manual run measures one
request at the stored context, and searches again only when that is
slow.  A background run uses the stored context without a measurement.
The file belongs to the machine, not to the config: the user does not
edit it.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .. import __version__
from ..utils import fw_context_home, write_text_atomic
from .client import ContextFit
from .errors import ModelDoesNotFit

log = logging.getLogger(__name__)

#: A context whose speed is at most this share below the best one is as
#: fast as the best one, for the selection (see the module).
SPEED_TOLERANCE = 0.10

#: The tolerance of the check of a manual run against the calibration.
#: WHY wider than ``SPEED_TOLERANCE``: the selected context can be up to
#: 10 % slower than the best one (the MoE model of the table: 7.8 % at
#: 16k), and a check at the same 10 % then starts a new search on 2 % of
#: noise.  The same margin decides "clearly faster than the calibration".
CHECK_TOLERANCE = 0.15

#: The step of the search: a multiple of it is a context that the KV cache
#: of llama.cpp allocates without waste.
CONTEXT_STEP = 1024

#: The answer of a typical symbol, in tokens (measured 2026-10-20 with
#: qwen2.5-coder:14b: 139 to 465): it weights the answer speed against the
#: prompt speed in ``Speed.seconds_per_symbol``.
TYPICAL_ANSWER_TOKENS = 300

_FILE_NAME = "llm_calibration.json"
_FORMAT = 1


@dataclass(frozen=True)
class Speed:
    """The speed of one reference request, from the times that llama.cpp gives (``prompt_eval_*``, ``eval_*``)."""

    prompt_tokens: int
    prompt_tps: float
    eval_tps: float

    @property
    def seconds_per_symbol(self) -> float:
        """The time of a typical symbol: the reference prompt and an answer of ``TYPICAL_ANSWER_TOKENS``."""
        return self.prompt_tokens / self.prompt_tps + TYPICAL_ANSWER_TOKENS / self.eval_tps

    def as_fast_as(self, best: Speed, tolerance: float = SPEED_TOLERANCE) -> bool:
        """Return True when this speed is at most *tolerance* below *best*."""
        return self.seconds_per_symbol * (1 - tolerance) <= best.seconds_per_symbol


@dataclass(frozen=True)
class Measure:
    """The result of one measurement at one context.

    ``speed`` is None when the measurement failed; ``error`` then tells why,
    and ``out_of_memory`` is True when the load failed for memory, which is
    a measurement ("does not fit") and not a failure.
    """

    speed: Speed | None
    error: str = ""
    out_of_memory: bool = False


@dataclass(frozen=True)
class Key:
    """What a calibration is valid for: the server, the model and its file, Ollama, the machine, the bounds.

    ``gpu_mib`` and ``ram_mib`` are the total memory of this machine for a
    server on this machine, else None: a new GPU or more RAM makes a new
    calibration.  ``lower`` and ``upper`` are the bounds of the search: the
    best speed is the speed at ``lower``, and ``upper`` is the context of
    the run (``num_ctx`` or the trained context).
    """

    server: str
    model: str
    digest: str
    ollama: str
    gpu_mib: int | None
    ram_mib: int | None
    lower: int
    upper: int

    def matches(self, stored: Key) -> bool:
        """Return True when *stored* is this key; a memory that one of the two does not tell is not a change.

        WHY: ``nvidia-smi`` can fail for one call (a driver update, a busy
        GPU), now or during the calibration, and a key without the memory
        must not throw the calibration away.  The limit: a GPU that
        ``nvidia-smi`` never tells (AMD, Intel) gives no memory, thus a new
        card of that kind does not make a new calibration.
        """
        def same(mine: int | None, theirs: int | None) -> bool:
            return mine is None or theirs is None or mine == theirs

        return (same(self.gpu_mib, stored.gpu_mib) and same(self.ram_mib, stored.ram_mib)
                and dataclasses.replace(stored, gpu_mib=None, ram_mib=None)
                == dataclasses.replace(self, gpu_mib=None, ram_mib=None))

    def slot(self) -> tuple[str, str, int, int]:
        """The calibration that a new one replaces: of the same server, model and bounds."""
        return self.server, self.model, self.lower, self.upper


@dataclass(frozen=True)
class Calibration:
    """The stored result of a full search: the speed at the lower bound and the selected context."""

    key: Key
    best: Speed
    context: int
    created: str
    fw_context: str


def select_context(measure: Callable[[int], Measure], key: Key, *, check: bool) -> ContextFit | None:
    """Return the context of a run from the calibration of *key*, with one check, a new search, or none.

    *check* False (a background run, ``reindex_file``): the stored context
    without a measurement, or None without a calibration.  *check* True (a
    manual run): see the module; a calibration that is not there or not
    valid any more is made and stored.  One context is measured one time
    in one call.

    Raises:
        ModelDoesNotFit: The model does not load at the lower bound for
            memory, and no context of this machine is known to work.
    """
    lower, upper = key.lower, key.upper
    if upper <= lower:
        return ContextFit(upper, f"context {upper} (the context of the run is not above {lower})")
    stored = load(key)
    if not check:
        return None if stored is None else ContextFit(stored.context, f"context {stored.context} (the calibration "
                                                                        f"of {stored.created[:10]})")
    measure = _measured_once(measure)
    if stored is None:
        return _calibrate(measure, key)
    now = measure(stored.context)
    if now.speed is not None and now.speed.as_fast_as(stored.best, CHECK_TOLERANCE):
        if stored.best.as_fast_as(now.speed, CHECK_TOLERANCE):
            return ContextFit(stored.context, f"context {stored.context}, {_rate(now.speed)}: as the calibration")
        # Clearly faster than the calibration: another program used the
        # memory during the calibration.  Its context can be too small.
        log.info("LLM analysis: the model is faster than its calibration; a new calibration")
        return _calibrate(measure, key, known=stored.context)
    if now.speed is None and not now.out_of_memory:
        return ContextFit(stored.context, f"context {stored.context} (the calibration; the check failed: "
                                          f"{now.error})")
    # Slow, or no memory, at the calibrated context: another program uses
    # the memory now.  The search keeps the calibration: it is the speed of
    # the machine, not of this moment.
    found, note = _largest(measure, key, stored.context, stored.best, first_is_known=False)
    if found is None:
        return ContextFit(lower, f"context {lower}: {note}")
    return ContextFit(found, f"context {found}, as fast as the calibration "
                             f"({_rate(stored.best)} at {lower}); another program uses the memory now{note}")


def _measured_once(measure: Callable[[int], Measure]) -> Callable[[int], Measure]:
    """Return *measure* that measures each context one time: a check and the search after it share a result."""
    results: dict[int, Measure] = {}

    def once(num_ctx: int) -> Measure:
        if num_ctx not in results:
            results[num_ctx] = measure(num_ctx)
        return results[num_ctx]

    return once


def _calibrate(measure: Callable[[int], Measure], key: Key, *, known: int | None = None) -> ContextFit:
    """Search the largest context as fast as the lower bound, store the calibration, and return the context.

    *known* is a context that a check of this run measured as working: a
    failure at the lower bound then gives it, not the upper bound or a
    stop of the run.
    """
    lower = key.lower
    first = measure(lower)
    if first.speed is None and known is not None:
        return ContextFit(known, f"context {known} (the calibration; a new calibration failed: {first.error})")
    if first.out_of_memory:
        raise _does_not_fit(key, first)
    if first.speed is None:
        return ContextFit(key.upper, f"context {key.upper} (the context of the run): the measurement failed: "
                                     f"{first.error}")
    found, note = _largest(measure, key, key.upper, first.speed, first_is_known=True)
    assert found is not None, "the lower bound was measured as fast as itself"
    if note and known is not None and known > found:
        # The search stopped below the context that the check of this run
        # measured: that measurement (one of this call) still counts.
        at_known = measure(known)
        if at_known.speed is not None and at_known.speed.as_fast_as(first.speed):
            found = known
    if not note:
        store(Calibration(key, first.speed, found, datetime.now(UTC).isoformat(timespec="seconds"), __version__))
    return ContextFit(found, f"context {found}, {_rate(first.speed)} at {lower}: the largest context as fast"
                             f"{note}")


def _largest(measure: Callable[[int], Measure], key: Key, upper: int, best: Speed, *,
             first_is_known: bool) -> tuple[int | None, str]:
    """Return the largest context from the lower bound to *upper* as fast as *best*, and a note for the log.

    The candidates are each multiple of ``CONTEXT_STEP`` above the lower
    bound and below *upper*, then *upper*: a trained context of 10000 is a
    candidate too.  The search measures *upper* first (the answer when the
    memory is free), then halves the candidates.  It keeps the index of the
    largest one that is fast (-1: the lower bound) and of the smallest one
    that is not.  *first_is_known*: the lower bound is as fast as *best*;
    else the search ends with the lower bound when no candidate is fast,
    and the result is None when the lower bound is slow too.

    A failure that is not a lack of memory stops the search at the largest
    context that it found: a failure is not a measurement.  The note then
    names it, and the caller does not store the result.
    """
    lower = key.lower
    candidates = [*range((lower // CONTEXT_STEP + 1) * CONTEXT_STEP, upper, CONTEXT_STEP), upper]
    fits, misses = -1, len(candidates)
    probe = len(candidates) - 1
    note = ""
    while misses - fits > 1:
        result = measure(candidates[probe])
        if result.speed is None and not result.out_of_memory:
            note = f" (the search stopped at context {candidates[probe]}: {result.error})"
            break
        if result.speed is not None and result.speed.as_fast_as(best):
            fits = probe
        else:
            misses = probe
        probe = (fits + misses) // 2
    if fits >= 0:
        return candidates[fits], note
    if first_is_known:
        return lower, note
    at_lower = measure(lower)
    if at_lower.out_of_memory:
        raise _does_not_fit(key, at_lower)
    if at_lower.speed is not None and at_lower.speed.as_fast_as(best):
        return lower, note
    if at_lower.speed is None:
        return None, f"the measurement failed: {at_lower.error}"
    factor = at_lower.speed.seconds_per_symbol / best.seconds_per_symbol
    return None, (f"the model is {factor:.1f}× slower than its calibration ({_rate(at_lower.speed)} against "
                  f"{_rate(best)}): another program uses the GPU memory or the RAM")


def _does_not_fit(key: Key, result: Measure) -> ModelDoesNotFit:
    return ModelDoesNotFit(
        f"Ollama cannot load the model {key.model} with a context of {key.lower} tokens, the smallest context of "
        f"the analysis: {result.error}. Make memory free, or select a smaller model (fw-context llm setup, --llm).")


def _rate(speed: Speed) -> str:
    return f"{speed.seconds_per_symbol:.1f} s/symbol"


def _path() -> Path:
    """The file of the calibrations; read at each call: the test session moves the global directory."""
    return fw_context_home() / _FILE_NAME


def load(key: Key) -> Calibration | None:
    """Return the stored calibration of *key*, or None: none, another key, or an entry that is not valid.

    An entry with a value that cannot be a measurement (a speed or a
    context of 0, a context outside the bounds) is not valid: a speed of 0
    divided by zero in the run.
    """
    for entry in _entries(warn=True):
        calibration = _calibration_of(entry)
        if calibration is not None and key.matches(calibration.key):
            return calibration
    return None


def _calibration_of(entry: dict) -> Calibration | None:
    """Return the calibration of one entry of the file, or None when a value is not of a measurement.

    WHY each check: the file is text that a person can change, and one
    entry of any model must not break the ``load`` and ``store`` of the
    others (a bound that is a string, a speed that is ``Infinity``).
    """
    try:
        key = Key(**entry["key"])
        best = Speed(**entry["best"])
        calibration = Calibration(key, best, entry["context"], str(entry["created"]), str(entry["fw_context"]))
    except (KeyError, TypeError, ValueError):
        return None
    integers = (key.lower, key.upper, calibration.context, best.prompt_tokens)
    if not all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in integers):
        return None
    if not all(isinstance(value, int | None) and not isinstance(value, bool) for value in (key.gpu_mib, key.ram_mib)):
        return None
    speeds = (best.prompt_tps, best.eval_tps)
    if not all(isinstance(value, int | float) and math.isfinite(value) and value > 0 for value in speeds):
        return None
    if not key.lower <= calibration.context <= key.upper:
        return None
    return calibration


def store(calibration: Calibration) -> None:
    """Store *calibration*: it replaces the one of the same server, model and bounds (``Key.slot``).

    A write that fails gives a warning: the run goes on, and the next
    manual run calibrates again.  Two processes that store at the same
    time can lose one entry: the next manual run then calibrates again.
    """
    kept = []
    for entry in _entries(warn=False):
        other = _calibration_of(entry)
        if other is not None and other.key.slot() != calibration.key.slot():
            kept.append(entry)
    kept.append(dataclasses.asdict(calibration))
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps({"format": _FORMAT, "calibrations": kept}, indent=2) + "\n")
    except OSError as e:
        log.warning("LLM analysis: cannot store the calibration in %s: %s", path, e)


def _entries(*, warn: bool) -> list[dict]:
    """Return the entries of the file; a file that is missing, of another format or not JSON gives none."""
    path = _path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        if warn:
            log.warning("LLM analysis: the calibration file %s is not valid (%s): a new calibration replaces it",
                        path, e)
        return []
    if not isinstance(data, dict) or data.get("format") != _FORMAT or not isinstance(data.get("calibrations"), list):
        return []
    return [entry for entry in data["calibrations"] if isinstance(entry, dict)]

"""The recommendation of a local model of Ollama, for the chat (the LLM analysis) and for the embeddings.

The rule of two bands (decision of the user, 2026-10-21): the quality
comes first, and the speed decides between models of the same quality.

1. Q* is the best quality of the candidates.  The band of the quality
   holds each model with a quality of at least Q* minus the tolerance of
   the role: the models in it are of the same quality.
2. F is the fastest model in the band of the quality.  The band of the
   speed holds each model with a time per symbol of at most F / (1 -
   ``SPEED_TOLERANCE``): the models in it are as fast as F.
3. The recommendation is the best quality in the band of the speed; then
   the faster model; then the name.

WHY these tolerances: the quality of a chat model is the blind grade 1-5
of the benchmark of 2026-10-20, with a noise of about 0.2.  The quality of
an embedding model is MRR@10 (0-1) of 105 queries on one project, with a
noise of about 0.05 (the bootstrap interval of 4b against 8b: -0.011 to
+0.064).  A difference in the noise is not a difference.  20 % of the speed: the
measurements of one model differ by 2 to 3 %, and the user waits 20 %
more without a loss that matters.

The candidates are the entries of the list of the models in the global
config (``[llm.models]``, ``[llm.embed_models]``, ``config._model_list``)
with a quality, without ``enabled = false`` and without ``remote = true``:
a cloud model sends the code out, thus only the setup of a cloud profile
offers one.

The time of an installed model is measured on this machine (the
reference request of ``measure_speed`` at ``LOWEST_CONTEXT``, or a batch
of ``measure_embed_speed``), and kept in the calibration file.  A model
that is not installed has no time: its estimate comes from its weights
and the memory of this machine (``estimate``), and the result says that it
is an estimate.  The measurement replaces it when the model is installed.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Callable
from dataclasses import dataclass, field

from .calibration import LOWEST_CONTEXT, load_speed, store_speed
from .hardware import Memory

#: A model whose time is at most this share above the fastest one is as
#: fast as it (see the module).
SPEED_TOLERANCE = 0.2


@dataclass(frozen=True)
class Role:
    """A role of a model: its table in the list of the models, and the tolerance of its quality."""

    name: str
    table: str
    quality_tolerance: float


CHAT = Role("chat", "models", 0.2)
EMBEDDING = Role("embedding", "embed_models", 0.05)

#: The GPU memory that a model does not get for its weights, apart from the
#: other programs: the KV cache of a context of 4096 and the buffers of
#: llama.cpp.  From the measurement of 2026-10-21 at 4096 on a GPU of 12282
#: MiB with 2.7 GiB (2765 MiB) used by the desktop: gemma4:12b (7629 MiB of
#: weights) ran at 3.7 s/symbol, qwen2.5-coder:14b (8583 MiB) and phi4:14b
#: (8678 MiB) at 12 to 13 s/symbol, thus partly from RAM.  The reserve of
#: that machine was 4096 MiB in all: 4096 - 2765 = 1331 MiB for the model.
RUNTIME_RESERVE_MIB = 4096 - 2765

#: The GPU memory of the other programs when ``nvidia-smi`` does not tell
#: it (``Memory.gpu_others_mib`` None: Apple Silicon, Windows WDDM): the
#: desktop of the measurement above.  WHY not 0: an estimate that gives a
#: model that does not fit costs a slow run; one that is too small costs
#: only a smaller model.
OTHERS_ESTIMATE_MIB = 2765

#: The RAM that the system and the other programs keep, when the RAM that
#: is available now is not known (``Memory.ram_available_mib`` None, macOS).
#: An estimate: a model is "GPU + RAM" when its part outside the GPU fits
#: in the RAM without it.  It holds the KV cache and the buffers of
#: llama.cpp too.  With the RAM of now, ``RUNTIME_RESERVE_MIB`` holds them.
RAM_RESERVE_MIB = 8 * 1024

_MIB_PER_GB = 1e9 / (1024 * 1024)


class Fit(enum.IntEnum):
    """Where a model that is not installed runs, estimated from its weights; a lower value is faster.

    A MoE model runs few experts for each token, thus it is faster in RAM
    than a dense model of the same weights.
    """

    GPU = 0
    MOE_IN_RAM = 1
    DENSE_IN_RAM = 2

    @property
    def label(self) -> str:
        # "RAM": all of the model or the part that the GPU does not hold.
        return {Fit.GPU: "on the GPU", Fit.MOE_IN_RAM: "in RAM (MoE)", Fit.DENSE_IN_RAM: "in RAM"}[self]


@dataclass(frozen=True)
class Candidate:
    """A model of the list, with its measured time on this machine or the estimate of where it runs.

    ``seconds_per_symbol`` is the measured time of a typical symbol, or
    None.  ``fit`` is the estimate, for a model without a measurement; None
    when the memory of the machine or the weights are not known, or when
    the model does not fit.  ``note`` tells why a model has neither.
    """

    tag: str
    quality: float
    installed: bool
    seconds_per_symbol: float | None = None
    fit: Fit | None = None
    weights_gb: float | None = None
    dimension: int | None = None
    note: str = ""
    #: An installed model without a stored speed that was not measured:
    #: a measurement can give it a time.
    can_measure: bool = False

    def cost(self) -> tuple[int, float] | None:
        """Return the order of the speed (a lower value is faster), or None when it is not known.

        Measured: ``(0, seconds)``.  Estimated: ``(fit, weights)``: in one
        place of the memory, the time follows the bytes that each token
        reads.  The rule compares only costs of one kind (``choose``).
        """
        if self.seconds_per_symbol is not None:
            return 0, self.seconds_per_symbol
        if self.fit is not None and self.weights_gb is not None:
            return int(self.fit), self.weights_gb
        return None


#: The epsilon of the two bands: 3.73 - 0.2 is 3.5300000000000002 in
#: binary, thus a quality of 3.53 is in the band; the same for a time of
#: 3.0 against 2.4 / 0.8.
_EPSILON = 1e-9


@dataclass(frozen=True)
class Bands:
    """The two bands of a set of candidates: the lowest quality of the band of the quality, and F.

    ``tier`` and ``fastest`` are the cost of F (``Candidate.cost``).
    """

    lowest_quality: float
    tier: int
    fastest: float
    speed_tolerance: float = SPEED_TOLERANCE

    def as_fast(self, candidate: Candidate) -> bool:
        """Return True when *candidate* is in the band of the speed."""
        cost = candidate.cost()
        return (cost is not None and cost[0] == self.tier
                and cost[1] * (1 - self.speed_tolerance) <= self.fastest * (1 + _EPSILON))

    def holds(self, candidate: Candidate) -> bool:
        """Return True when *candidate* is in the two bands: of the same quality as the best, and as fast as F."""
        return candidate.quality >= self.lowest_quality and self.as_fast(candidate)


def bands(candidates: list[Candidate], role: Role, speed_tolerance: float = SPEED_TOLERANCE) -> Bands | None:
    """Return the two bands of *candidates* (see the module), or None when no candidate has a cost."""
    rated = [(c, cost) for c in candidates if (cost := c.cost()) is not None]
    if not rated:
        return None
    lowest = max(c.quality for c, _ in rated) - role.quality_tolerance - _EPSILON
    _, (tier, fastest) = min(((c, cost) for c, cost in rated if c.quality >= lowest),
                             key=lambda item: (item[1], item[0].tag))
    return Bands(lowest, tier, fastest, speed_tolerance)


def choose(candidates: list[Candidate], role: Role, speed_tolerance: float = SPEED_TOLERANCE) -> Candidate | None:
    """Return the model that the rule of two bands selects from *candidates* (see the module), or None.

    A candidate without a cost does not take part.  The candidates must
    all be measured, or all estimated: a time and a size of weights are
    not of one scale.  The band of the speed of an estimate holds the
    models of the same place of the memory as F.
    """
    found = bands(candidates, role, speed_tolerance)
    if found is None:
        return None
    fast = [c for c in candidates if found.as_fast(c)]
    return min(fast, key=lambda c: (-c.quality, c.cost(), c.tag))


def estimate(weights_gb: float | None, moe: bool, memory: Memory | None) -> Fit | None:
    """Return where a model of *weights_gb* runs on a machine of *memory*, or None (not known, does not fit).

    The GPU gives its memory without ``RUNTIME_RESERVE_MIB`` and without
    the memory that other programs hold now (``Memory.gpu_others_mib``,
    else ``OTHERS_ESTIMATE_MIB``).  The RAM gives the memory that is
    available now (``Memory.ram_available_mib``) without
    ``RUNTIME_RESERVE_MIB``, else its size without ``RAM_RESERVE_MIB``.
    WHY the runtime reserve on the RAM side too: the KV cache and the
    buffers of the part outside the GPU are in the RAM, and a model that
    takes all of the available RAM swaps.  WHY the memory of now (decision of the user,
    2026-10-23): the other programs hold another amount on each machine
    and at each time.  The estimate thus describes the moment of the
    call.  A machine with unified memory (Apple Silicon) runs the model in
    its GPU part.
    """
    if weights_gb is None or memory is None:
        return None
    weights = weights_gb * _MIB_PER_GB
    others = memory.gpu_others_mib if memory.gpu_others_mib is not None else OTHERS_ESTIMATE_MIB
    gpu = max(0, (memory.gpu_mib or 0) - RUNTIME_RESERVE_MIB - others)
    if weights <= gpu:
        return Fit.GPU
    if memory.unified or memory.ram_mib is None:
        return None
    ram = (memory.ram_available_mib - RUNTIME_RESERVE_MIB if memory.ram_available_mib is not None
           else memory.ram_mib - RAM_RESERVE_MIB)
    if weights - gpu <= ram:
        return Fit.MOE_IN_RAM if moe else Fit.DENSE_IN_RAM
    return None


@dataclass
class Recommendation:
    """The candidates of one role and the recommended model.

    ``model`` is the measured choice of the installed models, or the
    estimated choice when no installed model has a time, or when the
    estimate is better than the measured choice by more than the
    tolerance of the quality.  ``estimated`` is True when ``model`` comes
    from the estimate: the model is then likely not installed.
    """

    role: Role
    candidates: list[Candidate] = field(default_factory=list)
    model: Candidate | None = None
    estimated: bool = False
    #: Why the list is short or not there, for the person.
    notes: list[str] = field(default_factory=list)
    #: The two bands of the measured models; None without a measured model.
    measured_bands: Bands | None = None

    def candidate(self, tag: str) -> Candidate | None:
        """Return the candidate of *tag* (with or without ``:latest``), or None."""
        from .ollama import _with_tag

        return next((c for c in self.candidates if _with_tag(c.tag) == _with_tag(tag)), None)


def recommend(role: Role, candidates: list[Candidate], notes: list[str] | None = None) -> Recommendation:
    """Return the recommendation of *role* from *candidates* (see ``Recommendation``)."""
    timed = [c for c in candidates if c.seconds_per_symbol is not None]
    measured = choose(timed, role)
    estimated = choose([c for c in candidates if c.seconds_per_symbol is None], role)
    result = Recommendation(role, candidates, measured, False, list(notes or []), bands(timed, role))
    if estimated is not None and (measured is None
                                  or estimated.quality > measured.quality + role.quality_tolerance + 1e-9):
        result.model, result.estimated = estimated, True
    return result


def list_entries(role: Role) -> tuple[dict[str, dict], str]:
    """Return the entries of *role* from the global config, and a note when they come from this release.

    WHY the release as a fallback: ``fw-context init`` and ``doctor --fix``
    write the list, and a global config without it (not synced yet, or a
    file that cannot be read) must not leave the setup without a
    recommendation.  The check ``llm-models`` of doctor reports it.
    """
    from ..config import ConfigError, settings
    from ..config._model_list import shipped

    try:
        llm = settings.read_config_file(settings._GLOBAL_CONFIG_PATH).get("llm")
    except ConfigError as e:
        return shipped()[role.table], f"the global config cannot be read ({e}): the list of fw-context applies"
    table = llm.get(role.table) if isinstance(llm, dict) else None
    if not isinstance(table, dict):
        return shipped()[role.table], ("the global config has no list of the models: the list of fw-context "
                                       "applies (fw-context doctor --fix --only llm-models)")
    return {tag: entry for tag, entry in table.items() if isinstance(entry, dict)}, ""


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def gather(role: Role, server_url: str, memory: Memory | None, *, measure: bool, again: bool = False,
           keep_alive: str = "5m", timeout: float = 600.0,
           progress: Callable[[str], None] | None = None) -> Recommendation:
    """Return the recommendation of *role* for the Ollama at *server_url*.

    *measure*: measure each installed model that has no stored time (a
    model loads, 5 to 70 s each), and store the time.  False: only the
    stored times (``check_dependencies`` of the MCP server must not load
    a model).  *again*: measure each installed model again.  A model that
    the measurement loaded is released after it, also after Ctrl-C: the
    next model gets the memory.  A model that the server held just before
    the measurement stays loaded, with the ``keep_alive`` of the
    measurement.  Only a server of this machine is measured: on another
    computer, the measurement can push out the models of other users.
    *memory* is the memory of the machine of the server, for the
    estimate; None for a server on another computer.
    """
    from ..config.settings import _is_loopback_url
    from ._diag import installed_models
    from .ollama import _with_tag, calibration_key

    entries, list_note = list_entries(role)
    notes = [list_note] if list_note else []
    # A server of another computer is never measured: its models are not
    # "measurable" either, thus no question and no hint offers it.
    measurable = _is_loopback_url(server_url)
    if measure and not measurable:
        measure = False
        notes.append(f"{server_url} is not on this machine: only the stored speeds count")
    names = installed_models(server_url)
    if names is None:
        notes.append(f"no Ollama at {server_url}: each value is an estimate")
    installed = {_with_tag(name) for name in names or []}
    absent = "not installed" if names is not None else "no Ollama"
    context = LOWEST_CONTEXT if role is CHAT else 0
    candidates = []
    for tag, entry in entries.items():
        quality = _number(entry.get("quality"))
        if quality is None or entry.get("enabled") is False or entry.get("remote") is True:
            continue
        weights = _number(entry.get("weights_gb"))
        dimension = entry.get("dimension") if isinstance(entry.get("dimension"), int) else None
        base = Candidate(tag, quality, _with_tag(tag) in installed, weights_gb=weights, dimension=dimension)
        moe = entry.get("moe") is True
        if not base.installed:
            candidates.append(_estimated(base, moe, memory, absent))
            continue
        key = calibration_key(server_url, tag, context, context)
        if key is None:
            candidates.append(dataclasses.replace(base, note="the server gives no file of the model"))
            continue
        # *again* only with a measurement: else it would throw away the
        # stored time and put no new one in its place.
        seconds = None if again and measure else load_speed(role.name, key)
        if seconds is None and measure:
            seconds, failure = _measure_and_release(role, server_url, tag, keep_alive, timeout, progress)
            if seconds is None:
                # A model that did not load gets no estimate: the estimate
                # would recommend the model that just failed.
                candidates.append(dataclasses.replace(base, note=failure))
                continue
            store_speed(role.name, key, seconds)
        if seconds is None:
            why = "not measured" if measurable else "not measured (another computer)"
            candidates.append(dataclasses.replace(_estimated(base, moe, memory, why), can_measure=measurable))
            continue
        candidates.append(dataclasses.replace(base, seconds_per_symbol=seconds))
    return recommend(role, candidates, notes)


def _estimated(candidate: Candidate, moe: bool, memory: Memory | None, why: str) -> Candidate:
    """Return *candidate* with the estimate of its place in the memory, and a note that says why it has no time."""
    fit = estimate(candidate.weights_gb, moe, memory)
    if fit is not None:
        return dataclasses.replace(candidate, fit=fit, note=why)
    if memory is None:
        reason = "no estimate: the memory of the machine is not known"
    elif candidate.weights_gb is None:
        reason = "no estimate: the list gives no weights"
    elif memory.ram_mib is None and not memory.unified:
        reason = "no estimate: too large for the GPU, and the RAM of the machine is not known"
    else:
        reason = "too large for this machine (estimate)"
    return dataclasses.replace(candidate, note=f"{why}; {reason}")


def _measure_and_release(role: Role, server_url: str, tag: str, keep_alive: str, timeout: float,
                         progress: Callable[[str], None] | None) -> tuple[float | None, str]:
    """Measure the time of a typical symbol of *tag*; return it, or None and why.

    The models that the server holds are read just before the
    measurement: a model that another client loaded during the loop
    stays loaded.  The release is in ``finally``: Ctrl-C does not leave
    the model in the memory.  The limit: Ctrl-C while the model still
    loads finds no model to release (``/api/ps`` does not list it yet),
    and Ollama has no request that stops a load; the model then stays for
    its ``keep_alive``.
    """
    from .ollama import _with_tag, loaded_models, measure_embed_speed, measure_speed, release_model

    held = {_with_tag(name) for name in loaded_models(server_url) or []}
    if progress is not None:
        progress(f"measuring the speed of {tag}")
    try:
        if role is CHAT:
            result = measure_speed(server_url, tag, LOWEST_CONTEXT, keep_alive=keep_alive, timeout=timeout)
            seconds = result.speed.seconds_per_symbol if result.speed is not None else None
            error, out_of_memory = result.error, result.out_of_memory
        else:
            embedded = measure_embed_speed(server_url, tag, keep_alive=keep_alive, timeout=timeout)
            seconds, error, out_of_memory = embedded.seconds_per_symbol, embedded.error, embedded.out_of_memory
    finally:
        if _with_tag(tag) not in held:
            release_model(server_url, tag)
    if seconds is not None:
        return seconds, ""
    return None, f"{'does not fit in the memory' if out_of_memory else 'the measurement failed'}: {error}"


def quality(candidate: Candidate, role: Role) -> str:
    """Return the quality of *candidate* for a person: a grade with 2 decimals, an MRR with 3."""
    return f"{candidate.quality:.2f}" if role is CHAT else f"{candidate.quality:.3f}"


def rate(candidate: Candidate, role: Role) -> str:
    """Return the speed of *candidate* for a person: measured, or the estimate, or why it has none."""
    if candidate.seconds_per_symbol is not None:
        if role is EMBEDDING:
            return f"{1 / candidate.seconds_per_symbol:.0f} symbols/s"
        return f"{candidate.seconds_per_symbol:.1f} s/symbol"
    if candidate.fit is not None:
        return f"estimate: {candidate.fit.label}"
    return "-"


def verdict(recommendation: Recommendation, tag: str) -> tuple[bool, str]:
    """Return if a model is better than *tag*, and one sentence about *tag* against the recommendation.

    Better: the recommendation is measured and *tag* is not in the two
    bands of the measured models (``Bands.holds``), or the recommendation is an estimate whose
    quality is above the quality of *tag* by more than the tolerance.  A
    model that the list does not hold, or that has no measurement, is not
    compared: the sentence says why.
    """
    found, text = _verdict(recommendation, tag)
    if found and recommendation.role is EMBEDDING:
        # The cost of the change: the vectors of another model do not
        # match, thus the next index run embeds each symbol again.
        text += " (a new embedding model embeds each index again)"
    return found, text


def _verdict(recommendation: Recommendation, tag: str) -> tuple[bool, str]:
    role, choice = recommendation.role, recommendation.model
    current = recommendation.candidate(tag)
    if choice is None:
        return False, f"no recommendation for the {role.name} model ({'; '.join(recommendation.notes) or 'no candidate'})"
    pick = f"{choice.tag} ({quality(choice, role)}, {rate(choice, role)})"
    if current is None:
        return False, f"{tag} is not a local model of the list; the recommendation is {pick}"
    if current.tag == choice.tag:
        return False, f"{tag} is the recommended {role.name} model ({quality(current, role)}, {rate(current, role)})"
    mine = f"{tag} ({quality(current, role)}, {rate(current, role)})"
    if recommendation.estimated:
        better = choice.quality > current.quality + role.quality_tolerance + 1e-9
        where = "to install" if not choice.installed else "not measured"
        return better, (f"{pick}, {where}, is better than {mine}" if better
                        else f"{mine} is as good as the recommendation {pick}")
    if current.seconds_per_symbol is None:
        why = "is not measured (fw-context llm recommend)" if current.can_measure else f"has no time ({current.note})"
        return False, f"{mine} {why}; the recommendation is {pick}"
    if recommendation.measured_bands is not None and recommendation.measured_bands.holds(current):
        return False, f"{mine} is as good as the recommendation {pick}"
    return True, f"{pick} is better than {mine}"


def remote_models(role: Role, provider: str | None = None) -> list[tuple[str, dict]]:
    """Return the external models of *role* of the list (``remote = true``), the best quality first.

    *provider*: only the embedding models of that API provider.  A cloud
    service is a choice next to the local models (decision of the user,
    2026-10-22): on a machine without a GPU it is the only way.  The rule
    of two bands does not rank them with the local models: the code goes
    out, and the user selects it.
    """
    entries, _note = list_entries(role)
    found = [(tag, entry) for tag, entry in entries.items()
             if entry.get("remote") is True and entry.get("enabled") is not False
             and _number(entry.get("quality")) is not None
             and (provider is None or entry.get("provider") == provider)]
    return sorted(found, key=lambda item: -float(item[1]["quality"]))


def price(entry: dict, role: Role) -> str:
    """Return the price of an external model for a person: of a chat model with an off-peak price, the range.

    WHY the range and not the lower price only: the off-peak price is
    the price outside 12:00-18:00 UTC on weekdays, and a run in that
    window pays the higher one.
    """
    if role is CHAT:
        prices = sorted({float(entry[key]) for key in ("usd_per_1k_symbols", "usd_per_1k_symbols_off_peak")
                         if _number(entry.get(key)) is not None})
        if not prices:
            return ""
        shown = f"${prices[0]:.2f}" if len(prices) == 1 else f"${prices[0]:.2f}-{prices[-1]:.2f}"
        return f"{shown}/1000 symbols"
    value = _number(entry.get("usd_per_mtok"))
    return f"${value:.2f}/Mtok" if value is not None else ""


def cloud_alternative(role: Role) -> str:
    """Return one sentence that names the best external model of *role*, its quality, price and host; "" for none."""
    models = remote_models(role)
    if not models:
        return ""
    from urllib.parse import urlparse

    from .providers import provider_spec

    tag, entry = models[0]
    # The host that gets the code: a cloud chat model runs at ollama.com
    # (through the local Ollama), an embedding model at its API provider.
    spec = provider_spec(str(entry.get("provider") or ""))
    api_host = urlparse(spec.base_url).hostname if spec is not None else None
    host = "ollama.com" if role is CHAT else api_host or "an API"
    cost = price(entry, role)
    grade = f"{float(entry['quality']):.2f}" if role is CHAT else f"{float(entry['quality']):.3f}"
    return f"{tag} ({grade}{', ' + cost if cost else ''}; the code goes to {host})"

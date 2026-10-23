"""``fw-context llm recommend`` and the table of the models in the setup: the output of ``llm.recommend``.

The rule and the measurements are ``llm.recommend``; this module prints
them.
"""

from __future__ import annotations

import argparse
import sys

from ..llm.recommend import CHAT, Candidate, Recommendation, quality, rate

#: A chat model below this quality gives weak analyses: the table marks it
#: (decision of the user, 2026-10-21: such a model stays in the list for a
#: machine with little memory, with a warning).
LOW_CHAT_QUALITY = 3.0

#: The line under a numbered list of models of the setup
#: (``_init_interactive.prompt_model`` takes a number or a name).
NUMBER_OR_NAME = "  Give the number of a row, or the name of a model"


def table_lines(recommendation: Recommendation, current: str | None = None, *, numbered: bool = False) -> list[str]:
    """Return the lines of the table of *recommendation*: the best quality first, the marks at the right.

    The marks: ``← recommended``; ``(estimate)`` when the speed is not
    measured on this machine; ``(current)`` for the model of the config.
    *numbered*: each row gets its number of ``table_tags``, for a question
    of the setup that takes a number (``_init_interactive.prompt_model``).
    """
    role = recommendation.role
    if role is CHAT:
        head = "  Chat models (quality 1-5 of the C/C++ analysis; time of a typical symbol at a context of 4096):"
    else:
        head = "  Embedding models (quality MRR@10 of the search, 0-1; symbols for each second):"
    lines = [head]
    if not recommendation.candidates:
        lines.append("    no model of the list")
    current_candidate = recommendation.candidate(current) if current else None
    width = len(str(len(recommendation.candidates)))
    for number, candidate in enumerate(_ordered(recommendation), 1):
        prefix = f"{number:>{width}}. " if numbered else ""
        lines.append("    " + prefix + _row(candidate, recommendation, candidate is current_candidate))
    if numbered and recommendation.candidates:
        lines.append(NUMBER_OR_NAME)
    lines += [f"  [info] {note}" for note in recommendation.notes]
    return lines


def table_tags(recommendation: Recommendation) -> list[str]:
    """Return the tags of the rows of ``table_lines``, in its order: row 1 is the first tag."""
    return [candidate.tag for candidate in _ordered(recommendation)]


def _ordered(recommendation: Recommendation) -> list[Candidate]:
    """The candidates in the order of the table: the best quality first, then the tag."""
    return sorted(recommendation.candidates, key=lambda c: (-c.quality, c.tag))


def _row(candidate: Candidate, recommendation: Recommendation, is_current: bool) -> str:
    role = recommendation.role
    marks = []
    if recommendation.model is not None and candidate.tag == recommendation.model.tag:
        marks.append("← recommended" + ("" if candidate.installed else " (to install)"))
    if is_current:
        marks.append("(current)")
    if role is CHAT and candidate.quality < LOW_CHAT_QUALITY:
        marks.append("low quality")
    extra = []
    if candidate.weights_gb is not None:
        extra.append(f"{candidate.weights_gb:g} GB")
    if candidate.dimension is not None:
        extra.append(f"{candidate.dimension} d")
    if candidate.note:
        extra.append(candidate.note)
    return (f"{candidate.tag:<34} {quality(candidate, role):>5}  {rate(candidate, role):<28} {', '.join(extra)}"
            + (f"  {' '.join(marks)}" if marks else "")).rstrip()


def cmd_llm_recommend(args: argparse.Namespace) -> int:
    """Measure the installed models of the list that have no speed, and print the recommendation of each role.

    Exit codes: 0 = printed, 1 = a config file cannot be used.
    """
    from ..config.settings import ConfigError, _is_loopback_url
    from ..llm.chat_router import chat_ollama_server
    from ..llm.embedder_factory import uses_ollama
    from ..llm.hardware import machine_memory
    from ..llm.recommend import EMBEDDING, gather
    from ._llm import _project_and_config
    from ._llm_wizard import local_url

    try:
        _root, cfg = _project_and_config(args)
    except ConfigError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    memory = machine_memory()
    # The URL of the profile when it is the Ollama of this machine: the key
    # of a speed holds the URL as written, thus the calibration of the
    # profile (http://127.0.0.1:11434) and doctor share the measurements.
    try:
        profile_server = chat_ollama_server(cfg.llm) if cfg.llm.provider == "ollama" else None
    except ValueError:
        profile_server = None
    chat_server = profile_server if profile_server and _is_loopback_url(profile_server) else local_url()
    embed_server = cfg.llm.ollama_url if uses_ollama(cfg.llm) else chat_server

    def progress(text: str) -> None:
        print(f"  [measure] {text}", flush=True)

    for role, server, current in (
        (CHAT, chat_server, cfg.llm.model if cfg.llm.provider == "ollama" else None),
        (EMBEDDING, embed_server, cfg.llm.embed_model or None),
    ):
        recommendation = gather(role, server, memory if _is_loopback_url(server) else None, measure=True,
                                again=args.again, keep_alive=cfg.llm.keep_alive, timeout=cfg.llm.timeout,
                                progress=progress)
        print("\n".join(table_lines(recommendation, current)))
        print()
    print("Select a model with: fw-context llm setup")
    return 0

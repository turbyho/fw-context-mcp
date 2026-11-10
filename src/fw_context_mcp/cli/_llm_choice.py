"""The LLM profile of one run of ``fw-context index`` or ``fw-context analyze``: ``--llm``, or a question at the start.

A project has one profile (the background reindex uses it).  A manual
run can use another one for its LLM analysis, for example a paid API for
a full ``--force-llm`` run (decision of the user, 2026-10-20):

* ``--llm <profile>`` selects it without a question.  A profile that does
  not exist, or that has no key or sign-in, stops the run: a silent
  switch to another profile would send the source code elsewhere.
* Else the question comes at the very start of the run, not after the
  parse of the sources: only on a terminal (stdin and stdout) in the
  foreground, not for ``--background``, only when the run analyses and
  the global config has two profiles or more that can run.  Enter keeps
  the profile of the project, and so does no answer in 60 s.  WHY each
  check: the daemon starts ``index --background`` with the stdin of the
  daemon, which is not always closed; a run started with ``&`` in a shell
  stops at a read (SIGTTIN); an LLM agent can run a command in a pseudo
  terminal and never answer.
* ``--llm`` of a run without the LLM analysis has no effect: a profile
  that cannot run must not stop a run that sends nothing.

The profile changes the chat of the run only; the embeddings stay
(``settings.profile_llm_config``).  No price is shown: the run tells the
usage that the provider reports at its end.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import replace

#: The seconds that the question waits; then the profile of the project applies.
QUESTION_TIMEOUT_S = 60


def may_ask(background: bool) -> bool:
    """Return True when a run can ask for its profile: not a background run, a terminal on stdin and stdout, the foreground."""
    return not background and _stdin_is_terminal() and sys.stdout.isatty() and _in_foreground()


def _stdin_is_terminal() -> bool:
    """Return True when stdin is a terminal that a person can type into.

    Windows: the console (``GetConsoleMode`` succeeds on its handle).  WHY
    not ``isatty``: CPython on Windows gives True for each character
    device, also for ``NUL`` (measured on Windows 11: ``< NUL`` gives
    ``isatty`` True and no console).  ``msvcrt.kbhit`` reads the keyboard
    of the console and not stdin, thus a run with ``< NUL`` waited the
    whole time of the question.
    """
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        mode = wintypes.DWORD()
        try:
            handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        except (OSError, ValueError):
            # No file descriptor (stdin closed, or replaced by an object without one).
            return False
        return bool(kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    return sys.stdin.isatty()


def _in_foreground() -> bool:
    """Return False for a process of a background job of the shell (``&``): its read of the terminal stops it.

    Windows has no job control of the shell, thus no background job that
    a read stops: True.  An ``OSError`` (stdin is no terminal of this
    process) also gives True: ``may_ask`` checks the terminal itself.
    """
    if sys.platform == "win32":
        return True
    try:
        return os.tcgetpgrp(sys.stdin.fileno()) == os.getpgrp()
    except OSError:
        return True


def _ask(prompt: str) -> str:
    """Read the answer; "" when the user gives none in ``QUESTION_TIMEOUT_S``."""
    from . import _init_interactive as ui

    print(prompt, end="", flush=True)
    if not _answer_starts(QUESTION_TIMEOUT_S):
        print(f"\nNo answer in {QUESTION_TIMEOUT_S} s: the run uses the profile of the project")
        return ""
    return ui._input("")


#: The seconds between two looks at the keyboard of the Windows console.
_KEY_POLL_S = 0.05


def _answer_starts(timeout_s: float) -> bool:
    """Return True when the user types into the terminal in *timeout_s*, else False.

    POSIX: ``select`` on stdin.  Windows: ``select`` takes only sockets
    there (measured: ``OSError`` 10093 on the stdin of a console), thus
    the function looks at the keyboard of the console with
    ``msvcrt.kbhit``, which does not take the key: ``input`` then reads
    the whole line (measured on Windows 11 through a pseudo console).
    ``may_ask`` asks on Windows only when stdin is that console.
    """
    if sys.platform == "win32":
        import msvcrt

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                return True
            time.sleep(_KEY_POLL_S)
        return False
    import select

    ready, _, _ = select.select([sys.stdin], [], [], timeout_s)
    return bool(ready)


def index_analyses(args, cfg) -> bool:
    """Return True when an index run makes the LLM analysis: the decision of the run (``_build_run_kwargs``).

    ``--force-llm`` and ``--analyze`` turn it on over ``analyze_symbols =
    false``; ``--no-analyze`` and ``enabled = false`` turn it off.  WHY
    one rule: the question came only from ``analyze_symbols``, thus a full
    ``--force-llm`` run, the main use of another profile, did not ask.
    """
    if not cfg.llm.enabled or getattr(args, "no_analyze", False):
        return False
    return bool(getattr(args, "force_llm", False)) or bool(getattr(args, "analyze", False)) or cfg.llm.analyze_symbols


class RunProfileError(Exception):
    """``--llm`` names a profile that does not exist, or that cannot run."""


def llm_for_run(args, cfg, *, background: bool, analyses: bool, count: str = ""):
    """Return *cfg* with the chat of the profile of this run (``--llm``, or the answer of the question).

    *analyses* is False when the run makes no LLM analysis: no question.
    *count* is a line about the symbols of the run, for the question.

    Raises:
        RunProfileError: ``--llm`` names a profile that does not exist or
            cannot run.  The message names the profiles or the command.
    """
    from ..config.settings import ConfigError
    from ..llm.profile_state import cloud_hosts, profile_state
    from ._llm_wizard import _profiles

    name = getattr(args, "llm", None) or ""
    if name and not analyses:
        print(f"--llm {name} has no effect: this run makes no LLM analysis", file=sys.stderr)
        return cfg
    if not name and (not analyses or not may_ask(background)):
        return cfg
    try:
        profiles = _profiles()
    except ConfigError as exc:
        if name:
            raise RunProfileError(str(exc)) from None
        return cfg
    cloud = cloud_hosts(profiles) if profiles else {}
    states = {key: profile_state(key, profile, cloud=cloud) for key, profile in profiles.items()}
    if name:
        if name not in states:
            raise RunProfileError(
                f"--llm {name}: the global config has no such LLM profile (profiles: {', '.join(states) or 'none'})"
            )
        if states[name].problem is not None:
            raise RunProfileError(f"--llm {name}: {states[name].problem}")
        return _switch(cfg, name, states[name])
    ready = [state for state in states.values() if state.problem is None]
    if len(ready) < 2:
        return cfg
    print("The LLM profile of this run (Enter: the profile of the project):")
    if count:
        print(f"  {count}")
    for number, state in enumerate(ready, 1):
        warning = f"  ⚠ {state.host}" if state.external else ""
        mark = " — Enter" if state.name == cfg.llm.profile else ""
        print(f"  {number}. {state.name}: {state.provider} {state.model}{warning}{mark}")
    answer = _ask("> ")
    if not answer.isdigit() or not 1 <= int(answer) <= len(ready):
        if answer:
            print(f"No profile {answer!r}: the run uses the profile of the project")
        return cfg
    state = ready[int(answer) - 1]
    return cfg if state.name == cfg.llm.profile else _switch(cfg, state.name, state)


def _switch(cfg, name: str, state):
    """Return *cfg* with the chat of the profile *name*; tell where the source code goes.

    Raises:
        RunProfileError: a value of the profile cannot be used (for example
            ``timeout = "slow"``): ``load`` skips such a value of a profile
            that it does not select, thus the error comes here.
    """
    from ..config.settings import ConfigError, profile_llm_config

    try:
        llm = profile_llm_config(cfg.llm, name)
    except (ConfigError, ValueError) as exc:
        raise RunProfileError(f"profile {name!r}: {exc} (see 'fw-context doctor')") from None
    where = f": source code snippets go to {state.host}" if state.external else ""
    print(f"LLM analysis of this run: profile {name!r} ({state.provider} {state.model}){where}", file=sys.stderr)
    return replace(cfg, llm=llm)


def analysis_count_line(conn, builds, *, project_only: bool, force: bool) -> str:
    """Return a line about the definitions that the analysis of *builds* reads, for the question.

    Only facts of the index: the definitions, and those without an
    analysis.  How many go to the model is known only during the run (a
    definition whose body did not change keeps its analysis; a continued
    ``--force-llm`` run skips the ones of the earlier command; a definition
    of more builds goes one time).
    """
    from ..indexer._llm_analysis import count_analysis_candidates

    total = without = 0
    for build in builds:
        build_total, build_without = count_analysis_candidates(conn, build["config_hash"], project_only)
        total += build_total
        without += build_without
    if force:
        return f"--force-llm: up to {total} definitions go to the model"
    return f"{total} definitions, {without} of them without an analysis"

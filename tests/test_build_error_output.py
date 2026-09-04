"""A failed build must say why, not quote the wrapper that noticed.

``run_build_command`` used to report ``result.stderr[:500]`` and throw
``result.stdout`` away. Measured on an Mbed OS build that could not find
its bootloader image: mbed-cli writes its own block to stderr and leaves
the compiler errors on stdout, so those 500 characters held

    [mbed] ERROR: "…/python" returned error.
           Code: 1
           Path: "…"
           Command: "…/make.py … --profile develop --p

— cut mid-word, with no line that said what was wrong. The three
``error: 'BOOTLOADER_SIZE' was not declared in this scope`` lines and the
``Configuration error: Bootloader … not found`` line were all on stdout,
and never reached the reader.

Two rules follow, and both are tested here: quote BOTH streams, and quote
the END of each, because that is where a build tool puts the failure.
"""

from __future__ import annotations

import subprocess

import pytest

from fw_context_mcp.utils import _BUILD_OUTPUT_TAIL, run_build_command


class _Result:
    """What subprocess.run gives back."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_run(monkeypatch, result):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: result)


# ── Both streams ───────────────────────────────────────────────────────


def test_the_reason_on_stdout_reaches_the_message(monkeypatch, tmp_path):
    """The Mbed shape: cause on stdout, wrapper noise on stderr."""
    _fake_run(
        monkeypatch,
        _Result(
            255,
            stdout="Compile [ 50.0%]: main.cpp\n"
            "./src/download_manager.cpp:1693:88: error: 'BOOTLOADER_SIZE' was not declared\n",
            stderr='[mbed] ERROR: "python" returned error.\n       Code: 1\n',
        ),
    )
    with pytest.raises(RuntimeError) as exc:
        run_build_command(["mbed", "compile"], cwd=tmp_path, description="mbed compile")

    message = str(exc.value)
    assert "BOOTLOADER_SIZE" in message, "the reason for the failure is missing"
    assert "[mbed] ERROR" in message, "the wrapper block is useful context"
    assert "Build command failed (exit 255): mbed compile" in message


def test_an_empty_stream_is_left_out(monkeypatch, tmp_path):
    """`stdout:` above nothing is noise in a log people read."""
    _fake_run(monkeypatch, _Result(1, stdout="   \n", stderr="ninja: build stopped.\n"))
    with pytest.raises(RuntimeError) as exc:
        run_build_command(["ninja"], cwd=tmp_path, description="ninja")

    message = str(exc.value)
    assert "stdout" not in message
    assert "ninja: build stopped." in message


def test_a_silent_failure_still_names_the_command(monkeypatch, tmp_path):
    _fake_run(monkeypatch, _Result(2))
    with pytest.raises(RuntimeError, match=r"Build command failed \(exit 2\): make"):
        run_build_command(["make"], cwd=tmp_path, description="make")


# ── The end, not the beginning ─────────────────────────────────────────


def test_the_tail_is_kept_and_the_head_is_dropped(monkeypatch, tmp_path):
    """A compiler prints thousands of progress lines before it fails."""
    noise = "Compile: file.cpp\n" * 4000
    _fake_run(monkeypatch, _Result(1, stdout=noise + "error: the real reason\n"))
    with pytest.raises(RuntimeError) as exc:
        run_build_command(["make"], cwd=tmp_path, description="make")

    message = str(exc.value)
    assert "error: the real reason" in message
    assert len(message) < len(noise), "the whole log must not land in one exception"


def test_a_cut_stream_says_how_much_is_missing(monkeypatch, tmp_path):
    """Nobody may read the first quoted line as the first line of output."""
    whole = "x" * (_BUILD_OUTPUT_TAIL + 500)
    _fake_run(monkeypatch, _Result(1, stderr=whole))
    with pytest.raises(RuntimeError) as exc:
        run_build_command(["make"], cwd=tmp_path, description="make")

    assert f"last {_BUILD_OUTPUT_TAIL} of {len(whole)} characters" in str(exc.value)


def test_a_short_stream_is_quoted_whole(monkeypatch, tmp_path):
    _fake_run(monkeypatch, _Result(1, stderr="fatal error: 'zephyr/kernel.h' file not found"))
    message = ""
    with pytest.raises(RuntimeError) as exc:
        run_build_command(["ninja"], cwd=tmp_path, description="ninja")
    message = str(exc.value)
    assert "characters)" not in message, "a stream that fits must not claim it was cut"
    assert "fatal error: 'zephyr/kernel.h' file not found" in message


# ── A timeout ──────────────────────────────────────────────────────────


def test_a_timeout_quotes_what_the_build_managed_to_say(monkeypatch, tmp_path):
    """A bare "timed out" gives the reader nothing to act on."""

    def _raise(*a, **kw):
        raise subprocess.TimeoutExpired(
            cmd=["make"], timeout=1, output="Compile: a.c\nlinking...\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", _raise)
    with pytest.raises(RuntimeError) as exc:
        run_build_command(["make"], cwd=tmp_path, description="make", timeout=1)

    message = str(exc.value)
    assert "timed out after 1s" in message
    assert "linking..." in message


def test_a_timeout_that_captured_bytes_does_not_raise_again(monkeypatch, tmp_path):
    """TimeoutExpired can carry bytes even when the run asked for text."""

    def _raise(*a, **kw):
        raise subprocess.TimeoutExpired(
            cmd=["make"], timeout=1, output=b"partial \xff output", stderr=None
        )

    monkeypatch.setattr(subprocess, "run", _raise)
    with pytest.raises(RuntimeError) as exc:
        run_build_command(["make"], cwd=tmp_path, description="make", timeout=1)

    assert "partial" in str(exc.value)


# ── Against a real subprocess ──────────────────────────────────────────


def test_a_real_process_that_fails_on_stdout(tmp_path):
    """No mock here.

    Every other test in this file replaces ``subprocess.run``.  A fake
    result that does not match what the real call returns would let the
    defect back in unseen, thus this one drives a real process that
    writes its reason to stdout and exits non-zero — the shape of the
    Mbed failure.
    """
    with pytest.raises(RuntimeError) as exc:
        run_build_command(
            ["sh", "-c", "echo 'error: the real reason'; exit 3"],
            cwd=tmp_path,
            description="sh",
        )

    message = str(exc.value)
    assert "Build command failed (exit 3): sh" in message
    assert "error: the real reason" in message


# ── What reads the message afterwards ──────────────────────────────────


def test_the_first_line_stays_the_progress_line(monkeypatch, tmp_path):
    """read_reindex_progress reports the first line and skips the rest.

    The quoted output must therefore start on a line of its own, or the
    progress that a caller sees ends up holding compiler text.
    """
    _fake_run(monkeypatch, _Result(1, stdout="error: nope\n"))
    with pytest.raises(RuntimeError) as exc:
        run_build_command(["ninja"], cwd=tmp_path, description="ninja (nrf52840-dev)")

    first = str(exc.value).splitlines()[0]
    assert first == "Build command failed (exit 1): ninja (nrf52840-dev)"

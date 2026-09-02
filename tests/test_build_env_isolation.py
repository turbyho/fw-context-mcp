"""A build must not inherit the shell startup hook of whoever started fw-context.

Observed defect: a background reindex could not build, while the same build
from a plain shell passed::

    fw-context: error: Build command failed (exit 126): west build --sysbuild
      (nrf52840-dev)
    stderr: [BLOCKED — DO NOT RETRY] Command uses eval or $()/ backticks at
      command position, which is blocked regardless of allowlist.
    Command: source .../scripts/setup_nordic_env.sh && west build --sysbuild

Cause: ``run_build_command`` wraps an ``activate`` script in ``bash -c
"source <script> && <cmd>"``.  ``bash -c`` reads the file named by BASH_ENV
before it runs the command, and an AI coding harness sets BASH_ENV to a hook
that re-execs the command through its own guarded shell.  The environment
reached the build through the whole chain: harness -> MCP server ->
``background._spawn_daemon`` -> ``daemon._run_index_async`` -> bash.

These tests use a real bash, because the point is what bash does with the
environment — a mock of subprocess.run cannot show that.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from fw_context_mcp.indexer.build import BuildConfig
from fw_context_mcp.utils import build_env, run_build_command

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash is not installed")


@pytest.fixture
def hook(tmp_path: Path) -> tuple[Path, Path]:
    """A BASH_ENV hook that leaves a trace, and the trace file it writes."""
    trace = tmp_path / "hook_ran"
    script = tmp_path / "bash_env_hook.sh"
    script.write_text(f"echo ran > {trace}\n", encoding="utf-8")
    return script, trace


# ── build_env ────────────────────────────────────────────────────────────────


def test_build_env_drops_bash_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASH_ENV", "/somewhere/hook.sh")

    # Assert over the KEYS.  A failed assert on the dict itself prints every
    # value, and the inherited environment holds API tokens — a CI log is
    # not the place for them.
    assert "BASH_ENV" not in set(build_env())


def test_build_env_keeps_everything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASH_ENV", "/somewhere/hook.sh")
    monkeypatch.setenv("ZEPHYR_BASE", "/opt/ncs/zephyr")

    env = build_env()

    assert env["ZEPHYR_BASE"] == "/opt/ncs/zephyr"
    assert "PATH" in env


def test_build_env_overrides_win(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit value is configuration, not inheritance, so it survives."""
    monkeypatch.setenv("BASH_ENV", "/inherited/hook.sh")

    env = build_env({"BASH_ENV": "/configured/hook.sh"})

    assert env["BASH_ENV"] == "/configured/hook.sh"


# ── run_build_command, against a real bash ───────────────────────────────────


@needs_bash
def test_activate_wrap_does_not_run_the_bash_env_hook(
    tmp_path: Path, hook: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported defect: the harness hook fired inside our own bash -c."""
    script, trace = hook
    monkeypatch.setenv("BASH_ENV", str(script))

    activate = tmp_path / "activate.sh"
    activate.write_text("export FW_CONTEXT_PROBE=1\n", encoding="utf-8")
    cfg = BuildConfig(activate=str(activate), timeout=30)

    run_build_command(["true"], cwd=tmp_path, build_cfg=cfg)

    assert not trace.exists(), "BASH_ENV hook ran inside the build's bash -c"


@needs_bash
def test_the_hook_would_have_run_without_the_fix(
    tmp_path: Path, hook: tuple[Path, Path]
) -> None:
    """Proof that the test above tests something.

    The same bash invocation with BASH_ENV left in the environment DOES run
    the hook.  Without this, a build_env() that silently stopped stripping
    would leave the test above passing for the wrong reason.
    """
    script, trace = hook
    assert BASH is not None

    subprocess.run(
        [BASH, "-c", "true"],
        cwd=tmp_path,
        env={"BASH_ENV": str(script), "PATH": "/usr/bin:/bin"},
        timeout=30,
        check=True,
    )

    assert trace.exists(), "the probe is wrong — bash -c did not read BASH_ENV"


@needs_bash
def test_activate_script_still_reaches_the_command(tmp_path: Path) -> None:
    """Stripping the hook must not break what the wrap is for."""
    activate = tmp_path / "activate.sh"
    activate.write_text("export FW_CONTEXT_PROBE=activated\n", encoding="utf-8")
    cfg = BuildConfig(activate=str(activate), timeout=30)

    result = run_build_command(
        ["printenv", "FW_CONTEXT_PROBE"], cwd=tmp_path, build_cfg=cfg
    )

    assert result.stdout.strip() == "activated"


@needs_bash
def test_configured_bash_env_still_reaches_the_build(
    tmp_path: Path, hook: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """extra_env is a declared intent and overrides the strip."""
    script, trace = hook
    monkeypatch.delenv("BASH_ENV", raising=False)

    activate = tmp_path / "activate.sh"
    activate.write_text("true\n", encoding="utf-8")
    cfg = BuildConfig(
        activate=str(activate), timeout=30, extra_env={"BASH_ENV": str(script)}
    )

    run_build_command(["true"], cwd=tmp_path, build_cfg=cfg)

    assert trace.exists(), "a BASH_ENV set in extra_env must reach the build"


def test_plain_command_environment_has_no_bash_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No activate script, so no bash — the environment is clean regardless.

    A build spawns bash on its own in places, so the variable must be gone
    from the whole subprocess tree, not only from the wrap.
    """
    monkeypatch.setenv("BASH_ENV", "/somewhere/hook.sh")
    # Keys only — a failed assert prints what it compared, and the inherited
    # environment holds API tokens.  A CI log is not the place for them.
    seen: set[str] = set()

    def _capture(cmd, **kwargs):  # noqa: ANN001, ANN202 — test double
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _capture)

    run_build_command(["true"], cwd=tmp_path, timeout=30)

    assert seen, "the test double captured no environment at all"
    assert "BASH_ENV" not in seen

"""init asks for the script that sets up the environment of a Zephyr project.

Each build of a Zephyr or NCS project runs after ``source <activate>``, and a
project can have its own script for that.  init offers the script that it
found or generated and takes a different one (decision of the user,
2026-10-24).  The detection of that script runs before the environment of
the project exists, thus it never asks ``west``: a ``west`` from outside the
environment can name a different workspace.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from fw_context_mcp.cli._init_interactive import _choose_activate_script
from fw_context_mcp.indexer.builders.zephyr import ZephyrBuildSystem

_INPUT = "fw_context_mcp.cli._init_interactive._input"


def _answers(monkeypatch: pytest.MonkeyPatch, *answers: str) -> None:
    pending = list(answers)
    monkeypatch.setattr(_INPUT, lambda _: pending.pop(0))


class TestChooseActivateScript:
    def test_enter_keeps_the_offered_script(self, tmp_path: Path, monkeypatch) -> None:
        _answers(monkeypatch, "")

        assert _choose_activate_script(tmp_path, ".fw-context/ncs-env.sh", non_interactive=False) == (
            ".fw-context/ncs-env.sh"
        )

    def test_a_script_of_the_project_replaces_it(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "tools").mkdir()
        (tmp_path / "tools" / "env.sh").write_text("export X=1\n", encoding="utf-8")
        _answers(monkeypatch, "tools/env.sh")

        assert _choose_activate_script(tmp_path, "/gen/ncs-env.sh", non_interactive=False) == "tools/env.sh"

    def test_a_path_that_is_not_a_file_asks_again(self, tmp_path: Path, monkeypatch, capsys) -> None:
        own = tmp_path / "env.sh"
        own.write_text("", encoding="utf-8")
        _answers(monkeypatch, "missing.sh", str(own))

        assert _choose_activate_script(tmp_path, "/gen/ncs-env.sh", non_interactive=False) == str(own)
        assert "missing.sh: no such file." in capsys.readouterr().out

    def test_without_a_terminal_the_offer_stays_and_the_way_to_change_it_shows(self, tmp_path: Path, capsys) -> None:
        assert _choose_activate_script(tmp_path, "/gen/ncs-env.sh", non_interactive=True) == "/gen/ncs-env.sh"
        assert "[build] activate" in capsys.readouterr().out


@pytest.fixture
def west_that_fails(tmp_path: Path, monkeypatch) -> Path:
    """A west on PATH that records each call and fails: detection must not call it."""
    bin_dir = tmp_path / "hostbin"
    bin_dir.mkdir()
    log = tmp_path / "west-calls.log"
    west = bin_dir / "west"
    west.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 1\n', encoding="utf-8")
    west.chmod(west.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]))
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    return log


class TestDetectionAsksNoWest:
    def test_the_preferred_version_does_not_ask_west(self, tmp_path: Path, west_that_fails: Path) -> None:
        assert ZephyrBuildSystem.preferred_ncs_version(tmp_path) is None
        assert not west_that_fails.exists()

    def test_the_environment_detection_does_not_ask_west(self, tmp_path: Path, west_that_fails: Path) -> None:
        assert ZephyrBuildSystem.detect_environment(tmp_path) == {"python": None, "activate": None}
        assert not west_that_fails.exists()

    def test_zephyr_env_of_the_exported_zephyr_base_is_found(
        self, tmp_path: Path, west_that_fails: Path, monkeypatch,
    ) -> None:
        zephyr = tmp_path / "zephyrproject" / "zephyr"
        zephyr.mkdir(parents=True)
        (zephyr / "zephyr-env.sh").write_text("", encoding="utf-8")
        monkeypatch.setenv("ZEPHYR_BASE", str(zephyr))

        detected = ZephyrBuildSystem.detect_environment(tmp_path)

        assert detected["activate"] == str(zephyr / "zephyr-env.sh")
        assert not west_that_fails.exists()

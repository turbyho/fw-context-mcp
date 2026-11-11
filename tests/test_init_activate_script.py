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
from pathlib import Path

import pytest

from fw_context_mcp.cli._init_interactive import _choose_activate_script
from fw_context_mcp.indexer.builders.zephyr import ZephyrBuildSystem
from tests._stubs import WINDOWS, system_path, write_python_tool

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
    log = tmp_path / "west-calls.log"
    write_python_tool(
        bin_dir, "west", f"import sys\nopen({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\nsys.exit(1)\n",
    )
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), *system_path()]))
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
        """zephyr-env.sh on POSIX, zephyr-env.cmd on Windows (the two scripts of the Zephyr repository)."""
        zephyr = tmp_path / "zephyrproject" / "zephyr"
        zephyr.mkdir(parents=True)
        script = "zephyr-env.cmd" if WINDOWS else "zephyr-env.sh"
        (zephyr / script).write_text("", encoding="utf-8")
        monkeypatch.setenv("ZEPHYR_BASE", str(zephyr))

        detected = ZephyrBuildSystem.detect_environment(tmp_path)

        assert detected["activate"] == str(zephyr / script)
        assert not west_that_fails.exists()

    @pytest.mark.skipif(not WINDOWS, reason="the scripts of Windows")
    def test_a_shell_script_is_not_offered_on_windows(self, tmp_path: Path, west_that_fails: Path, monkeypatch) -> None:
        """A .sh activate cannot run on Windows: every build would fail."""
        zephyr = tmp_path / "zephyrproject" / "zephyr"
        zephyr.mkdir(parents=True)
        (zephyr / "zephyr-env.sh").write_text("", encoding="utf-8")
        monkeypatch.setenv("ZEPHYR_BASE", str(zephyr))

        assert ZephyrBuildSystem.detect_environment(tmp_path)["activate"] is None


class TestEspIdfDetection:
    """export.sh on POSIX, export.bat on Windows: the scripts of ESP-IDF."""

    @pytest.fixture
    def home(self, tmp_path: Path, monkeypatch) -> Path:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.delenv("IDF_PATH", raising=False)
        return home

    def test_the_export_script_of_idf_path_is_found(self, tmp_path: Path, home: Path, monkeypatch) -> None:
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        idf = tmp_path / "esp-idf"
        idf.mkdir()
        script = idf / ("export.bat" if WINDOWS else "export.sh")
        script.write_text("", encoding="utf-8")
        monkeypatch.setenv("IDF_PATH", str(idf))

        assert ESPIDFBuildSystem.detect_environment(tmp_path)["activate"] == str(script)

    @pytest.mark.skipif(not WINDOWS, reason="the scripts of Windows")
    def test_a_shell_script_is_not_offered_on_windows(self, tmp_path: Path, home: Path, monkeypatch) -> None:
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        idf = home / "esp" / "esp-idf"
        idf.mkdir(parents=True)
        (idf / "export.sh").write_text("", encoding="utf-8")
        tools = home / ".espressif" / "tools"
        tools.mkdir(parents=True)
        (tools / "activate_idf_v5.2.sh").write_text("", encoding="utf-8")

        assert ESPIDFBuildSystem.detect_environment(tmp_path)["activate"] is None

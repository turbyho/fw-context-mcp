"""Zephyr builder — locating an nrfutil that really is the SDK manager.

``nrfutil`` is a name several unrelated tools answer to.  A pip-installed
``nrfutil`` (the click-based nRF5 utility) has no ``sdk-manager`` command, and
a machine can carry both — measured on the development host, four different
``nrfutil`` binaries were on PATH and only one was the SDK manager.

Picking the wrong one is silent: detection writes an activation script around
it, and every later Zephyr build dies with a bare
``Usage: nrfutil [OPTIONS] COMMAND [ARGS]...`` that names neither the script
nor the binary behind it.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from fw_context_mcp.indexer.builders.zephyr import ZephyrBuildSystem


def _fake_nrfutil(directory: Path, *, works: bool, name: str = "nrfutil") -> Path:
    """Write a fake nrfutil that either answers to sdk-manager or does not.

    The working one mimics the real binary's ``sdk-manager --version`` reply;
    the broken one mimics the click-based tool, which prints a usage line and
    exits non-zero because it has no such command.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if works:
        body = (
            "#!/bin/sh\n"
            'if [ "$1" = "sdk-manager" ]; then\n'
            '  echo "nrfutil-sdk-manager 1.16.1"\n'
            "  exit 0\n"
            "fi\n"
            "exit 1\n"
        )
    else:
        body = (
            "#!/bin/sh\n"
            'echo "Usage: nrfutil [OPTIONS] COMMAND [ARGS]..." >&2\n'
            "exit 2\n"
        )
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def isolated_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point PATH and HOME at tmp_path so the host's binaries stay out."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return tmp_path


class TestProvidesSdkManager:
    def test_a_working_binary_is_accepted(self, tmp_path: Path):
        binary = _fake_nrfutil(tmp_path / "good", works=True)
        assert ZephyrBuildSystem._provides_sdk_manager(str(binary)) is True

    def test_a_binary_without_the_command_is_rejected(self, tmp_path: Path):
        binary = _fake_nrfutil(tmp_path / "bad", works=False)
        assert ZephyrBuildSystem._provides_sdk_manager(str(binary)) is False

    def test_a_missing_binary_is_rejected(self, tmp_path: Path):
        assert ZephyrBuildSystem._provides_sdk_manager(str(tmp_path / "nope")) is False


class TestFindNrfutil:
    def test_the_first_hit_on_path_does_not_win_when_it_cannot_do_the_job(
        self, isolated_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The defect: shutil.which stops at the first match, valid or not."""
        bad = _fake_nrfutil(isolated_path / "first", works=False)
        good = _fake_nrfutil(isolated_path / "second", works=True)
        monkeypatch.setenv("PATH", os.pathsep.join([str(bad.parent), str(good.parent)]))

        assert ZephyrBuildSystem._find_nrfutil() == str(good)

    def test_none_when_no_candidate_provides_the_command(
        self, isolated_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        bad = _fake_nrfutil(isolated_path / "only", works=False)
        monkeypatch.setenv("PATH", str(bad.parent))

        assert ZephyrBuildSystem._find_nrfutil() is None

    def test_none_when_nothing_is_installed(
        self, isolated_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("PATH", str(isolated_path / "does-not-exist"))
        assert ZephyrBuildSystem._find_nrfutil() is None

    def test_a_real_binary_beats_a_shim_that_also_works(
        self, isolated_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A shim answers the probe now and can dispatch elsewhere later.

        The path chosen here is baked into the generated activation script, so
        a redirector that re-resolves the name at build time — when the
        environment may select another interpreter version — must lose to a
        binary that cannot change under us.
        """
        shim = _fake_nrfutil(isolated_path / "pyenv" / "shims", works=True)
        real = _fake_nrfutil(isolated_path / "opt" / "bin", works=True)
        # Shim first on PATH, which is exactly how pyenv installs itself.
        monkeypatch.setenv("PATH", os.pathsep.join([str(shim.parent), str(real.parent)]))

        assert ZephyrBuildSystem._find_nrfutil() == str(real)

    def test_a_shim_is_still_used_when_it_is_all_there_is(
        self, isolated_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        shim = _fake_nrfutil(isolated_path / "pyenv" / "shims", works=True)
        monkeypatch.setenv("PATH", str(shim.parent))

        assert ZephyrBuildSystem._find_nrfutil() == str(shim)

    def test_the_standard_install_locations_are_searched_after_path(
        self, isolated_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """~/ncs_tools/nrfutil is found even when PATH holds nothing usable."""
        bad = _fake_nrfutil(isolated_path / "first", works=False)
        monkeypatch.setenv("PATH", str(bad.parent))
        good = _fake_nrfutil(isolated_path / "home" / "ncs_tools", works=True)

        assert ZephyrBuildSystem._find_nrfutil() == str(good)

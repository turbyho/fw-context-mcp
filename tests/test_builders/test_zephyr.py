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
from tests._stubs import WINDOWS, write_python_tool


def _fake_nrfutil(directory: Path, *, works: bool, name: str = "nrfutil") -> Path:
    """Write a fake nrfutil that either answers to sdk-manager or does not.

    The working one mimics the real binary's ``sdk-manager --version`` reply;
    the broken one mimics the click-based tool, which prints a usage line and
    exits non-zero because it has no such command.  The form of the host:
    a file with a shebang, or ``nrfutil.exe`` on Windows (``tests/_stubs.py``).
    """
    if works:
        code = (
            "import sys\n"
            "if sys.argv[1:2] == ['sdk-manager']:\n"
            "    print('nrfutil-sdk-manager 1.16.1')\n"
            "    sys.exit(0)\n"
            "sys.exit(1)\n"
        )
    else:
        code = "import sys\nprint('Usage: nrfutil [OPTIONS] COMMAND [ARGS]...', file=sys.stderr)\nsys.exit(2)\n"
    return write_python_tool(directory, name, code)


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
        """~/.nrfutil/bin/nrfutil is found even when PATH holds nothing usable."""
        bad = _fake_nrfutil(isolated_path / "first", works=False)
        monkeypatch.setenv("PATH", str(bad.parent))
        good = _fake_nrfutil(isolated_path / "home" / ".nrfutil" / "bin", works=True)

        assert ZephyrBuildSystem._find_nrfutil() == str(good)


class TestNcsEnvScript:
    """The activate script that init writes for NCS: ncs-env.sh, or ncs-env.cmd on Windows."""

    @staticmethod
    def _fake_toolchain_env(directory: Path, *, batch: bool = False) -> Path:
        """An nrfutil that answers ``sdk-manager toolchain env --as-script`` with a script of the host."""
        code = (
            "import sys\n"
            "args = sys.argv[1:]\n"
            "assert args[:3] == ['sdk-manager', 'toolchain', 'env'], args\n"
            "assert args[args.index('--ncs-version') + 1] == 'v3.2.3', args\n"
            "if '--as-script' in args and args[args.index('--as-script') + 1:] == ['cmd']:\n"
            "    print('set \"FWCTX_TOOLCHAIN=ok\"')\n"
            "else:\n"
            "    print('export FWCTX_TOOLCHAIN=ok')\n"
        )
        return write_python_tool(directory, "nrfutil", code, batch=batch)

    @pytest.mark.parametrize("batch", [False, pytest.param(True, marks=pytest.mark.skipif(
        not WINDOWS, reason="a batch shim is a form of Windows"))], ids=["program", "batch-shim"])
    def test_the_script_sets_the_toolchain_and_zephyr_base(self, tmp_path: Path, batch: bool):
        """The script runs as each build runs it: through activate.

        A batch shim of nrfutil (nrfutil.bat) must run through `call`: without
        it the shim ended ncs-env.cmd, and the build got no toolchain.
        """
        import sys

        from fw_context_mcp.indexer.build import BuildConfig
        from fw_context_mcp.utils import run_build_command

        nrfutil = self._fake_toolchain_env(tmp_path / "bin", batch=batch)
        project = tmp_path / "project"
        project.mkdir()
        script = ZephyrBuildSystem._write_ncs_env_script(project, str(nrfutil), "v3.2.3", str(tmp_path / "ncs"))
        assert Path(script).name == ("ncs-env.cmd" if WINDOWS else "ncs-env.sh")
        expected_base = str(Path(tmp_path / "ncs", "v3.2.3", "zephyr")) if WINDOWS else f"{tmp_path / 'ncs'}/v3.2.3/zephyr"
        probe = [
            sys.executable, "-c",
            "import os, sys; e = os.environ; "
            f"sys.exit(0 if e['FWCTX_TOOLCHAIN'] == 'ok' and e['ZEPHYR_BASE'] == {expected_base!r} else 1)",
        ]

        result = run_build_command(probe, cwd=project, build_cfg=BuildConfig(activate=script))

        assert result.returncode == 0
        assert not list(Path(script).parent.glob(".ncs-toolchain-env-*")), "the script must remove its temporary file"

    def test_the_same_text_is_not_written_again(self, tmp_path: Path):
        """On Windows text mode turned an explicit \\r\\n into \\r\\r\\n, and each detection wrote the script."""
        project = tmp_path / "project"
        project.mkdir()
        script = Path(ZephyrBuildSystem._write_ncs_env_script(project, "nrfutil", "v3.2.3", str(tmp_path / "ncs")))
        written = script.stat().st_mtime_ns
        os.utime(script, ns=(written - 10**9, written - 10**9))

        ZephyrBuildSystem._write_ncs_env_script(project, "nrfutil", "v3.2.3", str(tmp_path / "ncs"))

        assert script.stat().st_mtime_ns == written - 10**9

    def test_a_new_version_replaces_the_script(self, tmp_path: Path):
        """os.replace: Path.rename on Windows refuses a target that exists."""
        project = tmp_path / "project"
        project.mkdir()
        first = ZephyrBuildSystem._write_ncs_env_script(project, "nrfutil", "v3.2.3", str(tmp_path / "ncs"))
        second = ZephyrBuildSystem._write_ncs_env_script(project, "nrfutil", "v3.4.0", str(tmp_path / "ncs"))

        assert first == second
        assert "v3.4.0" in Path(second).read_text(encoding="utf-8")

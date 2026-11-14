"""``fw-context init`` on a Zephyr project in the shell of an upstream Zephyr workspace.

Zephyr is also used alone, without the nRF Connect SDK.  The shell of such a
workspace sets ``ZEPHYR_BASE`` (``zephyr-env.sh``) and has the ``west`` of the
workspace venv on PATH.  The Zephyr SDK 1.0 has no script
``environment-setup-*``, thus init found nothing in the environment, and on a
machine with NCS it gave the project the environment of an NCS version
(measured with SDK 1.0.1 and Zephyr v4.5.0, 2026-11-13).

init now writes the environment of the shell into ``local.toml``: ``[build]
env`` and ``[build] extra_path``.  A build in the background (the daemon of an
MCP client) does not have the environment of the shell.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fw_context_mcp.cli import _init_interactive
from fw_context_mcp.config import load as load_config
from fw_context_mcp.config.settings import read_config_file
from fw_context_mcp.indexer.builders.zephyr import (
    SDK_KIND_NCS,
    SdkChoice,
    ZephyrBuildSystem,
)
from fw_context_mcp.utils import build_cfg_env
from tests._stubs import write_python_tool
from tests._toml import toml_string


def _ncs(path: Path) -> SdkChoice:
    return SdkChoice(kind=SDK_KIND_NCS, version="v3.2.3", path=path, toolchain_path=path / "tc", usable=True)


def _no_ncs_listing() -> list[SdkChoice]:
    raise AssertionError("init ran nrfutil, though the shell has no ZEPHYR_BASE")


def _upstream(ncs_installs=None):
    """``upstream_zephyr_from_environment`` with the NCS listing of the test (the fixture ``shell`` patches it)."""
    return ZephyrBuildSystem.upstream_zephyr_from_environment(
        ncs_installs if ncs_installs is not None else ZephyrBuildSystem.list_installed_ncs,
    )


@pytest.fixture
def shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """The shell of an upstream workspace: ``ZEPHYR_BASE``, the SDK and ``west`` of the venv on PATH.

    PATH holds only the venv: a ``west`` of the host must not answer for the
    one of the workspace.  No NCS is installed, and the SDK has no script
    ``environment-setup-*`` (SDK 1.0).
    """
    zephyr_base = tmp_path / "ws" / "zephyr"
    zephyr_base.mkdir(parents=True)
    sdk = tmp_path / "zephyr-sdk-1.0.1"
    sdk.mkdir()
    west = write_python_tool(tmp_path / "ws" / ".venv" / "bin", "west", "")
    monkeypatch.setenv("ZEPHYR_BASE", str(zephyr_base))
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(sdk))
    monkeypatch.setenv("ZEPHYR_TOOLCHAIN_VARIANT", "zephyr")
    monkeypatch.setenv("PATH", str(west.parent))
    monkeypatch.setattr(ZephyrBuildSystem, "list_installed_ncs", classmethod(lambda cls: []))
    return {"zephyr_base": zephyr_base, "sdk": sdk, "west_dir": west.parent}


class TestUpstreamZephyrFromEnvironment:
    def test_the_shell_names_the_workspace(self, shell):
        upstream = _upstream()

        assert upstream is not None
        assert upstream.zephyr_base == shell["zephyr_base"]
        assert upstream.west_dir == shell["west_dir"]
        assert upstream.variables == {
            "ZEPHYR_BASE": str(shell["zephyr_base"]),
            "ZEPHYR_SDK_INSTALL_DIR": str(shell["sdk"]),
            "ZEPHYR_TOOLCHAIN_VARIANT": "zephyr",
        }

    def test_only_the_variables_that_the_shell_sets_are_taken(self, shell, monkeypatch):
        monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR")
        monkeypatch.delenv("ZEPHYR_TOOLCHAIN_VARIANT")

        upstream = _upstream()

        assert upstream is not None
        assert upstream.variables == {"ZEPHYR_BASE": str(shell["zephyr_base"])}

    def test_without_west_on_path_there_is_no_west_dir(self, shell, tmp_path, monkeypatch):
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))

        upstream = _upstream()

        assert upstream is not None
        assert upstream.west_dir is None

    def test_without_zephyr_base_there_is_no_workspace_and_no_nrfutil_run(self, shell, monkeypatch):
        monkeypatch.delenv("ZEPHYR_BASE")

        assert _upstream(_no_ncs_listing) is None

    def test_a_zephyr_base_that_is_no_directory_is_no_workspace(self, shell, tmp_path, monkeypatch):
        monkeypatch.setenv("ZEPHYR_BASE", str(tmp_path / "gone"))

        assert _upstream(_no_ncs_listing) is None

    def test_a_zephyr_base_in_an_install_of_ncs_is_ncs(self, shell, tmp_path, monkeypatch):
        """The NCS picker keeps an NCS shell: it reads the version from ``ZEPHYR_BASE``."""
        install = tmp_path / "ncs" / "v3.2.3"
        (install / "zephyr").mkdir(parents=True)
        monkeypatch.setenv("ZEPHYR_BASE", str(install / "zephyr"))

        assert _upstream(lambda: [_ncs(install)]) is None

    def test_an_ncs_install_elsewhere_does_not_hide_the_workspace(self, shell, tmp_path, monkeypatch):
        install = tmp_path / "ncs" / "v3.2.3"
        install.mkdir(parents=True)

        assert _upstream(lambda: [_ncs(install)]) is not None

    def test_a_relative_path_of_the_shell_becomes_absolute(self, shell, monkeypatch):
        """A build runs in a different working directory than the shell (the out dir, the daemon)."""
        monkeypatch.chdir(shell["zephyr_base"].parent)
        monkeypatch.setenv("ZEPHYR_BASE", "zephyr")
        monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", os.path.relpath(shell["sdk"]))

        upstream = _upstream()

        assert upstream is not None
        assert upstream.variables["ZEPHYR_BASE"] == str(shell["zephyr_base"])
        # absolute() and no lexical clean-up of "..": with a link in the
        # path, the clean-up can name a different directory.
        sdk = Path(upstream.variables["ZEPHYR_SDK_INSTALL_DIR"])
        assert sdk.is_absolute()
        assert sdk.resolve() == shell["sdk"].resolve()

    def test_an_sdk_dir_that_is_no_directory_is_not_taken(self, shell, tmp_path, monkeypatch):
        gone = str(tmp_path / "gone-sdk")
        monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", gone)

        upstream = _upstream()

        assert upstream is not None
        assert "ZEPHYR_SDK_INSTALL_DIR" not in upstream.variables
        assert upstream.unusable_sdk_dir == gone


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".fw-context").mkdir(parents=True)
    return root


def _local(project: Path) -> dict:
    return read_config_file(project / ".fw-context" / "local.toml")


def _resolve(project: Path, *, dry_run: bool = False, non_interactive: bool = True) -> None:
    _init_interactive.resolve_build_env(
        project, load_config(project), "zephyr", non_interactive=non_interactive, dry_run=dry_run,
    )


@pytest.fixture
def no_ncs_picker(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record each call of the NCS picker: the upstream workspace must not reach it."""
    calls: list[str] = []

    def picker(project_root, **kwargs):
        calls.append(str(project_root))
        kwargs["ncs_installs"]()
        return None

    monkeypatch.setattr(_init_interactive, "_configure_ncs_version", picker)
    # The scan that comes after a picker without an answer: it reads the
    # host (nrfutil, ~/ncs), and a test must not depend on the host.
    monkeypatch.setattr(
        ZephyrBuildSystem, "detect_environment",
        classmethod(lambda cls, root: {"python": None, "activate": None}),
    )
    return calls


class TestInitWritesTheEnvironment:
    def test_env_and_extra_path_go_into_local_toml(self, shell, project, no_ncs_picker, capsys):
        _resolve(project)

        build = _local(project)["build"]
        assert build["env"] == {
            "ZEPHYR_BASE": str(shell["zephyr_base"]),
            "ZEPHYR_SDK_INSTALL_DIR": str(shell["sdk"]),
            "ZEPHYR_TOOLCHAIN_VARIANT": "zephyr",
        }
        assert build["extra_path"] == [str(shell["west_dir"])]
        assert "activate" not in build
        assert no_ncs_picker == []
        assert f"Zephyr of the environment: {shell['zephyr_base']}" in capsys.readouterr().out

    def test_a_build_gets_the_environment_without_the_shell(self, shell, project, no_ncs_picker, monkeypatch):
        """The config, and not the shell, gives a build its environment (the daemon of an MCP client)."""
        _resolve(project)
        for name in ("ZEPHYR_BASE", "ZEPHYR_SDK_INSTALL_DIR", "ZEPHYR_TOOLCHAIN_VARIANT"):
            monkeypatch.delenv(name)

        env = build_cfg_env(load_config(project).build)

        assert env["ZEPHYR_BASE"] == str(shell["zephyr_base"])
        assert env["PATH"].split(os.pathsep)[0] == str(shell["west_dir"])

    def test_the_env_of_config_toml_is_not_copied(self, shell, project, no_ncs_picker):
        """The layers merge ``env`` key by key: a copy in local.toml would hide a later change of config.toml."""
        (project / ".fw-context" / "config.toml").write_text(
            '[build.env]\nCCACHE_DISABLE = "1"\n', encoding="utf-8",
        )
        (project / ".fw-context" / "local.toml").write_text(
            '[build.env]\nMY_VAR = "x"\n', encoding="utf-8",
        )

        _resolve(project)

        env = _local(project)["build"]["env"]
        assert env["ZEPHYR_BASE"] == str(shell["zephyr_base"])
        assert env["MY_VAR"] == "x"
        assert "CCACHE_DISABLE" not in env
        assert load_config(project).build.env["CCACHE_DISABLE"] == "1"

    def test_an_extra_path_that_has_west_stays_as_it_is(self, shell, project, no_ncs_picker):
        (project / ".fw-context" / "config.toml").write_text(
            f"[build]\nextra_path = [{toml_string(str(shell['west_dir']))}]\n", encoding="utf-8",
        )

        _resolve(project)

        assert "extra_path" not in _local(project)["build"]

    def test_an_extra_path_of_config_toml_stays_effective(self, shell, project, no_ncs_picker, tmp_path):
        """A list of local.toml replaces the list of config.toml, thus the entry must come along."""
        tools = str(tmp_path / "tools")
        (project / ".fw-context" / "config.toml").write_text(
            f"[build]\nextra_path = [{toml_string(tools)}]\n", encoding="utf-8",
        )

        _resolve(project)

        assert load_config(project).build.extra_path == [str(shell["west_dir"]), tools]

    def test_a_dry_run_writes_nothing(self, shell, project, no_ncs_picker, capsys):
        _resolve(project, dry_run=True)

        assert _local(project) == {}
        assert "[dry-run]" in capsys.readouterr().out

    def test_without_west_init_warns(self, shell, project, no_ncs_picker, tmp_path, monkeypatch, capsys):
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))

        _resolve(project)

        assert "extra_path" not in _local(project)["build"]
        assert "No west on PATH" in capsys.readouterr().out

    def test_a_configured_env_is_kept(self, shell, project, no_ncs_picker, tmp_path, capsys):
        other = tmp_path / "other" / "zephyr"
        other.mkdir(parents=True)
        local = project / ".fw-context" / "local.toml"
        local.write_text(f"[build.env]\nZEPHYR_BASE = {toml_string(str(other))}\n", encoding="utf-8")

        _resolve(project)

        assert _local(project)["build"] == {"env": {"ZEPHYR_BASE": str(other)}}
        assert "(already configured)" in capsys.readouterr().out
        assert no_ncs_picker == []


class TestARunOfInitAgain:
    def test_west_of_the_venv_is_added_on_the_next_run(self, shell, project, no_ncs_picker, tmp_path, monkeypatch):
        """The warning tells the user to run init again with the venv active: that run must add west."""
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        _resolve(project)
        monkeypatch.setenv("PATH", str(shell["west_dir"]))

        _resolve(project)

        assert _local(project)["build"]["extra_path"] == [str(shell["west_dir"])]
        assert no_ncs_picker == []

    def test_a_second_run_changes_nothing(self, shell, project, no_ncs_picker):
        _resolve(project)
        before = (project / ".fw-context" / "local.toml").read_bytes()

        _resolve(project)

        assert (project / ".fw-context" / "local.toml").read_bytes() == before

    def test_an_extra_path_of_an_other_spelling_is_the_same_directory(self, shell, project, no_ncs_picker):
        spelled = str(shell["west_dir"] / ".." / shell["west_dir"].name)
        (project / ".fw-context" / "config.toml").write_text(
            f"[build]\nextra_path = [{toml_string(spelled)}]\n", encoding="utf-8",
        )

        _resolve(project)

        assert "extra_path" not in _local(project)["build"]

    def test_the_answer_yes_switches_to_the_workspace_of_the_shell(
        self, shell, project, no_ncs_picker, tmp_path, monkeypatch,
    ):
        other = tmp_path / "other" / "zephyr"
        other.mkdir(parents=True)
        (project / ".fw-context" / "local.toml").write_text(
            f'[build.env]\nZEPHYR_BASE = {toml_string(str(other))}\nZEPHYR_SDK_INSTALL_DIR = "/old/sdk"\nMY_VAR = "x"\n',
            encoding="utf-8",
        )
        monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR")
        monkeypatch.setattr(_init_interactive, "_input", lambda prompt: "y")

        _resolve(project, non_interactive=False)

        # The SDK of the old workspace goes: the shell of the new one sets none.
        assert _local(project)["build"]["env"] == {
            "ZEPHYR_BASE": str(shell["zephyr_base"]),
            "ZEPHYR_TOOLCHAIN_VARIANT": "zephyr",
            "MY_VAR": "x",
        }

    def test_the_answer_no_keeps_the_configured_workspace(self, shell, project, no_ncs_picker, tmp_path, monkeypatch):
        other = tmp_path / "other" / "zephyr"
        other.mkdir(parents=True)
        (project / ".fw-context" / "local.toml").write_text(
            f"[build.env]\nZEPHYR_BASE = {toml_string(str(other))}\n", encoding="utf-8",
        )
        monkeypatch.setattr(_init_interactive, "_input", lambda prompt: "")

        _resolve(project, non_interactive=False)

        assert _local(project)["build"] == {"env": {"ZEPHYR_BASE": str(other)}}

    def test_a_value_of_config_toml_wins_over_the_shell(self, shell, project, no_ncs_picker, capsys):
        """The team selected the toolchain: the shell of one developer must not change it in silence."""
        (project / ".fw-context" / "config.toml").write_text(
            '[build.env]\nZEPHYR_TOOLCHAIN_VARIANT = "gnuarmemb"\n', encoding="utf-8",
        )

        _resolve(project)

        assert "ZEPHYR_TOOLCHAIN_VARIANT" not in _local(project)["build"]["env"]
        assert load_config(project).build.env["ZEPHYR_TOOLCHAIN_VARIANT"] == "gnuarmemb"
        assert "ZEPHYR_TOOLCHAIN_VARIANT=gnuarmemb (already configured, the shell has zephyr)" in capsys.readouterr().out


class TestTheNcsPickerStays:
    def test_an_ncs_shell_goes_to_the_picker(self, shell, project, no_ncs_picker, tmp_path, monkeypatch):
        install = tmp_path / "ncs" / "v3.2.3"
        (install / "zephyr").mkdir(parents=True)
        monkeypatch.setenv("ZEPHYR_BASE", str(install / "zephyr"))
        monkeypatch.setattr(ZephyrBuildSystem, "list_installed_ncs", classmethod(lambda cls: [_ncs(install)]))

        _resolve(project)

        assert no_ncs_picker == [str(project)]
        assert "env" not in _local(project).get("build", {})

    def test_init_runs_nrfutil_once(self, shell, project, no_ncs_picker, tmp_path, monkeypatch):
        """The upstream check and the NCS picker share one ``nrfutil sdk-manager list``."""
        install = tmp_path / "ncs" / "v3.2.3"
        (install / "zephyr").mkdir(parents=True)
        monkeypatch.setenv("ZEPHYR_BASE", str(install / "zephyr"))
        runs: list[int] = []

        def listing(cls) -> list[SdkChoice]:
            runs.append(1)
            return [_ncs(install)]

        monkeypatch.setattr(ZephyrBuildSystem, "list_installed_ncs", classmethod(listing))

        _resolve(project)

        assert runs == [1]

    def test_a_workspace_with_an_sdk_0x_gets_the_env_too(self, shell, project, no_ncs_picker):
        """The script ``environment-setup-*`` of an SDK 0.x puts no ``west`` on PATH and sets no ``ZEPHYR_BASE``."""
        (shell["sdk"] / "environment-setup-x86_64-pokysdk-linux").write_text("", encoding="utf-8")

        _resolve(project)

        assert _local(project)["build"]["env"]["ZEPHYR_BASE"] == str(shell["zephyr_base"])
        assert no_ncs_picker == []

    def test_an_sdk_without_a_workspace_goes_to_the_picker(self, shell, project, no_ncs_picker, monkeypatch):
        """Without ``ZEPHYR_BASE``, ``_configure_ncs_version`` takes the script of the SDK, as before."""
        monkeypatch.delenv("ZEPHYR_BASE")

        _resolve(project)

        assert no_ncs_picker == [str(project)]
        assert "env" not in _local(project).get("build", {})


def test_extra_path_of_the_config_is_read(project, tmp_path):
    """``[build] extra_path`` was not in the fields of the loader: each value was silently lost."""
    tools = str(tmp_path / "tools")
    (project / ".fw-context" / "local.toml").write_text(f"[build]\nextra_path = [{toml_string(tools)}]\n", encoding="utf-8")

    assert load_config(project).build.extra_path == [tools]

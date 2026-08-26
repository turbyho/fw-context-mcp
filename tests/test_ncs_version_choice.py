"""Which nRF Connect SDK to build with is a choice, not a detectable fact.

The version is an argument to ``nrfutil sdk-manager toolchain env``, and that
command is what creates the environment — before it runs there is no
ZEPHYR_BASE and no toolchain on PATH.  A machine with several SDKs installed
therefore offers nothing to detect, and the code used to answer "the newest
directory under ~/ncs".  That builds the index against different headers and
different macros than the developer compiles with, which for a tool whose
job is "what actually compiles for this configuration" is the one thing it
must not get wrong.

Worse, the two signals that DO carry a version —
``ZEPHYR_BASE=<root>/vX.Y.Z/zephyr`` and ``west config zephyr.base`` — were
reduced to ``<root>`` and the version thrown away before the guess.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fw_context_mcp.cli._init_interactive import prompt_ncs_version
from fw_context_mcp.indexer.builders.zephyr import (
    SDK_KIND_NCS,
    SDK_KIND_ZEPHYR,
    SdkChoice,
    ZephyrBuildSystem,
)


def _install(version: str, *, usable: bool = True) -> SdkChoice:
    return SdkChoice(
        kind=SDK_KIND_NCS,
        version=version,
        path=Path(f"/home/u/ncs/{version}"),
        toolchain_path=Path("/home/u/ncs/toolchains/abc") if usable else None,
        usable=usable,
    )


class TestVersionFromEnvironment:
    """The version in the path must survive, not be re-guessed."""

    def test_zephyr_base_names_the_version(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ZEPHYR_BASE", "/home/u/ncs/v3.2.3/zephyr")
        assert ZephyrBuildSystem._zephyr_base_version() == "v3.2.3"

    def test_a_newer_sibling_does_not_win(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """The defect: v3.4.0 installed alongside made v3.2.3 answer v3.4.0."""
        ncs = tmp_path / "ncs"
        for version in ("v3.2.3", "v3.4.0"):
            (ncs / version / "zephyr").mkdir(parents=True)
        monkeypatch.setenv("ZEPHYR_BASE", str(ncs / "v3.2.3" / "zephyr"))

        assert ZephyrBuildSystem._zephyr_base_version() == "v3.2.3"
        # What the old code did instead, kept as the contrast:
        assert ZephyrBuildSystem._ncs_version(ncs) == "v3.4.0"

    def test_no_zephyr_base_means_no_preference(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ZEPHYR_BASE", raising=False)
        assert ZephyrBuildSystem._zephyr_base_version() is None

    @pytest.mark.parametrize(
        "value",
        ["/home/u/ncs/v3.2.3", "/home/u/somewhere/zephyr", "", "/zephyr"],
    )
    def test_a_path_of_another_shape_is_not_read_as_a_version(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ):
        monkeypatch.setenv("ZEPHYR_BASE", value)
        assert ZephyrBuildSystem._zephyr_base_version() is None


class TestListInstalled:
    @staticmethod
    def _fake_nrfutil(tmp_path: Path, payload: str) -> str:
        script = tmp_path / "nrfutil"
        script.write_text(
            "#!/bin/sh\ncat <<'JSON'\n" + payload + "\nJSON\n", encoding="utf-8"
        )
        script.chmod(0o755)
        return str(script)

    def test_versions_and_toolchains_are_read(self, tmp_path: Path):
        payload = json.dumps({"type": "info", "data": {"versions": [
            {"version": "v3.4.0", "dirNames": ["/home/u/ncs/v3.4.0"],
             "toolchainPath": "/home/u/ncs/toolchains/fbf", "sdkStatus": "installed",
             "toolchainStatus": "installed"},
            {"version": "v3.2.3", "dirNames": ["/home/u/ncs/v3.2.3"],
             "toolchainPath": "/home/u/ncs/toolchains/2ac", "sdkStatus": "installed",
             "toolchainStatus": "installed"},
        ]}})
        installs = ZephyrBuildSystem.list_installed_ncs(
            self._fake_nrfutil(tmp_path, payload)
        )
        assert [i.version for i in installs] == ["v3.4.0", "v3.2.3"]
        assert all(i.usable for i in installs)
        assert installs[0].path == Path("/home/u/ncs/v3.4.0")

    def test_a_version_without_its_toolchain_is_not_usable(self, tmp_path: Path):
        """Offering it would only defer the failure to the build."""
        payload = json.dumps({"type": "info", "data": {"versions": [
            {"version": "v3.2.1", "dirNames": ["/home/u/ncs/v3.2.1"],
             "sdkStatus": "installed", "toolchainStatus": "not installed"},
        ]}})
        installs = ZephyrBuildSystem.list_installed_ncs(
            self._fake_nrfutil(tmp_path, payload)
        )
        assert [i.usable for i in installs] == [False]

    def test_unparseable_output_yields_nothing(self, tmp_path: Path):
        installs = ZephyrBuildSystem.list_installed_ncs(
            self._fake_nrfutil(tmp_path, "not json at all")
        )
        assert installs == []

    def test_a_missing_binary_yields_nothing(self, tmp_path: Path):
        assert ZephyrBuildSystem.list_installed_ncs(str(tmp_path / "nope")) == []


class TestPrompt:
    def test_one_usable_sdk_is_taken_without_asking(self):
        """Nothing to choose, so nothing to ask."""
        chosen = prompt_ncs_version(
            [_install("v3.2.3")], default=None, non_interactive=False
        )
        assert chosen == "v3.2.3"

    def test_unusable_sdks_are_not_offered(self):
        chosen = prompt_ncs_version(
            [_install("v3.4.0", usable=False), _install("v3.2.3")],
            default=None, non_interactive=False,
        )
        assert chosen == "v3.2.3"

    def test_nothing_usable_gives_no_answer(self):
        assert prompt_ncs_version(
            [_install("v3.4.0", usable=False)], default=None, non_interactive=False
        ) is None

    def test_non_interactive_takes_the_projects_own_version(self):
        chosen = prompt_ncs_version(
            [_install("v3.4.0"), _install("v3.2.3")],
            default="v3.2.3", non_interactive=True,
        )
        assert chosen == "v3.2.3"

    def test_non_interactive_without_a_default_refuses_to_pick(self):
        """The whole point: no signal means ask, and unattended means decline.

        Answering "newest" here is what put the wrong SDK in the config.
        """
        assert prompt_ncs_version(
            [_install("v3.4.0"), _install("v3.2.3")],
            default=None, non_interactive=True,
        ) is None

    def test_an_empty_answer_keeps_the_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("fw_context_mcp.cli._init_interactive._input", lambda _: "")
        chosen = prompt_ncs_version(
            [_install("v3.4.0"), _install("v3.2.3")],
            default="v3.2.3", non_interactive=False,
        )
        assert chosen == "v3.2.3"

    def test_a_number_selects_from_the_list(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("fw_context_mcp.cli._init_interactive._input", lambda _: "2")
        chosen = prompt_ncs_version(
            [_install("v3.4.0"), _install("v3.2.3")],
            default=None, non_interactive=False,
        )
        assert chosen == "v3.2.3"

    def test_the_version_string_is_accepted_too(self, monkeypatch: pytest.MonkeyPatch):
        """It is what the list shows, so people will type it."""
        monkeypatch.setattr(
            "fw_context_mcp.cli._init_interactive._input", lambda _: "v3.2.3"
        )
        chosen = prompt_ncs_version(
            [_install("v3.4.0"), _install("v3.2.3")],
            default=None, non_interactive=False,
        )
        assert chosen == "v3.2.3"

    @pytest.mark.parametrize("answer", ["0", "99", "-1", "v9.9.9", "nonsense"])
    def test_an_out_of_range_answer_keeps_the_default(
        self, monkeypatch: pytest.MonkeyPatch, answer: str
    ):
        monkeypatch.setattr(
            "fw_context_mcp.cli._init_interactive._input", lambda _: answer
        )
        chosen = prompt_ncs_version(
            [_install("v3.4.0"), _install("v3.2.3")],
            default="v3.4.0", non_interactive=False,
        )
        assert chosen == "v3.4.0"

    def test_only_installed_sdks_are_offered(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """A hand-written script is manual configuration, not an init choice.

        A project that wants one sets [build] activate itself; init offers
        the SDKs it found and nothing else.
        """
        monkeypatch.setattr(
            "fw_context_mcp.cli._init_interactive._input", lambda _: "1"
        )
        prompt_ncs_version(
            [_install("v3.4.0"), _install("v3.2.3")],
            default=None, non_interactive=False,
        )
        menu = capsys.readouterr().out
        assert "v3.4.0" in menu and "v3.2.3" in menu
        assert "own" not in menu.lower()
        assert "script" not in menu.lower()

    def test_an_answer_past_the_last_sdk_keeps_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """There is no entry after the versions to select."""
        monkeypatch.setattr(
            "fw_context_mcp.cli._init_interactive._input", lambda _: "3"
        )
        chosen = prompt_ncs_version(
            [_install("v3.4.0"), _install("v3.2.3")],
            default="v3.4.0", non_interactive=False,
        )
        assert chosen == "v3.4.0"


class TestAlreadyConfigured:
    """A configured environment is the standing answer until someone changes it.

    init does not ask for a script path — a custom one is set by hand in the
    config.  What it does on a re-run is show what is configured, so the
    choice can be moved to a standard SDK without editing the file.
    """

    _SCRIPT = "/home/u/ncs_tools/nordic_minimal_setup.sh"

    def test_the_configured_script_is_shown_first_and_kept_by_default(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        monkeypatch.setattr("fw_context_mcp.cli._init_interactive._input", lambda _: "")
        chosen = prompt_ncs_version(
            [_install("v3.4.0"), _install("v3.2.3")],
            default=None, non_interactive=False, current=self._SCRIPT,
        )
        assert chosen == self._SCRIPT

        menu = capsys.readouterr().out
        first_line = next(line for line in menu.splitlines() if line.strip().startswith("1."))
        assert self._SCRIPT in first_line
        assert "(current)" in first_line

    def test_an_sdk_can_be_chosen_instead(self, monkeypatch: pytest.MonkeyPatch):
        """Entry 1 is the current script, so the SDKs start at 2."""
        monkeypatch.setattr("fw_context_mcp.cli._init_interactive._input", lambda _: "2")
        chosen = prompt_ncs_version(
            [_install("v3.4.0"), _install("v3.2.3")],
            default=None, non_interactive=False, current=self._SCRIPT,
        )
        assert chosen == "v3.4.0"

    def test_an_unattended_run_never_switches_away(self):
        """Silently replacing a configured environment is not init's call."""
        chosen = prompt_ncs_version(
            [_install("v3.4.0"), _install("v3.2.3")],
            default="v3.2.3", non_interactive=True, current=self._SCRIPT,
        )
        assert chosen == self._SCRIPT

    def test_it_is_offered_even_with_a_single_sdk_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """With one SDK and no current value there is nothing to ask.

        With a current value there is: keep it, or move to that one SDK.
        """
        monkeypatch.setattr("fw_context_mcp.cli._init_interactive._input", lambda _: "2")
        chosen = prompt_ncs_version(
            [_install("v3.2.3")],
            default=None, non_interactive=False, current=self._SCRIPT,
        )
        assert chosen == "v3.2.3"

    def test_a_junk_answer_keeps_the_configured_script(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            "fw_context_mcp.cli._init_interactive._input", lambda _: "nonsense"
        )
        chosen = prompt_ncs_version(
            [_install("v3.4.0")],
            default=None, non_interactive=False, current=self._SCRIPT,
        )
        assert chosen == self._SCRIPT


class TestZephyrSdkIsNotNcs:
    """Upstream Zephyr and the nRF Connect SDK are different things.

    Zephyr's own workflow exports ZEPHYR_BASE and ZEPHYR_SDK_INSTALL_DIR when
    you source zephyr-env.sh or activate the workspace, so anyone using it
    already has the paths set — there is nothing to pick between.  NCS is the
    opposite: its environment is created by `nrfutil sdk-manager toolchain
    env`, which takes the version as an argument, so before it runs there is
    nothing in the environment to read.
    """

    def test_it_is_read_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        sdk = tmp_path / "zephyr-sdk-0.17.0"
        sdk.mkdir()
        (sdk / "environment-setup-x86_64-pokysdk-linux").write_text("", encoding="utf-8")
        monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(sdk))

        choice = ZephyrBuildSystem.zephyr_sdk_from_environment()
        assert choice is not None
        assert choice.kind == SDK_KIND_ZEPHYR
        assert choice.version == "0.17.0"
        assert choice.usable is True
        assert choice.env_script is not None

    def test_without_the_variable_there_is_nothing_to_report(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)
        assert ZephyrBuildSystem.zephyr_sdk_from_environment() is None

    def test_a_directory_without_an_env_script_is_not_usable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        sdk = tmp_path / "zephyr-sdk-0.17.0"
        sdk.mkdir()
        monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(sdk))

        choice = ZephyrBuildSystem.zephyr_sdk_from_environment()
        assert choice is not None
        assert choice.usable is False

    def test_a_missing_directory_is_not_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(tmp_path / "gone"))
        assert ZephyrBuildSystem.zephyr_sdk_from_environment() is None

    def test_the_two_kinds_are_labelled_apart(self):
        ncs = _install("v3.2.3")
        zephyr = SdkChoice(
            kind=SDK_KIND_ZEPHYR, version="0.17.0",
            path=Path("/opt/zephyr-sdk-0.17.0"), usable=True,
        )
        assert ncs.label != zephyr.label
        assert "nRF Connect" in ncs.describe()
        assert "Zephyr SDK" in zephyr.describe()

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
from fw_context_mcp.indexer.builders.zephyr import NcsInstall, ZephyrBuildSystem


def _install(version: str, *, usable: bool = True) -> NcsInstall:
    return NcsInstall(
        version=version,
        sdk_dir=Path(f"/home/u/ncs/{version}"),
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
        assert installs[0].sdk_dir == Path("/home/u/ncs/v3.4.0")

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

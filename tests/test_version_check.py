"""Tests for the version facts that ``get_active_build`` reports.

Two facts, two sources:

* ``client_restart_required`` for an upgrade: the installed package metadata
  against the version that the process loaded.  No network.
* ``update_notice``: a state file that a background PyPI request writes.

No test sends a request: every test replaces ``httpx.get``.  The session
fixture in conftest sets ``FW_CONTEXT_NO_UPDATE_CHECK``, thus a test of the
enabled check removes it first.
"""

from __future__ import annotations

import importlib.metadata
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

import fw_context_mcp.version_check as vc
from fw_context_mcp.mcp.handlers.maintenance import _with_version_state, get_active_build

_NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """A global directory of its own, with its own global config.

    The state file must not come from another test.  The global config must
    not come from the operator: conftest copies the config of the operator
    into the session home, and an ``[updates] check = false`` there made
    the tests of the enabled check fail.
    """
    import fw_context_mcp.config.settings as settings

    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("FW_CONTEXT_HOME", str(path))
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", path / "config.toml")
    return path


@pytest.fixture
def enabled(monkeypatch) -> None:
    """Remove the stop that conftest sets for the whole session."""
    monkeypatch.delenv(vc.DISABLE_ENV, raising=False)


@pytest.fixture
def installed(monkeypatch):
    """Set the installed version, and make the install a regular one."""

    def _set(version: str | None) -> None:
        monkeypatch.setattr(vc, "installed_version", lambda: version)
        monkeypatch.setattr(vc, "is_editable_install", lambda: False)

    return _set


def _pypi_get(latest: str, calls: list[str] | None = None):
    """Return a stand-in for ``httpx.get`` that answers like PyPI."""

    def _get(url: str, timeout=None) -> httpx.Response:
        if calls is not None:
            calls.append(url)
        return httpx.Response(200, json={"info": {"version": latest}}, request=httpx.Request("GET", url))

    return _get


def _no_request(url: str, timeout=None) -> httpx.Response:
    raise AssertionError("the check sent a request that it must not send")


def _write_state(home: Path, latest: str, checked_at: datetime = _NOW) -> None:
    (home / "update_check.json").write_text(
        json.dumps({"checked_at": checked_at.isoformat(), "latest": latest}), encoding="utf-8"
    )


# ── The running process against the installed package ───────────────────────


class TestRestartReason:
    def test_the_same_version_gives_no_reason(self, installed):
        installed("0.33.0")
        assert vc.restart_reason("0.33.0") is None

    def test_an_upgrade_gives_the_reason(self, installed):
        installed("0.34.0")
        reason = vc.restart_reason("0.33.0")
        assert reason is not None
        assert "0.34.0" in reason and "0.33.0" in reason
        assert "restart the LLM client" in reason
        assert "child process" in reason

    def test_a_downgrade_gives_the_reason_too(self, installed):
        """The process then runs code that is not on disk any more."""
        installed("0.32.0")
        assert vc.restart_reason("0.33.0") is not None

    def test_no_installed_package_gives_no_reason(self, installed):
        installed(None)
        assert vc.restart_reason("0.33.0") is None

    def test_installed_version_reads_the_metadata_at_each_call(self, monkeypatch):
        """The value must follow an upgrade while the process runs."""
        versions = iter(["0.33.0", "0.34.0"])
        monkeypatch.setattr(vc.importlib.metadata, "version", lambda name: next(versions))
        assert vc.installed_version() == "0.33.0"
        assert vc.installed_version() == "0.34.0"

    def test_installed_version_without_metadata(self, monkeypatch):
        def _missing(name: str) -> str:
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(vc.importlib.metadata, "version", _missing)
        assert vc.installed_version() is None


# ── Editable install (PEP 610) ───────────────────────────────────────────────


class _FakeDistribution:
    def __init__(self, direct_url: str | None) -> None:
        self._direct_url = direct_url

    def read_text(self, name: str) -> str | None:
        return self._direct_url if name == "direct_url.json" else None


class TestEditableInstall:
    @pytest.mark.parametrize(
        ("direct_url", "expected"),
        [
            (None, False),  # an install from an index writes no direct_url.json
            (json.dumps({"url": "file:///src", "dir_info": {"editable": True}}), True),
            (json.dumps({"url": "file:///src", "dir_info": {}}), False),
            (json.dumps({"url": "https://x/y.whl", "archive_info": {}}), False),
            (json.dumps(["not", "an", "object"]), False),
        ],
    )
    def test_direct_url(self, monkeypatch, direct_url, expected):
        monkeypatch.setattr(vc.importlib.metadata, "distribution", lambda name: _FakeDistribution(direct_url))
        assert vc.is_editable_install() is expected

    def test_no_package(self, monkeypatch):
        def _missing(name: str):
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(vc.importlib.metadata, "distribution", _missing)
        assert vc.is_editable_install() is False

    def test_a_file_that_is_not_json_gives_unknown(self, monkeypatch, caplog):
        """One warning for each process: get_active_build calls the function at each call."""
        import logging

        monkeypatch.setattr(vc.importlib.metadata, "distribution", lambda name: _FakeDistribution("{not json"))
        monkeypatch.setattr(vc, "_direct_url_warned", set())
        with caplog.at_level(logging.WARNING, logger=vc.__name__):
            assert vc.is_editable_install() is None
            assert vc.is_editable_install() is None
        assert sum("direct_url.json" in r.getMessage() for r in caplog.records) == 1

    def test_an_unknown_install_type_skips_the_check(self, home, monkeypatch, enabled):
        """No guess: the check could report a release that the source tree holds."""
        monkeypatch.setattr(vc.importlib.metadata, "distribution", lambda name: _FakeDistribution("{not json"))
        monkeypatch.setattr(vc, "installed_version", lambda: "0.33.0")
        monkeypatch.setattr(vc.httpx, "get", _no_request)
        _write_state(home, "0.34.0")

        assert vc.start_update_check(True) is None
        assert vc.update_notice(True) is None


# ── The opt-out ──────────────────────────────────────────────────────────────


class TestEnabled:
    def test_on_by_default(self, enabled):
        assert vc.update_check_enabled(True) is True

    def test_the_config_stops_it(self, enabled):
        assert vc.update_check_enabled(False) is False

    @pytest.mark.parametrize("value", ["1", "true", "yes"])
    def test_the_variable_stops_it(self, monkeypatch, value):
        monkeypatch.setenv(vc.DISABLE_ENV, value)
        assert vc.update_check_enabled(True) is False

    def test_zero_does_not_stop_it(self, monkeypatch):
        monkeypatch.setenv(vc.DISABLE_ENV, "0")
        assert vc.update_check_enabled(True) is True


class TestLoadUpdateSettings:
    """``[updates]`` comes from the global file only, and the read writes nothing."""

    @staticmethod
    def _settings():
        from fw_context_mcp.config.settings import load_update_settings

        return load_update_settings()

    def test_no_file_gives_the_default_and_creates_no_file(self, home):
        assert self._settings().check is True
        assert not (home / "config.toml").exists()

    def test_the_key_stops_the_check(self, home):
        (home / "config.toml").write_text("[updates]\ncheck = false\n", encoding="utf-8")
        assert self._settings().check is False

    @pytest.mark.parametrize("text", ["[updates\ncheck = false\n", 'updates = "x"\n'])
    def test_a_damaged_file_gives_the_default(self, home, text):
        (home / "config.toml").write_text(text, encoding="utf-8")
        assert self._settings().check is True

    def test_it_does_not_resolve_the_embedding_model(self, home, monkeypatch):
        """The full load() runs nvidia-smi and asks Ollama: seconds before mcp.run()."""
        import fw_context_mcp.llm.auto_model as auto_model

        def _forbidden(*args, **kwargs):
            raise AssertionError("load_update_settings resolved the embedding model")

        monkeypatch.setattr(auto_model, "resolve_embed_model", _forbidden)
        (home / "config.toml").write_text("[updates]\ncheck = false\n", encoding="utf-8")
        assert self._settings().check is False

    @pytest.mark.parametrize("name", ["config.toml", "local.toml"])
    def test_a_project_file_gets_a_warning(self, home, tmp_path, caplog, name):
        """A team that sets the key in the committed config must learn that it has no effect."""
        import logging

        from fw_context_mcp.config import load

        root = tmp_path / "proj"
        (root / ".fw-context").mkdir(parents=True)
        (root / ".fw-context" / name).write_text("[updates]\ncheck = false\n", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="fw_context_mcp.config.settings"):
            load(root)
        assert any("[updates] has no effect in a project config" in r.getMessage() for r in caplog.records)
        assert self._settings().check is True


# ── The PyPI request and the state file ──────────────────────────────────────


class TestRefresh:
    def test_it_stores_the_latest_version(self, home, monkeypatch):
        monkeypatch.setattr(vc.httpx, "get", _pypi_get("0.34.0"))
        vc.refresh_latest_version(now=_NOW)

        state = vc.read_state()
        assert state == vc.UpdateState(checked_at=_NOW, latest="0.34.0")

    def test_a_young_state_sends_no_request(self, home, monkeypatch):
        _write_state(home, "0.34.0", checked_at=_NOW - timedelta(hours=23))
        monkeypatch.setattr(vc.httpx, "get", _no_request)
        vc.refresh_latest_version(now=_NOW)

    def test_an_old_state_sends_a_request(self, home, monkeypatch):
        _write_state(home, "0.34.0", checked_at=_NOW - timedelta(hours=25))
        calls: list[str] = []
        monkeypatch.setattr(vc.httpx, "get", _pypi_get("0.35.0", calls))
        vc.refresh_latest_version(now=_NOW)

        assert calls == [vc.PYPI_URL]
        assert vc.read_state() == vc.UpdateState(checked_at=_NOW, latest="0.35.0")

    def test_a_state_from_the_future_sends_a_request(self, home, monkeypatch):
        """A clock that moved back must not stop the check until it catches up."""
        _write_state(home, "0.34.0", checked_at=_NOW + timedelta(days=30))
        calls: list[str] = []
        monkeypatch.setattr(vc.httpx, "get", _pypi_get("0.35.0", calls))
        vc.refresh_latest_version(now=_NOW)

        assert calls == [vc.PYPI_URL]
        assert vc.read_state() == vc.UpdateState(checked_at=_NOW, latest="0.35.0")

    @pytest.mark.parametrize(
        "error",
        [httpx.ConnectError("no network"), httpx.ReadTimeout("slow network")],
    )
    def test_a_network_error_writes_nothing_and_does_not_raise(self, home, monkeypatch, error):
        def _fail(url: str, timeout=None) -> httpx.Response:
            raise error

        monkeypatch.setattr(vc.httpx, "get", _fail)
        vc._refresh_in_background()
        assert not (home / "update_check.json").exists()

    def test_an_http_status_writes_nothing(self, home, monkeypatch):
        def _get(url: str, timeout=None) -> httpx.Response:
            return httpx.Response(503, request=httpx.Request("GET", url))

        monkeypatch.setattr(vc.httpx, "get", _get)
        vc._refresh_in_background()
        assert not (home / "update_check.json").exists()

    @pytest.mark.parametrize(
        "body",
        [b"not json", b'{"info": {}}', b'{"info": {"version": "not a version"}}', b"[]"],
    )
    def test_a_strange_document_writes_nothing(self, home, monkeypatch, body):
        def _get(url: str, timeout=None) -> httpx.Response:
            return httpx.Response(200, content=body, request=httpx.Request("GET", url))

        monkeypatch.setattr(vc.httpx, "get", _get)
        vc._refresh_in_background()
        assert not (home / "update_check.json").exists()

    @pytest.mark.parametrize(
        "text",
        [
            "not json",
            json.dumps({"latest": "0.34.0"}),
            json.dumps({"checked_at": "yesterday", "latest": "0.34.0"}),
            json.dumps({"checked_at": _NOW.isoformat(), "latest": "not a version"}),
            # No time zone: a comparison with an aware time would raise.
            json.dumps({"checked_at": "2026-10-08T12:00:00", "latest": "0.34.0"}),
        ],
    )
    def test_a_damaged_state_file_reads_as_no_state(self, home, text):
        (home / "update_check.json").write_text(text, encoding="utf-8")
        assert vc.read_state() is None


class TestStartUpdateCheck:
    def test_stopped_starts_no_thread(self, home, monkeypatch, installed):
        installed("0.33.0")
        monkeypatch.setenv(vc.DISABLE_ENV, "1")
        monkeypatch.setattr(vc.httpx, "get", _no_request)
        assert vc.start_update_check(True) is None

    def test_an_editable_install_starts_no_thread(self, home, monkeypatch, enabled):
        monkeypatch.setattr(vc, "is_editable_install", lambda: True)
        monkeypatch.setattr(vc.httpx, "get", _no_request)
        assert vc.start_update_check(True) is None

    def test_the_thread_writes_the_state(self, home, monkeypatch, enabled, installed):
        installed("0.33.0")
        monkeypatch.setattr(vc.httpx, "get", _pypi_get("0.34.0"))
        thread = vc.start_update_check(True)
        assert thread is not None and thread.daemon
        thread.join(timeout=10)

        state = vc.read_state()
        assert state is not None and state.latest == "0.34.0"


# ── The notice ───────────────────────────────────────────────────────────────


class TestUpdateNotice:
    def test_a_newer_release_gives_the_notice(self, home, enabled, installed):
        installed("0.33.0")
        _write_state(home, "0.34.0")
        notice = vc.update_notice(True)
        assert notice is not None
        assert "0.34.0" in notice and "0.33.0" in notice
        assert "Do not upgrade" in notice

    @pytest.mark.parametrize("latest", ["0.33.0", "0.32.1"])
    def test_no_newer_release_gives_no_notice(self, home, enabled, installed, latest):
        installed("0.33.0")
        _write_state(home, latest)
        assert vc.update_notice(True) is None

    def test_versions_compare_as_numbers(self, home, enabled, installed):
        """A string comparison sorts "0.10.0" before "0.9.0"."""
        installed("0.9.0")
        _write_state(home, "0.10.0")
        assert vc.update_notice(True) is not None

    def test_a_pre_release_is_older_than_its_release(self, home, enabled, installed):
        installed("0.34.0")
        _write_state(home, "0.34.0rc1")
        assert vc.update_notice(True) is None

    def test_no_state_gives_no_notice(self, home, enabled, installed):
        installed("0.33.0")
        assert vc.update_notice(True) is None

    def test_the_opt_out_hides_an_old_state(self, home, enabled, installed):
        """A state file from before the opt-out must not keep the notice alive."""
        installed("0.33.0")
        _write_state(home, "0.34.0")
        assert vc.update_notice(False) is None

    def test_an_editable_install_gives_no_notice(self, home, enabled, monkeypatch):
        monkeypatch.setattr(vc, "installed_version", lambda: "0.29.1")
        monkeypatch.setattr(vc, "is_editable_install", lambda: True)
        _write_state(home, "0.33.0")
        assert vc.update_notice(True) is None


class TestCmdVersion:
    """``fw-context version`` reads the state file and sends no request."""

    def test_it_prints_a_newer_release(self, home, monkeypatch, enabled, installed, capsys):
        from argparse import Namespace

        from fw_context_mcp.cli._export import cmd_version

        installed("0.33.0")
        _write_state(home, "0.34.0")
        monkeypatch.setattr(vc.httpx, "get", _no_request)

        assert cmd_version(Namespace()) == 0
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 2
        assert "0.34.0 is available on PyPI" in lines[1]

    def test_it_prints_only_the_version_without_a_newer_release(self, home, enabled, installed, capsys):
        from argparse import Namespace

        from fw_context_mcp.cli._export import cmd_version

        installed("0.34.0")
        _write_state(home, "0.34.0")

        assert cmd_version(Namespace()) == 0
        assert len(capsys.readouterr().out.splitlines()) == 1


# ── get_active_build ─────────────────────────────────────────────────────────


def _uninitialized_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    (root / ".fw-context" / "config.toml").write_text("[project]\n", encoding="utf-8")
    return root


class TestGetActiveBuild:
    def test_an_upgrade_asks_for_a_restart_without_an_index(self, tmp_path, home, installed):
        """The fact concerns the process, thus it must not wait for an index."""
        installed("99.0.0")
        result = get_active_build(project_root=str(_uninitialized_project(tmp_path)))

        assert result["status"] == "not_initialized"
        assert result["client_restart_required"] is True
        assert "99.0.0" in result["client_restart_reason"]
        assert result["index_message"].startswith("Restart the LLM client")

    def test_no_upgrade_adds_no_field(self, tmp_path, home, enabled, installed):
        """The check runs (``enabled``), and the release on PyPI is the installed one."""
        from fw_context_mcp import __version__

        installed(__version__)
        _write_state(home, __version__)
        result = get_active_build(project_root=str(_uninitialized_project(tmp_path)))

        assert "client_restart_required" not in result
        assert "update_notice" not in result

    def test_the_notice_reaches_the_result(self, tmp_path, home, enabled, installed):
        from fw_context_mcp import __version__

        installed(__version__)
        _write_state(home, "99.0.0")
        result = get_active_build(project_root=str(_uninitialized_project(tmp_path)))

        assert "99.0.0" in result["update_notice"]
        assert result["status"] == "not_initialized"

    def test_the_session_stop_hides_the_notice(self, tmp_path, home, installed):
        """conftest sets FW_CONTEXT_NO_UPDATE_CHECK, thus no other test gets the field."""
        from fw_context_mcp import __version__

        installed(__version__)
        _write_state(home, "99.0.0")
        result = get_active_build(project_root=str(_uninitialized_project(tmp_path)))

        assert "update_notice" not in result

    def test_an_upgrade_keeps_the_row_format_reason(self, home, installed):
        """Two causes can apply together, and the caller must read the two."""
        installed("99.0.0")
        result = _with_version_state({
            "status": "ready",
            "client_restart_required": True,
            "client_restart_reason": "Row format reason.",
            "index_message": "Restart the LLM client — row format. Index is fine.",
        })

        assert result["client_restart_reason"].startswith("Row format reason. ")
        assert "99.0.0" in result["client_restart_reason"]
        # The action is at the front one time, not two times.
        assert result["index_message"] == "Restart the LLM client — row format. Index is fine."

    def test_an_error_result_gets_no_index_message(self, home, installed):
        installed("99.0.0")
        result = _with_version_state({"error": "No build config indexed."})

        assert result["client_restart_required"] is True
        assert "index_message" not in result

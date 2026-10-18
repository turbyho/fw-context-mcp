"""``check_ollama`` reads the config of the project also when the project has no index.

The fallback for a project without an index called ``load(None)`` when the
call named no project: that reads the global config only, thus
``[llm] enabled``, ``model`` and ``ollama_url`` of ``local.toml`` had no
effect.  A project name went to ``load`` as a path relative to the
current directory, which holds no config.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fw_context_mcp.config.settings as settings

PID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    """A project with an ID and no index; its local.toml disables the LLM, the global config does not."""
    home = tmp_path / "home" / "config.toml"
    home.parent.mkdir()
    # A port that refuses at once: a check that goes to Ollama fails fast.
    home.write_text('[llm]\nenabled = true\nollama_url = "http://127.0.0.1:9"\n', encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", home)
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text(f'[project]\nid = "{PID}"\n', encoding="utf-8")
    (root / ".fw-context" / "local.toml").write_text(
        f'[index]\ndb_dir = "{(tmp_path / "index").as_posix()}"\n[llm]\nenabled = false\n', encoding="utf-8",
    )
    return root


def test_the_project_of_the_current_directory(project, monkeypatch):
    from fw_context_mcp.mcp.handlers.maintenance import check_ollama

    monkeypatch.chdir(project)

    assert check_ollama()["status"] == "disabled"


def test_a_project_name(project, monkeypatch, tmp_path):
    from fw_context_mcp import utils
    from fw_context_mcp.mcp.handlers.maintenance import check_ollama

    monkeypatch.setattr(utils, "_lookup_registry_root", lambda name: project if name == "myproj" else None)
    monkeypatch.chdir(tmp_path)

    assert check_ollama(project_root="myproj")["status"] == "disabled"


def test_a_project_that_is_not_initialized(project, monkeypatch):
    """No ID: `_resolve_context` raises ProjectNotInitializedError, and the same fallback reads the project."""
    from fw_context_mcp.mcp.handlers.maintenance import check_ollama

    (project / ".fw-context" / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(project)

    assert check_ollama()["status"] == "disabled"

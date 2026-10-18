"""The LLM step of ``fw-context init`` offers the global settings.

Before, the step read only ``local.toml``.  A project that used a complete
``[llm]`` table of the global config got "LLM: not configured", and the
menu gave three ways to write a project setting (Ollama, cloud API,
disable) and none to keep the global one.  Enter skipped the step, which
kept the global settings, but the menu did not say so.

Each test writes its own global config: the session fixture copies the
global config of the operator, and its ``[llm]`` table would decide the
outcome.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

GLOBAL_LLM = '[llm]\nenabled = true\nmodel = "global-chat:1b"\nembed_model = "global-embed:1b"\n'


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    """A project without ``[llm]`` keys, and a global config of *GLOBAL_LLM*."""
    from fw_context_mcp.config import settings

    global_path = tmp_path / "home" / "config.toml"
    global_path.parent.mkdir()
    global_path.write_text(GLOBAL_LLM, encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", global_path)
    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    (root / ".fw-context" / "config.toml").write_text("[project]\n", encoding="utf-8")
    (root / ".fw-context" / "local.toml").write_text('[index]\ndb_dir = "/tmp/x"\n', encoding="utf-8")
    return root


def _run(monkeypatch, capsys, root: Path, answers: list[str], *, non_interactive: bool = False) -> str:
    """Run the LLM step with *answers* for the prompts; return the output."""
    from fw_context_mcp.cli import _init_interactive
    from fw_context_mcp.config import load

    replies = iter(answers)
    monkeypatch.setattr(_init_interactive, "_input", lambda prompt: next(replies))
    _init_interactive.prompt_llm_config(root, load(root), non_interactive=non_interactive)
    return capsys.readouterr().out


def _local_llm(root: Path) -> dict:
    return tomllib.loads((root / ".fw-context" / "local.toml").read_text(encoding="utf-8")).get("llm", {})


def test_the_global_settings_are_shown_and_enter_keeps_them(project, monkeypatch, capsys):
    out = _run(monkeypatch, capsys, project, [""])

    assert "LLM: ollama + global-chat:1b + global-embed:1b (from the global config)" in out, out
    assert "not configured" not in out, out
    assert "0. Use the global settings — Enter" in out, out
    assert "[ok] LLM: the global settings apply" in out, out
    assert _local_llm(project) == {}


def test_option_0_removes_the_backend_keys_of_local_toml(project, monkeypatch, capsys):
    """The existing projects of a user hold keys that an earlier init wrote."""
    local = project / ".fw-context" / "local.toml"
    local.write_text(local.read_text(encoding="utf-8") + '\n[llm]\nembed_model = "pinned:1b"\nnum_ctx = 4096\n',
                     encoding="utf-8")

    out = _run(monkeypatch, capsys, project, ["y", "0"])

    assert "(set in .fw-context/local.toml) (already configured)" in out, out
    assert "0. Use the global settings (removes the [llm] backend keys of .fw-context/local.toml)" in out, out
    assert "removed the [llm] backend keys" in out, out
    assert _local_llm(project) == {"num_ctx": 4096}, "a key that does not select the backend stays"
    assert 'db_dir = "/tmp/x"' in local.read_text(encoding="utf-8")


def test_enter_does_not_remove_the_keys_of_local_toml(project, monkeypatch, capsys):
    """A removal must be a choice: after "Change? y", Enter skips."""
    local = project / ".fw-context" / "local.toml"
    local.write_text(local.read_text(encoding="utf-8") + '\n[llm]\nembed_model = "pinned:1b"\n', encoding="utf-8")

    out = _run(monkeypatch, capsys, project, ["y", ""])

    assert "[skip] LLM configuration skipped" in out, out
    assert _local_llm(project) == {"embed_model": "pinned:1b"}


def test_no_option_0_when_the_committed_config_selects_the_backend(project, monkeypatch, capsys):
    """init writes only local.toml, and cannot remove a committed setting."""
    (project / ".fw-context" / "config.toml").write_text('[project]\n\n[llm]\nmodel = "team:1b"\n', encoding="utf-8")

    out = _run(monkeypatch, capsys, project, ["y", ""])

    assert "(set in .fw-context/config.toml)" in out, out
    assert "Use the global settings" not in out, out


def test_non_interactive_names_the_global_settings(project, monkeypatch, capsys):
    out = _run(monkeypatch, capsys, project, [], non_interactive=True)

    assert "LLM: ollama + global-chat:1b + global-embed:1b (from the global config)" in out, out
    assert "not configured" not in out, out
    assert _local_llm(project) == {}


def test_without_global_settings_enter_skips(project, monkeypatch, capsys):
    from fw_context_mcp.config import settings

    settings._GLOBAL_CONFIG_PATH.write_text("[index]\n", encoding="utf-8")

    out = _run(monkeypatch, capsys, project, [""])

    assert "LLM: not configured" in out, out
    assert "Use the global settings" not in out, out
    assert "[skip] LLM configuration skipped" in out, out


@pytest.mark.parametrize("text", [
    # An inline table must come before the first header, else it is a key of that table.
    'llm = { embed_model = "pinned:1b" }\n\n[index]\ndb_dir = "/tmp/x"\n',
    '[index]\ndb_dir = "/tmp/x"\n\n[llm]  # my settings\nembed_model = "pinned:1b"\n',
    '[index]\ndb_dir = "/tmp/x"\n\n[llm]\nembed_model = "pinned:1b"\nchat_api_key = """\nabc"""\n',
])
def test_a_table_that_init_cannot_edit_is_not_changed(project, monkeypatch, capsys, text):
    """Before: "[ok] removed" with the keys still there, or a file that was not valid TOML."""
    local = project / ".fw-context" / "local.toml"
    local.write_text(text, encoding="utf-8")
    before = local.read_text(encoding="utf-8")

    out = _run(monkeypatch, capsys, project, ["y", "0"])

    assert "[warn]" in out and "Nothing was changed" in out, out
    assert "[ok] LLM: removed" not in out, out
    assert local.read_text(encoding="utf-8") == before


def test_option_0_names_a_change_of_the_embedding_model(project, monkeypatch, capsys):
    """Another model embeds each definition again; the user must know it before the run."""
    local = project / ".fw-context" / "local.toml"
    local.write_text(local.read_text(encoding="utf-8") + '\n[llm]\nembed_model = "pinned:1b"\n', encoding="utf-8")

    out = _run(monkeypatch, capsys, project, ["y", "0"])

    assert "The embedding model changes from pinned:1b to global-embed:1b" in out, out


def test_a_failed_restore_does_not_say_nothing_was_changed(project, monkeypatch, capsys):
    """The editor removes ``model`` and does not see the quoted key, thus it must restore the file."""
    local = project / ".fw-context" / "local.toml"
    local.write_text('[index]\ndb_dir = "/tmp/x"\n\n[llm]\nmodel = "m:1b"\n"enabled" = true\n', encoding="utf-8")

    from fw_context_mcp.config import _toml_editor

    real_write = _toml_editor._atomic_write
    writes = []

    def _second_write_fails(path, content):
        """The edit writes first, the restore second."""
        writes.append(path)
        if len(writes) > 1:
            raise OSError("disk full")
        real_write(path, content)

    monkeypatch.setattr(_toml_editor, "_atomic_write", _second_write_fails)
    out = _run(monkeypatch, capsys, project, ["y", "0"])

    assert "cannot write its old text back (disk full)" in out, out
    assert "Nothing was changed" not in out, out

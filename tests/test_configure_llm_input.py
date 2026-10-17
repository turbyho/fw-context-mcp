"""The LLM settings writers check their input, and tell the truth about the result.

- A ``"`` or a ``\\`` in a value made a line of local.toml that is not
  valid TOML: ``_format_toml_value`` put quotes around the text.
- ``configure_llm(chat_api_base="http://[::1")`` gave "Internal server
  error": ``_is_loopback_url`` raised ValueError before the write.
- ``fw-context init`` printed "[ok] Saved to .fw-context/local.toml" after
  an error, when ``configure_llm_core`` had put the old settings back.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from fw_context_mcp.config.settings import _update_local_toml


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    (root / ".fw-context" / "config.toml").write_text("[project]\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("value", ['a"b', "a\\q", "line\nnext", "x' y"])
def test_a_written_value_reads_back_unchanged(tmp_path, value):
    root = _project(tmp_path)

    _update_local_toml(root, {"model": value, "enabled": True, "timeout": 30.5, "num_ctx": 4096})

    text = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")
    llm = tomllib.loads(text)["llm"]
    assert llm["model"] == value, text
    assert (llm["enabled"], llm["timeout"], llm["num_ctx"]) == (True, 30.5, 4096)


def test_a_url_that_cannot_be_parsed_is_refused_before_the_write(tmp_path):
    from fw_context_mcp.llm.configure import configure_llm_core

    root = _project(tmp_path)

    answer = configure_llm_core(root, {"chat_api_base": "http://[::1"}, chat_api_base="http://[::1")

    assert answer["status"] == "error", answer
    assert answer["message"].startswith("chat_api_base is not a valid URL ("), answer
    assert answer["message"].endswith("Nothing was changed."), answer
    assert not (root / ".fw-context" / "local.toml").exists()


def test_the_read_back_error_has_one_period(tmp_path, monkeypatch):
    import fw_context_mcp.config as config
    from fw_context_mcp.config import ConfigError
    from fw_context_mcp.llm.configure import configure_llm_core

    root = _project(tmp_path)

    def _refuses(**kwargs):
        raise ConfigError(root / ".fw-context" / "local.toml", "not valid TOML")

    monkeypatch.setattr(config, "load", _refuses)

    answer = configure_llm_core(root, {"model": "m:1b"})

    assert answer["status"] == "error"
    assert ".." not in answer["message"], answer["message"]


def _cloud_setup(monkeypatch, capsys, root: Path, answers: dict[str, str]) -> str:
    from fw_context_mcp.cli import _init_interactive

    monkeypatch.setattr(_init_interactive, "prompt_text", lambda label, **kwargs: answers.get(label, ""))
    _init_interactive._prompt_cloud_setup(root)
    return capsys.readouterr().out


def test_init_refuses_a_url_that_cannot_be_parsed(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path)

    out = _cloud_setup(monkeypatch, capsys, root, {"API URL": "http://[::1"})

    assert "is not a valid URL" in out and "[ok]" not in out, out
    assert not (root / ".fw-context" / "local.toml").exists()


def test_init_says_no_ok_after_an_error(tmp_path, monkeypatch, capsys):
    from fw_context_mcp.llm import configure

    root = _project(tmp_path)
    monkeypatch.setattr(configure, "configure_llm_core", lambda *a, **k: {"status": "error", "message": "test failed"})

    out = _cloud_setup(monkeypatch, capsys, root, {"API URL": "http://localhost:1/v1", "Model": "m"})

    assert "[warn] test failed" in out and "[ok]" not in out, out


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_a_second_write_of_a_value_with_a_line_separator_keeps_the_file_valid(tmp_path, separator):
    """`splitlines` split the line of the first value: the second write left a part of it."""
    root = _project(tmp_path)

    _update_local_toml(root, {"chat_api_key": f"key{separator}tail"})
    _update_local_toml(root, {"chat_api_key": "new"})

    text = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")
    assert tomllib.loads(text)["llm"]["chat_api_key"] == "new", text



@pytest.mark.parametrize("text", ['[llm]\nmodel = "x"', "[llm]", '[project]\nid = "p"'])
def test_a_key_after_a_last_line_without_a_newline_gets_its_own_line(tmp_path, text):
    """The new key joined the last line: `model = "x"embed_model = "e"`."""
    root = _project(tmp_path)
    (root / ".fw-context" / "local.toml").write_text(text, encoding="utf-8")

    _update_local_toml(root, {"embed_model": "e"})

    written = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")
    assert tomllib.loads(written)["llm"]["embed_model"] == "e", written


def _no_ok_after_an_error(monkeypatch, capsys, root: Path, choice: str) -> str:
    from fw_context_mcp.cli import _init_interactive
    from fw_context_mcp.config import load
    from fw_context_mcp.llm import auto_model, configure, ollama

    monkeypatch.setattr(configure, "configure_llm_core", lambda *a, **k: {"status": "error", "message": "test failed"})
    monkeypatch.setattr(_init_interactive, "_input", lambda prompt: choice)
    monkeypatch.setattr(_init_interactive, "prompt_text", lambda label, default=None, **kwargs: default or "m")
    monkeypatch.setattr(_init_interactive, "_confirm", lambda question, **kwargs: False)
    monkeypatch.setattr(auto_model, "_gpu_available", lambda: False)
    monkeypatch.setattr(ollama, "check_setup", lambda cfg: {"status": "ok", "installed_models": []})
    _init_interactive.prompt_llm_config(root, load(root), non_interactive=False)
    return capsys.readouterr().out


@pytest.mark.parametrize("choice", ["1", "3"])
def test_init_says_no_ok_after_an_error_of_the_ollama_or_the_disable_setup(tmp_path, monkeypatch, capsys, choice):
    root = _project(tmp_path)
    (root / ".git").mkdir()

    out = _no_ok_after_an_error(monkeypatch, capsys, root, choice)

    assert "[warn] test failed" in out, out
    assert "[ok] LLM configured" not in out and "[ok] Saved" not in out and "[ok] LLM disabled" not in out, out

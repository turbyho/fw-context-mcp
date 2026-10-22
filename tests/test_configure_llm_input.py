"""The LLM settings writers check their input, and tell the truth about the result.

- A ``"`` or a ``\\`` in a value made a line of local.toml that is not
  valid TOML: ``_format_toml_value`` put quotes around the text.
- ``configure_llm(chat_api_base="http://[::1")`` gave "Internal server
  error": ``_is_loopback_url`` raised ValueError before the write.  The
  tool takes no URL any more: a URL goes into a profile of the global
  config, and the profile writer refuses one that cannot be parsed.
- ``fw-context init`` printed "[ok] Saved to .fw-context/local.toml" after
  an error, when ``configure_llm_core`` had put the old settings back.
  Now the test of a new profile does it: after an error, the global config
  gets its earlier text back, and no "[ok]" names the profile.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from fw_context_mcp.config.settings import _update_local_toml


@pytest.fixture(autouse=True)
def global_config(tmp_path: Path, monkeypatch) -> Path:
    """Give each test its own global config.

    WHY: the init steps write their LLM profile to the global config.
    Without this fixture they wrote into the copy of the global config of
    the operator that the whole session shares, and a later test read
    their profiles.  ``embed_model`` is set, thus ``load`` does not probe
    the GPU or Ollama for the automatic model.
    """
    from fw_context_mcp.config import settings

    path = tmp_path / "home" / "config.toml"
    path.parent.mkdir()
    path.write_text('[llm]\nenabled = true\nembed_model = "test-embed:1b"\n', encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", path)
    settings._config_cache.clear()
    return path


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    (root / ".fw-context" / "config.toml").write_text("[project]\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("value", ['a"b', "a\\q", "line\nnext", "x' y"])
def test_a_written_value_reads_back_unchanged(tmp_path, value):
    root = _project(tmp_path)

    _update_local_toml(root, {"embed_model": value, "enabled": True, "float_example": 30.5, "embed_dim": 4096})

    text = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")
    llm = tomllib.loads(text)["llm"]
    assert llm["embed_model"] == value, text
    assert (llm["enabled"], llm["float_example"], llm["embed_dim"]) == (True, 30.5, 4096)


def test_a_profile_url_that_cannot_be_parsed_is_refused_before_the_write(global_config):
    """The URL of the chat comes only from a profile now: its writer checks it."""
    from fw_context_mcp.config import ConfigError
    from fw_context_mcp.config._profiles import set_profile

    before = global_config.read_text(encoding="utf-8")

    with pytest.raises(ConfigError, match=r"url is not a valid URL"):
        set_profile("broken", {"provider": "openai_compatible", "url": "http://[::1", "model": "m"})

    assert global_config.read_text(encoding="utf-8") == before


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
    monkeypatch.setattr(_init_interactive, "_read_secret", lambda prompt: "")
    _init_interactive._custom_api_profile()
    return capsys.readouterr().out


def test_init_refuses_a_url_that_cannot_be_parsed(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path)

    out = _cloud_setup(monkeypatch, capsys, root, {"API URL": "http://[::1"})

    assert "is not a valid URL" in out and "[ok]" not in out, out
    assert not (root / ".fw-context" / "local.toml").exists()


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_a_second_write_of_a_value_with_a_line_separator_keeps_the_file_valid(tmp_path, separator):
    """`splitlines` split the line of the first value: the second write left a part of it."""
    root = _project(tmp_path)

    _update_local_toml(root, {"embed_doc_prompt": f"key{separator}tail"})
    _update_local_toml(root, {"embed_doc_prompt": "new"})

    text = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")
    assert tomllib.loads(text)["llm"]["embed_doc_prompt"] == "new", text



@pytest.mark.parametrize("text", ['[llm]\nmodel = "x"', "[llm]", '[project]\nid = "p"'])
def test_a_key_after_a_last_line_without_a_newline_gets_its_own_line(tmp_path, text):
    """The new key joined the last line: `model = "x"embed_model = "e"`."""
    root = _project(tmp_path)
    (root / ".fw-context" / "local.toml").write_text(text, encoding="utf-8")

    _update_local_toml(root, {"embed_model": "e"})

    written = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")
    assert tomllib.loads(written)["llm"]["embed_model"] == "e", written


def _no_ok_after_an_error(monkeypatch, capsys, root: Path, answers: list[str]) -> str:
    from fw_context_mcp.cli import _init_interactive, _llm_wizard
    from fw_context_mcp.config import load
    from fw_context_mcp.llm import auto_model, hardware, ollama

    # The test request of the new profile fails.
    monkeypatch.setattr(_llm_wizard, "test_profile", lambda cfg, name: "test failed")
    monkeypatch.setattr(hardware, "machine_memory", lambda: hardware.Memory(gpu_mib=None, ram_mib=16 * 1024))
    monkeypatch.setattr(ollama, "calibration_key", lambda *args: None)
    replies = iter(answers)
    monkeypatch.setattr(_init_interactive, "_input", lambda prompt: next(replies))
    monkeypatch.setattr(_init_interactive, "prompt_text", lambda label, default=None, **kwargs: default or "m")
    monkeypatch.setattr(_init_interactive, "_confirm", lambda question, **kwargs: False)
    monkeypatch.setattr(auto_model, "_gpu_available", lambda: False)
    monkeypatch.setattr(ollama, "check_setup", lambda cfg: {"status": "ok", "installed_models": []})
    _init_interactive.prompt_llm_config(root, load(root), non_interactive=False)
    return capsys.readouterr().out


def test_init_says_no_ok_after_an_error_of_the_ollama_setup(tmp_path, monkeypatch, capsys, global_config):
    """Option 3 makes no test call any more: tests/test_init_llm_global.py covers its error."""
    root = _project(tmp_path)
    (root / ".git").mkdir()
    before = global_config.read_text(encoding="utf-8")

    # Service 1 (the local Ollama); then Enter keeps the LLM analysis and the embeddings.
    out = _no_ok_after_an_error(monkeypatch, capsys, root, ["1", "", ""])

    assert "[warn] Profile 'local' does not answer: test failed" in out, out
    assert "[ok] Profile 'local' answers" not in out and "selects profile" not in out, out
    assert global_config.read_text(encoding="utf-8") == before


@pytest.fixture(autouse=True)
def _no_cloud_model_list(monkeypatch):
    """No request to the Ollama of the operator: the rule of the Ollama profiles asks for the cloud models.

    The recommendation of the local Ollama setup asks for the installed
    models too.
    """
    from fw_context_mcp.llm import _diag

    monkeypatch.setattr(_diag, "remote_hosts", lambda url: {})
    monkeypatch.setattr(_diag, "installed_models", lambda url: [])

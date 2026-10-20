"""The LLM step of ``fw-context init`` offers the global settings.

Before, the step read only ``local.toml``.  A project that used a complete
``[llm]`` table of the global config got "LLM: not configured", and the
menu gave three ways to write a project setting (Ollama, cloud API,
disable) and none to keep the global one.  Enter skipped the step, which
kept the global settings, but the menu did not say so.

Each test writes its own global config: the session fixture copies the
global config of the operator, and its ``[llm]`` table and its profiles
would decide the outcome.  The steps 1 and 2 of the menu write a profile
to the global config, thus they must not write into that shared copy.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

#: The backend is a profile of the global config; ``[llm]`` selects it.
GLOBAL_LLM = (
    '[llm]\nprofile = "local"\nenabled = true\nembed_model = "global-embed:1b"\n\n'
    '[llm.profiles.local]\nprovider = "ollama"\nurl = "http://localhost:11434"\nmodel = "global-chat:1b"\n'
)

#: How ``_llm_summary`` shows the backend of *GLOBAL_LLM*.
GLOBAL_SUMMARY = "profile local: ollama global-chat:1b @ http://localhost:11434, embedding global-embed:1b"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    """A project without ``[llm]`` keys, and a global config of *GLOBAL_LLM*."""
    from fw_context_mcp.config import settings

    global_path = tmp_path / "home" / "config.toml"
    global_path.parent.mkdir()
    global_path.write_text(GLOBAL_LLM, encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", global_path)
    settings._config_cache.clear()
    # A stored API key goes to secrets.toml of this directory: the keyring
    # of the session fails each call (conftest).
    from fw_context_mcp.llm import secrets

    monkeypatch.setenv("FW_CONTEXT_HOME", str(global_path.parent))
    monkeypatch.setattr(secrets, "_found", {})
    monkeypatch.setattr(secrets, "_told", set())
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


def _global_llm() -> dict:
    """Return the ``[llm]`` table of the global config of the test."""
    from fw_context_mcp.config import settings

    return tomllib.loads(settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8")).get("llm", {})


def _setup(monkeypatch, capsys, root: Path, choice: str, answers: dict[str, str], *,
           shared: bool = False) -> tuple[str, list[str]]:
    """Choose *choice* in the menu and give *answers* to the text prompts (by label; else the default).

    *shared* is the answer to the question if a shared profile changes;
    each other question (the pull of a model) gets "no".

    The real ``configure_llm_core`` runs: it writes local.toml, loads the
    config again with the new profile, and tests it.  Only the test call
    is replaced, thus no request leaves the machine.  Returns the output
    and the model of each test call.
    """
    from fw_context_mcp.cli import _init_interactive
    from fw_context_mcp.config import load
    from fw_context_mcp.llm import auto_model, client, ollama

    tested: list[str] = []

    def _call_llm(prompt, cfg, **kwargs):
        tested.append(cfg.model)
        return "OK"

    monkeypatch.setattr(client, "call_llm", _call_llm)
    monkeypatch.setattr(_init_interactive, "prompt_text", lambda label, default=None, **kwargs: answers.get(label, default))
    monkeypatch.setattr(_init_interactive, "_confirm", lambda question, **kwargs: shared and "shared" in question)
    monkeypatch.setattr(_init_interactive, "_read_secret", lambda prompt: answers.get("API key", ""))
    monkeypatch.setattr(auto_model, "_gpu_available", lambda: False)
    monkeypatch.setattr(ollama, "check_setup", lambda cfg: {"status": "ok", "installed_models": []})
    replies = iter([choice])
    monkeypatch.setattr(_init_interactive, "_input", lambda prompt: next(replies))
    _init_interactive.prompt_llm_config(root, load(root), non_interactive=False)
    return capsys.readouterr().out, tested


def test_the_global_settings_are_shown_and_enter_keeps_them(project, monkeypatch, capsys):
    out = _run(monkeypatch, capsys, project, [""])

    assert f"LLM: {GLOBAL_SUMMARY} (from the global config)" in out, out
    assert "not configured" not in out, out
    assert "0. Use the global settings — Enter" in out, out
    assert "[ok] LLM: the global settings apply" in out, out
    assert _local_llm(project) == {}


def test_option_0_removes_the_backend_keys_of_local_toml(project, monkeypatch, capsys):
    """The existing projects of a user hold keys that an earlier init wrote.

    ``profile`` selects the backend now, thus option 0 removes it too.
    ``analyze_symbols`` does not select the backend, thus it stays.
    """
    local = project / ".fw-context" / "local.toml"
    local.write_text(
        local.read_text(encoding="utf-8")
        + '\n[llm]\nprofile = "local"\nembed_model = "pinned:1b"\nanalyze_symbols = false\n',
        encoding="utf-8",
    )

    out = _run(monkeypatch, capsys, project, ["y", "0"])

    assert "(set in .fw-context/local.toml) (already configured)" in out, out
    assert "0. Use the global settings (removes the [llm] backend keys of .fw-context/local.toml)" in out, out
    assert "removed the [llm] backend keys" in out, out
    assert _local_llm(project) == {"analyze_symbols": False}, "a key that does not select the backend stays"
    assert 'db_dir = "/tmp/x"' in local.read_text(encoding="utf-8")


def test_enter_does_not_remove_the_keys_of_local_toml(project, monkeypatch, capsys):
    """A removal must be a choice: after "Change? y", Enter skips."""
    local = project / ".fw-context" / "local.toml"
    local.write_text(local.read_text(encoding="utf-8") + '\n[llm]\nembed_model = "pinned:1b"\n', encoding="utf-8")

    out = _run(monkeypatch, capsys, project, ["y", ""])

    assert "[skip] LLM configuration skipped" in out, out
    assert _local_llm(project) == {"embed_model": "pinned:1b"}


def test_no_option_0_when_the_committed_config_selects_the_backend(project, monkeypatch, capsys):
    """init writes only local.toml, and cannot remove a committed setting.

    The committed file can only select a profile of the global config.
    """
    (project / ".fw-context" / "config.toml").write_text('[project]\n\n[llm]\nprofile = "local"\n', encoding="utf-8")

    out = _run(monkeypatch, capsys, project, ["y", ""])

    assert "(set in .fw-context/config.toml)" in out, out
    assert "Use the global settings" not in out, out


def test_non_interactive_names_the_global_settings(project, monkeypatch, capsys):
    out = _run(monkeypatch, capsys, project, [], non_interactive=True)

    assert f"LLM: {GLOBAL_SUMMARY} (from the global config)" in out, out
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
    # A multi-line string of a backend key: the line editor cannot see where
    # its value ends.  WHY profile: chat_api_key, the key of this case
    # before, is a key of a profile now, and a local.toml with it does not
    # load.  TOML trims the first newline, thus the value is "local".
    '[index]\ndb_dir = "/tmp/x"\n\n[llm]\nembed_model = "pinned:1b"\nprofile = """\nlocal"""\n',
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
    """The editor removes ``embed_model`` and does not see the quoted key, thus it must restore the file."""
    local = project / ".fw-context" / "local.toml"
    local.write_text('[index]\ndb_dir = "/tmp/x"\n\n[llm]\nembed_model = "m:1b"\n"enabled" = true\n', encoding="utf-8")

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


def _ollama_defaults(monkeypatch, capsys, root: Path) -> tuple[list[str | None], str]:
    """Choose option 1 (Ollama), accept each default; return the defaults of the model prompts and the output."""
    from fw_context_mcp.cli import _init_interactive
    from fw_context_mcp.config import load
    from fw_context_mcp.llm import auto_model, configure, ollama

    defaults: list[str | None] = []

    def _prompt_text(label, default=None, **kwargs):
        defaults.append(default)
        return default

    monkeypatch.setattr(configure, "configure_llm_core", lambda *a, **k: {"status": "ok"})
    monkeypatch.setattr(_init_interactive, "prompt_text", _prompt_text)
    monkeypatch.setattr(_init_interactive, "_confirm", lambda question, **kwargs: False)
    monkeypatch.setattr(auto_model, "_gpu_available", lambda: False)
    monkeypatch.setattr(ollama, "check_setup", lambda cfg: {"status": "ok", "installed_models": []})
    replies = iter(["1"])
    monkeypatch.setattr(_init_interactive, "_input", lambda prompt: next(replies))
    _init_interactive.prompt_llm_config(root, load(root), non_interactive=False)
    return defaults, capsys.readouterr().out


def test_the_ollama_setup_offers_the_models_of_the_config(project, monkeypatch, capsys):
    """Before: Enter replaced the models of the global config with the recommended ones."""
    defaults, out = _ollama_defaults(monkeypatch, capsys, project)

    assert defaults == ["global-chat:1b", "global-embed:1b"], out
    assert "Current config: chat global-chat:1b, embedding global-embed:1b — Enter keeps them" in out, out


def test_the_ollama_setup_offers_the_recommendation_without_a_config(project, monkeypatch, capsys):
    from fw_context_mcp.config import settings

    settings._GLOBAL_CONFIG_PATH.write_text("[index]\n", encoding="utf-8")

    defaults, out = _ollama_defaults(monkeypatch, capsys, project)

    assert defaults == ["qwen2.5-coder:14b", "qwen3-embedding:0.6b"], out
    assert "Current config" not in out, out


def test_the_chat_model_of_a_cloud_api_is_not_an_ollama_default(project, monkeypatch, capsys):
    """The model of an API profile is not offered, and the API profile is not changed.

    The selected profile is not an Ollama one, thus the step writes the
    Ollama profile ``local`` next to it.
    """
    from fw_context_mcp.config import settings

    settings._GLOBAL_CONFIG_PATH.write_text(
        '[llm]\nprofile = "api"\nenabled = true\nembed_model = "global-embed:1b"\n\n'
        '[llm.profiles.api]\nprovider = "openai_compatible"\nurl = "http://127.0.0.1:9/v1"\nmodel = "deepseek-chat"\n',
        encoding="utf-8",
    )

    defaults, _ = _ollama_defaults(monkeypatch, capsys, project)

    assert defaults == ["qwen2.5-coder:14b", "global-embed:1b"]
    profiles = _global_llm()["profiles"]
    assert profiles["api"] == {"provider": "openai_compatible", "url": "http://127.0.0.1:9/v1", "model": "deepseek-chat"}
    assert profiles["local"] == {"url": "http://localhost:11434", "provider": "ollama", "model": "qwen2.5-coder:14b"}


def test_an_embedding_model_that_ollama_does_not_serve_is_not_an_ollama_default(project, monkeypatch, capsys):
    """The step offered to pull it from Ollama, and Enter said yes."""
    from fw_context_mcp.config import settings

    settings._GLOBAL_CONFIG_PATH.write_text(
        GLOBAL_LLM.replace('embed_model = "global-embed:1b"', 'embed_model = "BAAI/bge-small-en-v1.5"'),
        encoding="utf-8",
    )

    defaults, _ = _ollama_defaults(monkeypatch, capsys, project)

    assert defaults == ["global-chat:1b", "qwen3-embedding:0.6b"]


def test_option_1_writes_the_chat_model_into_the_selected_ollama_profile(project, monkeypatch, capsys):
    """The selected profile is an Ollama one: after a "yes", the step changes its model and keeps its other keys."""
    from fw_context_mcp.config import settings

    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL_LLM + "num_ctx = 8192\n", encoding="utf-8")

    out, tested = _setup(
        monkeypatch, capsys, project, "1", {"Chat model": "new-chat:1b", "Embedding model": "new-embed:1b"},
        shared=True,
    )

    assert "[ok] LLM configured: profile local (ollama + new-chat:1b), embedding new-embed:1b" in out, out
    assert _global_llm()["profiles"]["local"] == {
        "provider": "ollama", "url": "http://localhost:11434", "model": "new-chat:1b", "num_ctx": 8192,
    }
    assert _local_llm(project) == {"enabled": True, "profile": "local", "embed_model": "new-embed:1b"}
    assert tested == ["new-chat:1b"], "the test call must use the model of the new profile"


def test_option_2_stores_a_key_for_the_profile_and_never_in_a_config_file(project, monkeypatch, capsys):
    """Without a variable, the step reads the key without an echo and stores it before the test call."""
    from fw_context_mcp.config import settings
    from fw_context_mcp.llm import secrets

    key = "sk-init-" + "test"
    out, tested = _setup(monkeypatch, capsys, project, "2", {
        "Profile name": "work", "API URL": "http://127.0.0.1:9/v1", "Model": "work-chat", "API key": key,
    })

    assert "[ok] LLM configured: profile work" in out and key not in out, out
    assert secrets.stored_api_key("work") == key
    assert "api_key_env" not in _global_llm()["profiles"]["work"]
    for path in (settings._GLOBAL_CONFIG_PATH, project / ".fw-context" / "local.toml"):
        assert key not in path.read_text(encoding="utf-8"), path


def test_option_2_writes_a_cloud_profile_that_names_the_key_variable(project, monkeypatch, capsys):
    """The profile holds the NAME of the variable, never the key: a key in a TOML file is a secret in plain text."""
    out, tested = _setup(monkeypatch, capsys, project, "2", {
        "Profile name": "work",
        "API URL": "http://127.0.0.1:9/v1",
        "Environment variable with the API key (empty: store the key in the keyring)": "WORK_API_KEY",
        "Model": "work-chat",
    })

    assert "[ok] LLM configured: profile work (cloud API work-chat @ http://127.0.0.1:9/v1)" in out, out
    llm = _global_llm()
    assert llm["profiles"]["work"] == {
        "provider": "openai_compatible", "url": "http://127.0.0.1:9/v1", "model": "work-chat",
        "api_key_env": "WORK_API_KEY",
    }
    assert llm["profiles"]["local"]["model"] == "global-chat:1b", "the other profiles stay"
    assert llm["profile"] == "local", "the global selection stays: the project selects in local.toml"
    assert _local_llm(project) == {"enabled": True, "profile": "work"}
    assert tested == ["work-chat"]


def test_option_2_without_a_url_writes_nothing(project, monkeypatch, capsys):
    out, tested = _setup(monkeypatch, capsys, project, "2", {"API URL": ""})

    assert "[skip] Cloud API setup skipped" in out, out
    assert set(_global_llm()["profiles"]) == {"local"}
    assert _local_llm(project) == {}
    assert tested == []


def test_option_3_turns_off_the_analysis_and_keeps_the_embeddings(project, monkeypatch, capsys):
    """Before: it wrote enabled = false, which also turned off the embeddings, as the menu said "embedding still works"."""
    out = _run(monkeypatch, capsys, project, ["3"])

    assert "[ok] LLM analysis off ([llm] analyze_symbols = false) — the embeddings stay" in out, out
    assert _local_llm(project) == {"analyze_symbols": False}
    assert "[note]" not in out, out


def test_option_3_says_when_the_llm_is_off(project, monkeypatch, capsys):
    from fw_context_mcp.config import settings

    settings._GLOBAL_CONFIG_PATH.write_text("[llm]\nenabled = false\n", encoding="utf-8")

    out = _run(monkeypatch, capsys, project, ["3"])

    assert "[note] The LLM is off ([llm] enabled = false): the embeddings need enabled = true" in out, out


def test_option_3_gives_no_ok_when_local_toml_cannot_be_written(project, monkeypatch, capsys):
    from fw_context_mcp.config import settings

    def _fails(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(settings, "_update_local_toml", _fails)

    out = _run(monkeypatch, capsys, project, ["3"])

    assert "[warn] .fw-context/local.toml cannot be changed (read-only). Nothing was changed." in out, out
    assert "[ok]" not in out, out

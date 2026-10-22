"""The LLM step of ``fw-context init`` offers the global settings.

Before, the step read only ``local.toml``.  A project that used a complete
``[llm]`` table of the global config got "LLM: not configured", and the
menu gave three ways to write a project setting (Ollama, cloud API,
disable) and none to keep the global one.  Enter skipped the step, which
kept the global settings, but the menu did not say so.

The step has two parts now (2026-10-22): the services to set up, then the
list of the LLM analysis and the list of the embeddings.  Enter keeps the
current setting in each.

Each test writes its own global config: the session fixture copies the
global config of the operator, and its ``[llm]`` table and its profiles
would decide the outcome.  The setup of a service writes a profile to the
global config, thus it must not write into that shared copy.
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
    """Run the LLM step with *answers* for the prompts; return the output.

    The answers come in this order: "Change?" (only when the project
    selects the backend), the services, the list of the LLM analysis, the
    list of the embeddings.
    """
    from fw_context_mcp.cli import _init_interactive
    from fw_context_mcp.config import load
    from fw_context_mcp.llm import client

    _no_machine(monkeypatch)
    # The test request of a selection (configure_llm_core): no request leaves the test.
    monkeypatch.setattr(client, "call_llm", lambda prompt, cfg, **kwargs: "OK")
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


def _no_machine(monkeypatch) -> None:
    """A machine without a GPU, with 16 GiB of RAM, and without an Ollama server: no request leaves the test."""
    from fw_context_mcp.llm import _diag, auto_model, hardware, ollama

    monkeypatch.setattr(hardware, "machine_memory", lambda: hardware.Memory(gpu_mib=None, ram_mib=16 * 1024))
    # A config without embed_model: load detects one and asks the server.
    monkeypatch.setattr(auto_model, "_gpu_available", lambda: False)
    monkeypatch.setattr(auto_model, "_model_installed", lambda url, model: True)
    monkeypatch.setattr(_diag, "installed_models", lambda url: None)
    # No key: the recommendation never asks a server and never measures.
    monkeypatch.setattr(ollama, "calibration_key", lambda *args: None)
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: None)


class _AnsweringChat:
    """A chat client that answers each test request; it records the model of the request."""

    def __init__(self, cfg, tested: list[str]) -> None:
        self._model = cfg.model
        self._tested = tested

    def generate(self, prompt, **kwargs):
        from fw_context_mcp.llm.client import ChatResult

        self._tested.append(self._model)
        return ChatResult("OK")


def _setup(monkeypatch, capsys, root: Path, choice: list[str], answers: dict[str, str], *,
           shared: bool = False) -> tuple[str, list[str]]:
    """Give *choice* (each answer of the lists) and *answers* to the text prompts (by label; else the default).

    *shared* is the answer to the question if a shared profile changes;
    each other question (the pull of a model) gets "no".

    The real test of a new profile runs (``_llm_wizard.test_profile``), and
    the real test of a selection for this project (``configure_llm_core``):
    each reads the profile from the global config.  Only the chat client is
    replaced, thus no request leaves the machine.  Returns the output and
    the model of each test request.
    """
    from fw_context_mcp.cli import _init_interactive
    from fw_context_mcp.config import load
    from fw_context_mcp.llm import auto_model, client, ollama

    tested: list[str] = []
    monkeypatch.setattr(client, "get_chat_client", lambda cfg: _AnsweringChat(cfg, tested))
    monkeypatch.setattr(_init_interactive, "prompt_text", lambda label, default=None, **kwargs: answers.get(label, default))
    monkeypatch.setattr(_init_interactive, "_confirm", lambda question, **kwargs: shared and "shared" in question)
    monkeypatch.setattr(_init_interactive, "_read_secret", lambda prompt: answers.get("API key", ""))
    monkeypatch.setattr(auto_model, "_gpu_available", lambda: False)
    monkeypatch.setattr(ollama, "check_setup", lambda cfg: {"status": "ok", "installed_models": []})
    _no_machine(monkeypatch)
    replies = iter(choice)
    monkeypatch.setattr(_init_interactive, "_input", lambda prompt: next(replies))
    _init_interactive.prompt_llm_config(root, load(root), non_interactive=False)
    return capsys.readouterr().out, tested


def test_the_global_settings_are_shown_and_enter_keeps_them(project, monkeypatch, capsys):
    # Enter: no service, the LLM analysis and the embeddings stay.
    out = _run(monkeypatch, capsys, project, ["", "", ""])

    assert f"LLM: {GLOBAL_SUMMARY} (from the global config)" in out, out
    assert "not configured" not in out, out
    assert "the LLM analysis: where it runs (Enter: keep local)" in out, out
    assert "[ok] LLM analysis: the current setting stays" in out, out
    assert "[ok] Embeddings: the current setting stays" in out, out
    assert _local_llm(project) == {}


def test_option_0_removes_the_backend_keys_of_local_toml(project, monkeypatch, capsys):
    """The existing projects of a user hold keys that an earlier init wrote.

    ``profile`` selects the backend now, thus option 0 of the list of the
    LLM analysis removes it, and option 0 of the list of the embeddings
    removes ``embed_model``.  ``analyze_symbols`` does not select the
    backend, thus it stays.
    """
    local = project / ".fw-context" / "local.toml"
    local.write_text(
        local.read_text(encoding="utf-8")
        + '\n[llm]\nprofile = "local"\nembed_model = "pinned:1b"\nanalyze_symbols = false\n',
        encoding="utf-8",
    )

    out = _run(monkeypatch, capsys, project, ["y", "", "0", "0"])

    assert "(set in .fw-context/local.toml) (already configured)" in out, out
    assert "0. the global settings (removes the profile of .fw-context/local.toml)" in out, out
    assert "0. the global settings (removes embed_profile and embed_model of .fw-context/local.toml)" in out, out
    assert "removed the profile of .fw-context/local.toml" in out, out
    assert "[ok] Embeddings: the global settings apply" in out, out
    assert _local_llm(project) == {"analyze_symbols": False}, "a key that does not select the backend stays"
    assert 'db_dir = "/tmp/x"' in local.read_text(encoding="utf-8")


def test_enter_does_not_remove_the_keys_of_local_toml(project, monkeypatch, capsys):
    """A removal must be a choice: after "Change? y", Enter keeps."""
    local = project / ".fw-context" / "local.toml"
    local.write_text(local.read_text(encoding="utf-8") + '\n[llm]\nembed_model = "pinned:1b"\n', encoding="utf-8")

    out = _run(monkeypatch, capsys, project, ["y", "", "", ""])

    assert "[ok] Embeddings: the current setting stays" in out, out
    assert _local_llm(project) == {"embed_model": "pinned:1b"}


def test_no_option_0_when_the_committed_config_selects_the_backend(project, monkeypatch, capsys):
    """init writes only local.toml, and cannot remove a committed setting.

    The committed file can only select a profile of the global config.
    """
    (project / ".fw-context" / "config.toml").write_text('[project]\n\n[llm]\nprofile = "local"\n', encoding="utf-8")

    out = _run(monkeypatch, capsys, project, ["y", "", "", ""])

    assert "(set in .fw-context/config.toml)" in out, out
    assert "the global settings" not in out, out


def test_non_interactive_names_the_global_settings(project, monkeypatch, capsys):
    out = _run(monkeypatch, capsys, project, [], non_interactive=True)

    assert f"LLM: {GLOBAL_SUMMARY} (from the global config)" in out, out
    assert "not configured" not in out, out
    assert _local_llm(project) == {}


def test_without_global_settings_enter_skips(project, monkeypatch, capsys):
    from fw_context_mcp.config import settings

    settings._GLOBAL_CONFIG_PATH.write_text("[index]\n", encoding="utf-8")

    out = _run(monkeypatch, capsys, project, ["", "", ""])

    assert "LLM: not configured" in out, out
    assert "the global settings" not in out, out
    assert "(no service: set one up in step 1)" in out, out
    assert "[ok] LLM analysis: the current setting stays" in out, out
    assert _local_llm(project) == {}


@pytest.mark.parametrize(("text", "answers"), [
    # An inline table must come before the first header, else it is a key of that table.
    ('llm = { embed_model = "pinned:1b" }\n\n[index]\ndb_dir = "/tmp/x"\n', ["", "0"]),
    ('[index]\ndb_dir = "/tmp/x"\n\n[llm]  # my settings\nembed_model = "pinned:1b"\n', ["", "0"]),
    # A multi-line string of a backend key: the line editor cannot see where
    # its value ends.  WHY profile: chat_api_key, the key of this case
    # before, is a key of a profile now, and a local.toml with it does not
    # load.  TOML trims the first newline, thus the value is "local".
    # Option 0 of the LLM analysis removes profile; Enter keeps the embeddings.
    ('[index]\ndb_dir = "/tmp/x"\n\n[llm]\nembed_model = "pinned:1b"\nprofile = """\nlocal"""\n', ["0", ""]),
], ids=["inline table", "header with a comment", "multi-line string"])
def test_a_table_that_init_cannot_edit_is_not_changed(project, monkeypatch, capsys, text, answers):
    """Before: "[ok] removed" with the keys still there, or a file that was not valid TOML."""
    local = project / ".fw-context" / "local.toml"
    local.write_text(text, encoding="utf-8")
    before = local.read_text(encoding="utf-8")

    out = _run(monkeypatch, capsys, project, ["y", "", *answers])

    assert "[warn]" in out and "Nothing was changed" in out, out
    assert "[ok] LLM analysis: the global settings apply" not in out, out
    assert "[ok] Embeddings: the global settings apply" not in out, out
    assert local.read_text(encoding="utf-8") == before


def test_option_0_names_a_change_of_the_embedding_model(project, monkeypatch, capsys):
    """Another model embeds each definition again; the user must know it before the run."""
    local = project / ".fw-context" / "local.toml"
    local.write_text(local.read_text(encoding="utf-8") + '\n[llm]\nembed_model = "pinned:1b"\n', encoding="utf-8")

    out = _run(monkeypatch, capsys, project, ["y", "", "", "0"])

    assert "The embedding model changes from pinned:1b to global-embed:1b" in out, out


def test_a_failed_write_of_option_0_says_that_nothing_was_changed(project, monkeypatch, capsys):
    """The editor writes local.toml in one atomic write: when it fails, no "[ok]"."""
    local = project / ".fw-context" / "local.toml"
    local.write_text('[index]\ndb_dir = "/tmp/x"\n\n[llm]\nembed_model = "m:1b"\n', encoding="utf-8")
    before = local.read_text(encoding="utf-8")

    from fw_context_mcp.config import _toml_editor

    def _fails(path, content):
        raise OSError("disk full")

    monkeypatch.setattr(_toml_editor, "_atomic_write", _fails)
    out = _run(monkeypatch, capsys, project, ["y", "", "", "0"])

    assert "[warn] .fw-context/local.toml cannot be changed (disk full). Nothing was changed." in out, out
    assert "[ok] Embeddings: the global settings apply" not in out, out
    assert local.read_text(encoding="utf-8") == before


def _ollama_defaults(monkeypatch, capsys, root: Path, answers: list[str]) -> tuple[list[str | None], str]:
    """Set up the local Ollama (service 1), accept each default; return the defaults of the model prompts and the output.

    *answers* are the answers to the two lists and to their questions of the scope.
    """
    from fw_context_mcp.cli import _init_interactive, _llm_wizard
    from fw_context_mcp.config import load
    from fw_context_mcp.llm import auto_model, client, ollama

    defaults: list[str | None] = []
    monkeypatch.setattr(client, "call_llm", lambda prompt, cfg, **kwargs: "OK")

    def _prompt_text(label, default=None, **kwargs):
        defaults.append(default)
        return default

    monkeypatch.setattr(_llm_wizard, "test_profile", lambda cfg, name, **kwargs: None)
    monkeypatch.setattr(_init_interactive, "prompt_text", _prompt_text)
    monkeypatch.setattr(_init_interactive, "_confirm", lambda question, **kwargs: False)
    monkeypatch.setattr(auto_model, "_gpu_available", lambda: False)
    monkeypatch.setattr(ollama, "check_setup", lambda cfg: {"status": "ok", "installed_models": []})
    _no_machine(monkeypatch)
    replies = iter(["1", *answers])
    monkeypatch.setattr(_init_interactive, "_input", lambda prompt: next(replies))
    _init_interactive.prompt_llm_config(root, load(root), non_interactive=False)
    return defaults, capsys.readouterr().out


def test_the_ollama_setup_offers_the_models_of_the_config(project, monkeypatch, capsys):
    """Before: Enter replaced the models of the global config with the recommended ones.

    The chat model of the config is the default of its prompt.  Enter in
    the list of the embeddings keeps the embedding model of the config.
    """
    defaults, out = _ollama_defaults(monkeypatch, capsys, project, ["", ""])

    assert defaults == ["global-chat:1b"], out
    assert "Current config: global-chat:1b — Enter keeps it" in out, out
    assert _global_llm()["embed_model"] == "global-embed:1b"
    assert _local_llm(project) == {}


def test_the_ollama_setup_offers_the_recommendation_without_a_config(project, monkeypatch, capsys):
    from fw_context_mcp.config import settings

    settings._GLOBAL_CONFIG_PATH.write_text("[index]\n", encoding="utf-8")

    # The embeddings: the new profile "local" (row 1), for this project (1).
    defaults, out = _ollama_defaults(monkeypatch, capsys, project, ["", "1", "1"])

    # No GPU and 16 GiB of RAM, no Ollama: the estimate of llm.recommend from the weights of the list.
    assert defaults == ["gemma4:12b", "qwen3-embedding:4b"], out
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

    defaults, _ = _ollama_defaults(monkeypatch, capsys, project, ["", ""])

    assert defaults == ["gemma4:12b"]
    llm = _global_llm()
    assert llm["profiles"]["api"] == {"provider": "openai_compatible", "url": "http://127.0.0.1:9/v1",
                                      "model": "deepseek-chat"}
    assert llm["profiles"]["local"] == {"url": "http://localhost:11434", "provider": "ollama", "model": "gemma4:12b"}
    assert llm["embed_model"] == "global-embed:1b"


def test_an_embedding_model_that_ollama_does_not_serve_is_not_an_ollama_default(project, monkeypatch, capsys):
    """The step offered to pull it from Ollama, and Enter said yes."""
    from fw_context_mcp.config import settings

    settings._GLOBAL_CONFIG_PATH.write_text(
        GLOBAL_LLM.replace('embed_model = "global-embed:1b"', 'embed_model = "BAAI/bge-small-en-v1.5"'),
        encoding="utf-8",
    )

    # The embeddings: the profile "local" (row 1), for this project (1).
    defaults, _ = _ollama_defaults(monkeypatch, capsys, project, ["", "1", "1"])

    assert defaults == ["global-chat:1b", "qwen3-embedding:4b"]


def test_option_1_writes_the_chat_model_into_the_selected_ollama_profile(project, monkeypatch, capsys):
    """The profile is an Ollama one: after a "yes", the setup changes its model and keeps its other keys.

    It is the global default, thus Enter in the list of the LLM analysis
    keeps it, and local.toml does not select it.  Scope 1 of the
    embeddings writes the model for this project only.
    """
    from fw_context_mcp.config import settings

    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL_LLM + "num_ctx = 8192\n", encoding="utf-8")

    # Service 1; keep the LLM analysis; the embeddings of profile "local" (row 1) for this project (1).
    out, tested = _setup(
        monkeypatch, capsys, project, ["1", "", "1", "1"],
        {"Chat model": "new-chat:1b", "Embedding model": "new-embed:1b"}, shared=True,
    )

    assert "[ok] Profile 'local' answers" in out, out
    assert _global_llm()["profiles"]["local"] == {
        "provider": "ollama", "url": "http://localhost:11434", "model": "new-chat:1b", "num_ctx": 8192,
    }
    assert _local_llm(project) == {"embed_profile": "local", "embed_model": "new-embed:1b"}
    assert ".fw-context/local.toml: embedding model new-embed:1b of profile 'local'" in out, out
    assert "The embedding model changes from global-embed:1b to new-embed:1b" in out, out
    assert _global_llm()["embed_model"] == "global-embed:1b", "the global embedding model stays"
    assert tested == ["new-chat:1b"], "the test call must use the model of the new profile"


def test_option_2_stores_a_key_for_the_profile_and_never_in_a_config_file(project, monkeypatch, capsys):
    """Without a variable, the step reads the key without an echo and stores it before the test call."""
    from fw_context_mcp.config import settings
    from fw_context_mcp.llm import secrets

    key = "sk-init-" + "test"
    # Service 7 (another API); the LLM analysis: row 2 (work, after local) for this project (1);
    # Enter keeps the embeddings.
    # The API gives no model list (conftest): the setup asks the context of the chat model.
    out, tested = _setup(monkeypatch, capsys, project, ["7", "2", "1", ""], {
        "Profile name": "work", "API URL": "http://127.0.0.1:9/v1", "Model": "work-chat", "API key": key,
        "Context of the chat model in tokens (empty: the profile serves the embeddings only)": "8192",
    })

    assert ".fw-context/local.toml selects profile 'work'" in out and key not in out, out
    assert secrets.stored_api_key("work") == key
    assert "api_key_env" not in _global_llm()["profiles"]["work"]
    for path in (settings._GLOBAL_CONFIG_PATH, project / ".fw-context" / "local.toml"):
        assert key not in path.read_text(encoding="utf-8"), path
    assert tested == ["work-chat", "work-chat"], "the test of the new profile, then the test of the selection"


def test_option_2_writes_a_cloud_profile_that_names_the_key_variable(project, monkeypatch, capsys):
    """The profile holds the NAME of the variable, never the key: a key in a TOML file is a secret in plain text."""
    out, tested = _setup(monkeypatch, capsys, project, ["7", "2", "1", ""], {
        "Profile name": "work",
        "API URL": "http://127.0.0.1:9/v1",
        "Environment variable with the API key (empty: store the key in the keyring)": "WORK_API_KEY",
        "Model": "work-chat",
        "Context of the chat model in tokens (empty: the profile serves the embeddings only)": "8192",
    })

    assert "[ok] Profile 'work' saved" in out and ".fw-context/local.toml selects profile 'work'" in out, out
    llm = _global_llm()
    assert llm["profiles"]["work"] == {
        "provider": "openai_compatible", "url": "http://127.0.0.1:9/v1", "model": "work-chat",
        "api_key_env": "WORK_API_KEY", "context": 8192,
    }
    assert llm["profiles"]["local"]["model"] == "global-chat:1b", "the other profiles stay"
    assert llm["profile"] == "local", "the global selection stays: the project selects in local.toml"
    assert _local_llm(project) == {"enabled": True, "profile": "work"}
    assert tested == ["work-chat", "work-chat"], "the test of the new profile, then the test of the selection"


def test_option_2_without_a_url_writes_nothing(project, monkeypatch, capsys):
    # Service 7 without a URL; Enter keeps the LLM analysis and the embeddings.
    out, tested = _setup(monkeypatch, capsys, project, ["7", "", ""], {"API URL": ""})

    assert "[skip] Cloud API setup skipped" in out, out
    assert set(_global_llm()["profiles"]) == {"local"}
    assert _local_llm(project) == {}
    assert tested == []


def test_option_d_turns_off_the_analysis_and_keeps_the_embeddings(project, monkeypatch, capsys):
    """Before: it wrote enabled = false, which also turned off the embeddings, as the menu said "embedding still works"."""
    out = _run(monkeypatch, capsys, project, ["", "d", ""])

    assert "[ok] LLM analysis off ([llm] analyze_symbols = false) — the embeddings stay" in out, out
    assert _local_llm(project) == {"analyze_symbols": False}
    assert "[note]" not in out, out


def test_option_d_says_when_the_llm_is_off(project, monkeypatch, capsys):
    from fw_context_mcp.config import settings

    settings._GLOBAL_CONFIG_PATH.write_text("[llm]\nenabled = false\n", encoding="utf-8")

    out = _run(monkeypatch, capsys, project, ["", "d", ""])

    assert "[note] The LLM is off ([llm] enabled = false): the embeddings need enabled = true" in out, out


def test_option_d_gives_no_ok_when_local_toml_cannot_be_written(project, monkeypatch, capsys):
    from fw_context_mcp.config import settings

    def _fails(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(settings, "_update_local_toml", _fails)

    out = _run(monkeypatch, capsys, project, ["", "d", ""])

    assert "[warn] .fw-context/local.toml cannot be changed (read-only). Nothing was changed." in out, out
    # The list of the embeddings comes after "d": its Enter says "[ok] ... stays".
    assert "[ok]" not in out.split("the embeddings: where they run")[0], out

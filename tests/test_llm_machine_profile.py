"""One Ollama profile for each machine: ``[llm] ollama_profile``, the rule of a project, its writers and doctor.

Decision of the user, 2026-10-21: a project selects an external profile
(an API, the ChatGPT plan, a cloud model of Ollama) or the Ollama profile
of this machine.  No test reaches a server: the lists of the cloud models
are replaced.
"""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

import pytest

from fw_context_mcp.config import settings
from fw_context_mcp.llm import _diag, profile_state

LOCAL = '[llm.profiles.local]\nprovider = "ollama"\nurl = "http://localhost:11434"\nmodel = "chat:14b"\n'
REMOTE = '[llm.profiles.box]\nprovider = "ollama"\nurl = "http://gpu-box:11434"\nmodel = "big:30b"\n'
CLOUD = '[llm.profiles.cloudy]\nprovider = "ollama"\nurl = "http://localhost:11434"\nmodel = "m:cloud"\n'
API = '[llm.profiles.ds]\nprovider = "deepseek"\nmodel = "deepseek-chat"\n'
#: The cloud models of the local Ollama (``/api/tags`` remote_host).
CLOUD_MODELS = {"m:cloud": "https://ollama.com:443"}


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """A global config of its own, a project, and a local Ollama with one cloud model."""
    path = tmp_path / "home" / "config.toml"
    path.parent.mkdir()
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", path)
    monkeypatch.setenv("FW_CONTEXT_HOME", str(path.parent))
    settings._config_cache.clear()
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: dict(CLOUD_MODELS))
    # A config without embed_model: load detects one and asks the server.
    from fw_context_mcp.llm import auto_model

    monkeypatch.setattr(auto_model, "_gpu_available", lambda: False)
    monkeypatch.setattr(auto_model, "_model_installed", lambda url, model: True)
    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    (root / ".fw-context" / "config.toml").write_text("[project]\nid = \"abc123\"\n", encoding="utf-8")
    return root


def _global(text: str) -> None:
    settings._GLOBAL_CONFIG_PATH.write_text(text, encoding="utf-8")
    settings._config_cache.clear()


def _global_llm() -> dict:
    return tomllib.loads(settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))["llm"]


def _local(root: Path, text: str) -> None:
    (root / ".fw-context" / "local.toml").write_text(text, encoding="utf-8")
    settings._config_cache.clear()


def _local_llm(root: Path) -> dict:
    path = root / ".fw-context" / "local.toml"
    return tomllib.loads(path.read_text(encoding="utf-8")).get("llm", {}) if path.is_file() else {}


def _cloud() -> dict:
    return {"http://localhost:11434": dict(CLOUD_MODELS)}


def _llm(text: str) -> dict:
    return tomllib.loads(text)["llm"]


# ── The Ollama profile of this machine ──


@pytest.mark.parametrize(("text", "machine"), [
    ('[llm]\nprofile = "local"\n' + LOCAL + REMOTE, "local"),
    ('[llm]\nprofile = "ds"\nollama_profile = "box"\n' + LOCAL + REMOTE + API, "box"),
    ('[llm]\nprofile = "ds"\n' + LOCAL + API, "local"),
    ('[llm]\nprofile = "ds"\n' + LOCAL + CLOUD + API, "local"),
    ('[llm]\nprofile = "ds"\n' + LOCAL + REMOTE + API, None),
    ('[llm]\nprofile = "ds"\nollama_profile = "cloudy"\n' + LOCAL + CLOUD + API, None),
    ('[llm]\nprofile = "ds"\n' + API, None),
], ids=["the global default", "the key", "the only one", "a cloud model is not one", "several without a key",
        "a key that names a cloud model", "no Ollama profile"])
def test_the_ollama_profile_of_this_machine(text, machine):
    assert profile_state.ollama_profile_name(_llm(text), _cloud()) == machine


@pytest.mark.parametrize(("name", "allowed"), [
    ("local", True), ("ds", True), ("cloudy", True), ("box", False),
])
def test_a_project_selects_an_external_profile_or_the_ollama_profile_of_this_machine(name, allowed):
    """An Ollama on another computer counts as Ollama: it shares the memory of that server."""
    llm = _llm('[llm]\nprofile = "local"\n' + LOCAL + REMOTE + CLOUD + API)

    problem = profile_state.project_choice_problem(name, llm, _cloud())

    assert (problem is None) == allowed
    if not allowed:
        assert "'local'" in problem and "--llm box" in problem


# ── The checks of the config ──


def test_ollama_profile_in_a_project_file_is_a_warning():
    problems = settings.config_value_problems({"llm": {"ollama_profile": "local"}}, global_file=False)

    assert [p.critical for p in problems] == [False] and "global config only" in problems[0].message


@pytest.mark.parametrize("value", ["ds", "missing", 3])
def test_ollama_profile_that_names_no_ollama_profile_is_a_warning(value):
    layer = {"llm": {"ollama_profile": value, "profiles": _llm("[llm]\n" + API)["profiles"]}}

    problems = settings.config_value_problems(layer)

    assert len(problems) == 1 and not problems[0].critical


def test_a_project_that_selects_another_ollama_profile_still_loads(home):
    """Load refuses nothing: the old projects run on, doctor reports them."""
    from fw_context_mcp.config import load

    # embed_model: else load detects one and asks the server of the profile.
    _global('[llm]\nprofile = "local"\nembed_model = "e:1b"\n' + LOCAL + REMOTE)
    _local(home, '[llm]\nprofile = "box"\n')

    assert load(home).llm.model == "big:30b"


# ── fw-context llm default ──


def _default(home: Path, profile: str, *, scope: str = "") -> int:
    from fw_context_mcp.cli import _llm

    return _llm.cmd_llm_default(argparse.Namespace(profile=profile, global_default=scope == "global",
                                                   ollama=scope == "ollama", project=str(home)))


def test_default_ollama_makes_the_ollama_profile_of_this_machine(home, capsys):
    _global('[llm]\nprofile = "ds"\n' + LOCAL + REMOTE + API)

    assert _default(home, "box", scope="ollama") == 0

    assert _global_llm()["ollama_profile"] == "box" and _global_llm()["profile"] == "ds"


def test_default_ollama_refuses_a_cloud_model_and_an_api(home, capsys):
    _global('[llm]\nprofile = "local"\n' + LOCAL + CLOUD + API)

    assert _default(home, "cloudy", scope="ollama") == 1
    assert _default(home, "ds", scope="ollama") == 1
    assert "ollama_profile" not in _global_llm()


def test_default_global_with_an_ollama_profile_makes_it_the_ollama_profile_too(home):
    _global('[llm]\nprofile = "local"\n' + LOCAL + REMOTE)

    assert _default(home, "box", scope="global") == 0

    assert (_global_llm()["profile"], _global_llm()["ollama_profile"]) == ("box", "box")


def test_default_global_with_an_api_keeps_the_ollama_profile(home):
    """Without the key, the Ollama profile followed the global default: the key keeps it."""
    _global('[llm]\nprofile = "local"\n' + LOCAL + REMOTE + API)

    assert _default(home, "ds", scope="global") == 0

    assert (_global_llm()["profile"], _global_llm()["ollama_profile"]) == ("ds", "local")


def test_a_project_cannot_select_another_ollama_profile(home, monkeypatch, capsys):
    from fw_context_mcp.llm import configure

    monkeypatch.setattr(configure, "configure_llm_core", lambda *a, **k: pytest.fail("no write"))
    _global('[llm]\nprofile = "local"\n' + LOCAL + REMOTE)

    assert _default(home, "box") == 1

    assert "--llm box" in capsys.readouterr().err


def test_the_mcp_tool_refuses_another_ollama_profile(home, monkeypatch):
    from fw_context_mcp.llm import configure
    from fw_context_mcp.mcp.handlers.maintenance import configure_llm

    monkeypatch.setattr(configure, "configure_llm_core", lambda *a, **k: pytest.fail("no write"))
    _global('[llm]\nprofile = "local"\n' + LOCAL + REMOTE)

    result = configure_llm(project_root=str(home), profile="box")

    assert result["status"] == "error" and "--llm box" in result["message"]


# ── The check llm-profile of doctor, and its fix ──


def _check(home: Path):
    from fw_context_mcp.deps._checks import check_llm_profile

    return check_llm_profile(home)


def test_the_check_reports_another_ollama_profile_and_the_fix_selects_the_one_of_this_machine(home):
    """The global default is external: the fix must not remove the selection, else the code goes out."""
    from fw_context_mcp.deps._fixes import fix_llm_profile

    _global('[llm]\nprofile = "ds"\nollama_profile = "local"\n' + LOCAL + REMOTE + API)
    _local(home, '[llm]\nprofile = "box"\n')

    result = _check(home)

    assert result.status == "degraded" and result.fix_cmd == "fw-context doctor --fix --only llm-profile"
    assert fix_llm_profile(result, home)[0]
    assert _local_llm(home)["profile"] == "local"
    assert _check(home).status == "ok"


def test_the_fix_removes_the_selection_when_the_ollama_profile_is_the_global_default(home):
    from fw_context_mcp.deps._fixes import fix_llm_profile

    _global('[llm]\nprofile = "local"\n' + LOCAL + REMOTE)
    _local(home, '[llm]\nprofile = "box"\nembed_model = "e:1b"\n')

    assert fix_llm_profile(_check(home), home)[0]

    assert "profile" not in _local_llm(home)


def test_a_selection_of_config_toml_gets_no_fix(home):
    _global('[llm]\nprofile = "local"\n' + LOCAL + REMOTE)
    (home / ".fw-context" / "config.toml").write_text('[project]\nid = "abc123"\n\n[llm]\nprofile = "box"\n', encoding="utf-8")

    result = _check(home)

    assert result.status == "degraded" and result.fix_cmd is None and "config.toml" in result.message


def test_an_external_profile_and_a_cloud_model_are_no_problem(home):
    _global('[llm]\nprofile = "local"\n' + LOCAL + CLOUD + API)
    for name in ("ds", "cloudy"):
        _local(home, f'[llm]\nprofile = "{name}"\n')

        assert _check(home).status == "ok", name


def test_without_the_model_list_of_ollama_no_profile_is_reported(home, monkeypatch):
    """A cloud model then looks like a model of this machine: the fix would change the choice of the user."""
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: None)
    _global('[llm]\nprofile = "local"\n' + LOCAL + CLOUD)
    _local(home, '[llm]\nprofile = "cloudy"\n')

    result = _check(home)

    assert result.status == "ok" and "gives no model list" in result.message


def test_several_ollama_profiles_without_the_key_name_the_command(home):
    _global('[llm]\nprofile = "ds"\n' + LOCAL + REMOTE + API)

    result = _check(home)

    assert result.status == "degraded" and "llm default <profile> --ollama" in result.message
    assert result.fix_cmd is None


def test_an_embedding_model_of_the_project_is_reported_and_never_fixed(home):
    """A new embedding model embeds each symbol again: hours for a large project, the user decides."""
    _global('[llm]\nprofile = "local"\nembed_model = "qwen3-embedding:4b"\n' + LOCAL)
    _local(home, '[llm]\nembed_model = "qwen3-embedding:8b"\n')

    result = _check(home)

    assert result.status == "degraded" and result.fix_cmd is None
    assert "remove embed_model from .fw-context/local.toml" in result.message


def test_the_same_embedding_model_is_no_problem(home):
    _global('[llm]\nprofile = "local"\nembed_model = "qwen3-embedding:8b"\n' + LOCAL)
    _local(home, '[llm]\nembed_model = "qwen3-embedding:8b"\n')

    assert _check(home).status == "ok"


def test_the_recheck_after_the_fix_knows_the_check(home):
    from fw_context_mcp import deps

    _global('[llm]\nprofile = "local"\n' + LOCAL)

    assert deps._recheck("llm-profile", home).name == "llm-profile"


# ── The local Ollama setup ──


def _setup(monkeypatch, root: Path, inputs: list[str], answers: dict[str, bool], chat: str = "chat:14b",
           embed: str = "e:4b"):
    """Run the LLM step of init with *inputs* for the lists; *answers* maps a part of a question to the answer (else no).

    *inputs*: "Change?" (only when the project selects the backend), the
    services (1: the local Ollama), the list of the LLM analysis and the
    list of the embeddings, each with its question of the scope.
    """
    from fw_context_mcp.cli import _init_interactive as ui
    from fw_context_mcp.cli import _llm_wizard
    from fw_context_mcp.config import load
    from fw_context_mcp.llm import auto_model, client, hardware, ollama

    monkeypatch.setattr(_llm_wizard, "test_profile", lambda cfg, name: None)
    # The test request of a selection for this project (configure_llm_core).
    monkeypatch.setattr(client, "call_llm", lambda prompt, cfg, **kwargs: "OK")
    monkeypatch.setattr(ollama, "check_setup", lambda cfg: {"status": "ok", "ollama_running": True,
                                                            "installed_models": []})
    monkeypatch.setattr(ollama, "calibration_key", lambda *args: None)
    monkeypatch.setattr(_diag, "installed_models", lambda url: [])
    monkeypatch.setattr(hardware, "machine_memory", lambda: hardware.Memory(gpu_mib=None, ram_mib=16 * 1024))
    monkeypatch.setattr(auto_model, "_gpu_available", lambda: False)
    monkeypatch.setattr(ui, "prompt_text", lambda label, default=None, **kwargs: {
        "Chat model": chat, "Embedding model": embed}.get(label, default))
    monkeypatch.setattr(ui, "_confirm", lambda question, **kwargs: next(
        (value for part, value in answers.items() if part in question), False))
    replies = iter(inputs)
    monkeypatch.setattr(ui, "_input", lambda prompt: next(replies))
    ui.prompt_llm_config(root, load(root), non_interactive=False)


@pytest.mark.parametrize("remove", [True, False], ids=["yes removes the own model", "no keeps it"])
def test_the_setup_writes_the_embedding_model_to_the_global_config_for_each_project(home, monkeypatch, capsys,
                                                                                   remove):
    """Scope 2 writes the global default; the own model of local.toml goes only after a "yes".

    WHY a question: the own model of local.toml wins over the new global
    one, and a removal embeds each symbol again at the next index run.
    """
    _global('[llm]\nprofile = "local"\nembed_model = "e:8b"\n' + LOCAL)
    _local(home, '[llm]\nembed_model = "e:8b"\n')

    # Change, service 1, keep the LLM analysis, the embeddings of "local" (1) for each project (2).
    _setup(monkeypatch, home, ["y", "1", "", "1", "2"], {"sets its own embedding model": remove})

    assert (_global_llm()["embed_model"], _global_llm()["embed_profile"]) == ("e:4b", "local")
    out = capsys.readouterr().out
    if remove:
        assert "embed_model" not in _local_llm(home), out
    else:
        assert _local_llm(home) == {"embed_model": "e:8b"}
        assert "[note] .fw-context/local.toml of this project keeps its own embeddings" in out, out


def test_the_setup_selects_the_ollama_profile_in_local_toml_when_the_global_default_is_external(home, monkeypatch):
    _global('[llm]\nprofile = "ds"\nembed_model = "e:4b"\n' + LOCAL + API)

    # Service 1; the LLM analysis: "local" (row 1, before the external ds) for this project (1); keep the embeddings.
    _setup(monkeypatch, home, ["1", "1", "1", ""], {})

    assert _local_llm(home)["profile"] == "local" and "embed_model" not in _local_llm(home)


# ── The findings of the review of 2026-10-21 ──


def test_the_fix_keeps_the_selection_when_config_toml_selects_an_external_profile(home):
    """Before: the fix removed the key of local.toml, and the DeepSeek profile of config.toml then applied."""
    from fw_context_mcp.config import load
    from fw_context_mcp.deps._fixes import fix_llm_profile

    _global('[llm]\nprofile = "local"\n' + LOCAL + REMOTE + API)
    (home / ".fw-context" / "config.toml").write_text('[project]\nid = "abc123"\n\n[llm]\nprofile = "ds"\n',
                                                      encoding="utf-8")
    _local(home, '[llm]\nprofile = "box"\n')

    assert fix_llm_profile(_check(home), home)[0]

    assert _local_llm(home)["profile"] == "local" and load(home).llm.provider == "ollama"


def test_the_fix_does_not_send_the_code_to_another_computer(home):
    _global('[llm]\nprofile = "box"\n' + LOCAL + REMOTE)
    _local(home, '[llm]\nprofile = "local"\n')

    result = _check(home)

    assert result.status == "degraded" and result.fix_cmd is None and "does not send the code" in result.message


def test_a_global_default_that_is_another_ollama_profile_is_reported(home):
    _global('[llm]\nprofile = "box"\nollama_profile = "local"\n' + LOCAL + REMOTE)

    result = _check(home)

    assert result.status == "degraded" and "the global default profile 'box'" in result.message


def test_default_ollama_warns_about_a_global_default_of_another_ollama_profile(home, capsys):
    _global('[llm]\nprofile = "box"\n' + LOCAL + REMOTE)

    assert _default(home, "local", scope="ollama") == 0

    assert "global default profile 'box' is another Ollama profile" in capsys.readouterr().err


@pytest.mark.parametrize("scope", ["", "ollama", "global"])
def test_without_the_model_list_of_ollama_no_writer_decides_about_an_ollama_profile(home, monkeypatch, capsys, scope):
    """A cloud model then looks like a local one: --ollama would write it as the profile of this machine."""
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: None)
    _global('[llm]\nprofile = "local"\n' + LOCAL + CLOUD)

    assert _default(home, "cloudy", scope=scope) == 1

    assert "gives no model list" in capsys.readouterr().err and "ollama_profile" not in _global_llm()


def test_without_the_model_list_an_external_profile_can_still_be_selected(home, monkeypatch):
    from fw_context_mcp.llm import configure

    monkeypatch.setattr(_diag, "remote_hosts", lambda url: None)
    monkeypatch.setattr(configure, "configure_llm_core", lambda root, updates, **kwargs: {"status": "ok"})
    _global('[llm]\nprofile = "local"\n' + LOCAL + API)

    assert _default(home, "ds") == 0


def test_without_the_model_list_the_mcp_tool_decides_nothing(home, monkeypatch):
    from fw_context_mcp.llm import configure
    from fw_context_mcp.mcp.handlers.maintenance import configure_llm

    monkeypatch.setattr(_diag, "remote_hosts", lambda url: None)
    monkeypatch.setattr(configure, "configure_llm_core", lambda *a, **k: pytest.fail("no write"))
    _global('[llm]\nprofile = "ds"\n' + LOCAL + CLOUD + API)

    result = configure_llm(project_root=str(home), profile="cloudy")

    assert result["status"] == "error" and "gives no model list" in result["message"]


def test_without_the_model_list_doctor_fixes_nothing(home, monkeypatch):
    """Before: a global cloud profile looked like the Ollama profile, and the fix moved the project to the cloud."""
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: None)
    _global('[llm]\nprofile = "cloudy"\n' + LOCAL + CLOUD + REMOTE)
    _local(home, '[llm]\nprofile = "box"\n')

    result = _check(home)

    assert result.status == "ok" and result.fix_cmd is None and "gives no model list" in result.message


def test_the_setup_selects_the_profile_when_config_toml_selects_another_one(home, monkeypatch):
    """Before: the key went (the global default is local), and the DeepSeek profile of config.toml applied."""
    _global('[llm]\nprofile = "local"\nembed_model = "e:4b"\n' + LOCAL + API)
    (home / ".fw-context" / "config.toml").write_text(
        '[project]\nid = "abc123"\n\n[llm]\nprofile = "ds"\nembed_model = "e:8b"\n', encoding="utf-8")

    # Change; service 1; the LLM analysis "local" (row 1) for this project (1); the embeddings of
    # "local" (row 1) for this project (1).
    _setup(monkeypatch, home, ["y", "1", "1", "1", "1", "1"], {})

    assert _local_llm(home)["profile"] == "local"
    # config.toml sets another embedding model: only local.toml selects this one over it.
    assert _local_llm(home)["embed_model"] == "e:4b" and _global_llm()["embed_model"] == "e:4b"


def test_the_setup_never_changes_a_profile_local_of_an_api(home, monkeypatch):
    """Before: a "no" to the shared question still wrote provider ollama into it, and its key variable was lost."""
    _global('[llm]\nprofile = "local"\n\n[llm.profiles.local]\nprovider = "openai_compatible"\n'
            'url = "https://api.example.test/v1"\nmodel = "m"\napi_key_env = "K"\n')

    # Service 1; the LLM analysis: "local-2" (row 1: the external "local" comes last) for this project (1).
    _setup(monkeypatch, home, ["1", "1", "1", ""], {})

    profiles = _global_llm()["profiles"]
    assert profiles["local"]["api_key_env"] == "K" and profiles["local-2"]["provider"] == "ollama"
    # The only Ollama profile: the rule names it without the key, and local.toml selects it.
    assert profile_state.ollama_profile_name(_global_llm(), _cloud()) == "local-2"
    assert _local_llm(home)["profile"] == "local-2"


def test_a_failed_write_of_the_global_embedding_model_puts_the_global_config_back(home, monkeypatch, capsys):
    _global('[llm]\nprofile = "local"\nembed_model = "e:8b"\n' + LOCAL)
    before = settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8")

    def _fail(key, value):
        raise OSError("disk full")

    monkeypatch.setattr(settings, "update_global_llm_key", _fail)

    # No service; keep the LLM analysis; the embeddings of "local" (row 1) for each project (2).
    _setup(monkeypatch, home, ["", "", "1", "2"], {})

    assert settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8") == before
    assert "The global config is not changed: disk full" in capsys.readouterr().out


def test_the_setup_never_takes_a_profile_local_of_a_cloud_model(home, monkeypatch):
    """Before: a "no" kept the cloud model, it became the Ollama profile of this machine, and the code went out."""
    _global('[llm]\nprofile = "ds"\n\n[llm.profiles.local]\nprovider = "ollama"\nurl = "http://localhost:11434"\n'
            'model = "m:cloud"\n\n' + API)

    # Service 1; the LLM analysis: "local-2" (row 1: the cloud model and ds go out) for this project (1).
    _setup(monkeypatch, home, ["1", "1", "1", ""], {})

    profiles = _global_llm()["profiles"]
    assert profiles["local"]["model"] == "m:cloud" and profiles["local-2"]["model"] == "chat:14b"
    assert _local_llm(home)["profile"] == "local-2"


def test_default_global_with_an_api_needs_no_model_list_when_the_key_is_there(home, monkeypatch):
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: None)
    _global('[llm]\nprofile = "local"\nollama_profile = "local"\n' + LOCAL + API)

    assert _default(home, "ds", scope="global") == 0

    assert (_global_llm()["profile"], _global_llm()["ollama_profile"]) == ("ds", "local")


def test_without_the_model_list_the_check_claims_nothing(home, monkeypatch):
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: None)
    _global('[llm]\nprofile = "local"\n' + LOCAL)

    message = _check(home).message

    assert "gives no model list" in message and "uses the Ollama profile" not in message

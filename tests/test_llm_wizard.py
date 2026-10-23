"""The setup of the LLM services (``cli/_llm_wizard.py``) and ``fw-context llm``.

No test sends a request out: the Ollama lists, the test request, the
GPU query and the keyring are replaced (the keyring of the session fails
each call, thus a key goes to secrets.toml of the test directory).  The
memory of the machine: ``test_hardware.py``.
"""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from fw_context_mcp.cli import _init_interactive as ui
from fw_context_mcp.cli import _llm_wizard as wizard
from fw_context_mcp.config import settings
from fw_context_mcp.llm import _diag, hardware, ollama, secrets

GLOBAL = (
    '[llm]\nprofile = "local"\nenabled = true\nembed_model = "e:1b"\n\n'
    '[llm.profiles.local]\nprovider = "ollama"\nurl = "http://localhost:11434"\nmodel = "chat:1b"\n'
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "home" / "config.toml"
    path.parent.mkdir()
    path.write_text(GLOBAL, encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", path)
    monkeypatch.setenv("FW_CONTEXT_HOME", str(path.parent))
    monkeypatch.setattr(secrets, "_found", {})
    monkeypatch.setattr(secrets, "_told", set())
    settings._config_cache.clear()
    monkeypatch.setattr(hardware, "machine_memory", lambda: hardware.Memory(gpu_mib=None, ram_mib=16 * 1024))
    monkeypatch.setattr(_diag, "installed_models", lambda url: ["chat:1b"])
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: {})
    # No key: gather then never asks a server for a model file, and never
    # measures (a test that installs a model of the list stays offline).
    monkeypatch.setattr(ollama, "calibration_key", lambda *args: None)
    # The test request of a selection (configure_llm_core): no request leaves the test.
    from fw_context_mcp.llm import client

    monkeypatch.setattr(client, "call_llm", lambda prompt, cfg, **kwargs: "OK")
    # A key of the operator would make a profile without a key ready.
    for name in ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    from fw_context_mcp.llm.model_list import ModelInfo

    # The lists of DeepSeek and of the ChatGPT plan, each with the context of its models.
    _fake_model_lists(monkeypatch, {
        "deepseek": [ModelInfo("deepseek-flash", 1048576), ModelInfo("deepseek-chat", 1048576),
                     ModelInfo("new-model", 131072)],
        "chatgpt": [ModelInfo("gpt-6-luna", 272000), ModelInfo("m", 128000)],
    })
    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    (root / ".fw-context" / "config.toml").write_text("[project]\n", encoding="utf-8")
    return root


def _fake_model_lists(monkeypatch, lists: dict) -> None:
    """Give *lists* (provider → [ModelInfo]) as the model list of each API provider; another provider has none.

    WHY: the conftest blocks the GET of a real list, and the setup and the
    selection of an API profile need the list for the names and the context.
    """
    from fw_context_mcp.llm import model_list

    def _models(llm):
        if llm.provider not in lists:
            raise model_list.ModelListError(f"no model list of {llm.provider} in this test")
        return lists[llm.provider]

    monkeypatch.setattr(model_list, "provider_models", _models)


def _profiles() -> dict:
    return tomllib.loads(settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))["llm"].get("profiles", {})


def _answers(monkeypatch, inputs: list[str], texts: dict[str, str] | None = None, *, confirm: bool = False,
             secret: str = "") -> None:
    replies = iter(inputs)
    monkeypatch.setattr(ui, "_input", lambda prompt: next(replies))
    monkeypatch.setattr(ui, "prompt_text", lambda label, default=None, **kwargs: (texts or {}).get(label, default))
    monkeypatch.setattr(ui, "_confirm", lambda question, **kwargs: confirm)
    monkeypatch.setattr(ui, "_read_secret", lambda prompt: secret)


def _run(home: Path):
    from fw_context_mcp.config import load

    wizard.run_wizard(home, load(home))


#: The answers of run_wizard after the services when the user keeps each
#: setting: the list of the LLM analysis, the list of the embeddings, the removal.
KEEP = ["", "", ""]


# ── The chat of one run: the embeddings stay ──


def test_a_profile_of_one_run_keeps_the_embeddings(home):
    from fw_context_mcp.config import load

    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL + (
        '\n[llm.profiles.gpu]\nprovider = "ollama"\nurl = "http://gpu-box:11434"\nmodel = "big:30b"\n'
        'keep_alive = "1m"\n\n[llm.profiles.ds]\nprovider = "deepseek"\nmodel = "deepseek-v4-flash"\n'
    ), encoding="utf-8")
    base = load(home).llm

    gpu = settings.profile_llm_config(base, "gpu")
    ds = settings.profile_llm_config(base, "ds")

    assert (gpu.model, gpu.chat_api_base, gpu.chat_api_format) == ("big:30b", "http://gpu-box:11434", "ollama")
    assert (ds.provider, ds.chat_api_base) == ("deepseek", "https://api.deepseek.com")
    for run in (gpu, ds):
        assert (run.ollama_url, run.embed_model, run.keep_alive) == (base.ollama_url, base.embed_model, base.keep_alive)


def test_an_unknown_profile_of_one_run_lists_the_profiles(home):
    from fw_context_mcp.config import load

    with pytest.raises(settings.ConfigError, match="profiles: local"):
        settings.profile_llm_config(load(home).llm, "nope")


# ── Ollama Cloud: the server tells where a model runs ──


def test_a_cloud_model_is_an_external_host(home):
    from fw_context_mcp.llm.profile_state import profile_state

    state = profile_state("c", {"provider": "ollama", "url": "http://localhost:11434", "model": "gpt-oss:120b-cloud"},
                          cloud={"http://localhost:11434": {"gpt-oss:120b-cloud": "https://ollama.com"}})

    assert (state.host, state.external) == ("https://ollama.com", True)


def test_the_remote_host_comes_from_the_tag_list(monkeypatch):
    import httpx

    body = {"models": [{"name": "gpt-oss:20b-cloud", "remote_host": "https://ollama.com"}, {"name": "local:1b"}]}
    monkeypatch.setattr(httpx, "get", lambda url, timeout: httpx.Response(200, json=body, request=httpx.Request("GET", url)))

    assert _diag.remote_hosts("http://localhost:11434") == {"gpt-oss:20b-cloud": "https://ollama.com"}


def test_the_chat_of_a_cloud_model_is_told_one_time(monkeypatch, capsys):
    from fw_context_mcp.llm import ollama

    monkeypatch.setattr(ollama, "_cloud_checked", set())
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: {"m:cloud": "https://ollama.com"})
    cfg = settings.LLMConfig(profile="c", model="m:cloud")

    ollama._tell_cloud_model(cfg, "http://localhost:11434")
    ollama._tell_cloud_model(cfg, "http://localhost:11434")

    assert capsys.readouterr().err.count("source code snippets go to https://ollama.com") == 1


# ── The setup ──


def test_the_list_marks_the_services_with_a_profile_and_the_external_ones(home, monkeypatch, capsys):
    _answers(monkeypatch, ["", *KEEP])

    _run(home)

    out = capsys.readouterr().out
    assert "1. ✓ Local Ollama — models on the GPU or CPU of this machine; no cost  profiles: local" in out, out
    assert "5.   OpenRouter — models of many providers with one API key; a price per token  ⚠ to openrouter.ai" in out, out


def test_a_service_of_the_registry_stores_the_key_of_its_provider_and_a_tested_profile(home, monkeypatch, capsys):
    tested: list[str] = []
    monkeypatch.setattr(wizard, "test_profile", lambda cfg, name, **kwargs: tested.append(name))
    _answers(monkeypatch, ["6", *KEEP], secret="sk-test-" + "key")

    _run(home)

    # Enter: the recommended model, which the list of DeepSeek holds; the list gives its context.
    assert _profiles()["deepseek"] == {"provider": "deepseek", "model": "deepseek-flash"}
    assert secrets.stored_api_key(secrets.provider_entry("deepseek")) == "sk-test-key"
    assert tested == ["deepseek"]
    assert "sk-test-key" not in capsys.readouterr().out


def test_a_profile_that_does_not_answer_is_removed_unless_the_user_keeps_it(home, monkeypatch, capsys):
    monkeypatch.setattr(wizard, "test_profile", lambda cfg, name, **kwargs: "HTTP 401")
    _answers(monkeypatch, ["6", *KEEP], secret="sk-x")

    _run(home)

    assert "deepseek" not in _profiles()
    assert "does not answer: HTTP 401" in capsys.readouterr().out


def test_another_computer_offers_its_models(home, monkeypatch):
    monkeypatch.setattr(wizard, "test_profile", lambda cfg, name, **kwargs: None)
    monkeypatch.setattr(_diag, "installed_models", lambda url: ["big:30b"] if "gpu-box" in url else ["chat:1b"])
    _answers(monkeypatch, ["2", *KEEP], {"URL of the Ollama server (for example http://192.168.1.20:11434)":
                                          "http://gpu-box:11434"})

    _run(home)

    assert _profiles()["remote"] == {"provider": "ollama", "url": "http://gpu-box:11434", "model": "big:30b"}


def test_ollama_cloud_offers_the_cloud_models_of_the_local_server(home, monkeypatch):
    monkeypatch.setattr(wizard, "test_profile", lambda cfg, name, **kwargs: None)
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: {"gpt-oss:20b-cloud": "https://ollama.com"})
    _answers(monkeypatch, ["3", *KEEP])

    _run(home)

    assert _profiles()["ollama-cloud"]["model"] == "gpt-oss:20b-cloud"


def test_a_removal_asks_and_removes_the_own_key(home, monkeypatch):
    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL + '\n[llm.profiles.work]\nprovider = "openrouter"\nmodel = "m"\n',
                                            encoding="utf-8")
    secrets.set_api_key("work", "sk-work")
    _answers(monkeypatch, ["", "", "", "work"], confirm=True)

    _run(home)

    assert "work" not in _profiles()
    assert secrets.stored_api_key("work") is None


def test_the_profiles_to_remove_are_one_on_each_line_and_a_number_removes_one(home, monkeypatch, capsys):
    """Request of the user, 2026-10-23: one choice on each line, and a number for each."""
    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL + '\n[llm.profiles.work]\nprovider = "openrouter"\nmodel = "m"\n',
                                            encoding="utf-8")
    _answers(monkeypatch, ["", "", "", "2"], confirm=True)

    _run(home)

    assert "work" not in _profiles()
    assert ("    1. local: Local Ollama, chat:1b  — in use: the default of the projects without their own selection, "
            "their embeddings too; the Ollama profile of this machine, and the local Ollama answers\n"
            "    2. work: OpenRouter, m\n") in capsys.readouterr().out


def test_the_global_default_cannot_be_removed(home, monkeypatch, capsys):
    _answers(monkeypatch, ["", "", "", "local"], confirm=True)

    _run(home)

    assert "local" in _profiles()
    assert "is in use (the default of the projects without their own selection" in capsys.readouterr().out


def test_the_list_names_the_command_that_repairs_a_profile(home, monkeypatch, capsys):
    """A profile of an API without a key is listed with the command that repairs it."""
    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL + '\n[llm.profiles.or]\nprovider = "openrouter"\nmodel = "m"\n',
                                            encoding="utf-8")
    _answers(monkeypatch, ["", *KEEP])

    _run(home)

    out = capsys.readouterr().out
    assert "or: OpenRouter, m  ⚠ https://openrouter.ai/api/v1  (no API key: run 'fw-context llm key set or')" in out, out


def test_the_project_selects_a_profile(home, monkeypatch, capsys):
    # The LLM analysis: profile 1 (local) for this project (1); keep the embeddings, no removal.
    _answers(monkeypatch, ["", "1", "1", "", ""])

    _run(home)

    local = tomllib.loads((home / ".fw-context" / "local.toml").read_text(encoding="utf-8"))
    assert local["llm"]["profile"] == "local"


# ── fw-context llm ──


def test_list_shows_the_host_and_the_default(home, monkeypatch, capsys):
    from fw_context_mcp.cli import _llm

    assert _llm.cmd_llm_list(argparse.Namespace(project=str(home))) == 0

    assert "local: ollama chat:1b, this machine [global default]" in capsys.readouterr().out


def test_default_global_selects_the_profile_of_each_project(home):
    from fw_context_mcp.cli import _llm

    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL + '\n[llm.profiles.ds]\nprovider = "deepseek"\nmodel = "m"\n',
                                            encoding="utf-8")

    assert _llm.cmd_llm_default(argparse.Namespace(profile="ds", global_default=True, ollama=False, project=None)) == 0

    assert tomllib.loads(settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))["llm"]["profile"] == "ds"


def test_test_names_an_unknown_profile(home, capsys):
    from fw_context_mcp.cli import _llm

    assert _llm.cmd_llm_test(argparse.Namespace(profile="nope", project=str(home))) == 1

    assert "has no profile 'nope'" in capsys.readouterr().err


# ── The findings of the review of 2026-10-21 ──


def test_the_local_service_does_not_take_a_profile_of_another_computer(home, monkeypatch):
    """A selected profile "remote" got the local model before, and kept the URL of the other computer."""
    from fw_context_mcp.config import load
    from fw_context_mcp.llm import auto_model, ollama

    settings._GLOBAL_CONFIG_PATH.write_text(
        '[llm]\nprofile = "remote"\nembed_model = "e:1b"\n\n'
        '[llm.profiles.remote]\nprovider = "ollama"\nurl = "http://gpu-box:11434"\nmodel = "big:30b"\n',
        encoding="utf-8")
    monkeypatch.setattr(auto_model, "_gpu_available", lambda: False)
    monkeypatch.setattr(ollama, "check_setup", lambda cfg: {"status": "ok", "installed_models": []})
    monkeypatch.setattr(wizard, "test_profile", lambda cfg, name, **kwargs: None)
    _answers(monkeypatch, ["1", *KEEP])
    # Yes only to the question that makes "local" the Ollama profile of this machine: no pull.
    monkeypatch.setattr(ui, "_confirm", lambda question, **kwargs: "Ollama profile of each project" in question)

    wizard.run_wizard(home, load(home))

    llm = tomllib.loads(settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))["llm"]
    assert llm["profiles"]["remote"] == {"provider": "ollama", "url": "http://gpu-box:11434", "model": "big:30b"}
    assert llm["profiles"]["local"]["url"] == "http://localhost:11434"
    assert llm["ollama_profile"] == "local"


def test_the_local_service_stops_when_the_ollama_profile_of_another_computer_stays(home, monkeypatch, capsys):
    """One Ollama profile for each machine: a "no" writes no second one."""
    from fw_context_mcp.config import load
    from fw_context_mcp.llm import auto_model, ollama

    settings._GLOBAL_CONFIG_PATH.write_text(
        '[llm]\nprofile = "remote"\nembed_model = "e:1b"\n\n'
        '[llm.profiles.remote]\nprovider = "ollama"\nurl = "http://gpu-box:11434"\nmodel = "big:30b"\n',
        encoding="utf-8")
    monkeypatch.setattr(auto_model, "_gpu_available", lambda: False)
    monkeypatch.setattr(ollama, "check_setup", lambda cfg: {"status": "ok", "installed_models": []})
    monkeypatch.setattr(wizard, "test_profile", lambda cfg, name, **kwargs: None)
    _answers(monkeypatch, ["1", *KEEP])

    wizard.run_wizard(home, load(home))

    assert set(_profiles()) == {"remote"}
    assert "llm default <profile> --ollama" in capsys.readouterr().out


def test_a_failed_test_puts_back_the_profile_that_it_replaced(home, monkeypatch):
    """Before, the profile was removed with its earlier values: each project that selected it stopped."""
    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL + '\n[llm.profiles.deepseek]\nprovider = "deepseek"\n'
                                            'model = "old-model"\n', encoding="utf-8")
    monkeypatch.setattr(wizard, "test_profile", lambda cfg, name, **kwargs: "HTTP 500")
    _answers(monkeypatch, ["6", *KEEP], {"Model": "new-model"}, secret="sk-x")
    monkeypatch.setattr(ui, "_confirm", lambda question, **kwargs: "Replace" in question)

    _run(home)

    assert _profiles()["deepseek"]["model"] == "old-model"


def test_the_chatgpt_sign_in_belongs_to_the_name_that_the_user_selected(home, monkeypatch):
    """Before, the sign-in came first, and a rename left the tokens under the old name."""
    from fw_context_mcp.llm import chatgpt_plan

    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL + '\n[llm.profiles.chatgpt]\nprovider = "chatgpt"\nmodel = "x"\n',
                                            encoding="utf-8")
    signed: list[str] = []
    monkeypatch.setattr(chatgpt_plan, "login", lambda name, open_url: signed.append(name))
    monkeypatch.setattr(wizard, "test_profile", lambda cfg, name, **kwargs: None)
    _answers(monkeypatch, ["4", *KEEP], {"Another profile name (empty: skip)": "chatgpt2"})

    _run(home)

    assert signed == ["chatgpt2"]
    assert _profiles()["chatgpt2"] == {"provider": "chatgpt", "model": "gpt-6-luna"}


def test_a_failed_chatgpt_profile_does_not_leave_a_sign_in(home, monkeypatch):
    from fw_context_mcp.llm import chatgpt_plan

    logged_out: list[str] = []
    monkeypatch.setattr(chatgpt_plan, "login", lambda name, open_url: None)
    monkeypatch.setattr(chatgpt_plan, "load_credentials", lambda name: chatgpt_plan.PlanCredentials(
        client_id="c", host_id="h", access_token="a", refresh_token="r"))
    monkeypatch.setattr(chatgpt_plan, "logout", lambda name: logged_out.append(name) or True)
    monkeypatch.setattr(wizard, "test_profile", lambda cfg, name, **kwargs: "HTTP 401")
    _answers(monkeypatch, ["4", *KEEP])

    _run(home)

    assert "chatgpt" not in _profiles() and logged_out == ["chatgpt"]


def test_a_removed_profile_of_the_project_is_not_kept(home, monkeypatch, capsys):
    """Before, the project kept a profile that was gone: the next load stopped."""
    from fw_context_mcp.config import load

    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL + '\n[llm.profiles.work]\nprovider = "deepseek"\nmodel = "m"\n',
                                            encoding="utf-8")
    (home / ".fw-context" / "local.toml").write_text('[llm]\nprofile = "work"\n', encoding="utf-8")
    # Keep each setting, then remove the profile that local.toml selects.
    _answers(monkeypatch, ["", "", "", "work"], confirm=True)

    _run(home)

    assert "work" not in _profiles()
    assert "profile" not in tomllib.loads((home / ".fw-context" / "local.toml").read_text(encoding="utf-8")).get(
        "llm", {}), capsys.readouterr().out
    settings._config_cache.clear()
    load(home)


def test_the_global_default_is_not_offered_when_config_toml_selects_a_profile(home, monkeypatch, capsys):
    """``local.toml`` can select another profile over config.toml, but cannot remove its selection."""
    (home / ".fw-context" / "config.toml").write_text('[project]\n\n[llm]\nprofile = "local"\n', encoding="utf-8")
    # Option 0 of the LLM analysis; then keep the embeddings, no removal.
    _answers(monkeypatch, ["", "0", "", ""])

    _run(home)

    out = capsys.readouterr().out
    assert "0. the global settings" not in out, out
    assert "[skip] The LLM analysis is not changed" in out, out
    assert not (home / ".fw-context" / "local.toml").exists()


def test_a_key_that_a_header_cannot_hold_is_asked_again(home, monkeypatch):
    """Before, the ValueError of set_api_key stopped fw-context init."""
    keys = iter(["sk bad", "sk-good"])
    monkeypatch.setattr(ui, "_read_secret", lambda prompt: next(keys))

    assert ui._store_api_key("provider:deepseek") is True
    assert secrets.stored_api_key("provider:deepseek") == "sk-good"


def test_a_profile_of_one_run_does_not_take_the_tuning_of_the_default(home):
    from fw_context_mcp.config import load

    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL.replace('model = "chat:1b"', 'model = "chat:1b"\nnum_ctx = 65536\n'
                                                           'timeout = 900.0') + (
        '\n[llm.profiles.small]\nprovider = "ollama"\nmodel = "small:1b"\n'), encoding="utf-8")

    run = settings.profile_llm_config(load(home).llm, "small")

    assert (run.num_ctx, run.timeout) == (settings.LLMConfig().num_ctx, settings.LLMConfig().timeout)


def test_ctrl_c_stops_and_does_not_answer_the_default():
    """Before, Ctrl-C was an empty answer: "Keep the profile?" removed it."""
    import builtins

    def _interrupt(prompt):
        raise KeyboardInterrupt

    original = builtins.input
    builtins.input = _interrupt
    try:
        with pytest.raises(KeyboardInterrupt):
            ui._input("> ")
    finally:
        builtins.input = original


def test_a_selected_profile_turns_the_analysis_on_again(home, monkeypatch, capsys):
    from fw_context_mcp.cli import _llm_lists

    (home / ".fw-context" / "local.toml").write_text("[llm]\nanalyze_symbols = false\n", encoding="utf-8")
    # Profile 1 (local) for this project (1).
    _answers(monkeypatch, ["1", "1"])

    _llm_lists.choose_chat(home, settings.Config())

    local = tomllib.loads((home / ".fw-context" / "local.toml").read_text(encoding="utf-8"))["llm"]
    assert local.get("profile") == "local" and "analyze_symbols" not in local, local


def test_a_cloud_model_of_a_profile_server_shows_its_host(home, monkeypatch, capsys):
    """Before, the cloud models came from the server of the embeddings: a cloud profile showed no warning."""
    from fw_context_mcp.cli import _llm

    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL + '\n[llm.profiles.cloud]\nprovider = "ollama"\n'
                                            'url = "http://127.0.0.1:11434"\nmodel = "m:cloud"\n', encoding="utf-8")
    monkeypatch.setattr(_diag, "remote_hosts",
                        lambda url: {"m:cloud": "https://ollama.com"} if "127.0.0.1" in url else {})

    _llm.cmd_llm_list(argparse.Namespace(project=str(home)))

    assert "cloud: ollama m:cloud, ⚠ https://ollama.com" in capsys.readouterr().out


# ── The model list of an API (decisions of the user, 2026-10-22) ──


def _deepseek(name: str = "ds"):
    return settings.LLMConfig(provider="deepseek", profile=name, chat_api_base="https://api.deepseek.com")


def _prompts(monkeypatch, texts: dict[str, str]) -> list[tuple[str, str | None]]:
    """Answer the text prompts by label (else the default); return each (label, default)."""
    asked: list[tuple[str, str | None]] = []

    def _prompt_text(label, default=None, **kwargs):
        asked.append((label, default))
        return texts.get(label, default)

    monkeypatch.setattr(ui, "prompt_text", _prompt_text)
    return asked


def test_the_default_model_is_the_recommended_one_of_the_list(home, monkeypatch, capsys):
    asked = _prompts(monkeypatch, {})

    values = wizard._model_from_list(_deepseek(), "DeepSeek", "deepseek-chat")

    assert values == {"model": "deepseek-chat"}, "the list gives the context: no key context"
    assert asked == [("Model", "deepseek-chat")], "no question about the context"
    out = capsys.readouterr().out
    assert "Models of DeepSeek:\n    1. deepseek-flash\n    2. deepseek-chat\n    3. new-model\n" in out, out
    assert "Give the number of a row, or the name of a model" in out, out
    assert "Context of deepseek-chat: 1048576 tokens (the model list of the API)" in out, out


@pytest.mark.parametrize(("answer", "model"), [("2", "deepseek-chat"), ("deepseek-chat", "deepseek-chat"),
                                               ("", "deepseek-flash")], ids=["a number", "a name", "Enter"])
def test_a_model_of_the_list_is_selected_by_its_number_or_its_name(home, monkeypatch, answer, model):
    """Request of the user, 2026-10-23: the setup asked for the name of a model, a long text to type."""
    _prompts(monkeypatch, {"Model": answer})

    assert wizard._model_from_list(_deepseek(), "DeepSeek", "deepseek-flash") == {"model": model}


def test_a_number_outside_the_list_asks_again(monkeypatch, capsys):
    answers = iter(["7", "3"])
    monkeypatch.setattr(ui, "prompt_text", lambda label, default=None, **kwargs: next(answers))

    assert ui.prompt_model("Model", ["a:1b", "b:2b", "c:3b"], default="a:1b") == "c:3b"
    assert "[warn] No row 7: the list has 3 rows" in capsys.readouterr().out


def test_a_long_list_takes_a_name_and_no_number(monkeypatch, capsys):
    """A list longer than 20 is not printed: a number names no printed row."""
    answers = iter(["5", "vendor/model-05"])
    monkeypatch.setattr(ui, "prompt_text", lambda label, default=None, **kwargs: next(answers))
    names = [f"vendor/model-{n:02d}" for n in range(30)]

    assert ui.prompt_model("Model", [], default=names[0]) == "vendor/model-05"
    assert "[warn] The list of the models is not printed: give the name of a model" in capsys.readouterr().out


@pytest.mark.parametrize("names", [["a:1b", "b:2b"], []], ids=["a short list", "a list that is not printed"])
def test_a_default_of_digits_is_the_default_and_no_row(monkeypatch, names):
    """The review of 2026-10-23: a default "2" selected row 2, and outside the list it asked again for ever."""
    monkeypatch.setattr(ui, "prompt_text", lambda label, default=None, **kwargs: default)

    assert ui.prompt_model("Model", names, default="2") == "2"
    assert ui.prompt_model("Model", names, default="123") == "123"


def test_without_the_recommended_model_the_default_is_the_first_of_the_list(home, monkeypatch):
    asked = _prompts(monkeypatch, {})

    assert wizard._model_from_list(_deepseek(), "DeepSeek", "deepseek-v4-flash") == {"model": "deepseek-flash"}
    assert asked[0] == ("Model", "deepseek-flash")


def test_a_long_list_shows_its_count_and_not_each_name(home, monkeypatch, capsys):
    from fw_context_mcp.llm.model_list import ModelInfo

    names = [f"vendor/model-{number:02d}" for number in range(25)]
    _fake_model_lists(monkeypatch, {"openrouter": [ModelInfo(name, None) for name in names]})
    _prompts(monkeypatch, {"Model": "", "Context of the model in tokens": "8192"})
    llm = settings.LLMConfig(provider="openrouter", profile="or", chat_api_base="https://openrouter.ai/api/v1")
    from fw_context_mcp.llm import model_list

    # The endpoints give no context: the question comes.
    monkeypatch.setattr(model_list, "_endpoint_context", lambda llm: None)

    values = wizard._model_from_list(llm, "OpenRouter", "vendor/model-07")

    out = capsys.readouterr().out
    assert "OpenRouter lists 25 models; the recommended one is vendor/model-07" in out, out
    assert "vendor/model-08" not in out, out
    assert values == {"model": "vendor/model-07", "context": 8192}


def test_a_name_that_the_list_does_not_hold_is_skipped_with_the_similar_names(home, monkeypatch, capsys):
    _prompts(monkeypatch, {"Model": "deepseek-flsh"})

    assert wizard._model_from_list(_deepseek(), "DeepSeek", "deepseek-flash") is None
    assert ("[skip] 'deepseek-flsh' is not in the model list of DeepSeek (similar names: deepseek-flash"
            in capsys.readouterr().out)


@pytest.mark.parametrize(("answer", "values"), [("32768", {"model": "m", "context": 32768}), ("big", None),
                                                ("", None)])
def test_the_context_is_asked_only_when_the_list_gives_none(home, monkeypatch, capsys, answer, values):
    from fw_context_mcp.llm.model_list import ModelInfo

    _fake_model_lists(monkeypatch, {"openai_compatible": [ModelInfo("m", None)]})
    asked = _prompts(monkeypatch, {"Model": "m", "Context of the model in tokens": answer})
    llm = settings.LLMConfig(provider="openai_compatible", profile="api", chat_api_base="https://api.example.test/v1")

    assert wizard._model_from_list(llm, "the API", "m") == values
    assert [label for label, _default in asked] == ["Model", "Context of the model in tokens"]
    if values is None:
        assert f"[skip] {answer!r} is not a number of tokens: no profile is written" in capsys.readouterr().out


def test_an_unreadable_list_writes_no_profile(home, monkeypatch, capsys):
    _prompts(monkeypatch, {})
    llm = settings.LLMConfig(provider="openrouter", profile="or", chat_api_base="https://openrouter.ai/api/v1")

    assert wizard._model_from_list(llm, "OpenRouter", "m") is None
    assert "[warn] The model list of OpenRouter cannot be read" in capsys.readouterr().out


def test_unreadable_endpoints_ask_for_no_context(home, monkeypatch, capsys):
    """The analysis reads the endpoints at its start too: a static context would not let it run.

    Before, the setup asked for a context, wrote it, and the analysis still
    stopped with "the endpoints of model ... cannot be read".
    """
    from fw_context_mcp.llm import model_list

    _fake_model_lists(monkeypatch, {"openrouter": [model_list.ModelInfo("vendor/m", None)]})
    asked = _prompts(monkeypatch, {"Model": "vendor/m"})
    llm = settings.LLMConfig(provider="openrouter", profile="or", chat_api_base="https://openrouter.ai/api/v1")

    assert wizard._model_from_list(llm, "OpenRouter", "vendor/m") is None
    assert [label for label, _default in asked] == ["Model"]
    assert "[skip] the endpoints of model 'vendor/m' cannot be read" in capsys.readouterr().out


def _api_values() -> dict[str, object]:
    return {"provider": "openai_compatible", "url": "https://api.example.test/v1", "model": "m", "api_key_env": None}


def test_another_api_whose_list_gives_the_context_stores_none(home, monkeypatch):
    from fw_context_mcp.llm.model_list import ModelInfo

    _fake_model_lists(monkeypatch, {"openai_compatible": [ModelInfo("m", 65536)]})
    asked = _prompts(monkeypatch, {})

    assert ui._custom_api_context("api", _api_values()) == {}
    assert asked == []


@pytest.mark.parametrize(("answer", "values"), [("", {}), ("4096", {"context": 4096}), ("many", None)],
                         ids=["empty: embeddings only", "a number", "not a number"])
def test_another_api_without_a_context_asks_for_it(home, monkeypatch, answer, values):
    """The conftest gives no model list: the API gives no context."""
    _prompts(monkeypatch, {"Context of the chat model in tokens (empty: the profile serves the embeddings only)":
                           answer})

    assert ui._custom_api_context("api", _api_values()) == values


@pytest.mark.parametrize(("answer", "values"), [("16384", {"context": 16384}), ("", {}), ("many", None)],
                         ids=["a number", "empty: embeddings only", "not a number"])
def test_another_api_whose_list_does_not_hold_the_model_asks_for_the_context(home, monkeypatch, capsys, answer,
                                                                             values):
    """llama-server lists the name of its model file and runs each name: with a context, the name stays."""
    from fw_context_mcp.llm import model_list
    from fw_context_mcp.llm.model_list import ModelInfo

    _fake_model_lists(monkeypatch, {"openai_compatible": [ModelInfo("model-file.gguf", None)]})
    label = "Context of the chat model in tokens (empty: the profile serves the embeddings only)"
    asked = _prompts(monkeypatch, {label: answer})

    assert ui._custom_api_context("api", _api_values()) == values
    assert [entry[0] for entry in asked] == [label]
    assert ("[note] The model list of the API does not hold 'm': with a context, the name goes to the API as it is"
            in capsys.readouterr().out)
    if values:
        # The profile with that context: the context applies, the list does not stop it.
        llm = settings.LLMConfig(provider="openai_compatible", profile="api", model="m",
                                 chat_api_base="https://api.example.test/v1", context=values["context"])
        assert model_list.model_context(llm) == values["context"]


def test_the_test_of_a_profile_names_a_context_problem_without_a_request(home, monkeypatch):
    """A model that the list no longer holds answered the test, and the analysis stopped later."""
    from fw_context_mcp.config import load
    from fw_context_mcp.llm import client

    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL + '\n[llm.profiles.ds]\nprovider = "deepseek"\n'
                                            'model = "deepseek-v4-flash"\n', encoding="utf-8")
    monkeypatch.setattr(client, "get_chat_client", lambda cfg: pytest.fail("no test request"))

    problem = wizard.test_profile(load(home), "ds")

    assert problem is not None and "is not in the model list of its provider" in problem


def test_a_new_profile_for_the_embeddings_only_is_kept_without_a_context(home, monkeypatch, capsys):
    """An openai_compatible profile without a context serves the embeddings: its test must not roll it back."""
    from fw_context_mcp.config import load
    from fw_context_mcp.llm import client
    from fw_context_mcp.llm.client import ChatResult

    before = settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8")
    settings._GLOBAL_CONFIG_PATH.write_text(before + '\n[llm.profiles.emb]\nprovider = "openai_compatible"\n'
                                            'url = "https://api.example.test/v1"\nmodel = "embed-x"\n',
                                            encoding="utf-8")
    sent: list[str] = []

    class _Chat:
        def generate(self, prompt, **kwargs):
            sent.append(prompt)
            return ChatResult("OK")

    monkeypatch.setattr(client, "get_chat_client", lambda cfg: _Chat())
    cfg = load(home)

    assert wizard.test_profile(cfg, "emb") is not None, "the profile cannot analyse: no context"
    assert wizard._test_new_profile(cfg, "emb", before) is True
    assert "emb" in _profiles() and len(sent) == 1
    assert "[ok] Profile 'emb' answers" in capsys.readouterr().out


def test_a_profile_named_twice_is_removed_one_time(home, monkeypatch, capsys):
    """The review of 2026-10-23: "2, work" asked two times, and the second removal named a fault of the config."""
    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL + '\n[llm.profiles.work]\nprovider = "openrouter"\nmodel = "m"\n',
                                            encoding="utf-8")
    questions: list[str] = []
    _answers(monkeypatch, ["", "", "", "2, work"])
    monkeypatch.setattr(ui, "_confirm", lambda question, **kwargs: questions.append(question) or True)

    _run(home)

    assert "work" not in _profiles()
    assert sum("Remove the profile 'work'" in question for question in questions) == 1
    assert "Correct the file" not in capsys.readouterr().out


@pytest.mark.parametrize(("key", "how"), [("profile", "--global"), ("embed_profile", "fw-context llm setup")])
def test_a_profile_that_the_global_config_selects_is_not_removed(home, monkeypatch, key, how):
    """Each project without its own selection would get an error."""
    from fw_context_mcp.config._profiles import remove_profile

    selection = {"profile": "box"} if key == "profile" else {"profile": "other", key: "box"}
    settings._GLOBAL_CONFIG_PATH.write_text(
        "[llm]\n" + "".join(f'{name} = "{value}"\n' for name, value in selection.items())
        + '\n[llm.profiles.other]\nprovider = "ollama"\nmodel = "chat:1b"\n'
        '\n[llm.profiles.box]\nprovider = "ollama"\nurl = "http://localhost:11434"\nmodel = "chat:7b"\n',
        encoding="utf-8")

    with pytest.raises(settings.ConfigError, match=f"{key} of the global config selects 'box'.*{how}"):
        remove_profile("box")

    assert "box" in _profiles()


def test_a_profile_that_nothing_selects_is_removed(home):
    from fw_context_mcp.config._profiles import remove_profile

    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL + '\n[llm.profiles.work]\nprovider = "openrouter"\nmodel = "m"\n',
                                            encoding="utf-8")

    remove_profile("work")

    assert "work" not in _profiles()


def test_the_ollama_profile_of_the_machine_goes_with_its_key(home):
    """Decision of the user, 2026-10-23: before, the last Ollama profile could not be removed after an API default."""
    from fw_context_mcp.config._profiles import remove_profile

    settings._GLOBAL_CONFIG_PATH.write_text(
        '[llm]\nprofile = "ds"\nollama_profile = "local"\n\n'
        '[llm.profiles.ds]\nprovider = "deepseek"\nmodel = "deepseek-flash"\n\n'
        '[llm.profiles.local]\nprovider = "ollama"\nurl = "http://localhost:11434"\nmodel = "chat:1b"\n',
        encoding="utf-8")

    remove_profile("local")

    llm = tomllib.loads(settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))["llm"]
    assert "local" not in llm["profiles"] and "ollama_profile" not in llm


#: An API is the default, and the local Ollama profile is the Ollama profile of this machine.
MACHINE = (
    '[llm]\nprofile = "ds"\nollama_profile = "local"\n\n'
    '[llm.profiles.ds]\nprovider = "deepseek"\nmodel = "deepseek-flash"\n\n'
    '[llm.profiles.local]\nprovider = "ollama"\nurl = "http://localhost:11434"\nmodel = "chat:1b"\n'
)
#: The profile of an Ollama on another computer.
BOX = '\n[llm.profiles.box]\nprovider = "ollama"\nurl = "http://box.lan:11434"\nmodel = "chat:7b"\n'


def _no_local_ollama(monkeypatch) -> None:
    """The local Ollama gives no model list: it is gone, or it does not run."""
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: None)


def _removal_step(monkeypatch, answer: str, *, confirm: bool = True) -> list[str]:
    """Run the removal step with *answer*, *confirm* to each question; return the questions."""
    questions: list[str] = []
    monkeypatch.setattr(ui, "_input", lambda prompt: answer)
    monkeypatch.setattr(ui, "_confirm", lambda question, **kwargs: questions.append(question) or confirm)
    wizard._remove_profiles()
    return questions


def test_the_local_ollama_profile_of_the_machine_stays_while_the_local_ollama_answers(home, monkeypatch, capsys):
    """Decision of the user, 2026-10-23: the only reason to remove it is that the local Ollama is gone."""
    settings._GLOBAL_CONFIG_PATH.write_text(MACHINE, encoding="utf-8")

    questions = _removal_step(monkeypatch, "local")

    out = capsys.readouterr().out
    assert questions == [], questions
    assert "local: Local Ollama, chat:1b  — in use: the Ollama profile of this machine, and the local Ollama " \
           "answers" in out, out
    assert "Before you remove it, select another Ollama profile of this machine with " \
           "'fw-context llm default <profile> --ollama'" in out, out
    assert "local" in _profiles()


def test_the_step_removes_the_ollama_profile_of_the_machine_when_the_local_ollama_is_gone(home, monkeypatch, capsys):
    settings._GLOBAL_CONFIG_PATH.write_text(MACHINE, encoding="utf-8")
    _no_local_ollama(monkeypatch)

    questions = _removal_step(monkeypatch, "local")

    out = capsys.readouterr().out
    assert "local: Local Ollama, chat:1b  — the Ollama profile of this machine" in out, out
    assert questions == ["Remove the profile 'local'? The Ollama at http://localhost:11434 does not answer. This machine then has no "
                         "Ollama profile: 'fw-context llm setup' or 'fw-context llm default <profile> --ollama' "
                         "selects one"], questions
    llm = tomllib.loads(settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))["llm"]
    assert "local" not in llm["profiles"] and "ollama_profile" not in llm


def test_the_question_names_the_profile_that_becomes_the_profile_of_the_machine(home, monkeypatch):
    """Review, 2026-10-23: without the key, the only Ollama profile left is the profile of the machine."""
    settings._GLOBAL_CONFIG_PATH.write_text(MACHINE + BOX, encoding="utf-8")
    _no_local_ollama(monkeypatch)

    questions = _removal_step(monkeypatch, "local", confirm=False)

    assert questions == ["Remove the profile 'local'? The Ollama at http://localhost:11434 does not answer. The Ollama profile of this "
                         "machine then is 'box' (the Ollama at http://box.lan:11434)"], questions


def test_the_profile_of_a_remote_ollama_can_always_go(home, monkeypatch):
    """Decision of the user, 2026-10-23: also when it is the Ollama profile of this machine."""
    settings._GLOBAL_CONFIG_PATH.write_text(MACHINE.replace('ollama_profile = "local"', 'ollama_profile = "box"')
                                            + BOX, encoding="utf-8")

    questions = _removal_step(monkeypatch, "box")

    assert questions == ["Remove the profile 'box'? The Ollama profile of this machine then is 'local'"], questions
    llm = tomllib.loads(settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))["llm"]
    assert "box" not in llm["profiles"] and "ollama_profile" not in llm


def test_a_profile_in_use_gets_no_question(home, monkeypatch, capsys):
    settings._GLOBAL_CONFIG_PATH.write_text(
        GLOBAL.replace('embed_model = "e:1b"', 'embed_profile = "emb"\nembed_model = "e:1b"')
        + '\n[llm.profiles.emb]\nprovider = "ollama"\nurl = "http://localhost:11434"\nmodel = "chat:1b"\n',
        encoding="utf-8")
    _no_local_ollama(monkeypatch)

    questions = _removal_step(monkeypatch, "local, emb")

    out = capsys.readouterr().out
    assert questions == [], questions
    assert "'local' is in use (the default of the projects without their own selection). Before you remove it, " \
           "select another default in step 2 of 'fw-context llm setup' outside a project, or with " \
           "'fw-context llm default <profile> --global'\n" in out, out
    assert "'emb' is in use (the embeddings of the projects without their own selection). Before you remove it, " \
           "select other embeddings in step 2 of 'fw-context llm setup' outside a project\n" in out, out
    assert {"local", "emb"} <= set(_profiles())


def test_a_profile_with_two_uses_names_each_one(home, monkeypatch, capsys):
    """The global default is the only Ollama profile, thus the profile of the machine too."""
    questions = _removal_step(monkeypatch, "local")

    out = capsys.readouterr().out
    assert questions == [], questions
    assert "(the default of the projects without their own selection, their embeddings too; the Ollama profile " \
           "of this machine, and the local Ollama answers)" in out, out
    assert "'fw-context llm default <profile> --global'; and select another Ollama profile" in out, out


def test_a_machine_profile_without_the_key_counts_too(home, monkeypatch):
    """Without [llm] ollama_profile, the only Ollama profile is the profile of the machine."""
    settings._GLOBAL_CONFIG_PATH.write_text(MACHINE.replace('ollama_profile = "local"\n', ""), encoding="utf-8")

    assert _removal_step(monkeypatch, "local") == []
    _no_local_ollama(monkeypatch)
    assert _removal_step(monkeypatch, "local") == [
        "Remove the profile 'local'? The Ollama at http://localhost:11434 does not answer. This machine then has no Ollama profile: "
        "'fw-context llm setup' or 'fw-context llm default <profile> --ollama' selects one"]
    assert "local" not in _profiles()


def test_a_chatgpt_profile_without_a_sign_in_names_no_sign_in(home, monkeypatch):
    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL + '\n[llm.profiles.gpt]\nprovider = "chatgpt"\nmodel = "gpt-6-luna"\n',
                                            encoding="utf-8")

    questions = _removal_step(monkeypatch, "gpt")

    assert questions == ["Remove the profile 'gpt'?"], questions


def test_a_signed_in_chatgpt_profile_names_the_sign_in(home, monkeypatch):
    from fw_context_mcp.llm import chatgpt_plan

    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL + '\n[llm.profiles.gpt]\nprovider = "chatgpt"\nmodel = "gpt-6-luna"\n',
                                            encoding="utf-8")
    monkeypatch.setattr(chatgpt_plan, "load_credentials",
                        lambda name: SimpleNamespace(signed_in=lambda: True))

    # No to the question: a removal would revoke the sign-in at the server.
    questions = _removal_step(monkeypatch, "gpt", confirm=False)

    assert questions == ["Remove the profile 'gpt' and its ChatGPT sign-in?"], questions
    assert "gpt" in _profiles()


def test_a_profile_with_an_own_key_names_the_key(home, monkeypatch):
    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL + '\n[llm.profiles.work]\nprovider = "openrouter"\nmodel = "m"\n',
                                            encoding="utf-8")
    secrets.set_api_key("work", "sk-work")

    questions = _removal_step(monkeypatch, "work")

    assert questions == ["Remove the profile 'work' and its own API key?"], questions


def test_llm_remove_has_the_rules_of_the_setup(home, monkeypatch, capsys):
    """Review, 2026-10-23: the command removed the profile of the machine and told nothing."""
    from fw_context_mcp.cli._llm import cmd_llm_remove

    settings._GLOBAL_CONFIG_PATH.write_text(MACHINE + BOX, encoding="utf-8")

    assert cmd_llm_remove(argparse.Namespace(profile="local")) == 1
    assert "Before you remove it, select another Ollama profile of this machine" in capsys.readouterr().err
    assert "local" in _profiles()

    _no_local_ollama(monkeypatch)
    assert cmd_llm_remove(argparse.Namespace(profile="local")) == 0
    out = capsys.readouterr().out
    assert "[ok] Profile 'local' removed\n  [note] The Ollama profile of this machine then is 'box' " \
           "(the Ollama at http://box.lan:11434)" in out, out
    assert "local" not in _profiles()


#: A cloud model of the local Ollama.
CLOUD = '\n[llm.profiles.c]\nprovider = "ollama"\nurl = "http://localhost:11434"\nmodel = "gpt-oss:120b-cloud"\n'


def test_the_question_says_that_it_cannot_tell_while_a_cloud_model_has_no_list(home, monkeypatch):
    """Review, 2026-10-23: a cloud model of an Ollama that does not answer looks like a model of this machine."""
    settings._GLOBAL_CONFIG_PATH.write_text(MACHINE + CLOUD, encoding="utf-8")
    _no_local_ollama(monkeypatch)

    questions = _removal_step(monkeypatch, "local", confirm=False)

    assert questions == ["Remove the profile 'local'? The Ollama at http://localhost:11434 does not answer. "
                         "fw-context cannot tell the Ollama profile of this machine then: the Ollama at "
                         "http://localhost:11434 gives no model list"], questions


def test_another_local_ollama_without_a_list_keeps_a_profile_of_an_ollama_that_answers(home, monkeypatch, capsys):
    """Review, 2026-10-23: the profile of the other server counted as a second Ollama profile."""
    settings._GLOBAL_CONFIG_PATH.write_text(
        MACHINE.replace('ollama_profile = "local"\n', "")
        + '\n[llm.profiles.c2]\nprovider = "ollama"\nurl = "http://127.0.0.1:11500"\nmodel = "m:cloud"\n',
        encoding="utf-8")
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: {} if url.endswith(":11434") else None)

    questions = _removal_step(monkeypatch, "local")

    assert questions == []
    assert "the Ollama at http://127.0.0.1:11500 gives no model list, thus fw-context cannot tell the Ollama " \
           "profile of this machine). Before you remove it, start the Ollama at http://127.0.0.1:11500 and " \
           "repeat, or remove the profiles of that Ollama first" in capsys.readouterr().out
    assert "local" in _profiles()


def test_the_url_of_the_profile_decides_and_not_the_default_port(home, monkeypatch, capsys):
    settings._GLOBAL_CONFIG_PATH.write_text(MACHINE.replace("localhost:11434", "localhost:11500"), encoding="utf-8")
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: {} if url.endswith(":11500") else None)

    assert _removal_step(monkeypatch, "local") == []
    assert "the Ollama profile of this machine, and the local Ollama answers" in capsys.readouterr().out


def test_a_removal_that_makes_another_profile_the_profile_of_the_machine_keeps_that_one(home, monkeypatch, capsys):
    """Two local Ollama profiles and no key: no profile of the machine, until one of them goes."""
    settings._GLOBAL_CONFIG_PATH.write_text(
        MACHINE.replace('ollama_profile = "local"\n', "")
        + '\n[llm.profiles.b]\nprovider = "ollama"\nurl = "http://localhost:11434"\nmodel = "chat:7b"\n',
        encoding="utf-8")

    questions = _removal_step(monkeypatch, "local, b")

    assert questions == ["Remove the profile 'local'? The Ollama profile of this machine then is 'b'"], questions
    assert "[skip] The profile 'b' is in use (the Ollama profile of this machine, and the local Ollama answers)" \
        in capsys.readouterr().out
    assert set(_profiles()) == {"ds", "b"}


def test_llm_remove_names_each_use(home, capsys):
    """Review, 2026-10-23: the command told one use, and the setup two."""
    from fw_context_mcp.cli._llm import cmd_llm_remove

    assert cmd_llm_remove(argparse.Namespace(profile="local")) == 1
    err = capsys.readouterr().err
    assert "(the default of the projects without their own selection, their embeddings too; the Ollama profile " \
           "of this machine, and the local Ollama answers)" in err, err


#: A cloud model of a second local Ollama, which gives no list in these tests.
C2 = '\n[llm.profiles.c2]\nprovider = "ollama"\nurl = "http://127.0.0.1:11500"\nmodel = "m:cloud"\n'


def test_the_key_decides_the_profile_of_the_machine_while_another_ollama_has_no_list(home, monkeypatch):
    """Second review, 2026-10-23: the removal was refused with "cannot tell", but the key names 'box'."""
    settings._GLOBAL_CONFIG_PATH.write_text(
        MACHINE.replace('ollama_profile = "local"', 'ollama_profile = "box"') + BOX + C2, encoding="utf-8")
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: {} if url.endswith(":11434") else None)

    assert _removal_step(monkeypatch, "local") == ["Remove the profile 'local'?"]
    assert "local" not in _profiles()


def test_the_global_ollama_default_decides_while_another_ollama_has_no_list(home, monkeypatch, capsys):
    settings._GLOBAL_CONFIG_PATH.write_text(
        GLOBAL + '\n[llm.profiles.b]\nprovider = "ollama"\nurl = "http://localhost:11434"\nmodel = "chat:7b"\n'
        + C2, encoding="utf-8")
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: {} if url.endswith(":11434") else None)

    assert _removal_step(monkeypatch, "b") == ["Remove the profile 'b'?"]
    assert "1. local: Local Ollama, chat:1b  — in use: the default of the projects without their own selection, " \
           "their embeddings too; the Ollama profile of this machine, and the local Ollama answers" \
        in capsys.readouterr().out


def test_no_cannot_tell_when_the_key_stays(home, monkeypatch):
    """Second review, 2026-10-23: the question said "cannot tell", but the profile of the machine stays 'box'."""
    settings._GLOBAL_CONFIG_PATH.write_text(
        MACHINE.replace('ollama_profile = "local"', 'ollama_profile = "box"') + BOX + CLOUD, encoding="utf-8")
    _no_local_ollama(monkeypatch)

    assert _removal_step(monkeypatch, "local", confirm=False) == [
        "Remove the profile 'local'? The Ollama at http://localhost:11434 does not answer."]

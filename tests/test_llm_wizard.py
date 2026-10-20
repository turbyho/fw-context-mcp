"""The setup of the LLM services (``cli/_llm_wizard.py``), the memory of the machine, and ``fw-context llm``.

No test sends a request out: the Ollama lists, the test request, the
GPU query and the keyring are replaced (the keyring of the session fails
each call, thus a key goes to secrets.toml of the test directory).
"""

from __future__ import annotations

import argparse
import subprocess
import tomllib
from pathlib import Path

import pytest

from fw_context_mcp.cli import _init_interactive as ui
from fw_context_mcp.cli import _llm_wizard as wizard
from fw_context_mcp.config import settings
from fw_context_mcp.llm import _diag, hardware, secrets

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
    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    (root / ".fw-context" / "config.toml").write_text("[project]\n", encoding="utf-8")
    return root


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


# ── The memory of the machine ──


@pytest.mark.parametrize(("gpu", "ram", "model"), [
    (12282, 64 * 1024, "gemma4:26b"),  # the driver reports a 12 GB card as 12282 MiB
    (8188, 32 * 1024, "gemma4:12b"),
    (4096, 64 * 1024, "qwen3-coder:30b"),
    (None, 16 * 1024, "qwen2.5-coder:7b"),
])
def test_the_recommended_local_model_follows_the_memory(gpu, ram, model):
    assert hardware.recommended_local_model(hardware.Memory(gpu, ram)).model == model


def test_the_largest_nvidia_gpu_counts(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="8192\n12282\n"))

    assert hardware._nvidia_mib() == 12282


def test_no_nvidia_smi_is_no_gpu(monkeypatch):
    def _missing(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(subprocess, "run", _missing)

    assert hardware._nvidia_mib() is None


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
    _answers(monkeypatch, ["", "", ""])

    _run(home)

    out = capsys.readouterr().out
    assert "1. ✓ Local Ollama  profiles: local" in out, out
    assert "5.   OpenRouter  ⚠ to openrouter.ai" in out, out


def test_a_service_of_the_registry_stores_the_key_of_its_provider_and_a_tested_profile(home, monkeypatch, capsys):
    tested: list[str] = []
    monkeypatch.setattr(wizard, "test_profile", lambda cfg, name: tested.append(name))
    _answers(monkeypatch, ["6", "", ""], secret="sk-test-" + "key")

    _run(home)

    assert _profiles()["deepseek"] == {"provider": "deepseek", "model": "deepseek-v4-flash"}
    assert secrets.stored_api_key(secrets.provider_entry("deepseek")) == "sk-test-key"
    assert tested == ["deepseek"]
    assert "sk-test-key" not in capsys.readouterr().out


def test_a_profile_that_does_not_answer_is_removed_unless_the_user_keeps_it(home, monkeypatch, capsys):
    monkeypatch.setattr(wizard, "test_profile", lambda cfg, name: "HTTP 401")
    _answers(monkeypatch, ["6", "", ""], secret="sk-x")

    _run(home)

    assert "deepseek" not in _profiles()
    assert "does not answer: HTTP 401" in capsys.readouterr().out


def test_another_computer_offers_its_models(home, monkeypatch):
    monkeypatch.setattr(wizard, "test_profile", lambda cfg, name: None)
    monkeypatch.setattr(_diag, "installed_models", lambda url: ["big:30b"] if "gpu-box" in url else ["chat:1b"])
    _answers(monkeypatch, ["2", "", ""], {"URL of the Ollama server (for example http://192.168.1.20:11434)":
                                          "http://gpu-box:11434"})

    _run(home)

    assert _profiles()["remote"] == {"provider": "ollama", "url": "http://gpu-box:11434", "model": "big:30b"}


def test_ollama_cloud_offers_the_cloud_models_of_the_local_server(home, monkeypatch):
    monkeypatch.setattr(wizard, "test_profile", lambda cfg, name: None)
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: {"gpt-oss:20b-cloud": "https://ollama.com"})
    _answers(monkeypatch, ["3", "", ""])

    _run(home)

    assert _profiles()["ollama-cloud"]["model"] == "gpt-oss:20b-cloud"


def test_a_removal_asks_and_removes_the_own_key(home, monkeypatch):
    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL + '\n[llm.profiles.work]\nprovider = "openrouter"\nmodel = "m"\n',
                                            encoding="utf-8")
    secrets.set_api_key("work", "sk-work")
    _answers(monkeypatch, ["", "work", ""], confirm=True)

    _run(home)

    assert "work" not in _profiles()
    assert secrets.stored_api_key("work") is None


def test_the_global_default_cannot_be_removed(home, monkeypatch, capsys):
    _answers(monkeypatch, ["", "local", ""], confirm=True)

    _run(home)

    assert "local" in _profiles()
    assert "select another profile first" in capsys.readouterr().out


def test_the_project_offers_only_a_profile_that_can_run(home, monkeypatch, capsys):
    """A profile of an API without a key is listed with the command that repairs it, not as a choice."""
    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL + '\n[llm.profiles.or]\nprovider = "openrouter"\nmodel = "m"\n',
                                            encoding="utf-8")
    _answers(monkeypatch, ["", "", "2"])

    _run(home)

    out = capsys.readouterr().out
    assert "(or: no API key: run 'fw-context llm key set or')" in out, out
    assert "[skip] The profile of the project is not changed" in out, out


def test_the_project_selects_a_profile(home, monkeypatch, capsys):
    from fw_context_mcp.llm import client

    monkeypatch.setattr(client, "call_llm", lambda prompt, cfg, **kwargs: "OK")
    _answers(monkeypatch, ["", "", "1"])

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

    assert _llm.cmd_llm_default(argparse.Namespace(profile="ds", global_default=True, project=None)) == 0

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
    monkeypatch.setattr(wizard, "test_profile", lambda cfg, name: None)
    _answers(monkeypatch, ["1", "", ""])

    wizard.run_wizard(home, load(home))

    profiles = _profiles()
    assert profiles["remote"] == {"provider": "ollama", "url": "http://gpu-box:11434", "model": "big:30b"}
    assert profiles["local"]["url"] == "http://localhost:11434"


def test_a_failed_test_puts_back_the_profile_that_it_replaced(home, monkeypatch):
    """Before, the profile was removed with its earlier values: each project that selected it stopped."""
    settings._GLOBAL_CONFIG_PATH.write_text(GLOBAL + '\n[llm.profiles.deepseek]\nprovider = "deepseek"\n'
                                            'model = "old-model"\n', encoding="utf-8")
    monkeypatch.setattr(wizard, "test_profile", lambda cfg, name: "HTTP 500")
    _answers(monkeypatch, ["6", "", ""], {"Model": "new-model"}, secret="sk-x")
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
    monkeypatch.setattr(chatgpt_plan, "list_models", lambda name: [("gpt-6-luna", "GPT-6-Luna")])
    monkeypatch.setattr(wizard, "test_profile", lambda cfg, name: None)
    _answers(monkeypatch, ["4", "", ""], {"Another profile name (empty: skip)": "chatgpt2"})

    _run(home)

    assert signed == ["chatgpt2"]
    assert _profiles()["chatgpt2"] == {"provider": "chatgpt", "model": "gpt-6-luna"}


def test_a_failed_chatgpt_profile_does_not_leave_a_sign_in(home, monkeypatch):
    from fw_context_mcp.llm import chatgpt_plan

    logged_out: list[str] = []
    monkeypatch.setattr(chatgpt_plan, "login", lambda name, open_url: None)
    monkeypatch.setattr(chatgpt_plan, "list_models", lambda name: [("m", "M")])
    monkeypatch.setattr(chatgpt_plan, "load_credentials", lambda name: chatgpt_plan.PlanCredentials(
        client_id="c", host_id="h", access_token="a", refresh_token="r"))
    monkeypatch.setattr(chatgpt_plan, "logout", lambda name: logged_out.append(name) or True)
    monkeypatch.setattr(wizard, "test_profile", lambda cfg, name: "HTTP 401")
    _answers(monkeypatch, ["4", "", ""])

    _run(home)

    assert "chatgpt" not in _profiles() and logged_out == ["chatgpt"]


def test_a_removed_profile_of_the_project_is_not_kept(home, monkeypatch, capsys):
    """Before, Enter said that the project keeps a profile that was gone: the next load stopped."""
    (home / ".fw-context" / "local.toml").write_text('[llm]\nprofile = "gone"\n', encoding="utf-8")
    _answers(monkeypatch, [""])

    wizard.select_project_profile(home, settings.Config())

    out = capsys.readouterr().out
    assert "selects profile 'gone', which the global config does not have" in out, out
    assert "keeps profile" not in out
    assert "profile" not in tomllib.loads((home / ".fw-context" / "local.toml").read_text(encoding="utf-8")).get(
        "llm", {})


def test_the_global_default_is_not_offered_when_config_toml_selects_a_profile(home, monkeypatch, capsys):
    (home / ".fw-context" / "config.toml").write_text('[project]\n\n[llm]\nprofile = "local"\n', encoding="utf-8")
    _answers(monkeypatch, ["0"])

    wizard.select_project_profile(home, settings.Config())

    out = capsys.readouterr().out
    assert "not offered, .fw-context/config.toml selects 'local'" in out, out
    assert "the global settings apply" not in out


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
    from fw_context_mcp.llm import client

    (home / ".fw-context" / "local.toml").write_text("[llm]\nanalyze_symbols = false\n", encoding="utf-8")
    monkeypatch.setattr(client, "call_llm", lambda prompt, cfg, **kwargs: "OK")
    _answers(monkeypatch, ["1"])

    wizard.select_project_profile(home, settings.Config())

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

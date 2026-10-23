"""The LLM setup in two steps (``cli/_llm_lists.py``): the services, then the list of the chat and of the embeddings.

Decision of the user, 2026-10-22.  Step 1 sets up the services that the
user selects.  Step 2 has two lists of the profiles that are set up, plus
the local Ollama when it runs without a profile: one for the LLM analysis,
one for the embeddings.  Enter keeps the current setting of each.

No test sends a request out: the Ollama lists, the test request of a new
profile, the GPU query and the memory of the machine are replaced, and the
keyring of the session fails each call (conftest).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from fw_context_mcp.cli import _init_interactive as ui
from fw_context_mcp.cli import _llm_lists, _llm_wizard
from fw_context_mcp.config import settings
from fw_context_mcp.llm import _diag, auto_model, client, hardware, ollama, profile_state, secrets

LOCAL = '[llm.profiles.local]\nprovider = "ollama"\nurl = "http://localhost:11434"\nmodel = "chat:14b"\n'
CLOUD = '[llm.profiles.cloudy]\nprovider = "ollama"\nurl = "http://localhost:11434"\nmodel = "m:cloud"\n'
DS = '[llm.profiles.ds]\nprovider = "deepseek"\nmodel = "deepseek-chat"\n'
OR = '[llm.profiles.or]\nprovider = "openrouter"\nmodel = "m"\n'
GPT = '[llm.profiles.gpt]\nprovider = "chatgpt"\nmodel = "gpt-6-luna"\n'
API = ('[llm.profiles.work]\nprovider = "openai_compatible"\nurl = "https://api.example.test/v1"\nmodel = "w"\n'
       'api_key_env = "WORK_KEY"\n')
#: The global default is the local Ollama profile.
GLOBAL = '[llm]\nprofile = "local"\nembed_model = "e:1b"\n\n' + LOCAL
#: The cloud models of the local Ollama (``/api/tags`` remote_host).
CLOUD_MODELS = {"m:cloud": "https://ollama.com:443"}


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


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> dict:
    """A global config of its own, a project, a machine without a GPU, and no local Ollama.

    Returns the project root and the list of the profiles that the test
    request of a new profile got.
    """
    path = tmp_path / "home" / "config.toml"
    path.parent.mkdir()
    path.write_text(GLOBAL, encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", path)
    monkeypatch.setenv("FW_CONTEXT_HOME", str(path.parent))
    monkeypatch.setattr(secrets, "_found", {})
    monkeypatch.setattr(secrets, "_told", set())
    settings._config_cache.clear()
    monkeypatch.setattr(hardware, "machine_memory", lambda: hardware.Memory(gpu_mib=None, ram_mib=16 * 1024))
    monkeypatch.setattr(_diag, "installed_models", lambda url: None)
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: dict(CLOUD_MODELS))
    # No key: the recommendation never asks a server and never measures.
    monkeypatch.setattr(ollama, "calibration_key", lambda *args: None)
    monkeypatch.setattr(ollama, "check_setup", lambda cfg: {"status": "ok", "ollama_running": True,
                                                            "installed_models": []})
    # A config without embed_model: load detects one and asks the server.
    monkeypatch.setattr(auto_model, "_gpu_available", lambda: False)
    monkeypatch.setattr(auto_model, "_model_installed", lambda url, model: True)
    tested: list[str] = []
    monkeypatch.setattr(_llm_wizard, "test_profile", lambda cfg, name, **kwargs: tested.append(name))
    # The test request of a selection for this project (configure_llm_core): the profile of each request.
    selected: list[str] = []
    monkeypatch.setattr(client, "call_llm", lambda prompt, cfg, **kwargs: selected.append(cfg.profile) or "OK")
    # A key of the operator would make a profile without a key ready.
    for name in ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    from fw_context_mcp.llm.model_list import ModelInfo

    # DeepSeek lists the model of the profile ds, with its context.
    _fake_model_lists(monkeypatch, {"deepseek": [ModelInfo("deepseek-chat", 1048576)]})
    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    (root / ".fw-context" / "config.toml").write_text("[project]\n", encoding="utf-8")
    return {"root": root, "tested": tested, "selected": selected}


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


def _cfg(root: Path | None):
    from fw_context_mcp.config import load

    settings._config_cache.clear()
    return load(root)


def _answers(monkeypatch, inputs: list[str], texts: dict[str, str] | None = None, *,
             confirm: bool = False) -> list[str]:
    """Give *inputs* to the lists and *texts* to the text prompts (by label; else the default).

    Each yes/no question gets *confirm*.  Returns the labels of the text
    prompts, in their order.
    """
    replies = iter(inputs)
    labels: list[str] = []

    def _prompt_text(label, default=None, **kwargs):
        labels.append(label)
        return (texts or {}).get(label, default)

    monkeypatch.setattr(ui, "_input", lambda prompt: next(replies))
    monkeypatch.setattr(ui, "prompt_text", _prompt_text)
    monkeypatch.setattr(ui, "_confirm", lambda question, **kwargs: confirm)
    return labels


def _rows(out: str) -> list[str]:
    """Return the numbered rows of a list (``    1. ...``), without option 0."""
    return [line.strip() for line in out.splitlines() if re.match(r"^ {4}[1-9]\d*\. ", line)]


# ── Step 1: the services ──


def test_enter_in_step_1_sets_up_no_service(home, monkeypatch, capsys):
    root = home["root"]
    before = settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8")
    _answers(monkeypatch, [""])

    _llm_lists.setup_services(root, _cfg(root))

    out = capsys.readouterr().out
    assert "1. ✓ Local Ollama  profiles: local" in out, out
    assert settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8") == before
    assert home["tested"] == []
    assert not (root / ".fw-context" / "local.toml").exists()


def test_step_1_tells_when_the_local_ollama_runs_without_a_profile(home, monkeypatch, capsys):
    _global('[llm]\nprofile = "ds"\n\n' + DS)
    monkeypatch.setattr(_diag, "installed_models", lambda url: ["chat:1b"])
    _answers(monkeypatch, [""])

    _llm_lists.setup_services(home["root"], _cfg(home["root"]))

    assert "(the local Ollama runs: step 2 offers it, also without a profile)" in capsys.readouterr().out


# ── Step 2: the list of the LLM analysis ──


def test_the_chat_list_shows_only_the_profiles_and_the_detected_local_ollama(home, monkeypatch, capsys):
    """Only services that are set up: no row for a service of step 1 without a profile."""
    root = home["root"]
    _global('[llm]\nprofile = "ds"\n\n' + DS + OR)
    monkeypatch.setattr(_diag, "installed_models", lambda url: ["chat:1b"])
    _answers(monkeypatch, [""])

    _llm_lists.choose_chat(root, _cfg(root))

    rows = _rows(capsys.readouterr().out)
    assert len(rows) == 3, rows
    assert rows[0].startswith("1. Local Ollama (detected, no profile yet: set it up now)"), rows
    assert rows[1].startswith("2. ds: deepseek deepseek-chat  ⚠ https://api.deepseek.com"), rows
    assert rows[2].startswith("3. or: openrouter m  ⚠ https://openrouter.ai"), rows


def test_the_chat_list_puts_the_profiles_of_this_machine_first(home, monkeypatch, capsys):
    """The order of the file is kept within each group: first this machine, then the external hosts."""
    root = home["root"]
    _global('[llm]\nprofile = "local"\n\n' + DS + OR + LOCAL)
    _answers(monkeypatch, [""])

    _llm_lists.choose_chat(root, _cfg(root))

    rows = _rows(capsys.readouterr().out)
    assert [row.split(":")[0] for row in rows] == ["1. local", "2. ds", "3. or"], rows


@pytest.mark.parametrize(("text", "running"), [(GLOBAL, ["chat:1b"]), ('[llm]\nprofile = "ds"\n\n' + DS, None)],
                         ids=["a profile names the local Ollama", "the local Ollama does not run"])
def test_no_detected_row_without_a_need(home, monkeypatch, capsys, text, running):
    root = home["root"]
    _global(text)
    monkeypatch.setattr(_diag, "installed_models", lambda url: running)
    _answers(monkeypatch, [""])

    _llm_lists.choose_chat(root, _cfg(root))

    assert "Local Ollama (detected" not in capsys.readouterr().out


def test_the_detected_local_ollama_is_set_up_and_selected(home, monkeypatch, capsys):
    root = home["root"]
    _global('[llm]\nprofile = "ds"\n\n' + DS)
    monkeypatch.setattr(_diag, "installed_models", lambda url: ["chat:1b"])
    # The detected row (1), for this project (1).
    _answers(monkeypatch, ["1", "1"], {"Chat model": "chat:1b"})

    _llm_lists.choose_chat(root, _cfg(root))

    llm = _global_llm()
    assert llm["profiles"]["local"] == {"url": "http://localhost:11434", "provider": "ollama", "model": "chat:1b"}
    assert llm["profile"] == "ds", "scope 1: the global default stays"
    assert home["tested"] == ["local"], "the new profile is tested before the selection"
    assert home["selected"] == ["local"], "the selection for this project is tested too"
    assert _local_llm(root) == {"enabled": True, "profile": "local"}
    assert profile_state.ollama_profile_name(llm, {"http://localhost:11434": CLOUD_MODELS}) == "local"


def test_a_detected_local_ollama_that_does_not_answer_is_not_selected(home, monkeypatch, capsys):
    root = home["root"]
    _global('[llm]\nprofile = "ds"\n\n' + DS)
    before = settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8")
    monkeypatch.setattr(_diag, "installed_models", lambda url: ["chat:1b"])
    monkeypatch.setattr(_llm_wizard, "test_profile", lambda cfg, name, **kwargs: "HTTP 500")
    # No to "Keep the profile anyway?": the global config gets its earlier text back.
    _answers(monkeypatch, ["1"], {"Chat model": "chat:1b"})

    _llm_lists.choose_chat(root, _cfg(root))

    assert "does not answer: HTTP 500" in capsys.readouterr().out
    assert settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8") == before
    assert not (root / ".fw-context" / "local.toml").exists()


def test_d_turns_off_the_llm_analysis_and_keeps_the_embeddings(home, monkeypatch, capsys):
    root = home["root"]
    _answers(monkeypatch, ["d"])

    _llm_lists.choose_chat(root, _cfg(root))

    assert "[ok] LLM analysis off ([llm] analyze_symbols = false) — the embeddings stay" in capsys.readouterr().out
    assert _local_llm(root) == {"analyze_symbols": False}


def test_d_without_a_project_writes_nothing(home, monkeypatch, capsys):
    before = settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8")
    _answers(monkeypatch, ["d"])

    _llm_lists.choose_chat(None, _cfg(None))

    assert "[skip] No project: the LLM analysis is a setting of a project" in capsys.readouterr().out
    assert settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8") == before


def test_0_removes_the_profile_of_local_toml(home, monkeypatch, capsys):
    root = home["root"]
    _global(GLOBAL + "\n" + DS)
    _local(root, '[llm]\nprofile = "ds"\nembed_model = "x:1b"\n')
    _answers(monkeypatch, ["0"])

    _llm_lists.choose_chat(root, _cfg(root), offer_global=True)

    out = capsys.readouterr().out
    assert "0. the global settings (removes the profile of .fw-context/local.toml)" in out, out
    assert "[ok] LLM analysis: the global settings apply" in out, out
    assert _local_llm(root) == {"embed_model": "x:1b"}, "the embeddings have their own list"


def test_0_is_no_choice_without_offer_global(home, monkeypatch, capsys):
    """Without offer_global (config.toml selects the backend, or ``llm setup``), "0" changes nothing."""
    root = home["root"]
    _global(GLOBAL + "\n" + DS)
    _local(root, '[llm]\nprofile = "ds"\n')
    _answers(monkeypatch, ["0"])

    _llm_lists.choose_chat(root, _cfg(root))

    out = capsys.readouterr().out
    assert "0. the global settings" not in out and "[skip] The LLM analysis is not changed" in out, out
    assert _local_llm(root) == {"profile": "ds"}


def test_scope_2_of_an_external_profile_writes_the_global_default_and_keeps_the_ollama_profile(
        home, monkeypatch, capsys):
    """Without the key ollama_profile, the Ollama profile of this machine followed the global default."""
    root = home["root"]
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    _global(GLOBAL + "\n" + DS)
    # The selection of local.toml would win over the new global default: the step removes it.
    _local(root, '[llm]\nprofile = "local"\n')
    # The LLM analysis: ds (row 2, after local) for each project (2).
    _answers(monkeypatch, ["2", "2"])

    _llm_lists.choose_chat(root, _cfg(root))

    llm = _global_llm()
    assert (llm["profile"], llm["ollama_profile"]) == ("ds", "local")
    assert profile_state.ollama_profile_name(llm, {"http://localhost:11434": CLOUD_MODELS}) == "local"
    assert "profile" not in _local_llm(root)
    assert "[ok] Profile 'ds' is the profile of each project that selects none" in capsys.readouterr().out
    assert home["selected"] == [], "scope 2 writes no project file, thus no test of a selection"


def test_an_ollama_profile_that_is_not_the_one_of_this_machine_asks_first(home, monkeypatch, capsys):
    """One Ollama profile for each machine: a "no" changes nothing."""
    root = home["root"]
    _global(GLOBAL + '\n[llm.profiles.box]\nprovider = "ollama"\nurl = "http://gpu-box:11434"\nmodel = "big:30b"\n')
    before = settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8")
    _answers(monkeypatch, ["2"])

    _llm_lists.choose_chat(root, _cfg(root))

    assert "The Ollama profile of this machine stays; the LLM analysis is not changed" in capsys.readouterr().out
    assert settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8") == before
    assert not (root / ".fw-context" / "local.toml").exists()


def test_without_the_model_list_the_chat_list_makes_no_cloud_model_the_ollama_profile(home, monkeypatch, capsys):
    """Without the list, a cloud model looks like a local one (``profile_state.unknown_servers``).

    The other writers (``fw-context llm default``, the MCP tool) refuse to
    decide about an Ollama profile then.
    """
    root = home["root"]
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: None)
    _global('[llm]\nprofile = "ds"\n\n' + LOCAL + CLOUD + DS)
    # cloudy is row 2: without the list it shows no external host.
    _answers(monkeypatch, ["2", "1"], confirm=True)

    _llm_lists.choose_chat(root, _cfg(root))

    assert "ollama_profile" not in _global_llm(), capsys.readouterr().out
    assert _local_llm(root).get("profile") != "cloudy"


def test_without_the_model_list_scope_2_of_an_external_profile_decides_nothing(home, monkeypatch, capsys):
    """Without the key ollama_profile, the step must name the Ollama profile of this machine: it needs the lists."""
    root = home["root"]
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(_diag, "remote_hosts", lambda url: None)
    _global('[llm]\nprofile = "local"\n\n' + LOCAL + CLOUD + DS)
    before = settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8")
    # ds is row 3 (external); for each project (2).
    _answers(monkeypatch, ["3", "2"])

    _llm_lists.choose_chat(root, _cfg(root))

    assert "gives no model list" in capsys.readouterr().out
    assert settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8") == before


def test_a_profile_that_cannot_run_is_no_choice(home, monkeypatch, capsys):
    """An API profile without a key is shown with the command that repairs it, and is not selected."""
    root = home["root"]
    _global(GLOBAL + "\n" + DS)
    before = settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8")
    _answers(monkeypatch, ["2", "1"])

    _llm_lists.choose_chat(root, _cfg(root))

    out = capsys.readouterr().out
    assert "[skip] The profile 'ds' cannot run: no API key: run 'fw-context llm key set ds'" in out, out
    assert settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8") == before
    assert not (root / ".fw-context" / "local.toml").exists()


def test_scope_1_of_an_external_profile_is_tested_and_names_the_host(home, monkeypatch, capsys):
    root = home["root"]
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    _global(GLOBAL + "\n" + DS)
    # ds (row 2) for this project (1).
    _answers(monkeypatch, ["2", "1"])

    _llm_lists.choose_chat(root, _cfg(root))

    out = capsys.readouterr().out
    assert home["selected"] == ["ds"]
    assert _local_llm(root) == {"enabled": True, "profile": "ds"}
    assert "[ok] .fw-context/local.toml selects profile 'ds'" in out, out
    assert "⚠ The chat of profile 'ds' go to an external host (https://api.deepseek.com)" in out, out


def test_a_selection_that_does_not_answer_leaves_local_toml_as_it_was(home, monkeypatch, capsys):
    from fw_context_mcp.llm.errors import LLMError

    root = home["root"]
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    _global(GLOBAL + "\n" + DS)
    _local(root, '[llm]\nembed_model = "x:1b"\n')
    before = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")

    def _fails(prompt, cfg, **kwargs):
        raise LLMError("HTTP 401")

    monkeypatch.setattr(client, "call_llm", _fails)
    _answers(monkeypatch, ["2", "1"])

    _llm_lists.choose_chat(root, _cfg(root))

    out = capsys.readouterr().out
    assert "[warn] The test call failed: HTTP 401" in out and "selects profile" not in out, out
    assert (root / ".fw-context" / "local.toml").read_text(encoding="utf-8") == before


# ── Step 2: the list of the embeddings ──


def test_the_ollama_embedding_model_of_the_config_is_the_default(home, monkeypatch, capsys):
    """Before: Enter replaced the configured model with the recommended one."""
    root = home["root"]
    defaults: list[str | None] = []
    _answers(monkeypatch, ["1", "1"])
    monkeypatch.setattr(ui, "prompt_text", lambda label, default=None, **kwargs: defaults.append(default) or default)

    _llm_lists.choose_embeddings(root, _cfg(root))

    assert defaults == ["e:1b"]
    assert _local_llm(root) == {"embed_profile": "local", "embed_model": "e:1b"}
    assert "The embedding model changes" not in capsys.readouterr().out


def test_a_failed_second_global_write_puts_the_global_config_back(home, monkeypatch, capsys):
    """embed_profile and embed_model go together: a half change sends the model to the server of the other key."""
    root = home["root"]
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    _global(GLOBAL + "\n" + OR)
    before = settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8")
    real = settings.update_global_llm_key

    def _second_fails(key, value):
        if key == "embed_model":
            raise OSError("disk full")
        return real(key, value)

    monkeypatch.setattr(settings, "update_global_llm_key", _second_fails)
    # OpenRouter (row 2), Enter: the best model, for each project (2).
    _answers(monkeypatch, ["2", "2"])

    _llm_lists.choose_embeddings(root, _cfg(root))

    assert "[warn] The global config is not changed: disk full" in capsys.readouterr().out
    assert settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8") == before


def test_the_embeddings_list_shows_only_the_profiles_that_can_embed(home, monkeypatch, capsys):
    """Not DeepSeek, not the ChatGPT plan, not a cloud model of Ollama: they give no embeddings."""
    root = home["root"]
    _global('[llm]\nprofile = "local"\nembed_model = "e:1b"\n\n' + LOCAL + DS + GPT + CLOUD + OR + API)
    _answers(monkeypatch, [""])

    _llm_lists.choose_embeddings(root, _cfg(root))

    rows = _rows(capsys.readouterr().out)
    assert [row.split(":")[0] for row in rows] == ["1. local", "2. or", "3. work"], rows


def test_the_embeddings_list_offers_the_local_ollama_without_a_profile(home, monkeypatch, capsys):
    root = home["root"]
    _global('[llm]\nprofile = "ds"\nembed_model = "e:1b"\n\n' + DS + OR)
    monkeypatch.setattr(_diag, "installed_models", lambda url: ["e:1b"])
    _answers(monkeypatch, [""])

    _llm_lists.choose_embeddings(root, _cfg(root))

    rows = _rows(capsys.readouterr().out)
    assert rows[0].startswith("1. Local Ollama (detected)") and len(rows) == 2, rows


@pytest.mark.parametrize("scope", ["1", "2"], ids=["this project", "each project"])
def test_openrouter_embeddings_offer_the_models_of_the_list(home, monkeypatch, capsys, scope):
    root = home["root"]
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    _global(GLOBAL + "\n" + OR)
    # OpenRouter is row 2 (after the local Ollama); Enter takes the best model of the list.
    _answers(monkeypatch, ["2", scope])

    _llm_lists.choose_embeddings(root, _cfg(root))

    out = capsys.readouterr().out
    assert "Embedding models of OpenRouter" in out and "mistralai/codestral-embed-2505" in out, out
    assert "⚠ The source code and each search query go to openrouter.ai" in out, out
    assert "The embedding model changes from e:1b to mistralai/codestral-embed-2505" in out, out
    llm = _global_llm()
    if scope == "1":
        assert _local_llm(root) == {"embed_profile": "or", "embed_model": "mistralai/codestral-embed-2505"}
        assert llm["embed_model"] == "e:1b" and "embed_profile" not in llm
    else:
        assert (llm["embed_profile"], llm["embed_model"]) == ("or", "mistralai/codestral-embed-2505")
        assert _local_llm(root) == {}


def test_another_api_asks_for_the_context_of_its_embedding_model_and_writes_it(home, monkeypatch, capsys):
    """The profile has no embed_context: without it, the input of the model is not known."""
    root = home["root"]
    _global(GLOBAL + "\n" + API)
    labels = _answers(monkeypatch, ["2", "1"], {"Embedding model of that API": "text-embed-x",
                                                "Tokens of one input of the model (embed_context)": "4096"})

    _llm_lists.choose_embeddings(root, _cfg(root))

    assert labels == ["Embedding model of that API", "Tokens of one input of the model (embed_context)"]
    profile = _global_llm()["profiles"]["work"]
    assert profile["embed_context"] == 4096 and profile["api_key_env"] == "WORK_KEY", profile
    assert _local_llm(root) == {"embed_profile": "work", "embed_model": "text-embed-x"}
    assert "⚠ The source code and each search query go to https://api.example.test/v1" in capsys.readouterr().out


def test_another_api_with_a_context_is_not_asked_again(home, monkeypatch):
    root = home["root"]
    _global(GLOBAL + "\n" + API + "embed_context = 2048\n")
    labels = _answers(monkeypatch, ["2", "1"], {"Embedding model of that API": "text-embed-x"})

    _llm_lists.choose_embeddings(root, _cfg(root))

    assert labels == ["Embedding model of that API"]
    assert _global_llm()["profiles"]["work"]["embed_context"] == 2048


def test_a_context_that_is_not_a_number_writes_nothing(home, monkeypatch, capsys):
    root = home["root"]
    _global(GLOBAL + "\n" + API)
    before = settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8")
    _answers(monkeypatch, ["2"], {"Embedding model of that API": "text-embed-x",
                                  "Tokens of one input of the model (embed_context)": "many"})

    _llm_lists.choose_embeddings(root, _cfg(root))

    assert "[skip] 'many' is not a number of tokens" in capsys.readouterr().out
    assert settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8") == before
    assert not (root / ".fw-context" / "local.toml").exists()


def test_embeddings_0_removes_the_embeddings_of_local_toml(home, monkeypatch, capsys):
    root = home["root"]
    _local(root, '[llm]\nprofile = "local"\nembed_profile = "local"\nembed_model = "x:1b"\n')
    _answers(monkeypatch, ["0"])

    _llm_lists.choose_embeddings(root, _cfg(root), offer_global=True)

    out = capsys.readouterr().out
    assert "0. the global settings (removes embed_profile and embed_model of .fw-context/local.toml)" in out, out
    assert "[ok] Embeddings: the global settings apply" in out, out
    assert _local_llm(root) == {"profile": "local"}, "the LLM analysis has its own list"


def test_embeddings_0_is_no_choice_without_offer_global(home, monkeypatch, capsys):
    root = home["root"]
    _local(root, '[llm]\nembed_model = "x:1b"\n')
    _answers(monkeypatch, ["0"])

    _llm_lists.choose_embeddings(root, _cfg(root))

    out = capsys.readouterr().out
    assert "0. the global settings" not in out and "[skip] The embeddings are not changed" in out, out
    assert _local_llm(root) == {"embed_model": "x:1b"}


# ── The two lists together ──


def test_enter_keeps_both_lists(home, monkeypatch, capsys):
    """``fw-context llm setup`` with Enter at each question changes no file."""
    root = home["root"]
    _global(GLOBAL + "\n" + DS + OR)
    _local(root, '[llm]\nprofile = "ds"\nembed_profile = "or"\nembed_model = "mistralai/codestral-embed-2505"\n')
    global_before = settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8")
    local_before = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")
    # The services, the LLM analysis, the embeddings, the removal of profiles.
    _answers(monkeypatch, ["", "", "", ""])

    _llm_wizard.run_wizard(root, _cfg(root))

    out = capsys.readouterr().out
    assert "[ok] LLM analysis: the current setting stays" in out, out
    assert "[ok] Embeddings: the current setting stays" in out, out
    assert settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8") == global_before
    assert (root / ".fw-context" / "local.toml").read_text(encoding="utf-8") == local_before
    assert home["tested"] == []


# ── The findings of the review of 2026-10-22 ──


@pytest.mark.parametrize("where", ["global", "config.toml"])
def test_the_detected_local_ollama_for_this_project_keeps_the_embeddings_on_ollama(home, monkeypatch, where):
    """Before: the step removed embed_profile, the embed_profile below applied, and the Ollama tag went to openrouter.ai."""
    root = home["root"]
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(_diag, "installed_models", lambda url: ["e:1b"])
    embed = 'embed_profile = "or"\nembed_model = "mistralai/codestral-embed-2505"\n'
    if where == "global":
        _global('[llm]\nprofile = "or"\n' + embed + "\n" + OR)
    else:
        _global('[llm]\nprofile = "or"\nembed_model = "e:1b"\n\n' + OR)
        (root / ".fw-context" / "config.toml").write_text("[project]\n\n[llm]\n" + embed, encoding="utf-8")
    # The detected local Ollama (row 1), for this project (1).
    _answers(monkeypatch, ["1", "1"], {"Embedding model": "e:1b"})

    _llm_lists.choose_embeddings(root, _cfg(root))

    assert _local_llm(root) == {"embed_model": "e:1b", "embed_profile": ""}
    llm = _cfg(root).llm
    assert (llm.embed_provider, llm.embed_model) == ("ollama", "e:1b")


def test_a_failed_test_of_a_new_ollama_profile_of_this_machine_puts_the_global_config_back(
        home, monkeypatch, capsys):
    """Scope 1 makes the profile the Ollama profile of this machine before the test: a failure takes it back."""
    from fw_context_mcp.llm.errors import LLMError

    root = home["root"]
    _global(GLOBAL + '\n[llm.profiles.box]\nprovider = "ollama"\nurl = "http://gpu-box:11434"\nmodel = "big:30b"\n')
    before = settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8")

    def _fails(prompt, cfg, **kwargs):
        raise LLMError("HTTP 500")

    monkeypatch.setattr(client, "call_llm", _fails)
    # box (row 2), yes to the Ollama profile of this machine, for this project (1).
    _answers(monkeypatch, ["2", "1"], confirm=True)

    _llm_lists.choose_chat(root, _cfg(root))

    out = capsys.readouterr().out
    assert settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8") == before
    assert "[warn] The test call failed: HTTP 500" in out, out
    assert "is the Ollama profile of this machine" not in out, out
    assert _local_llm(root).get("profile") != "box"


def test_a_new_ollama_profile_of_this_machine_is_told_after_the_test(home, monkeypatch, capsys):
    root = home["root"]
    _global(GLOBAL + '\n[llm.profiles.box]\nprovider = "ollama"\nurl = "http://gpu-box:11434"\nmodel = "big:30b"\n')
    # The test request prints a mark: the "[ok]" must come after it.
    monkeypatch.setattr(client, "call_llm", lambda prompt, cfg, **kwargs: print("<test request>") or "OK")
    _answers(monkeypatch, ["2", "1"], confirm=True)

    _llm_lists.choose_chat(root, _cfg(root))

    out = capsys.readouterr().out
    assert _global_llm()["ollama_profile"] == "box" and _local_llm(root)["profile"] == "box"
    assert out.index("[ok] Profile 'box' is the Ollama profile of this machine") > out.index("<test request>"), out


def test_option_0_removes_an_old_enabled_false_too(home, monkeypatch, capsys):
    """Else the analysis stays off, and the step said that the global settings apply."""
    root = home["root"]
    _global(GLOBAL + "\n" + DS)
    _local(root, '[llm]\nenabled = false\nprofile = "ds"\nanalyze_symbols = false\n')
    assert _llm_lists.global_offers(root) == (True, False)
    _answers(monkeypatch, ["0"])

    _llm_lists.choose_chat(root, _cfg(root), offer_global=True)

    assert _local_llm(root) == {"analyze_symbols": False}, "analyze_symbols selects no backend (option d)"


def test_option_0_is_offered_for_an_old_enabled_false_alone(home, monkeypatch, capsys):
    root = home["root"]
    _local(root, '[llm]\nenabled = false\n')
    assert _llm_lists.global_offers(root) == (True, False)
    # Change, no service, option 0 of the LLM analysis, keep the embeddings.
    _answers(monkeypatch, ["y", "", "0", ""])

    ui.prompt_llm_config(root, _cfg(root), non_interactive=False)

    out = capsys.readouterr().out
    assert "0. the global settings (removes the profile of .fw-context/local.toml)" in out, out
    assert _local_llm(root) == {}
    assert _cfg(root).llm.enabled is True


def test_scope_2_of_a_chat_selection_removes_an_old_enabled_false(home, monkeypatch, capsys):
    root = home["root"]
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    _global(GLOBAL + "\n" + DS)
    _local(root, '[llm]\nenabled = false\n')
    # ds (row 2) for each project (2).
    _answers(monkeypatch, ["2", "2"])

    _llm_lists.choose_chat(root, _cfg(root))

    assert _local_llm(root) == {}
    assert _global_llm()["profile"] == "ds"
    assert "[ok] LLM analysis on again (removed enabled = false from .fw-context/local.toml)" in \
        capsys.readouterr().out


def test_an_embeddings_profile_that_cannot_run_is_no_choice(home, monkeypatch, capsys):
    """Else the index run fails at the embeddings."""
    root = home["root"]
    _global(GLOBAL + "\n" + OR)
    before = settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8")
    _answers(monkeypatch, ["2", "1"])

    _llm_lists.choose_embeddings(root, _cfg(root))

    out = capsys.readouterr().out
    assert "(no API key: run 'fw-context llm key set or')" in out, out
    assert "[skip] The profile 'or' cannot run: no API key: run 'fw-context llm key set or'" in out, out
    assert settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8") == before
    assert not (root / ".fw-context" / "local.toml").exists()


def test_llm_setup_in_a_project_offers_and_applies_option_0(home, monkeypatch, capsys):
    root = home["root"]
    # enabled: a key of the global config that local.toml does not set (see the next test).
    _global(GLOBAL.replace("[llm]\n", "[llm]\nenabled = true\n", 1) + "\n" + DS)
    _local(root, '[llm]\nprofile = "ds"\nembed_model = "x:1b"\n')
    # No service, option 0 in both lists, no removal.
    _answers(monkeypatch, ["", "0", "0", ""])

    _llm_wizard.run_wizard(root, _cfg(root))

    out = capsys.readouterr().out
    assert "0. the global settings (removes the profile of .fw-context/local.toml)" in out, out
    assert "0. the global settings (removes embed_profile and embed_model of .fw-context/local.toml)" in out, out
    assert _local_llm(root) == {}
    assert (_cfg(root).llm.profile, _cfg(root).llm.embed_model) == ("local", "e:1b")


def test_a_removed_embeddings_profile_takes_its_model_with_it(home, monkeypatch, capsys):
    """Else the model of the removed API goes to the Ollama of the global settings."""
    root = home["root"]
    _global(GLOBAL + "\n" + OR)
    _local(root, '[llm]\nembed_profile = "or"\nembed_model = "mistralai/codestral-embed-2505"\n')
    # Keep each setting, then remove the profile that local.toml selects.
    _answers(monkeypatch, ["", "", "", "or"], confirm=True)

    _llm_wizard.run_wizard(root, _cfg(root))

    assert _local_llm(root) == {}, capsys.readouterr().out
    llm = _cfg(root).llm
    assert (llm.embed_provider, llm.embed_model) == ("ollama", "e:1b")


@pytest.mark.parametrize(("entry", "shown"), [
    ({"usd_per_1k_symbols": 1.162, "usd_per_1k_symbols_off_peak": 0.581}, "$0.58-1.16/1000 symbols"),
    ({"usd_per_1k_symbols": 0.62}, "$0.62/1000 symbols"),
    ({"usd_per_1k_symbols": 0.5, "usd_per_1k_symbols_off_peak": 0.5}, "$0.50/1000 symbols"),
    ({}, ""),
], ids=["off-peak range", "one price", "the same two prices", "no price"])
def test_the_price_of_a_chat_model_shows_the_range(entry, shown):
    """A run in the peak window pays the higher price: the lower one alone was too low."""
    from fw_context_mcp.llm.recommend import CHAT, price

    assert price(entry, CHAT) == shown


def test_the_cloud_alternative_names_the_host_that_gets_the_code(home):
    from fw_context_mcp.llm.recommend import CHAT, EMBEDDING, cloud_alternative

    assert cloud_alternative(EMBEDDING).endswith("; the code goes to openrouter.ai)")
    assert cloud_alternative(CHAT).endswith("; the code goes to ollama.com)")


def test_a_quality_that_is_not_a_number_shows_a_dash(home, monkeypatch, capsys):
    """[llm.models] is a file of the user: before, a text quality stopped the list with a traceback."""
    root = home["root"]
    _global('[llm]\nprofile = "local"\nembed_model = "e:1b"\n\n' + LOCAL + CLOUD
            + '\n[llm.models."m:cloud"]\nremote = true\nquality = "high"\n')
    _answers(monkeypatch, [""])

    _llm_lists.choose_chat(root, _cfg(root))

    rows = _rows(capsys.readouterr().out)
    assert rows[1] == "2. cloudy: ollama m:cloud  ⚠ https://ollama.com:443  -", rows


def test_a_failed_restore_of_local_toml_says_so(home, monkeypatch, capsys):
    """The editor cannot change a table of another form; when the old text cannot be written back, the step says so."""
    from fw_context_mcp.config import _toml_editor

    root = home["root"]
    _local(root, '[llm]  # my settings\nembed_model = "x:1b"\n')
    before = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")

    def _fails(path, content):
        raise OSError("disk full")

    monkeypatch.setattr(_toml_editor, "_atomic_write", _fails)
    _answers(monkeypatch, ["0"])

    _llm_lists.choose_embeddings(root, _cfg(root), offer_global=True)

    out = capsys.readouterr().out
    assert ("[warn] fw-context cannot change embed_model of .fw-context/local.toml, and cannot write its old text "
            "back (disk full). Read the file before the next command.") in out, out
    assert "[ok] Embeddings: the global settings apply" not in out, out
    assert (root / ".fw-context" / "local.toml").read_text(encoding="utf-8") == before


def test_option_0_is_offered_when_local_toml_sets_each_key_of_the_global_config(home):
    """The global config sets a backend: local.toml that sets the same keys does not remove it."""
    root = home["root"]
    _global(GLOBAL + "\n" + DS)
    _local(root, '[llm]\nprofile = "ds"\nembed_model = "x:1b"\n')

    assert _llm_lists.global_offers(root) == (True, True)


def test_a_profile_whose_context_is_not_known_is_no_choice(home, monkeypatch, capsys):
    """The analysis needs the context: a model that the list of DeepSeek does not hold cannot run."""
    root = home["root"]
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    _global(GLOBAL + '\n[llm.profiles.ds]\nprovider = "deepseek"\nmodel = "deepseek-v4-flash"\n')
    before = settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8")
    _answers(monkeypatch, ["2", "1"])

    _llm_lists.choose_chat(root, _cfg(root))

    out = capsys.readouterr().out
    assert "[skip] The profile 'ds' cannot analyse: the model 'deepseek-v4-flash'" in out, out
    assert home["selected"] == []
    assert settings._GLOBAL_CONFIG_PATH.read_text(encoding="utf-8") == before
    assert not (root / ".fw-context" / "local.toml").exists()


@pytest.mark.parametrize(("answer", "scope"), [("", "project"), ("1", "project"), ("2", "global")])
def test_the_scope_has_one_choice_on_each_line(home, monkeypatch, capsys, answer, scope):
    """Request of the user, 2026-10-23: the two choices were on one line."""
    _answers(monkeypatch, [answer])

    assert _llm_lists._ask_scope(home["root"], "the profile 'ds'") == scope
    out = capsys.readouterr().out
    assert ("  Where does the profile 'ds' apply?\n    1. this project only (.fw-context/local.toml)\n"
            "    2. each project (the global default)\n") in out

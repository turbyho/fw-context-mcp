"""The model list of an API or ChatGPT profile (``llm.model_list``): the names of the models and their context.

Decisions of the user, 2026-10-22: the context of an API chat model must be
known, the names come from the list of the provider, and a static
``context`` of the profile applies only to a model whose list gives none.

These tests run the real ``_get`` and ``chatgpt_plan.model_contexts``:
this module replaces the guard of conftest with a fake ``httpx.get`` that
answers only the URLs of the test.  Any other URL fails the test, thus no
request leaves.
"""

from __future__ import annotations

import httpx
import pytest

from fw_context_mcp.config.settings import LLMConfig
from fw_context_mcp.llm import chatgpt_plan, model_list
from fw_context_mcp.llm.model_list import ModelContextError, ModelInfo, ModelListError

DEEPSEEK = "https://api.deepseek.com"
#: The list of DeepSeek: the chat client adds /v1 to the bare host, and the list is under the chat endpoint.
DEEPSEEK_LIST = f"{DEEPSEEK}/v1/models"
OPENROUTER = "https://openrouter.ai/api/v1"
OTHER = "https://api.example.test/v1"


@pytest.fixture(autouse=True)
def _no_model_list_requests(monkeypatch):
    """Replace the guard of conftest (same name): empty caches; ``httpx.get`` comes from the fixture ``server``."""
    monkeypatch.setattr(model_list, "_LISTS", {})
    monkeypatch.setattr(model_list, "_ENDPOINT_CONTEXTS", {})


@pytest.fixture(autouse=True)
def server(monkeypatch) -> dict:
    """Answer ``httpx.get`` from ``answers`` (URL → (status, JSON object or text)); record each request in ``sent``."""
    answers: dict[str, tuple[int, object]] = {}
    sent: list[dict] = []

    def _get(url, headers=None, timeout=None):
        sent.append({"url": url, "auth": (headers or {}).get("Authorization")})
        if url not in answers:
            pytest.fail(f"a request to {url}, which the test does not answer")
        status, body = answers[url]
        request = httpx.Request("GET", url)
        if isinstance(body, str):
            return httpx.Response(status, text=body, request=request)
        return httpx.Response(status, json=body, request=request)

    monkeypatch.setattr(httpx, "get", _get)
    return {"answers": answers, "sent": sent}


def _llm(provider: str, model: str, *, url: str = "", context: int = 0, profile: str = "p") -> LLMConfig:
    """Return the config of a chat profile; the key is given, thus no key store is read."""
    base = url or {"deepseek": DEEPSEEK, "openrouter": OPENROUTER, "openai_compatible": OTHER,
                   "chatgpt": chatgpt_plan.API_BASE}.get(provider, "")
    return LLMConfig(provider=provider, profile=profile, model=model, chat_api_base=base, chat_api_key="sk-test",
                     context=context)


def _deepseek_list(server: dict, url: str = DEEPSEEK_LIST) -> None:
    server["answers"][url] = (200, {"data": [
        {"id": "deepseek-flash", "context_window": 1048576},
        {"id": "deepseek-pro"},
        # Entries of another shape do not count.
        "text", {"id": ""}, {"context_window": 5},
    ]})


# ── Each provider ──


def test_deepseek_gives_the_names_and_the_context_window(server):
    _deepseek_list(server)
    llm = _llm("deepseek", "deepseek-flash")

    assert model_list.provider_models(llm) == [ModelInfo("deepseek-flash", 1048576), ModelInfo("deepseek-pro", None)]
    assert model_list.model_context(llm) == 1048576
    assert server["sent"][0]["auth"] == "Bearer sk-test"


def test_openrouter_takes_the_smallest_context_of_the_endpoints(server):
    """The routing selects one endpoint: the prompt must fit each of them."""
    model = "deepseek/deepseek-chat"
    server["answers"][f"{OPENROUTER}/models"] = (200, {"data": [{"id": model, "context_length": 163840}]})
    server["answers"][f"{OPENROUTER}/models/{model}/endpoints"] = (200, {"data": {"endpoints": [
        {"context_length": 163840}, {"context_length": 128000}, {"context_length": "x"}, {"name": "no context"},
    ]}})

    assert model_list.model_context(_llm("openrouter", model)) == 128000


def test_openrouter_without_an_endpoint_context_uses_the_static_context(server):
    model = "vendor/m"
    server["answers"][f"{OPENROUTER}/models"] = (200, {"data": [{"id": model}]})
    server["answers"][f"{OPENROUTER}/models/{model}/endpoints"] = (200, {"data": {"endpoints": []}})

    assert model_list.model_context(_llm("openrouter", model, context=32768)) == 32768


def test_the_chatgpt_plan_gives_the_context_window_of_its_list(server, monkeypatch):
    """Only the models that the plan lists for a user; max_context_window needs an option that is not sent."""
    monkeypatch.setattr(chatgpt_plan, "access_token", lambda profile, **kwargs: "plan-token")
    server["answers"][chatgpt_plan.MODELS_URL] = (200, {"models": [
        {"slug": "gpt-6-luna", "visibility": "list", "context_window": 272000, "max_context_window": 872000},
        {"slug": "gpt-hidden", "visibility": "hide", "context_window": 1000},
        {"slug": "gpt-mini", "visibility": "list"},
        {"visibility": "list", "context_window": 5},
    ]})

    assert chatgpt_plan.model_contexts("gpt") == {"gpt-6-luna": 272000, "gpt-mini": None}
    assert server["sent"][0]["auth"] == "Bearer plan-token"
    assert model_list.model_context(_llm("chatgpt", "gpt-6-luna", profile="gpt")) == 272000


def test_a_failed_list_of_the_chatgpt_plan_is_a_context_error(server, monkeypatch):
    monkeypatch.setattr(chatgpt_plan, "access_token", lambda profile, **kwargs: "plan-token")
    server["answers"][chatgpt_plan.MODELS_URL] = (500, "server error")

    with pytest.raises(ModelContextError, match="the model list of the ChatGPT plan failed") as error:
        model_list.model_context(_llm("chatgpt", "gpt-6-luna", profile="gpt"))

    assert error.value.not_listed is False


def test_another_api_gives_the_names_without_a_context(server):
    server["answers"][f"{OTHER}/models"] = (200, {"object": "list", "data": [{"id": "m", "object": "model"}]})

    assert model_list.provider_models(_llm("openai_compatible", "m")) == [ModelInfo("m", None)]
    with pytest.raises(ModelContextError, match=r"gives no context of model 'm': set context") as error:
        model_list.model_context(_llm("openai_compatible", "m"))
    assert error.value.not_listed is False
    assert model_list.model_context(_llm("openai_compatible", "m", context=8192)) == 8192


# ── The rules ──


def test_a_model_that_the_list_does_not_hold_is_not_listed(server):
    """A static context does not repair it: a model of the list does."""
    _deepseek_list(server)

    with pytest.raises(ModelContextError, match="is not in the model list of its provider") as error:
        model_list.model_context(_llm("deepseek", "deepseek-v4-flash", context=65536))

    assert error.value.not_listed is True
    assert "deepseek-flash, deepseek-pro" in str(error.value)


def test_an_unreadable_list_is_a_context_error(server):
    server["answers"][DEEPSEEK_LIST] = (503, "busy")

    with pytest.raises(ModelContextError, match="gives HTTP 503") as error:
        model_list.model_context(_llm("deepseek", "deepseek-flash", context=65536))

    assert (error.value.not_listed, error.value.unreadable) == (False, True)


def test_another_api_without_a_list_uses_the_static_context(server):
    """Such a server need not have the list: the user gave the context for it."""
    server["answers"][f"{OTHER}/models"] = (404, "not found")

    assert model_list.model_context(_llm("openai_compatible", "m", context=16384)) == 16384
    with pytest.raises(ModelContextError, match="gives HTTP 404"):
        model_list.model_context(_llm("openai_compatible", "m"))


def test_the_context_of_the_list_wins_over_the_static_context(server):
    _deepseek_list(server)

    assert model_list.model_context(_llm("deepseek", "deepseek-flash", context=4096)) == 1048576


def test_the_static_context_applies_when_the_list_gives_none(server):
    _deepseek_list(server)

    assert model_list.model_context(_llm("deepseek", "deepseek-pro", context=65536)) == 65536


def test_each_list_is_read_one_time_in_a_process(server):
    model = "vendor/m"
    server["answers"][f"{OPENROUTER}/models"] = (200, {"data": [{"id": model}]})
    server["answers"][f"{OPENROUTER}/models/{model}/endpoints"] = (200, {"data": {"endpoints": [
        {"context_length": 8192}]}})
    llm = _llm("openrouter", model)

    for _ in range(3):
        assert model_list.model_context(llm) == 8192

    assert [entry["url"] for entry in server["sent"]] == [f"{OPENROUTER}/models",
                                                          f"{OPENROUTER}/models/{model}/endpoints"]


@pytest.mark.parametrize(("url", "listed"), [
    (DEEPSEEK, DEEPSEEK_LIST),
    (f"{DEEPSEEK}/chat/completions", f"{DEEPSEEK}/models"),
    (f"{DEEPSEEK}/v1", DEEPSEEK_LIST),
], ids=["bare host", "chat endpoint", "v1"])
def test_the_list_is_under_the_chat_endpoint_without_chat_completions(server, url, listed):
    """The list must come from the API that runs the chat: the chat client adds /v1 to a bare host."""
    _deepseek_list(server, listed)

    assert model_list.model_context(_llm("deepseek", "deepseek-flash", url=url)) == 1048576
    assert [entry["url"] for entry in server["sent"]] == [listed]


def test_a_provider_without_a_list_has_only_the_static_context(server):
    llm = LLMConfig(provider="ollama", model="m")

    with pytest.raises(ValueError, match="has no model list"):
        model_list.provider_models(llm)
    with pytest.raises(ModelContextError, match="has no model list"):
        model_list.model_context(llm)
    assert model_list.model_context(LLMConfig(provider="ollama", model="m", context=2048)) == 2048
    assert server["sent"] == []


def test_an_http_error_names_the_status_and_not_the_body(server):
    """The body of an error can echo the key."""
    server["answers"][DEEPSEEK_LIST] = (401, "invalid key sk-test")

    with pytest.raises(ModelListError) as error:
        model_list.provider_models(_llm("deepseek", "deepseek-flash"))

    assert str(error.value) == f"{DEEPSEEK_LIST} gives HTTP 401"


def test_an_answer_that_is_not_a_list_is_a_list_error(server):
    server["answers"][DEEPSEEK_LIST] = (200, "<html>proxy login</html>")

    with pytest.raises(ModelListError, match="does not answer with a model list"):
        model_list.provider_models(_llm("deepseek", "deepseek-flash"))


# ── doctor: the check chat-model of an API profile ──


def _check(llm: LLMConfig):
    from fw_context_mcp.deps._checks import _check_api_chat_model

    return _check_api_chat_model(llm)


def test_the_check_names_the_context_of_the_list(server):
    _deepseek_list(server)

    result = _check(_llm("deepseek", "deepseek-flash"))

    assert (result.status, result.message) == ("ok", "deepseek-flash (context 1048576 tokens)")


def test_the_check_is_an_error_without_the_context(server):
    _deepseek_list(server)

    result = _check(_llm("deepseek", "deepseek-v4-flash"))

    assert result.status == "error" and "not in the model list" in result.message
    assert result.instructions == "fw-context llm setup"


def test_the_check_reports_a_static_context_next_to_the_list(server):
    _deepseek_list(server)

    result = _check(_llm("deepseek", "deepseek-flash", context=4096))

    assert result.status == "degraded"
    assert "context = 4096" in result.message and "1048576" in result.message


def test_the_check_skips_a_provider_without_a_list(server):
    assert _check(LLMConfig(provider="ollama", model="m")).status == "skipped"


def test_the_chat_model_check_of_an_api_profile_reads_the_list(server):
    """``check_ollama_chat_model`` gives the check of the list to an OpenAI-compatible chat."""
    from fw_context_mcp.deps._checks import check_ollama_chat_model

    _deepseek_list(server)
    llm = _llm("deepseek", "deepseek-flash")
    llm.chat_api_format = "openai"

    assert check_ollama_chat_model(llm).message == "deepseek-flash (context 1048576 tokens)"


# ── The review of 2026-10-22 ──


def test_another_api_with_a_context_runs_a_name_that_its_list_does_not_hold(server):
    """llama-server lists the name of its model file and runs each name: the context of the user applies."""
    server["answers"][f"{OTHER}/models"] = (200, {"data": [{"id": "model-file.gguf"}]})

    assert model_list.model_context(_llm("openai_compatible", "qwen-chat", context=32768)) == 32768
    with pytest.raises(ModelContextError) as error:
        model_list.model_context(_llm("openai_compatible", "qwen-chat"))
    assert error.value.not_listed is True


def test_a_list_expires_after_ten_minutes(server, monkeypatch):
    """A model that the provider added after the start of the MCP server was "not in the list" until a restart."""
    clock = [1000.0]
    monkeypatch.setattr(model_list.time, "monotonic", lambda: clock[0])
    _deepseek_list(server)
    llm = _llm("deepseek", "deepseek-flash")

    model_list.provider_models(llm)
    clock[0] += model_list._CACHE_SECONDS - 1
    model_list.provider_models(llm)
    assert len(server["sent"]) == 1, "the list of this process"

    clock[0] += 2
    model_list.provider_models(llm)
    assert len(server["sent"]) == 2, "an old list is read again"


def test_the_endpoint_context_expires_too(server, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(model_list.time, "monotonic", lambda: clock[0])
    model = "vendor/m"
    server["answers"][f"{OPENROUTER}/models"] = (200, {"data": [{"id": model}]})
    endpoints = f"{OPENROUTER}/models/{model}/endpoints"
    server["answers"][endpoints] = (200, {"data": {"endpoints": [{"context_length": 8192}]}})
    llm = _llm("openrouter", model)

    model_list.model_context(llm)
    clock[0] += model_list._CACHE_SECONDS + 1
    model_list.model_context(llm)

    assert [entry["url"] for entry in server["sent"]].count(endpoints) == 2


@pytest.mark.parametrize(("status", "forgotten"), [(401, ["p"]), (403, []), (500, [])])
def test_a_refused_key_is_forgotten(server, monkeypatch, status, forgotten):
    """As the chat client: a key that the user stores after the start of the process applies next time."""
    from fw_context_mcp.llm import chat_router, secrets

    calls: list[str] = []
    monkeypatch.setattr(secrets, "forget", calls.append)
    monkeypatch.setattr(chat_router, "chat_api_key", lambda llm: "sk-stored")
    server["answers"][DEEPSEEK_LIST] = (status, "refused")
    llm = _llm("deepseek", "deepseek-flash")
    llm.chat_api_key = None

    with pytest.raises(ModelListError, match=f"gives HTTP {status}"):
        model_list.provider_models(llm)

    assert calls == forgotten
    assert server["sent"][0]["auth"] == "Bearer sk-stored"


def test_a_key_of_the_caller_is_not_forgotten(server, monkeypatch):
    from fw_context_mcp.llm import secrets

    calls: list[str] = []
    monkeypatch.setattr(secrets, "forget", calls.append)
    server["answers"][DEEPSEEK_LIST] = (401, "refused")

    with pytest.raises(ModelListError):
        model_list.provider_models(_llm("deepseek", "deepseek-flash"))

    assert calls == []


def test_openrouter_without_an_endpoint_context_takes_the_context_of_the_list(server):
    model = "vendor/m"
    server["answers"][f"{OPENROUTER}/models"] = (200, {"data": [{"id": model, "context_length": 163840}]})
    server["answers"][f"{OPENROUTER}/models/{model}/endpoints"] = (200, {"data": {"endpoints": [{"name": "e"}]}})

    assert model_list.model_context(_llm("openrouter", model, context=4096)) == 163840


@pytest.mark.parametrize(("value", "expected"), [
    (8192, 8192), (True, None), (False, None), (0, None), (-1, None), ("8192", None), (8192.0, None), (None, None),
])
def test_only_a_positive_integer_is_a_context(value, expected):
    """``True`` is an int in Python: a list with "context_window": true gave a context of 1 token."""
    assert model_list.tokens(value) == expected


def test_a_context_window_of_true_is_no_context(server, monkeypatch):
    server["answers"][DEEPSEEK_LIST] = (200, {"data": [{"id": "m", "context_window": True}]})
    monkeypatch.setattr(chatgpt_plan, "access_token", lambda profile, **kwargs: "plan-token")
    server["answers"][chatgpt_plan.MODELS_URL] = (200, {"models": [
        {"slug": "gpt-x", "visibility": "list", "context_window": True}]})

    assert model_list.provider_models(_llm("deepseek", "m")) == [ModelInfo("m", None)]
    assert chatgpt_plan.model_contexts("gpt") == {"gpt-x": None}


@pytest.mark.parametrize("body", [[1, 2], {"models": "x"}, {"models": {"slug": "gpt"}}],
                         ids=["a list", "models a text", "models an object"])
def test_an_odd_answer_of_the_chatgpt_plan_is_an_llm_error(server, monkeypatch, body):
    """Before: AttributeError or TypeError, out of the setup and of doctor."""
    from fw_context_mcp.llm.errors import LLMError

    monkeypatch.setattr(chatgpt_plan, "access_token", lambda profile, **kwargs: "plan-token")
    server["answers"][chatgpt_plan.MODELS_URL] = (200, body)

    with pytest.raises(LLMError):
        chatgpt_plan.model_contexts("gpt")
    with pytest.raises(ModelListError):
        model_list.provider_models(_llm("chatgpt", "gpt-6-luna", profile="gpt"))


# ── configure_llm_core reads the context before the test call ──


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A global config with a DeepSeek profile, and a project; the test call is recorded."""
    from fw_context_mcp.config import settings
    from fw_context_mcp.llm import client

    path = tmp_path / "home" / "config.toml"
    path.parent.mkdir()
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", path)
    monkeypatch.setenv("FW_CONTEXT_HOME", str(path.parent))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    settings._config_cache.clear()
    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    (root / ".fw-context" / "config.toml").write_text("[project]\n", encoding="utf-8")
    (root / ".fw-context" / "local.toml").write_text('[llm]\nembed_model = "x:1b"\n', encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(client, "call_llm", lambda prompt, cfg, **kwargs: calls.append(cfg.model) or "OK")
    return {"root": root, "global": path, "calls": calls}


def _deepseek_profile(project: dict, model: str) -> None:
    from fw_context_mcp.config import settings

    project["global"].write_text(f'[llm]\nembed_model = "e:1b"\n\n[llm.profiles.ds]\nprovider = "deepseek"\n'
                                 f'model = "{model}"\n', encoding="utf-8")
    settings._config_cache.clear()


def test_configure_refuses_a_model_whose_context_is_not_known(server, project):
    """Before: the test call answered, and the analysis stopped later without the context."""
    from fw_context_mcp.llm.configure import configure_llm_core

    _deepseek_list(server)
    _deepseek_profile(project, "deepseek-v4-flash")
    local = project["root"] / ".fw-context" / "local.toml"
    before = local.read_text(encoding="utf-8")

    result = configure_llm_core(project["root"], {"enabled": True, "profile": "ds"})

    assert result["status"] == "error"
    assert result["message"].startswith("The profile cannot analyse: the model 'deepseek-v4-flash'"), result
    assert local.read_text(encoding="utf-8") == before
    assert project["calls"] == [], "no test call"


def test_configure_gives_the_context_of_the_list(server, project):
    from fw_context_mcp.llm.configure import configure_llm_core

    _deepseek_list(server)
    _deepseek_profile(project, "deepseek-flash")

    result = configure_llm_core(project["root"], {"enabled": True, "profile": "ds"})

    assert result["status"] == "ok", result
    assert result["context"] == 1048576
    assert project["calls"] == ["deepseek-flash"]


def test_the_template_names_the_recommended_deepseek_model():
    """The model of the commented DeepSeek profile must be in the list of DeepSeek: the recommended one is."""
    from fw_context_mcp.config import settings
    from fw_context_mcp.llm.providers import PROVIDERS

    recommended = PROVIDERS["deepseek"].recommended_model
    assert recommended == "deepseek-flash"
    assert f'# model = "{recommended}"' in settings._GLOBAL_DEFAULTS

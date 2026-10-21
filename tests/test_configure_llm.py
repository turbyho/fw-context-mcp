"""Tests for the configure_llm MCP tool.

The tool selects an LLM profile of the global config for one project.
The profiles themselves (server, model, key variable) are in the global
config only, and only the operator writes them: the tool takes no URL and
no API key.  WHY: a key that a tool call writes passes through the
conversation of the LLM client, and a URL that a tool call sets can send
the source code to any host.

Covers:
- Writing to local.toml (only local.toml, not global/shared config)
- Test API call success and failure, and the rollback after a failure
- Compliance warning for a profile whose chat host is not this machine
- An unknown profile, and a call with no change
- Integration with config reload (mtime-based cache invalidation)
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from fw_context_mcp.mcp.handlers.maintenance import configure_llm

# ── Helpers ──────────────────────────────────────────────────────────────────

#: The name of the environment variable that holds the key of profile "api".
API_KEY_ENV = "FW_CONTEXT_TEST_API_KEY"

#: The global config of each test.  WHY each test writes its own: the
#: session fixture copies the global config of the operator, and its
#: profiles would decide the result.  ``embed_model`` is set, thus ``load``
#: does not probe the GPU or Ollama for the automatic model.  The ``api``
#: profile has a long timeout, thus a test can see that the test call uses
#: its own short one.
GLOBAL_PROFILES = (
    '[llm]\nprofile = "local"\nenabled = true\nembed_model = "test-embed:1b"\n\n'
    '[llm.profiles.local]\nprovider = "ollama"\nurl = "http://localhost:11434"\nmodel = "local-chat:1b"\n\n'
    '[llm.profiles.api]\nprovider = "openai_compatible"\nurl = "https://api.example.com/v1"\n'
    f'model = "api-chat"\napi_key_env = "{API_KEY_ENV}"\ntimeout = 600.0\n\n'
    '[llm.profiles.loopback]\nprovider = "openai_compatible"\nurl = "http://localhost:4000"\nmodel = "proxy-chat"\n'
)


@pytest.fixture(autouse=True)
def global_config(tmp_path: Path, monkeypatch) -> Path:
    """Point the global config to a file with the profiles of *GLOBAL_PROFILES*."""
    from fw_context_mcp.config import settings

    path = tmp_path / "home" / "config.toml"
    path.parent.mkdir()
    path.write_text(GLOBAL_PROFILES, encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", path)
    monkeypatch.setenv(API_KEY_ENV, "sk-from-the-environment")
    settings._config_cache.clear()
    return path


def _make_project(tmp_path: Path, project_id: str = "abc123") -> Path:
    """Create a project root with config.toml (initialized) and empty local.toml."""
    root = tmp_path / "proj"
    cfg_dir = root / ".fw-context"
    cfg_dir.mkdir(parents=True)
    index_dir = tmp_path / "index"
    index_dir.mkdir()

    config_toml = (
        "[project]\n"
        f'id = "{project_id}"\n\n'
        "[index]\n"
        f'db_dir = "{str(index_dir).replace(chr(92), "/")}"\n'
        'compile_commands = "compile_commands.json"\n'
    )
    (cfg_dir / "config.toml").write_text(config_toml, encoding="utf-8")
    (cfg_dir / "local.toml").write_text("", encoding="utf-8")
    return root


def _local_llm(root: Path) -> dict:
    """Return the ``[llm]`` table of local.toml (empty when the file has none)."""
    return tomllib.loads((root / ".fw-context" / "local.toml").read_text(encoding="utf-8")).get("llm", {})


def _mock_openai_response(
    status_code: int = 200,
    content: str = "OK",
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json={"choices": [{"message": {"content": content}}]},
        request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"),
    )


# ── Write behavior ───────────────────────────────────────────────────────────


class TestConfigureLlmWrite:
    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_writes_the_profile_to_local_toml(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        configure_llm(project_root=str(root), profile="api")
        local = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")
        assert 'profile = "api"' in local

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_the_api_key_comes_from_its_variable_and_is_not_written(self, mock_post, tmp_path):
        """The profile names the variable; the key itself never goes into a file."""
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        configure_llm(project_root=str(root), profile="api")
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer sk-from-the-environment"
        local = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")
        assert "sk-from-the-environment" not in local
        assert "chat_api_key" not in local

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_the_test_call_uses_the_model_of_the_profile(self, mock_post, tmp_path):
        """The model is a key of the profile, not of local.toml."""
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        configure_llm(project_root=str(root), profile="api")
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["model"] == "api-chat"
        assert "model" not in _local_llm(root)

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_writes_embed_model_to_local_toml(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        configure_llm(project_root=str(root), profile="api", embed_model="other-embed:1b")
        local = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")
        assert 'embed_model = "other-embed:1b"' in local

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_does_not_modify_config_toml(self, mock_post, tmp_path, global_config):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        config_path = root / ".fw-context" / "config.toml"
        original = config_path.read_text(encoding="utf-8")
        configure_llm(project_root=str(root), profile="api")
        assert config_path.read_text(encoding="utf-8") == original
        assert global_config.read_text(encoding="utf-8") == GLOBAL_PROFILES

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_partial_params_only_write_provided(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        configure_llm(
            project_root=str(root),
            profile="api",
            # embed_model not provided
        )
        assert _local_llm(root) == {"profile": "api"}


# ── Test call behavior ───────────────────────────────────────────────────────


class TestConfigureLlmTestCall:
    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_test_call_success_returns_status_ok(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response(content="OK")
        root = _make_project(tmp_path)
        result = configure_llm(project_root=str(root), profile="api")
        assert result["status"] == "ok"

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_test_call_returns_latency(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        result = configure_llm(project_root=str(root), profile="api")
        assert "test_latency_s" in result
        assert result["test_latency_s"] >= 0

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_test_call_failure_returns_error_status(self, mock_post, tmp_path):
        mock_post.return_value = httpx.Response(
            status_code=500,
            json={"error": "server error"},
            text="server error",
            request=httpx.Request("POST", "test"),
        )
        root = _make_project(tmp_path)
        result = configure_llm(project_root=str(root), profile="api")
        assert result["status"] == "error"
        assert "message" in result

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_test_call_connect_error_returns_error(self, mock_post, tmp_path):
        mock_post.side_effect = httpx.ConnectError("refused")
        root = _make_project(tmp_path)
        result = configure_llm(project_root=str(root), profile="api")
        assert result["status"] == "error"

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_test_call_uses_simple_prompt(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        configure_llm(project_root=str(root), profile="api")
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["messages"][0]["content"] == "Reply with exactly: OK"

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    def test_an_ollama_profile_is_tested_on_its_server(self, mock_post, tmp_path):
        """The other provider: the test call goes to /api/generate of the Ollama profile."""
        mock_post.return_value = httpx.Response(
            status_code=200,
            json={"response": "OK"},
            request=httpx.Request("POST", "http://localhost:11434/api/generate"),
        )
        root = _make_project(tmp_path)
        (root / ".fw-context" / "local.toml").write_text('[llm]\nprofile = "api"\n', encoding="utf-8")

        result = configure_llm(project_root=str(root), profile="local")

        assert result["status"] == "ok", result
        assert (result["profile"], result["provider"]) == ("local", "ollama")
        args, kwargs = mock_post.call_args
        assert args[0] == "http://localhost:11434/api/generate"
        assert kwargs["json"]["model"] == "local-chat:1b"
        assert _local_llm(root) == {"profile": "local"}


class TestASettingThatFailsItsTestIsNotKept:
    """The tool writes AND verifies, thus a failed test undoes the write.

    It used to answer "Configuration written but test call failed" and
    leave the unusable setting in local.toml.  Measured on a throwaway
    project: ``model="no_such_model_xyz:1b"`` with ``auto_pull=False``
    wrote the model, and the NEXT call then tested against that broken
    model rather than against what the operator asked for.
    """

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_a_failed_test_restores_the_previous_settings(self, mock_post, tmp_path):
        mock_post.side_effect = httpx.ConnectError("refused")
        root = _make_project(tmp_path)
        local = root / ".fw-context" / "local.toml"
        local.write_text('[llm]\nprofile = "loopback"\n', encoding="utf-8")

        result = configure_llm(project_root=str(root), profile="api")

        assert result["status"] == "error"
        text = local.read_text(encoding="utf-8")
        assert 'profile = "loopback"' in text, text
        assert 'profile = "api"' not in text, (
            f"a setting that failed its own test stayed behind: {text}"
        )

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_the_message_says_that_nothing_changed(self, mock_post, tmp_path):
        mock_post.side_effect = httpx.ConnectError("refused")
        root = _make_project(tmp_path)

        result = configure_llm(project_root=str(root), profile="api")

        assert "nothing was changed" in result["message"], result["message"]
        assert "Configuration written" not in result["message"], result["message"]

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_a_passing_test_keeps_the_new_settings(self, mock_post, tmp_path):
        """The rollback must not touch a write that verified."""
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)

        result = configure_llm(project_root=str(root), profile="api")

        assert result["status"] == "ok"
        text = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")
        assert 'profile = "api"' in text, text


# ── Result structure ─────────────────────────────────────────────────────────


class TestConfigureLlmResult:
    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_returns_chat_api_dict(self, mock_post, tmp_path):
        """An openai_compatible profile always uses the OpenAI format: the provider names it."""
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        result = configure_llm(project_root=str(root), profile="api")
        assert result["chat_api"] == {
            "configured": True,
            "endpoint": "https://api.example.com/v1/chat/completions",
            "format": "openai",
            "model": "api-chat",
        }
        assert (result["profile"], result["provider"]) == ("api", "openai_compatible")

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_returns_model(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        result = configure_llm(project_root=str(root), profile="api")
        assert result["model"] == "api-chat"

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_compliance_warning_external_url(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        result = configure_llm(project_root=str(root), profile="api")
        assert "compliance_warning" in result
        assert "external" in result["compliance_warning"].lower()
        assert "'api'" in result["compliance_warning"], "the warning names the profile"

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_no_warning_loopback_url(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        result = configure_llm(project_root=str(root), profile="loopback")
        assert result["status"] == "ok", result
        assert "compliance_warning" not in result

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_no_changes_returns_error(self, mock_post, tmp_path):
        root = _make_project(tmp_path)
        result = configure_llm(project_root=str(root))
        assert result["status"] == "error"
        assert "No configuration changes" in result["message"]
        mock_post.assert_not_called()

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_the_current_profile_is_no_change(self, mock_post, tmp_path):
        """The global config selects "local": selecting it again writes nothing."""
        root = _make_project(tmp_path)
        result = configure_llm(project_root=str(root), profile="local")
        assert result["status"] == "error"
        assert "No configuration changes" in result["message"]
        assert (root / ".fw-context" / "local.toml").read_text(encoding="utf-8") == ""
        mock_post.assert_not_called()

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_an_unknown_profile_is_refused(self, mock_post, tmp_path):
        """The tool cannot make a profile: the error names the ones that exist."""
        root = _make_project(tmp_path)
        result = configure_llm(project_root=str(root), profile="anthropic")
        assert result["status"] == "error"
        assert "'anthropic' is not a profile of the global config" in result["message"], result["message"]
        assert "profiles: api, local, loopback" in result["message"], result["message"]
        assert (root / ".fw-context" / "local.toml").read_text(encoding="utf-8") == ""
        mock_post.assert_not_called()

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_never_raises_when_the_backend_answers_garbage(self, mock_post, tmp_path):
        """An answer without ``choices`` is a structured error, not a traceback through the tool."""
        mock_post.return_value = httpx.Response(
            status_code=200,
            json={"unexpected": True},
            request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"),
        )
        root = _make_project(tmp_path)
        result = configure_llm(project_root=str(root), profile="api")
        assert result["status"] == "error", result
        assert _local_llm(root) == {}, "a setting that failed its test stayed behind"


# ── Uninitialized project ────────────────────────────────────────────────────


class TestConfigureLlmUninitialized:
    def test_uninitialized_project_returns_error_dict(self, tmp_path):
        root = tmp_path / "uninit"
        root.mkdir()
        result = configure_llm(project_root=str(root), profile="api")
        assert result["status"] == "error"
        assert "not initialized" in result["message"].lower()

    def test_uninitialized_project_no_config_dir(self, tmp_path):
        root = tmp_path / "empty"
        root.mkdir()
        result = configure_llm(project_root=str(root))
        assert result["status"] == "error"


# ── Test-call timeout ────────────────────────────────────────────────────────


class TestConfigureLlmTimeout:
    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_test_call_uses_short_timeout(self, mock_post, tmp_path):
        """The profile has ``timeout = 600.0``; the test call uses its own 30 s."""
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        configure_llm(project_root=str(root), profile="api")
        _, kwargs = mock_post.call_args
        assert kwargs["timeout"] == 30.0


# ── An omitted parameter keeps its value ────────────────────────────────────


class TestOmittedParametersKeepTheirValue:
    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_an_embedding_model_change_keeps_the_selected_profile(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        (root / ".fw-context" / "local.toml").write_text('[llm]\nprofile = "api"\n', encoding="utf-8")

        result = configure_llm(project_root=str(root), embed_model="other-embed:1b")

        assert result["status"] == "ok", result
        assert result["profile"] == "api"
        assert _local_llm(root) == {"profile": "api", "embed_model": "other-embed:1b"}


@pytest.fixture(autouse=True)
def _no_cloud_model_list(monkeypatch):
    """No request to the Ollama of the operator: the rule of the Ollama profiles asks for the cloud models."""
    from fw_context_mcp.llm import _diag

    monkeypatch.setattr(_diag, "remote_hosts", lambda url: {})

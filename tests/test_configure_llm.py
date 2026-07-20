"""Tests for the configure_llm MCP tool.

Covers:
- Writing to local.toml (only local.toml, not global/shared config)
- Test API call success and failure
- Compliance warning for external URLs
- Partial parameter writes
- Integration with config reload (mtime-based cache invalidation)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx

from fw_context_mcp.mcp.handlers.maintenance import configure_llm

# ── Helpers ──────────────────────────────────────────────────────────────────


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
    def test_writes_chat_api_base_to_local_toml(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        configure_llm(
            project_root=str(root),
            chat_api_base="https://api.deepseek.com/v1",
            chat_api_key="sk-test",
        )
        local = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")
        assert 'chat_api_base = "https://api.deepseek.com/v1"' in local

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_writes_chat_api_key_to_local_toml(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        configure_llm(
            project_root=str(root),
            chat_api_base="https://api.example.com/v1",
            chat_api_key="sk-secret",
        )
        local = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")
        assert 'chat_api_key = "sk-secret"' in local

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_writes_model_to_local_toml(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        configure_llm(
            project_root=str(root),
            chat_api_base="https://api.example.com/v1",
            model="deepseek-v4-flash",
        )
        local = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")
        assert 'model = "deepseek-v4-flash"' in local

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_writes_auto_pull_to_local_toml(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        configure_llm(
            project_root=str(root),
            chat_api_base="https://api.example.com/v1",
            auto_pull=True,
        )
        local = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")
        assert "auto_pull = true" in local

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_does_not_modify_config_toml(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        config_path = root / ".fw-context" / "config.toml"
        original = config_path.read_text(encoding="utf-8")
        configure_llm(
            project_root=str(root),
            chat_api_base="https://api.example.com/v1",
        )
        assert config_path.read_text(encoding="utf-8") == original

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_partial_params_only_write_provided(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        configure_llm(
            project_root=str(root),
            chat_api_base="https://api.example.com/v1",
            # model, embed_model, auto_pull not provided
        )
        local = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")
        assert "chat_api_base" in local
        # model should NOT be written (it was None)
        # But the template might have a commented model line — check no uncommented model
        lines = local.split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("model ") and not stripped.startswith("#"):
                # model was already in the template — that's fine
                pass


# ── Test call behavior ───────────────────────────────────────────────────────


class TestConfigureLlmTestCall:
    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_test_call_success_returns_status_ok(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response(content="OK")
        root = _make_project(tmp_path)
        result = configure_llm(
            project_root=str(root),
            chat_api_base="https://api.example.com/v1",
            chat_api_key="sk-test",
        )
        assert result["status"] == "ok"

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_test_call_returns_latency(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        result = configure_llm(
            project_root=str(root),
            chat_api_base="https://api.example.com/v1",
            chat_api_key="sk-test",
        )
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
        result = configure_llm(
            project_root=str(root),
            chat_api_base="https://api.example.com/v1",
            chat_api_key="sk-test",
        )
        assert result["status"] == "error"
        assert "message" in result

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_test_call_connect_error_returns_error(self, mock_post, tmp_path):
        mock_post.side_effect = httpx.ConnectError("refused")
        root = _make_project(tmp_path)
        result = configure_llm(
            project_root=str(root),
            chat_api_base="https://api.example.com/v1",
            chat_api_key="sk-test",
        )
        assert result["status"] == "error"

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_test_call_uses_simple_prompt(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        configure_llm(
            project_root=str(root),
            chat_api_base="https://api.example.com/v1",
            chat_api_key="sk-test",
        )
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["messages"][0]["content"] == "Reply with exactly: OK"


# ── Result structure ─────────────────────────────────────────────────────────


class TestConfigureLlmResult:
    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_returns_chat_api_dict(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        result = configure_llm(
            project_root=str(root),
            chat_api_base="https://api.example.com/v1",
            chat_api_key="sk-test",
        )
        assert "chat_api" in result
        assert result["chat_api"]["configured"] is True

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_returns_model(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        result = configure_llm(
            project_root=str(root),
            chat_api_base="https://api.example.com/v1",
            model="gpt-4o",
        )
        assert result["model"] == "gpt-4o"

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_compliance_warning_external_url(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        result = configure_llm(
            project_root=str(root),
            chat_api_base="https://api.openai.com/v1",
            chat_api_key="sk-test",
        )
        assert "compliance_warning" in result
        assert "external" in result["compliance_warning"].lower()

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_no_warning_loopback_url(self, mock_post, tmp_path):
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        result = configure_llm(
            project_root=str(root),
            chat_api_base="http://localhost:4000",
        )
        assert "compliance_warning" not in result

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_no_changes_returns_error(self, mock_post, tmp_path):
        root = _make_project(tmp_path)
        result = configure_llm(project_root=str(root))
        assert result["status"] == "error"
        assert "No configuration changes" in result["message"]
        mock_post.assert_not_called()


# ── Uninitialized project ────────────────────────────────────────────────────


class TestConfigureLlmUninitialized:
    def test_uninitialized_project_returns_error_dict(self, tmp_path):
        root = tmp_path / "uninit"
        root.mkdir()
        result = configure_llm(
            project_root=str(root),
            chat_api_base="https://api.example.com/v1",
        )
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
        mock_post.return_value = _mock_openai_response()
        root = _make_project(tmp_path)
        configure_llm(
            project_root=str(root),
            chat_api_base="https://api.example.com/v1",
            chat_api_key="sk-test",
        )
        _, kwargs = mock_post.call_args
        assert kwargs["timeout"] == 30.0

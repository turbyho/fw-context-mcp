"""The manual pull command of doctor names the Ollama server of the check.

The ``ollama`` CLI reads the server from ``OLLAMA_HOST``.  Doctor gave a
plain ``ollama pull <model>`` in ``fix_cmd``, ``instructions`` and the
message of a stalled pull.  With a remote server, a person who ran it
pulled the model onto the local server, and the check still found the
model missing.
"""

from __future__ import annotations

import httpx
import pytest

REMOTE = "http://gpu-box:11434"


@pytest.mark.parametrize(("model", "url", "system", "command"), [
    # Also the default server: the OLLAMA_HOST of the shell can name a different one.
    ("emb:1", "http://localhost:11434", "linux", "OLLAMA_HOST=http://localhost:11434 ollama pull emb:1"),
    ("emb:1", REMOTE, "linux", f"OLLAMA_HOST={REMOTE} ollama pull emb:1"),
    ("emb:1", REMOTE, "darwin", f"OLLAMA_HOST={REMOTE} ollama pull emb:1"),
    ("emb:1", REMOTE, "win32", f"$env:OLLAMA_HOST='{REMOTE}'; ollama pull 'emb:1'"),
    ("hf.co/org/m:Q4_K_M", REMOTE, "linux", f"OLLAMA_HOST={REMOTE} ollama pull hf.co/org/m:Q4_K_M"),
    ("emb:1", "http://proxy/a b", "linux", "OLLAMA_HOST='http://proxy/a b' ollama pull emb:1"),
    ("we'ird;x", "http://h'q:1", "win32", "$env:OLLAMA_HOST='http://h''q:1'; ollama pull 'we''ird;x'"),
    ("we'ird;x", REMOTE, "linux", f"OLLAMA_HOST={REMOTE} ollama pull 'we'\"'\"'ird;x'"),
])
def test_the_command_names_the_server_and_quotes_each_value(model, url, system, command):
    from fw_context_mcp.llm.ollama import ollama_pull_command

    assert ollama_pull_command(model, url, system) == command


class _Tags:
    def __init__(self, names):
        self._names = names

    def raise_for_status(self):
        return None

    def json(self):
        return {"models": [{"name": name} for name in self._names]}


def _cfg(**overrides):
    from fw_context_mcp.config.settings import LLMConfig

    cfg = LLMConfig()
    cfg.enabled = True
    cfg.ollama_url = REMOTE
    cfg.model = "chat:7b"
    cfg.embed_model = "emb:1"
    cfg.chat_api_base = None
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


@pytest.fixture
def linux(monkeypatch):
    """The checks give the command of a POSIX shell."""
    from fw_context_mcp.deps import _checks

    monkeypatch.setattr(_checks, "get_platform_info", lambda: {"system": "linux"})


def test_the_check_of_the_embedding_model_names_the_server(monkeypatch, linux):
    from fw_context_mcp.deps._checks import check_ollama_embed_model

    monkeypatch.setattr(httpx, "get", lambda url, timeout: _Tags([]))

    result = check_ollama_embed_model(_cfg())

    assert result.status == "missing", result
    assert result.fix_cmd == result.instructions == f"OLLAMA_HOST={REMOTE} ollama pull emb:1"


def test_the_check_of_the_chat_model_names_the_server_of_the_chat_calls(monkeypatch, linux):
    from fw_context_mcp.deps._checks import check_ollama_chat_model

    other = "http://chat-box:11434"
    monkeypatch.setattr(httpx, "get", lambda url, timeout: _Tags([]))

    result = check_ollama_chat_model(_cfg(chat_api_base=other))

    assert result.status == "missing", result
    assert result.fix_cmd == f"OLLAMA_HOST={other} ollama pull chat:7b"


def test_a_stalled_pull_names_the_server(monkeypatch):
    from fw_context_mcp.deps import _fixes, _instructions

    monkeypatch.setattr(_instructions, "get_platform_info", lambda: {"system": "linux"})

    def _stalls(*args, **kwargs):
        raise httpx.ReadTimeout("no bytes")

    monkeypatch.setattr(httpx, "stream", _stalls)

    ok, message = _fixes._pull_via_api("emb:1", REMOTE)

    assert not ok
    assert f"run `OLLAMA_HOST={REMOTE} ollama pull emb:1` manually" in message


def test_a_stalled_cli_pull_names_the_server(monkeypatch):
    import io

    from fw_context_mcp.deps import _fixes, _instructions

    monkeypatch.setattr(_instructions, "get_platform_info", lambda: {"system": "linux"})
    # Any time without bytes is a stall: the first loop pass stops the pull.
    monkeypatch.setattr(_fixes, "STALL_TIMEOUT", -1)

    class _Hangs:
        def __init__(self, args, **kwargs):
            self.stdout = io.BytesIO(b"")
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(_fixes.subprocess, "Popen", _Hangs)

    ok, message = _fixes._pull_via_cli("emb:1", REMOTE)

    assert not ok
    assert f"run `OLLAMA_HOST={REMOTE} ollama pull emb:1` manually" in message

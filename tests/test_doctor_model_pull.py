"""``doctor --fix`` pulls a missing model onto the server of ``[llm] ollama_url``.

The model checks of doctor ask that server for the models (`llm._diag.check_setup`).
The pull called the ``ollama`` CLI without ``OLLAMA_HOST``, or the HTTP API
of ``http://localhost:11434``.  With a remote ``ollama_url``, the model went
to the local server, or the pull failed, and the recheck found it missing
again.
"""

from __future__ import annotations

import io

REMOTE = "http://gpu-box:11434"


def _project(tmp_path):
    """A project with a remote ``ollama_url`` in ``local.toml``, the documented place for it."""
    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    (root / ".fw-context" / "local.toml").write_text(
        f'[llm]\nollama_url = "{REMOTE}"\nmodel = "chat:7b"\nembed_model = "emb:1"\n', encoding="utf-8")
    return root


def test_the_cli_pull_asks_the_configured_server(monkeypatch):
    from fw_context_mcp.deps import _fixes

    calls = []

    class _Proc:
        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))
            self.stdout = io.BytesIO(b"")
            self.returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(_fixes.subprocess, "Popen", _Proc)

    assert _fixes._pull_via_cli("emb:1", REMOTE) == (True, "pulled")
    args, kwargs = calls[0]
    assert args == ["ollama", "pull", "emb:1"]
    assert kwargs["env"]["OLLAMA_HOST"] == REMOTE


def test_a_pull_without_the_cli_uses_the_api_of_the_configured_server(monkeypatch):
    from fw_context_mcp.deps import _fixes

    calls = []
    monkeypatch.setattr(_fixes, "which", lambda name: None)
    monkeypatch.setattr(_fixes, "_pull_via_api", lambda model, url: calls.append((model, url)) or (True, "pulled via API"))

    _fixes._ollama_pull("emb:1", REMOTE)

    assert calls == [("emb:1", REMOTE)]


def test_the_fixes_pull_onto_the_server_of_the_project_config(tmp_path, monkeypatch):
    from fw_context_mcp.deps import DepCheckResult, _fixes

    root = _project(tmp_path)
    calls = []
    monkeypatch.setattr(_fixes, "_ollama_pull", lambda model, url: calls.append((model, url)) or (True, "pulled"))

    # The model name comes from the config: fix_cmd is a command for a person.
    _fixes.fix_embed_model(DepCheckResult("embed-model", "missing", "", fix_cmd=f"OLLAMA_HOST={REMOTE} ollama pull x"),
                           str(root))
    _fixes.fix_chat_model(DepCheckResult("chat-model", "missing", "", fix_cmd=f"OLLAMA_HOST={REMOTE} ollama pull y"),
                          str(root))

    assert calls == [("emb:1", REMOTE), ("chat:7b", REMOTE)]

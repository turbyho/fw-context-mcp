"""``load`` stops on a config file that it cannot use, and the MCP server still starts.

``load`` logged a config file that was not valid TOML and ignored it.  Each
setting in the file was lost, the project ID too: the project read as not
initialized, and ``fw-context init`` wrote a new ID over the file.  Now
``load`` raises ``ConfigError``, which names the file.  The MCP server
starts all the same, each tool gives the error, and the ping thread does
not stop, thus the watcher starts when the user corrects the file.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

PID = "0123456789abcdef0123456789abcdef"
VALID = f'[project]\nid = "{PID}"\n'


def _project(tmp_path: Path, config: str, local: str | None = None) -> Path:
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text(config, encoding="utf-8")
    if local is not None:
        (root / ".fw-context" / "local.toml").write_text(local, encoding="utf-8")
    return root


@pytest.mark.parametrize(("config", "local", "name"), [
    (VALID + "[index\nx = 1\n", None, "config.toml"),
    (VALID, "[llm]\nmodel = \n", "local.toml"),
])
def test_load_names_a_project_file_that_is_not_valid_toml(tmp_path, config, local, name):
    from fw_context_mcp.config import ConfigError, load

    root = _project(tmp_path, config, local)

    with pytest.raises(ConfigError, match=rf"{name}: not valid TOML: .*line \d+"):
        load(project_root=root)


def test_load_names_a_global_file_that_is_not_utf8(tmp_path, monkeypatch):
    from fw_context_mcp.config import ConfigError, load, settings

    path = tmp_path / "global.toml"
    path.write_bytes(b'[llm]\nmodel = "caf\xe9"\n')
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", path)

    with pytest.raises(ConfigError, match="global.toml: not UTF-8"):
        load()


def test_load_works_again_after_the_file_is_corrected(tmp_path):
    from fw_context_mcp.config import ConfigError, load

    root = _project(tmp_path, VALID + "[index\n")
    with pytest.raises(ConfigError):
        load(project_root=root)

    (root / ".fw-context" / "config.toml").write_text(VALID, encoding="utf-8")

    assert load(project_root=root).project.id == PID


def test_a_tool_gives_the_error_and_not_an_internal_error(tmp_path):
    from fw_context_mcp.mcp import server

    root = _project(tmp_path, VALID + "[index\n")

    out = asyncio.run(server.mcp.call_tool("lookup_symbol", {"name": "x", "project_root": str(root)}))

    rows = out[1]["result"]
    assert len(rows) == 1 and "Internal server error" not in rows[0]["error"], rows
    assert rows[0]["error"].startswith(f"{root / '.fw-context' / 'config.toml'}: not valid TOML")
    assert rows[0]["error"].endswith("Correct the file, then do the command again.")


def test_the_server_starts_on_a_config_error(tmp_path, monkeypatch):
    """It must not take the path of "not initialized": that path told the caller to run init."""
    import fw_context_mcp.config.settings as settings_mod
    import fw_context_mcp.deps as deps_mod
    import fw_context_mcp.version_check as version_check_mod
    from fw_context_mcp.config import ConfigError
    from fw_context_mcp.mcp import server as server_mod

    root = _project(tmp_path, VALID + "[index\n")
    calls: dict[str, list] = {"run": [], "ping": [], "ensure": []}
    monkeypatch.setattr(version_check_mod, "start_update_check", lambda settings: None)
    monkeypatch.setattr(settings_mod, "load_update_settings", lambda: settings_mod.UpdatesConfig(check=False))
    monkeypatch.setattr(deps_mod, "run_preflight", lambda: [])
    monkeypatch.setattr(server_mod, "resolve_project_root", lambda arg: root)

    def _not_ready():
        raise ConfigError(root / ".fw-context" / "config.toml", "not valid TOML: x")

    monkeypatch.setattr(server_mod, "_check_server_ready", _not_ready)
    monkeypatch.setattr(server_mod, "_start_ping_thread", calls["ping"].append)
    monkeypatch.setattr(server_mod, "_ensure_daemon_running", calls["ensure"].append)
    monkeypatch.setattr(server_mod.mcp, "run", lambda: calls["run"].append(True))

    server_mod.main()

    assert calls == {"run": [True], "ping": [root], "ensure": []}


def test_the_ping_thread_survives_a_config_error(monkeypatch, tmp_path):
    """One tick with a file that the user edits stopped the thread for the life of the server."""
    from fw_context_mcp.config import ConfigError
    from fw_context_mcp.mcp import daemon as daemon_mod
    from fw_context_mcp.mcp import server as server_mod

    never = threading.Event()
    attempts: list[int] = []
    done = threading.Event()

    def _ping(root):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConfigError(root / "config.toml", "not valid TOML: x")
        done.set()
        never.wait()  # block the daemon thread for good; the interpreter ends it at exit
        return True

    monkeypatch.setattr(daemon_mod, "PING_INTERVAL", 0.001)
    monkeypatch.setattr(daemon_mod, "ping_daemon", _ping)

    server_mod._start_ping_thread(tmp_path)

    assert done.wait(timeout=5.0), "the ping loop stopped on the ConfigError"
    assert len(attempts) == 3


def test_a_url_that_cannot_be_parsed_names_its_file(tmp_path):
    """The ValueError of urlparse named no file, and it stopped the MCP server at its start."""
    from fw_context_mcp.config import ConfigError, load

    root = _project(tmp_path, VALID + '[llm]\nollama_url = "http://[::1"\n',
                    local='[llm]\nollama_url = "http://localhost:11434"\n')

    with pytest.raises(ConfigError, match=r"config\.toml: \[llm\] ollama_url is not a valid URL"):
        load(project_root=root)

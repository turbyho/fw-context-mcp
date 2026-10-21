"""The isolation of the test session (``tests/conftest.py``): no test reaches a server of the operator.

Measured 2026-10-21: the unchanged copy of the global config of the
operator sent the LLM analysis of the system tests to the Ollama of the
operator.  These tests read the config that the session gives to a test.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

import fw_context_mcp.config.settings as settings
from fw_context_mcp.config import load
from tests import conftest

UNREACHABLE = conftest._isolation["unreachable_url"]


@pytest.fixture
def session_config(tmp_path: Path, monkeypatch) -> dict:
    """The config that the session writes from the global config of the operator, written again.

    WHY not the file of the session: a test of the suite can change it.
    """
    target = tmp_path / "config.toml"
    conftest._write_isolated_config(conftest._isolation["global_path"], target, keep_llm_servers=False, url=UNREACHABLE)
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", target)
    settings._config_cache.clear()
    return tomllib.loads(target.read_text(encoding="utf-8"))


def test_the_session_config_names_no_server_of_the_operator(session_config):
    data = session_config
    llm = data["llm"]

    assert "cache_server" not in data and "db_dir" not in data.get("index", {})
    assert llm["analyze_symbols"] is False and llm["embed_profile"] == conftest._TEST_EMBED_PROFILE
    for name, profile in llm["profiles"].items():
        assert profile.get("url", UNREACHABLE) == UNREACHABLE or profile["provider"] == "chatgpt", name
        if profile["provider"] not in ("ollama", "chatgpt"):
            assert profile["api_key_env"] == "", name


def test_each_profile_of_the_session_sends_nothing_to_a_server(session_config, tmp_path: Path):
    """The embeddings too: with an API profile they went to the default http://localhost:11434."""
    for name, profile in session_config["llm"]["profiles"].items():
        project = tmp_path / name
        (project / ".fw-context").mkdir(parents=True)
        (project / ".fw-context" / "local.toml").write_text(f'[llm]\nprofile = "{name}"\n', encoding="utf-8")
        settings._config_cache.clear()

        cfg = load(project)

        assert cfg.llm.ollama_url == UNREACHABLE, name
        assert profile["provider"] == "chatgpt" or cfg.llm.chat_api_base in (None, "", UNREACHABLE), name


@pytest.mark.parametrize("source", ["missing", "not toml"])
def test_without_a_config_of_the_operator_the_session_has_one_of_its_own(tmp_path: Path, source):
    """``load`` would else make the default config, whose Ollama is the one of this machine."""
    path = tmp_path / "source.toml"
    if source == "not toml":
        path.write_text("[llm\n", encoding="utf-8")
    target = tmp_path / "config.toml"

    conftest._write_isolated_config(path, target, keep_llm_servers=False, url=UNREACHABLE)

    llm = tomllib.loads(target.read_text(encoding="utf-8"))["llm"]
    assert {profile["url"] for profile in llm["profiles"].values()} == {UNREACHABLE}


def test_the_copy_for_the_tests_of_ollama_keeps_the_llm_but_not_the_cache_server(tmp_path: Path):
    source = tmp_path / "source.toml"
    source.write_text('[llm]\nprofile = "local"\n[llm.profiles.local]\nprovider = "ollama"\nmodel = "m"\n'
                      'url = "http://gpu-box:11434"\n[cache_server]\nurl = "https://cache.example.test"\n',
                      encoding="utf-8")
    target = tmp_path / "config.toml"

    conftest._write_isolated_config(source, target, keep_llm_servers=True, url=UNREACHABLE)

    data = tomllib.loads(target.read_text(encoding="utf-8"))
    assert data["llm"]["profiles"]["local"]["url"] == "http://gpu-box:11434" and "cache_server" not in data


def test_a_single_profile_without_a_selection_stays_selected(tmp_path: Path):
    """The extra profile of the embeddings must not make the only profile ambiguous: a ConfigError for each test."""
    source = tmp_path / "source.toml"
    source.write_text('[llm.profiles.mine]\nprovider = "ollama"\nmodel = "m"\n', encoding="utf-8")
    target = tmp_path / "config.toml"

    conftest._write_isolated_config(source, target, keep_llm_servers=False, url=UNREACHABLE)

    assert tomllib.loads(target.read_text(encoding="utf-8"))["llm"]["profile"] == "mine"


def test_the_marker_of_the_session_is_set():
    """The processes of the session are found by it (``_stop_leftover_processes``)."""
    assert os.environ[conftest._OPERATOR_HOME_ENV]


def _isolated(tmp_path: Path, text: str) -> dict:
    source, target = tmp_path / "source.toml", tmp_path / "config.toml"
    source.write_text(text, encoding="utf-8")
    conftest._write_isolated_config(source, target, keep_llm_servers=False, url=UNREACHABLE)
    return tomllib.loads(target.read_text(encoding="utf-8"))


def test_a_selected_chatgpt_profile_gives_the_selection_to_an_unreachable_ollama(tmp_path: Path):
    """Else each load names api.openai.com, and the tests depend on the selection of the operator."""
    llm = _isolated(tmp_path, '[llm]\nprofile = "plan"\n[llm.profiles.plan]\nprovider = "chatgpt"\nmodel = "m"\n')["llm"]

    assert llm["profile"] == conftest._TEST_EMBED_PROFILE and llm["profiles"]["plan"] == {"provider": "chatgpt",
                                                                                           "model": "m"}


def test_the_key_variable_of_an_api_is_off(tmp_path: Path):
    """The shell of the operator can hold the key: it must not go in a header, also not to a closed port."""
    llm = _isolated(tmp_path, '[llm]\nprofile = "api"\n[llm.profiles.api]\nprovider = "openai_compatible"\n'
                              'model = "m"\nurl = "https://api.example.test/v1"\napi_key_env = "MY_KEY"\n')["llm"]

    assert (llm["profiles"]["api"]["url"], llm["profiles"]["api"]["api_key_env"]) == (UNREACHABLE, "")


def test_a_config_that_is_not_utf8_gives_a_config_of_its_own(tmp_path: Path):
    source, target = tmp_path / "source.toml", tmp_path / "config.toml"
    source.write_bytes(b"[llm]\nprofile = \"\xff\"\n")

    conftest._write_isolated_config(source, target, keep_llm_servers=False, url=UNREACHABLE)

    assert tomllib.loads(target.read_text(encoding="utf-8"))["llm"]["profiles"]["local"]["url"] == UNREACHABLE


def test_the_unreachable_url_refuses_the_connection():
    """Not a port with a listener that never answers: each request would wait for its timeout."""
    import socket
    from urllib.parse import urlparse

    url = urlparse(UNREACHABLE)
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection((url.hostname, url.port), timeout=2).close()


def test_operator_environ_refuses_a_name_that_the_isolation_does_not_set():
    with pytest.raises(KeyError, match="FW_CONTEXT_INDEX_DRI"), conftest.operator_environ("FW_CONTEXT_INDEX_DRI"):
        pass

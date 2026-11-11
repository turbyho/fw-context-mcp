"""The key store of the API keys of the LLM profiles (``llm.secrets``).

The order is the environment variable of the profile, the keyring, then
``secrets.toml`` (mode 0600).  A key must never reach a message, and the
tests never touch the keyring of the operator: the session sets a
backend that fails each call (conftest), and a test of the keyring sets
a backend in memory.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import httpx
import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail
from keyring.errors import PasswordDeleteError

from fw_context_mcp import net
from fw_context_mcp.config.settings import LLMConfig
from fw_context_mcp.llm import secrets

KEY = "sk-test-" + "value"


class MemoryKeyring(KeyringBackend):
    """A keyring in memory, for one test."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.entries[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.entries.pop((service, username), None) is None:
            raise PasswordDeleteError("no such entry")


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> Path:
    """A global directory of its own, an empty cache, and the backend that fails each call."""
    monkeypatch.setenv("FW_CONTEXT_HOME", str(tmp_path))
    monkeypatch.setattr(secrets, "_found", {})
    monkeypatch.setattr(secrets, "_told", set())
    keyring.set_keyring(fail.Keyring())
    yield tmp_path
    keyring.set_keyring(fail.Keyring())


@pytest.fixture
def memory(store) -> MemoryKeyring:
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    return backend


def test_the_keyring_gets_the_key_and_the_file_stays_empty(memory, store):
    assert secrets.set_api_key("work", KEY) == "keyring"

    assert memory.entries == {(secrets.SERVICE, "work"): KEY}
    assert not (store / secrets.SECRETS_FILE).exists()
    assert secrets.get_api_key("work") == KEY


def test_without_a_keyring_the_key_goes_to_a_file_of_mode_0600(store):
    assert secrets.set_api_key("work", KEY) == "file"

    path = store / secrets.SECRETS_FILE
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert secrets.get_api_key("work") == KEY


def test_the_environment_variable_wins_over_the_stored_key(memory, monkeypatch):
    secrets.set_api_key("work", KEY)
    monkeypatch.setenv("WORK_KEY", "from-env")

    assert secrets.get_api_key("work", "WORK_KEY") == "from-env"
    assert secrets.api_key_source("work", "WORK_KEY") == "env:WORK_KEY"


def test_a_key_in_the_keyring_removes_the_key_of_the_file(memory, store, monkeypatch):
    """Else the two places held two keys, and a removal left one of them."""
    keyring.set_keyring(fail.Keyring())
    secrets.set_api_key("work", "old-key")
    keyring.set_keyring(memory)

    secrets.set_api_key("work", KEY)

    assert "old-key" not in (store / secrets.SECRETS_FILE).read_text(encoding="utf-8")
    assert secrets.get_api_key("work") == KEY


def test_delete_removes_the_key_from_each_place(memory, store):
    keyring.set_keyring(fail.Keyring())
    secrets.set_api_key("work", "file-key")
    keyring.set_keyring(memory)
    memory.set_password(secrets.SERVICE, "work", KEY)

    assert sorted(secrets.delete_api_key("work")) == ["file", "keyring"]

    assert secrets.api_key_source("work") is None


def test_a_missing_key_gives_one_notice_without_a_value(store, capsys):
    """Before, the first sign was an HTTP 401 in the middle of an index run."""
    assert secrets.get_api_key("work", "WORK_KEY") is None
    assert secrets.get_api_key("work", "WORK_KEY") is None

    err = capsys.readouterr().err
    assert err.count("LLM profile 'work'") == 1, err
    assert "WORK_KEY" in err and "fw-context llm key set work" in err


def test_a_key_stored_later_applies_without_a_restart(store):
    """Only a found key is cached: the MCP server must see a key that the user stores later."""
    assert secrets.get_api_key("work") is None

    secrets.set_api_key("work", KEY)

    assert secrets.get_api_key("work") == KEY


def test_a_file_that_cannot_be_written_is_an_error_without_the_key(store, monkeypatch):
    def _refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(secrets, "write_text_atomic", _refuse)

    with pytest.raises(secrets.SecretStoreError) as error:
        secrets.set_api_key("work", KEY)

    assert KEY not in str(error.value) and "no keyring" in str(error.value)


# ── The chat client ──


def _client(**fields):
    from fw_context_mcp.llm.chat_router import OpenAIChatClient

    cfg = LLMConfig(chat_api_base="https://api.example.test/v1", chat_api_format="openai", model="m", **fields)
    return OpenAIChatClient(cfg, "https://api.example.test/v1/chat/completions")


def test_the_client_sends_the_stored_key_of_its_profile(memory, monkeypatch):
    secrets.set_api_key("work", KEY)
    sent: list[dict] = []

    def _post(url, json, headers, timeout):
        sent.append(headers)
        request = httpx.Request("POST", url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}, request=request)

    monkeypatch.setattr(net, "post", _post)

    assert _client(profile="work").generate("hi").text == "OK"

    assert sent[0]["Authorization"] == f"Bearer {KEY}"


def test_a_refused_key_names_the_command_and_not_the_text_of_the_server(store, monkeypatch):
    """Some servers repeat a part of the key that they refused."""

    def _post(url, json, headers, timeout):
        request = httpx.Request("POST", url)
        return httpx.Response(401, text=f"invalid key {KEY[:10]}", request=request)

    monkeypatch.setattr(net, "post", _post)

    from fw_context_mcp.llm.errors import ChatApiError

    with pytest.raises(ChatApiError) as error:
        _client(profile="work", api_key_env="WORK_KEY").generate("hi")

    message = str(error.value)
    assert "fw-context llm key set work" in message and "WORK_KEY" in message, message
    assert KEY[:10] not in message


# ── fw-context llm key ──


def _key_command(name: str, profile: str | None, monkeypatch, stdin: str = "") -> int:
    import argparse
    import io

    from fw_context_mcp.cli import _llm

    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    return getattr(_llm, f"cmd_llm_key_{name}")(argparse.Namespace(profile=profile))


def test_the_key_command_stores_a_key_from_stdin_and_shows_only_its_place(store, monkeypatch, capsys):
    """stdin, not an argument: the process list and the shell history show an argument."""
    assert _key_command("set", "work", monkeypatch, stdin=KEY + "\n") == 0
    assert secrets.stored_api_key("work") == KEY

    assert _key_command("status", "work", monkeypatch) == 0
    assert _key_command("delete", "work", monkeypatch) == 0
    assert secrets.stored_api_key("work") is None

    out = capsys.readouterr()
    assert KEY not in out.out + out.err
    assert "work: secrets.toml" in out.out, out.out


def test_the_key_command_refuses_an_empty_key_and_a_name_that_is_not_valid(store, monkeypatch):
    assert _key_command("set", "work", monkeypatch, stdin="\n") == 1
    assert _key_command("set", "bad name", monkeypatch, stdin=KEY) == 1
    assert not (store / secrets.SECRETS_FILE).exists()


# ── Keys that an HTTP header cannot hold, backends that fail, a file that is not valid ──


def test_a_trailing_newline_of_a_variable_is_removed(store, monkeypatch):
    """A variable from a .env file: httpx raised an error whose text held the key."""
    monkeypatch.setenv("WORK_KEY", KEY + "\n")

    assert secrets.get_api_key("work", "WORK_KEY") == KEY


@pytest.mark.parametrize("bad", [KEY + " x", KEY + "\u200b", KEY + "\x07"])
def test_a_key_that_a_header_cannot_hold_is_refused_without_its_value(store, monkeypatch, caplog, bad):
    with pytest.raises(ValueError) as error:
        secrets.set_api_key("work", bad)
    assert KEY not in str(error.value)

    monkeypatch.setenv("WORK_KEY", bad)
    assert secrets.get_api_key("work", "WORK_KEY") is None
    assert KEY not in caplog.text and "cannot hold" in caplog.text


class BrokenBusKeyring(MemoryKeyring):
    """A Secret Service whose D-Bus socket went away: keyring 25 lets FileNotFoundError pass."""

    def get_password(self, service: str, username: str) -> str | None:
        raise FileNotFoundError(2, "No such file or directory")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise FileNotFoundError(2, "No such file or directory")


def test_a_backend_error_that_is_not_a_keyring_error_uses_the_file(store):
    keyring.set_keyring(BrokenBusKeyring())

    assert secrets.set_api_key("work", KEY) == "file"
    secrets.forget("work")
    assert secrets.get_api_key("work") == KEY


def test_a_file_that_is_not_valid_toml_is_not_written_over(store):
    """A write of the keys that were read removed each other key of the file."""
    path = store / secrets.SECRETS_FILE
    path.write_text('[api_keys]\nother = "kept\n', encoding="utf-8")

    with pytest.raises(secrets.SecretStoreError) as error:
        secrets.set_api_key("work", KEY)

    assert path.read_text(encoding="utf-8") == '[api_keys]\nother = "kept\n'
    assert KEY not in str(error.value)


def test_a_refused_key_is_read_again_after_a_change_in_another_process(store, monkeypatch):
    """The cache kept the old key of a running MCP server until a restart."""
    secrets.set_api_key("work", "old-key")
    assert secrets.get_api_key("work") == "old-key"
    # Another process stores a new key: this process does not see the write.
    secrets._file_save({"work": KEY})
    sent: list[str] = []

    def _post(url, json, headers, timeout):
        sent.append(headers["Authorization"])
        request = httpx.Request("POST", url)
        if headers["Authorization"].endswith("old-key"):
            return httpx.Response(401, text="no", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}, request=request)

    monkeypatch.setattr(net, "post", _post)
    from fw_context_mcp.llm.errors import ChatApiError

    with pytest.raises(ChatApiError):
        _client(profile="work").generate("hi")
    assert _client(profile="work").generate("hi").text == "OK"


def test_a_refused_key_of_the_caller_gives_no_advice_about_the_key_store(store, monkeypatch):
    def _post(url, json, headers, timeout):
        return httpx.Response(401, text="no", request=httpx.Request("POST", url))

    monkeypatch.setattr(net, "post", _post)
    from fw_context_mcp.llm.errors import ChatApiError

    with pytest.raises(ChatApiError) as error:
        _client(profile="work", chat_api_key=KEY).generate("hi")

    assert "llm key set" not in str(error.value) and KEY not in str(error.value)


def test_the_repr_of_the_config_does_not_show_the_key():
    assert KEY not in repr(LLMConfig(chat_api_key=KEY))


def test_a_profile_without_a_key_uses_the_key_of_its_provider(store):
    """Two profiles of OpenRouter use one account: the key is stored one time, as "provider:openrouter"."""
    secrets.set_api_key(secrets.provider_entry("openrouter"), KEY)

    assert secrets.get_api_key("or-oss", "", "openrouter") == KEY
    assert secrets.api_key_source("or-oss", "", "openrouter") == "file:openrouter"


def test_the_key_of_the_profile_wins_over_the_key_of_the_provider(store):
    secrets.set_api_key(secrets.provider_entry("openrouter"), "provider-key")
    secrets.set_api_key("or-oss", KEY)

    assert secrets.get_api_key("or-oss", "", "openrouter") == KEY


def test_a_new_key_of_the_provider_applies_after_a_refusal(store):
    """forget clears each cached key: a profile can hold the key of its provider."""
    secrets.set_api_key(secrets.provider_entry("openrouter"), "old-key")
    assert secrets.get_api_key("or-oss", "", "openrouter") == "old-key"
    secrets._file_save({secrets.provider_entry("openrouter"): KEY})

    secrets.forget("or-oss")

    assert secrets.get_api_key("or-oss", "", "openrouter") == KEY

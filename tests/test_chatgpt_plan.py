"""The provider chatgpt: the sign-in, the refresh of the tokens and the Responses API of the ChatGPT plan.

No test sends a request out: httpx is replaced, and the sign-in callback
goes to the loopback server of the test itself.  The ID token is signed
with a key of the test, and its JWKS client is replaced.
"""

from __future__ import annotations

import json
import stat
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from fw_context_mcp.config.settings import LLMConfig
from fw_context_mcp.llm import chatgpt_plan as plan
from fw_context_mcp.llm.errors import ChatApiError, LLMStopRun

TOKEN = "access-" + "token-1"
REFRESH = "refresh-" + "token-1"


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FW_CONTEXT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def signing_key(monkeypatch):
    """A key of the test, and a JWKS client that gives it: _validate_id_token checks a real signature."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class _Jwks:
        def __init__(self, *args, **kwargs):
            pass

        def get_signing_key_from_jwt(self, token):
            return type("Key", (), {"key": key.public_key()})()

    monkeypatch.setattr(jwt, "PyJWKClient", _Jwks)
    return key


def _id_token(key, client_id: str, nonce: str | None, sub: str = "user-1") -> str:
    now = int(time.time())
    claims = {"iss": plan.ISSUER, "aud": client_id, "sub": sub, "iat": now, "exp": now + 600,
              "email": "dev@example.test"}
    if nonce is not None:
        claims["nonce"] = nonce
    return jwt.encode(claims, key, algorithm="RS256")


def _signed_in(profile: str = "gpt", *, expires_in: float = 3600) -> None:
    plan.save_credentials(profile, plan.PlanCredentials(
        client_id="client-1", host_id="urn:uuid:host", subject="user-1", email="dev@example.test",
        id_token="id", access_token=TOKEN, refresh_token=REFRESH, expires_at=time.time() + expires_in,
        scopes=plan.SCOPE.split(),
    ))


# ── The sign-in ──


def test_the_sign_in_stores_validated_credentials_with_mode_0600(home, signing_key, monkeypatch):
    posted: list[dict] = []

    def _post(url, data, timeout):
        posted.append(data)
        return httpx.Response(200, json={
            "access_token": TOKEN, "refresh_token": REFRESH, "expires_in": 3600, "scope": plan.SCOPE,
            "id_token": _id_token(signing_key, "client-1", nonce_holder["nonce"]),
        }, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", _post)
    nonce_holder: dict[str, str] = {}
    shown_urls: list[str] = []

    def _open(url: str, shown: str) -> None:
        shown_urls.append(shown)
        query = {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}
        nonce_holder["nonce"] = query["nonce"]
        callback = f"{query['redirect_uri']}?code=c1&state={query['state']}&client_id=client-1"
        threading.Thread(target=lambda: httpx.Client().get(callback, timeout=10), daemon=True).start()

    credentials = plan.login("gpt", open_url=_open, timeout_s=20)

    assert (credentials.client_id, credentials.subject, credentials.email) == ("client-1", "user-1", "dev@example.test")
    assert posted[0]["grant_type"] == "authorization_code" and posted[0]["client_id"] == "client-1"
    path = plan.credentials_path("gpt")
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "agent_name_hint=fw-context" in shown_urls[0]


def test_a_wrong_nonce_is_refused(home, signing_key):
    with pytest.raises(plan.LoginError, match="nonce"):
        plan._validate_id_token(_id_token(signing_key, "client-1", "other"), "client-1", "expected")


def test_a_token_of_another_audience_is_refused(home, signing_key):
    with pytest.raises(plan.LoginError, match="not valid"):
        plan._validate_id_token(_id_token(signing_key, "client-2", None), "client-1", None)


# ── The refresh ──


def test_an_expired_access_token_is_refreshed_and_the_new_refresh_token_is_stored(home, monkeypatch):
    _signed_in(expires_in=-10)
    sent: list[dict] = []

    def _post(url, data, timeout):
        sent.append(data)
        return httpx.Response(200, json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", _post)

    assert plan.access_token("gpt") == "access-2"

    assert sent[0] == {"grant_type": "refresh_token", "client_id": "client-1", "refresh_token": REFRESH,
                       "resource": plan.API_BASE}
    assert plan.load_credentials("gpt").refresh_token == "refresh-2"


def test_a_valid_access_token_is_not_refreshed(home, monkeypatch):
    _signed_in()
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("no refresh"))

    assert plan.access_token("gpt") == TOKEN


def test_a_reused_refresh_token_ends_the_sign_in_and_keeps_the_client(home, monkeypatch):
    _signed_in(expires_in=-10)
    monkeypatch.setattr(httpx, "post", lambda url, data, timeout: httpx.Response(
        400, json={"error": "refresh_token_reused"}, request=httpx.Request("POST", url)))

    with pytest.raises(plan.PlanAuthError, match="fw-context llm login gpt"):
        plan.access_token("gpt")

    credentials = plan.load_credentials("gpt")
    assert credentials.client_id == "client-1" and not credentials.signed_in()


def test_a_network_failure_of_the_refresh_keeps_the_tokens(home, monkeypatch):
    _signed_in(expires_in=-10)

    def _fail(*args, **kwargs):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "post", _fail)

    with pytest.raises(plan.LLMError) as error:
        plan.access_token("gpt")

    assert not isinstance(error.value, plan.PlanAuthError)
    assert plan.load_credentials("gpt").refresh_token == REFRESH


def test_a_profile_without_a_sign_in_stops_the_run(home):
    with pytest.raises(LLMStopRun, match="fw-context llm login gpt"):
        plan.access_token("gpt")


# ── The Responses API ──


def _events(*events: dict) -> bytes:
    return b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events)


COMPLETED = {"type": "response.completed", "response": {"usage": {
    "input_tokens": 12, "input_tokens_details": {"cached_tokens": 4}, "output_tokens": 3,
    "output_tokens_details": {"reasoning_tokens": 0}}}}


def _serve(monkeypatch, *answers: tuple[int, bytes]) -> list[dict]:
    """Answer each stream with the next of *answers*; return the bodies and headers that were sent."""
    sent: list[dict] = []
    queue = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({"body": json.loads(request.content), "auth": request.headers.get("Authorization")})
        status, body = queue.pop(0)
        return httpx.Response(status, stream=httpx.ByteStream(body))

    def _stream(method, url, **kwargs):
        kwargs.pop("timeout", None)
        return httpx.Client(transport=httpx.MockTransport(handler)).stream(method, url, **kwargs)

    monkeypatch.setattr(httpx, "stream", _stream)
    return sent


def _client():
    return plan.ChatGPTPlanClient(LLMConfig(profile="gpt", provider="chatgpt", model="gpt-6-luna"))


def test_a_request_streams_the_text_and_reads_the_usage(home, monkeypatch):
    _signed_in()
    sent = _serve(monkeypatch, (200, _events({"type": "response.output_text.delta", "delta": "O"},
                                             {"type": "response.output_text.delta", "delta": "K"}, COMPLETED)))

    result = _client().generate("hi", temperature=0.1, max_tokens=50)

    assert result.text == "OK"
    assert (result.usage.prompt_tokens, result.usage.cached_tokens, result.usage.completion_tokens) == (12, 4, 3)
    body = sent[0]["body"]
    assert (body["store"], body["stream"], body["reasoning"]) == (False, True, {"effort": "none"})
    assert "temperature" not in body and "max_output_tokens" not in body
    assert sent[0]["auth"] == f"Bearer {TOKEN}"


def test_the_usage_limit_of_the_plan_stops_the_run(home, monkeypatch):
    _signed_in()
    _serve(monkeypatch, (200, _events({"type": "response.failed", "response": {
        "error": {"code": "subscription_sharing_usage_limit_exceeded"}}})))

    with pytest.raises(plan.PlanLimitError, match="ChatGPT Settings"):
        _client().generate("hi")


def test_a_401_refreshes_the_token_and_sends_again(home, monkeypatch):
    _signed_in()
    monkeypatch.setattr(httpx, "post", lambda url, data, timeout: httpx.Response(
        200, json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600},
        request=httpx.Request("POST", url)))
    sent = _serve(monkeypatch, (401, b'{"error": {"code": "subscription_sharing_invalid_user"}}'),
                  (200, _events(COMPLETED)))

    _client().generate("hi")

    assert [entry["auth"] for entry in sent] == [f"Bearer {TOKEN}", "Bearer access-2"]


def test_a_refused_reasoning_field_is_left_out(home, monkeypatch):
    _signed_in()
    refusal = b'{"error": {"code": "subscription_sharing_unsupported_capability", "param": "reasoning"}}'
    sent = _serve(monkeypatch, (400, refusal), (200, _events(COMPLETED)), (200, _events(COMPLETED)))
    client = _client()

    client.generate("one")
    client.generate("two")

    assert ["reasoning" in entry["body"] for entry in sent] == [True, False, False]


def test_a_refusal_that_a_retry_does_not_repair_stops_the_run(home, monkeypatch):
    """Each next request has the same body: before, each symbol failed three times, with a pause of 2 s."""
    _signed_in()
    _serve(monkeypatch, (400, b'{"error": {"code": "subscription_sharing_unsupported_capability", "param": "tools"}}'))

    with pytest.raises(LLMStopRun, match="param tools"):
        _client().generate("hi")


def test_a_403_of_the_admission_stops_the_run(home, monkeypatch):
    _signed_in()
    _serve(monkeypatch, (403, b'{"detail": "region not supported"}'))

    with pytest.raises(LLMStopRun, match="region not supported"):
        _client().generate("hi")


def test_a_refusal_of_the_reasoning_effort_leaves_the_field_out(home, monkeypatch):
    """The Responses API names the nested field: reasoning.effort."""
    _signed_in()
    refusal = b'{"error": {"code": "subscription_sharing_unsupported_capability", "param": "reasoning.effort"}}'
    sent = _serve(monkeypatch, (400, refusal), (200, _events(COMPLETED)))

    _client().generate("hi")

    assert ["reasoning" in entry["body"] for entry in sent] == [True, False]


def test_an_event_of_two_data_lines_is_one_event(home, monkeypatch):
    """Before, each data line was one event: the event was lost, and the answer was empty."""
    _signed_in()
    stream = (b'data: {"type": "response.output_text.delta",\ndata: "delta": "OK"}\n\n'
              + _events(COMPLETED))
    _serve(monkeypatch, (200, stream))

    assert _client().generate("hi").text == "OK"


def test_no_message_holds_a_token(home, monkeypatch):
    _signed_in()
    _serve(monkeypatch, (500, b"boom"))

    with pytest.raises(ChatApiError) as error:
        _client().generate("hi")

    assert TOKEN not in str(error.value) and REFRESH not in str(error.value)


def test_the_profile_resolves_to_the_plan_client(home, tmp_path, monkeypatch):
    from fw_context_mcp.config import settings
    from fw_context_mcp.llm.client import get_chat_client

    path = tmp_path / "config.toml"
    path.write_text('[llm]\nprofile = "gpt"\nembed_model = "e:1b"\n\n[llm.profiles.gpt]\nprovider = "chatgpt"\n'
                    'model = "gpt-6-luna"\n', encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", path)
    settings._config_cache.clear()

    llm = settings.load(None).llm

    assert isinstance(get_chat_client(llm), plan.ChatGPTPlanClient)
    assert llm.chat_api_base == plan.API_BASE


# ── The refresh: the rotated token is never lost ──


def _refresh_answer(monkeypatch, status: int, body: dict | str) -> None:
    def _post(url, data, timeout):
        if isinstance(body, dict):
            return httpx.Response(status, json=body, request=httpx.Request("POST", url))
        return httpx.Response(status, text=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", _post)


def test_the_rotated_token_is_stored_when_the_keys_cannot_be_fetched(home, monkeypatch):
    """Before, a JWKS failure kept the old refresh token: the next refresh got refresh_token_reused."""
    _signed_in(expires_in=-10)
    _refresh_answer(monkeypatch, 200, {"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600,
                                       "id_token": "x.y.z"})

    def _down(*args, **kwargs):
        raise jwt.PyJWKClientConnectionError("down")

    monkeypatch.setattr(jwt, "PyJWKClient", lambda *a, **k: type("C", (), {"get_signing_key_from_jwt": _down})())

    assert plan.access_token("gpt") == "access-2"
    assert plan.load_credentials("gpt").refresh_token == "refresh-2"


def test_an_answer_without_an_access_token_keeps_the_tokens(home, monkeypatch):
    _signed_in(expires_in=-10)
    _refresh_answer(monkeypatch, 200, {"token_type": "Bearer"})

    with pytest.raises(plan.LLMError, match="without an access token"):
        plan.access_token("gpt")

    assert plan.load_credentials("gpt").refresh_token == REFRESH


@pytest.mark.parametrize(("status", "code"), [(400, "temporarily_unavailable"), (401, "server_error"), (503, "x")])
def test_a_refresh_error_that_can_pass_keeps_the_tokens(home, monkeypatch, status, code):
    """Only the terminal codes of errors-and-recovery remove the sign-in."""
    _signed_in(expires_in=-10)
    _refresh_answer(monkeypatch, status, {"error": code})

    with pytest.raises(plan.LLMError) as error:
        plan.access_token("gpt")

    assert not isinstance(error.value, plan.PlanAuthError)
    assert plan.load_credentials("gpt").refresh_token == REFRESH


def test_an_invalid_client_stops_the_run_and_keeps_the_tokens(home, monkeypatch):
    _signed_in(expires_in=-10)
    _refresh_answer(monkeypatch, 401, {"error": "invalid_client"})

    with pytest.raises(plan.PlanAuthError, match="logout"):
        plan.access_token("gpt")

    assert plan.load_credentials("gpt").refresh_token == REFRESH


def test_a_refresh_with_the_id_token_of_another_account_removes_the_sign_in(home, signing_key, monkeypatch):
    _signed_in(expires_in=-10)
    _refresh_answer(monkeypatch, 200, {"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600,
                                       "id_token": _id_token(signing_key, "client-1", None, sub="someone-else")})

    with pytest.raises(plan.PlanAuthError, match="another ChatGPT account"):
        plan.access_token("gpt")

    assert not plan.load_credentials("gpt").signed_in()


# ── The sign-in: the callback server ──


def _browser(callbacks, *, before=None):
    """Return an open_url that sends *callbacks* (functions of the query) to the loopback server, in order."""
    seen: dict[str, str] = {}

    def _open(url: str, shown: str) -> None:
        query = {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}
        seen.update(query)

        def _send():
            if before is not None:
                before(query)
            for make in callbacks:
                try:
                    httpx.Client().get(make(query), timeout=15)
                except httpx.HTTPError:
                    pass

        threading.Thread(target=_send, daemon=True).start()

    return _open, seen


def _token_endpoint(monkeypatch, signing_key, seen, *, scope=plan.SCOPE, sub="user-1"):
    revoked: list[dict] = []

    def _post(url, data, timeout):
        if url == plan.REVOKE_URL:
            revoked.append(data)
            return httpx.Response(200, request=httpx.Request("POST", url))
        return httpx.Response(200, json={
            "access_token": "access-new", "refresh_token": "refresh-new", "expires_in": 3600, "scope": scope,
            "id_token": _id_token(signing_key, "client-1", seen["nonce"], sub=sub),
        }, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", _post)
    return revoked


def _callback(query, *, state=None, client_id="client-1"):
    return f"{query['redirect_uri']}?code=c1&state={state or query['state']}&client_id={client_id}"


def test_a_callback_with_another_state_does_not_end_the_sign_in(home, signing_key, monkeypatch):
    """A web page can send a request to the loopback port: before, it ended the sign-in."""
    open_url, seen = _browser([lambda q: _callback(q, state="forged"), _callback])
    _token_endpoint(monkeypatch, signing_key, seen)

    assert plan.login("gpt", open_url=open_url, timeout_s=20).access_token == "access-new"


def test_a_silent_connection_does_not_hold_the_sign_in(home, signing_key, monkeypatch):
    """A connection that sent nothing held the server past the deadline."""
    import socket

    sockets: list[socket.socket] = []

    def _silent(query):
        port = int(urlparse(query["redirect_uri"]).port)
        sockets.append(socket.create_connection(("127.0.0.1", port)))

    open_url, seen = _browser([_callback], before=_silent)
    _token_endpoint(monkeypatch, signing_key, seen)

    started = time.monotonic()
    assert plan.login("gpt", open_url=open_url, timeout_s=30).access_token == "access-new"
    assert time.monotonic() - started < 25
    for sock in sockets:
        sock.close()


def test_a_callback_with_another_client_is_refused(home, signing_key, monkeypatch):
    """sign-in: a client ID that differs from the saved one is rejected."""
    _signed_in()
    open_url, seen = _browser([lambda q: _callback(q, client_id="client-OTHER")])
    _token_endpoint(monkeypatch, signing_key, seen)

    with pytest.raises(plan.LoginError, match="another client ID"):
        plan.login("gpt", open_url=open_url, timeout_s=20)

    assert plan.load_credentials("gpt").refresh_token == REFRESH


def test_a_sign_in_without_the_plan_scope_keeps_the_old_tokens_and_revokes_the_new(home, signing_key, monkeypatch):
    _signed_in()
    open_url, seen = _browser([_callback])
    revoked = _token_endpoint(monkeypatch, signing_key, seen, scope="openid profile email offline_access")

    with pytest.raises(plan.LoginError, match=plan.PLAN_SCOPE):
        plan.login("gpt", open_url=open_url, timeout_s=20)

    assert plan.load_credentials("gpt").refresh_token == REFRESH
    assert revoked and revoked[0]["token"] == "refresh-new"


def test_a_prompt_that_does_not_fit_the_context_is_a_prompt_that_is_too_long(home, monkeypatch):
    """The analysis marks such a symbol at once: the same request fails again at each run."""
    from fw_context_mcp.llm.errors import ChatPromptTooLong

    _signed_in()
    sent = _serve(monkeypatch, (400, b'{"error": {"code": "context_length_exceeded", "param": "input"}}'))

    with pytest.raises(ChatPromptTooLong, match=r"the prompt does not fit the context of 'gpt-6-luna' \(context_length_exceeded\)"):
        _client().generate("hi")

    assert len(sent) == 1

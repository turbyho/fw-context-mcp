"""The provider ``chatgpt``: the chat of an LLM profile on the ChatGPT plan of the user ("Sign in with ChatGPT").

The open-source direct flow of OpenAI (developers.openai.com/siwc,
read 2026-10-20) bills the requests to the ChatGPT plan of the user, not
to an API key:

* Sign-in: OAuth 2 with PKCE in the browser, a loopback callback on
  ``127.0.0.1``, dynamic registration (``client_id=dynamic_agent_client``
  the first time, then the issued client ID), and the scope
  ``chatgpt.tokens.use.direct``.  The ID token is checked against the JWKS
  of OpenAI (signature, issuer, audience, expiry, nonce).
* Tokens: the access token lives one hour, the refresh token 30 days, and
  each refresh gives a new refresh token.  Thus a refresh holds a file
  lock: two processes (an index run and the MCP server) must not refresh
  one token at the same time.
* Inference: the Responses API, ``store: false`` and ``stream: true`` on
  each request; ``response.completed`` is the only sign of success.

The tokens are in ``<global directory>/auth/chatgpt-<profile>.json``, mode
0600, and not in the keyring: the record is about 4 KB, and the Windows
Credential Manager takes about 2.5 KB.  No function writes a token to a
log, an exception or a message.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import secrets as token_random
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .. import net
from ..file_lock import lock, unlock
from ..utils import fw_context_home, is_fatal, write_text_atomic
from .client import ChatResult, Usage
from .errors import ChatApiError, ChatPromptTooLong, LLMError, LLMStopRun

if TYPE_CHECKING:
    from ..config.settings import LLMConfig

log = logging.getLogger(__name__)

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
REVOKE_URL = f"{ISSUER}/api/accounts/oauth/revoke"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
API_BASE = "https://api.openai.com/v1"
RESPONSES_URL = f"{API_BASE}/responses"
MODELS_URL = f"{API_BASE}/models"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
#: The scope that lets the requests use the plan; a sign-in without it is not usable.
PLAN_SCOPE = "chatgpt.tokens.use.direct"
#: The first port of the loopback callback; only the port can change.
CALLBACK_PORT = 1455
#: A refresh starts this many seconds before the access token expires.
REFRESH_MARGIN_S = 120
#: The refresh errors after which only a new sign-in helps (errors-and-recovery).
_TERMINAL_REFRESH_ERRORS = frozenset({
    "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
    "refresh_token_invalidated", "refresh_token_reused",
})
_USAGE_URL_HINT = "ChatGPT Settings → Usage"


class PlanAuthError(LLMStopRun):
    """The profile is not signed in, or the sign-in is no longer valid: ``fw-context llm login <profile>``."""


class PlanLimitError(LLMStopRun):
    """The plan refuses requests now (its usage limit): each request of the run would fail the same way."""


class LoginError(RuntimeError):
    """The sign-in in the browser did not give usable credentials."""


@dataclass
class PlanCredentials:
    """The sign-in of one profile.

    ``client_id`` and ``host_id`` stay after a sign-out: the next sign-in
    of the same account reuses them (profiles-and-sessions).
    """

    client_id: str
    host_id: str
    subject: str = ""
    email: str = ""
    id_token: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0
    scopes: list[str] = field(default_factory=list)

    def signed_in(self) -> bool:
        return bool(self.refresh_token and self.access_token)


# ── The credential file ──


def credentials_path(profile: str) -> Path:
    """Return the path of the credentials of *profile* (read at each call: tests move the global directory)."""
    return fw_context_home() / "auth" / f"chatgpt-{profile}.json"


def load_credentials(profile: str) -> PlanCredentials | None:
    """Return the credentials of *profile*, or None when the file is missing or not usable."""
    path = credentials_path(profile)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("%s cannot be read: %s", path, type(exc).__name__)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("client_id"), str):
        return None
    known = {name for name in PlanCredentials.__dataclass_fields__}
    return PlanCredentials(**{key: value for key, value in data.items() if key in known})


def save_credentials(profile: str, credentials: PlanCredentials) -> None:
    """Write the credentials of *profile* atomically, with mode 0600 in a directory of mode 0700."""
    path = credentials_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    write_text_atomic(path, json.dumps(asdict(credentials), indent=1), mode=0o600)


@contextlib.contextmanager
def _refresh_lock(profile: str) -> Iterator[None]:
    """Hold an exclusive lock of the credentials of *profile* across processes.

    The refresh token rotates: a second process that refreshed the old one
    at the same time made the sign-in unusable (``refresh_token_reused``).
    :mod:`fw_context_mcp.file_lock` (``flock`` on Linux and macOS,
    ``LockFileEx`` on Windows); the kernel releases the lock when the process
    ends.
    """
    path = credentials_path(profile).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        lock(handle.fileno())
        try:
            yield
        finally:
            unlock(handle.fileno())


# ── The ID token ──


#: The clock difference that the check of an ID token accepts (iat, exp).
_CLOCK_LEEWAY_S = 60


def _validate_id_token(id_token: str, client_id: str, nonce: str | None, *, subject: str | None = None) -> dict:
    """Return the claims of *id_token* after the check of its signature (JWKS), issuer, audience, expiry and nonce.

    *subject*, when given, must be the ``sub`` of the token: a refresh must
    not give the identity of another account.

    Raises:
        LoginError: a check failed.  The message names the check, not the token.
        _KeysUnavailableError: the keys of OpenAI cannot be fetched (network):
            the token is not known to be wrong.
    """
    import jwt

    try:
        signing_key = jwt.PyJWKClient(JWKS_URL, timeout=30).get_signing_key_from_jwt(id_token)
    except jwt.PyJWKClientConnectionError:
        raise _KeysUnavailableError("the keys of OpenAI cannot be fetched") from None
    except jwt.PyJWTError as exc:
        raise LoginError(f"the ID token of the sign-in is not valid ({type(exc).__name__})") from None
    try:
        claims = jwt.decode(
            id_token, signing_key.key, algorithms=["RS256"], audience=client_id, issuer=ISSUER,
            leeway=_CLOCK_LEEWAY_S, options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise LoginError(f"the ID token of the sign-in is not valid ({type(exc).__name__})") from None
    if nonce is not None and claims.get("nonce") != nonce:
        raise LoginError("the ID token of the sign-in has another nonce: the sign-in is refused")
    if subject is not None and claims.get("sub") != subject:
        raise LoginError("the ID token belongs to another ChatGPT account")
    return claims


class _KeysUnavailableError(Exception):
    """The JWKS of OpenAI cannot be fetched now."""


def _revoke(client_id: str, refresh_token: str) -> bool:
    """Revoke *refresh_token*; return True when OpenAI confirms it (HTTP 200, also for a token that is not valid)."""
    try:
        response = net.post(REVOKE_URL, data={
            "token": refresh_token, "token_type_hint": "refresh_token", "client_id": client_id,
        }, timeout=30)
    except httpx.HTTPError as exc:
        log.warning("The revocation of the ChatGPT sign-in failed: %s", type(exc).__name__)
        return False
    return response.status_code == 200


# ── Sign-in, refresh, sign-out ──


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class _CallbackServer(HTTPServer):
    """The loopback server of the sign-in; ``result`` gets the query of the callback with ``expected_state``."""

    result: dict[str, str]
    expected_state: str


def _callback_server() -> _CallbackServer:
    """Return an HTTP server on 127.0.0.1, port 1455 when it is free, else a free port."""
    try:
        server = _CallbackServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
    except OSError:
        server = _CallbackServer(("127.0.0.1", 0), _CallbackHandler)
    server.result = {}
    server.expected_state = ""
    return server


class _CallbackHandler(BaseHTTPRequestHandler):
    """Take the query of ``/auth/callback`` with the expected state into ``server.result``.

    A query with another state gets HTTP 400, and the server waits on.
    WHY: a web page can send a request to the loopback port (an image
    tag); before, its request ended the sign-in.  ``timeout``: a
    connection that sends nothing held the server past the deadline.
    """

    timeout = 10

    def do_GET(self) -> None:  # noqa: N802 - the name of BaseHTTPRequestHandler
        url = urlparse(self.path)
        if url.path != "/auth/callback":
            self.send_response(404)
            self.end_headers()
            return
        server = cast(_CallbackServer, self.server)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        if query.get("state") != server.expected_state:
            self.send_response(400)
            self.end_headers()
            return
        server.result = query
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"fw-context: the sign-in is received. You can close this tab.")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the name of the base class
        """Write nothing: the query holds the authorization code."""


def login(profile: str, *, open_url: Callable[[str, str], None], timeout_s: float = 600.0) -> PlanCredentials:
    """Sign *profile* in with ChatGPT in the browser, and store the credentials.

    *open_url* gets the URL for the browser and a URL to show to the user;
    the second one has no ``id_token_hint`` (the documentation: keep it out
    of logs and of the diagnostics).  A profile signed in before keeps its
    client ID, and must sign in with the same account.

    Raises:
        LoginError: no callback in *timeout_s*, the user refused, the
            ID token is not valid, another account, or the plan scope is
            missing.
        OSError: the credentials cannot be written.
    """
    old = load_credentials(profile)
    host_id = old.host_id if old else f"urn:uuid:{uuid.uuid4()}"
    client_id = old.client_id if old else "dynamic_agent_client"
    state, nonce, verifier = (token_random.token_urlsafe(24), token_random.token_urlsafe(24),
                              token_random.token_urlsafe(48))
    server = _callback_server()
    server.expected_state = state
    redirect = f"http://127.0.0.1:{server.server_address[1]}/auth/callback"
    params = {
        "response_type": "code", "client_id": client_id, "ext_agent_host_id": host_id, "redirect_uri": redirect,
        "scope": SCOPE, "resource": API_BASE, "state": state, "nonce": nonce,
        "code_challenge_method": "S256", "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
    }
    if old is None:
        params["agent_name_hint"] = "fw-context"
    shown = f"{AUTHORIZE_URL}?{urlencode(params)}"
    if old is not None and old.id_token:
        params["id_token_hint"] = old.id_token
    if old is not None and old.email:
        params["login_hint"] = old.email
    open_url(f"{AUTHORIZE_URL}?{urlencode(params)}", shown)

    server.timeout = 1
    deadline = time.monotonic() + timeout_s
    try:
        while not server.result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    result = server.result
    if not result:
        raise LoginError(f"no answer of the browser in {timeout_s:.0f} s")
    if "error" in result:
        raise LoginError(f"the sign-in was refused: {result.get('error')} {result.get('error_description', '')}".strip())
    if old is not None and result.get("client_id") and result["client_id"] != old.client_id:
        # sign-in: "If it is present and differs from the saved ID, reject the result."
        raise LoginError("the answer of the browser has another client ID than the profile: the sign-in is refused")
    issued_client = result.get("client_id") or (old.client_id if old else "")
    if not issued_client or issued_client == "dynamic_agent_client":
        raise LoginError("the answer of the browser has no client ID: the registration is not complete")
    if old is None:
        # The registration is made: the next sign-in reuses it, also when
        # the code exchange below fails.
        with _refresh_lock(profile):
            save_credentials(profile, PlanCredentials(client_id=issued_client, host_id=host_id))

    response = net.post(TOKEN_URL, data={
        "grant_type": "authorization_code", "client_id": issued_client, "code": result.get("code", ""),
        "code_verifier": verifier, "redirect_uri": redirect, "resource": API_BASE,
    }, timeout=60)
    if response.status_code != 200:
        raise LoginError(f"the code exchange failed: HTTP {response.status_code} ({_error_code(response)})")
    try:
        tokens = response.json()
        claims = _validate_id_token(tokens.get("id_token", ""), issued_client, nonce)
    except ValueError:
        raise LoginError("the code exchange gave an answer that is not JSON") from None
    except _KeysUnavailableError as exc:
        raise LoginError(f"{exc}: try the sign-in again") from None
    if old is not None and old.subject and claims["sub"] != old.subject:
        raise LoginError(
            f"the browser signed in another ChatGPT account than profile {profile!r} had: "
            f"run 'fw-context llm logout {profile}' first, or make a new profile"
        )
    scopes = str(tokens.get("scope", "")).split()
    credentials = PlanCredentials(
        client_id=issued_client, host_id=host_id, subject=str(claims["sub"]), email=str(claims.get("email", "")),
        id_token=tokens["id_token"], access_token=tokens.get("access_token", ""),
        refresh_token=tokens.get("refresh_token", ""), expires_at=time.time() + float(tokens.get("expires_in", 3600)),
        scopes=scopes,
    )
    if PLAN_SCOPE not in scopes:
        # The new tokens cannot use the plan: revoke them, and keep the
        # sign-in that the profile had (its tokens can still be valid).
        if credentials.refresh_token:
            _revoke(issued_client, credentials.refresh_token)
        raise LoginError(
            f"the sign-in did not grant {PLAN_SCOPE}: the plan of this account cannot be used by an app. "
            f"See {_USAGE_URL_HINT}"
        )
    if not credentials.refresh_token:
        raise LoginError("the sign-in gave no refresh token (offline_access): the profile cannot stay signed in")
    # Under the lock: a refresh of another process must not write its
    # older tokens over the new sign-in.
    with _refresh_lock(profile):
        save_credentials(profile, credentials)
    return credentials


def access_token(profile: str, *, force_refresh: bool = False) -> str:
    """Return a valid access token of *profile*; refresh it under the lock when it expires soon.

    Raises:
        PlanAuthError: the profile is not signed in, or the refresh token
            is not valid any more (the tokens are removed, the client ID
            stays).
        LLMError: the refresh failed for a reason that can pass (network,
            HTTP 5xx): the tokens stay.
    """
    with _refresh_lock(profile):
        credentials = load_credentials(profile)
        if credentials is None or not credentials.signed_in():
            raise PlanAuthError(f"LLM profile {profile!r} is not signed in: run 'fw-context llm login {profile}'")
        if not force_refresh and credentials.expires_at - REFRESH_MARGIN_S > time.time():
            return credentials.access_token
        try:
            response = net.post(TOKEN_URL, data={
                "grant_type": "refresh_token", "client_id": credentials.client_id,
                "refresh_token": credentials.refresh_token, "resource": API_BASE,
            }, timeout=60)
        except httpx.HTTPError as exc:
            raise LLMError(f"the refresh of the ChatGPT sign-in failed: {type(exc).__name__}") from None
        if response.status_code == 200:
            return _store_refresh(profile, credentials, response)
        code = _error_code(response)
        if code in _TERMINAL_REFRESH_ERRORS:
            _clear_tokens(profile, credentials)
            raise PlanAuthError(
                f"the ChatGPT sign-in of LLM profile {profile!r} is not valid any more ({code}): "
                f"run 'fw-context llm login {profile}'"
            )
        if code == "invalid_client":
            # A problem of the registration, not of the tokens: they stay.
            raise PlanAuthError(f"OpenAI refuses the client of LLM profile {profile!r} ({code}): "
                                f"run 'fw-context llm logout {profile}', then 'fw-context llm login {profile}'")
        # Another error can pass (errors-and-recovery: do not delete the
        # credentials over a temporary failure).
        raise LLMError(f"the refresh of the ChatGPT sign-in failed: HTTP {response.status_code} ({code})")


def _store_refresh(profile: str, credentials: PlanCredentials, response: httpx.Response) -> str:
    """Store the tokens of a successful refresh at once, then check its ID token; return the access token.

    WHY the order: the server has rotated the refresh token.  A failure
    before the write (the JWKS fetch, a missing field) kept the old one,
    and the next refresh got ``refresh_token_reused`` and lost the sign-in.
    """
    try:
        tokens = response.json()
        access = str(tokens["access_token"])
    except (ValueError, KeyError, TypeError):
        # Nothing to store: the answer holds no usable token.
        raise LLMError("the refresh of the ChatGPT sign-in gave an answer without an access token") from None
    credentials.access_token = access
    credentials.refresh_token = str(tokens.get("refresh_token") or credentials.refresh_token)
    credentials.expires_at = time.time() + float(tokens.get("expires_in", 3600))
    if tokens.get("scope"):
        credentials.scopes = str(tokens["scope"]).split()
    save_credentials(profile, credentials)
    id_token = tokens.get("id_token")
    if not id_token:
        return access
    try:
        _validate_id_token(str(id_token), credentials.client_id, None, subject=credentials.subject or None)
    except _KeysUnavailableError:
        log.warning("The ID token of the ChatGPT refresh is not checked: the keys of OpenAI cannot be fetched")
        return access
    except LoginError as exc:
        _clear_tokens(profile, credentials)
        raise PlanAuthError(f"the refresh of LLM profile {profile!r} gave {exc}: the sign-in is removed. "
                            f"Run 'fw-context llm login {profile}'") from None
    credentials.id_token = str(id_token)
    save_credentials(profile, credentials)
    return access


def _clear_tokens(profile: str, credentials: PlanCredentials) -> None:
    """Remove the tokens of *profile*; keep the client ID, the host ID and the account for the next sign-in."""
    save_credentials(profile, PlanCredentials(client_id=credentials.client_id, host_id=credentials.host_id,
                                              subject=credentials.subject, email=credentials.email))


def logout(profile: str) -> bool:
    """Revoke the refresh token of *profile* and remove its tokens; return True when the revocation is confirmed.

    The client ID and the host ID stay for the next sign-in.  A revocation
    that is not confirmed (network) still removes the tokens: the caller
    tells the user to disconnect the app in ChatGPT Settings.
    """
    with _refresh_lock(profile):
        credentials = load_credentials(profile)
        if credentials is None:
            return True
        confirmed = _revoke(credentials.client_id, credentials.refresh_token) if credentials.refresh_token else True
        _clear_tokens(profile, credentials)
        return confirmed


def list_models(profile: str) -> list[tuple[str, str]]:
    """Return ``(slug, display name)`` of each model that the plan of *profile* lists, in the order of the server.

    Raises:
        PlanAuthError: the profile is not signed in.
        LLMError: the request failed.
    """
    return [(str(model["slug"]), str(model.get("display_name") or model["slug"])) for model in _listed_models(profile)]


def model_contexts(profile: str) -> dict[str, int | None]:
    """Return the context of each model that the plan of *profile* lists (``context_window``), None when it gives none.

    Measured 2026-10-22: the list gives ``context_window`` for each model
    (gpt-6-luna 272000), and ``max_context_window`` (872000), the context
    with an option that fw-context does not send.

    Raises:
        PlanAuthError: the profile is not signed in.
        LLMError: the request failed.
    """
    from .model_list import tokens

    return {str(model["slug"]): tokens(model.get("context_window")) for model in _listed_models(profile)}


def _listed_models(profile: str) -> list[dict]:
    """Return the entries of the model list of the plan that it lists for a user (``visibility`` "list")."""
    token = access_token(profile)
    try:
        response = net.get(MODELS_URL, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LLMError(f"the model list of the ChatGPT plan failed: {type(exc).__name__}") from None
    # An answer of another form raised AttributeError or TypeError, which
    # the callers (reindex_file among them) do not catch.
    models = body.get("models") if isinstance(body, dict) else None
    if not isinstance(models, list):
        raise LLMError("the model list of the ChatGPT plan failed: an answer without a list of models")
    return [model for model in models
            if isinstance(model, dict) and model.get("visibility") == "list" and model.get("slug")]


def _error_code(response: httpx.Response) -> str:
    """Return the error code of an error answer (``error``, ``error.code`` or ``detail``), never more of the body."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if not isinstance(body, dict):
        return f"HTTP {response.status_code}"
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or error.get("type") or f"HTTP {response.status_code}")
    if isinstance(error, str):
        return error
    return str(body.get("detail") or f"HTTP {response.status_code}")


# ── The chat client ──


class ChatGPTPlanClient:
    """The chat of a profile of provider ``chatgpt``: the Responses API, billed to the ChatGPT plan.

    No temperature and no answer limit go with a request: the
    documentation of the flow names only ``model``, ``input``, ``store``
    and ``stream``, and refuses an input that it does not support
    (``subscription_sharing_unsupported_capability``).  The reasoning is
    asked at ``effort: "none"`` (accepted in the measurement of
    2026-10-20); a refusal of it removes the field for the client.
    """

    counts_tokens = False

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._reasoning = True

    def generate(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
    ) -> ChatResult:
        """Send *prompt* and return the answer; *temperature*, *max_tokens* and *num_ctx* are not sent.

        WHY no ``max_output_tokens``: the terms of "Sign in with ChatGPT"
        for an open-source app name the fields of a request, and that field
        is not one of them (research 2026-10-20).  A probe of 2026-10-22
        showed that the plan takes it; the terms decide, not the probe.
        Thus the API checks only that the prompt fits the context of the
        model; the context of the analysis comes from the model list.

        Raises:
            PlanAuthError: the profile is not signed in.
            PlanLimitError: the usage limit of the plan.
            LLMStopRun: a refusal that each next request gets too.
            LLMError: a failure that can pass (``ChatApiError``, or a
                refresh that failed on the network).
        """
        result = self._request(prompt)
        if self.cfg.debug_log:
            from ._debug import _write_debug_log

            _write_debug_log(self.cfg.debug_log, {
                "ts": datetime.now(UTC).isoformat(), "model": self.cfg.model, "endpoint": RESPONSES_URL,
                "prompt": prompt, "response": result.text, "streamed": True,
            })
        return result

    def _request(self, prompt: str) -> ChatResult:
        refreshed = False
        while True:
            token = access_token(self.cfg.profile, force_refresh=refreshed)
            body: dict[str, object] = {
                "model": self.cfg.model, "input": [{"role": "user", "content": prompt}], "store": False, "stream": True,
            }
            if self._reasoning:
                body["reasoning"] = {"effort": "none"}
            try:
                return self._stream(token, body)
            except _RefusalError as refusal:
                if refusal.status_code == 401 and not refreshed:
                    refreshed = True
                    continue
                if (refusal.param == "reasoning" or refusal.param.startswith("reasoning.")) and self._reasoning:
                    with self._lock:
                        self._reasoning = False
                    continue
                raise refusal.error from None

    def _stream(self, token: str, body: dict[str, object]) -> ChatResult:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        chunks: list[str] = []
        try:
            with net.stream("POST", RESPONSES_URL, headers=headers, json=body, timeout=self.cfg.timeout) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    raise self._refusal(resp.status_code, _parse_error(resp))
                for event in _sse_events(resp.iter_lines()):
                    kind = event.get("type")
                    if kind == "response.output_text.delta":
                        chunks.append(str(event.get("delta", "")))
                    elif kind == "response.completed":
                        return ChatResult("".join(chunks), _plan_usage(event.get("response", {}).get("usage")))
                    elif kind in ("response.failed", "error"):
                        error = event.get("response", {}).get("error") if kind == "response.failed" else event
                        raise self._refusal(200, error if isinstance(error, dict) else {})
                    elif kind == "response.incomplete":
                        reason = (event.get("response", {}).get("incomplete_details") or {}).get("reason", "unknown")
                        raise _RefusalError(ChatApiError(f"the ChatGPT plan answer is not complete ({reason})"))
        except (_RefusalError, LLMError):
            raise
        except httpx.TimeoutException:
            raise ChatApiError(f"the ChatGPT plan request timed out after {self.cfg.timeout:.0f}s") from None
        except Exception as exc:
            if is_fatal(exc):
                raise
            raise ChatApiError(f"the ChatGPT plan request failed: {type(exc).__name__}") from None
        raise ChatApiError("the ChatGPT plan stream ended without response.completed")

    def _refusal(self, status_code: int, error: dict) -> _RefusalError:
        """Return the refusal for the HTTP status and the error object of an answer (errors-and-recovery)."""
        code = str(error.get("code") or error.get("detail") or f"HTTP {status_code}")
        param = str(error.get("param") or "")
        profile = self.cfg.profile
        if code == "context_length_exceeded":
            # The code of the Responses API for an input that does not fit
            # the context of the model (reports of the API, 2026-10-22; not
            # probed: a probe needs a prompt of more than 272k tokens).
            return _RefusalError(ChatPromptTooLong(
                f"the ChatGPT plan refused the request: the prompt does not fit the context of '{self.cfg.model}' "
                f"({code})", status_code=status_code,
            ))
        if code == "subscription_sharing_usage_limit_exceeded":
            return _RefusalError(PlanLimitError(
                f"the ChatGPT plan of LLM profile {profile!r} reached a usage limit ({code}): the run stops. "
                f"See {_USAGE_URL_HINT}; a later run continues"
            ))
        if code in ("subscription_sharing_user_not_eligible", "chatpass_v2_scope_not_authorized",
                    "chatpass_v2_invalid_authorization_context"):
            return _RefusalError(PlanAuthError(f"the ChatGPT plan of LLM profile {profile!r} refuses this app ({code})"))
        if status_code == 401:
            return _RefusalError(PlanAuthError(
                f"the ChatGPT plan refused the sign-in of LLM profile {profile!r} ({code}): "
                f"run 'fw-context llm login {profile}'"
            ), status_code=401)
        if status_code == 403 or code in ("subscription_sharing_unsupported_capability",
                                          "subscription_sharing_route_not_supported"):
            # Each next request has the same body or the same policy: a
            # retry does not repair it (a region, a field of the request).
            # The caller removes a refused reasoning field first.
            return _RefusalError(LLMStopRun(
                f"the ChatGPT plan refused the requests of LLM profile {profile!r}: {code}"
                + (f" (param {param})" if param else "")
            ), status_code=status_code, param=param)
        return _RefusalError(ChatApiError(f"the ChatGPT plan refused the request: {code}"
                                     + (f" (param {param})" if param else ""), status_code=status_code), param=param)

    def count_tokens(self, prompt: str, num_ctx: int) -> int:
        """The Responses API of the plan gives no count of the tokens of a prompt.

        Raises:
            ChatApiError: Always.
        """
        raise ChatApiError("the ChatGPT plan gives no count of the tokens of a prompt")

    def trained_context(self) -> int | None:
        """None: this client counts no tokens.  The analysis reads the context from ``llm.model_list``."""
        return None

    def fit_context(self, lower: int, upper: int, *, check: bool) -> None:
        """None: the plan has no GPU of fw-context, thus its context is not a question of memory."""
        return None

    def pull_model(self) -> None:
        """A ChatGPT plan cannot install a model.

        Raises:
            ChatApiError: Always.
        """
        raise ChatApiError("a ChatGPT plan has no pull")


class _RefusalError(Exception):
    """A refusal of the API, with the error to raise when no retry helps."""

    def __init__(self, error: LLMError, *, status_code: int = 0, param: str = "") -> None:
        super().__init__(str(error))
        self.error = error
        self.status_code = status_code
        self.param = param


def _sse_events(lines: Iterator[str]) -> Iterator[dict]:
    """Yield the JSON object of each server-sent event of *lines*.

    The data lines of one event join with a newline, and an empty line
    ends the event (the SSE format).  WHY: before, each data line was one
    event, thus an event of two lines was lost, and the answer was empty.
    A data part that is not a JSON object is skipped.
    """
    data: list[str] = []
    for line in [*lines, ""]:
        if line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
            continue
        if line or not data:
            continue
        text, data = "\n".join(data), []
        if text.strip() == "[DONE]":
            continue
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _parse_error(resp: httpx.Response) -> dict:
    """Return the error object of an error answer: ``{"error": {...}}``, or ``{"detail": "..."}`` of an admission."""
    try:
        body = resp.json()
    except ValueError:
        return {}
    if not isinstance(body, dict):
        return {}
    error = body.get("error")
    return error if isinstance(error, dict) else body


def _plan_usage(usage: object) -> Usage:
    """Return the token counts of ``response.completed`` (``input_tokens``, ``output_tokens`` and their details)."""
    if not isinstance(usage, dict):
        return Usage()

    def number(obj: object, key: str) -> int | None:
        value = obj.get(key) if isinstance(obj, dict) else None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return Usage(
        prompt_tokens=number(usage, "input_tokens"),
        cached_tokens=number(usage.get("input_tokens_details"), "cached_tokens"),
        completion_tokens=number(usage, "output_tokens"),
        reasoning_tokens=number(usage.get("output_tokens_details"), "reasoning_tokens"),
    )

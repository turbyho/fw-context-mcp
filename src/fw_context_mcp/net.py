"""Outgoing HTTP of fw-context: ``get``, ``post``, ``stream`` and ``client``, as in ``httpx``.

Each HTTP request of fw-context goes through this module, never through
``httpx.get``, ``httpx.post``, ``httpx.stream`` or ``httpx.Client`` directly
(``tests/test_net.py`` holds that rule).  The functions have the arguments
of the ``httpx`` functions that fw-context uses, and they do what ``httpx``
does, with one difference: a request to a loopback host goes through a
transport that connects with ``loopback.create_connection``.

WHY: on Windows a refused loopback connect needed 2 s, and each request to
``http://localhost:11434`` waited 2 s for ``::1`` before it tried
``127.0.0.1``, where Ollama listens (``fw_context_mcp.loopback`` gives the
measurements).  ``fw-context init`` without Ollama needed 39 s, and 36.5 s
of that were 9 refused connects.  With the transport: 3.9 s.

The client selects the transport for each request (``mounts`` of
``httpx.Client``), thus a redirect to another host gets the transport of
that host.  The hosts of the transport: ``localhost``, ``127.0.0.1``,
``::1``, and the host of the URL of ``client`` when it is a loopback
address.

WHY the transport also on Linux and macOS: only the socket option is
Windows only (``loopback``).  One code path on all systems lets the tests
of each system run the transport.

WHY no proxy for a loopback host: a mount of the client replaces the
proxies of the environment (``HTTP_PROXY``) for its host.  A proxy cannot
forward a request for a loopback address: the request gets to the
loopback interface of the proxy, not of this computer.

WHY the private parts of ``httpx`` and ``httpcore`` (``HTTPTransport._pool``,
``httpcore._backends.sync.SyncStream``): ``httpx`` gives no argument for
the network backend of a transport, and ``httpcore.SyncBackend`` sets the
socket options only after the connect.  ``pyproject.toml`` limits the two
packages to the versions that have these parts, and ``tests/test_net.py``
makes requests through the transport.
"""

from __future__ import annotations

import functools
import socket
import ssl
import typing
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlparse

import httpcore
import httpx
from httpcore._backends.sync import SyncStream

from .config.settings import _is_loopback_url
from .loopback import create_connection

#: ``httpx.get``, ``httpx.post`` and ``httpx.stream`` use this timeout when the
#: caller gives none.
DEFAULT_TIMEOUT = httpx.Timeout(5.0)

#: The limits of an ``httpx.Client`` when the caller gives none.
DEFAULT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0)

#: The loopback hosts that each client sends through the loopback transport.
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")

#: The argument types that the callers of fw-context give.  The ``httpx``
#: names of these types are in its private module ``httpx._types``.
Timeout = float | httpx.Timeout | None
Params = typing.Mapping[str, str | int | float | bool | None]
Headers = typing.Mapping[str, str]
FormData = typing.Mapping[str, str]


class _LoopbackBackend(httpcore.NetworkBackend):
    """The network backend of ``httpcore`` with ``loopback.create_connection``.

    The rest is as in ``httpcore.SyncBackend``: the socket options and
    ``TCP_NODELAY`` after the connect, and an ``OSError`` as
    ``httpcore.ConnectError``.
    """

    def __init__(self) -> None:
        self._sync = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        source_address = None if local_address is None else (local_address, 0)
        try:
            sock = create_connection((host, port), timeout, source_address=source_address)
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout(exc) from exc
        except OSError as exc:
            raise httpcore.ConnectError(exc) from exc
        try:
            for option in socket_options or ():
                sock.setsockopt(*option)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            sock.close()
            raise httpcore.ConnectError(exc) from exc
        return SyncStream(sock)

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        return self._sync.connect_unix_socket(path, timeout, socket_options)

    def sleep(self, seconds: float) -> None:
        self._sync.sleep(seconds)


@functools.cache
def _ssl_context(trust_env: bool) -> ssl.SSLContext:
    """The SSL context of the loopback transport, made one time in each process.

    WHY one time: each request of ``get``, ``post`` and ``stream`` makes a
    new client, and each new context loads the CA bundle again.  A context
    can serve each connection of the process.
    """
    return httpx.create_ssl_context(trust_env=trust_env)


class _LoopbackTransport(httpx.HTTPTransport):
    """``httpx.HTTPTransport`` whose connection pool connects through ``_LoopbackBackend``."""

    def __init__(self, *, trust_env: bool, limits: httpx.Limits) -> None:
        context = _ssl_context(trust_env)
        super().__init__(verify=context, trust_env=trust_env, limits=limits)
        self._pool = httpcore.ConnectionPool(
            ssl_context=context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            network_backend=_LoopbackBackend(),
        )


def _loopback_mount_keys(url: str) -> list[str]:
    """The ``mounts`` keys of the loopback hosts, with the host of *url* when it is a loopback address."""
    hosts = list(LOOPBACK_HOSTS)
    if _is_loopback_url(url):
        host = (urlparse(url).hostname or "").lower()
        hosts.append(f"[{host}]" if ":" in host else host)
    return [f"all://{host}" for host in dict.fromkeys(hosts)]


def client(
    url: str,
    *,
    base_url: str = "",
    headers: Headers | None = None,
    timeout: Timeout = DEFAULT_TIMEOUT,
    trust_env: bool = True,
    limits: httpx.Limits = DEFAULT_LIMITS,
) -> httpx.Client:
    """Return an ``httpx.Client`` whose requests to a loopback host go through the loopback transport.

    *url* is the first URL that the client will use; when its host is a
    loopback address that ``LOOPBACK_HOSTS`` does not name (for example
    ``127.0.0.2``), that host gets the transport too.  Give *base_url* when
    the requests of the client use relative paths.
    """
    transport = _LoopbackTransport(trust_env=trust_env, limits=limits)
    return httpx.Client(
        base_url=base_url, headers=headers, timeout=timeout, trust_env=trust_env, limits=limits,
        mounts={key: transport for key in _loopback_mount_keys(url)},
    )


def request(
    method: str,
    url: str,
    *,
    params: Params | None = None,
    content: bytes | str | None = None,
    data: FormData | None = None,
    json: typing.Any | None = None,
    headers: Headers | None = None,
    timeout: Timeout = DEFAULT_TIMEOUT,
    follow_redirects: bool = False,
) -> httpx.Response:
    """Send one request, as ``httpx.request`` does."""
    with client(url, timeout=timeout) as http:
        return http.request(
            method, url, params=params, content=content, data=data, json=json, headers=headers,
            follow_redirects=follow_redirects,
        )


def get(
    url: str,
    *,
    params: Params | None = None,
    headers: Headers | None = None,
    timeout: Timeout = DEFAULT_TIMEOUT,
    follow_redirects: bool = False,
) -> httpx.Response:
    """Send a GET request, as ``httpx.get`` does."""
    return request("GET", url, params=params, headers=headers, timeout=timeout, follow_redirects=follow_redirects)


def post(
    url: str,
    *,
    content: bytes | str | None = None,
    data: FormData | None = None,
    json: typing.Any | None = None,
    params: Params | None = None,
    headers: Headers | None = None,
    timeout: Timeout = DEFAULT_TIMEOUT,
    follow_redirects: bool = False,
) -> httpx.Response:
    """Send a POST request, as ``httpx.post`` does."""
    return request(
        "POST", url, content=content, data=data, json=json, params=params, headers=headers,
        timeout=timeout, follow_redirects=follow_redirects,
    )


@contextmanager
def stream(
    method: str,
    url: str,
    *,
    params: Params | None = None,
    content: bytes | str | None = None,
    data: FormData | None = None,
    json: typing.Any | None = None,
    headers: Headers | None = None,
    timeout: Timeout = DEFAULT_TIMEOUT,
    follow_redirects: bool = False,
) -> Iterator[httpx.Response]:
    """Stream the response of one request, as ``httpx.stream`` does."""
    with client(url, timeout=timeout) as http, http.stream(
        method, url, params=params, content=content, data=data, json=json, headers=headers,
        follow_redirects=follow_redirects,
    ) as response:
        yield response

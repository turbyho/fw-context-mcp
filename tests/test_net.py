"""fw_context_mcp.net: the outgoing HTTP of fw-context.

The module docstring gives the defect: on Windows a refused loopback
connect needed 2 s, and each request to ``http://localhost:11434`` waited
2 s for ``::1`` before it tried ``127.0.0.1``.  The timing tests run on
Windows only, because Linux and macOS fail a refused connect at once.  The
other tests run on each system: the transport of a loopback host is the
same there.
"""

from __future__ import annotations

import ast
import socket
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpcore
import httpx
import pytest

from fw_context_mcp import loopback, net

SRC = Path(__file__).resolve().parent.parent / "src" / "fw_context_mcp"

#: On Windows a refused connect without the socket option needs 2 s.
FAST_S = 1.0


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — the name that http.server calls
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — the signature of http.server
        pass


@pytest.fixture
def ipv4_server() -> Iterator[int]:
    """An HTTP server on 127.0.0.1 only, as Ollama listens; yield its port."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _free_port() -> int:
    """A loopback port with no listener: a connect to it is refused."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def connects(monkeypatch) -> list[tuple[str, int]]:
    """Record each connect of the loopback transport."""
    seen: list[tuple[str, int]] = []
    real = net.create_connection

    def spy(address, timeout=None, source_address=None):
        seen.append(address)
        return real(address, timeout, source_address=source_address)

    monkeypatch.setattr(net, "create_connection", spy)
    return seen


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_a_loopback_request_goes_through_the_loopback_transport(ipv4_server, connects, host):
    """Also a test of the httpx version: the transport replaces the pool of HTTPTransport."""
    response = net.get(f"http://{host}:{ipv4_server}/")

    assert (response.status_code, response.text) == (200, "ok")
    assert connects == [(host, ipv4_server)]


def test_a_streamed_loopback_request_goes_through_the_loopback_transport(ipv4_server, connects):
    with net.stream("GET", f"http://127.0.0.1:{ipv4_server}/") as response:
        assert b"".join(response.iter_bytes()) == b"ok"
    assert connects == [("127.0.0.1", ipv4_server)]


def test_a_refused_loopback_connect_is_a_connect_error():
    with pytest.raises(httpx.ConnectError):
        net.get(f"http://127.0.0.1:{_free_port()}/", timeout=10)


def test_a_remote_host_keeps_the_transport_and_the_proxy_of_httpx(monkeypatch):
    """Only a loopback host gets the transport of the module: a remote host keeps HTTP_PROXY."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    with net.client("https://openrouter.ai/api/v1") as remote:
        transport = remote._transport_for_url(httpx.URL("https://openrouter.ai/api/v1/models"))
    assert not isinstance(transport, net._LoopbackTransport)
    assert isinstance(transport._pool, httpcore.HTTPProxy)


def test_a_loopback_host_gets_no_proxy(monkeypatch):
    """A proxy gets to its own loopback interface, never to this computer."""
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    with net.client("http://localhost:11434") as local:
        transport = local._transport_for_url(httpx.URL("http://localhost:11434/api/tags"))
    assert isinstance(transport, net._LoopbackTransport)


def test_a_redirect_to_a_remote_host_leaves_the_loopback_transport():
    """The transport is selected for each request, not for the client."""
    with net.client("http://localhost:11434") as http:
        assert isinstance(http._transport_for_url(httpx.URL("http://localhost:11434/")), net._LoopbackTransport)
        remote = http._transport_for_url(httpx.URL("https://openrouter.ai/api/v1/models"))
    assert not isinstance(remote, net._LoopbackTransport)


def test_a_loopback_host_of_the_url_that_the_list_does_not_name_gets_the_transport():
    with net.client("http://127.0.0.2:8080") as http:
        transport = http._transport_for_url(httpx.URL("http://127.0.0.2:8080/v1/models"))
    assert isinstance(transport, net._LoopbackTransport)


def test_the_first_error_is_raised_when_no_address_connects():
    """As socket.create_connection: the error of the first address of getaddrinfo."""
    port = _free_port()
    with pytest.raises(ConnectionRefusedError):
        loopback.create_connection(("127.0.0.1", port), timeout=10)


def test_an_address_family_that_the_system_cannot_make_goes_to_the_next_address(ipv4_server, monkeypatch):
    """IPv6 off: socket(AF_INET6) fails, and localhost must still reach 127.0.0.1."""
    real_socket = socket.socket

    def no_ipv6(family=socket.AF_INET, *args, **kwargs):
        if family == socket.AF_INET6:
            raise OSError(97, "Address family not supported by protocol")
        return real_socket(family, *args, **kwargs)

    v6_first = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", ipv4_server, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", ipv4_server)),
    ]
    monkeypatch.setattr(loopback.socket, "getaddrinfo", lambda *a, **kw: v6_first)
    monkeypatch.setattr(loopback.socket, "socket", no_ipv6)

    with loopback.create_connection(("localhost", ipv4_server), timeout=10) as sock:
        assert sock.getpeername()[:2] == ("127.0.0.1", ipv4_server)


def test_a_failed_socket_option_is_logged_one_time_and_the_connect_goes_on(ipv4_server, monkeypatch, caplog):
    """Each request makes a new socket: thousands of the same warning would fill the log."""
    def refuse(sock):
        raise OSError(10045, "The attempted operation is not supported for the type of object referenced")

    monkeypatch.setattr(loopback, "_no_syn_retransmissions", refuse)
    monkeypatch.setattr(loopback, "_warned", threading.Event())

    with caplog.at_level("WARNING", logger="fw_context_mcp.loopback"):
        for _ in range(3):
            loopback.create_connection(("127.0.0.1", ipv4_server), timeout=10).close()
    assert [r.getMessage()[:45] for r in caplog.records] == ["Could not stop the SYN retransmissions of a l"]


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows retransmits the SYN of a refused loopback connect")
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_a_refused_loopback_connect_fails_at_once_on_windows(host):
    """Measured without the socket option: 2.0 s for 127.0.0.1, 4.1 s for localhost."""
    start = time.perf_counter()
    with pytest.raises(httpx.ConnectError):
        net.get(f"http://{host}:{_free_port()}/", timeout=10)
    assert time.perf_counter() - start < FAST_S


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows retransmits the SYN of a refused loopback connect")
def test_localhost_reaches_an_ipv4_server_at_once_on_windows(ipv4_server):
    """Measured without the socket option: 2.2 s for each request, because ::1 came first."""
    start = time.perf_counter()
    assert net.get(f"http://localhost:{ipv4_server}/").status_code == 200
    assert time.perf_counter() - start < FAST_S


_HTTPX_REQUESTS = {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream", "Client", "AsyncClient"}


def test_each_http_request_of_the_product_goes_through_net():
    """A direct ``httpx.get`` or ``httpx.Client`` has the delay of Windows again (module docstring of net)."""
    found = []
    for path in SRC.rglob("*.py"):
        if path == SRC / "net.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "httpx" and node.attr in _HTTPX_REQUESTS):
                found.append(f"{path.relative_to(SRC)}:{node.lineno} httpx.{node.attr}")
    assert found == []


def test_a_client_loads_the_ca_bundle_no_more(monkeypatch):
    """Each request makes a new client: its default transport and each proxy transport loaded the CA bundle again.

    The loopback transport had the shared context already, but
    ``httpx.Client`` got no ``verify``, thus each other transport made its
    own (about 6 ms for each request).  The fake records each load of CA
    certificates after the shared context exists.
    """
    import ssl

    net._ssl_context(True)  # the one load of the process
    loads: list[object] = []
    real_load = ssl.SSLContext.load_verify_locations

    def counting_load(self, *args, **kwargs):
        loads.append(args or kwargs)
        return real_load(self, *args, **kwargs)

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", counting_load)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    for _ in range(3):
        net.client("https://example.invalid/").close()
    assert loads == []


def test_a_change_of_the_ca_bundle_of_the_environment_gets_a_new_context(monkeypatch, tmp_path):
    """SSL_CERT_FILE selects the CA bundle: the context of the process kept the bundle of the first request.

    The same value gives the same context (one load), a new value a context
    that trusts that bundle: here one CA, the first one of certifi.  A
    context that only differs as an object, and still trusts the bundle of
    certifi, ignored the variable again.
    """
    import certifi

    pem = Path(certifi.where()).read_text(encoding="ascii")
    end = "-----END CERTIFICATE-----"
    bundle = tmp_path / "ca.pem"
    bundle.write_text(pem[: pem.index(end) + len(end)] + "\n", encoding="ascii")
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    default = net._ssl_context(True)
    assert net._ssl_context(True) is default
    assert len(default.get_ca_certs()) > 1, "the bundle of certifi"

    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    own = net._ssl_context(True)
    assert own is not default
    assert len(own.get_ca_certs()) == 1, "only the CA of SSL_CERT_FILE"
    assert net._ssl_context(False) is default, "without trust_env the variable does not count"

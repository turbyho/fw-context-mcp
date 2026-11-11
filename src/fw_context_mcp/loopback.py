"""TCP connect to a loopback address, with each SYN sent once on Windows.

Windows connects slowly to a loopback port that has no listener.  The peer
sends RST at once, but the TCP stack of Windows sends the SYN two more
times, and the connect fails only after 2 s.  Measured on Windows 11
(2026-11-09): 2.0 s for ``127.0.0.1``, 4.1 s for ``localhost`` (it tries
``::1`` and then ``127.0.0.1``).  When the server listens on IPv4 only, as
Ollama does, each connect to ``localhost`` waited 2 s for ``::1`` before it
tried IPv4.

``create_connection`` sets ``SIO_TCP_INITIAL_RTO`` with no SYN
retransmissions on each socket before the connect.  A refused connect then
fails at once (measured: 0.0 s), and the change to the next address is
immediate.  Linux and macOS fail a refused connect at once, thus the
option is set on Windows only.

WHY only for a loopback address: a loopback connection has no packet loss,
thus a retransmission of the SYN gives nothing.  For a remote host, the
retransmissions help on a network with packet loss.

The cost: Windows also sends RST when the listen queue of the server is
full, and a retransmitted SYN could then connect.  fw-context limits the
requests to Ollama in each process (``llm.ollama.ollama_guard``: 1, or up
to 4 when the operator sets it), thus a few requests come at a time, far
fewer than the listen queue of a server holds.

WHY a module of its own: the watcher daemon pings over loopback TCP and
sends no HTTP, thus it imports this module and not ``fw_context_mcp.net``
with ``httpx``.
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
import typing

log = logging.getLogger(__name__)

#: Windows: ``_WSAIOW(IOC_VENDOR, 17)``, ``mstcpip.h``.
_SIO_TCP_INITIAL_RTO = 0x98000011
#: ``TCP_INITIAL_RTO_UNSPECIFIED_RTT``: the system keeps its initial RTT.
_UNSPECIFIED_RTT = 0xFFFF
#: ``TCP_INITIAL_RTO_NO_SYN_RETRANSMISSIONS``.
_NO_SYN_RETRANSMISSIONS = 0xFE


def _windows_ioctl() -> typing.Callable[[socket.socket], None] | None:
    """Return the function that stops the SYN retransmissions of a socket, or None outside of Windows."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class _InitialRtoParameters(ctypes.Structure):
        """``TCP_INITIAL_RTO_PARAMETERS`` of ``mstcpip.h``."""

        _fields_ = [("Rtt", ctypes.c_ushort), ("MaxSynRetransmissions", ctypes.c_ubyte)]

    ws2_32 = ctypes.WinDLL("ws2_32", use_last_error=True)
    wsa_ioctl = ws2_32.WSAIoctl
    wsa_ioctl.argtypes = [
        ctypes.c_size_t, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p,
    ]
    wsa_ioctl.restype = ctypes.c_int

    def no_syn_retransmissions(sock: socket.socket) -> None:
        params = _InitialRtoParameters(_UNSPECIFIED_RTT, _NO_SYN_RETRANSMISSIONS)
        returned = wintypes.DWORD()
        if wsa_ioctl(sock.fileno(), _SIO_TCP_INITIAL_RTO, ctypes.byref(params), ctypes.sizeof(params),
                     None, 0, ctypes.byref(returned), None, None) != 0:
            raise ctypes.WinError(ctypes.get_last_error())

    return no_syn_retransmissions


_no_syn_retransmissions = _windows_ioctl()
_warned = threading.Event()


def _send_each_syn_once(sock: socket.socket) -> None:
    """Stop the SYN retransmissions of *sock* on Windows; elsewhere do nothing.

    A failure is logged one time in each process, and the connect goes on:
    it then has the delay of Windows, and its result is still correct.
    WHY one time: each HTTP request makes a new socket, and an index run
    sends thousands of requests.
    """
    if _no_syn_retransmissions is None:
        return
    try:
        _no_syn_retransmissions(sock)
    except OSError as exc:
        if not _warned.is_set():
            _warned.set()
            log.warning(
                "Could not stop the SYN retransmissions of a loopback socket (%s): a refused loopback "
                "connect then needs 2 s", exc,
            )


def create_connection(address: tuple[str, int], timeout: float | None = None,
                      source_address: tuple[str, int] | None = None) -> socket.socket:
    """Connect to a loopback *address*, as ``socket.create_connection`` does, with each SYN sent once on Windows.

    Each address that ``getaddrinfo`` gives is tried in its sequence, also
    when the socket of one address family cannot be made (IPv6 off).  When
    no address connects, the error of the first address is raised, as
    ``socket.create_connection`` does it.
    """
    host, port = address
    errors: list[OSError] = []
    for family, kind, proto, _canonname, sockaddr in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        sock: socket.socket | None = None
        try:
            sock = socket.socket(family, kind, proto)
            _send_each_syn_once(sock)
            sock.settimeout(timeout)
            if source_address is not None:
                sock.bind(source_address)
            sock.connect(sockaddr)
        except OSError as exc:
            if sock is not None:
                sock.close()
            errors.append(exc)
            continue
        return sock
    if errors:
        raise errors[0]
    raise OSError(f"getaddrinfo gave no address for {host}")

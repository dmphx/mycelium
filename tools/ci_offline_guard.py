"""Pytest plugin that names the tests which try to reach the network.

Loaded only by CI: python -m pytest -p tools.ci_offline_guard tests/

Diagnostics only. The CI test step runs with network_mode: none, and
tools/ci_network_probe.py proves that boundary before the suite starts; this
plugin cannot see child processes, the base _socket type or native code.
What it adds is attribution: in-process connects, sends and name lookups to
anything but loopback or Unix sockets are refused, recorded with the running
test's node id and listed in the terminal summary, so a test that silently
depends on the network (and swallows the failure) shows up by name.
"""
import ipaddress
import socket

import pytest

_ATTEMPTS = []
_CURRENT = ["<collection>"]
_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def _is_local(host):
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip("[]").split("%", 1)[0].lower()
    if host == "" or host in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _refuse(kind, target):
    _ATTEMPTS.append(f"{_CURRENT[0]}: {kind} {target!r}")
    raise OSError(f"ci_offline_guard: network {kind} to {target!r} refused")


def _check_address(sock, address):
    if sock.family == getattr(socket, "AF_UNIX", None):
        return
    host = address[0] if isinstance(address, tuple) and address else address
    if not _is_local(host):
        _refuse("connect", address)


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_sendto = socket.socket.sendto
_real_sendmsg = socket.socket.sendmsg
_real_getaddrinfo = socket.getaddrinfo
_real_gethostbyaddr = socket.gethostbyaddr
_real_gethostbyname = socket.gethostbyname
_real_gethostbyname_ex = socket.gethostbyname_ex


def _connect(self, address):
    _check_address(self, address)
    return _real_connect(self, address)


def _connect_ex(self, address):
    _check_address(self, address)
    return _real_connect_ex(self, address)


def _sendto(self, data, *args):
    _check_address(self, args[-1])
    return _real_sendto(self, data, *args)


def _sendmsg(self, buffers, *args):
    if len(args) >= 3 and args[2] is not None:
        _check_address(self, args[2])
    return _real_sendmsg(self, buffers, *args)


def _gethostbyaddr(host):
    if not _is_local(host):
        _refuse("reverse lookup", host)
    return _real_gethostbyaddr(host)


def _getaddrinfo(host, *args, **kwargs):
    if not _is_local(host):
        _refuse("lookup", host)
    return _real_getaddrinfo(host, *args, **kwargs)


def _gethostbyname(host):
    if not _is_local(host):
        _refuse("lookup", host)
    return _real_gethostbyname(host)


def _gethostbyname_ex(host):
    if not _is_local(host):
        _refuse("lookup", host)
    return _real_gethostbyname_ex(host)


socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex
socket.socket.sendto = _sendto
socket.socket.sendmsg = _sendmsg
socket.gethostbyaddr = _gethostbyaddr
socket.getaddrinfo = _getaddrinfo
socket.gethostbyname = _gethostbyname
socket.gethostbyname_ex = _gethostbyname_ex


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _CURRENT[0] = item.nodeid
    try:
        return (yield)
    finally:
        _CURRENT[0] = "<between tests>"


def pytest_terminal_summary(terminalreporter):
    if _ATTEMPTS:
        terminalreporter.section("ci_offline_guard: tests that tried to reach the network")
        for attempt in sorted(set(_ATTEMPTS)):
            terminalreporter.write_line(attempt)

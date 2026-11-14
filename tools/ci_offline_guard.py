"""Pytest plugin that keeps the CI test run offline.

Loaded only by CI: python -m pytest -p tools.ci_offline_guard tests/

Untrusted Woodpecker repos cannot drop a step's network, and unshare is
refused by the default seccomp profile, so the guard sits in the pytest
process. Loopback and Unix sockets stay allowed for in-process test servers.
Every other connect or name lookup is refused and recorded; a recorded
attempt fails the session even when the code under test swallowed the error.
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
_real_getaddrinfo = socket.getaddrinfo
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
        terminalreporter.section("ci_offline_guard: refused network access")
        for attempt in sorted(set(_ATTEMPTS)):
            terminalreporter.write_line(attempt)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    if _ATTEMPTS and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED

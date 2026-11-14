"""Prove the CI test step is offline before the suite runs.

Run as: python -I tools/ci_network_probe.py

Every egress path that an in-process socket wrapper cannot see is tried
directly: a child process, the base _socket type, libc connect through
ctypes, forward and reverse DNS, and UDP sendmsg. Each must fail. Loopback
must still work, so a pass is not just a broken probe. Exits 1 if any
external path succeeds or loopback fails.
"""
import _socket
import ctypes
import ctypes.util
import socket
import struct
import subprocess
import sys

TARGET = ("1.1.1.1", 53)
results = []


def expect_blocked(name, attempt):
    try:
        attempt()
    except OSError as exc:
        results.append((name, True, f"blocked: {exc.__class__.__name__}: {exc}"))
    else:
        results.append((name, False, "REACHED THE NETWORK"))


def tcp_connect():
    with socket.create_connection(TARGET, timeout=3):
        pass


def base_socket_connect():
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        s.settimeout(3)
        s.connect(TARGET)
    finally:
        s.close()


def libc_connect():
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    fd = libc.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "socket() failed")
    # struct sockaddr_in: family in host order, port in network order, address, padding.
    addr = (struct.pack("=H", socket.AF_INET) + struct.pack("!H", TARGET[1])
            + socket.inet_aton(TARGET[0]) + bytes(8))
    try:
        if libc.connect(fd, ctypes.c_char_p(addr), len(addr)) != 0:
            raise OSError(ctypes.get_errno(), "connect() failed")
    finally:
        libc.close(fd)


def child_connect():
    code = "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3).close()"
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, timeout=30)
    if proc.returncode != 0:
        raise OSError(f"child exit {proc.returncode}")


def forward_dns():
    socket.getaddrinfo("example.com", 443)


def reverse_dns():
    socket.gethostbyaddr("1.1.1.1")


def udp_sendmsg():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendmsg([b"\0" * 12], [], 0, TARGET)
    finally:
        s.close()


def loopback_works():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.create_connection(server.getsockname(), timeout=3)
        peer, _ = server.accept()
        client.sendall(b"ok")
        ok = peer.recv(2) == b"ok"
        client.close()
        peer.close()
    finally:
        server.close()
    results.append(("loopback tcp", ok, "works" if ok else "BROKEN"))


for name, attempt in [
    ("tcp connect", tcp_connect),
    ("base _socket connect", base_socket_connect),
    ("libc connect via ctypes", libc_connect),
    ("child process connect", child_connect),
    ("forward dns", forward_dns),
    ("reverse dns", reverse_dns),
    ("udp sendmsg", udp_sendmsg),
]:
    expect_blocked(name, attempt)
loopback_works()

for name, ok, detail in results:
    print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")
sys.exit(0 if all(ok for _, ok, _ in results) else 1)

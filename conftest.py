"""Offline suite: permit HTTP only to servers bound by this test process."""
import ipaddress
import os
import socket
import sys

os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

_test_ports = set()
_original_bind = socket.socket.bind


def _bind_test_server(sock, address):
    result = _original_bind(sock, address)
    actual = sock.getsockname()
    if isinstance(actual, tuple) and actual[0] in ("127.0.0.1", "::1"):
        _test_ports.add(actual[1])
    return result


socket.socket.bind = _bind_test_server


def _offline_audit(event, args):
    if event != "socket.connect":
        return
    address = args[1]
    if isinstance(address, tuple):
        try:
            loopback = ipaddress.ip_address(address[0]).is_loopback
        except ValueError:
            loopback = False
        if loopback and address[1] in _test_ports:
            return
    raise RuntimeError(f"Offline suite blocked network connection to {address!r}")


sys.addaudithook(_offline_audit)

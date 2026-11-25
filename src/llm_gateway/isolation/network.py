"""Child-only socket guard. Deployment firewall remains the OS security boundary."""

import ipaddress
import socket
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

from .resolver import check_search_configuration

_permit = ContextVar("owned_provider_network", default=None)
_installed = False


def proxy_destination(value):
    if value is None:
        return None
    try:
        # urlsplit silently strips some controls; getaddrinfo on supported
        # interpreters can truncate NUL-containing hosts (CVE-2026-116566).
        # Validate the original trusted setting before either normalization.
        if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value):
            raise ValueError()
        url = urlsplit(value)
        if (
            url.scheme != "http"
            or not url.hostname
            or url.username
            or url.password
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
            or not url.port
        ):
            raise ValueError()
        return url.hostname, url.port
    except ValueError:
        raise ValueError("deployment_proxy_invalid") from None


@contextmanager
def owned_network(host, port=443, *, public=False):
    token = _permit.set((host, port, set(), public))
    try:
        yield
    finally:
        _permit.reset(token)


def install():
    """Install only in the owned child, before importing any provider SDK."""
    global _installed
    if _installed:
        return
    lookup, connect, connect_ex = (
        socket.getaddrinfo,
        socket.socket.connect,
        socket.socket.connect_ex,
    )

    def resolve(host, port, *args, **kwargs):
        allowed = _permit.get()
        if allowed is None or (host, port) != allowed[:2]:
            raise PermissionError("unsolicited_provider_network_denied")
        check_search_configuration()
        result = lookup(host, port, *args, **kwargs)
        if allowed[3] and any(not ipaddress.ip_address(item[4][0]).is_global for item in result):
            raise PermissionError("provider_nonpublic_address_denied")
        allowed[2].update((item[4][0], item[4][1]) for item in result)
        return result

    def check(sock, address):
        allowed = _permit.get()
        if sock.family not in {socket.AF_INET, socket.AF_INET6} or allowed is None:
            raise PermissionError("unsolicited_provider_network_denied")
        try:
            host, port = address[:2]
            literal = str(ipaddress.ip_address(allowed[0]))
        except ValueError:
            literal = None
        if (host, port) not in allowed[2] and (host, port) != (literal, allowed[1]):
            raise PermissionError("provider_network_destination_denied")

    def guarded_connect(sock, address):
        check(sock, address)
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        check(sock, address)
        return connect_ex(sock, address)

    def deny(*args, **kwargs):
        raise PermissionError("unsolicited_provider_network_denied")

    socket.getaddrinfo = resolve
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.socket.sendto = deny
    socket.gethostbyname = deny
    socket.gethostbyname_ex = deny
    socket.gethostbyaddr = deny
    if hasattr(socket.socket, "sendmsg"):
        socket.socket.sendmsg = deny
    _installed = True

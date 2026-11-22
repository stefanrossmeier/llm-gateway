"""Small deployment CONNECT allowlist; no credentials, TLS interception or logs."""

import argparse
import ipaddress
import select
import socket
import socketserver
import threading
import time

from llm_gateway.isolation.resolver import check_search_configuration

ALLOWED_HOSTS = frozenset({"api.openai.com", "openrouter.ai"})


def destination(header):
    try:
        lines = header.decode("ascii").split("\r\n")
        method, target, version = lines[0].split(" ")
        host, port = target.split(":")
        if (
            method != "CONNECT"
            or host not in ALLOWED_HOSTS
            or port != "443"
            or version != "HTTP/1.1"
        ):
            raise ValueError()
        if any(
            line.lower().startswith(("authorization:", "proxy-authorization:")) for line in lines
        ):
            raise ValueError()
        return host
    except (ValueError, UnicodeError):
        raise ValueError("proxy_destination_denied") from None


def public_addresses(host):
    check_search_configuration()
    addresses = []
    for family, socktype, proto, _, address in socket.getaddrinfo(
        host, 443, type=socket.SOCK_STREAM
    ):
        if not ipaddress.ip_address(address[0]).is_global:
            raise ValueError("proxy_nonpublic_address_denied")
        addresses.append((family, socktype, proto, address))
    if not addresses:
        raise ValueError("proxy_destination_unavailable")
    return addresses


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        if not self.server.capacity.acquire(blocking=False):
            return
        try:
            self.request.settimeout(5)
            header = bytearray()
            header_deadline = time.monotonic() + 5
            while not header.endswith(b"\r\n\r\n"):
                remaining = header_deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError()
                self.request.settimeout(remaining)
                byte = self.request.recv(1)
                if not byte:
                    raise ValueError()
                header.extend(byte)
                if len(header) > 8192:
                    raise ValueError()
            host = destination(header)
            remote = None
            for family, socktype, proto, address in public_addresses(host):
                candidate = socket.socket(family, socktype, proto)
                try:
                    candidate.settimeout(10)
                    candidate.connect(address)
                    remote = candidate
                    break
                except OSError:
                    candidate.close()
            if remote is None:
                raise ValueError()
            with remote:
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                deadline = time.monotonic() + 3600
                while time.monotonic() < deadline:
                    readable, _, _ = select.select(
                        [self.request, remote], [], [], min(30, max(0, deadline - time.monotonic()))
                    )
                    if not readable:
                        continue
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        (remote if source is self.request else self.request).sendall(data)
        except (OSError, ValueError):
            try:
                self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
        finally:
            self.server.capacity.release()


class Proxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address):
        self.capacity = threading.BoundedSemaphore(64)
        super().__init__(address, Handler)

    def handle_error(self, request, client_address):
        pass  # Never log caller bytes or untrusted exception strings.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=3128)
    args = parser.parse_args()
    with Proxy((args.host, args.port)) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()

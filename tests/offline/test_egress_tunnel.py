"""CONNECT policy and tunnel lifecycle with socket/selector doubles only."""

import socket
from types import SimpleNamespace

import pytest

from llm_gateway.service import egress_proxy as proxy

pytestmark = pytest.mark.offline
HEADER = b"CONNECT api.openai.com:443 HTTP/1.1\r\nHost: api.openai.com:443\r\n\r\n"
PUBLIC = (socket.AF_INET, socket.SOCK_STREAM, 6, ("1.1.1.1", 443))


class Stream:
    def __init__(self, chunks=(), *, fail_connect=False, fail_send=False):
        self.chunks = list(chunks)
        self.fail_connect, self.fail_send = fail_connect, fail_send
        self.sent, self.timeouts, self.connected, self.closed = [], [], [], False

    def recv(self, size):
        assert size in {1, 65536}
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, data):
        if self.fail_send:
            raise OSError("synthetic send failure")
        self.sent.append(data)

    def settimeout(self, value):
        self.timeouts.append(value)

    def connect(self, address):
        assert address == PUBLIC[-1]
        if self.fail_connect:
            raise OSError("synthetic connect failure")
        self.connected = True

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Capacity:
    def __init__(self, allowed=True):
        self.allowed, self.released = allowed, 0

    def acquire(self, *, blocking):
        assert blocking is False
        return self.allowed

    def release(self):
        self.released += 1


def run(request, *, capacity=None):
    capacity = capacity or Capacity()
    proxy.Handler(
        request,
        ("127.0.0.1", 1),
        SimpleNamespace(capacity=capacity, allowed_hosts=proxy.ALLOWED_HOSTS),
    )
    return capacity


def configure(monkeypatch, candidates, readable):
    monkeypatch.setattr(proxy, "public_addresses", lambda host: [PUBLIC] * len(candidates))
    pending = iter(candidates)
    monkeypatch.setattr(proxy.socket, "socket", lambda *args: next(pending))
    selections = iter(readable)
    monkeypatch.setattr(proxy.select, "select", lambda *args: (next(selections), [], []))
    monkeypatch.setattr(proxy.time, "monotonic", lambda: 1)


def test_quiet_upstream_remains_open_then_forwards_both_directions(monkeypatch):
    request = Stream([bytes([value]) for value in HEADER] + [b"synthetic-client", b""])
    remote = Stream([b"synthetic-server"])
    configure(monkeypatch, [remote], [[], [], [request, remote], [request]])
    capacity = run(request)
    assert request.sent == [b"HTTP/1.1 200 Connection Established\r\n\r\n", b"synthetic-server"]
    assert remote.sent == [b"synthetic-client"]
    assert remote.connected and remote.closed and capacity.released == 1


def test_connect_fallback_closes_failed_socket_and_stops_after_success(monkeypatch):
    request = Stream([bytes([value]) for value in HEADER])
    failed, successful = Stream(fail_connect=True), Stream()
    configure(monkeypatch, [failed, successful], [[successful]])
    assert run(request).released == 1
    assert failed.closed and successful.connected and successful.closed
    assert request.sent[0].startswith(b"HTTP/1.1 200")


@pytest.mark.parametrize("kind", ["eof", "too_large", "wrong_destination", "send_failure"])
def test_header_rejections_are_bounded_and_release_capacity(monkeypatch, kind):
    header = b"" if kind == "eof" else b"x" * 8193
    if kind in {"wrong_destination", "send_failure"}:
        header = HEADER.replace(b"api.openai.com", b"evil.invalid")
    request = Stream([bytes([value]) for value in header], fail_send=kind == "send_failure")
    monkeypatch.setattr(proxy, "public_addresses", lambda host: pytest.fail("DNS must not run"))
    monkeypatch.setattr(proxy.time, "monotonic", lambda: 1)
    assert run(request).released == 1
    if kind != "send_failure":
        assert request.sent == [b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n"]


def test_header_absolute_deadline_precedes_read(monkeypatch):
    request = Stream([b"x"])
    times = iter([0, 6])
    monkeypatch.setattr(proxy.time, "monotonic", lambda: next(times))
    assert run(request).released == 1
    assert request.chunks == [b"x"] and request.sent[0].startswith(b"HTTP/1.1 403")


def test_all_connect_failures_close_candidates_and_release(monkeypatch):
    request = Stream([bytes([value]) for value in HEADER])
    candidates = [Stream(fail_connect=True), Stream(fail_connect=True)]
    configure(monkeypatch, candidates, [])
    assert run(request).released == 1
    assert all(candidate.closed for candidate in candidates)
    assert request.sent[0].startswith(b"HTTP/1.1 403")


def test_absolute_tunnel_deadline_stops_quiet_polls(monkeypatch):
    request = Stream([bytes([value]) for value in HEADER])
    remote = Stream()
    configure(monkeypatch, [remote], [[], []])
    calls, waits = [0], []

    def now():
        # Header time stays fixed. After establishment, a quiet poll advances
        # through the actual absolute deadline, independently of the poll size.
        return 3602 if calls[0] >= 2 else 1

    def select(reads, writes, errors, timeout):
        calls[0] += 1
        waits.append(timeout)
        return [], [], []

    monkeypatch.setattr(proxy.time, "monotonic", now)
    monkeypatch.setattr(proxy.select, "select", select)
    assert run(request).released == 1
    assert calls[0] == 2 and waits == [30, 30]
    assert remote.closed


def test_capacity_rejection_does_not_read_resolve_connect_or_release():
    request, capacity = Stream([b"untouched"]), Capacity(False)
    assert run(request, capacity=capacity).released == 0
    assert request.chunks == [b"untouched"] and request.timeouts == [] and request.sent == []


@pytest.mark.parametrize("addresses", [[], ["127.0.0.1"], ["1.1.1.1", "10.0.0.1"], ["::1"]])
def test_dns_empty_private_or_mixed_results_fail_before_connect(monkeypatch, addresses):
    monkeypatch.setattr(proxy, "check_search_configuration", lambda: None)
    monkeypatch.setattr(
        proxy.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses
        ],
    )
    with pytest.raises(
        ValueError, match="proxy_(nonpublic_address_denied|destination_unavailable)"
    ):
        proxy.public_addresses("api.openai.com")


def test_public_dns_results_preserve_family_and_port(monkeypatch):
    monkeypatch.setattr(proxy, "check_search_configuration", lambda: None)
    monkeypatch.setattr(
        proxy.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(PUBLIC[0], PUBLIC[1], PUBLIC[2], "", PUBLIC[3])],
    )
    assert proxy.public_addresses("api.openai.com") == [PUBLIC]


def test_proxy_cli_and_error_handler_do_not_log_caller_data(monkeypatch, capsys):
    calls = []

    class Server:
        def __init__(self, address, allowed_hosts):
            assert allowed_hosts == proxy.ALLOWED_HOSTS
            calls.append(address)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def serve_forever(self):
            calls.append("served")

    monkeypatch.setattr(proxy, "Proxy", Server)
    monkeypatch.setattr("sys.argv", ["egress_proxy", "--host", "127.0.0.1", "--port", "1234"])
    proxy.main()
    assert calls == [("127.0.0.1", 1234), "served"]
    assert capsys.readouterr().out == ""


def test_server_capacity_and_safe_error_handler(monkeypatch, capsys):
    addresses = []
    monkeypatch.setattr(
        proxy.socketserver.ThreadingTCPServer,
        "__init__",
        lambda self, address, handler: addresses.append((address, handler)),
    )
    server = proxy.Proxy(("127.0.0.1", 3128))
    assert addresses == [(("127.0.0.1", 3128), proxy.Handler)]
    assert all(server.capacity.acquire(blocking=False) for _ in range(64))
    assert not server.capacity.acquire(blocking=False)
    server.handle_error(b"untrusted caller bytes", ("private-address", 1))
    assert capsys.readouterr() == ("", "")

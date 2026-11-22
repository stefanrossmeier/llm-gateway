import socket

import pytest

from llm_gateway.service import egress_proxy
from llm_gateway.service.egress_proxy import destination, public_addresses

pytestmark = pytest.mark.offline


@pytest.mark.parametrize(
    "target",
    [
        "evil.invalid:443",
        "api.openai.com:80",
        "127.0.0.1:443",
        "api.openai.com.evil.invalid:443",
        "API.OPENAI.COM:443",
    ],
)
def test_only_exact_fixed_tls_destinations(target):
    with pytest.raises(ValueError):
        destination(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())


def test_proxy_accepts_fixed_hosts_without_receiving_credentials():
    assert (
        destination(b"CONNECT openrouter.ai:443 HTTP/1.1\r\nHost: openrouter.ai:443\r\n\r\n")
        == "openrouter.ai"
    )
    with pytest.raises(ValueError):
        destination(b"CONNECT openrouter.ai:443 HTTP/1.1\r\nAuthorization: Bearer secret\r\n\r\n")


def test_proxy_rejects_private_dns_answers(monkeypatch):
    monkeypatch.setattr(egress_proxy, "check_search_configuration", lambda: None)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(ValueError, match="nonpublic"):
        public_addresses("api.openai.com")

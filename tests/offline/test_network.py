import socket

import pytest
from test_provider_runtime import test_owned_transport_all_operations as exercise_adapter

from llm_gateway.isolation import network

pytestmark = pytest.mark.offline


@pytest.fixture
def guarded(monkeypatch):
    # Scope the child-only installation when exercising it inside a unit test.
    for target, names in [
        (socket, ["getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"]),
        (socket.socket, ["connect", "connect_ex", "sendto", "sendmsg"]),
    ]:
        for name in names:
            if hasattr(target, name):
                monkeypatch.setattr(target, name, getattr(target, name))
    monkeypatch.setattr(network, "_installed", False)
    return network


def test_unsolicited_dns_and_sockets_denied_and_permit_expires(guarded, monkeypatch):
    connects = []
    monkeypatch.setattr(guarded, "check_search_configuration", lambda: None)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))],
    )
    monkeypatch.setattr(socket.socket, "connect", lambda sock, address: connects.append(address))
    guarded.install()
    with socket.socket() as sock:
        with pytest.raises(PermissionError):
            socket.getaddrinfo("metadata.invalid", 443)
        with pytest.raises(PermissionError):
            sock.connect(("192.0.2.1", 443))
        with network.owned_network("api.openai.com"):
            socket.getaddrinfo("api.openai.com", 443)
            sock.connect(("192.0.2.1", 443))
            with pytest.raises(PermissionError):
                socket.getaddrinfo("evil.invalid", 443)
            with pytest.raises(PermissionError):
                sock.connect(("192.0.2.2", 443))
        with pytest.raises(PermissionError):
            sock.connect(("192.0.2.1", 443))
    assert connects == [("192.0.2.1", 443)]


@pytest.mark.parametrize("addresses", [["8.8.8.8"], ["8.8.8.8", "127.0.0.1"]])
def test_configured_direct_provider_rejects_mixed_or_private_dns(guarded, monkeypatch, addresses):
    monkeypatch.setattr(guarded, "check_search_configuration", lambda: None)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses
        ],
    )
    guarded.install()
    with network.owned_network("inference.example", public=True):
        if len(addresses) > 1:
            with pytest.raises(PermissionError, match="nonpublic"):
                socket.getaddrinfo("inference.example", 443)
        else:
            assert socket.getaddrinfo("inference.example", 443)[0][4][0] == "8.8.8.8"


@pytest.mark.parametrize("kind", ["openai", "openrouter"])
@pytest.mark.parametrize("operation", ["chat", "responses", "decisions", "embeddings"])
def test_pinned_sdk_works_under_child_network_guard(guarded, kind, operation):
    guarded.install()
    exercise_adapter(kind, operation)


@pytest.mark.parametrize(
    "value",
    [
        "https://proxy:3128",
        "http://user:secret@proxy:3128",
        "http://proxy",
        "http://proxy:3128/path",
        "http://proxy:bad",
    ],
)
def test_deployment_proxy_rejects_implicit_or_secret_destinations(value):
    with pytest.raises(ValueError, match="deployment_proxy_invalid"):
        network.proxy_destination(value)


def test_child_network_guard_install_is_idempotent_and_proxy_is_explicit(guarded):
    assert guarded.proxy_destination(None) is None
    assert guarded.proxy_destination("http://proxy:3128") == ("proxy", 3128)
    guarded.install()
    first = socket.getaddrinfo
    guarded.install()
    assert socket.getaddrinfo is first
    with pytest.raises(PermissionError):
        socket.getaddrinfo("api.openai.com", 443)


@pytest.mark.parametrize(
    "value",
    [
        "http://proxy\x00untrusted:3128",
        "http://proxy:31\x0028",
        "http://pro\nxy:3128",
        "http://pro\rxy:3128",
        "http://pro\txy:3128",
        " http://proxy:3128",
        "http://proxy:3128\x7f",
        b"http://proxy:3128",
    ],
)
def test_proxy_control_characters_are_rejected_before_url_or_dns_normalization(value):
    with pytest.raises(ValueError, match="deployment_proxy_invalid"):
        network.proxy_destination(value)

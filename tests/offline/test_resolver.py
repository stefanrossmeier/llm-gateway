import os
import stat
from types import SimpleNamespace

import pytest

from llm_gateway.isolation import resolver
from llm_gateway.service import egress_proxy

pytestmark = pytest.mark.offline


def configuration(monkeypatch, tmp_path, body, *, uid=0, mode=stat.S_IFREG | 0o644, env=None):
    path = tmp_path / "resolv.conf"
    path.write_bytes(body)
    monkeypatch.setattr(resolver, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(
        resolver,
        "os",
        SimpleNamespace(
            environ=env or {},
            O_RDONLY=os.O_RDONLY,
            O_NONBLOCK=os.O_NONBLOCK,
            open=lambda *_: os.open(path, os.O_RDONLY),
            fstat=lambda fd: SimpleNamespace(st_mode=mode, st_uid=uid, st_size=len(body)),
            read=os.read,
            close=os.close,
        ),
    )


@pytest.mark.parametrize(
    "body",
    [
        b"nameserver 127.0.0.11\nsearch .\n",
        b"search example.test internal.test # trusted comment\n",
        b"domain example.test;comment\n",
        b"search " + b"x" * 128 + b"\n",
    ],
)
def test_bounded_private_host_resolver_configuration(monkeypatch, tmp_path, body):
    configuration(monkeypatch, tmp_path, body)
    resolver.check_search_configuration()


@pytest.mark.parametrize(
    "body,changes",
    [
        (b"search " + b"x" * 129, {}),
        (b"search " + b"x" * 200, {}),
        (b"domain " + b"x" * 200, {}),
        (b"search " + b"a " * 70, {}),
        (b"nameserver 127.0.0.11\0", {}),
        (b"#" * 16385, {}),
        (b"search \xff", {}),
        (b"", {"uid": 10001}),
        (b"", {"mode": stat.S_IFREG | 0o666}),
        (b"", {"mode": stat.S_IFIFO | 0o644}),
        (b"", {"env": {"LOCALDOMAIN": "x" * 200}}),
    ],
)
def test_oversized_or_untrusted_configuration_never_reaches_dns(
    monkeypatch, tmp_path, body, changes
):
    configuration(monkeypatch, tmp_path, body, **changes)
    monkeypatch.setattr(
        egress_proxy.socket,
        "getaddrinfo",
        lambda *_a, **_k: pytest.fail("libc resolver was reached"),
    )
    with pytest.raises(ValueError, match="resolver_search_configuration_denied"):
        egress_proxy.public_addresses("api.openai.com")


def test_non_linux_does_not_read_linux_host_configuration(monkeypatch):
    monkeypatch.setattr(resolver, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(resolver, "os", SimpleNamespace())
    resolver.check_search_configuration()

import socket

import pytest
from pydantic import ValidationError

from deal_tracker.browser_proxy import BrowserProxy, browser_proxy, browser_target
from deal_tracker.fetcher import FetchBlocked
from deal_tracker.settings import Settings


def test_browser_proxy_pins_public_dns_and_allows_only_approved_hosts(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ])
    assert browser_target("shop.example.com:443", ["https://example.com"]) == ("93.184.216.34", 443)
    assert browser_target("static.example.net:443", ["https://example.com", "https://static.example.net"]) == ("93.184.216.34", 443)
    with pytest.raises(FetchBlocked, match="not approved"):
        browser_target("other.example.net:443", ["https://example.com"])


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["93.184.216.34", "10.0.0.1"], ["169.254.169.254"]])
def test_browser_proxy_rejects_private_and_mixed_dns_answers(monkeypatch, addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses
    ])
    with pytest.raises(FetchBlocked, match="non-public"):
        browser_target("example.com:443", ["https://example.com"])


@pytest.mark.parametrize("authority", ["user:password@example.com:443", "example.com:80", "example.com/path", "example.com?next=evil"])
def test_browser_proxy_rejects_unsafe_authorities(authority):
    with pytest.raises(ValueError):
        browser_target(authority, ["https://example.com"])


def test_browser_proxy_denies_loopback_over_real_connect_protocol():
    with browser_proxy(["https://example.com"]) as endpoint:
        port = int(endpoint.rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            client.sendall(b"CONNECT 127.0.0.1:8765 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            assert b"403" in client.recv(1024)


def test_browser_permissions_respect_robots_and_separate_resource_navigation(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ])
    downloads = []

    def robots(url, *_args, **_kwargs):
        downloads.append(url)
        return 200, "User-agent: *\nDisallow: /private", url

    monkeypatch.setattr("deal_tracker.browser_proxy.download", robots)
    server = BrowserProxy(["https://example.com", "https://static.example.net"])
    try:
        assert server.permitted("https://example.com/product", True)
        assert not server.permitted("https://example.com/private", True)
        assert server.permitted("https://static.example.net/script.js", False)
        assert not server.permitted("https://static.example.net/page", True)
        assert len(downloads) == 2
    finally:
        server.server_close()


@pytest.mark.parametrize("origin", ["http://example.com", "https://127.0.0.1", "https://example.com:80", "https://example.com/path", "https://user:pass@example.com"])
def test_browser_resource_configuration_rejects_unsafe_origins(origin):
    with pytest.raises(ValidationError):
        Settings(browser_resource_origins=origin)
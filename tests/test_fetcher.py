import socket
from unittest.mock import MagicMock

import pytest

from deal_tracker.fetcher import FetchBlocked, download, extract_page, fetch_page, public_target


def dns(monkeypatch, *addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses
    ])


def test_resolved_address_is_pinned_and_hostname_is_kept(monkeypatch):
    dns(monkeypatch, "93.184.216.34")
    assert public_target("https://shop.example.com/shirt?size=M", "https://example.com") == (
        "shop.example.com", "93.184.216.34", "/shirt?size=M",
    )


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"])
def test_dns_private_and_mixed_answers_are_blocked(monkeypatch, address):
    dns(monkeypatch, "93.184.216.34", address)
    with pytest.raises(FetchBlocked, match="non-public"):
        public_target("https://example.com/shirt", "https://example.com")


@pytest.mark.parametrize("url", ["http://example.com", "https://evil.example.org", "https://example.com:80"])
def test_wrong_scheme_scope_and_port_are_blocked(url):
    with pytest.raises(FetchBlocked):
        public_target(url, "https://example.com")


def test_robots_denial_prevents_page_download(monkeypatch):
    requests = []

    def download(url, shop_url, limit):
        requests.append(url)
        return 200, "User-agent: *\nDisallow: /private", url

    monkeypatch.setattr("deal_tracker.fetcher.download", download)
    with pytest.raises(FetchBlocked, match="robots.txt"):
        fetch_page("https://example.com/private/shirt", "https://example.com")
    assert requests == ["https://example.com/robots.txt"]


def test_page_extraction_keeps_evidence_and_in_scope_links():
    page = extract_page(
        '<title>Shirt</title><script type="application/ld+json">{"price":49}</script>'
        '<script>ignoreThis()</script><p>Linen, green, size M</p>'
        '<a href="/shirts">Shirts</a><a href="https://other.example.org">Outside</a>',
        "https://example.com/product", "https://example.com",
    )
    assert page["title"] == "Shirt"
    assert "Linen, green, size M" in page["text"]
    assert "ignoreThis" not in page["text"]
    assert page["structured_data"] == '{"price":49}'
    assert page["links"] == [{"url": "https://example.com/shirts", "title": "Shirts"}]


def test_missing_robots_file_allows_same_origin_only(monkeypatch):
    def response(url, shop_url, limit=None, allowed=None):
        if url.endswith("robots.txt"):
            return 404, "Not found", url
        assert allowed("https://example.com/another-product")
        assert not allowed("https://cdn.example.com/product")
        return 200, "<p>Product</p>", url

    monkeypatch.setattr("deal_tracker.fetcher.download", response)
    assert fetch_page("https://example.com/product", "https://example.com")["text"] == "Product"


def test_script_only_page_is_reported_as_unreadable():
    with pytest.raises(FetchBlocked, match="no readable content"):
        extract_page('<html><script>loadProduct()</script></html>', "https://example.com/product", "https://example.com")
    structured = extract_page(
        '<script type="application/ld+json">{"price":49}</script>',
        "https://example.com/product", "https://example.com",
    )
    assert structured["structured_data"] == '{"price":49}'


def test_redirect_permission_is_checked_before_fetching_destination(monkeypatch):
    dns(monkeypatch, "93.184.216.34")
    factory = MagicMock()
    pool = factory.return_value.__enter__.return_value
    pool.request.return_value.status = 302
    pool.request.return_value.headers = {"Location": "/private"}
    monkeypatch.setattr("deal_tracker.fetcher.urllib3.HTTPSConnectionPool", factory)
    with pytest.raises(FetchBlocked, match="robots.txt"):
        download(
            "https://example.com/product", "https://example.com",
            allowed=lambda url: not url.endswith("/private"),
        )
    assert pool.request.call_count == 1
    assert factory.call_args.args == ("93.184.216.34",)
    assert factory.call_args.kwargs["server_hostname"] == "example.com"
    assert factory.call_args.kwargs["assert_hostname"] == "example.com"
    assert pool.request.call_args.kwargs["retries"] is False


def test_download_size_limit_is_enforced(monkeypatch):
    dns(monkeypatch, "93.184.216.34")
    factory = MagicMock()
    response = factory.return_value.__enter__.return_value.request.return_value
    response.status = 200
    response.headers = {"Content-Type": "text/html"}
    response.read.return_value = b"oversized"
    monkeypatch.setattr("deal_tracker.fetcher.urllib3.HTTPSConnectionPool", factory)
    with pytest.raises(FetchBlocked, match="download limit"):
        download("https://example.com", "https://example.com", limit=3)
    response.close.assert_called_once()
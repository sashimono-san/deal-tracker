import json
import socket
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import urllib3
from bs4 import BeautifulSoup

from .collection import in_shop_scope
from .models import public_url

USER_AGENT = "DealTracker/0.1"
MAX_BODY = 1_000_000


class FetchBlocked(ValueError):
    pass


def public_target(url: str, shop_url: str) -> tuple[str, str, str]:
    public_url(url)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.port not in {None, 443}:
        raise FetchBlocked("Collection requires HTTPS on port 443")
    if not in_shop_scope(url, shop_url):
        raise FetchBlocked("URL is outside the selected shop")
    hostname = (parsed.hostname or "").encode("idna").decode("ascii")
    addresses = {
        record[4][0] for record in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    }
    if not addresses or any(not ip_address(address).is_global for address in addresses):
        raise FetchBlocked("Shop DNS resolves to a non-public address")
    address = sorted(addresses, key=lambda value: (":" in value, value))[0]
    target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    return hostname, address, target


def download(
    url: str, shop_url: str, limit: int = MAX_BODY,
    allowed: Callable[[str], bool] | None = None,
) -> tuple[int, str, str]:
    for _attempt in range(4):
        if allowed is not None and not allowed(url):
            raise FetchBlocked("Redirect is outside the permitted robots.txt scope")
        hostname, address, target = public_target(url, shop_url)
        with urllib3.HTTPSConnectionPool(
            address, port=443, server_hostname=hostname, assert_hostname=hostname,
            cert_reqs="CERT_REQUIRED", timeout=urllib3.Timeout(connect=5, read=10),
        ) as pool:
            response = pool.request(
                "GET", target, headers={
                    "Host": hostname, "User-Agent": USER_AGENT, "Accept-Encoding": "identity",
                    "Accept": "text/html,application/xhtml+xml,text/plain",
                }, assert_same_host=False, redirect=False, retries=False,
                preload_content=False, decode_content=False,
            )
            try:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise FetchBlocked("Redirect has no destination")
                    url = urljoin(url, location)
                    continue
                encoding = response.headers.get("Content-Encoding", "identity").lower()
                if encoding not in {"", "identity"}:
                    raise FetchBlocked("Compressed responses are not accepted")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                if response.status == 200 and content_type not in {
                    "text/html", "application/xhtml+xml", "text/plain",
                }:
                    raise FetchBlocked("The response is not an HTML or text page")
                raw = response.read(limit + 1, decode_content=False)
                if len(raw) > limit:
                    raise FetchBlocked("Page exceeds the download limit")
                return response.status, raw.decode("utf-8", errors="replace"), url
            finally:
                response.close()
    raise FetchBlocked("Too many redirects")


def fetch_page(url: str, shop_url: str) -> dict:
    parsed = urlsplit(url)
    robots_url = urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
    status, robots, _final_robots = download(robots_url, shop_url, limit=128_000)
    policy = None
    if status == 200:
        policy = RobotFileParser()
        policy.parse(robots.splitlines())
        if not policy.can_fetch(USER_AGENT, url):
            raise FetchBlocked("robots.txt disallows this page")
    elif status not in {404, 410}:
        raise FetchBlocked(f"Cannot establish robots.txt permission (HTTP {status})")
    def allowed(target: str) -> bool:
        destination = urlsplit(target)
        return (
            (destination.scheme, destination.netloc) == (parsed.scheme, parsed.netloc)
            and (policy is None or policy.can_fetch(USER_AGENT, target))
        )

    status, document, final_url = download(url, shop_url, allowed=allowed)
    if status != 200:
        raise FetchBlocked(f"Shop returned HTTP {status}")
    return extract_page(document, final_url, shop_url)


def extract_page(document: str, url: str, shop_url: str) -> dict:
    soup = BeautifulSoup(document, "html.parser")
    title = soup.title.get_text(" ", strip=True)[:200] if soup.title else ""
    structured = "\n".join(
        element.get_text() for element in soup.find_all("script", type="application/ld+json")
    )[:6000]
    for element in soup(["script", "style", "noscript", "svg"]):
        element.decompose()
    text = soup.get_text(" ", strip=True)
    links = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(url, str(anchor["href"]))
        if href in seen or len(href) > 1000 or not in_shop_scope(href, shop_url):
            continue
        if urlsplit(href).scheme != "https":
            continue
        seen.add(href)
        links.append({"url": href, "title": anchor.get_text(" ", strip=True)[:120]})
        if len(links) == 20:
            break
    if not text and not structured and not links:
        raise FetchBlocked("Page has no readable content; JavaScript rendering or browser access may be required")
    return {
        "url": url, "fetched_at": datetime.now(UTC).isoformat(), "title": title,
        "text": text[:6000], "structured_data": structured, "links": links,
        "truncated": len(text) > 6000,
    }


def main() -> None:
    try:
        if len(sys.argv) != 3:
            raise FetchBlocked("Supply a page URL and a shop URL")
        print(json.dumps(fetch_page(sys.argv[1], sys.argv[2])))
    except (ValueError, OSError, urllib3.exceptions.HTTPError) as error:
        print(json.dumps({"error": str(error)[:500]}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
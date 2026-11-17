import json
import selectors
import socket
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import BoundedSemaphore, Event, Lock, Thread
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import urllib3

from .collection import in_shop_scope
from .fetcher import USER_AGENT, FetchBlocked, download, public_target


def browser_target(authority: str, origins: list[str]) -> tuple[str, int]:
    url = f"https://{authority}"
    parsed = urlsplit(url)
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise FetchBlocked("Invalid proxy destination")
    origin = next((origin for origin in origins if in_shop_scope(url, origin)), None)
    if origin is None:
        raise FetchBlocked("Browser destination is not approved")
    _hostname, address, _target = public_target(url, origin)
    return address, 443


class BrowserProxy(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, origins: list[str]):
        self.origins = origins
        self.stopped = Event()
        self.slots = BoundedSemaphore(24)
        self.budget_lock = Lock()
        self.policy_lock = Lock()
        self.policies = {}
        self.remaining_bytes = 64_000_000
        super().__init__(("127.0.0.1", 0), ProxyHandler)

    def permitted(self, url: str, navigation: bool) -> bool:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or (navigation and not in_shop_scope(url, self.origins[0])):
            return False
        browser_target(parsed.netloc, self.origins)
        origin = f"https://{parsed.netloc}"
        with self.policy_lock:
            if origin not in self.policies:
                status, text, _final = download(f"{origin}/robots.txt", origin, limit=128_000)
                if status == 200:
                    policy = RobotFileParser()
                    policy.parse(text.splitlines())
                    self.policies[origin] = policy
                elif status in {404, 410}:
                    self.policies[origin] = None
                else:
                    return False
            policy = self.policies[origin]
            return policy is None or policy.can_fetch(USER_AGENT, url)


class ProxyHandler(BaseHTTPRequestHandler):
    server: BrowserProxy
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def send_error(self, code, message=None, explain=None):
        try:
            super().send_error(code, message, explain)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_POST(self):
        allowed = False
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.path != "/permission" or not 0 < length <= 4096:
                raise ValueError("Invalid permission request")
            request = json.loads(self.rfile.read(length))
            allowed = self.server.permitted(request["url"], request.get("navigation", False))
        except (ValueError, KeyError, TypeError, OSError, urllib3.exceptions.HTTPError):
            pass
        body = json.dumps({"allowed": allowed}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self):
        if not self.server.slots.acquire(blocking=False):
            self.send_error(429, "Browser connection limit reached")
            return
        connected = False
        try:
            address = browser_target(self.path, self.server.origins)
            with socket.create_connection(address, timeout=5) as upstream:
                self.send_response(200, "Connection established")
                self.end_headers()
                self.wfile.flush()
                connected = True
                self.connection.settimeout(10)
                upstream.settimeout(10)
                with selectors.DefaultSelector() as selector:
                    selector.register(self.connection, selectors.EVENT_READ, upstream)
                    selector.register(upstream, selectors.EVENT_READ, self.connection)
                    while not self.server.stopped.is_set():
                        for key, _events in selector.select(timeout=0.5):
                            chunk = key.fileobj.recv(65536)
                            if not chunk:
                                return
                            with self.server.budget_lock:
                                self.server.remaining_bytes -= len(chunk)
                                if self.server.remaining_bytes < 0:
                                    return
                            key.data.sendall(chunk)
        except (ValueError, OSError):
            if not connected:
                self.send_error(403, "Browser destination blocked or unavailable")
        finally:
            self.close_connection = True
            self.server.slots.release()


@contextmanager
def browser_proxy(origins: list[str]):
    server = BrowserProxy(origins)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.stopped.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
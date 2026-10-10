#!/usr/bin/env python3
"""voice-journey's cutover responder: every request -> 308 to voice-lab.

Voice Journey moved to voice-lab (scshafe/voice-lab, docs/design/VOICE-PLATFORM.md
section 5.6 step 5, D-032). For 90 days after the cutover the old node
`voice-journey.<tailnet>.ts.net` keeps answering so bookmarks keep working:
every method and path gets `308 Permanent Redirect` to the same path and query
on the voice-lab origin. It serves no data and reads nothing.

Python standard library only (3.12). Configuration, all from the environment:

  REDIRECT_ORIGIN  required: the target origin, `https://<host>` (no path,
                   query, port or userinfo). The responder refuses to start
                   without a valid one. The request's Host header is never
                   used: the Location is always this origin + the request path.
  REDIRECT_HOST    comma-separated listen addresses (default 127.0.0.1).
  REDIRECT_PORT    listen port (default 8787).

`GET /healthz` answers 200 {"ok":true} (the deploy witness through the door).
`responder.py --healthcheck` probes that route on 127.0.0.1 (the container's
healthcheck) and exits 0 or 1.

Nothing about a request is logged: no path, query or header.
"""

from __future__ import annotations

import ipaddress
import os
import re
import signal
import socket
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# https:// + a lowercase DNS name (labels of [a-z0-9-], no leading/trailing
# hyphen), nothing after it but an optional trailing slash.
ORIGIN_RE = re.compile(
    r"^https://(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?/?$"
)
DEFAULT_PORT = 8787
BODY = b"Voice Journey moved to voice-lab.\n"
HEALTH_BODY = b'{"ok":true}'


class ConfigError(Exception):
    pass


def parse_origin(value: str | None) -> str:
    """The target origin without a trailing slash, or ConfigError."""
    if not value:
        raise ConfigError("REDIRECT_ORIGIN is not set")
    if len(value) > 256 or not ORIGIN_RE.match(value):
        raise ConfigError("REDIRECT_ORIGIN must be https://<lowercase host name>, with no path, port or query")
    return value.rstrip("/")


def parse_port(value: str | None) -> int:
    if value in (None, ""):
        return DEFAULT_PORT
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise ConfigError("REDIRECT_PORT must be a port number")
    return int(value)


def parse_hosts(value: str | None) -> list[str]:
    hosts = [h.strip() for h in (value or "127.0.0.1").split(",") if h.strip()]
    if not hosts:
        raise ConfigError("REDIRECT_HOST names no address")
    for h in hosts:
        try:
            ipaddress.ip_address(h)
        except ValueError:
            raise ConfigError("REDIRECT_HOST must be IP addresses, comma-separated") from None
    return hosts


def encode_target(target: str) -> str:
    """The request target as it may appear in a Location header.

    http.server decodes the request line as latin-1, so each character is one
    byte of the original. Bytes outside visible ASCII (controls, space, DEL,
    8-bit) are percent-encoded; everything else, existing %XX escapes
    included, passes through unchanged.
    """
    out = []
    for ch in target:
        b = ord(ch)
        if 0x21 <= b <= 0x7E:
            out.append(ch)
        else:
            out.append(f"%{b & 0xFF:02X}")
    return "".join(out)


def location_for(origin: str, target: str) -> str | None:
    """origin + the request's path and query, or None if the target is not origin-form.

    Only an origin-form target (one that starts with "/") is appended: an
    absolute-form target ("http://x/") or anything else could change the host
    the Location names (e.g. "@evil.example/" after the origin).
    """
    if not target.startswith("/"):
        return None
    return origin + encode_target(target)


class Handler(BaseHTTPRequestHandler):
    server_version = "voice-journey-redirect"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30  # seconds a client may take to send its request

    origin: str = ""  # set by make_handler

    def handle_one_request(self) -> None:
        # BaseHTTPRequestHandler dispatches to do_<METHOD> and answers 501 for
        # any method without one; every method gets the same answer here.
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            self.respond()
            self.wfile.flush()
        except TimeoutError:
            self.close_connection = True

    def respond(self) -> None:
        path = self.path.split("?", 1)[0]
        if self.command == "GET" and path == "/healthz":
            self.answer(HTTPStatus.OK, HEALTH_BODY, "application/json")
            return
        location = location_for(self.origin, self.path)
        if location is None:
            self.answer(HTTPStatus.BAD_REQUEST, b"Bad request.\n", "text/plain; charset=utf-8")
            return
        self.answer(HTTPStatus.PERMANENT_REDIRECT, BODY, "text/plain; charset=utf-8", location)

    def answer(self, status: HTTPStatus, body: bytes, ctype: str, location: str | None = None) -> None:
        # The request body (an upload, say) is never read: close the
        # connection after answering instead of draining it.
        self.close_connection = True
        self.send_response(status)
        if location is not None:
            self.send_header("Location", location)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # A rollback of the cutover must not be stuck in browsers' caches.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None) -> None:
        # A malformed request gets a fixed text: the stdlib's error page would
        # quote parts of the request back.
        self.log_message("%s", code)
        # A request line that failed to parse leaves the stdlib's default
        # version (HTTP/0.9, no status line or headers): answer as HTTP/1.0.
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
        try:
            status = HTTPStatus(code)
        except ValueError:
            status = HTTPStatus.BAD_REQUEST
        self.answer(status, b"Bad request.\n", "text/plain; charset=utf-8")

    # No request details in the logs (paths and queries may name recordings).
    def log_request(self, code="-", size="-") -> None:
        pass

    def log_message(self, format, *args) -> None:  # noqa: A002 (stdlib signature)
        sys.stderr.write("voice-journey-redirect: a malformed request was refused\n")


def make_handler(origin: str) -> type[Handler]:
    return type("RedirectHandler", (Handler,), {"origin": origin})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(origin: str, hosts: list[str], port: int) -> list[Server]:
    """Start one server per listen address, each on its own thread."""
    handler = make_handler(origin)
    servers = []
    for host in hosts:
        cls = Server
        if ipaddress.ip_address(host).version == 6:
            cls = type("Server6", (Server,), {"address_family": socket.AF_INET6})
        srv = cls((host, port), handler)
        threading.Thread(target=srv.serve_forever, name=f"serve-{host}", daemon=True).start()
        servers.append(srv)
    return servers


def healthcheck(port: int) -> int:
    import http.client

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/healthz")
        ok = conn.getresponse().status == 200
        conn.close()
    except OSError:
        return 1
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    try:
        port = parse_port(os.environ.get("REDIRECT_PORT"))
        if argv[1:] == ["--healthcheck"]:
            return healthcheck(port)
        if argv[1:]:
            raise ConfigError("usage: responder.py [--healthcheck]")
        origin = parse_origin(os.environ.get("REDIRECT_ORIGIN"))
        hosts = parse_hosts(os.environ.get("REDIRECT_HOST"))
    except ConfigError as e:
        sys.stderr.write(f"voice-journey-redirect: {e}\n")
        return 2
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    servers = serve(origin, hosts, port)
    sys.stderr.write(f"voice-journey-redirect: 308 to {origin} on {','.join(hosts)}:{port}\n")
    while not stop.wait(1):
        pass
    for srv in servers:
        srv.shutdown()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

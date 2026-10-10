"""Tests for the cutover responder (python3 -B -m unittest discover -s redirect)."""

from __future__ import annotations

import http.client
import os
import signal
import socket
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import responder  # noqa: E402

ORIGIN = "https://voice-lab.example-tailnet.ts.net"
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "responder.py")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Running(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servers = responder.serve(ORIGIN, ["127.0.0.1"], 0)
        cls.port = cls.servers[0].server_address[1]

    @classmethod
    def tearDownClass(cls):
        for s in cls.servers:
            s.shutdown()
            s.server_close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            conn.close()

    def raw(self, data: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(data)
            chunks = []
            while True:
                c = s.recv(65536)
                if not c:
                    break
                chunks.append(c)
        return b"".join(chunks)

    # --- the redirect --------------------------------------------------------
    def test_root_redirects(self):
        status, h, body = self.request("GET", "/")
        self.assertEqual(status, 308)
        self.assertEqual(h["Location"], ORIGIN + "/")
        self.assertEqual(body, responder.BODY)
        self.assertEqual(h["Cache-Control"], "no-store")

    def test_path_and_query_are_kept(self):
        status, h, _ = self.request("GET", "/journeys/42?take=a%20b&x=1")
        self.assertEqual(status, 308)
        self.assertEqual(h["Location"], ORIGIN + "/journeys/42?take=a%20b&x=1")

    def test_every_method_redirects(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "PROPFIND", "BREW"):
            with self.subTest(method=method):
                status, h, body = self.request(method, "/api/intake/sessions?id=7", body=b"x" * 1000)
                self.assertEqual(status, 308)
                self.assertEqual(h["Location"], ORIGIN + "/api/intake/sessions?id=7")
                self.assertEqual(body, responder.BODY)

    def test_head_has_no_body(self):
        status, h, body = self.request("HEAD", "/browse")
        self.assertEqual(status, 308)
        self.assertEqual(h["Location"], ORIGIN + "/browse")
        self.assertEqual(body, b"")

    def test_host_header_is_never_reflected(self):
        for host in ("evil.example", "voice-journey.example-tailnet.ts.net", "a@evil.example"):
            with self.subTest(host=host):
                status, h, _ = self.request("GET", "/x", headers={"Host": host})
                self.assertEqual(status, 308)
                self.assertEqual(h["Location"], ORIGIN + "/x")

    def test_forwarded_headers_are_ignored(self):
        _, h, _ = self.request("GET", "/x", headers={"X-Forwarded-Host": "evil.example",
                                                      "X-Forwarded-Proto": "http"})
        self.assertEqual(h["Location"], ORIGIN + "/x")

    def test_double_slash_stays_a_path(self):
        # http.server itself collapses a leading "//" to "/" (gh-87389); either
        # way the origin's host is the one the Location names.
        _, h, _ = self.request("GET", "//evil.example/x")
        self.assertIn(h["Location"], (ORIGIN + "//evil.example/x", ORIGIN + "/evil.example/x"))

    def test_absolute_form_target_is_refused(self):
        for target in (b"http://evil.example/", b"@evil.example/", b"*", b"evil.example"):
            with self.subTest(target=target):
                resp = self.raw(b"GET " + target + b" HTTP/1.1\r\nHost: x\r\n\r\n")
                head, _, body = resp.partition(b"\r\n\r\n")
                self.assertTrue(head.startswith(b"HTTP/1.1 400"), head)
                self.assertNotIn(b"Location", head)
                self.assertNotIn(b"evil", resp)
                self.assertEqual(body, b"Bad request.\n")

    def test_unsafe_bytes_are_percent_encoded(self):
        resp = self.raw(b"GET /a\x7fb\xe9c\x01 HTTP/1.1\r\nHost: x\r\n\r\n")
        head = resp.partition(b"\r\n\r\n")[0]
        self.assertTrue(head.startswith(b"HTTP/1.1 308"), head)
        self.assertIn(b"\r\nLocation: " + ORIGIN.encode() + b"/a%7Fb%E9c%01\r\n", head)

    def test_malformed_request_gets_fixed_text(self):
        resp = self.raw(b"GET /x HTTP/9.<script>\r\n\r\n")
        head, _, body = resp.partition(b"\r\n\r\n")
        self.assertIn(b" 400", head.split(b"\r\n")[0])
        self.assertEqual(body, b"Bad request.\n")
        self.assertNotIn(b"script", resp)

    def test_upload_body_is_not_read(self):
        # A large declared body: the answer comes without the body being sent.
        resp = self.raw(b"POST /api/intake/upload HTTP/1.1\r\nHost: x\r\n"
                        b"Content-Length: 4000000000\r\n\r\npartial")
        head = resp.partition(b"\r\n\r\n")[0]
        self.assertTrue(head.startswith(b"HTTP/1.1 308"), head)
        self.assertIn(b"Connection: close", head)

    # --- the health witness -------------------------------------------------
    def test_healthz(self):
        status, h, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"ok":true}')
        self.assertEqual(h["Content-Type"], "application/json")
        self.assertNotIn("Location", h)

    def test_healthz_only_for_get(self):
        for method in ("POST", "HEAD"):
            with self.subTest(method=method):
                status, h, _ = self.request(method, "/healthz")
                self.assertEqual(status, 308)
                self.assertEqual(h["Location"], ORIGIN + "/healthz")

    def test_healthz_is_exact(self):
        for path in ("/healthz/", "/healthzx", "/x/healthz"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 308)

    def test_healthcheck_mode(self):
        env = {"PATH": os.environ.get("PATH", ""), "REDIRECT_PORT": str(self.port)}
        ok = subprocess.run([sys.executable, "-B", SCRIPT, "--healthcheck"], env=env, timeout=10)
        self.assertEqual(ok.returncode, 0)
        env["REDIRECT_PORT"] = str(free_port())
        bad = subprocess.run([sys.executable, "-B", SCRIPT, "--healthcheck"], env=env, timeout=10)
        self.assertEqual(bad.returncode, 1)


class Config(unittest.TestCase):
    def test_origin_accepted(self):
        self.assertEqual(responder.parse_origin(ORIGIN), ORIGIN)
        self.assertEqual(responder.parse_origin(ORIGIN + "/"), ORIGIN)

    def test_origin_refused(self):
        for bad in (None, "", "http://voice-lab.x.ts.net", "https://voice-lab.x.ts.net/path",
                    "https://voice-lab.x.ts.net:8443", "https://u@voice-lab.x.ts.net",
                    "https://voice-lab.x.ts.net?q", "https://Voice-Lab.x.ts.net",
                    "voice-lab.x.ts.net", "https://-bad.x.ts.net", "https://x.ts.net//"):
            with self.subTest(bad=bad):
                with self.assertRaises(responder.ConfigError):
                    responder.parse_origin(bad)

    def test_hosts_and_port(self):
        self.assertEqual(responder.parse_hosts("10.253.86.11, 127.0.0.1"), ["10.253.86.11", "127.0.0.1"])
        self.assertEqual(responder.parse_hosts(None), ["127.0.0.1"])
        self.assertEqual(responder.parse_port(None), 8787)
        for bad in ("abc", "0", "70000", "-1"):
            with self.assertRaises(responder.ConfigError):
                responder.parse_port(bad)
        for bad in ("localhost", "0.0.0.0;x", ","):
            with self.assertRaises(responder.ConfigError):
                responder.parse_hosts(bad)

    def test_location_for(self):
        self.assertEqual(responder.location_for(ORIGIN, "/a?b=c"), ORIGIN + "/a?b=c")
        self.assertIsNone(responder.location_for(ORIGIN, "http://evil.example/"))
        self.assertIsNone(responder.location_for(ORIGIN, "@evil.example/"))
        self.assertIsNone(responder.location_for(ORIGIN, ""))


class Process(unittest.TestCase):
    def env(self, **extra):
        e = {"PATH": os.environ.get("PATH", "")}
        e.update(extra)
        return e

    def test_refuses_to_start_without_origin(self):
        for env in (self.env(), self.env(REDIRECT_ORIGIN="http://voice-lab.x.ts.net")):
            with self.subTest(env=env.get("REDIRECT_ORIGIN")):
                p = subprocess.run([sys.executable, "-B", SCRIPT], env=env, timeout=10,
                                   capture_output=True, text=True)
                self.assertEqual(p.returncode, 2)
                self.assertIn("REDIRECT_ORIGIN", p.stderr)

    def test_serves_and_stops_on_sigterm(self):
        port = free_port()
        p = subprocess.Popen([sys.executable, "-B", SCRIPT],
                             env=self.env(REDIRECT_ORIGIN=ORIGIN, REDIRECT_PORT=str(port)),
                             stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                    conn.request("GET", "/q?a=1", headers={"Host": "evil.example"})
                    r = conn.getresponse()
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.1)
            self.assertEqual(r.status, 308)
            self.assertEqual(r.getheader("Location"), ORIGIN + "/q?a=1")
            conn.close()
            p.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(timeout=10), 0)
            err = p.stderr.read()
            self.assertNotIn("/q", err)  # no request paths in the log
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stderr.close()


if __name__ == "__main__":
    unittest.main()

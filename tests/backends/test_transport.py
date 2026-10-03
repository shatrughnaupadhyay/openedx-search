"""Exercise actual local HTTP requests, authentication and safe failure handling."""

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from openedx_search.backends import BackendError, HTTPTransport


class TransportTests(unittest.TestCase):
    """The HTTP server is a protocol fixture, not a search engine."""

    def setUp(self):
        self.seen = []
        seen = self.seen

        class Handler(BaseHTTPRequestHandler):
            """Serve bounded JSON responses and intentional protocol failures."""

            def do_POST(self):
                """Record the request and serve its selected test response."""
                seen.append(
                    (
                        self.path,
                        dict(self.headers),
                        self.rfile.read(int(self.headers.get("Content-Length", 0))),
                    )
                )
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/leak")
                    self.end_headers()
                elif self.path == "/unavailable":
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"private indexed text")
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"success": true}\n' if self.path == "/import" else b'{"ok": true}')

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.transport = HTTPTransport(f"http://127.0.0.1:{self.server.server_port}", "private-key")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_authenticated_json_and_ndjson(self):
        self.assertEqual(
            self.transport.request("POST", "/json", body={"id": "1"}, engine="meilisearch"),
            {"ok": True},
        )
        self.assertEqual(self.seen[-1][1]["Authorization"], "Bearer private-key")
        self.assertEqual(
            self.transport.request("POST", "/import", body='{"id":"1"}', engine="typesense", ndjson=True),
            [{"success": True}],
        )
        self.assertEqual(self.seen[-1][1]["X-Typesense-Api-Key"], "private-key")

    def test_redirects_never_forward_credentials(self):
        with self.assertRaises(BackendError):
            self.transport.request("POST", "/redirect", body={}, engine="typesense")
        self.assertEqual(len(self.seen), 1)

    def test_response_size_limit_and_engine_validation(self):
        self.transport.max_response_bytes = 1
        with self.assertRaisesRegex(BackendError, "size limit"):
            self.transport.request("POST", "/json", body={}, engine="typesense")
        with self.assertRaises(ValueError):
            self.transport.request("POST", "/json", body={}, engine="unexpected")
        self.assertEqual(len(self.seen), 1)

    def test_server_failure_is_sanitized_and_retryable(self):
        with self.assertRaises(BackendError) as caught:
            self.transport.request("POST", "/unavailable", body={}, engine="meilisearch")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(str(caught.exception), "Engine HTTP status 503")

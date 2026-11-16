import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from maintenance.model_client import ProviderConfigurationError, ProviderLengthError, complete


class ProviderTests(unittest.TestCase):
    def test_length_finish_reason_is_recoverable_but_content_is_not_used(self):
        response = io.BytesIO(
            b'{"choices":[{"finish_reason":"length","message":{"content":"private partial"}}]}'
        )
        with patch("maintenance.model_client.open_model_request", return_value=response):
            with self.assertRaises(ProviderLengthError) as caught:
                complete([], key="test-only")
        self.assertNotIn("private partial", str(caught.exception))

    def test_direct_success_preserves_normal_model_response(self):
        response = io.BytesIO(
            b'{"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"{}"}}]}'
        )
        with patch("maintenance.model_client.open_model_request", return_value=response) as request:
            self.assertEqual(complete([], key="test-only")["content"], "{}")
            sent = request.call_args.args[0]
            self.assertEqual(sent.full_url, "https://api.deepseek.com/chat/completions")
            self.assertEqual(sent.get_header("Authorization"), "Bearer test-only")
            self.assertNotIn("thinking", json.loads(sent.data))
            self.assertNotIn("reasoning_effort", json.loads(sent.data))

    def test_thinking_can_be_disabled_for_short_copy_without_changing_defaults(self):
        response = io.BytesIO(b'{"choices":[{"finish_reason":"stop","message":{"content":"{}"}}]}')
        with patch("maintenance.model_client.open_model_request", return_value=response) as request:
            complete([], key="test-only", thinking="disabled")
            body = json.loads(request.call_args.args[0].data)
            self.assertEqual(body["thinking"], {"type": "disabled"})
            self.assertEqual(body["max_tokens"], 8000)

    def test_invalid_thinking_option_cannot_send_a_request(self):
        with patch("maintenance.model_client.open_model_request") as request:
            with self.assertRaises(ValueError):
                complete([], key="test-only", thinking="invented")
            request.assert_not_called()

    def test_payment_error_is_actionable_and_not_retried(self):
        error = HTTPError(
            "https://example.invalid", 402, "Payment Required", {}, io.BytesIO(b"private details")
        )
        with (
            patch("maintenance.model_client.open_model_request", side_effect=error) as request,
            patch("maintenance.model_client.time.sleep") as sleep,
        ):
            with self.assertRaises(ProviderConfigurationError) as caught:
                complete([], key="test-only")
            self.assertIn("402", str(caught.exception))
            self.assertNotIn("private details", str(caught.exception))
            self.assertEqual(request.call_count, 1)
            sleep.assert_not_called()

    def test_redirect_is_sanitized_and_not_retried(self):
        for status in (301, 302, 303, 307, 308):
            error = HTTPError(
                "https://example.invalid",
                status,
                "redirect",
                {"Location": "https://private.invalid"},
                io.BytesIO(b"private body"),
            )
            with (
                self.subTest(status=status),
                patch("maintenance.model_client.open_model_request", side_effect=error) as request,
                patch("maintenance.model_client.time.sleep") as sleep,
            ):
                with self.assertRaises(ProviderConfigurationError) as caught:
                    complete([], key="test-only")
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual(request.call_count, 1)
                sleep.assert_not_called()

    def test_real_http_redirect_never_reaches_destination(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        from urllib.request import Request

        from maintenance.model_client import open_model_request

        reached = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(int(self.path.strip("/")))
                self.send_header("Location", f"http://localhost:{self.server.server_port}/destination")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                reached.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for status in (301, 302, 303, 307, 308):
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/{status}",
                    data=b"{}",
                    headers={"Authorization": "Bearer test-only-not-a-key"},
                )
                with self.subTest(status=status), self.assertRaises(HTTPError) as caught:
                    open_model_request(request, timeout=3)
                caught.exception.close()
            self.assertEqual(reached, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

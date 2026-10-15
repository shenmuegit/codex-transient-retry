import gzip
import http.client
import importlib.util
import json
import tempfile
import threading
import time
import tomllib
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "retry_proxy.py"


class ProxyTests(unittest.TestCase):
    def load_proxy(self):
        self.assertTrue(SCRIPT.is_file(), "The retry proxy has not been implemented yet")
        spec = importlib.util.spec_from_file_location("retry_proxy", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_capacity_and_stream_errors_are_retried_before_output(self):
        proxy = self.load_proxy()
        complete = (
            b'data: {"type":"response.completed","response":{"status":"completed","output":[]}}\n\n'
        )
        capacity = b'data: {"type":"error","code":"model_capacity_exceeded","message":"Selected model is at capacity. Please try a different model."}\n\n'
        server_error = b'data: {"type":"response.failed","response":{"error":{"code":"server_error","message":"An error occurred while processing your request."}}}\n\n'
        responses = [capacity, server_error, complete]
        requests = []

        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(
                    (
                        self.path,
                        self.headers.get("Authorization"),
                        self.rfile.read(int(self.headers["Content-Length"])),
                    )
                )
                body = responses.pop(0)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with (
                patch.object(proxy, "UPSTREAM", f"http://127.0.0.1:{server.server_port}"),
                patch.object(proxy.time, "sleep") as sleep,
                patch.object(proxy, "log_event"),
            ):
                result = proxy.request_with_retry(
                    "POST",
                    "/backend-api/codex/responses",
                    b'{"stream":true}',
                    {"Authorization": "Bearer test-only"},
                )
            self.assertEqual(result[2], complete)
            self.assertEqual(len(requests), 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [30, 60])
            self.assertTrue(
                all(
                    r == ("/backend-api/codex/responses", "Bearer test-only", b'{"stream":true}')
                    for r in requests
                )
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_compaction_http_failures_and_retry_limit(self):
        proxy = self.load_proxy()
        failures = (
            503,
            {"Content-Type": "application/json"},
            b'{"error":{"code":"server_is_overloaded"}}',
        )
        success = (200, {"Content-Type": "application/json"}, b'{"output":[]}')
        with (
            patch.object(proxy, "fetch", side_effect=[failures, success]) as fetch,
            patch.object(proxy.time, "sleep") as sleep,
            patch.object(proxy, "log_event"),
        ):
            self.assertEqual(
                proxy.request_with_retry("POST", "/backend-api/codex/responses/compact", b"{}", {})[
                    0
                ],
                200,
            )
            self.assertEqual(fetch.call_count, 2)
            sleep.assert_called_once_with(30)
        with (
            patch.object(proxy, "fetch", return_value=failures) as fetch,
            patch.object(proxy.time, "sleep") as sleep,
            patch.object(proxy, "log_event"),
        ):
            result = proxy.request_with_retry(
                "POST", "/backend-api/codex/responses/compact", b"{}", {}
            )
            self.assertEqual(result[0], 400)
            self.assertEqual(json.loads(result[2])["error"]["code"], "local_retry_limit_exceeded")
            self.assertEqual(fetch.call_count, 4)
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [30, 60, 120])

    def test_upstream_retry_buffer_507_recovers_for_model_and_compaction(self):
        proxy = self.load_proxy()
        message = "exceeded request buffer limit while retrying upstream"
        success = {
            "/backend-api/codex/responses": (
                "text/event-stream",
                b'data: {"type":"response.completed"}\n\n',
            ),
            "/backend-api/codex/responses/compact": ("application/json", b'{"output":[]}'),
        }
        requests = {path: [] for path in success}

        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                requests[self.path].append(
                    (
                        self.headers.get("Authorization"),
                        self.rfile.read(int(self.headers["Content-Length"])),
                    )
                )
                if len(requests[self.path]) == 1:
                    status = 507
                    content_type = "text/plain"
                    body = message.encode()
                    if self.path.endswith("/compact"):
                        content_type = "application/json"
                        body = json.dumps({"error": {"message": message.upper()}}).encode()
                else:
                    status = 200
                    content_type, body = success[self.path]
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for path in success:
                with (
                    self.subTest(path=path),
                    patch.object(proxy, "UPSTREAM", f"http://127.0.0.1:{server.server_port}"),
                    patch.object(proxy.time, "sleep") as sleep,
                    patch.object(proxy, "log_event"),
                ):
                    result = proxy.request_with_retry(
                        "POST", path, b"{}", {"Authorization": "Bearer test-only"}
                    )
                    self.assertEqual(result[0], 200)
                    self.assertEqual(result[2], success[path][1])
                    self.assertEqual(requests[path], [("Bearer test-only", b"{}")] * 2)
                    sleep.assert_called_once_with(30)
        finally:
            server.shutdown()
            server.server_close()

    def test_upstream_retry_buffer_507_sse_errors_recover(self):
        proxy = self.load_proxy()
        message = "exceeded request buffer limit while retrying upstream"
        events = [
            {"type": "error", "error": {"message": message}},
            {"type": "response.failed", "response": {"error": {"message": message.upper()}}},
        ]
        success = (
            200,
            {"Content-Type": "text/event-stream"},
            b'data: {"type":"response.completed"}\n\n',
        )
        for event in events:
            for status in (507, 200):
                failure = (
                    status,
                    {"Content-Type": "text/event-stream"},
                    ("data: " + json.dumps(event) + "\n\n").encode(),
                )
                with (
                    self.subTest(event=event["type"], status=status),
                    patch.object(proxy, "fetch", side_effect=[failure, success]) as fetch,
                    patch.object(proxy.time, "sleep") as sleep,
                    patch.object(proxy, "log_event"),
                ):
                    result = proxy.request_with_retry(
                        "POST", "/backend-api/codex/responses", b"{}", {}
                    )
                    if status == 507:
                        self.assertEqual(result, success)
                        self.assertEqual(fetch.call_count, 2)
                        sleep.assert_called_once_with(30)
                    else:
                        self.assertEqual(result, failure)
                        self.assertEqual(fetch.call_count, 1)
                        sleep.assert_not_called()

    def test_upstream_retry_buffer_507_uses_existing_limit_and_delays(self):
        proxy = self.load_proxy()
        failure = (
            507,
            {"X-Request-ID": "test-only-request"},
            b"exceeded request buffer limit while retrying upstream",
        )
        with (
            patch.object(proxy, "fetch", return_value=failure) as fetch,
            patch.object(proxy.time, "sleep") as sleep,
            patch.object(proxy, "log_event"),
        ):
            result = proxy.request_with_retry("POST", "/backend-api/codex/responses", b"{}", {})
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [30, 60, 120])
        self.assertEqual(result[0], 400)
        self.assertEqual(result[1]["X-Request-ID"], "test-only-request")
        error = json.loads(result[2])
        self.assertEqual(error["error"]["code"], "local_retry_limit_exceeded")
        self.assertEqual(error["attempts"], 4)
        self.assertEqual(error["upstream_status"], 507)
        self.assertIn("upstream_retry_buffer_limit", error["error"]["message"])

    def test_upstream_retry_buffer_rule_preserves_non_retryable_results(self):
        proxy = self.load_proxy()
        message = "exceeded request buffer limit while retrying upstream"
        cases = [
            ("POST", "/backend-api/codex/responses", (507, {}, b"Insufficient Storage")),
            (
                "POST",
                "/backend-api/codex/responses",
                (507, {}, b"Local retry buffer limit exceeded"),
            ),
            ("POST", "/backend-api/codex/responses", (400, {}, message.encode())),
            ("POST", "/backend-api/codex/responses", (401, {}, message.encode())),
            (
                "POST",
                "/backend-api/codex/responses",
                (
                    507,
                    {},
                    json.dumps(
                        {"error": {"code": "insufficient_quota", "message": message}}
                    ).encode(),
                ),
            ),
            (
                "POST",
                "/backend-api/codex/responses",
                (200, {}, json.dumps({"output": [{"text": message}]}).encode()),
            ),
        ]
        for event in (
            {"type": "error", "error": {"code": "insufficient_quota", "message": message}},
            {
                "type": "response.failed",
                "response": {"error": {"code": "insufficient_quota", "message": message}},
            },
        ):
            cases.append(
                (
                    "POST",
                    "/backend-api/codex/responses",
                    (
                        507,
                        {"Content-Type": "text/event-stream"},
                        ("data: " + json.dumps(event) + "\n\n").encode(),
                    ),
                )
            )
        for method, path, failure in cases:
            with (
                self.subTest(method=method, status=failure[0], body=failure[2]),
                patch.object(proxy, "fetch", return_value=failure) as fetch,
                patch.object(proxy.time, "sleep") as sleep,
                patch.object(proxy, "log_event"),
            ):
                result = proxy.request_with_retry(
                    method, path, b"{}" if method == "POST" else None, {}
                )
                self.assertEqual(result, failure)
                self.assertEqual(fetch.call_count, 1)
                sleep.assert_not_called()

    def test_auth_quota_and_token_limits_do_not_retry(self):
        proxy = self.load_proxy()
        cases = [
            (401, {}, b'{"error":{"code":"invalid_api_key"}}'),
            (403, {}, b'{"error":{"message":"An error occurred while processing your request."}}'),
            (429, {}, b'{"error":{"code":"insufficient_quota"}}'),
            (
                200,
                {"Content-Type": "text/event-stream"},
                b'data: {"type":"response.incomplete","response":{"incomplete_details":{"reason":"max_output_tokens"}}}\n\n',
            ),
            (
                200,
                {"Content-Type": "text/event-stream"},
                b'data: {"type":"error","code":"context_length_exceeded","message":"context too long"}\n\n',
            ),
        ]
        for result in cases:
            with (
                self.subTest(result=result),
                patch.object(proxy, "fetch", return_value=result) as fetch,
                patch.object(proxy.time, "sleep") as sleep,
                patch.object(proxy, "log_event"),
            ):
                self.assertEqual(
                    proxy.request_with_retry("POST", "/backend-api/codex/responses", b"{}", {}),
                    result,
                )
                self.assertEqual(fetch.call_count, 1)
                sleep.assert_not_called()

    def test_truncated_stream_and_network_disconnect_retry(self):
        proxy = self.load_proxy()
        self.assertEqual(
            proxy.retry_reason(
                200, {"Content-Type": "text/event-stream"}, b'data: {"type":"response.created"}\n\n'
            ),
            "stream_disconnected",
        )
        with (
            patch.object(proxy, "fetch", side_effect=urllib.error.URLError("reset")) as fetch,
            patch.object(proxy.time, "sleep"),
            patch.object(proxy, "log_event"),
        ):
            result = proxy.request_with_retry("POST", "/backend-api/codex/responses", b"{}", {})
            self.assertEqual(result[0], 400)
            self.assertEqual(fetch.call_count, 4)

    def test_truncated_gzip_response_is_retried(self):
        proxy = self.load_proxy()
        complete = b'data: {"type":"response.completed"}\n\n'
        compressed = gzip.compress(complete)
        invalid_deflate = compressed[:10] + b"\x06" + compressed[11:]
        replies = [compressed[:-8], invalid_deflate, compressed]
        calls = []

        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                calls.append(self.path)
                body = replies.pop(0)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        delays = []
        try:
            with (
                patch.object(proxy, "UPSTREAM", f"http://127.0.0.1:{server.server_port}"),
                patch.object(proxy, "log_event"),
            ):
                result = proxy.request_with_retry(
                    "POST", "/backend-api/codex/responses", b"{}", {}, pause=delays.append
                )
            self.assertEqual(result[2], complete)
            self.assertEqual(len(calls), 3)
            self.assertEqual(delays, [30, 60])
        finally:
            server.shutdown()
            server.server_close()

    def test_truncated_compaction_json_is_retried(self):
        proxy = self.load_proxy()
        replies = [b'{"output":[', b'{"output":[]}']
        calls = []

        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                calls.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(replies.pop(0))

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with (
                patch.object(proxy, "UPSTREAM", f"http://127.0.0.1:{server.server_port}"),
                patch.object(proxy, "log_event"),
            ):
                result = proxy.request_with_retry(
                    "POST", "/backend-api/codex/responses/compact", b"{}", {}, pause=lambda _: None
                )
            self.assertEqual(result[2], b'{"output":[]}')
            self.assertEqual(len(calls), 2)
        finally:
            server.shutdown()
            server.server_close()

    def test_explicit_capacity_error_with_http_400_is_retried(self):
        proxy = self.load_proxy()
        failure = (
            400,
            {"Content-Type": "application/json"},
            b'{"error":{"code":"model_capacity_exceeded"}}',
        )
        success = (200, {"Content-Type": "application/json"}, b'{"output":[]}')
        with (
            patch.object(proxy, "fetch", side_effect=[failure, success]) as fetch,
            patch.object(proxy, "log_event"),
        ):
            self.assertEqual(
                proxy.request_with_retry(
                    "POST", "/backend-api/codex/responses/compact", b"{}", {}, pause=lambda _: None
                ),
                success,
            )
            self.assertEqual(fetch.call_count, 2)
        invalid = (400, {}, b'{"error":{"code":"invalid_request_error"}}')
        with (
            patch.object(proxy, "fetch", return_value=invalid) as fetch,
            patch.object(proxy, "log_event"),
        ):
            self.assertEqual(
                proxy.request_with_retry(
                    "POST",
                    "/backend-api/codex/responses/compact",
                    b"{}",
                    {},
                    pause=lambda _: self.fail("Invalid requests must not retry"),
                ),
                invalid,
            )
            self.assertEqual(fetch.call_count, 1)

    def test_successful_text_containing_capacity_is_not_retried(self):
        proxy = self.load_proxy()
        body = b'data: {"type":"response.output_text.delta","delta":"Selected model is at capacity"}\n\ndata: {"type":"response.completed"}\n\n'
        self.assertIsNone(proxy.retry_reason(200, {"Content-Type": "text/event-stream"}, body))

    def test_retry_after_is_honored(self):
        proxy = self.load_proxy()
        result = (503, {"Retry-After": "45"}, b"busy")
        success = (200, {"Content-Type": "application/json"}, b"{}")
        with (
            patch.object(proxy, "fetch", side_effect=[result, success]),
            patch.object(proxy.time, "sleep") as sleep,
            patch.object(proxy, "log_event"),
        ):
            proxy.request_with_retry("POST", "/backend-api/codex/responses/compact", b"{}", {})
            sleep.assert_called_once_with(45)

    def test_cancellation_prevents_new_requests(self):
        proxy = self.load_proxy()
        with patch.object(proxy, "fetch") as fetch:
            with self.assertRaises(proxy.ClientDisconnected):
                proxy.request_with_retry(
                    "POST", "/backend-api/codex/responses", b"{}", {}, cancelled=lambda: True
                )
            fetch.assert_not_called()

    def test_sse_event_name_without_type_is_recognized(self):
        proxy = self.load_proxy()
        body = b'event: error\ndata: {"code":"context_length_exceeded","message":"Too long"}\n\n'
        self.assertIsNone(proxy.retry_reason(200, {"Content-Type": "text/event-stream"}, body))

    def test_sse_errors_without_content_type_are_recognized(self):
        proxy = self.load_proxy()
        for code in ("model_capacity_exceeded", "server_error"):
            with self.subTest(code=code):
                body = ("data: " + json.dumps({"type": "error", "code": code}) + "\n\n").encode()
                self.assertEqual(proxy.retry_reason(200, {}, body), code)

    def test_malformed_failed_response_is_retried(self):
        for response in (None, [], "invalid", 0):
            with self.subTest(response=response):
                proxy = self.load_proxy()
                malformed = (
                    "data: "
                    + json.dumps({"type": "response.failed", "response": response})
                    + "\n\n"
                ).encode()
                complete = (200, {}, b'data: {"type":"response.completed"}\n\n')
                with (
                    patch.object(
                        proxy, "fetch", side_effect=[(200, {}, malformed), complete]
                    ) as fetch,
                    patch.object(proxy, "log_event"),
                ):
                    result = proxy.request_with_retry(
                        "POST", "/backend-api/codex/responses", b"{}", {}, pause=lambda _: None
                    )
                    self.assertEqual(result, complete)
                    self.assertEqual(fetch.call_count, 2)

    def test_sse_bom_and_cr_line_endings(self):
        proxy = self.load_proxy()
        cases = [
            (b'\xef\xbb\xbfdata: {"type":"error","code":"invalid_api_key"}\n\n', None),
            (
                b'\xef\xbb\xbfdata: {"type":"error","code":"model_capacity_exceeded"}\n\ndata: [DONE]\n\n',
                "model_capacity_exceeded",
            ),
            (b'data: {"type":"response.created"}\r\rdata: {"type":"response.completed"}\r\r', None),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(
                    proxy.retry_reason(200, {"Content-Type": "text/event-stream"}, body), expected
                )

    def test_invalid_utf8_is_retried_without_releasing_corrupted_output(self):
        proxy = self.load_proxy()
        corrupted = b'data: {"type":"response.output_text.delta","delta":"\xff"}\n\ndata: {"type":"response.completed"}\n\n'
        complete = b'data: {"type":"response.completed"}\n\n'
        with (
            patch.object(
                proxy,
                "fetch",
                side_effect=[
                    (200, {"Content-Type": "text/event-stream"}, corrupted),
                    (200, {"Content-Type": "text/event-stream"}, complete),
                ],
            ) as fetch,
            patch.object(proxy, "log_event"),
        ):
            result = proxy.request_with_retry(
                "POST", "/backend-api/codex/responses", b"{}", {}, pause=lambda _: None
            )
            self.assertEqual(result[2], complete)
            self.assertEqual(fetch.call_count, 2)

    def test_not_modified_model_catalog_is_preserved(self):
        proxy = self.load_proxy()

        class NotModified(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(304)
                self.send_header("ETag", '"test-models"')
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), NotModified)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with patch.object(proxy, "UPSTREAM", f"http://127.0.0.1:{server.server_port}"):
                result = proxy.fetch("GET", "/backend-api/codex/models", None, {}, 2)
            self.assertEqual(result[0], 304)
            self.assertEqual(result[1]["ETag"], '"test-models"')
        finally:
            server.shutdown()
            server.server_close()

    def test_compressed_model_catalog_is_decoded_for_the_client(self):
        proxy = self.load_proxy()
        catalog = b'{"models":[{"slug":"test-model"}]}'

        class CompressedCatalog(BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.encoding = self.headers.get("Accept-Encoding")
                body = gzip.compress(catalog)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), CompressedCatalog)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with patch.object(proxy, "UPSTREAM", f"http://127.0.0.1:{server.server_port}"):
                result = proxy.fetch("GET", "/backend-api/codex/models", None, {}, 2)
            self.assertEqual(result[2], catalog)
            self.assertNotIn("Content-Encoding", result[1])
            self.assertEqual(server.encoding, "gzip")
        finally:
            server.shutdown()
            server.server_close()

    def test_proxy_does_not_release_partial_tool_calls_on_retry(self):
        proxy = self.load_proxy()
        partial = b'data: {"type":"response.output_item.done","item":{"type":"function_call","name":"do_not_execute"}}\n\ndata: {"type":"error","code":"server_error"}\n\n'
        complete = b'data: {"type":"response.completed"}\n\n'
        server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            results = [
                (200, {"Content-Type": "text/event-stream"}, partial),
                (200, {"Content-Type": "text/event-stream"}, complete),
            ]
            with (
                patch.object(proxy, "fetch", side_effect=results),
                patch.object(proxy.time, "sleep"),
                patch.object(proxy.Handler, "pause"),
                patch.object(proxy, "log_event"),
            ):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/backend-api/codex/responses",
                    data=b"{}",
                    headers={"Authorization": "Bearer test-only"},
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    self.assertEqual(response.read(), complete)
        finally:
            server.shutdown()
            server.server_close()

    def test_config_backup_idempotence_and_rollback_preserve_other_edits(self):
        proxy = self.load_proxy()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            original = 'model = "test-model"\n[features]\nmemories = true\n'
            config.write_text(original, encoding="utf-8")
            result = proxy.configure(config, True)
            self.assertEqual(Path(result["backup"]).read_text(encoding="utf-8"), original)
            self.assertIn("requires_openai_auth = true", config.read_text())
            self.assertIn(
                'openai_base_url = "http://127.0.0.1:18761/backend-api/codex"', config.read_text()
            )
            self.assertEqual(
                tomllib.loads(config.read_text()).get("model_provider", "openai"), "openai"
            )
            self.assertTrue(proxy.configure(config, True)["unchanged"])
            config.write_text(config.read_text().replace("memories = true", "memories = false"))
            proxy.configure(config, False)
            self.assertEqual(
                config.read_text(), original.replace("memories = true", "memories = false")
            )

    def test_missing_config_can_be_enabled_and_preserves_later_edits(self):
        proxy = self.load_proxy()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "new-home" / "config.toml"
            result = proxy.configure(config, True)
            self.assertEqual(Path(result["backup"]).read_bytes(), b"")
            config.write_text(config.read_text() + "[features]\nmemories = false\n")
            proxy.configure(config, False)
            self.assertEqual(config.read_text(), "[features]\nmemories = false\n")

    def test_disabling_a_missing_config_does_not_create_files(self):
        proxy = self.load_proxy()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "new-home" / "config.toml"
            self.assertTrue(proxy.configure(config, False)["unchanged"])
            self.assertFalse(config.parent.exists())

    def test_legacy_setup_is_upgraded_without_losing_original_backup(self):
        proxy = self.load_proxy()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            original = 'model = "test-model"\n'
            config.write_text(original)
            proxy.configure(config, True)
            config.write_text(
                config.read_text().replace(proxy.CONFIG_HEADER, proxy.LEGACY_CONFIG_HEADER, 1)
            )
            result = proxy.configure(config, True)
            self.assertIn(
                'openai_base_url = "http://127.0.0.1:18761/backend-api/codex"', config.read_text()
            )
            self.assertEqual(Path(result["backup"]).read_text(), original)
            proxy.configure(config, False)
            self.assertEqual(config.read_text(), original)

    def test_existing_user_base_url_is_not_silently_replaced(self):
        proxy = self.load_proxy()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            original = 'openai_base_url = "https://example.invalid/v1"\n'
            config.write_text(original)
            with self.assertRaises(ValueError):
                proxy.configure(config, True)
            self.assertEqual(config.read_text(), original)

    def test_websocket_upgrade_is_declined_for_native_http_fallback(self):
        proxy = self.load_proxy()
        server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/backend-api/codex/responses",
                headers={
                    "Connection": "Upgrade",
                    "Upgrade": "websocket",
                    "Authorization": "Bearer test-only",
                },
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(error.exception.code, 426)
        finally:
            server.shutdown()
            server.server_close()

    def test_configuration_rejects_replacing_a_different_provider(self):
        proxy = self.load_proxy()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            original = 'model_provider = "existing-company-provider"\n'
            config.write_text(original)
            with self.assertRaises(ValueError):
                proxy.configure(config, True)
            self.assertEqual(config.read_text(), original)

    def test_catalog_cache_is_reused_only_for_the_same_credential(self):
        proxy = self.load_proxy()
        result = (200, {"Content-Type": "application/json"}, b'{"models":[]}')
        with (
            patch.object(proxy, "fetch", return_value=result) as fetch,
            patch.object(proxy, "log_event"),
        ):
            proxy.request_with_retry(
                "GET", "/backend-api/codex/models", None, {"Authorization": "Bearer test-one"}
            )
            proxy.request_with_retry(
                "GET", "/backend-api/codex/models", None, {"Authorization": "Bearer test-one"}
            )
            self.assertEqual(fetch.call_count, 1)
            proxy.request_with_retry(
                "GET", "/backend-api/codex/models", None, {"Authorization": "Bearer test-two"}
            )
            self.assertEqual(fetch.call_count, 2)

    def test_catalog_cache_is_isolated_by_account_header(self):
        proxy = self.load_proxy()
        first = (200, {}, b'{"models":[{"slug":"account-one"}]}')
        second = (200, {}, b'{"models":[{"slug":"account-two"}]}')
        with (
            patch.object(proxy, "fetch", side_effect=[first, second]) as fetch,
            patch.object(proxy, "log_event"),
        ):
            one = proxy.request_with_retry(
                "GET",
                "/backend-api/codex/models",
                None,
                {"Authorization": "Bearer test-only", "ChatGPT-Account-ID": "one"},
            )
            two = proxy.request_with_retry(
                "GET",
                "/backend-api/codex/models",
                None,
                {"Authorization": "Bearer test-only", "chatgpt-account-id": "two"},
            )
            self.assertNotEqual(one[2], two[2])
            self.assertEqual(fetch.call_count, 2)

    def test_catalog_cache_refresh_during_hit_keeps_the_matching_account(self):
        proxy = self.load_proxy()
        first = (200, {}, b'{"models":[{"slug":"account-one"}]}')
        second = (200, {}, b'{"models":[{"slug":"account-two"}]}')
        proxy.MODEL_CATALOG_CACHE = (
            ("/backend-api/codex/models", "Bearer test-only", "one"),
            200,
            first,
        )

        def concurrent_refresh():
            proxy.MODEL_CATALOG_CACHE = (
                ("/backend-api/codex/models", "Bearer test-only", "two"),
                200,
                second,
            )
            return 100

        with (
            patch.object(proxy.time, "monotonic", side_effect=concurrent_refresh),
            patch.object(proxy, "fetch") as fetch,
        ):
            result = proxy.request_with_retry(
                "GET",
                "/backend-api/codex/models",
                None,
                {"Authorization": "Bearer test-only", "ChatGPT-Account-ID": "one"},
            )
            self.assertEqual(result, first)
            fetch.assert_not_called()

    def test_invalid_catalog_is_not_cached(self):
        for bad in (b'{"models":[', b'{"output":[]}', b"null"):
            with self.subTest(body=bad):
                proxy = self.load_proxy()
                first = (200, {"Content-Type": "application/json"}, bad)
                valid = (200, {"Content-Type": "application/json"}, b'{"models":[]}')
                with (
                    patch.object(proxy, "fetch", side_effect=[first, valid]) as fetch,
                    patch.object(proxy, "log_event"),
                ):
                    proxy.request_with_retry(
                        "GET",
                        "/backend-api/codex/models",
                        None,
                        {"Authorization": "Bearer test-only"},
                        pause=lambda _: None,
                    )
                    result = proxy.request_with_retry(
                        "GET",
                        "/backend-api/codex/models",
                        None,
                        {"Authorization": "Bearer test-only"},
                    )
                    self.assertEqual(result, valid)
                    self.assertEqual(fetch.call_count, 2)

    def test_model_refresh_times_out_quickly_without_shortening_inference(self):
        proxy = self.load_proxy()

        class SlowUpstream(BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(0.2)
                try:
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.do_GET()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowUpstream)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with (
                patch.object(proxy, "UPSTREAM", f"http://127.0.0.1:{server.server_port}"),
                patch.object(proxy, "MODEL_CATALOG_TIMEOUT", 0.05, create=True),
                patch.object(proxy, "log_event"),
            ):
                result = proxy.request_with_retry(
                    "GET",
                    "/backend-api/codex/models",
                    None,
                    {},
                    pause=lambda _: None,
                )
                self.assertEqual(result[0], 400)
                self.assertEqual(json.loads(result[2])["attempts"], 4)
                self.assertEqual(
                    proxy.request_with_retry(
                        "POST", "/backend-api/codex/responses/compact", b"{}", {}
                    )[0],
                    200,
                )
        finally:
            server.shutdown()
            server.server_close()

    def test_explicit_builtin_provider_remains_selected_and_rolls_back(self):
        proxy = self.load_proxy()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            original = 'model_provider = "openai"\nmodel = "test-model"\n'
            config.write_text(original)
            proxy.configure(config, True)
            self.assertEqual(tomllib.loads(config.read_text())["model_provider"], "openai")
            self.assertTrue(proxy.configure(config, True)["unchanged"])
            proxy.configure(config, False)
            self.assertEqual(config.read_text(), original)

    def test_previous_header_upgrades_back_to_original_builtin_provider(self):
        proxy = self.load_proxy()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            original = 'model_provider = "openai"\nmodel = "test-model"\n'
            config.write_text(original)
            setup = proxy.configure(config, True)
            previous_header = '# BEGIN CODEX TRANSIENT RETRY\nmodel_provider = "openai_transient_retry"\nopenai_base_url = "http://127.0.0.1:18761/backend-api/codex"\n# END CODEX TRANSIENT RETRY HEADER\n'
            config.write_text(previous_header + 'model = "test-model"\n' + proxy.CONFIG_BLOCK)
            state_path = Path(directory) / "transient-retry" / "config-state.json"
            state = json.loads(state_path.read_text())
            state["old_provider_line"] = 'model_provider = "openai"\n'
            state_path.write_text(json.dumps(state))
            proxy.configure(config, True)
            self.assertEqual(tomllib.loads(config.read_text())["model_provider"], "openai")
            self.assertEqual(Path(setup["backup"]).read_text(), original)
            proxy.configure(config, False)
            self.assertEqual(config.read_text(), original)

    def test_all_paths_and_methods_retry_and_preserve_the_request(self):
        proxy = self.load_proxy()
        calls = []
        delays = []

        class Upstream(BaseHTTPRequestHandler):
            def respond(self):
                payload = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                key = (self.command, self.path)
                previous = sum(call[:2] == key for call in calls)
                calls.append((self.command, self.path, payload, self.headers.get("Authorization")))
                status = 503 if previous == 0 else 200
                body = b"temporary" if previous == 0 else b"\x89PNG\r\n\x1a\nvalid"
                self.send_response(status)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Test-Response", "preserved")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = respond
            do_HEAD = do_OPTIONS = do_PROPFIND = respond

            def log_message(self, *args):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        for service in (upstream, server):
            threading.Thread(target=service.serve_forever, daemon=True).start()
        cases = [
            ("GET", "/backend-api/codex/models?test=1"),
            ("POST", "/backend-api/codex/images/generations?test=1"),
            ("POST", "/backend-api/codex/images/edits?test=1"),
            ("GET", "/backend-api/wham/usage?test=1"),
            ("PUT", "/future-feature?test=1"),
            ("PATCH", "/backend-api/codex/future-feature?test=1"),
            ("DELETE", "/future-feature?test=1"),
            ("HEAD", "/future-feature?test=1"),
            ("OPTIONS", "/future-feature?test=1"),
            ("PROPFIND", "/future-feature?test=1"),
        ]
        try:
            with (
                patch.object(proxy, "UPSTREAM", f"http://127.0.0.1:{upstream.server_port}"),
                patch.object(proxy.Handler, "pause", side_effect=delays.append),
                patch.object(proxy, "log_event"),
            ):
                for method, path in cases:
                    with self.subTest(method=method, path=path):
                        connection = http.client.HTTPConnection(
                            "127.0.0.1", server.server_port, timeout=3
                        )
                        try:
                            connection.request(
                                method,
                                path,
                                body=b"\x00test-payload\xff",
                                headers={"Authorization": "Bearer test-only"},
                            )
                            response = connection.getresponse()
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.headers["X-Test-Response"], "preserved")
                            self.assertEqual(response.headers["Content-Length"], "13")
                            self.assertEqual(
                                response.read(),
                                b"" if method == "HEAD" else b"\x89PNG\r\n\x1a\nvalid",
                            )
                        finally:
                            connection.close()
                self.assertEqual(delays, [30] * len(cases))
                self.assertEqual(
                    calls,
                    [
                        (method, path, b"\x00test-payload\xff", "Bearer test-only")
                        for method, path in cases
                        for _ in range(2)
                    ],
                )
        finally:
            for service in (server, upstream):
                service.shutdown()
                service.server_close()

    def test_non_model_success_bodies_and_errors_are_not_misclassified(self):
        proxy = self.load_proxy()
        for status, content_type, body in [
            (200, "image/png", b"\x89PNG\r\n\x1a\nvalid"),
            (200, "text/plain", b"okay"),
            (200, "application/json", b'["okay"]'),
            (201, "application/json", b'{"created":true}'),
            (200, "application/json", b""),
            (204, "application/json", b""),
            (
                200,
                "text/event-stream",
                b'data: {"type":"response.failed","response":null}\n\n',
            ),
            (404, "text/plain", b"upstream route is unavailable"),
        ]:
            with self.subTest(status=status, content_type=content_type):
                result = (status, {"Content-Type": content_type}, body)
                with (
                    patch.object(proxy, "fetch", return_value=result) as fetch,
                    patch.object(proxy, "log_event"),
                ):
                    actual = proxy.request_with_retry(
                        "POST",
                        "/future-feature",
                        b"{}",
                        {},
                        pause=lambda _: self.fail("A valid or terminal response must not retry"),
                    )
                self.assertEqual(actual, result)
                self.assertEqual(fetch.call_count, 1)

    def test_all_methods_use_the_same_retry_budget_for_generic_failures(self):
        proxy = self.load_proxy()
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "PROPFIND"):
            for failure in (
                (507, {}, b"exceeded request buffer limit while retrying upstream"),
                (503, {"Content-Type": "text/event-stream"}, b"data: temporary\n\n"),
                (
                    503,
                    {"Content-Type": "text/event-stream"},
                    b'data: {"type":"error","code":"unknown_temporary_error"}\n\n',
                ),
                (
                    503,
                    {"Content-Type": "text/event-stream"},
                    b'data: {"type":"error","error":null}\n\n',
                ),
                (
                    503,
                    {"Content-Type": "text/event-stream"},
                    b'data: {"type":"response.failed","response":{"error":null}}\n\n',
                ),
                (
                    503,
                    {"Content-Type": "text/event-stream"},
                    b'data: {"type":"response.failed","response":null}\n\n',
                ),
                (
                    503,
                    {"Content-Type": "text/event-stream"},
                    b'data: {"type":"response.incomplete"}\n\n',
                ),
            ):
                delays = []
                with (
                    self.subTest(method=method, failure=failure),
                    patch.object(proxy, "fetch", return_value=failure) as fetch,
                    patch.object(proxy, "log_event"),
                ):
                    result = proxy.request_with_retry(
                        method, "/future-feature", b"{}", {}, pause=delays.append
                    )
                self.assertEqual(result[0], 400)
                self.assertEqual(json.loads(result[2])["attempts"], 4)
                self.assertEqual(fetch.call_count, 4)
                self.assertEqual(delays, [30, 60, 120])

    def test_bare_paths_do_not_use_codex_validation_or_catalog_cache(self):
        proxy = self.load_proxy()
        for method, path, body in (
            ("POST", "/responses", b"okay"),
            ("POST", "/responses/compact", b"okay"),
            ("GET", "/models", b'{"models":[]}'),
        ):
            result = (200, {}, body)
            with (
                self.subTest(method=method, path=path),
                patch.object(proxy, "fetch", return_value=result) as fetch,
                patch.object(proxy, "log_event"),
            ):
                for _ in range(2):
                    self.assertEqual(
                        proxy.request_with_retry(
                            method,
                            path,
                            None,
                            {},
                            pause=lambda _: self.fail("A bare path must not use model validation"),
                        ),
                        result,
                    )
                self.assertEqual(fetch.call_count, 2)

    def test_image_routes_preserve_auth_origin_and_size_guards(self):
        proxy = self.load_proxy()
        server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        cases = [
            ("POST", {}, 401),
            (
                "POST",
                {"Authorization": "Bearer test-only", "Origin": "https://example.invalid"},
                401,
            ),
            (
                "POST",
                {"Authorization": "Bearer test-only", "Content-Length": str(proxy.MAX_BODY + 1)},
                413,
            ),
        ]
        try:
            for path in ("/images/generations", "/images/edits"):
                for method, headers, expected in cases:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_port}/backend-api/codex{path}",
                        data=b"{}" if method == "POST" else None,
                        headers=headers,
                        method=method,
                    )
                    with (
                        self.subTest(path=path, method=method, expected=expected),
                        patch.object(proxy, "fetch") as fetch,
                        self.assertRaises(urllib.error.HTTPError) as error,
                    ):
                        opener.open(request, timeout=2)
                    self.assertEqual(error.exception.code, expected)
                    fetch.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()

    def test_image_routes_reject_absolute_request_targets(self):
        proxy = self.load_proxy()
        server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            with patch.object(proxy, "fetch") as fetch:
                connection.request(
                    "POST",
                    "http://example.invalid/backend-api/codex/images/generations",
                    body=b"",
                    headers={"Authorization": "Bearer test-only"},
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 404)
                response.read()
                fetch.assert_not_called()
        finally:
            connection.close()
            server.shutdown()
            server.server_close()

    def test_local_routes_reject_unknown_paths_and_missing_credentials(self):
        proxy = self.load_proxy()
        server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as response:
                self.assertEqual(json.load(response)["service"], "codex-transient-retry")
            for path, expected in [
                ("/backend-api/codex/responses", 401),
                ("/backend-api/codex/anything", 401),
            ]:
                with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(
                        urllib.request.Request(base + path, data=b"{}"), timeout=2
                    )
                self.assertEqual(error.exception.code, expected)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()

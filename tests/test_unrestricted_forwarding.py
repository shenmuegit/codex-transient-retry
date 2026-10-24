import gzip
import http.client
import importlib.util
import json
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


class ForwardingTests(unittest.TestCase):
    def setUp(self):
        script = Path(__file__).resolve().parents[1] / "retry_proxy.py"
        spec = importlib.util.spec_from_file_location("retry_proxy", script)
        self.proxy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.proxy)
        self.received = []
        self.responses = [(200, {}, b"okay")]
        self.delays = []
        self.upgrade_frames = []
        test = self

        class Upstream(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def respond(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                test.received.append((self.command, self.path, self.headers, body))
                status, headers, payload = test.responses[0]
                if len(test.responses) > 1:
                    test.responses.pop(0)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                if status != 101:
                    self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
                    self.wfile.flush()
                if status == 101:
                    test.upgrade_frames.append(self.rfile.read(8))
                    self.wfile.write(b"\x81\x02ok")
                    self.wfile.flush()
                    self.close_connection = True

            do_GET = do_POST = do_PUT = do_HEAD = do_OPTIONS = do_PROPFIND = respond

            def log_message(self, *args):
                pass

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), self.proxy.Handler)
        self.patches = [
            patch.object(self.proxy, "UPSTREAM", f"http://127.0.0.1:{self.upstream.server_port}"),
            patch.object(self.proxy.Handler, "pause", side_effect=self.delays.append),
            patch.object(self.proxy, "log_event"),
        ]
        for item in self.patches:
            item.start()
        for service in (self.upstream, self.gateway):
            threading.Thread(target=service.serve_forever, daemon=True).start()

    def tearDown(self):
        for service in (self.gateway, self.upstream):
            service.shutdown()
            service.server_close()
        for item in reversed(self.patches):
            item.stop()

    def connect(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=3)
        self.addCleanup(connection.close)
        return connection

    def test_authentication_and_origin_are_only_forwarded(self):
        for authorization in (None, "Basic test-only", "Bearer test-only"):
            with self.subTest(authorization=authorization):
                headers = {"Origin": "https://example.invalid", "Accept-Encoding": "identity"}
                if authorization:
                    headers["Authorization"] = authorization
                connection = self.connect()
                connection.request("POST", "/future-route?test=1", b"\x00payload\xff", headers)
                response = connection.getresponse()
                self.assertEqual((response.status, response.read()), (200, b"okay"))
                method, path, forwarded, body = self.received[-1]
                self.assertEqual(
                    (method, path, body), ("POST", "/future-route?test=1", b"\x00payload\xff")
                )
                self.assertEqual(forwarded.get("Authorization"), authorization)
                self.assertEqual(forwarded.get("Origin"), "https://example.invalid")
                self.assertEqual(forwarded.get("Accept-Encoding"), "identity")

    def test_chunked_upload_is_replayed_without_changing_the_payload(self):
        self.responses = [(503, {}, b"busy"), (200, {}, b"okay")]
        connection = self.connect()
        connection.putrequest("PUT", "/upload")
        connection.putheader("Authorization", "Bearer test-only")
        connection.putheader("Transfer-Encoding", "chunked")
        connection.endheaders()
        connection.send(b"3;test=1\r\nabc\r\n2\r\n\x00\xff\r\n0\r\nX-Trailer: test\r\n\r\n")
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (200, b"okay"))
        self.assertEqual([request[3] for request in self.received], [b"abc\x00\xff"] * 2)
        self.assertEqual([request[2].get("X-Trailer") for request in self.received], ["test"] * 2)
        self.assertEqual(self.delays, [30])

    def test_request_and_response_have_no_local_size_cap(self):
        payload = b"payload-larger-than-the-old-test-caps"
        self.responses = [(200, {}, payload)]
        with (
            patch.object(self.proxy, "MAX_BODY", 8, create=True),
            patch.object(self.proxy, "MAX_REQUEST_BODY", 8, create=True),
        ):
            connection = self.connect()
            connection.request("POST", "/upload", payload, {"Authorization": "Bearer test-only"})
            response = connection.getresponse()
            self.assertEqual((response.status, response.read()), (200, payload))
        self.assertEqual(self.received[0][3], payload)

    def test_redirect_is_returned_without_becoming_a_local_error(self):
        self.responses = [(307, {"Location": "https://example.invalid/next"}, b"redirect")]
        connection = self.connect()
        connection.request("GET", "/redirect", headers={"Authorization": "Bearer test-only"})
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (307, b"redirect"))
        self.assertEqual(response.headers["Location"], "https://example.invalid/next")
        self.assertEqual(len(self.received), 1)

    def test_gzip_payload_and_encoding_are_preserved(self):
        payload = gzip.compress(b'{"models":[]}')
        self.responses = [
            (200, {"Content-Type": "application/json", "Content-Encoding": "gzip"}, payload)
        ]
        connection = self.connect()
        connection.request(
            "GET",
            "/backend-api/codex/models",
            headers={"Accept-Encoding": "gzip", "Authorization": "Bearer test-only"},
        )
        response = connection.getresponse()
        self.assertEqual(response.read(), payload)
        self.assertEqual(response.headers["Content-Encoding"], "gzip")

    def test_deflate_success_is_forwarded_once_without_changing_bytes(self):
        for raw in (False, True):
            with self.subTest(raw=raw):
                compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS if raw else zlib.MAX_WBITS)
                payload = compressor.compress(b'{"models":[]}') + compressor.flush()
                self.responses = [
                    (
                        200,
                        {"Content-Type": "application/json", "Content-Encoding": "deflate"},
                        payload,
                    )
                ]
                connection = self.connect()
                connection.request(
                    "GET", "/backend-api/codex/models", headers={"Accept-Encoding": "deflate"}
                )
                response = connection.getresponse()
                self.assertEqual((response.status, response.read()), (200, payload))
                self.assertEqual(response.headers["Content-Encoding"], "deflate")
        self.assertEqual(len(self.received), 2)
        self.assertEqual(self.delays, [])

    def test_compressed_failure_is_retried_before_forwarding_success(self):
        failure = zlib.compress(b'{"error":{"code":"future_failure"}}')
        success = gzip.compress(zlib.compress(b'{"result":"okay"}'))
        self.responses = [
            (200, {"Content-Type": "application/json", "Content-Encoding": "deflate"}, failure),
            (
                200,
                {"Content-Type": "application/json", "Content-Encoding": "deflate, gzip"},
                success,
            ),
        ]
        connection = self.connect()
        connection.request("POST", "/future-route", b"request")
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (200, success))
        self.assertEqual(len(self.received), 2)
        self.assertEqual(self.delays, [30])

    def test_unknown_content_encoding_is_forwarded_without_false_retries(self):
        self.responses = [
            (
                200,
                {"Content-Type": "application/json", "Content-Encoding": "future-encoding"},
                b"\xff\x00encoded",
            )
        ]
        connection = self.connect()
        connection.request("GET", "/future-route")
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (200, b"\xff\x00encoded"))
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.delays, [])

    def test_false_error_fields_do_not_replay_successful_writes(self):
        for value in (False, 0, "", [], None):
            with self.subTest(value=value):
                payload = json.dumps({"error": value, "result": "okay"}).encode()
                self.responses = [(200, {"Content-Type": "application/json"}, payload)]
                connection = self.connect()
                connection.request("POST", "/future-route", b"write-once")
                response = connection.getresponse()
                self.assertEqual((response.status, response.read()), (200, payload))
        self.assertEqual(len(self.received), 5)
        self.assertEqual(self.delays, [])

    def test_parser_resource_boundaries_do_not_change_successful_responses(self):
        for payload in (
            b"[" * 1100 + b"0" + b"]" * 1100,
            b'{"value":' + b"1" * 4500 + b"}",
            b'data: {"type":"response.completed","output":'
            + b"[" * 1100
            + b"0"
            + b"]" * 1100
            + b"}\n\n",
        ):
            with self.subTest(bytes=len(payload)):
                content_type = (
                    "text/event-stream" if payload.startswith(b"data:") else "application/json"
                )
                self.responses = [(200, {"Content-Type": content_type}, payload)]
                connection = self.connect()
                connection.request("GET", "/future-route")
                response = connection.getresponse()
                self.assertEqual((response.status, response.read()), (200, payload))
        self.assertEqual(len(self.received), 3)
        self.assertEqual(self.delays, [])

    def test_duplicate_request_headers_are_preserved_on_every_attempt(self):
        self.responses = [(503, {}, b"busy"), (200, {}, b"okay")]
        connection = self.connect()
        connection.putrequest("POST", "/future-route")
        connection.putheader("X-Parts", "one")
        connection.putheader("x-parts", "two")
        connection.putheader("Content-Length", "7")
        connection.endheaders(b"request")
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (200, b"okay"))
        self.assertEqual(
            [request[2].get_all("X-Parts") for request in self.received], [["one", "two"]] * 2
        )
        self.assertEqual([request[3] for request in self.received], [b"request"] * 2)
        self.assertEqual(self.delays, [30])

    def test_absolute_request_target_keeps_its_path_and_query(self):
        connection = self.connect()
        connection.request(
            "GET",
            "https://example.invalid/future-route?test=1",
            headers={"Authorization": "Bearer test-only"},
        )
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (200, b"okay"))
        self.assertEqual(self.received[0][1], "/future-route?test=1")

    def test_model_catalog_is_fetched_each_time(self):
        first = b'{"models":["first"]}'
        second = b'{"models":["second"]}'
        self.responses = [(200, {}, first), (200, {}, second)]
        for expected in (first, second):
            connection = self.connect()
            connection.request(
                "GET", "/backend-api/codex/models", headers={"Authorization": "Bearer test-only"}
            )
            self.assertEqual(connection.getresponse().read(), expected)
        self.assertEqual(len(self.received), 2)

    def test_request_target_cannot_replace_the_configured_upstream(self):
        open_upstream = self.proxy.OPENER.open
        expected_host = f"127.0.0.1:{self.upstream.server_port}"
        destinations = []

        def checked_open(request, **kwargs):
            destinations.append(request.host)
            if request.host != expected_host:
                raise OSError("Unexpected destination")
            return open_upstream(request, **kwargs)

        with patch.object(self.proxy.OPENER, "open", side_effect=checked_open):
            connection = self.connect()
            connection.request("GET", "@example.invalid/future-route")
            response = connection.getresponse()
            self.assertEqual((response.status, response.read()), (200, b"okay"))
        self.assertEqual(destinations, [expected_host])
        self.assertEqual(self.received[0][1], "/@example.invalid/future-route")

    def test_asterisk_request_target_is_forwarded(self):
        connection = self.connect()
        connection.request("OPTIONS", "*")
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (200, b"okay"))
        self.assertEqual(self.received[0][:2], ("OPTIONS", "*"))

    def test_retry_after_has_no_scheduling_deadline(self):
        self.responses = [(503, {"Retry-After": "1800"}, b"busy"), (200, {}, b"okay")]
        connection = self.connect()
        connection.request("GET", "/future-route")
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (200, b"okay"))
        self.assertEqual(self.delays, [1800])

    def test_nonfinite_retry_after_uses_the_normal_backoff(self):
        for value in ("inf", "NaN", "1e999"):
            with self.subTest(value=value):
                self.responses = [(503, {"Retry-After": value}, b"busy"), (200, {}, b"okay")]
                connection = self.connect()
                connection.request("GET", "/future-route")
                self.assertEqual(connection.getresponse().read(), b"okay")
        self.assertEqual(self.delays, [30, 30, 30])

    def test_invalid_framing_does_not_create_an_upstream_request(self):
        for length in ("-1", "not-a-number"):
            with self.subTest(length=length):
                connection = self.connect()
                connection.request("POST", "/future-route", headers={"Content-Length": length})
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
        self.assertEqual(self.received, [])

    def test_shutdown_path_without_control_header_is_forwarded(self):
        connection = self.connect()
        connection.request("POST", "/shutdown", b"request")
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (200, b"okay"))
        self.assertEqual(self.received[0][1], "/shutdown")

    def test_all_http_errors_retry_without_error_code_lists(self):
        for status, code in (
            (400, "invalid_request_error"),
            (401, "invalid_api_key"),
            (429, "insufficient_quota"),
            (507, "future_error"),
            (599, "unknown_error"),
        ):
            with self.subTest(status=status, code=code):
                failure = json.dumps({"error": {"code": code}}).encode()
                self.responses = [
                    (status, {"Content-Type": "application/json"}, failure),
                    (200, {}, b"okay"),
                ]
                connection = self.connect()
                connection.request(
                    "GET", "/future-route", headers={"Authorization": "Bearer test-only"}
                )
                response = connection.getresponse()
                self.assertEqual((response.status, response.read()), (200, b"okay"))
        self.assertEqual(self.delays, [30] * 5)

    def test_retry_exhaustion_returns_the_original_upstream_error(self):
        self.responses = [(503, {"X-Request-ID": "test-only"}, b"original error")]
        connection = self.connect()
        connection.request(
            "POST", "/future-route", b"request", {"Authorization": "Bearer test-only"}
        )
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (503, b"original error"))
        self.assertEqual(response.headers["X-Request-ID"], "test-only")
        self.assertEqual(len(self.received), 4)
        self.assertEqual(self.delays, [30, 60, 120])

    def test_websocket_upgrade_retries_then_relays_in_both_directions(self):
        self.responses = [
            (503, {}, b"busy"),
            (101, {"Connection": "Upgrade", "Upgrade": "websocket"}, b"\x81\x05ready"),
        ]
        frame = b"\x81\x82\x01\x02\x03\x04ik"
        connection = self.connect()
        connection.putrequest("GET", "/future-websocket")
        connection.putheader("connection", "Upgrade")
        connection.putheader("upgrade", "websocket")
        connection.putheader("Origin", "https://example.invalid")
        connection.endheaders(frame)
        response = connection.getresponse()
        self.assertEqual(response.status, 101)
        self.assertEqual(response.headers["Upgrade"], "websocket")
        self.assertEqual(response.fp.read(11), b"\x81\x05ready\x81\x02ok")
        self.assertEqual(self.upgrade_frames, [frame])
        self.assertEqual(self.received[-1][2]["Connection"].lower(), "upgrade")
        self.assertEqual(self.delays, [30])


if __name__ == "__main__":
    unittest.main()

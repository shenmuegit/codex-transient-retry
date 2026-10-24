"""Optional fault injection through the installed Codex CLI, using only local servers."""

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


@unittest.skipUnless(shutil.which("codex"), "Requires a locally installed Codex CLI")
class NativeRetryTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "native_retry_proxy", Path(__file__).resolve().parents[1] / "retry_proxy.py"
        )
        self.proxy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.proxy)

    def run_cli(self, failures):
        requests = []
        reply = b'data: {"type":"response.output_text.delta","delta":"OK"}\n\ndata: {"type":"response.completed","response":{"id":"resp_test","status":"completed","output":[]}}\n\n'

        class Upstream(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def respond(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.command == "POST":
                    requests.append(self.path)
                if self.headers.get("Upgrade"):
                    status, content_type, body = (
                        426,
                        "application/json",
                        b'{"error":{"message":"HTTP only in this test server"}}',
                    )
                elif self.command == "GET":
                    status, content_type, body = 200, "application/json", b'{"models":[],"data":[]}'
                elif len(requests) <= failures:
                    status, content_type, body = (
                        503,
                        "application/json",
                        b'{"error":{"code":"server_error","message":"Synthetic local failure"}}',
                    )
                else:
                    status, content_type, body = 200, "text/event-stream", reply
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            do_GET = do_POST = do_CONNECT = respond

            def log_message(self, *args):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        gateway = ThreadingHTTPServer(("127.0.0.1", 0), self.proxy.Handler)
        self.proxy.UPSTREAM = f"http://127.0.0.1:{upstream.server_port}"
        self.proxy.DELAYS = (0, 0, 0)
        for service in (upstream, gateway):
            threading.Thread(target=service.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(prefix="codex-retry-native-test-") as directory:
                root = Path(directory).resolve()
                self.assertTrue(root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
                config = root / "config.toml"
                config.write_text(
                    'model = "gpt-5.3-codex"\ncli_auth_credentials_store = "file"\n',
                    encoding="utf-8",
                )
                self.proxy.configure(config, True)
                base = f"http://127.0.0.1:{gateway.server_port}/backend-api/codex"
                config.write_text(
                    config.read_text(encoding="utf-8").replace(
                        "http://127.0.0.1:18761/backend-api/codex", base
                    ),
                    encoding="utf-8",
                )
                (root / "auth.json").write_text(
                    json.dumps({"OPENAI_API_KEY": "test-only"}), encoding="utf-8"
                )
                env = dict(os.environ)
                for key in (
                    "CODEX_API_KEY",
                    "OPENAI_ACCESS_TOKEN",
                    "OPENAI_TOKEN",
                    "CHATGPT_ACCESS_TOKEN",
                ):
                    env.pop(key, None)
                env.update(
                    CODEX_HOME=str(root),
                    OPENAI_API_KEY="test-only",
                    OPENAI_BASE_URL=base,
                    HTTP_PROXY=self.proxy.UPSTREAM,
                    HTTPS_PROXY=self.proxy.UPSTREAM,
                    ALL_PROXY=self.proxy.UPSTREAM,
                    NO_PROXY="localhost,127.0.0.1,::1",
                    RUST_LOG="error",
                )
                result = subprocess.run(
                    [
                        shutil.which("codex"),
                        "exec",
                        "--skip-git-repo-check",
                        "--ephemeral",
                        "--sandbox",
                        "read-only",
                        "--color",
                        "never",
                        "Reply OK.",
                    ],
                    cwd=root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                self.assertTrue(requests, result.stderr[-2000:])
                return result, requests
        finally:
            for service in (gateway, upstream):
                service.shutdown()
                service.server_close()

    def test_builtin_client_can_retry_after_one_exhausted_proxy_request(self):
        result, requests = self.run_cli(failures=4)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(len(requests), 5)

    def test_native_client_recovers_from_one_upstream_failure(self):
        result, requests = self.run_cli(failures=1)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(len(requests), 2)


if __name__ == "__main__":
    unittest.main()

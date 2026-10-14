"""Buffer Codex responses and retry temporary upstream failures on localhost."""

import argparse
import gzip
import hashlib
import http.client
import io
import json
import logging
import os
import select
import shutil
import socket
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

UPSTREAM = "https://chatgpt.com/backend-api/codex"
PREFIX = "/backend-api/codex"
PORT = 18761
PROXY_VERSION = 5
DELAYS = (30, 60, 120)
MODEL_CATALOG_TIMEOUT = 3
MAX_BODY = 64 * 1024 * 1024
DEADLINE_SECONDS = 900
HOP_HEADERS = {
    "host",
    "connection",
    "content-length",
    "transfer-encoding",
    "accept-encoding",
    "proxy-authorization",
    "proxy-authenticate",
    "keep-alive",
    "te",
    "trailer",
    "upgrade",
}
RETRY_CODES = {
    "model_capacity_exceeded",
    "model_capacity",
    "server_error",
    "internal_error",
    "server_is_overloaded",
    "server_overloaded",
    "service_unavailable_error",
    "rate_limit_exceeded",
    "connection_error",
}
BLOCK_CODES = {
    "insufficient_quota",
    "usage_limit_exceeded",
    "credit_balance_exhausted",
    "project_spend_limit_exceeded",
    "organization_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
    "authentication_error",
    "invalid_api_key",
    "context_length_exceeded",
    "model_not_found",
    "invalid_request_error",
    "response_too_large",
    "unexpected_redirect",
}
LOGGER = logging.getLogger("codex-transient-retry")
MODEL_CATALOG_CACHE = None
PROVIDER = "openai_transient_retry"
LEGACY_CONFIG_HEADER = '# BEGIN CODEX TRANSIENT RETRY\nmodel_provider = "openai_transient_retry"\n# END CODEX TRANSIENT RETRY HEADER\n'
PREVIOUS_CONFIG_HEADER = '# BEGIN CODEX TRANSIENT RETRY\nmodel_provider = "openai_transient_retry"\nopenai_base_url = "http://127.0.0.1:18761/backend-api/codex"\n# END CODEX TRANSIENT RETRY HEADER\n'
CONFIG_HEADER = '# BEGIN CODEX TRANSIENT RETRY\nopenai_base_url = "http://127.0.0.1:18761/backend-api/codex"\n# END CODEX TRANSIENT RETRY HEADER\n'
CONFIG_BLOCK = """\n# BEGIN CODEX TRANSIENT RETRY PROVIDER
[model_providers.openai_transient_retry]
name = "OpenAI"
base_url = "http://127.0.0.1:18761/backend-api/codex"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
# The local proxy owns retries; avoid multiplying native and proxy attempts.
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = 900000
# END CODEX TRANSIENT RETRY PROVIDER
"""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


class ClientDisconnected(Exception):
    pass


def log_event(event, **fields):
    LOGGER.info(json.dumps({"event": event, **fields}, ensure_ascii=True))


def error_reason(error):
    if not isinstance(error, dict):
        return None
    code = str(error.get("code", "")).lower()
    message = str(error.get("message", "")).lower()
    if code in BLOCK_CODES:
        return None
    if code in RETRY_CODES:
        return code
    if "selected model is at capacity" in message:
        return "model_capacity"
    if "an error occurred while processing your request" in message:
        return "server_error"
    return None


def retry_reason(status, headers, body):
    headers = {k.lower(): v for k, v in headers.items()}
    if status in (401, 403, 404, 413, 422):
        return None
    if headers.get("content-encoding", "").lower() == "gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                body = compressed.read(MAX_BODY + 1)
        except (OSError, EOFError, zlib.error):
            return "stream_disconnected"
    if len(body) > MAX_BODY:
        return None
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        if status == 200:
            return "stream_disconnected"
        text = ""
    if "text/event-stream" in headers.get("content-type", "").lower() or text.lstrip(
        "\ufeff \t\r\n"
    ).startswith(("data:", "event:", ":")):
        completed = False
        data_lines = []
        event_name = ""
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") + [""]:
            if line.startswith("event:"):
                event_name = line[6:].strip()
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
                continue
            if line or not data_lines:
                continue
            data = "\n".join(data_lines)
            data_lines = []
            if data == "[DONE]":
                completed = True
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                return "stream_disconnected"
            if not isinstance(event, dict):
                return "stream_disconnected"
            kind = event.get("type", event_name)
            event_name = ""
            if kind == "error":
                return error_reason(event.get("error", event))
            if kind == "response.failed":
                response = event.get("response", {})
                if not isinstance(response, dict):
                    return "stream_disconnected"
                return error_reason(response.get("error", {}))
            if kind == "response.incomplete":
                return None
            if kind == "response.completed":
                completed = True
        return None if completed else "stream_disconnected"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        if status == 200:
            return "stream_disconnected"
        data = {}
    if status == 200 and not isinstance(data, dict):
        return "stream_disconnected"
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        error = data["error"]
        if str(error.get("code", "")).lower() in BLOCK_CODES:
            return None
        reason = error_reason(error)
        if reason:
            return reason
    if status in (429, 500, 502, 503, 504):
        return "upstream_" + str(status)
    return None


def fetch(method, path, body, headers, timeout):
    forwarded = {k: v for k, v in headers.items() if k.lower() not in HOP_HEADERS}
    forwarded["Accept-Encoding"] = "gzip"
    request = urllib.request.Request(
        UPSTREAM + path, data=body if method == "POST" else None, headers=forwarded, method=method
    )
    try:
        response = OPENER.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        log_event(
            "upstream_headers",
            path=path.split("?", 1)[0],
            status=response.status,
            content_type=response.headers.get("Content-Type"),
        )
        if 300 <= response.status < 400 and response.status != 304:
            return (
                502,
                {"Content-Type": "application/json"},
                b'{"error":{"code":"unexpected_redirect","message":"Upstream redirect was not followed"}}',
            )
        data = response.read(MAX_BODY + 1)
        response_headers = dict(response.headers.items())
        if response.headers.get("Content-Encoding", "").lower() == "gzip":
            if data:
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
                    data = compressed.read(MAX_BODY + 1)
            response_headers = {
                k: v for k, v in response_headers.items() if k.lower() != "content-encoding"
            }
        if len(data) > MAX_BODY:
            return (
                502,
                {"Content-Type": "application/json"},
                b'{"error":{"code":"response_too_large","message":"Local retry buffer limit exceeded"}}',
            )
        return response.status, response_headers, data


def retry_limit_reply(result, reason, attempts):
    headers = {"Content-Type": "application/json"}
    headers.update(
        {k: v for k, v in result[1].items() if k.lower() in ("x-oai-request-id", "x-request-id")}
    )
    message = f"Local automatic retry limit reached after {attempts} attempts. Last transient error: {reason}. Please retry later."
    body = json.dumps(
        {
            "error": {"code": "local_retry_limit_exceeded", "message": message},
            "attempts": attempts,
            "upstream_status": result[0],
        }
    ).encode()
    # A terminal local error prevents the built-in provider from multiplying our retry budget.
    return 400, headers, body


def request_with_retry(method, path, body, headers, cancelled=None, pause=None):
    global MODEL_CATALOG_CACHE
    cancelled = cancelled or (lambda: False)
    pause = pause or time.sleep
    catalog_key = None
    if method == "GET" and path.split("?", 1)[0] == "/models":
        authorization = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
        account = next((v for k, v in headers.items() if k.lower() == "chatgpt-account-id"), "")
        catalog_key = (path, authorization, account)
        catalog_cache = MODEL_CATALOG_CACHE
        if (
            catalog_cache
            and catalog_cache[0] == catalog_key
            and catalog_cache[1] > time.monotonic()
        ):
            return catalog_cache[2]
    deadline = time.monotonic() + DEADLINE_SECONDS
    for attempt in range(len(DELAYS) + 1):
        if cancelled():
            raise ClientDisconnected()
        try:
            result = fetch(
                method,
                path,
                body,
                headers,
                min(
                    MODEL_CATALOG_TIMEOUT if catalog_key else 300,
                    max(1, deadline - time.monotonic()),
                ),
            )
        except (urllib.error.URLError, OSError, EOFError, zlib.error, http.client.HTTPException):
            result = (
                502,
                {"Content-Type": "application/json"},
                b'{"error":{"code":"connection_error","message":"Upstream connection ended before completion"}}',
            )
        if cancelled():
            raise ClientDisconnected()
        status, response_headers, response_body = result
        reason = retry_reason(status, response_headers, response_body)
        if catalog_key and status == 200 and not reason:
            catalog = json.loads(response_body.decode("utf-8-sig"))
            if isinstance(catalog.get("models"), list):
                # Catalog refreshes have a short timeout; reuse only a valid matching account catalog.
                MODEL_CATALOG_CACHE = (catalog_key, time.monotonic() + 300, result)
        if not reason or method != "POST":
            log_event(
                "finished",
                path=path.split("?", 1)[0],
                attempts=attempt + 1,
                status=status,
                reason=reason,
            )
            return result
        if attempt == len(DELAYS):
            log_event(
                "exhausted",
                path=path.split("?", 1)[0],
                attempts=attempt + 1,
                status=status,
                reason=reason,
            )
            return retry_limit_reply(result, reason, attempt + 1)
        delay = DELAYS[attempt]
        retry_after = next(
            (v for k, v in response_headers.items() if k.lower() == "retry-after"), ""
        )
        try:
            delay = max(delay, float(retry_after))
        except (ValueError, TypeError):
            try:
                delay = max(
                    delay,
                    (
                        parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)
                    ).total_seconds(),
                )
            except (ValueError, TypeError, OverflowError):
                pass
        if time.monotonic() + delay >= deadline:
            log_event("deadline", path=path.split("?", 1)[0], reason=reason)
            return retry_limit_reply(result, reason, attempt + 1)
        log_event(
            "retry",
            path=path.split("?", 1)[0],
            retry=attempt + 1,
            delay_seconds=delay,
            reason=reason,
        )
        pause(delay)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, status, headers, body):
        self.send_response(status)
        for key, value in headers.items():
            if key.lower() not in HOP_HEADERS | {"server", "date"}:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def cancelled(self):
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def pause(self, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if self.cancelled():
                raise ClientDisconnected()
            time.sleep(max(0, min(1, until - time.monotonic())))

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def handle_request(self):
        self.connection.settimeout(30)
        try:
            if self.command == "GET" and self.path == "/health":
                body = json.dumps(
                    {
                        "service": "codex-transient-retry",
                        "version": PROXY_VERSION,
                        "pid": os.getpid(),
                        "max_retries": len(DELAYS),
                        "delays_seconds": DELAYS,
                        "model_catalog_io_timeout_seconds": MODEL_CATALOG_TIMEOUT,
                    }
                ).encode()
                return self.reply(200, {"Content-Type": "application/json"}, body)
            if self.command == "POST" and self.path == "/shutdown":
                if self.headers.get("X-Codex-Retry-Control") != "stop":
                    return self.reply(403, {}, b"Forbidden")
                self.reply(200, {}, b"Stopped")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            parsed = urllib.parse.urlsplit(self.path)
            routes = {
                ("GET", PREFIX + "/models"),
                ("POST", PREFIX + "/responses"),
                ("POST", PREFIX + "/responses/compact"),
            }
            websocket = (
                self.command == "GET"
                and parsed.path == PREFIX + "/responses"
                and self.headers.get("Upgrade", "").lower() == "websocket"
            )
            if ((self.command, parsed.path) not in routes and not websocket) or parsed.netloc:
                return self.reply(404, {}, b"Unknown route")
            if self.headers.get("Origin") or not self.headers.get("Authorization", "").startswith(
                "Bearer "
            ):
                return self.reply(401, {}, b"OpenAI authentication required")
            if websocket:
                log_event("websocket_http_fallback")
                return self.reply(
                    426,
                    {"Content-Type": "application/json"},
                    b'{"error":{"message":"Use HTTP streaming for this local retry gateway"}}',
                )
            if self.headers.get("Transfer-Encoding"):
                return self.reply(400, {}, b"Content-Length required")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.reply(400, {}, b"Invalid Content-Length")
            if not 0 <= length <= MAX_BODY:
                return self.reply(413, {}, b"Request too large")
            body = self.rfile.read(length)
            if len(body) != length:
                return
            path = parsed.path[len(PREFIX) :] + ("?" + parsed.query if parsed.query else "")
            result = request_with_retry(
                self.command, path, body, dict(self.headers.items()), self.cancelled, self.pause
            )
            self.reply(*result)
        except (ClientDisconnected, BrokenPipeError, ConnectionResetError, TimeoutError):
            log_event("client_disconnected")
        except Exception as error:
            log_event("local_error", error_type=type(error).__name__)
            try:
                self.reply(
                    502,
                    {"Content-Type": "application/json"},
                    b'{"error":{"code":"local_retry_error","message":"Local retry helper could not complete this request"}}',
                )
            except OSError:
                pass


def configure(config_path, enable):
    config_path = Path(config_path)
    if not config_path.exists():
        if not enable:
            return {"enabled": False, "unchanged": True}
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.touch(exist_ok=False)
    original = config_path.read_bytes()
    text = original.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    text = text.replace("\r\n", "\n")
    config = tomllib.loads(text)
    runtime = config_path.parent / "transient-retry"
    state_path = runtime / "config-state.json"
    runtime.mkdir(exist_ok=True)
    managed_header = next(
        (
            header
            for header in (CONFIG_HEADER, PREVIOUS_CONFIG_HEADER, LEGACY_CONFIG_HEADER)
            if header in text
        ),
        None,
    )
    if managed_header:
        expected_providers = (None, "openai") if managed_header == CONFIG_HEADER else (PROVIDER,)
        expected_url = (
            None
            if managed_header == LEGACY_CONFIG_HEADER
            else "http://127.0.0.1:18761/backend-api/codex"
        )
        if (
            CONFIG_BLOCK not in text
            or not state_path.is_file()
            or config.get("model_provider") not in expected_providers
            or config.get("openai_base_url") != expected_url
        ):
            raise ValueError("Existing retry configuration differs; refusing to overwrite it")
        state = json.loads(state_path.read_text(encoding="utf-8"))
    elif "# BEGIN CODEX TRANSIENT RETRY" in text:
        raise ValueError("Retry settings were edited; refusing to overwrite user changes")
    if enable:
        if managed_header:
            if managed_header == CONFIG_HEADER:
                return {"enabled": True, "unchanged": True, "backup": state["backup"]}
            upgrade_backup = config_path.with_name(
                config_path.name
                + ".retry-upgrade-"
                + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                + ".bak"
            )
            shutil.copy2(config_path, upgrade_backup)
            operation_backup = upgrade_backup
            state["upgrade_backup"] = str(upgrade_backup)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            updated = text.replace(managed_header, CONFIG_HEADER + state["old_provider_line"], 1)
        else:
            if (
                config.get("model_provider") not in (None, "openai")
                or PROVIDER in config.get("model_providers", {})
                or config.get("openai_base_url") is not None
            ):
                raise ValueError(
                    "A different provider or base URL is configured; refusing to replace it"
                )
            backup = config_path.with_name(
                config_path.name
                + ".retry-backup-"
                + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                + ".bak"
            )
            shutil.copy2(config_path, backup)
            operation_backup = backup
            state = {"backup": str(backup), "old_provider_line": ""}
            state_path.write_text(json.dumps(state), encoding="utf-8")
            updated = CONFIG_HEADER + text + CONFIG_BLOCK
    else:
        if not managed_header:
            return {"enabled": False, "unchanged": True}
        replacement = "" if managed_header == CONFIG_HEADER else state["old_provider_line"]
        updated = text.replace(managed_header, replacement, 1).replace(CONFIG_BLOCK, "", 1)
    tomllib.loads(updated)
    if config_path.read_bytes() != original:
        raise ValueError("Configuration changed during setup; please retry")
    temporary = config_path.with_name(config_path.name + ".retry-tmp")
    applied = updated.replace("\n", newline).encode("utf-8")
    temporary.write_bytes(applied)
    shutil.copymode(config_path, temporary)
    os.replace(temporary, config_path)
    result = {"enabled": enable, "backup": state["backup"]}
    if enable:
        result.update(
            operation_backup=str(operation_backup),
            config_sha256=hashlib.sha256(applied).hexdigest(),
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help="Run the localhost retry proxy")
    parser.add_argument(
        "--configure",
        choices=("enable", "disable"),
        help="Apply or remove the local provider configuration",
    )
    args = parser.parse_args()
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    if args.configure:
        print(json.dumps(configure(home / "config.toml", args.configure == "enable")))
        return
    if not args.serve:
        parser.print_help()
        return
    runtime = home / "transient-retry"
    runtime.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        runtime / "proxy.log", maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log_event("started", port=PORT, max_retries=len(DELAYS))
    try:
        server.serve_forever()
    finally:
        server.server_close()
        log_event("stopped")


if __name__ == "__main__":
    main()

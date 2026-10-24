"""Forward Codex gateway requests and retry failed attempts on localhost."""

import argparse
import gzip
import hashlib
import http.client
import json
import logging
import math
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

UPSTREAM = "https://chatgpt.com"
PORT = 18761
PROXY_VERSION = 12
DELAYS = (30, 60, 120)
# HTTP framing and connection-scoped headers are regenerated on each hop.
HOP_HEADERS = {
    "host",
    "connection",
    "content-length",
    "transfer-encoding",
    "proxy-authorization",
    "proxy-authenticate",
    "keep-alive",
    "te",
    "trailer",
    "upgrade",
}
LOGGER = logging.getLogger("codex-transient-retry")
PROVIDER = "openai_transient_retry"
LEGACY_CONFIG_HEADER = '# BEGIN CODEX TRANSIENT RETRY\nmodel_provider = "openai_transient_retry"\n# END CODEX TRANSIENT RETRY HEADER\n'
PREVIOUS_CONFIG_HEADER = '# BEGIN CODEX TRANSIENT RETRY\nmodel_provider = "openai_transient_retry"\nopenai_base_url = "http://127.0.0.1:18761/backend-api/codex"\n# END CODEX TRANSIENT RETRY HEADER\n'
BUILTIN_CONFIG_HEADER = '# BEGIN CODEX TRANSIENT RETRY\nopenai_base_url = "http://127.0.0.1:18761/backend-api/codex"\n# END CODEX TRANSIENT RETRY HEADER\n'
CONFIG_HEADER = BUILTIN_CONFIG_HEADER
PROVIDER_LINE_MARKER = "# CODEX TRANSIENT RETRY ORIGINAL PROVIDER\n"
# Recognize old installations for migration; this block is never applied.
LEGACY_CONFIG_BLOCK = """\n# BEGIN CODEX TRANSIENT RETRY PROVIDER
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
CONFIG_BLOCK = LEGACY_CONFIG_BLOCK.replace(
    "supports_websockets = false\n", "supports_websockets = true\n"
).replace("stream_idle_timeout_ms = 900000\n", "")


class ForwardHTTPHandler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    def do_open(self, http_class, request, **kwargs):
        connection = http_class(request.host, timeout=request.timeout, **kwargs)
        headers = list(getattr(request, "forwarded_headers", request.header_items()))
        names = {key.lower() for key, _ in headers}
        headers.extend(
            (key, value) for key, value in request.header_items() if key.lower() not in names
        )
        if request._tunnel_host:
            tunnel_headers = {
                key: value for key, value in headers if key.lower() == "proxy-authorization"
            }
            headers = [
                (key, value) for key, value in headers if key.lower() != "proxy-authorization"
            ]
            connection.set_tunnel(request._tunnel_host, headers=tunnel_headers)
        if "upgrade" not in {key.lower() for key, _ in headers}:
            headers = [(key, value) for key, value in headers if key.lower() != "connection"]
            headers.append(("Connection", "close"))
        try:
            connection.putrequest(
                request.get_method(),
                request.selector,
                skip_host=True,
                skip_accept_encoding=any(key.lower() == "accept-encoding" for key, _ in headers),
            )
            for key, value in headers:
                connection.putheader(key, value)
            connection.endheaders(request.data)
            response = connection.getresponse()
            if response.status == 101:
                response.upstream_socket = connection.sock
                connection.sock = None
            elif connection.sock:
                connection.sock.close()
                connection.sock = None
            response.url = request.full_url
            response.msg = response.reason
            return response
        except BaseException:
            connection.close()
            raise

    def https_open(self, request):
        return self.do_open(http.client.HTTPSConnection, request)


class ForwardHTTPStatus(urllib.request.HTTPErrorProcessor):
    def http_response(self, request, response):
        return response

    https_response = http_response


OPENER = urllib.request.build_opener(ForwardHTTPHandler, ForwardHTTPStatus)


class ClientDisconnected(Exception):
    pass


def log_event(event, **fields):
    LOGGER.info(json.dumps({"event": event, **fields}, ensure_ascii=True))


def error_reason(error, status=None):
    if isinstance(error, dict) and error.get("code"):
        return str(error["code"]).lower()
    return "upstream_" + str(status) if status and status >= 400 else "upstream_error"


def retry_reason(status, headers, body):
    headers = {k.lower(): v for k, v in headers.items()}
    json_response = "json" in headers.get("content-type", "").lower()
    if status >= 400:
        return "upstream_" + str(status)
    if not body:
        return None
    for encoding in reversed(headers.get("content-encoding", "").lower().split(",")):
        encoding = encoding.strip()
        try:
            if encoding == "gzip":
                body = gzip.decompress(body)
            elif encoding == "deflate":
                try:
                    body = zlib.decompress(body)
                except zlib.error:
                    body = zlib.decompress(body, -zlib.MAX_WBITS)
            elif encoding and encoding != "identity":
                return None
        except (OSError, EOFError, zlib.error):
            return "stream_disconnected"
    sse_response = "text/event-stream" in headers.get("content-type", "").lower()
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        if json_response or sse_response:
            return "stream_disconnected"
        return None
    text = text.lstrip("\ufeff")
    if sse_response or text.lstrip().startswith(("data:", "event:", ":")):
        completed = False
        response_events = False
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
                if response_events or data.lstrip().startswith("{"):
                    return "stream_disconnected"
                continue
            except (RecursionError, ValueError):
                return None
            if not isinstance(event, dict):
                continue
            kind = event.get("type", event_name)
            event_name = ""
            response_events |= isinstance(kind, str) and kind.startswith("response.")
            if kind == "error":
                return error_reason(event.get("error", event), status)
            if kind == "response.failed":
                response = event.get("response", {})
                if not isinstance(response, dict):
                    return "stream_disconnected"
                return error_reason(response.get("error"), status)
            if kind == "response.incomplete":
                return None
            if kind == "response.completed":
                completed = True
        return "stream_disconnected" if response_events and not completed else None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        if json_response:
            return "stream_disconnected"
        return None
    except (RecursionError, ValueError):
        return None
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) or error:
        return error_reason(error, status)
    return None


def fetch(method, path, body, headers, timeout=None):
    upgrade = any(key.lower() == "upgrade" for key, _ in headers.items())
    hop_headers = HOP_HEADERS - {"connection", "upgrade"} if upgrade else HOP_HEADERS
    forwarded = [(key, value) for key, value in headers.items() if key.lower() not in hop_headers]
    request = urllib.request.Request(
        UPSTREAM + (path if path.startswith("/") else "/" + path),
        data=body if body else None,
        headers=dict(forwarded),
        method=method,
    )
    request.forwarded_headers = forwarded
    if path == "*":
        request.selector = path
    response = OPENER.open(request, timeout=timeout)
    log_event(
        "upstream_headers",
        path=path.split("?", 1)[0],
        status=response.status,
        content_type=response.headers.get("Content-Type"),
    )
    if response.status == 101:
        return response.status, response.headers, response
    with response:
        return response.status, response.headers, response.read()


def request_with_retry(method, path, body, headers, cancelled=None, pause=None):
    cancelled = cancelled or (lambda: False)
    pause = pause or time.sleep
    for attempt in range(len(DELAYS) + 1):
        if cancelled():
            raise ClientDisconnected()
        try:
            result = fetch(method, path, body, headers)
        except (urllib.error.URLError, OSError, EOFError, zlib.error, http.client.HTTPException):
            result = (
                502,
                {"Content-Type": "application/json"},
                b'{"error":{"code":"connection_error","message":"Upstream connection ended before completion"}}',
            )
        if result[0] == 101:
            return result
        if cancelled():
            raise ClientDisconnected()
        status, response_headers, response_body = result
        reason = retry_reason(status, response_headers, response_body)
        if not reason:
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
            return result
        delay = DELAYS[attempt]
        retry_after = next(
            (v for k, v in response_headers.items() if k.lower() == "retry-after"), ""
        )
        try:
            retry_after_seconds = float(retry_after)
            if not math.isfinite(retry_after_seconds):
                raise ValueError("Invalid Retry-After")
            delay = max(delay, retry_after_seconds)
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
        self.send_response_only(status)
        if status == 101:
            try:
                for key, value in headers.items():
                    if key.lower() not in HOP_HEADERS - {"connection", "upgrade"}:
                        self.send_header(key, value)
                self.end_headers()
                self.close_connection = True
                return self.relay(body)
            finally:
                body.close()
                body.upstream_socket.close()
        for key, value in headers.items():
            if key.lower() not in HOP_HEADERS:
                self.send_header(key, value)
        length = (
            next(
                (value for key, value in headers.items() if key.lower() == "content-length"),
                str(len(body)),
            )
            if self.command == "HEAD"
            else str(len(body))
        )
        self.send_header("Content-Length", length)
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def relay(self, response):
        upstream = response.upstream_socket

        def copy(reader, destination):
            try:
                read = getattr(reader, "read1", reader.read)
                while data := read(65536):
                    destination.sendall(data)
            except OSError:
                pass
            finally:
                for connection in (self.connection, upstream):
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        worker = threading.Thread(target=copy, args=(response.fp, self.connection), daemon=True)
        worker.start()
        try:
            copy(self.rfile, upstream)
        finally:
            worker.join()

    def read_body(self):
        if self.headers.get("Transfer-Encoding", "").strip().lower().endswith("chunked"):
            body = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";", 1)[0], 16)
                if size < 0:
                    raise ValueError("Invalid chunk size")
                if not size:
                    for key, value in http.client.parse_headers(self.rfile).items():
                        self.headers[key] = value
                    return bytes(body)
                chunk = self.rfile.read(size)
                if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                    raise ClientDisconnected()
                body.extend(chunk)
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0:
            raise ValueError("Invalid Content-Length")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ClientDisconnected()
        return body

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

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self.handle_request
        raise AttributeError(name)

    def handle_request(self):
        try:
            if self.command == "GET" and self.path == "/health":
                body = json.dumps(
                    {
                        "service": "codex-transient-retry",
                        "version": PROXY_VERSION,
                        "pid": os.getpid(),
                        "max_retries": len(DELAYS),
                        "delays_seconds": DELAYS,
                    }
                ).encode()
                return self.reply(200, {"Content-Type": "application/json"}, body)
            if (
                self.command == "POST"
                and self.path == "/shutdown"
                and self.headers.get("X-Codex-Retry-Control") == "stop"
            ):
                self.reply(200, {}, b"Stopped")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.scheme and parsed.netloc:
                self.path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            try:
                body = self.read_body()
            except ValueError:
                return self.reply(400, {}, b"Invalid HTTP request framing")
            result = request_with_retry(
                self.command,
                self.path,
                body,
                self.headers,
                self.cancelled,
                self.pause,
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


def configure(config_path, enable, runtime=None):
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
    runtime = Path(runtime) if runtime is not None else config_path.parent / "transient-retry"
    state_path = runtime / "config-state.json"
    legacy_state_path = config_path.parent / "transient-retry" / "config-state.json"
    source_state_path = state_path if state_path.is_file() else legacy_state_path
    managed_header = next(
        (
            header
            for header in (CONFIG_HEADER, PREVIOUS_CONFIG_HEADER, LEGACY_CONFIG_HEADER)
            if header in text
        ),
        None,
    )
    if managed_header:
        managed_block = retained_tables = ""
        end_marker = "# END CODEX TRANSIENT RETRY PROVIDER\n"
        for block in (CONFIG_BLOCK, LEGACY_CONFIG_BLOCK):
            prefix = block.removesuffix(end_marker)
            start = text.find(prefix)
            end = text.find(end_marker, start + len(prefix)) if start >= 0 else -1
            if end < 0:
                continue
            tail = text[start + len(prefix) : end]
            first_setting = next(
                (
                    line.strip()
                    for line in tail.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                ),
                "",
            )
            if first_setting and not first_setting.startswith("["):
                continue
            managed_block = text[start : end + len(end_marker)]
            retained_tables = tail
            break
        expected_providers = (None, "openai") if managed_header == CONFIG_HEADER else (PROVIDER,)
        expected_url = (
            None
            if managed_header == LEGACY_CONFIG_HEADER
            else "http://127.0.0.1:18761/backend-api/codex"
        )
        if (
            not source_state_path.is_file()
            or config.get("model_provider") not in expected_providers
            or config.get("openai_base_url") != expected_url
            or (managed_header != CONFIG_HEADER and not managed_block)
            or (not managed_block and "# BEGIN CODEX TRANSIENT RETRY PROVIDER" in text)
        ):
            raise ValueError("Existing retry configuration differs; refusing to overwrite it")
        state = json.loads(source_state_path.read_text(encoding="utf-8"))
        replacement = (
            ""
            if managed_header == CONFIG_HEADER or PROVIDER_LINE_MARKER in text
            else state["old_provider_line"]
        )
        restored = text.replace(managed_header, replacement, 1)
        if managed_block:
            restored = restored.replace(managed_block, retained_tables, 1)
        restored = restored.replace(PROVIDER_LINE_MARKER, state["old_provider_line"], 1)
    elif "# BEGIN CODEX TRANSIENT RETRY" in text:
        raise ValueError("Retry settings were edited; refusing to overwrite user changes")
    else:
        restored = text
    if not enable and not managed_header:
        return {"enabled": False, "unchanged": True}
    updated = CONFIG_HEADER + restored if enable else restored
    updated_config = tomllib.loads(updated)
    if enable and updated_config.get("model_provider") not in (None, "openai"):
        raise ValueError("The original provider differs; refusing to replace it")
    if managed_header and updated == text and source_state_path == state_path:
        return {"enabled": enable, "unchanged": True, "backup": state["backup"]}
    runtime.mkdir(parents=True, exist_ok=True)
    if enable:
        if managed_header:
            old_backup = Path(state["backup"])
            backup = runtime / old_backup.name
            if old_backup.resolve() != backup.resolve():
                if backup.exists() and backup.read_bytes() != old_backup.read_bytes():
                    raise ValueError("A different configuration backup already exists")
                if not backup.exists():
                    shutil.copy2(old_backup, backup)
            state["backup"] = str(backup)
            upgrade_backup = runtime / (
                config_path.name
                + ".retry-upgrade-"
                + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                + ".bak"
            )
            shutil.copy2(config_path, upgrade_backup)
            operation_backup = upgrade_backup
            state["upgrade_backup"] = str(upgrade_backup)
        else:
            if (
                config.get("model_provider") not in (None, "openai")
                or PROVIDER in config.get("model_providers", {})
                or config.get("openai_base_url") is not None
            ):
                raise ValueError(
                    "A different provider or base URL is configured; refusing to replace it"
                )
            backup = runtime / (
                config_path.name
                + ".retry-backup-"
                + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                + ".bak"
            )
            shutil.copy2(config_path, backup)
            operation_backup = backup
            state = {"backup": str(backup), "old_provider_line": ""}
        state_path.write_text(json.dumps(state), encoding="utf-8")
    applied = updated.replace("\n", newline).encode("utf-8")
    # Keep staging and backups out of the Codex config directory, even across volumes.
    with config_path.open("r+b") as destination:
        if destination.read() != original:
            raise ValueError("Configuration changed during setup; please retry")
        destination.seek(0)
        try:
            destination.write(applied)
            destination.truncate()
            destination.flush()
        except OSError:
            destination.seek(0)
            destination.write(original)
            destination.truncate()
            raise
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
    runtime = Path(
        os.environ.get(
            "CODEX_RETRY_RUNTIME_DIR",
            Path(__file__).resolve().parent / "local-diagnostics" / "transient-retry",
        )
    )
    if args.configure:
        print(json.dumps(configure(home / "config.toml", args.configure == "enable", runtime)))
        return
    if not args.serve:
        parser.print_help()
        return
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

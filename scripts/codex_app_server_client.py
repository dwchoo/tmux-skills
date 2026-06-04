#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
import select
import socket
import struct
import sys
import time
from dataclasses import dataclass
from typing import Any


CLIENT_TITLE = "tmux-control bridge"
CLIENT_VERSION = "0.1"
INITIALIZE_CAPABILITIES: dict[str, Any] = {
    "experimentalApi": False,
    "requestAttestation": False,
    "optOutNotificationMethods": [],
}
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
RETRYABLE_ERROR_TOKENS = (
    "active turn",
    "already running",
    "busy",
    "overload",
    "temporar",
    "timeout",
    "timed out",
    "eof",
    "socket",
    "connection",
    "no rollout found",
)
PERMANENT_ERROR_TOKENS = (
    "unsupported",
    "method not found",
    "invalid endpoint",
    "malformed",
)


class AppServerClientError(RuntimeError):
    def __init__(self, message: str, *, failure_class: str = "retryable_failure") -> None:
        super().__init__(message)
        self.failure_class = failure_class


class RetryableAppServerError(AppServerClientError):
    def __init__(self, message: str) -> None:
        super().__init__(message, failure_class="retryable_failure")


class PermanentAppServerError(AppServerClientError):
    def __init__(self, message: str) -> None:
        super().__init__(message, failure_class="permanent_failure")


@dataclass(frozen=True)
class Endpoint:
    raw: str
    socket_path: str


def parse_endpoint(endpoint: str) -> Endpoint:
    prefix = "unix://"
    if not endpoint.startswith(prefix):
        raise PermanentAppServerError(f"unsupported bridge endpoint: {endpoint!r}")
    socket_path = endpoint[len(prefix) :]
    if not socket_path.strip():
        raise PermanentAppServerError("bridge endpoint must be explicit unix://PATH in v1")
    if not socket_path.startswith("/"):
        raise PermanentAppServerError(f"bridge endpoint path must be absolute: {endpoint!r}")
    return Endpoint(raw=endpoint, socket_path=socket_path)


def _error_failure_class(error: Any) -> str:
    code = None
    message = ""
    if isinstance(error, dict):
        code = error.get("code")
        message = str(error.get("message", ""))
    else:
        message = str(error)
    lowered = message.lower()
    if code == -32601 or any(token in lowered for token in PERMANENT_ERROR_TOKENS):
        return "permanent_failure"
    if any(token in lowered for token in RETRYABLE_ERROR_TOKENS):
        return "retryable_failure"
    return "retryable_failure"


def _thread_id_from_result(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    thread = result.get("thread")
    if isinstance(thread, dict) and isinstance(thread.get("id"), str):
        return thread["id"]
    for key in ("threadId", "thread_id"):
        value = result.get(key)
        if isinstance(value, str):
            return value
    return None


def _turn_id_from_result(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    turn = result.get("turn")
    if isinstance(turn, dict) and isinstance(turn.get("id"), str):
        return turn["id"]
    for key in ("turnId", "turn_id"):
        value = result.get(key)
        if isinstance(value, str):
            return value
    return None


class UnixSocketWebSocketTransport:
    def __init__(self, socket_path: str, timeout_seconds: float = 10.0) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds
        self.sock: socket.socket | None = None
        self._read_buffer = b""

    def connect(self) -> None:
        if self.sock is not None:
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            sock.connect(self.socket_path)
        except BlockingIOError:
            pass
        except OSError as exc:
            sock.close()
            raise RetryableAppServerError(f"failed to connect app-server Unix socket: {exc}") from exc
        self.sock = sock
        self._complete_connect()
        self._handshake()

    def send_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send_frame(0x1, data)

    def recv_json(self) -> dict[str, Any]:
        while True:
            opcode, payload = self._read_frame()
            if opcode == 0x1:
                try:
                    message = json.loads(payload.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    raise PermanentAppServerError(f"malformed JSON-RPC response: {payload!r}") from exc
                if not isinstance(message, dict):
                    raise PermanentAppServerError(f"malformed JSON-RPC response object: {message!r}")
                return message
            if opcode == 0x8:
                raise RetryableAppServerError("app-server WebSocket closed")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            raise PermanentAppServerError(f"unsupported app-server WebSocket opcode: {opcode}")

    def close(self) -> None:
        sock = self.sock
        if sock is None:
            return
        try:
            self._send_frame(0x8, b"")
        except AppServerClientError:
            pass
        self.sock = None
        try:
            sock.close()
        except OSError:
            pass

    def _complete_connect(self) -> None:
        sock = self._require_socket()
        _, writable, _ = select.select([], [sock], [], self.timeout_seconds)
        if not writable:
            raise RetryableAppServerError("timed out connecting app-server Unix socket")
        err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err:
            raise RetryableAppServerError(f"failed to connect app-server Unix socket: errno {err}")

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        expected_accept = base64.b64encode(hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        self._write_all(request)
        response = self._read_http_response()
        header_text = response.decode("iso-8859-1", errors="replace")
        status_line = header_text.split("\r\n", 1)[0]
        if " 101 " not in status_line:
            raise RetryableAppServerError(f"app-server WebSocket handshake failed: {status_line}")
        headers: dict[str, str] = {}
        for line in header_text.split("\r\n")[1:]:
            if not line or ":" not in line:
                continue
            key_part, value_part = line.split(":", 1)
            headers[key_part.strip().lower()] = value_part.strip()
        if headers.get("sec-websocket-accept", "").lower() != expected_accept.lower():
            raise PermanentAppServerError("app-server WebSocket handshake returned invalid Sec-WebSocket-Accept")

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._write_all(bytes(header) + mask + masked)

    def _read_frame(self) -> tuple[int, bytes]:
        header = self._read_exact(2)
        first, second = header
        opcode = first & 0x0F
        length = second & 0x7F
        masked = bool(second & 0x80)
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def _read_http_response(self) -> bytes:
        deadline = time.monotonic() + self.timeout_seconds
        while b"\r\n\r\n" not in self._read_buffer:
            self._read_some(deadline)
        response, self._read_buffer = self._read_buffer.split(b"\r\n\r\n", 1)
        return response + b"\r\n\r\n"

    def _read_exact(self, length: int) -> bytes:
        deadline = time.monotonic() + self.timeout_seconds
        while len(self._read_buffer) < length:
            self._read_some(deadline)
        data = self._read_buffer[:length]
        self._read_buffer = self._read_buffer[length:]
        return data

    def _read_some(self, deadline: float) -> None:
        sock = self._require_socket()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RetryableAppServerError("timed out waiting for app-server WebSocket data")
        readable, _, _ = select.select([sock], [], [], remaining)
        if not readable:
            raise RetryableAppServerError("timed out waiting for app-server WebSocket data")
        try:
            chunk = sock.recv(4096)
        except OSError as exc:
            raise RetryableAppServerError(f"failed reading app-server WebSocket data: {exc}") from exc
        if not chunk:
            raise RetryableAppServerError("app-server WebSocket EOF")
        self._read_buffer += chunk

    def _write_all(self, data: bytes) -> None:
        sock = self._require_socket()
        view = memoryview(data)
        deadline = time.monotonic() + self.timeout_seconds
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RetryableAppServerError("timed out writing app-server WebSocket data")
            _, writable, _ = select.select([], [sock], [], remaining)
            if not writable:
                raise RetryableAppServerError("timed out writing app-server WebSocket data")
            try:
                written = sock.send(view)
            except OSError as exc:
                raise RetryableAppServerError(f"failed writing app-server WebSocket data: {exc}") from exc
            if written <= 0:
                raise RetryableAppServerError("app-server WebSocket write returned zero bytes")
            view = view[written:]

    def _require_socket(self) -> socket.socket:
        if self.sock is None:
            raise RetryableAppServerError("app-server WebSocket is not connected")
        return self.sock


class AppServerClient:
    def __init__(self, endpoint: str, codex_bin: str = "codex", timeout_seconds: float = 10.0) -> None:
        del codex_bin
        self.endpoint = parse_endpoint(endpoint)
        self.timeout_seconds = timeout_seconds
        self.transport: UnixSocketWebSocketTransport | None = None
        self._next_id = 1
        self.transcript: dict[str, list[dict[str, Any]]] = {
            "outbound": [],
            "responses": [],
            "notifications": [],
        }

    def connect(self) -> None:
        if self.transport is not None:
            return
        transport = UnixSocketWebSocketTransport(self.endpoint.socket_path, self.timeout_seconds)
        transport.connect()
        self.transport = transport

    def initialize(self, client_name: str = "tmux-control-bridge") -> dict[str, Any]:
        response = self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": client_name,
                    "title": CLIENT_TITLE,
                    "version": CLIENT_VERSION,
                },
                "capabilities": INITIALIZE_CAPABILITIES,
            },
        )
        self._notify("initialized", {})
        return response

    def resume_thread(self, thread_id: str) -> dict[str, Any]:
        response = self._request("thread/resume", {"threadId": thread_id})
        resumed = _thread_id_from_result(response.get("result"))
        if resumed != thread_id:
            raise PermanentAppServerError(
                f"thread/resume returned mismatched thread id: requested {thread_id!r}, got {resumed!r}"
            )
        return response

    def start_turn(self, thread_id: str, prompt: str, cwd: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt, "text_elements": []}],
        }
        if cwd is not None:
            params["cwd"] = cwd
        return self._request("turn/start", params)

    def close(self) -> None:
        transport = self.transport
        if transport is None:
            return
        self.transport = None
        transport.close()

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {
            "id": request_id,
            "method": method,
            "params": params,
        }
        self._write_json(payload)
        return self._read_response(request_id)

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write_json({"method": method, "params": params})

    def _write_json(self, payload: dict[str, Any]) -> None:
        transport = self._require_transport()
        self.transcript["outbound"].append(payload)
        transport.send_json(payload)

    def _read_response(self, request_id: int) -> dict[str, Any]:
        while True:
            message = self._require_transport().recv_json()
            if "method" in message and "id" not in message:
                self.transcript["notifications"].append(message)
                continue
            if message.get("id") != request_id:
                self.transcript["responses"].append(message)
                continue
            self.transcript["responses"].append(message)
            if "error" in message:
                failure_class = _error_failure_class(message["error"])
                text = f"JSON-RPC {request_id} error: {message['error']}"
                if failure_class == "permanent_failure":
                    raise PermanentAppServerError(text)
                raise RetryableAppServerError(text)
            return message

    def _require_transport(self) -> UnixSocketWebSocketTransport:
        if self.transport is None:
            raise RetryableAppServerError("app-server WebSocket is not connected")
        return self.transport


def response_id(response: dict[str, Any]) -> str:
    return str(response.get("id", ""))


def response_thread_id(response: dict[str, Any], fallback: str | None = None) -> str | None:
    return _thread_id_from_result(response.get("result")) or fallback


def response_turn_id(response: dict[str, Any]) -> str | None:
    return _turn_id_from_result(response.get("result"))


def main(argv: list[str] | None = None) -> int:
    del argv
    print("codex_app_server_client.py is a helper module; use scripts/tmux_bridge.py poc", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

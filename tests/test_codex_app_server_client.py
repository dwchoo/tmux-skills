from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import codex_app_server_client  # noqa: E402


class FakeUnixWebSocketAppServer:
    def __init__(self, socket_path: Path, *, resume_thread_id: str | None = None) -> None:
        self.socket_path = socket_path
        self.resume_thread_id = resume_thread_id
        self.received: list[dict[str, Any]] = []
        self.thread: threading.Thread | None = None
        self.error: BaseException | None = None
        self.ready = threading.Event()
        self.done = threading.Event()

    def __enter__(self) -> FakeUnixWebSocketAppServer:
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise AssertionError("fake app-server did not start")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.done.wait(5)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        if self.error is not None and exc_type is None:
            raise AssertionError(f"fake app-server failed: {self.error}") from self.error

    def _serve(self) -> None:
        try:
            try:
                self.socket_path.unlink()
            except FileNotFoundError:
                pass
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(self.socket_path))
                server.listen(1)
                self.ready.set()
                conn, _ = server.accept()
                with conn:
                    self._handshake(conn)
                    while True:
                        message = self._read_message(conn)
                        if message is None:
                            return
                        self.received.append(message)
                        method = message.get("method")
                        if method == "initialize":
                            self._send_message(conn, {"id": message["id"], "result": {"serverInfo": {"name": "fake"}}})
                        elif method == "initialized":
                            continue
                        elif method == "thread/start":
                            self._send_message(
                                conn,
                                {
                                    "id": message["id"],
                                    "result": {
                                        "thread": {
                                            "id": "thr_started",
                                            "cwd": message["params"].get("cwd"),
                                        }
                                    },
                                },
                            )
                        elif method == "thread/resume":
                            thread_id = self.resume_thread_id
                            if thread_id is None:
                                thread_id = str(message["params"]["threadId"])
                            self._send_message(conn, {"id": message["id"], "result": {"thread": {"id": thread_id}}})
                        elif method == "turn/start":
                            self._send_message(
                                conn,
                                {
                                    "method": "turn/started",
                                    "params": {
                                        "threadId": message["params"]["threadId"],
                                        "turn": {"id": "turn_1"},
                                    },
                                },
                            )
                            self._send_message(
                                conn,
                                {
                                    "id": message["id"],
                                    "result": {
                                        "thread": {"id": message["params"]["threadId"]},
                                        "turn": {"id": "turn_1"},
                                    },
                                },
                            )
                            return
                        else:
                            self._send_message(
                                conn,
                                {"id": message.get("id"), "error": {"code": -32601, "message": "Method not found"}},
                            )
        except BaseException as exc:
            self.error = exc
            self.ready.set()
        finally:
            self.done.set()

    def _handshake(self, conn: socket.socket) -> None:
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = conn.recv(4096)
            if not chunk:
                raise EOFError("handshake EOF")
            request += chunk
        headers: dict[str, str] = {}
        for line in request.decode("iso-8859-1").split("\r\n")[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        ws_key = headers["sec-websocket-key"]
        accept = base64.b64encode(
            hashlib.sha1((ws_key + codex_app_server_client.WEBSOCKET_GUID).encode("ascii")).digest()
        ).decode("ascii")
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            ).encode("ascii")
        )

    def _read_message(self, conn: socket.socket) -> dict[str, Any] | None:
        header = self._read_exact(conn, 2)
        if header == b"":
            return None
        first, second = header
        opcode = first & 0x0F
        if opcode == 0x8:
            return None
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(conn, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(conn, 8))[0]
        mask = self._read_exact(conn, 4)
        payload = self._read_exact(conn, length)
        unmasked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        decoded = json.loads(unmasked.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise AssertionError(f"expected JSON object, got {decoded!r}")
        return decoded

    def _send_message(self, conn: socket.socket, message: dict[str, Any]) -> None:
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        header = bytearray([0x81])
        if len(payload) < 126:
            header.append(len(payload))
        elif len(payload) < 65536:
            header.append(126)
            header.extend(struct.pack("!H", len(payload)))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", len(payload)))
        conn.sendall(bytes(header) + payload)

    def _read_exact(self, conn: socket.socket, length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            chunk = conn.recv(length - len(chunks))
            if not chunk:
                if not chunks and length == 2:
                    return b""
                raise EOFError("frame EOF")
            chunks.extend(chunk)
        return bytes(chunks)


class AppServerClientTests(unittest.TestCase):
    def test_endpoint_validation_allows_explicit_absolute_unix_socket_only(self) -> None:
        self.assertEqual(codex_app_server_client.parse_endpoint("unix:///tmp/codex.sock").socket_path, "/tmp/codex.sock")

        for endpoint in ("", "unix://", "unix://relative.sock", "ws://127.0.0.1:4500", "http://x", "127.0.0.1:4500"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(codex_app_server_client.PermanentAppServerError):
                    codex_app_server_client.parse_endpoint(endpoint)

    def test_initialize_resume_start_turn_protocol_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            socket_path = Path(tmp_name) / "app-server.sock"
            with FakeUnixWebSocketAppServer(socket_path) as server:
                client = codex_app_server_client.AppServerClient(f"unix://{socket_path}", timeout_seconds=2)
                try:
                    client.connect()
                    client.initialize()
                    started = client.start_thread(
                        cwd="/workspace",
                        developer_instructions="bridge handler",
                        sandbox="danger-full-access",
                        approval_policy="never",
                    )
                    resume = client.resume_thread("thr_test")
                    turn = client.start_turn("thr_test", "wake prompt", "/workspace")
                finally:
                    client.close()

            self.assertEqual(codex_app_server_client.response_thread_id(started), "thr_started")
            self.assertEqual(codex_app_server_client.response_thread_id(resume), "thr_test")
            self.assertEqual(codex_app_server_client.response_thread_id(turn), "thr_test")
            self.assertEqual(codex_app_server_client.response_turn_id(turn), "turn_1")
            self.assertEqual(
                [item["method"] for item in client.transcript["outbound"]],
                ["initialize", "initialized", "thread/start", "thread/resume", "turn/start"],
            )
            self.assertFalse(any("jsonrpc" in item for item in client.transcript["outbound"]))
            self.assertEqual(client.transcript["outbound"][0]["params"]["clientInfo"]["title"], "tmux-control bridge")
            self.assertEqual(client.transcript["outbound"][0]["params"]["clientInfo"]["version"], "0.1")
            self.assertEqual(client.transcript["outbound"][0]["params"]["capabilities"]["experimentalApi"], False)
            self.assertEqual(
                client.transcript["outbound"][4]["params"]["input"],
                [{"type": "text", "text": "wake prompt", "text_elements": []}],
            )
            self.assertEqual(client.transcript["notifications"][0]["method"], "turn/started")
            self.assertEqual([item["method"] for item in server.received], ["initialize", "initialized", "thread/start", "thread/resume", "turn/start"])

    def test_mismatched_resume_thread_is_permanent_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            socket_path = Path(tmp_name) / "app-server.sock"
            with FakeUnixWebSocketAppServer(socket_path, resume_thread_id="thr_other"):
                client = codex_app_server_client.AppServerClient(f"unix://{socket_path}", timeout_seconds=2)
                try:
                    client.connect()
                    client.initialize()
                    with self.assertRaises(codex_app_server_client.PermanentAppServerError):
                        client.resume_thread("thr_test")
                finally:
                    client.close()

    def test_unsupported_method_error_is_permanent(self) -> None:
        failure_class = codex_app_server_client._error_failure_class({"code": -32601, "message": "Method not found"})
        self.assertEqual(failure_class, "permanent_failure")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
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

import tmux_bridge  # noqa: E402


def websocket_accept_key(request: bytes) -> str:
    import base64
    import hashlib

    headers: dict[str, str] = {}
    for line in request.decode("iso-8859-1").split("\r\n")[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return base64.b64encode(
        hashlib.sha1((headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
    ).decode("ascii")


class FakePocAppServer:
    def __init__(self, socket_path: Path, *, resume_error: bool = False) -> None:
        self.socket_path = socket_path
        self.resume_error = resume_error
        self.ready = threading.Event()
        self.done = threading.Event()
        self.error: BaseException | None = None

    def __enter__(self) -> FakePocAppServer:
        threading.Thread(target=self._serve, daemon=True).start()
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
                        method = message.get("method")
                        if method == "initialize":
                            self._send_message(conn, {"id": message["id"], "result": {"serverInfo": {"name": "fake"}}})
                        elif method == "initialized":
                            continue
                        elif method == "thread/resume":
                            if self.resume_error:
                                self._send_message(
                                    conn,
                                    {"id": message["id"], "error": {"code": -32600, "message": "no rollout found for thread id"}},
                                )
                            else:
                                self._send_message(
                                    conn,
                                    {"id": message["id"], "result": {"thread": {"id": message["params"]["threadId"]}}},
                                )
                        elif method == "turn/start":
                            self._send_message(
                                conn,
                                {
                                    "id": message["id"],
                                    "result": {
                                        "thread": {"id": message["params"]["threadId"]},
                                        "turn": {"id": "turn_poc"},
                                    },
                                },
                            )
                            return
        except BaseException as exc:
            self.error = exc
            self.ready.set()
        finally:
            self.done.set()

    def _handshake(self, conn: socket.socket) -> None:
        request = b""
        while b"\r\n\r\n" not in request:
            request += conn.recv(4096)
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {websocket_accept_key(request)}\r\n"
                "\r\n"
            ).encode("ascii")
        )

    def _read_message(self, conn: socket.socket) -> dict[str, Any] | None:
        header = self._read_exact(conn, 2)
        if header == b"":
            return None
        first, second = header
        if first & 0x0F == 0x8:
            return None
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(conn, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(conn, 8))[0]
        mask = self._read_exact(conn, 4)
        payload = self._read_exact(conn, length)
        decoded = json.loads(bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload)).decode("utf-8"))
        if not isinstance(decoded, dict):
            raise AssertionError("expected object message")
        return decoded

    def _send_message(self, conn: socket.socket, message: dict[str, Any]) -> None:
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        header = bytearray([0x81])
        if len(payload) < 126:
            header.append(len(payload))
        else:
            header.append(126)
            header.extend(struct.pack("!H", len(payload)))
        conn.sendall(bytes(header) + payload)

    def _read_exact(self, conn: socket.socket, length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            chunk = conn.recv(length - len(chunks))
            if not chunk:
                return b"" if not chunks and length == 2 else bytes(chunks)
            chunks.extend(chunk)
        return bytes(chunks)


class TmuxBridgePocArtifactTests(unittest.TestCase):
    def test_poc_rejects_blank_thread_id_before_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            workspace = tmp / "workspace"
            workspace.mkdir()
            state_dir = tmp / "state"

            with self.assertRaisesRegex(Exception, "thread id must be nonblank"):
                tmux_bridge.run_poc(
                    thread_id="   ",
                    endpoint="unix:///tmp/app-server.sock",
                    workspace=str(workspace),
                    prompt="tmux-manager observed a terminal event.",
                    state_dir=str(state_dir),
                    fixture_root=tmp / "fixtures",
                    timestamp="20260604-120000",
                )

            self.assertFalse((state_dir / "bridge").exists())
            self.assertFalse((tmp / "fixtures").exists())

    def test_poc_rejects_unsupported_endpoint_before_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            workspace = tmp / "workspace"
            workspace.mkdir()
            state_dir = tmp / "state"

            with self.assertRaisesRegex(Exception, "unsupported bridge endpoint"):
                tmux_bridge.run_poc(
                    thread_id="thr_test",
                    endpoint="ws://127.0.0.1:4500",
                    workspace=str(workspace),
                    prompt="tmux-manager observed a terminal event.",
                    state_dir=str(state_dir),
                    codex_bin=str(tmp / "missing-codex"),
                    fixture_root=tmp / "fixtures",
                    timestamp="20260604-120000",
                )

            self.assertFalse((state_dir / "bridge").exists())
            self.assertFalse((tmp / "fixtures").exists())

    def test_poc_writes_fixture_runtime_and_manual_note_then_validator_promotes_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            workspace = tmp / "workspace"
            workspace.mkdir()
            state_dir = tmp / "state"
            fixture_root = tmp / "fixtures"
            socket_path = tmp / "app-server.sock"
            with FakePocAppServer(socket_path):
                result = tmux_bridge.run_poc(
                    thread_id="thr_test",
                    endpoint=f"unix://{socket_path}",
                    workspace=str(workspace),
                    prompt="tmux-manager observed a terminal event.\n\nWorkspace: test",
                    state_dir=str(state_dir),
                    fixture_root=fixture_root,
                    timestamp="20260604-120000",
                )

            runtime_path = Path(result["runtime_path"])
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            fixture_path = Path(runtime["protocol_fixture_path"])
            manual_path = Path(runtime["manual_confirmation_note_path"])

            self.assertTrue(runtime["provisional_delivery"])
            self.assertIsNone(runtime["resume_error"])
            self.assertFalse(runtime["delivered"])
            self.assertEqual(runtime["request_sequence"], ["initialize", "initialized", "thread/resume", "turn/start"])
            self.assertTrue(fixture_path.exists())
            self.assertTrue(manual_path.exists())

            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
            self.assertEqual(fixture["canonical_success_signal"], "turn_start_response")
            self.assertEqual([item["method"] for item in fixture["requests"]], ["initialize", "initialized", "thread/resume", "turn/start"])
            self.assertEqual(fixture["requests"][0]["params"]["clientInfo"]["title"], "tmux-manager bridge")
            self.assertEqual(fixture["requests"][0]["params"]["clientInfo"]["version"], "0.1")
            self.assertEqual(fixture["requests"][0]["params"]["capabilities"]["optOutNotificationMethods"], [])
            self.assertEqual(fixture["requests"][3]["params"]["input"][0]["text"].splitlines()[0], "tmux-manager observed a terminal event.")
            self.assertEqual(fixture["requests"][3]["params"]["input"][0]["text_elements"], [])
            self.assertFalse(any("jsonrpc" in item for item in fixture["requests"]))
            self.assertEqual(fixture["protocol_evidence"]["command"], "codex app-server generate-ts --experimental")

            manual_path.write_text(
                "\n".join(
                    [
                        "# tmux-manager bridge PoC manual confirmation",
                        "",
                        "main_cli_thread_id: thr_test",
                        "received_prompt_timestamp: 2026-06-04T12:00:01+09:00",
                        "received_prompt_first_line: tmux-manager observed a terminal event.",
                        "operator_confirmation: confirmed_same_thread",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            validation = tmux_bridge.validate_poc_artifacts(runtime_path)

            self.assertTrue(validation["valid"])
            promoted = json.loads(runtime_path.read_text(encoding="utf-8"))
            self.assertTrue(promoted["delivered"])

    def test_poc_continues_when_live_session_resume_has_no_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            workspace = tmp / "workspace"
            workspace.mkdir()
            socket_path = tmp / "app-server.sock"

            with FakePocAppServer(socket_path, resume_error=True):
                result = tmux_bridge.run_poc(
                    thread_id="thr_live",
                    endpoint=f"unix://{socket_path}",
                    workspace=str(workspace),
                    prompt="tmux-manager observed a terminal event.",
                    state_dir=str(tmp / "state"),
                    fixture_root=tmp / "fixtures",
                    timestamp="20260604-120000",
                )

            runtime = json.loads(Path(result["runtime_path"]).read_text(encoding="utf-8"))
            self.assertTrue(runtime["provisional_delivery"])
            self.assertIsNone(runtime["resume_thread_id"])
            self.assertIn("no rollout found", runtime["resume_error"])
            self.assertEqual(runtime["turn_start_thread_id"], "thr_live")

    def test_validator_rejects_unconfirmed_manual_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            runtime = tmp / "runtime.json"
            fixture = tmp / "fixture.json"
            manual = tmp / "manual.md"
            fixture.write_text(
                json.dumps(
                    {
                        "requests": [
                            {
                                "id": "<request-id>",
                                "method": "initialize",
                                "params": {
                                    "clientInfo": {
                                        "name": "tmux-manager-bridge",
                                        "title": "tmux-manager bridge",
                                        "version": "0.1",
                                    },
                                    "capabilities": {
                                        "experimentalApi": False,
                                        "requestAttestation": False,
                                        "optOutNotificationMethods": [],
                                    },
                                },
                            },
                            {"method": "initialized", "params": {}},
                            {
                                "id": "<request-id>",
                                "method": "thread/resume",
                                "params": {"threadId": "thr_test"},
                            },
                            {
                                "id": "<request-id>",
                                "method": "turn/start",
                                "params": {
                                    "threadId": "thr_test",
                                    "input": [
                                        {
                                            "type": "text",
                                            "text": "tmux-manager observed a terminal event.",
                                            "text_elements": [],
                                        }
                                    ],
                                    "cwd": "/tmp/workspace",
                                },
                            },
                        ],
                        "responses": [{"id": 1, "result": {}}],
                        "notifications": [],
                        "canonical_success_signal": "turn_start_response",
                        "protocol_evidence": {
                            "command": "codex app-server generate-ts --experimental",
                            "observed_at": "2026-06-04T00:00:00+00:00",
                            "initialize_params": {
                                "clientInfo": {
                                    "name": "tmux-manager-bridge",
                                    "title": "tmux-manager bridge",
                                    "version": "0.1",
                                },
                                "capabilities": {
                                    "experimentalApi": False,
                                    "requestAttestation": False,
                                    "optOutNotificationMethods": [],
                                },
                            },
                            "turn_start_text_input": {
                                "type": "text",
                                "text": "<wake prompt>",
                                "text_elements": [],
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            manual.write_text(
                "\n".join(
                    [
                        "main_cli_thread_id: thr_test",
                        "received_prompt_timestamp: 2026-06-04T12:00:01+09:00",
                        "received_prompt_first_line: tmux-manager observed a terminal event.",
                        "operator_confirmation: confirmed",
                    ]
                ),
                encoding="utf-8",
            )
            runtime.write_text(
                json.dumps(
                    {
                        "endpoint": "unix:///tmp/app-server.sock",
                        "supplied_thread_id": "thr_test",
                        "resume_thread_id": "thr_test",
                        "resume_error": None,
                        "turn_start_thread_id": "thr_test",
                        "delivered": False,
                        "response_id": "3",
                        "turn_id": None,
                        "request_sequence": ["initialize", "initialized", "thread/resume", "turn/start"],
                        "protocol_fixture_path": str(fixture),
                        "manual_confirmation_note_path": str(manual),
                        "created_at": "2026-06-04T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "operator_confirmation"):
                tmux_bridge.validate_poc_artifacts(runtime)

    def test_validator_rejects_forbidden_fixture_methods_before_manual_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            runtime = tmp / "runtime.json"
            fixture = tmp / "fixture.json"
            manual = tmp / "manual.md"
            fixture.write_text(
                json.dumps(
                    {
                        "requests": [
                            {
                                "id": "<request-id>",
                                "method": "initialize",
                                "params": {
                                    "clientInfo": {
                                        "name": "tmux-manager-bridge",
                                        "title": "tmux-manager bridge",
                                        "version": "0.1",
                                    },
                                    "capabilities": {
                                        "experimentalApi": False,
                                        "requestAttestation": False,
                                        "optOutNotificationMethods": [],
                                    },
                                },
                            },
                            {"method": "initialized", "params": {}},
                            {
                                "id": "<request-id>",
                                "method": "thread/list",
                                "params": {},
                            },
                            {
                                "id": "<request-id>",
                                "method": "turn/start",
                                "params": {
                                    "threadId": "thr_test",
                                    "input": [{"type": "text", "text": "wake", "text_elements": []}],
                                },
                            },
                        ],
                        "responses": [{"id": 1, "result": {}}],
                        "notifications": [],
                        "canonical_success_signal": "turn_start_response",
                        "protocol_evidence": {
                            "command": "codex app-server generate-ts --experimental",
                            "observed_at": "2026-06-04T00:00:00+00:00",
                            "initialize_params": {},
                            "turn_start_text_input": {},
                        },
                    }
                ),
                encoding="utf-8",
            )
            manual.write_text(
                "\n".join(
                    [
                        "main_cli_thread_id: thr_test",
                        "received_prompt_timestamp: 2026-06-04T12:00:01+09:00",
                        "received_prompt_first_line: tmux-manager observed a terminal event.",
                        "operator_confirmation: confirmed_same_thread",
                    ]
                ),
                encoding="utf-8",
            )
            runtime.write_text(
                json.dumps(
                    {
                        "endpoint": "unix:///tmp/app-server.sock",
                        "supplied_thread_id": "thr_test",
                        "resume_thread_id": "thr_test",
                        "resume_error": None,
                        "turn_start_thread_id": "thr_test",
                        "delivered": False,
                        "response_id": "3",
                        "turn_id": None,
                        "request_sequence": ["initialize", "initialized", "thread/list", "turn/start"],
                        "protocol_fixture_path": str(fixture),
                        "manual_confirmation_note_path": str(manual),
                        "created_at": "2026-06-04T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "outbound sequence"):
                tmux_bridge.validate_poc_artifacts(runtime)


if __name__ == "__main__":
    unittest.main()

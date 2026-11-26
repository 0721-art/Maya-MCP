import base64
import json
import re
import socket
import threading
import unittest
from pathlib import Path

from maya_mcp.client import BridgeError
from maya_mcp.commandport import (BEGIN, END, CommandPortClient, build_request,
                                  dispatch, parse_reply)
from maya_mcp.config import Settings


def _request_payload(script: str) -> dict:
    """Decode the outer/inner base64 layers used by the commandPort line."""
    outer_b64 = re.search(r"b64decode\('([^']+)'\)", script).group(1)
    outer = base64.b64decode(outer_b64).decode("utf-8")
    inner_b64 = re.search(r"dispatch\('([^']+)'\)", outer).group(1)
    return json.loads(base64.b64decode(inner_b64).decode("utf-8"))


class CommandPortTests(unittest.TestCase):
    def test_build_request_is_one_line_and_roundtrips_payload(self):
        request_id, line = build_request(
            "get_maya_info", {"scene": "遥控器"}, "token-1", Path("."),
            timeout=12, request_id="req-1")
        self.assertEqual(request_id, "req-1")
        self.assertEqual(line.count("\n"), 1)
        self.assertTrue(line.endswith("\n"))
        payload = _request_payload(line)
        self.assertEqual(payload["id"], "req-1")
        self.assertEqual(payload["type"], "get_maya_info")
        self.assertEqual(payload["params"], {"scene": "遥控器"})
        self.assertEqual(payload["token"], "token-1")
        self.assertEqual(payload["timeout_ms"], 12000)

    def test_parse_reply_validates_markers_id_and_status(self):
        reply = {"id": "req-2", "status": "ok", "result": {"version": "2026"},
                 "ok": True, "data": {"version": "2026"}, "error": None,
                 "warnings": [], "artifacts": []}
        text = "Maya output\n" + BEGIN + json.dumps(reply) + END
        self.assertEqual(parse_reply(text, "req-2"), reply)
        with self.assertRaises(ValueError):
            parse_reply("no markers", "req-2")
        with self.assertRaises(ValueError):
            parse_reply(text, "other-id")
        error = {"id": "req-3", "status": "error",
                 "error": {"kind": "valueerror", "message": "bad input"}}
        with self.assertRaisesRegex(BridgeError, "bad input"):
            parse_reply(BEGIN + json.dumps(error) + END, "req-3")

    def test_socket_client_roundtrip(self):
        ready = threading.Event()
        address = []

        def serve():
            with socket.socket() as server:
                server.bind(("127.0.0.1", 0))
                server.listen(1)
                address.append(server.getsockname()[1])
                ready.set()
                conn, _ = server.accept()
                with conn:
                    data = b""
                    while not data.endswith(b"\n"):
                        chunk = conn.recv(65536)
                        if not chunk:
                            return
                        data += chunk
                    payload = _request_payload(data.decode("utf-8"))
                    reply = {"id": payload["id"], "status": "ok",
                             "result": {"version": "2026"}, "ok": True,
                             "data": {"version": "2026"}, "error": None,
                             "warnings": [], "artifacts": []}
                    conn.sendall((BEGIN + json.dumps(reply) + END).encode("utf-8"))

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        ready.wait(2)
        self.assertTrue(address, "fake commandPort did not start")
        client = CommandPortClient("127.0.0.1", address[0], "token", Path("."))
        self.assertEqual(client.call("get_maya_info"), {"version": "2026"})
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_dispatch_rejects_malformed_or_tokenless_requests(self):
        malformed = json.loads(dispatch(base64.b64encode(b"not-json").decode("ascii")))
        self.assertFalse(malformed["ok"])
        self.assertIn("error", malformed)
        payload = {"id": "x", "type": "get_maya_info", "params": {}, "token": "",
                   "root": str(Path(".").resolve())}
        tokenless = json.loads(dispatch(
            base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")))
        self.assertFalse(tokenless["ok"])
        self.assertIn("token", tokenless["error"]["message"].lower())

    def test_script_has_maya_import_and_no_real_newlines_inside_exec(self):
        _, line = build_request("get_maya_info", {}, "t", Path("."))
        self.assertTrue(line.startswith("import base64;exec(compile("))
        self.assertEqual(line.count("\n"), 1)

    def test_settings_reads_command_port_environment(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {
            "MAYA_MCP_MODE": "commandport",
            "MAYA_MCP_COMMAND_PORT": "9877",
            "MAYA_MCP_TOKEN": "secret",
        }, clear=False):
            settings = Settings.from_env()
        self.assertEqual(settings.mode, "commandport")
        self.assertEqual(settings.port, 9877)
        self.assertEqual(settings.token, "secret")


if __name__ == "__main__":
    unittest.main()

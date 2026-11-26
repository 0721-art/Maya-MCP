import socket
import tempfile
import unittest
from pathlib import Path
from threading import Thread

from maya_mcp.bridge import Bridge
from maya_mcp.client import BridgeClient, BridgeError
from maya_mcp.config import Settings
from maya_mcp.protocol import encode, read_frame
from maya_mcp.policy import output_path, run_commands


class BridgeTests(unittest.TestCase):
    def test_frames_and_utf8(self):
        writer, reader = socket.socketpair()
        with writer, reader, reader.makefile("rb") as stream:
            first = encode({"id": "遥控器"})
            writer.sendall(first[:10])
            writer.sendall(first[10:] + encode({"id": "next"}))
            self.assertEqual(read_frame(stream), {"id": "遥控器"})
            self.assertEqual(read_frame(stream), {"id": "next"})

    def test_socket_dispatch_error_and_token(self):
        settings = Settings(port=0, token="secret")
        with Bridge(settings, {"echo": lambda value: {"echo": value}},
                    mode="test", version="2026", schedule=lambda f: f()) as bridge:
            thread = Thread(target=bridge.serve_forever, daemon=True)
            thread.start()
            try:
                client = BridgeClient("127.0.0.1", bridge.server_address[1], "secret")
                self.assertEqual(client.call("echo", value="遥控器"), {"echo": "遥控器"})
                with self.assertRaisesRegex(BridgeError, "Unknown command"):
                    client.call("unknown")
                with self.assertRaisesRegex(BridgeError, "authentication"):
                    BridgeClient("127.0.0.1", bridge.server_address[1]).call("echo", value=1)
            finally:
                bridge.shutdown()
                thread.join()

    def test_queued_timeout_cancels_before_mutation(self):
        scheduled = []
        settings = Settings(port=0)
        with Bridge(settings, {"explode": lambda: self.fail("expired command ran")},
                    "test", "2026", scheduled.append) as bridge:
            response = bridge.dispatch({"id": "1", "type": "explode", "params": {},
                                        "timeout_ms": 10})
            self.assertEqual(response["error"]["kind"], "timeout")
            scheduled.pop()()

    def test_output_and_code_boundaries(self):
        Path(".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=".tmp") as folder:
            root = Path(folder)
            self.assertEqual(output_path(root, "asset.obj", {".obj"}).parent, root.resolve())
            with self.assertRaisesRegex(ValueError, "project root"):
                output_path(root, "../asset.obj", {".obj"})
        for code in ("import os", "cmds.file(new=True)", "cmds.polyCube(n=open('x'))",
                     "cmds.ls(selection=True)\nx = 1", "cmds.delete('x')"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                run_commands(code, {"polyCube": lambda **kw: kw, "ls": lambda **kw: []})
        self.assertEqual(run_commands("cmds.polyCube(n='safe')", {"polyCube": dict}),
                         [{"n": "safe"}])

import errno
import os
import socket
import subprocess
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from maya_mcp.bridge import Bridge
from maya_mcp.client import BridgeClient, BridgeError
from maya_mcp.config import Settings
from maya_mcp.handlers import MayaHandlers
from maya_mcp.policy import output_path, publish_file, run_commands
from maya_mcp.protocol import encode, read_frame
from maya_mcp.versions import discover


class BridgeTests(unittest.TestCase):
    def test_discovers_only_existing_mayapy(self):
        Path(".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=Path(".tmp").resolve()) as folder:
            root = Path(folder)
            for version in (2023, 2025):
                (root / f"Maya{version}" / "bin").mkdir(parents=True)
            executable = "mayapy.exe" if os.name == "nt" else "mayapy"
            (root / "Maya2025" / "bin" / executable).touch()
            with patch.dict(os.environ, {"MAYA_MCP_INSTALL_ROOTS": str(root)}):
                self.assertNotIn(2023, discover())
                self.assertEqual(discover()[2025].parent.parent, root / "Maya2025")

    def test_frames_and_utf8(self):
        writer, reader = socket.socketpair()
        with writer, reader, reader.makefile("rb") as stream:
            first = encode({"id": "测试场景"})
            writer.sendall(first[:10])
            writer.sendall(first[10:] + encode({"id": "next"}))
            self.assertEqual(read_frame(stream), {"id": "测试场景"})
            self.assertEqual(read_frame(stream), {"id": "next"})

    def test_rejects_incomplete_and_nonfinite_json(self):
        for frame in (b'{"x": 1}', b'{"x": NaN}\n', b'{"x": 1e999}\n', b'[]\n'):
            with self.subTest(frame=frame), self.assertRaises(ValueError):
                read_frame(BytesIO(frame))

    def test_socket_dispatch_error_and_token(self):
        settings = Settings(port=0, token="secret")
        with Bridge(settings, {"echo": lambda value: {"echo": value}},
                    mode="test", version="2026", schedule=lambda f: f()) as bridge:
            thread = Thread(target=bridge.serve_forever, daemon=True)
            thread.start()
            try:
                client = BridgeClient("127.0.0.1", bridge.server_address[1], "secret")
                self.assertEqual(client.call("echo", value="测试场景"), {"echo": "测试场景"})
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
            self.assertFalse(response["ok"])
            self.assertEqual(response["artifacts"], [])
            scheduled.pop()()

    def test_subprocess_timeout_classified_as_timeout(self):
        def slow_render():
            raise subprocess.TimeoutExpired(cmd=["Render"], timeout=1)

        settings = Settings(port=0)
        with Bridge(settings, {"render": slow_render}, "test", "2026", lambda f: f()) as bridge:
            response = bridge.dispatch({"id": "2", "type": "render", "params": {}, "timeout_ms": 5000})
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"]["kind"], "timeout")

    def test_output_and_code_boundaries(self):
        Path(".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=".tmp") as folder:
            root = Path(folder)
            self.assertEqual(output_path(root, "asset.obj", {".obj"}).parent, root.resolve())
            with self.assertRaisesRegex(ValueError, "project root"):
                output_path(root, "../asset.obj", {".obj"})
            with self.assertRaisesRegex(ValueError, "metadata"):
                output_path(root, ".GIT/asset.obj", {".obj"})
            # publish_file falls back cleanly if os.link is unsupported on a filesystem
            src = root / "tmp.obj"
            dst = root / "final.obj"
            src.write_bytes(b"v 0 0 0\n")
            with patch("maya_mcp.policy.os.link", side_effect=OSError(errno.EXDEV, "cross-device")):
                publish_file(src, dst, overwrite=False)
            self.assertEqual(dst.read_bytes(), b"v 0 0 0\n")
            with patch("maya_mcp.policy.os.link", side_effect=OSError(errno.EXDEV, "cross-device")):
                with self.assertRaisesRegex(ValueError, "already exists"):
                    publish_file(src, dst, overwrite=False)
        for code in ("import os", "cmds.file(new=True)", "cmds.polyCube(n=open('x'))",
                     "cmds.ls(selection=True)\nx = 1", "cmds.delete('x')"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                run_commands(code, {"polyCube": lambda **kw: kw, "ls": lambda **kw: []})
        self.assertEqual(run_commands("cmds.polyCube(n='safe')", {"polyCube": dict}),
                         [{"n": "safe"}])

    def test_settings_default_root_tracks_cwd(self):
        Path(".tmp").mkdir(exist_ok=True)
        previous = Path.cwd()
        with tempfile.TemporaryDirectory(dir=Path(".tmp").resolve()) as folder:
            target = Path(folder).resolve()
            os.chdir(target)
            try:
                self.assertEqual(Settings().root.resolve(), target)
            finally:
                os.chdir(previous)

    def test_modify_object_rejects_self_or_descendant_parent(self):
        class FakeCmds:
            def ls(self, name, long=False, type=None):
                mapping = {"|Grp": ["|Grp"], "|Grp|Box": ["|Grp|Box"]}
                return mapping.get(name, [])

            def parent(self, *args, **kwargs):
                raise AssertionError("parent must not be called for self or descendant")

        handlers = MayaHandlers(FakeCmds(), Settings())
        with self.assertRaisesRegex(ValueError, "itself or one of its descendants"):
            handlers.modify_object("|Grp|Box", parent="|Grp|Box")
        with self.assertRaisesRegex(ValueError, "itself or one of its descendants"):
            handlers.modify_object("|Grp", parent="|Grp|Box")

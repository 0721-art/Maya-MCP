import errno
import os
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from io import BytesIO
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from maya_mcp import bridge as bridge_module
from maya_mcp import images as images_module
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

    def test_output_path_rejects_intra_root_file_and_parent_symlinks(self):
        Path(".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=Path(".tmp").resolve()) as folder:
            root = Path(folder).resolve()
            real_dir = root / "real"
            real_dir.mkdir()
            real_file = real_dir / "target.png"
            real_file.write_bytes(b"real")
            link_file = root / "link.png"
            link_dir = root / "linkdir"
            created_real_symlinks = False
            try:
                os.symlink(real_file, link_file)
                os.symlink(real_dir, link_dir, target_is_directory=True)
                created_real_symlinks = True
            except OSError:
                pass

            if created_real_symlinks:
                with self.assertRaisesRegex(ValueError, "symlink"):
                    output_path(root, "link.png", {".png"}, overwrite=True)
                with self.assertRaisesRegex(ValueError, "symlink"):
                    output_path(root, "linkdir/out.png", {".png"}, overwrite=True)
                with self.assertRaisesRegex(ValueError, "symlink"):
                    output_path(root, "real/../link.png", {".png"}, overwrite=True)
            else:
                # Fallback when Windows lacks symlink privilege: verify symlink check runs before resolve()
                fake_links = {os.path.normcase(str(link_file)), os.path.normcase(str(link_dir))}
                orig_is_symlink = Path.is_symlink

                def fake_is_symlink(p):
                    return os.path.normcase(str(p)) in fake_links or orig_is_symlink(p)

                with patch.object(Path, "is_symlink", fake_is_symlink):
                    with self.assertRaisesRegex(ValueError, "symlink"):
                        output_path(root, "link.png", {".png"}, overwrite=True)
                    with self.assertRaisesRegex(ValueError, "symlink"):
                        output_path(root, "linkdir/out.png", {".png"}, overwrite=True)

    def test_handlers_do_not_create_output_dirs_when_validation_fails(self):
        class FakeCmds:
            def about(self, batch=False):
                return False

            def ls(self, *args, **kwargs):
                return []

            def pluginInfo(self, *args, **kwargs):
                return False

            def loadPlugin(self, name, quiet=True):
                raise RuntimeError("plugin unavailable")

        Path(".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=Path(".tmp").resolve()) as folder:
            root = Path(folder).resolve()
            handlers = MayaHandlers(FakeCmds(), Settings(root=root))

            with self.assertRaises(ValueError):
                handlers.export_scene("nested_export/a.fbx", names=["Missing"])
            self.assertFalse((root / "nested_export").exists())

            with self.assertRaises(ValueError):
                handlers.render_view("nested_render/v.png", width=4, height=4)
            self.assertFalse((root / "nested_render").exists())

            with self.assertRaises(ValueError):
                handlers.capture_viewport("nested_vp/v.png", width=4, height=4)
            self.assertFalse((root / "nested_vp").exists())

            with self.assertRaises(ValueError):
                handlers.uv_snapshot("nested_uv/u.png", names=["Missing"], size=128)
            self.assertFalse((root / "nested_uv").exists())

    def test_render_executable_uses_maya_location_and_render_view_passes_full_cam_path(self):
        Path(".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=Path(".tmp").resolve()) as folder:
            root = Path(folder).resolve()
            maya_loc = root / "Maya2025"
            (maya_loc / "bin").mkdir(parents=True)
            render_name = "Render.exe" if os.name == "nt" else "Render"
            fake_render = maya_loc / "bin" / render_name
            fake_render.write_bytes(b"")
            with patch.dict(os.environ, {"MAYA_LOCATION": str(maya_loc)}, clear=False), \
                 patch.object(images_module.sys, "executable", str(root / "other" / "maya")):
                self.assertEqual(images_module._render_executable(None), fake_render)

            # Verify render_view passes the full DAG camera shape path to Render -cam
            captured_args = []

            class FakeCmds:
                def ls(self, name, exactType=None, long=False):
                    if exactType == "camera" and name == "|RigA|camShape":
                        return ["|RigA|camShape"]
                    return []

                def file(self, *args, **kwargs):
                    return None

                def currentTime(self, query=True):
                    return 1.0

            def fake_run(arguments, cwd, stdout, stderr, timeout, creationflags):
                captured_args.extend(arguments)
                (Path(cwd) / "mcp_render.png").write_bytes(b"\x89PNG\r\n\x1a\n")
                return types.SimpleNamespace(returncode=0)

            out = root / "out.png"
            with patch.object(images_module, "_render_executable", return_value=fake_render), \
                 patch.object(images_module.subprocess, "run", side_effect=fake_run), \
                 patch.object(images_module, "publish_png", lambda src, dst, w, h, ow: dst.write_bytes(b"ok")):
                images_module.render_view(FakeCmds(), out, "|RigA|camShape", 64, 64)
            cam_idx = captured_args.index("-cam")
            self.assertEqual(captured_args[cam_idx + 1], "|RigA|camShape")

    def test_start_gui_clears_auto_discovered_mayapy_when_not_explicit(self):
        captured = {}

        class FakeBridge:
            def __init__(self, settings, handlers, mode, version, schedule):
                captured["settings"] = settings
                self.server_address = ("127.0.0.1", 9876)

            def serve_forever(self):
                return None

            def shutdown(self):
                return None

            def server_close(self):
                return None

        fake_cmds = types.ModuleType("maya.cmds")
        fake_cmds.about = lambda batch=False, version=False: "2025" if version else False
        fake_utils = types.ModuleType("maya.utils")
        fake_utils.executeDeferred = lambda f: f()
        fake_maya = types.ModuleType("maya")
        fake_maya.cmds = fake_cmds
        fake_maya.utils = fake_utils

        clean_env = {k: v for k, v in os.environ.items()
                     if k not in {"MAYA_MCP_MAYAPY", "MAYA_MCP_VERSION"}}
        clean_env["MAYA_MCP_TOKEN"] = "gui-token"
        with patch.dict(os.environ, clean_env, clear=True), \
             patch.dict(sys.modules, {"maya": fake_maya, "maya.cmds": fake_cmds, "maya.utils": fake_utils}), \
             patch.object(bridge_module, "Bridge", FakeBridge), \
             patch.object(bridge_module, "Thread") as fake_thread, \
             patch("maya_mcp.config.choose_mayapy", return_value=Path("C:/Maya2026/bin/mayapy.exe")):
            fake_thread.return_value.start.return_value = None
            try:
                bridge_module.start_gui()
            finally:
                bridge_module._gui = None
        self.assertIsNone(captured["settings"].mayapy)

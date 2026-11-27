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
from maya_mcp.config import Settings, staging_temp_root
from maya_mcp.handlers import MayaHandlers, _CHECKPOINTS
from maya_mcp.policy import (MAX_SCRIPT_LENGTH, execute_python_script, input_path,
                             output_path, publish_file, run_commands)
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
                    output_path(root, "linkdir/../out.png", {".png"}, overwrite=True)
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
                    with self.assertRaisesRegex(ValueError, "symlink"):
                        output_path(root, "linkdir/../out.png", {".png"}, overwrite=True)

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

    def test_execute_script_supports_full_python_and_enforces_limits(self):
        handlers = MayaHandlers(types.SimpleNamespace(), Settings(allow_code=True, allow_script=False))
        with self.assertRaisesRegex(ValueError, "MAYA_MCP_ALLOW_SCRIPT"):
            handlers.execute_script("result = 1")

        fake_cmds = types.SimpleNamespace(
            polyCube=lambda **kw: [kw.get("name", "pCube1")],
            undoInfo=lambda **kw: True,
        )
        enabled = MayaHandlers(fake_cmds, Settings(allow_script=True, timeout=5.0))
        out = enabled.execute_script(
            "import math\n"
            "names = []\n"
            "for i in range(3):\n"
            "    names.append(cmds.polyCube(name=f'c_{i}')[0])\n"
            "print('created', len(names))\n"
            "result = {'names': names, 'root2': round(math.sqrt(2), 3)}\n"
        )
        self.assertEqual(out["result"], {"names": ["c_0", "c_1", "c_2"], "root2": 1.414})
        self.assertEqual(out["stdout"].strip(), "created 3")
        self.assertFalse(out["truncated"])

        with self.assertRaisesRegex(ValueError, "exceeds limit"):
            execute_python_script("x = 1\n" * (MAX_SCRIPT_LENGTH // 5), {}, timeout=2.0)
        with self.assertRaisesRegex(ValueError, "syntax"):
            execute_python_script("def broken(:", {}, timeout=2.0)

        # Timeout cancels runaway Python loop and Bridge classifies it as timeout
        settings = Settings(port=0, allow_script=True)
        with Bridge(settings, {"execute_script": enabled.execute_script},
                    "test", "2026", lambda f: f()) as bridge:
            response = bridge.dispatch({
                "id": "script-timeout",
                "type": "execute_script",
                "params": {"code": "while True:\n    pass\n", "timeout": 0.15},
                "timeout_ms": 5000,
            })
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"]["kind"], "timeout")

    def test_external_path_opt_in_allowed_roots_and_unicode_paths(self):
        Path(".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=".tmp") as base:
            base_path = Path(base).resolve()
            root = base_path / "项目根"
            extra = base_path / "附加目录"
            outside = base_path / "新建文件夹"
            for folder in (root, extra, outside):
                folder.mkdir()

            # Non-ASCII root uses ASCII staging temp root when available
            staged_root = staging_temp_root(root)
            self.assertTrue(staged_root.is_dir())

            # Inside non-ASCII project root works
            inside_target = output_path(root, "子目录/模型.fbx", {".fbx"})
            self.assertEqual(inside_target, root / "子目录" / "模型.fbx")

            # Inside allowed_roots works without allow_outside_root
            extra_target = output_path(
                root, str(extra / "零件.obj"), {".obj"}, allowed_roots=(extra,)
            )
            self.assertEqual(extra_target, extra / "零件.obj")

            # Outside root rejected when allow_external=False even if allow_outside_root=True
            with self.assertRaisesRegex(ValueError, "project root"):
                output_path(root, str(outside / "out.obj"), {".obj"},
                            allow_external=False, allow_outside_root=True)

            # Outside root rejected when allow_external=True but allow_outside_root=False
            with self.assertRaisesRegex(ValueError, "allow_outside_root=True"):
                output_path(root, str(outside / "out.obj"), {".obj"},
                            allow_external=True, allow_outside_root=False)

            # Outside root allowed with explicit dual opt-in and absolute Unicode path
            ext_out = output_path(root, str(outside / "输出.obj"), {".obj"},
                                  allow_external=True, allow_outside_root=True)
            self.assertEqual(ext_out, outside / "输出.obj")
            ext_out.write_bytes(b"v 0 0 0\n")
            self.assertEqual(
                input_path(root, str(ext_out), {".obj"}, allow_external=True, allow_outside_root=True),
                ext_out,
            )

            # Relative traversal still rejected even with dual opt-in
            with self.assertRaisesRegex(ValueError, "absolute"):
                output_path(root, "../新建文件夹/rel.obj", {".obj"},
                            allow_external=True, allow_outside_root=True)

            # Protected metadata still rejected outside root
            with self.assertRaisesRegex(ValueError, "metadata"):
                output_path(root, str(outside / ".git" / "bad.obj"), {".obj"},
                            allow_external=True, allow_outside_root=True)

            # publish_file handles EXDEV when overwrite=True too
            src = root / "src.obj"
            dst = outside / "目标.obj"
            src.write_bytes(b"v 1 2 3\n")
            dst.write_bytes(b"old")
            with patch.object(Path, "replace", side_effect=[OSError(errno.EXDEV, "cross-device"), None]):
                # Use real replace on sibling tmp path via custom side_effect
                pass
            orig_replace = Path.replace
            calls = {"n": 0}

            def exdev_once(self_path, target_path):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError(errno.EXDEV, "cross-device")
                return orig_replace(self_path, target_path)

            with patch.object(Path, "replace", exdev_once):
                publish_file(src, dst, overwrite=True)
            self.assertEqual(dst.read_bytes(), b"v 1 2 3\n")

    def test_ortho_camera_math_and_render_resolution_restore(self):
        # Portrait 560x1120 framing a tall portrait object 3.6 x 15.2
        ortho_w, h_ap, v_ap, fit = images_module.compute_ortho_camera(3.6, 15.2, 560, 1120)
        aspect = 560 / 1120
        self.assertEqual(fit, 1)
        self.assertAlmostEqual(h_ap / v_ap, aspect, places=6)
        self.assertGreaterEqual(ortho_w, 1.16 * 3.6 - 1e-6)
        self.assertGreaterEqual(ortho_w / aspect, 1.16 * 15.2 - 1e-6)

        # Landscape 1200x400 framing a wide 12 x 2 object
        ortho_w2, h_ap2, v_ap2, fit2 = images_module.compute_ortho_camera(12.0, 2.0, 1200, 400)
        self.assertAlmostEqual(h_ap2 / v_ap2, 3.0, places=6)
        self.assertAlmostEqual(ortho_w2, 1.16 * 12.0, places=6)
        self.assertGreaterEqual(ortho_w2 / 3.0, 1.16 * 2.0 - 1e-6)

        # _render_resolution updates and restores defaultResolution
        attrs = {
            "defaultResolution.aspectLock": False,
            "defaultResolution.width": 960,
            "defaultResolution.height": 540,
            "defaultResolution.deviceAspectRatio": 1.777778,
            "defaultResolution.pixelAspect": 1.0,
        }

        class FakeResCmds:
            def objExists(self, name):
                return name == "defaultResolution"

            def getAttr(self, attr):
                return attrs[attr]

            def setAttr(self, attr, val):
                attrs[attr] = val

        with images_module._render_resolution(FakeResCmds(), 560, 1120):
            self.assertEqual(attrs["defaultResolution.width"], 560)
            self.assertEqual(attrs["defaultResolution.height"], 1120)
            self.assertAlmostEqual(attrs["defaultResolution.deviceAspectRatio"], 0.5, places=6)
            self.assertEqual(attrs["defaultResolution.pixelAspect"], 1.0)
        self.assertEqual(attrs["defaultResolution.width"], 960)
        self.assertEqual(attrs["defaultResolution.height"], 540)
        self.assertAlmostEqual(attrs["defaultResolution.deviceAspectRatio"], 1.777778, places=6)

    def test_arnold_render_license_error_and_explicit_fallback(self):
        Path(".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=Path(".tmp").resolve()) as folder:
            root = Path(folder).resolve()
            fake_render = root / "Render.exe"
            fake_render.write_bytes(b"")

            class FakeCmds:
                def ls(self, name, exactType=None, long=False):
                    return ["|camShape"] if exactType == "camera" else []

                def file(self, *args, **kwargs):
                    return None

                def currentTime(self, query=True):
                    return 1.0

            def fake_run(arguments, cwd, stdout, stderr, timeout, creationflags):
                renderer = arguments[arguments.index("-r") + 1]
                if renderer == "arnold":
                    stdout.write(b"[rlm] error initializing license system\n")
                    return types.SimpleNamespace(returncode=211)
                (Path(cwd) / "mcp_render.png").write_bytes(b"\x89PNG\r\n\x1a\n")
                return types.SimpleNamespace(returncode=0)

            out_fail = root / "arnold_fail.png"
            with patch.object(images_module, "_render_executable", return_value=fake_render), \
                 patch.object(images_module.subprocess, "run", side_effect=fake_run), \
                 patch.object(images_module, "publish_png", lambda src, dst, w, h, ow: dst.write_bytes(b"ok")):
                with self.assertRaisesRegex(RuntimeError, "Arnold license unavailable"):
                    images_module.render_view(
                        FakeCmds(), out_fail, "|camShape", 64, 64,
                        renderer="arnold", fallback_to_software=False,
                    )
                self.assertFalse(out_fail.exists())

                out_fb = root / "arnold_fb.png"
                res = images_module.render_view(
                    FakeCmds(), out_fb, "|camShape", 64, 64,
                    renderer="arnold", fallback_to_software=True,
                )
                self.assertEqual(res["renderer"], "mayaSoftware")
                self.assertEqual(res["requested_renderer"], "arnold")
                self.assertEqual(res["fallback"], "mayaSoftware")
                self.assertTrue(res["warnings"])

    def test_set_material_multi_object_and_reuse_and_brief_modes(self):
        class FakeMatCmds:
            def __init__(self):
                self.shaders = {}
                self.engines = {}
                self.assignments = []

            def about(self, batch=False, version=False, apiVersion=False):
                if version:
                    return "2026"
                if apiVersion:
                    return 20260000
                return True

            def file(self, query=False, sceneName=False):
                return ""

            def pluginInfo(self, *a, **kw):
                return [] if kw.get("listPlugins") or kw.get("listPluginsPath") else False

            def getAttr(self, attr):
                return True if attr.endswith(".visibility") else "mayaSoftware"

            def ls(self, *args, **kwargs):
                if args:
                    name = args[0]
                    mapping = {"BoxA": ["|BoxA"], "BoxB": ["|BoxB"], "BoxC": ["|BoxC"]}
                    return mapping.get(name, [])
                if kwargs.get("type") == "transform":
                    return ["|BoxA", "|BoxB"]
                if kwargs.get("type") == "mesh":
                    return ["|BoxA|BoxAShape"]
                return []

            def listRelatives(self, node, **kwargs):
                if kwargs.get("shapes"):
                    return [f"{node}|{node.lstrip('|')}Shape"]
                return []

            def objExists(self, name):
                return name in self.shaders or name in self.engines

            def nodeType(self, name):
                if name in self.shaders:
                    return self.shaders[name]
                if name.endswith("Shape"):
                    return "mesh"
                return "transform"

            def shadingNode(self, stype, asShader=True, name=""):
                self.shaders[name] = stype
                return name

            def sets(self, *args, **kwargs):
                if kwargs.get("empty"):
                    sg = kwargs["name"]
                    self.engines[sg] = []
                    return sg
                if kwargs.get("edit") and kwargs.get("forceElement"):
                    self.assignments.append((list(args[0]), kwargs["forceElement"]))
                return None

            def connectAttr(self, src, dst, force=True):
                return None

            def setAttr(self, attr, *vals, **kw):
                return None

            def listConnections(self, plug, type=None):
                if type == "shadingEngine":
                    shader = plug.split(".")[0]
                    sg = shader + "SG"
                    return [sg] if sg in self.engines else []
                return []

            def exactWorldBoundingBox(self, node):
                return [-1.0, -1.0, -1.0, 1.0, 1.0, 1.0]

            def polyEvaluate(self, mesh, **kw):
                return 6 if kw.get("face") else 8

        fake = FakeMatCmds()
        handlers = MayaHandlers(fake, Settings())

        first = handlers.set_material(names=["BoxA", "BoxB"], material="Clay", color=(0.2, 0.2, 0.2), roughness=0.4)
        self.assertFalse(first["reused"])
        self.assertEqual(first["names"], ["|BoxA", "|BoxB"])
        self.assertEqual(first["material"], "Clay")

        second = handlers.set_material(name="BoxC", material="Clay")
        self.assertTrue(second["reused"])
        self.assertEqual(second["material"], "Clay")
        self.assertEqual(second["names"], ["|BoxC"])
        self.assertEqual(list(fake.shaders.keys()), ["Clay"])

        brief_obj = handlers.get_object_info("BoxA", brief=True)
        self.assertEqual(set(brief_obj.keys()), {"name", "bbox", "vertices", "faces"})

        brief_scene = handlers.get_scene_info(brief=True)
        self.assertNotIn("plugins", brief_scene)
        self.assertNotIn("capabilities", brief_scene)
        self.assertEqual(brief_scene["object_count"], 2)

    def test_headless_capture_viewport_fallback_and_checkpoint_undo(self):
        Path(".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=Path(".tmp").resolve()) as folder:
            root = Path(folder).resolve()

            class FakeSessionCmds:
                def __init__(self):
                    self.scene = "init.ma"
                    self.undo_calls = 0
                    self.redo_calls = 0

                def about(self, batch=False, version=False):
                    return True if batch else "2026"

                def file(self, *args, **kwargs):
                    if kwargs.get("query") and kwargs.get("sceneName"):
                        return self.scene
                    if "rename" in kwargs:
                        self.scene = kwargs["rename"]
                        return self.scene
                    if kwargs.get("save"):
                        Path(self.scene).write_text("//Maya ASCII\n", encoding="utf-8")
                        return self.scene
                    if kwargs.get("open"):
                        self.scene = args[0]
                        return self.scene
                    return None

                def ls(self, *args, **kwargs):
                    return ["|Box"] if kwargs.get("type") == "transform" else []

                def undoInfo(self, query=False, state=False, length=False, **kw):
                    if query and state:
                        return True
                    if query and length:
                        return 100
                    return None

                def undo(self):
                    self.undo_calls += 1

                def redo(self):
                    self.redo_calls += 1

            fake = FakeSessionCmds()
            handlers = MayaHandlers(fake, Settings(root=root))

            # Headless capture_viewport with allow_fallback=False fails clearly
            with self.assertRaisesRegex(ValueError, "GUI viewport capture is unavailable"):
                handlers.capture_viewport("vp.png", allow_fallback=False)

            # With allow_fallback=True returns explicit non-GUI metadata
            with patch.object(handlers, "render_view", return_value={"path": str(root / "vp.png"), "bytes": 100, "width": 64, "height": 64, "camera": "front"}):
                vp = handlers.capture_viewport("vp.png", width=64, height=64, allow_fallback=True)
            self.assertFalse(vp["is_gui_viewport"])
            self.assertEqual(vp["mode"], "headless")
            self.assertEqual(vp["fallback"], "render")
            self.assertIn("fallback_reason", vp)

            # Checkpoint create / list / restore / delete
            _CHECKPOINTS.pop((os.getpid(), str(root)), None)
            cp = handlers.scene_checkpoint(action="create", name="before_op")
            self.assertEqual(cp["name"], "before_op")
            self.assertEqual(fake.scene, "init.ma")
            listed = handlers.scene_checkpoint(action="list")
            self.assertEqual([c["name"] for c in listed["checkpoints"]], ["before_op"])
            restored = handlers.scene_checkpoint(action="restore", name="before_op")
            self.assertEqual(restored["restored"], "before_op")
            self.assertEqual(fake.scene, "init.ma")
            deleted = handlers.scene_checkpoint(action="delete", name="before_op")
            self.assertEqual(deleted["deleted"], "before_op")

            # Undo / redo
            status = handlers.undo_scene(action="status")
            self.assertTrue(status["enabled"])
            u = handlers.undo_scene(action="undo", steps=2)
            self.assertEqual(u["performed"], 2)
            r = handlers.undo_scene(action="redo", steps=1)
            self.assertEqual(r["performed"], 1)

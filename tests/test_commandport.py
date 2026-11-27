import base64
import contextlib
import io
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from maya_mcp.client import BridgeError
from maya_mcp.commandport import (BEGIN, END, REPO_ROOT, CommandPortClient,
                                  build_request, dispatch, parse_reply)
from maya_mcp.config import Settings


def _request_payload(script: str) -> dict:
    """Decode the outer/inner base64 layers used by the commandPort line."""
    outer_b64 = re.search(r"b64decode\('([^']+)'\)", script).group(1)
    outer = base64.b64decode(outer_b64).decode("utf-8")
    inner_b64 = re.search(r"dispatch\('([^']+)'\)", outer).group(1)
    return json.loads(base64.b64decode(inner_b64).decode("utf-8"))


def _fake_maya_modules(version="2026"):
    cmds = types.ModuleType("maya.cmds")
    cmds.about = lambda version=False, apiVersion=False, batch=False: (
        "2026" if version else (20260000 if apiVersion else False)
    )
    cmds.file = lambda query=False, sceneName=False: ""
    cmds.pluginInfo = lambda *a, **kw: [] if kw.get("listPlugins") or kw.get("listPluginsPath") else False
    cmds.getAttr = lambda attr: "mayaSoftware"
    cmds.ls = lambda *a, **kw: []
    cmds.listRelatives = lambda *a, **kw: []
    cmds.objExists = lambda name: False
    cmds.polyCube = lambda **kw: ["pCube1"]
    cmds.polySphere = lambda **kw: ["pSphere1"]
    cmds.polyCylinder = lambda **kw: ["pCylinder1"]
    cmds.polyPlane = lambda **kw: ["pPlane1"]
    cmds.polyEvaluate = lambda *a, **kw: 0
    cmds.xform = lambda *a, **kw: [0, 0, 0]
    maya_pkg = types.ModuleType("maya")
    maya_pkg.cmds = cmds
    return {"maya": maya_pkg, "maya.cmds": cmds}


class CommandPortTests(unittest.TestCase):
    def test_build_request_is_one_line_and_roundtrips_payload(self):
        with tempfile.TemporaryDirectory() as external_root:
            project_root = Path(external_root).resolve()
            request_id, line = build_request(
                "get_maya_info", {"scene": "测试场景"}, "token-1", project_root,
                timeout=12, request_id="req-1")
            self.assertEqual(request_id, "req-1")
            self.assertEqual(line.count("\n"), 1)
            self.assertTrue(line.endswith("\n"))
            self.assertNotIn(BEGIN, line)
            self.assertNotIn(END, line)
            payload = _request_payload(line)
            self.assertEqual(payload["id"], "req-1")
            self.assertEqual(payload["type"], "get_maya_info")
            self.assertEqual(payload["params"], {"scene": "测试场景"})
            self.assertEqual(payload["token"], "token-1")
            self.assertEqual(payload["root"], str(project_root))
            self.assertEqual(payload["timeout_ms"], 12000)

    def test_generated_script_uses_repo_root_not_project_root_and_works_via_eval_and_stdout(self):
        with tempfile.TemporaryDirectory() as external_root:
            project_root = Path(external_root).resolve()
            request_id, line = build_request(
                "get_maya_info", {}, "tok-eval", project_root, request_id="req-eval")
            fake_sys_path = [p for p in sys.path if Path(p).resolve() != project_root]
            # Strip REPO_ROOT to verify the script adds REPO_ROOT (not project_root).
            stripped_path = [p for p in fake_sys_path if Path(p).resolve() != REPO_ROOT.resolve()]
            fake_modules = _fake_maya_modules()
            with patch.dict(os.environ, {"MAYA_MCP_TOKEN": "tok-eval",
                                         "MAYA_MCP_PROJECT_ROOT": str(project_root)}, clear=False), \
                 patch.dict(sys.modules, fake_modules), \
                 patch.object(sys, "path", list(stripped_path)):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    returned = eval(line.strip(), {})
                    returned_again = eval(line.strip(), {})
                # REPO_ROOT added once, project_root never added to sys.path
                self.assertEqual(sys.path.count(str(REPO_ROOT)), 1)
                self.assertNotIn(str(project_root), sys.path)
                # Works both via expression return value (echoOutput=False) and printed output (echoOutput=True)
                self.assertEqual(parse_reply(returned, request_id)["result"]["version"], "2026")
                self.assertEqual(parse_reply(returned_again, request_id)["result"]["version"], "2026")
                self.assertEqual(parse_reply(buf.getvalue(), request_id)["result"]["version"], "2026")

    def test_generated_script_wraps_import_errors_with_markers(self):
        with tempfile.TemporaryDirectory() as empty_repo, tempfile.TemporaryDirectory() as project_dir:
            request_id, line = build_request(
                "get_maya_info", {}, "tok", Path(project_dir),
                request_id="req-import-err", repo_root=Path(empty_repo))
            # Simulate Maya without maya_mcp in sys.modules or sys.path
            saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "maya_mcp" or k.startswith("maya_mcp.")}
            try:
                with patch.object(sys, "path", [empty_repo]):
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        returned = eval(line.strip(), {})
            finally:
                sys.modules.update(saved)
            self.assertIn(BEGIN, returned)
            self.assertIn(END, returned)
            with self.assertRaises(BridgeError) as ctx:
                parse_reply(returned, request_id)
            self.assertEqual(ctx.exception.kind, "internal")
            self.assertIn("ModuleNotFoundError", str(ctx.exception))

    def test_parse_reply_validates_markers_id_finite_json_and_status(self):
        reply = {"id": "req-2", "status": "ok", "result": {"version": "2026"},
                 "ok": True, "data": {"version": "2026"}, "error": None,
                 "warnings": [], "artifacts": []}
        text = "Maya output\n" + BEGIN + json.dumps(reply) + END
        self.assertEqual(parse_reply(text, "req-2"), reply)
        for bad in ("no markers", BEGIN + "not-json" + END, BEGIN + '{"x": NaN}' + END, BEGIN + "[]" + END):
            with self.subTest(bad=bad), self.assertRaises(BridgeError) as ctx:
                parse_reply(bad, "req-2")
            self.assertEqual(ctx.exception.kind, "protocol")
        with self.assertRaises(BridgeError) as ctx:
            parse_reply(text, "other-id")
        self.assertEqual(ctx.exception.kind, "protocol")
        # Error with id=None (e.g. decode error before request id known) surfaces real error
        error_no_id = {"id": None, "status": "error",
                       "error": {"kind": "validation", "message": "malformed payload"}}
        with self.assertRaises(BridgeError) as ctx:
            parse_reply(BEGIN + json.dumps(error_no_id) + END, "req-3")
        self.assertEqual(ctx.exception.kind, "validation")
        self.assertIn("malformed payload", str(ctx.exception))

    def test_socket_client_executes_script_and_trims_envelope(self):
        ready = threading.Event()
        address = []
        fake_modules = _fake_maya_modules()

        with tempfile.TemporaryDirectory() as external_root:
            project_root = Path(external_root).resolve()

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
                        # Simulate Maya commandPort (echoOutput=False): eval expression and append \n\x00
                        with contextlib.redirect_stdout(io.StringIO()):
                            result = eval(data.decode("utf-8").strip(), {})
                        conn.sendall((result + "\n\x00").encode("utf-8"))

            with patch.dict(os.environ, {"MAYA_MCP_TOKEN": "tok-socket",
                                         "MAYA_MCP_PROJECT_ROOT": str(project_root)}, clear=False), \
                 patch.dict(sys.modules, fake_modules):
                thread = threading.Thread(target=serve, daemon=True)
                thread.start()
                ready.wait(2)
                self.assertTrue(address, "fake commandPort did not start")
                client = CommandPortClient("127.0.0.1", address[0], "tok-socket", project_root)
                envelope = client.envelope("get_maya_info")
                self.assertEqual(set(envelope.keys()), {"ok", "data", "error", "warnings", "artifacts"})
                self.assertTrue(envelope["ok"])
                self.assertEqual(envelope["data"]["version"], "2026")
                thread.join(2)
                self.assertFalse(thread.is_alive())

    def test_exchange_stops_immediately_on_nul_without_end_marker(self):
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
                    conn.recv(65536)
                    # Send Maya NUL terminator WITHOUT END marker, keeping socket open briefly
                    conn.sendall(b"SyntaxError: boom\n\x00")
                    time.sleep(2)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        ready.wait(2)
        client = CommandPortClient("127.0.0.1", address[0], "tok", Path("."), timeout=30.0)
        started = time.monotonic()
        with self.assertRaises(BridgeError) as ctx:
            client.envelope("get_maya_info")
        elapsed = time.monotonic() - started
        self.assertEqual(ctx.exception.kind, "protocol")
        self.assertIn("SyntaxError: boom", str(ctx.exception))
        self.assertLess(elapsed, 1.5, "client hung waiting for timeout instead of stopping on NUL")

    def test_dispatch_fail_closed_auth_root_sandbox_and_error_kinds(self):
        malformed = json.loads(dispatch(base64.b64encode(b"not-json").decode("ascii")))
        self.assertFalse(malformed["ok"])
        self.assertEqual(malformed["error"]["kind"], "validation")

        def encode_req(payload):
            return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")

        with tempfile.TemporaryDirectory() as project_dir, tempfile.TemporaryDirectory() as other_dir:
            base_req = {"id": "x", "type": "get_maya_info", "params": {},
                        "token": "secret-令牌", "root": project_dir}
            # 1) Missing MAYA_MCP_TOKEN in Maya env -> fail closed!
            clean_env = {k: v for k, v in os.environ.items()
                         if k not in {"MAYA_MCP_TOKEN", "MAYA_MCP_COMMAND_TOKEN", "MAYA_MCP_PROJECT_ROOT"}}
            with patch.dict(os.environ, clean_env, clear=True):
                res = json.loads(dispatch(encode_req(base_req)))
                self.assertFalse(res["ok"])
                self.assertEqual(res["error"]["kind"], "validation")
                self.assertIn("MAYA_MCP_TOKEN", res["error"]["message"])

            # 2) Wrong token -> validation error
            with patch.dict(os.environ, {"MAYA_MCP_TOKEN": "expected-token"}, clear=False):
                res = json.loads(dispatch(encode_req(base_req)))
                self.assertFalse(res["ok"])
                self.assertEqual(res["error"]["kind"], "validation")
                self.assertIn("authentication", res["error"]["message"].lower())

            # 3) Mismatched project root when MAYA_MCP_PROJECT_ROOT is configured -> validation error
            with patch.dict(os.environ, {"MAYA_MCP_TOKEN": "secret-令牌",
                                         "MAYA_MCP_PROJECT_ROOT": other_dir}, clear=False):
                res = json.loads(dispatch(encode_req(base_req)))
                self.assertFalse(res["ok"])
                self.assertEqual(res["error"]["kind"], "validation")
                self.assertIn("MAYA_MCP_PROJECT_ROOT", res["error"]["message"])

            # 4) Matching UTF-8 token + RuntimeError from Maya -> maya_error kind
            fake_modules = _fake_maya_modules()
            fake_modules["maya.cmds"].ls = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("maya boom"))
            with patch.dict(os.environ, {"MAYA_MCP_TOKEN": "secret-令牌",
                                         "MAYA_MCP_PROJECT_ROOT": project_dir}, clear=False), \
                 patch.dict(sys.modules, fake_modules):
                res = json.loads(dispatch(encode_req({**base_req, "type": "get_scene_info"})))
                self.assertFalse(res["ok"])
                self.assertEqual(res["error"]["kind"], "maya_error")
                self.assertIn("maya boom", res["error"]["message"])

            # 5) execute_maya_code respects Maya-side MAYA_MCP_ALLOW_CODE
            with patch.dict(os.environ, {"MAYA_MCP_TOKEN": "secret-令牌",
                                         "MAYA_MCP_PROJECT_ROOT": project_dir,
                                         "MAYA_MCP_ALLOW_CODE": "0"}, clear=False), \
                 patch.dict(sys.modules, _fake_maya_modules()):
                denied = json.loads(dispatch(encode_req({
                    **base_req, "type": "execute_maya_code",
                    "params": {"code": "cmds.objExists('x')"},
                })))
                self.assertFalse(denied["ok"])
                self.assertEqual(denied["error"]["kind"], "validation")

            with patch.dict(os.environ, {"MAYA_MCP_TOKEN": "secret-令牌",
                                         "MAYA_MCP_PROJECT_ROOT": project_dir,
                                         "MAYA_MCP_ALLOW_CODE": "1"}, clear=False), \
                 patch.dict(sys.modules, _fake_maya_modules()):
                allowed = json.loads(dispatch(encode_req({
                    **base_req, "type": "execute_maya_code",
                    "params": {"code": "cmds.objExists('x')"},
                })))
                self.assertTrue(allowed["ok"])
                self.assertEqual(allowed["data"], {"results": [False]})

    def test_client_rejects_empty_token_and_bad_port(self):
        with self.assertRaisesRegex(ValueError, "MAYA_MCP_TOKEN"):
            CommandPortClient("127.0.0.1", 7001, "", Path("."))
        with self.assertRaisesRegex(ValueError, "commandPort"):
            CommandPortClient("127.0.0.1", 0, "tok", Path("."))
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            CommandPortClient("0.0.0.0", 7001, "tok", Path("."))

    def test_settings_reads_command_port_environment(self):
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

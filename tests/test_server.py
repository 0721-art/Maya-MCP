import asyncio
import importlib.util
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from maya_mcp.client import BridgeClient, BridgeError
from maya_mcp.commandport import CommandPortClient
from maya_mcp.config import Settings
from maya_mcp.handlers import MayaHandlers
from maya_mcp.session import MayaSession

HAS_MCP = importlib.util.find_spec("mcp") is not None
LOCAL_TOOLS = {"list_maya_installations", "compose_sheet", "submit_job", "job_status"}


class SessionModeTests(unittest.TestCase):
    def test_commandport_mode_uses_commandport_client_and_never_starts_worker(self):
        settings = Settings(mode="commandport", port=7001, token="tok")
        session = MayaSession(settings)
        self.assertIsInstance(session.client, CommandPortClient)
        with patch.object(session.client, "call", side_effect=BridgeError("connection", "refused")), \
             patch.object(session, "_start_worker", side_effect=AssertionError("must not start worker")):
            with self.assertRaises(BridgeError) as ctx:
                session.call("get_maya_info")
            self.assertEqual(ctx.exception.kind, "connection")

    def test_gui_mode_uses_bridge_client_and_never_falls_back_to_worker(self):
        settings = Settings(mode="gui", port=9876, token="tok")
        session = MayaSession(settings)
        self.assertIsInstance(session.client, BridgeClient)
        with patch.object(session.client, "call", side_effect=BridgeError("connection", "refused")), \
             patch.object(session, "_start_worker", side_effect=AssertionError("must not start worker")):
            with self.assertRaises(BridgeError) as ctx:
                session.call("get_maya_info")
            self.assertEqual(ctx.exception.kind, "connection")

    def test_auto_mode_reuses_existing_bridge_without_starting_worker(self):
        settings = Settings(mode="auto", port=9876, token="tok")
        session = MayaSession(settings)
        with patch.object(session.client, "call", return_value={"mode": "gui", "version": "2026"}), \
             patch.object(session.client, "envelope", return_value={"ok": True, "data": {}, "error": None,
                                                                    "warnings": [], "artifacts": []}), \
             patch.object(session, "_start_worker", side_effect=AssertionError("must not start worker")):
            reply = session.call("get_scene_info")
            self.assertTrue(reply["ok"])
            self.assertTrue(session.ready)

    def test_headless_mode_starts_worker_when_port_unused(self):
        settings = Settings(mode="headless", port=9876, token="tok")
        session = MayaSession(settings)
        started = []

        def fake_start():
            started.append(True)

        with patch.object(session.client, "call", side_effect=BridgeError("connection", "refused")), \
             patch.object(session, "_start_worker", side_effect=fake_start), \
             patch.object(session.client, "envelope", return_value={"ok": True, "data": {"mode": "headless"},
                                                                    "error": None, "warnings": [], "artifacts": []}):
            reply = session.call("get_maya_info")
            self.assertTrue(reply["ok"])
            self.assertEqual(len(started), 1)

    def test_commandport_probe_rejects_wrong_version(self):
        settings = Settings(mode="commandport", port=7001, token="tok", version=2025)
        session = MayaSession(settings)
        with patch.object(session.client, "call", return_value={"mode": "gui", "version": "2026"}):
            with self.assertRaises(BridgeError) as ctx:
                session.call("get_maya_info")
            self.assertEqual(ctx.exception.kind, "configuration")

    def test_handlers_command_surface_is_38_commands(self):
        commands = MayaHandlers(object(), Settings()).commands()
        self.assertEqual(len(commands), 38)
        self.assertEqual(len(commands) + len(LOCAL_TOOLS), 42)


@unittest.skipUnless(HAS_MCP, "mcp SDK is required to instantiate FastMCP server")
class ServerSchemaTests(unittest.TestCase):
    def test_server_registers_all_42_tools_with_valid_schemas(self):
        from maya_mcp.server import make_server

        server = make_server(Settings(mode="headless", port=9876))
        tools = asyncio.run(server.list_tools())
        names = {tool.name for tool in tools}
        expected = set(MayaHandlers(object(), Settings()).commands()) | LOCAL_TOOLS
        self.assertEqual(len(tools), 42)
        self.assertEqual(names, expected)
        for tool in tools:
            self.assertTrue(tool.description, f"Missing description for {tool.name}")
            schema = tool.inputSchema
            self.assertIsInstance(schema, dict, tool.name)
            self.assertEqual(schema.get("type"), "object", tool.name)
            self.assertIn("properties", schema, tool.name)


@unittest.skipUnless(HAS_MCP, "mcp SDK is required to import tests/maya_smoke.py")
class SmokeCliTests(unittest.TestCase):
    def test_smoke_year_flag_parses_and_reports_untested_when_missing(self):
        import tests.maya_smoke as maya_smoke

        parsed = maya_smoke.parse_args(["--year", "2026"])
        self.assertEqual(parsed.year, 2026)
        buf = io.StringIO()
        with patch.object(maya_smoke, "discover", return_value={2026: Path("fake/mayapy.exe")}), \
             redirect_stdout(buf):
            code = maya_smoke.main(["--year", "2022"])
        self.assertEqual(code, 2)
        self.assertIn("UNTESTED: Maya 2022 mayapy not found", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

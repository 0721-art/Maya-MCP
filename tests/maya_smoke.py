"""Run with the host Python that has the project's MCP SDK installed."""

import asyncio
import os
import socket
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from maya_mcp.config import Settings

REQUIRED = {"get_maya_info", "get_scene_info", "get_object_info", "create_object",
            "modify_object", "delete_object", "set_material", "render_view", "capture_viewport",
            "create_layer", "assign_layer", "uv_snapshot", "save_scene", "export_scene",
            "execute_maya_code"}


async def smoke():
    settings = Settings.from_env()
    if not settings.mayapy.is_file():
        print(f"SKIP: mayapy does not exist at {settings.mayapy}")
        return
    root = Path.cwd()
    (root / ".tmp").mkdir(exist_ok=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="smoke-", dir=root / ".tmp") as folder:
        env = {**os.environ, "MAYA_MCP_PORT": str(port), "MAYA_MCP_MODE": "headless",
               "MAYA_MCP_PROJECT_ROOT": folder, "MAYA_MCP_MAYAPY": str(settings.mayapy)}
        args = StdioServerParameters(command=sys.executable, args=["-m", "maya_mcp.server"],
                                     cwd=root, env=env)
        async with stdio_client(args) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert REQUIRED <= {tool.name for tool in tools.tools}, tools

                async def call(name, **values):
                    result = await client.call_tool(name, values)
                    assert not result.isError, (name, result)
                    return result.structuredContent

                info = await call("get_maya_info")
                assert info["version"] == "2026" and info["mode"] == "headless", info
                await call("create_layer", name="smoke_geo")
                obj = await call("create_object", kind="cube", name="SmokeCube", layer="smoke_geo")
                assert obj["faces"] == 6 and obj["uv_sets"] == ["map1"], obj
                obj = await call("modify_object", name="SmokeCube", new_name="MovedCube",
                                 translation=[1, 2, 0])
                assert obj["translation"] == [1, 2, 0], obj
                assigned = await call("set_material", name="MovedCube", material="SmokeMat",
                                      color=[0.25, 0.4, 0.6])
                assert assigned["material"] == "SmokeMat", assigned
                scene = await call("get_scene_info")
                assert scene["triangles"] == 12 and "smoke_geo" in scene["layers"], scene
                executed = await call("execute_maya_code", code="cmds.objExists('MovedCube')")
                assert executed["results"] == [True], executed
                result = await client.call_tool("execute_maya_code", {"code": "cmds.quit(force=True)"})
                assert result.isError, result
                await call("save_scene", path="smoke.mb")
                for ext in ("obj", "fbx"):
                    await call("export_scene", path=f"smoke.{ext}", names=["MovedCube"])
                view = await call("capture_viewport", path="view.png", width=128, height=128)
                assert view["fallback"] == "render", view
                await call("uv_snapshot", path="uv.png", names=["MovedCube"], size=128)
                for filename in ("smoke.mb", "smoke.obj", "smoke.fbx", "view.png", "uv.png"):
                    assert (Path(folder) / filename).stat().st_size > 0, filename
                await call("delete_object", names=["MovedCube"])
                assert (await call("get_scene_info"))["mesh_count"] == 0
    print("PASS: MCP stdio → JSON bridge → Maya 2026, modeling, export, render, UV")


if __name__ == "__main__":
    asyncio.run(smoke())

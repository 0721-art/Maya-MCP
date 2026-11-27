"""Run with the host Python that has the project's MCP SDK installed."""

import argparse
import asyncio
import os
import socket
import sys
import tempfile
import traceback
from pathlib import Path

from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maya_mcp.versions import discover

REQUIRED = {"get_maya_info", "get_scene_info", "get_object_info", "create_object",
            "modify_object", "delete_object", "set_material", "render_view", "capture_viewport",
            "create_layer", "assign_layer", "uv_snapshot", "save_scene", "export_scene",
            "execute_maya_code", "create_curve", "loft_mesh", "mesh_extrude", "mesh_bevel",
            "mesh_mirror", "array_objects", "mesh_boolean", "mesh_combine", "mesh_separate",
            "group_objects", "mesh_topology", "mesh_retopo", "mesh_reduce", "mesh_rebuild",
            "mesh_snap", "set_symmetry", "uv_project", "uv_unfold", "uv_layout", "uv_sets", "uv_inspect",
            "bake_map", "bake_arnold_texture", "compose_sheet",
            "list_maya_installations", "submit_job", "job_status"}


async def smoke(year, executable):
    root = Path.cwd()
    (root / ".tmp").mkdir(exist_ok=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="smoke-", dir=root / ".tmp", ignore_cleanup_errors=True) as folder:
        env = {**os.environ, "MAYA_MCP_PORT": str(port), "MAYA_MCP_MODE": "headless",
               "MAYA_MCP_PROJECT_ROOT": folder, "MAYA_MCP_MAYAPY": str(executable),
               "MAYA_MCP_VERSION": str(year), "MAYA_MCP_ALLOW_CODE": "1"}
        args = StdioServerParameters(command=sys.executable, args=["-m", "maya_mcp.server"],
                                     cwd=root, env=env)
        async with stdio_client(args) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert REQUIRED <= {tool.name for tool in tools.tools}, tools

                async def call(name, /, **values):
                    result = await client.call_tool(name, values)
                    response = result.structuredContent
                    assert not result.isError and response and response["ok"], (name, result)
                    return response["data"]

                info = await call("get_maya_info")
                assert str(year) in info["version"] and info["mode"] == "headless", info
                print(f"Maya {info['version']} API {info['api_version']}, Python {info['python']}, "
                      f"Transfer Maps {info['capabilities']['transfer_maps']}")
                installations = await call("list_maya_installations")
                assert any(item["version"] == year and item["installed"] for item in installations["versions"])
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
                assert result.structuredContent["error"]["kind"] == "validation", result
                await call("save_scene", path="smoke.mb")
                for ext in ("obj", "fbx"):
                    await call("export_scene", path=f"smoke.{ext}", names=["MovedCube"])
                view = await call("capture_viewport", path="view.png", width=128, height=128)
                assert view["fallback"] == "render", view
                job = await call("submit_job", tool="uv_snapshot",
                                 arguments={"path": "uv.png", "names": ["MovedCube"], "size": 128})
                async with asyncio.timeout(30):
                    while True:
                        state = await call("job_status", job_id=job["job_id"])
                        if state["state"] in {"succeeded", "failed"}:
                            assert state["result"]["ok"] and state["result"]["artifacts"], state
                            break
                        await asyncio.sleep(0.05)
                sheet = await call("compose_sheet", rows=[["view.png", "uv.png"]], path="sheet.png")
                assert sheet["width"] == 256 and sheet["height"] == 128, sheet
                for filename in ("smoke.mb", "smoke.obj", "smoke.fbx", "view.png", "uv.png"):
                    assert (Path(folder) / filename).stat().st_size > 0, filename
                await call("delete_object", names=["MovedCube"])
                assert (await call("get_scene_info"))["mesh_count"] == 0
                for index in range(2):
                    await call("create_curve", name=f"Profile{index}",
                               points=[[0, index, 0], [1, index, 0], [2, index, 1]])
                mesh = await call("loft_mesh", curves=["Profile0", "Profile1"], name="Loft")
                assert mesh["faces"] > 0, mesh
                await call("mesh_extrude", name="Loft", faces=[0], distance=0.1)
                array = await call("array_objects", name="Loft", prefix="Copy", count=2, offset=[4, 0, 0])
                assert len(array["objects"]) == 2, array
                await call("group_objects", name="Profiles", names=["Profile0", "Profile1"])
                await call("create_object", kind="cube", name="Base")
                await call("create_object", kind="cube", name="Cutter", translation=[0.25, 0.25, 0.25])
                await call("mesh_boolean", first="Base", second="Cutter", name="Cut")
                await call("mesh_bevel", name="Base", edges=[0, 1, 2, 3], segments=1)
                await call("mesh_mirror", source="Base", name="Mirrored")
                await call("create_object", kind="cube", name="Apart", translation=[5, 0, 0])
                await call("mesh_combine", names=["Base", "Apart"], name="Combined")
                separated = await call("mesh_separate", source="Combined", name="Separated")
                assert len(separated["objects"]) == 2, separated
                await call("create_object", kind="sphere", name="High", segments=20)
                reduced = await call("mesh_retopo", source="High", name="Low", target_faces=120)
                assert 0 < reduced["faces"] < 400, reduced
                print(f"Maya {year} retopology: {reduced['algorithm']}, {reduced['faces']} faces")
                topology = await call("mesh_topology", name="Low", symmetry_axis="x")
                assert topology["faces"] == reduced["faces"] and not topology["non_manifold_edges"], topology
                await call("mesh_snap", name="Low", target="High", vertices=[0])
                await call("uv_sets", name="Low", action="create", uv_set="bakeUV")
                await call("uv_project", name="Low", method="automatic", uv_set="bakeUV")
                unfolded = await call("uv_unfold", name="Low", uv_set="bakeUV")
                print(f"Maya {year} UV unfold: {unfolded['algorithm']}")
                await call("uv_layout", names=["Low"], uv_set="bakeUV")
                uv = await call("uv_inspect", name="Low", uv_set="bakeUV")
                assert uv["tiles"] == [{"u": 0, "v": 0, "udim": 1001}] and not uv["unmapped_faces"], uv
                await call("create_object", kind="sphere", name="BakeTarget", segments=12)
                await call("set_material", name="High", material="BakeSourceMat", color=[0.3, 0.75, 0.25])
                for kind in ("normal", "color", "ao", "curvature"):
                    result = await call("bake_map", source="High", target="BakeTarget", kind=kind,
                                        path=f"{kind}.png", size=32, max_distance=0.2, ao_samples=4)
                    with Image.open(result["path"]) as image:
                        assert image.size == (32, 32), result
                        image.load()
                        if kind == "color":
                            red, green, blue = image.convert("RGB").getpixel((16, 16))
                            assert green > red > blue > 0, (kind, result)
                        else:
                            assert any(hi > lo for lo, hi in image.convert("RGB").getextrema()), (kind, result)
                if os.environ.get("MAYA_MCP_SMOKE_ARNOLD") == "1" and info["capabilities"]["plugins"]["arnold"]["available"]:
                    result = await client.call_tool("bake_arnold_texture", {
                        "name": "High", "kind": "ao", "path": "arnold-ao.exr", "size": 32, "aa_samples": 1})
                    response = result.structuredContent
                    if response["ok"]:
                        assert Path(response["data"]["path"]).read_bytes().startswith(b"\x76\x2f\x31\x01"), response
                        print(f"PASS: Maya {year} Arnold AO EXR")
                    else:
                        assert "license state" in response["error"]["message"], response
                        assert not response["artifacts"] and not (Path(folder) / "arnold-ao.exr").exists()
                        print(f"UNAVAILABLE: Maya {year} {response['error']['message']}")
    print(f"PASS: MCP stdio -> Maya {year}: modeling, topology, UV, four PNG bake maps, render, export, jobs")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run Maya MCP stdio smoke tests.")
    parser.add_argument(
        "--year",
        type=int,
        choices=tuple(range(2022, 2027)),
        default=None,
        help="Test only one Maya release year (2022-2026).",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    versions = discover()
    years = [args.year] if args.year is not None else list(range(2022, 2027))
    if args.year is not None and args.year not in versions:
        print(f"UNTESTED: Maya {args.year} mayapy not found")
        return 2
    if not versions:
        print("UNTESTED: no Maya 2022-2026 mayapy found")
        return 2
    failed = []
    for year in years:
        if year in versions:
            try:
                asyncio.run(smoke(year, versions[year]))
            except Exception:
                traceback.print_exc()
                failed.append(year)
        else:
            print(f"UNTESTED: Maya {year} mayapy not found")
    if failed:
        print(f"FAIL: Maya smoke versions {failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

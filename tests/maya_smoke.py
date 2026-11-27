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
            "import_asset", "execute_maya_code", "execute_script", "scene_checkpoint", "undo_scene",
            "create_curve", "create_text", "extrude_curve", "loft_mesh", "mesh_extrude", "mesh_bevel",
            "mesh_mirror", "array_objects", "mesh_boolean", "mesh_combine", "mesh_separate",
            "group_objects", "mesh_topology", "mesh_retopo", "mesh_reduce", "mesh_rebuild",
            "mesh_snap", "set_symmetry", "uv_project", "uv_unfold", "uv_layout", "uv_sets", "uv_inspect",
            "bake_map", "bake_arnold_texture", "compose_sheet",
            "list_maya_installations", "submit_job", "job_status"}


def _rendered_bbox(image_path: Path):
    """Return (x0, y0, x1, y1, w, h) of non-background pixels in a rendered PNG."""
    with Image.open(image_path) as img:
        rgb = img.convert("RGB")
        w, h = rgb.size
        bg = rgb.getpixel((0, 0))
        pixels = rgb.load()
        xs, ys = [], []
        for y in range(h):
            for x in range(w):
                p = pixels[x, y]
                if abs(p[0] - bg[0]) + abs(p[1] - bg[1]) + abs(p[2] - bg[2]) > 8:
                    xs.append(x)
                    ys.append(y)
        assert xs and ys, f"No foreground pixels detected in {image_path}"
        return min(xs), min(ys), max(xs), max(ys), w, h


async def smoke(year, executable):
    root = Path.cwd()
    (root / ".tmp").mkdir(exist_ok=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with (
        tempfile.TemporaryDirectory(prefix="smoke-", dir=root / ".tmp", ignore_cleanup_errors=True) as folder,
        tempfile.TemporaryDirectory(prefix="smoke-ext-中文-", dir=root / ".tmp", ignore_cleanup_errors=True) as ext_folder,
    ):
        env = {
            **os.environ,
            "MAYA_MCP_PORT": str(port),
            "MAYA_MCP_MODE": "headless",
            "MAYA_MCP_PROJECT_ROOT": folder,
            "MAYA_MCP_MAYAPY": str(executable),
            "MAYA_MCP_VERSION": str(year),
            "MAYA_MCP_ALLOW_CODE": "1",
            "MAYA_MCP_ALLOW_SCRIPT": "1",
            "MAYA_MCP_ALLOW_EXTERNAL_PATHS": "1",
        }
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
                assert info["capabilities"]["plugins"]["fbx"]["loaded"] is True, info
                assert info["allow_code"] is True and info["allow_script"] is True, info
                assert info["project_root"] == str(Path(folder).resolve()), info
                print(f"Maya {info['version']} API {info['api_version']}, Python {info['python']}, "
                      f"Transfer Maps {info['capabilities']['transfer_maps']}, "
                      f"FBX loaded={info['capabilities']['plugins']['fbx']['loaded']}")

                installations = await call("list_maya_installations")
                assert any(item["version"] == year and item["installed"] for item in installations["versions"])

                # Basic modeling, layers, brief mode
                await call("create_layer", name="smoke_geo")
                obj = await call("create_object", kind="cube", name="SmokeCube", layer="smoke_geo")
                assert obj["faces"] == 6 and obj["uv_sets"] == ["map1"], obj
                brief_obj = await call("get_object_info", name="SmokeCube", brief=True)
                assert set(brief_obj.keys()) == {"name", "bbox", "vertices", "faces"}, brief_obj
                obj = await call("modify_object", name="SmokeCube", new_name="MovedCube",
                                 translation=[1, 2, 0])
                assert obj["translation"] == [1, 2, 0], obj

                # Multi-object set_material + shader reuse (P2-9)
                await call("create_object", kind="cube", name="MatPeer", translation=[3, 0, 0], brief=True)
                assigned = await call("set_material", names=["MovedCube", "MatPeer"], material="SmokeMat",
                                      color=[0.25, 0.4, 0.6])
                assert assigned["material"] == "SmokeMat" and assigned["reused"] is False, assigned
                assert len(assigned["names"]) == 2, assigned
                reassigned = await call("set_material", name="MovedCube", material="SmokeMat")
                assert reassigned["material"] == "SmokeMat" and reassigned["reused"] is True, reassigned

                scene = await call("get_scene_info")
                assert scene["triangles"] == 24 and "smoke_geo" in scene["layers"], scene
                brief_scene = await call("get_scene_info", brief=True)
                assert "plugins" not in brief_scene and brief_scene["mesh_count"] == 2, brief_scene

                # P0-1: execute_maya_code (strict AST allowlist) AND execute_script (full Python)
                executed = await call("execute_maya_code", code="cmds.objExists('MovedCube')")
                assert executed["results"] == [True], executed
                result = await client.call_tool("execute_maya_code", {"code": "cmds.quit(force=True)"})
                assert result.structuredContent["error"]["kind"] == "validation", result

                script_out = await call(
                    "execute_script",
                    code=(
                        "import math\n"
                        "created = []\n"
                        "for i in range(3):\n"
                        "    n = cmds.polyCube(name=f'ScriptBox_{i}', w=0.4, h=0.4, d=0.4, ch=False)[0]\n"
                        "    cmds.move(i * 1.0, 0, -3, n)\n"
                        "    created.append(n)\n"
                        "print('built', len(created))\n"
                        "result = {'count': len(created), 'cos0': math.cos(0)}\n"
                    ),
                )
                assert script_out["result"] == {"count": 3, "cos0": 1.0}, script_out
                assert "built 3" in script_out["stdout"], script_out
                timeout_res = await client.call_tool(
                    "execute_script", {"code": "while True:\n    pass\n", "timeout": 0.25}
                )
                assert timeout_res.structuredContent["error"]["kind"] == "timeout", timeout_res
                await call("delete_object", names=["ScriptBox_0", "ScriptBox_1", "ScriptBox_2", "MatPeer"])

                # P0-3 + P1-5 + P2-12: Save, export, import (including Chinese path and external opt-in)
                await call("save_scene", path="smoke.mb")
                await call("save_scene", path="中文输出/场景快照.ma")
                for ext in ("obj", "fbx"):
                    exp = await call("export_scene", path=f"smoke.{ext}", names=["MovedCube"])
                    assert exp["format"] == ext, exp
                exp_zh = await call("export_scene", path="中文输出/模型导出.fbx", names=["MovedCube"])
                assert Path(exp_zh["path"]).stat().st_size > 0, exp_zh

                # External path denied without allow_outside_root=True, allowed with True
                ext_obj_path = str(Path(ext_folder) / "外部导出.obj")
                denied_ext = await client.call_tool(
                    "export_scene", {"path": ext_obj_path, "names": ["MovedCube"]}
                )
                assert not denied_ext.structuredContent["ok"], denied_ext
                ext_exported = await call(
                    "export_scene", path=ext_obj_path, names=["MovedCube"], allow_outside_root=True
                )
                assert Path(ext_exported["path"]).stat().st_size > 0, ext_exported
                imported = await call(
                    "import_asset", path=ext_obj_path, group_name="ImportedGrp",
                    allow_outside_root=True, brief=True,
                )
                assert imported["count"] >= 1 and imported["group"], imported
                await call("delete_object", names=["ImportedGrp"])

                # P3-13: capture_viewport explicit headless fallback & strict refusal
                strict_vp = await client.call_tool(
                    "capture_viewport", {"path": "strict_vp.png", "width": 64, "height": 64, "allow_fallback": False}
                )
                assert strict_vp.structuredContent["error"]["kind"] == "validation", strict_vp
                view = await call("capture_viewport", path="view.png", width=128, height=128)
                assert view["fallback"] == "render" and view["is_gui_viewport"] is False and view["mode"] == "headless", view

                # P1-4: Non-square aspect ratio render framing (portrait & landscape)
                await call("delete_object", names=["MovedCube"])
                await call("create_object", kind="cube", name="TallRod", size=[2.0, 10.0, 2.0], brief=True)
                portrait = await call("render_view", path="中文输出/竖图.png", camera="front", width=200, height=400)
                assert portrait["width"] == 200 and portrait["height"] == 400, portrait
                x0, y0, x1, y1, pw, ph = _rendered_bbox(Path(portrait["path"]))
                assert x0 > 5 and y0 > 5 and x1 < pw - 5 and y1 < ph - 5, (x0, y0, x1, y1, pw, ph)
                rendered_ratio = (x1 - x0 + 1) / (y1 - y0 + 1)
                assert 0.17 <= rendered_ratio <= 0.23, f"Expected ~0.2 aspect ratio, got {rendered_ratio}"

                landscape = await call("render_view", path="landscape.png", camera="front", width=400, height=200)
                lx0, ly0, lx1, ly1, lw, lh = _rendered_bbox(Path(landscape["path"]))
                assert lx0 > 5 and ly0 > 5 and lx1 < lw - 5 and ly1 < lh - 5, (lx0, ly0, lx1, ly1, lw, lh)
                land_ratio = (lx1 - lx0 + 1) / (ly1 - ly0 + 1)
                assert 0.17 <= land_ratio <= 0.23, f"Expected ~0.2 aspect ratio in landscape, got {land_ratio}"

                # P1-6: Arnold render explicit fallback (graceful without faking success)
                arnold_fb = await call(
                    "render_view", path="arnold_fb.png", camera="front", width=96, height=96,
                    renderer="arnold", fallback_to_software=True,
                )
                assert Path(arnold_fb["path"]).stat().st_size > 0, arnold_fb
                await call("delete_object", names=["TallRod"])

                # P1-7: New primitives (torus, rounded_box, capsule, cone, pipe)
                torus = await call("create_object", kind="torus", name="TestTorus", size=[3.0, 0.5, 3.0], brief=True)
                assert torus["faces"] > 0 and abs((torus["bbox"][3] - torus["bbox"][0]) - 3.0) < 0.05, torus
                rbox = await call("create_object", kind="rounded_box", name="TestRBox", size=[4.0, 2.0, 1.0], radius=0.2, brief=True)
                assert rbox["faces"] > 6 and abs((rbox["bbox"][3] - rbox["bbox"][0]) - 4.0) < 0.01, rbox
                capsule = await call("create_object", kind="capsule", name="TestCapsule", size=[1.0, 3.0, 1.0], brief=True)
                assert capsule["faces"] > 0 and abs((capsule["bbox"][4] - capsule["bbox"][1]) - 3.0) < 0.02, capsule
                pipe = await call("create_object", kind="pipe", name="TestPipe", size=[3.0, 0.6, 3.0], radius=0.4, brief=True)
                assert pipe["faces"] > 0 and abs((pipe["bbox"][4] - pipe["bbox"][1]) - 0.6) < 0.02, pipe
                cone = await call("create_object", kind="cone", name="TestCone", size=[2.0, 2.5, 2.0], brief=True)
                assert cone["faces"] > 0, cone

                # P0-2: 3D Text + Curve Extrusion
                logo = await call("create_text", text="OPEN", name="LogoText", size=1.0,
                                  extrude_depth=0.15, bevel_width=0.015, bevel_depth=0.015, brief=True)
                assert logo["faces"] > 100 and abs((logo["bbox"][4] - logo["bbox"][1]) - 1.0) < 0.1, logo
                await call("create_curve", name="IconOuter", closed=True, degree=1,
                           points=[[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0]], brief=True)
                icon_mesh = await call("extrude_curve", curves=["IconOuter"], name="IconExtruded",
                                       depth=0.2, keep_curves=False, brief=True)
                assert icon_mesh["faces"] > 0 and icon_mesh["name"].endswith("|IconExtruded"), icon_mesh

                # P2-10: Scene checkpoint & rollback + undo_scene
                cp = await call("scene_checkpoint", action="create", name="snap_primitives")
                assert cp["name"] == "snap_primitives", cp
                await call("create_object", kind="cube", name="TempNoise", brief=True)
                assert (await call("execute_maya_code", code="cmds.objExists('TempNoise')"))["results"] == [True]
                await call("scene_checkpoint", action="restore", name="snap_primitives")
                assert (await call("execute_maya_code", code="cmds.objExists('TempNoise')"))["results"] == [False]
                await call("scene_checkpoint", action="delete", name="snap_primitives")
                undo_st = await call("undo_scene", action="status")
                assert undo_st["enabled"] is True, undo_st

                # Clean up before jobs/modeling/topology/UV/bake tests
                await call("delete_object", names=[
                    "TestTorus", "TestRBox", "TestCapsule", "TestPipe", "TestCone", "LogoText", "IconExtruded"
                ])
                await call("create_object", kind="cube", name="MovedCube")
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

                # Loft, extrude, array, group, boolean (including delete_inputs=True), bevel, mirror, combine, separate
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
                # P2-8: delete_inputs=True removes both inputs on success
                await call("create_object", kind="cube", name="BoolA", brief=True)
                await call("create_object", kind="cube", name="BoolB", translation=[0.25, 0.25, 0.25], brief=True)
                bool_del = await call("mesh_boolean", first="BoolA", second="BoolB", name="BoolClean",
                                      delete_inputs=True, brief=True)
                assert len(bool_del["deleted_inputs"]) == 2, bool_del
                assert (await call("execute_maya_code", code="cmds.objExists('BoolA')"))["results"] == [False]
                assert (await call("execute_maya_code", code="cmds.objExists('BoolB')"))["results"] == [False]

                await call("mesh_bevel", name="Base", edges=[0, 1, 2, 3], segments=1)
                await call("mesh_mirror", source="Base", name="Mirrored")
                await call("create_object", kind="cube", name="Apart", translation=[5, 0, 0])
                await call("mesh_combine", names=["Base", "Apart"], name="Combined")
                separated = await call("mesh_separate", source="Combined", name="Separated")
                assert len(separated["objects"]) == 2, separated

                # Topology, UV, and native baking
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
    print(f"PASS: MCP stdio -> Maya {year}: all 48 tools, scripts, text, primitives, aspect render, Chinese/external paths, checkpoints, topology, UV, bake, export, import, jobs")


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

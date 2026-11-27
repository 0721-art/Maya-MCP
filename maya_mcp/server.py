import argparse
import dataclasses
from contextlib import asynccontextmanager
from inspect import signature
from pathlib import Path
from typing import Any, Literal, get_type_hints

from mcp.server.fastmcp import FastMCP
from PIL import Image

from .baking import TOOLS as BAKING_TOOLS
from .client import BridgeError
from .composition import compose_sheet as compose
from .config import Settings
from .envelope import failure, success
from .jobs import MayaJobs
from .modeling import TOOLS as MODELING_TOOLS
from .session import MayaSession
from .topology import TOOLS as TOPOLOGY_TOOLS
from .uv import TOOLS as UV_TOOLS
from .versions import YEARS, choose_mayapy, discover

Vector = tuple[float, float, float]
PrimitiveKind = Literal[
    "cube", "sphere", "cylinder", "plane",
    "torus", "rounded_box", "capsule", "cone", "pipe",
]


def register_tools(mcp, call, methods, long=False):
    registered = {}
    for method in methods:
        def bind(method):
            def invoke(**params):
                return call(method.__name__, long=long, **params)

            sig = signature(method)
            hints = get_type_hints(method)
            args = [param.replace(annotation=hints[param.name])
                    for param in list(sig.parameters.values())[1:]]
            invoke.__signature__ = sig.replace(parameters=args, return_annotation=dict[str, Any])
            invoke.__name__ = method.__name__
            invoke.__doc__ = method.__doc__
            return invoke

        tool = bind(method)
        mcp.add_tool(tool)
        registered[method.__name__] = tool
    return registered


def make_server(settings):
    session = MayaSession(settings)

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield
        finally:
            session.close(permanent=True)

    mcp = FastMCP("Maya MCP", lifespan=lifespan)

    def call(command, *, long=False, call_timeout=None, **params):
        try:
            return session.call(command, long=long, call_timeout=call_timeout, **params)
        except BridgeError as exc:
            return failure(exc.kind, str(exc))
        except (OSError, ValueError) as exc:
            return failure("configuration", str(exc))

    @mcp.tool()
    def get_maya_info() -> dict[str, Any]:
        """Identify the running Maya session, its mode, version, plugins, and root/policy config."""
        return call("get_maya_info")

    @mcp.tool()
    def list_maya_installations() -> dict[str, Any]:
        """Find 2022–2026 mayapy executables on this machine without starting Maya."""
        found = discover()
        return success({"versions": [
            {"version": year, "installed": year in found,
             "mayapy": str(found[year]) if year in found else None,
             "smoke_status": "not_run" if year in found else "untested"}
            for year in YEARS
        ]})

    @mcp.tool()
    def get_scene_info(brief: bool = False) -> dict[str, Any]:
        """Inspect the active scene. Pass brief=True for a compact summary without plugins/capabilities."""
        return call("get_scene_info", brief=brief)

    @mcp.tool()
    def get_object_info(name: str, brief: bool = False) -> dict[str, Any]:
        """Inspect one transform by exact name or full DAG path. Pass brief=True for name/bbox/counts only."""
        return call("get_object_info", name=name, brief=brief)

    @mcp.tool()
    def create_object(kind: PrimitiveKind, name: str,
                      size: Vector = (1, 1, 1), segments: int = 16,
                      translation: Vector = (0, 0, 0), rotation: Vector = (0, 0, 0),
                      parent: str | None = None, layer: str | None = None,
                      radius: float | None = None, bevel_segments: int = 3,
                      brief: bool = False) -> dict[str, Any]:
        """Create a Maya polygon primitive (cube, sphere, cylinder, plane, torus, rounded_box, capsule, cone, pipe). Size is width, height, depth; radius controls tube/bevel/wall thickness where applicable."""
        return call("create_object", kind=kind, name=name, size=size, segments=segments,
                    translation=translation, rotation=rotation, parent=parent, layer=layer,
                    radius=radius, bevel_segments=bevel_segments, brief=brief)

    @mcp.tool()
    def modify_object(name: str, new_name: str | None = None,
                      translation: Vector | None = None, rotation: Vector | None = None,
                      scale: Vector | None = None, visible: bool | None = None,
                      parent: str | None = None, layer: str | None = None,
                      brief: bool = False) -> dict[str, Any]:
        """Change an existing transform by exact name. Translation and rotation are world-space."""
        return call("modify_object", name=name, new_name=new_name, translation=translation,
                    rotation=rotation, scale=scale, visible=visible, parent=parent, layer=layer,
                    brief=brief)

    @mcp.tool()
    def delete_object(names: list[str]) -> dict[str, Any]:
        """Delete exactly the listed transforms and their descendants from the active scene."""
        return call("delete_object", names=names)

    @mcp.tool()
    def set_material(material: str, name: str | None = None, names: list[str] | None = None,
                     color: Vector | None = None, roughness: float | None = None,
                     reuse: bool = True,
                     shader_type: Literal["blinn", "lambert", "phong", "standardSurface"] = "blinn") -> dict[str, Any]:
        """Assign a Maya surface shader to one or more objects, reusing existing shaders by name when reuse=True."""
        return call("set_material", name=name, names=names, material=material, color=color,
                    roughness=roughness, reuse=reuse, shader_type=shader_type)

    @mcp.tool()
    def create_layer(name: str, names: list[str] | None = None,
                     visible: bool = True, color: int = 0) -> dict[str, Any]:
        """Create a display layer and optionally add transforms."""
        return call("create_layer", name=name, names=names or [], visible=visible, color=color)

    @mcp.tool()
    def assign_layer(name: str, names: list[str]) -> dict[str, Any]:
        """Move transforms to an existing display layer."""
        return call("assign_layer", name=name, names=names)

    @mcp.tool()
    def render_view(path: str, camera: str = "front", width: int = 800, height: int = 800,
                    overwrite: bool = False, names: list[str] | None = None,
                    frame_names: list[str] | None = None,
                    renderer: Literal["mayaSoftware", "arnold"] = "mayaSoftware",
                    fallback_to_software: bool = False,
                    allow_outside_root: bool = False) -> dict[str, Any]:
        """Render a camera/preset to PNG with exact aspect framing; optional Arnold renderer with explicit Maya Software fallback."""
        return call("render_view", long=True, path=path, camera=camera, width=width, height=height,
                    overwrite=overwrite, names=names or [], frame_names=frame_names or [],
                    renderer=renderer, fallback_to_software=fallback_to_software,
                    allow_outside_root=allow_outside_root)

    @mcp.tool()
    def capture_viewport(path: str, camera: str = "front", width: int = 800, height: int = 800,
                         overwrite: bool = False, names: list[str] | None = None,
                         frame_names: list[str] | None = None,
                         allow_fallback: bool = True,
                         allow_outside_root: bool = False) -> dict[str, Any]:
        """Capture a GUI model panel; in headless Maya either fail (allow_fallback=False) or return an explicit Maya Software fallback with metadata."""
        return call("capture_viewport", long=True, path=path, camera=camera, width=width,
                    height=height, overwrite=overwrite, names=names or [], frame_names=frame_names or [],
                    allow_fallback=allow_fallback, allow_outside_root=allow_outside_root)

    @mcp.tool()
    def compose_sheet(rows: list[list[str]], path: str, overwrite: bool = False,
                      allow_outside_root: bool = False) -> dict[str, Any]:
        """Arrange existing PNG/JPEG captures as a rectangular grid and write one composite PNG."""
        try:
            return success(compose(
                settings.root, rows, path, overwrite,
                allowed_roots=settings.allowed_roots,
                allow_external=settings.allow_external_paths,
                allow_outside_root=allow_outside_root,
            ))
        except (ValueError, OSError, Image.DecompressionBombError) as exc:
            return failure("validation", str(exc))

    @mcp.tool()
    def uv_snapshot(path: str, names: list[str], size: int = 1024, overwrite: bool = False,
                    uv_set: str | None = None,
                    bounds: tuple[float, float, float, float] | None = None,
                    allow_outside_root: bool = False) -> dict[str, Any]:
        """Save a UV Snapshot PNG. Bounds are [min_u, min_v, max_u, max_v]."""
        return call("uv_snapshot", long=True, path=path, names=names, size=size, overwrite=overwrite,
                    uv_set=uv_set, bounds=bounds, allow_outside_root=allow_outside_root)

    @mcp.tool()
    def save_scene(path: str, overwrite: bool = False,
                   allow_outside_root: bool = False) -> dict[str, Any]:
        """Save the active Maya scene as .ma or .mb (supports Unicode paths and explicit external opt-in)."""
        return call("save_scene", long=True, path=path, overwrite=overwrite,
                    allow_outside_root=allow_outside_root)

    @mcp.tool()
    def export_scene(path: str, names: list[str] | None = None, overwrite: bool = False,
                     allow_outside_root: bool = False) -> dict[str, Any]:
        """Export the listed transforms, or the entire active scene if omitted, to .fbx or .obj (auto-loads plugin)."""
        return call("export_scene", long=True, path=path, names=names or [], overwrite=overwrite,
                    allow_outside_root=allow_outside_root)

    @mcp.tool()
    def import_asset(path: str, namespace: str | None = None, group_name: str | None = None,
                     allow_outside_root: bool = False, brief: bool = False) -> dict[str, Any]:
        """Import an external .obj, .fbx, .ma, or .mb asset into the active scene (auto-loads FBX/OBJ plugins)."""
        return call("import_asset", long=True, path=path, namespace=namespace,
                    group_name=group_name, allow_outside_root=allow_outside_root, brief=brief)

    @mcp.tool()
    def execute_maya_code(code: str) -> dict[str, Any]:
        """Execute allowlisted literal cmds calls (no eval/loops/imports). Gated by MAYA_MCP_ALLOW_CODE=1."""
        return call("execute_maya_code", code=code)

    @mcp.tool()
    def execute_script(code: str, timeout: float | None = None) -> dict[str, Any]:
        """Execute full Maya Python (imports, loops, variables, cmds/mel/om) with size & timeout limits. Gated by MAYA_MCP_ALLOW_SCRIPT=1."""
        bridge_timeout = (float(timeout) + 5.0) if timeout is not None else None
        return call("execute_script", call_timeout=bridge_timeout, code=code, timeout_seconds=timeout)

    @mcp.tool()
    def scene_checkpoint(action: Literal["create", "restore", "list", "delete"] = "create",
                         name: str | None = None) -> dict[str, Any]:
        """Create, restore, list, or delete safe scene snapshot checkpoints for rollback."""
        return call("scene_checkpoint", long=True, action=action, name=name)

    @mcp.tool()
    def undo_scene(action: Literal["undo", "redo", "status"] = "undo",
                   steps: int = 1) -> dict[str, Any]:
        """Undo, redo, or inspect Maya's native undo queue (1–50 steps)."""
        return call("undo_scene", action=action, steps=steps)

    register_tools(mcp, call, MODELING_TOOLS)
    topology_tools = register_tools(mcp, call, TOPOLOGY_TOOLS, long=True)
    register_tools(mcp, call, UV_TOOLS, long=True)
    bake_tools = register_tools(mcp, call, BAKING_TOOLS, long=True)
    jobs = MayaJobs({**bake_tools, "mesh_retopo": topology_tools["mesh_retopo"],
                    "mesh_rebuild": topology_tools["mesh_rebuild"], "mesh_reduce": topology_tools["mesh_reduce"],
                    "render_view": render_view, "capture_viewport": capture_viewport,
                    "uv_snapshot": uv_snapshot, "save_scene": save_scene, "export_scene": export_scene,
                    "import_asset": import_asset})

    @mcp.tool()
    def submit_job(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Start one pollable bake, render, topology, snapshot, save, import or export operation."""
        return jobs.submit(tool, arguments)

    @mcp.tool()
    def job_status(job_id: str) -> dict[str, Any]:
        """Poll a queued or running Maya job and retrieve its response once finished."""
        return jobs.status(job_id)

    return mcp


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run the Autodesk Maya MCP stdio server.")
    parser.add_argument("--project-root", type=str, default=None,
                        help="Project root directory for file sandboxing (overrides MAYA_MCP_PROJECT_ROOT).")
    parser.add_argument("--allowed-root", action="append", default=[],
                        help="Additional allowed root directory (repeatable).")
    parser.add_argument("--allow-external-paths", action="store_true", default=False,
                        help="Allow tools to access explicit absolute paths outside root when allow_outside_root=True.")
    parser.add_argument("--allow-code", action="store_true", default=False,
                        help="Enable execute_maya_code (allowlisted literal cmds calls).")
    parser.add_argument("--allow-script", action="store_true", default=False,
                        help="Enable execute_script (full Maya Python with size & timeout limits).")
    parser.add_argument("--mode", choices=("auto", "gui", "headless", "commandport"), default=None,
                        help="Override MAYA_MCP_MODE.")
    parser.add_argument("--port", type=int, default=None, help="Override bridge/commandPort TCP port.")
    parser.add_argument("--version", type=int, choices=tuple(YEARS), default=None,
                        help="Target Maya release year (2022-2026).")
    return parser.parse_args(argv)


def settings_from_args(args, base=None):
    settings = Settings.from_env() if base is None else base
    updates = {}
    if args.project_root:
        updates["root"] = Path(args.project_root).resolve()
    if args.allowed_root:
        extra = tuple(Path(p).resolve() for p in args.allowed_root if p.strip())
        updates["allowed_roots"] = tuple(dict.fromkeys((*settings.allowed_roots, *extra)))
    if args.allow_external_paths:
        updates["allow_external_paths"] = True
    if args.allow_code:
        updates["allow_code"] = True
    if args.allow_script:
        updates["allow_script"] = True
    if args.mode:
        updates["mode"] = args.mode
    if args.port is not None:
        updates["port"] = args.port
    if args.version is not None:
        updates["version"] = args.version
        updates["mayapy"] = choose_mayapy(args.version)
    return dataclasses.replace(settings, **updates) if updates else settings


def main(argv=None):
    args = parse_args(argv)
    make_server(settings_from_args(args)).run(transport="stdio")


if __name__ == "__main__":
    main()

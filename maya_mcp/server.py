from contextlib import asynccontextmanager
from inspect import signature
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
from .versions import YEARS, discover

Vector = tuple[float, float, float]


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

    def call(command, *, long=False, **params):
        try:
            return session.call(command, long=long, **params)
        except BridgeError as exc:
            return failure(exc.kind, str(exc))
        except (OSError, ValueError) as exc:
            return failure("configuration", str(exc))

    @mcp.tool()
    def get_maya_info() -> dict[str, Any]:
        """Identify the running Maya session and its mode, version and plugins."""
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
    def get_scene_info() -> dict[str, Any]:
        """Inspect the active scene, including Outliner paths, display layers and mesh counts."""
        return call("get_scene_info")

    @mcp.tool()
    def get_object_info(name: str) -> dict[str, Any]:
        """Inspect one transform by exact name or full DAG path."""
        return call("get_object_info", name=name)

    @mcp.tool()
    def create_object(kind: Literal["cube", "sphere", "cylinder", "plane"], name: str,
                      size: Vector = (1, 1, 1), segments: int = 16,
                      translation: Vector = (0, 0, 0), rotation: Vector = (0, 0, 0),
                      parent: str | None = None, layer: str | None = None) -> dict[str, Any]:
        """Create a Maya polygon primitive. Size is width, height, depth."""
        return call("create_object", kind=kind, name=name, size=size, segments=segments,
                    translation=translation, rotation=rotation, parent=parent, layer=layer)

    @mcp.tool()
    def modify_object(name: str, new_name: str | None = None,
                      translation: Vector | None = None, rotation: Vector | None = None,
                      scale: Vector | None = None, visible: bool | None = None,
                      parent: str | None = None, layer: str | None = None) -> dict[str, Any]:
        """Change an existing transform by exact name. Translation and rotation are world-space."""
        return call("modify_object", name=name, new_name=new_name, translation=translation,
                    rotation=rotation, scale=scale, visible=visible, parent=parent, layer=layer)

    @mcp.tool()
    def delete_object(names: list[str]) -> dict[str, Any]:
        """Delete exactly the listed transforms and their descendants from the active scene."""
        return call("delete_object", names=names)

    @mcp.tool()
    def set_material(name: str, material: str, color: Vector, roughness: float = 0.5) -> dict[str, Any]:
        """Assign a Maya Blinn shader. RGB and roughness are in the range 0 to 1."""
        return call("set_material", name=name, material=material, color=color, roughness=roughness)

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
                    frame_names: list[str] | None = None) -> dict[str, Any]:
        """Render a camera/preset to PNG, optionally isolating meshes and sharing framing across captures."""
        return call("render_view", long=True, path=path, camera=camera, width=width, height=height,
                    overwrite=overwrite, names=names or [], frame_names=frame_names or [])

    @mcp.tool()
    def capture_viewport(path: str, camera: str = "front", width: int = 800, height: int = 800,
                         overwrite: bool = False, names: list[str] | None = None,
                         frame_names: list[str] | None = None) -> dict[str, Any]:
        """Capture a GUI model panel; in standalone, return a Maya Software render marked fallback."""
        return call("capture_viewport", long=True, path=path, camera=camera, width=width,
                    height=height, overwrite=overwrite, names=names or [], frame_names=frame_names or [])

    @mcp.tool()
    def compose_sheet(rows: list[list[str]], path: str, overwrite: bool = False) -> dict[str, Any]:
        """Arrange existing PNG/JPEG captures as a rectangular grid and write one composite PNG."""
        try:
            return success(compose(settings.root, rows, path, overwrite))
        except (ValueError, OSError, Image.DecompressionBombError) as exc:
            return failure("validation", str(exc))

    @mcp.tool()
    def uv_snapshot(path: str, names: list[str], size: int = 1024, overwrite: bool = False,
                    uv_set: str | None = None,
                    bounds: tuple[float, float, float, float] | None = None) -> dict[str, Any]:
        """Save a UV Snapshot PNG. Bounds are [min_u, min_v, max_u, max_v]."""
        return call("uv_snapshot", long=True, path=path, names=names, size=size, overwrite=overwrite,
                    uv_set=uv_set, bounds=bounds)

    @mcp.tool()
    def save_scene(path: str, overwrite: bool = False) -> dict[str, Any]:
        """Save the active Maya scene as .ma or .mb inside the configured root."""
        return call("save_scene", long=True, path=path, overwrite=overwrite)

    @mcp.tool()
    def export_scene(path: str, names: list[str] | None = None, overwrite: bool = False) -> dict[str, Any]:
        """Export the listed transforms, or the entire active scene if omitted, to .fbx or .obj."""
        return call("export_scene", long=True, path=path, names=names or [], overwrite=overwrite)

    @mcp.tool()
    def execute_maya_code(code: str) -> dict[str, Any]:
        """Execute literal cmds calls for ls, listRelatives, objExists, polygon primitives, polyEvaluate and xform."""
        return call("execute_maya_code", code=code)

    register_tools(mcp, call, MODELING_TOOLS)
    topology_tools = register_tools(mcp, call, TOPOLOGY_TOOLS, long=True)
    register_tools(mcp, call, UV_TOOLS, long=True)
    bake_tools = register_tools(mcp, call, BAKING_TOOLS, long=True)
    jobs = MayaJobs({**bake_tools, "mesh_retopo": topology_tools["mesh_retopo"],
                    "mesh_rebuild": topology_tools["mesh_rebuild"], "mesh_reduce": topology_tools["mesh_reduce"],
                    "render_view": render_view, "capture_viewport": capture_viewport,
                    "uv_snapshot": uv_snapshot, "save_scene": save_scene, "export_scene": export_scene})

    @mcp.tool()
    def submit_job(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Start one pollable bake, render, topology, snapshot, save or export operation."""
        return jobs.submit(tool, arguments)

    @mcp.tool()
    def job_status(job_id: str) -> dict[str, Any]:
        """Poll a queued or running Maya job and retrieve its response once finished."""
        return jobs.status(job_id)

    return mcp


def main():
    make_server(Settings.from_env()).run(transport="stdio")


if __name__ == "__main__":
    main()

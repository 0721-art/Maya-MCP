from contextlib import asynccontextmanager
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .client import BridgeError
from .config import Settings
from .session import MayaSession

Vector = tuple[float, float, float]


def make_server(settings):
    session = MayaSession(settings)

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield
        finally:
            session.close()

    mcp = FastMCP("Maya 2026", lifespan=lifespan)

    def call(command, *, long=False, **params):
        try:
            return session.call(command, long=long, **params)
        except BridgeError as exc:
            raise ToolError(f"{exc.kind}: {exc}") from exc

    @mcp.tool()
    def get_maya_info() -> dict[str, Any]:
        """Identify the running Maya session and its mode, version and plugins."""
        return call("get_maya_info")

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
                    overwrite: bool = False) -> dict[str, Any]:
        """Render to PNG using Maya Software. Use front, back, side, left, top, persp or a camera name."""
        return call("render_view", long=True, path=path, camera=camera, width=width, height=height,
                    overwrite=overwrite)

    @mcp.tool()
    def capture_viewport(path: str, camera: str = "front", width: int = 800, height: int = 800,
                         overwrite: bool = False) -> dict[str, Any]:
        """Capture a GUI model panel; in standalone, return a Maya Software render marked fallback."""
        return call("capture_viewport", long=True, path=path, camera=camera, width=width,
                    height=height, overwrite=overwrite)

    @mcp.tool()
    def uv_snapshot(path: str, names: list[str], size: int = 1024, overwrite: bool = False) -> dict[str, Any]:
        """Save a PNG from Maya UV Snapshot for the specified polygon meshes."""
        return call("uv_snapshot", long=True, path=path, names=names, size=size, overwrite=overwrite)

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

    return mcp


def main():
    make_server(Settings.from_env()).run(transport="stdio")


if __name__ == "__main__":
    main()

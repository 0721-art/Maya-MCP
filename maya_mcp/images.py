from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

PRESETS = {
    "front": (0, 0, 0), "back": (0, 180, 0), "side": (0, 90, 0),
    "left": (0, -90, 0), "top": (-90, 0, 0), "persp": (-14, 24, 0),
}


def dimensions(width, height):
    if not all(type(v) is int and 16 <= v <= 4096 for v in (width, height)):
        raise ValueError("Image dimensions must be between 16 and 4096 pixels")


@contextmanager
def view_camera(cmds, camera, width, height):
    if camera not in PRESETS:
        matches = cmds.ls(camera, exactType="camera", long=True) or []
        if not matches:
            matches = [shape for shape in
                       cmds.listRelatives(camera, shapes=True, fullPath=True) or []
                       if cmds.nodeType(shape) == "camera"]
        if len(matches) != 1:
            raise ValueError(f"Expected one camera or preset, got {camera!r}")
        yield matches[0]
        return

    from maya.api import OpenMaya as om

    meshes = cmds.ls(type="mesh", long=True, noIntermediate=True, visible=True) or []
    if not meshes:
        raise ValueError("No visible meshes to frame")
    box = cmds.exactWorldBoundingBox(meshes, ignoreInvisible=True)
    center = om.MVector(*[(lo + hi) / 2 for lo, hi in zip(box[:3], box[3:])])
    rotation = PRESETS[camera]
    node, shape = cmds.camera(name="MCP_view", orthographic=True)
    try:
        cmds.xform(node, worldSpace=True, rotation=rotation)
        matrix = cmds.xform(node, query=True, matrix=True, worldSpace=True)
        right, up, forward = (om.MVector(*matrix[i:i + 3]) for i in (0, 4, 8))
        from itertools import product

        offsets = [om.MVector(*point) - center for point in product(*zip(box[:3], box[3:]))]
        span_x = 2 * max(abs(v * right) for v in offsets)
        span_y = 2 * max(abs(v * up) for v in offsets)
        position = center + forward * (30 + 3 * max(v.length() for v in offsets))
        cmds.xform(node, worldSpace=True, translation=tuple(position))
        cmds.setAttr(shape + ".orthographicWidth", 1.16 * max(span_x, span_y * width / height, 0.01))
        cmds.setAttr(shape + ".filmFit", 1)
        yield shape
    finally:
        cmds.delete(node)


def render_view(cmds, path, camera, width, height):
    from .handlers import written

    dimensions(width, height)
    globals_node = "defaultRenderGlobals."
    attributes = {"imageFormat": 32, "animation": False, "currentRenderer": "mayaSoftware"}
    previous = {key: cmds.getAttr(globals_node + key) for key in (*attributes, "imageFilePrefix")}
    with view_camera(cmds, camera, width, height) as shape, TemporaryDirectory() as temp:
        attributes["imageFilePrefix"] = (Path(temp) / "view").as_posix()
        try:
            for key, value in attributes.items():
                if isinstance(value, str):
                    cmds.setAttr(globals_node + key, value, type="string")
                else:
                    cmds.setAttr(globals_node + key, value)
            source = Path(cmds.render(shape, x=width, y=height))
            path.write_bytes(source.read_bytes())
        finally:
            for key, value in previous.items():
                if isinstance(value, str) or value is None:
                    cmds.setAttr(globals_node + key, value or "", type="string")
                else:
                    cmds.setAttr(globals_node + key, value)
    return {**written(path), "source": "mayaSoftware", "camera": camera}


def capture_viewport(cmds, path, camera, width, height):
    from .handlers import written

    dimensions(width, height)
    panels = cmds.getPanel(type="modelPanel") or []
    if not panels:
        raise RuntimeError("No Maya model panel available")
    focused = cmds.getPanel(withFocus=True)
    panel = focused if focused in panels else panels[0]
    previous = cmds.modelPanel(panel, query=True, camera=True)
    image_format = cmds.getAttr("defaultRenderGlobals.imageFormat")
    with view_camera(cmds, camera, width, height) as shape, TemporaryDirectory() as temp:
        target = Path(temp) / "viewport.png"
        try:
            cmds.modelPanel(panel, edit=True, camera=shape)
            cmds.setAttr("defaultRenderGlobals.imageFormat", 32)
            cmds.playblast(editorPanelName=panel, format="image", compression="png", viewer=False,
                           completeFilename=target.as_posix(), showOrnaments=False,
                           frame=cmds.currentTime(query=True), percent=100,
                           widthHeight=(width, height), offScreen=True, forceOverwrite=True)
            path.write_bytes(target.read_bytes())
        finally:
            cmds.modelPanel(panel, edit=True, camera=previous)
            cmds.setAttr("defaultRenderGlobals.imageFormat", image_format)
    return {**written(path), "source": "mayaViewport", "camera": camera}


def uv_snapshot(cmds, path, nodes, size):
    from .handlers import written

    dimensions(size, size)
    selection = cmds.ls(selection=True, long=True) or []
    with TemporaryDirectory() as temp:
        target = Path(temp) / "uv.png"
        try:
            cmds.select(nodes, replace=True)
            cmds.uvSnapshot(name=target.as_posix(), fileFormat="png", overwrite=True,
                            xResolution=size, yResolution=size,
                            redColor=244, greenColor=246, blueColor=250, antiAlias=True)
            path.write_bytes(target.read_bytes())
        finally:
            cmds.select(selection, replace=True) if selection else cmds.select(clear=True)
    return {**written(path), "source": "mayaUvSnapshot"}

from contextlib import contextmanager
import os
import locale
import math
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from .policy import publish_file

PRESETS = {
    "front": (0, 0, 0), "back": (0, 180, 0), "side": (0, 90, 0),
    "left": (0, -90, 0), "top": (-90, 0, 0), "persp": (-14, 24, 0),
}


def dimensions(width, height):
    if not all(type(v) is int and 16 <= v <= 4096 for v in (width, height)):
        raise ValueError("Image dimensions must be between 16 and 4096 pixels")


@contextmanager
def view_camera(cmds, camera, width, height, frame_shapes=()):
    if camera not in PRESETS:
        if not isinstance(camera, str) or any(char in camera for char in "*?[]"):
            raise ValueError("Use an exact camera name or preset")
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

    meshes = frame_shapes or cmds.ls(type="mesh", long=True, noIntermediate=True, visible=True) or []
    if not meshes:
        raise ValueError("No visible meshes to frame")
    box = cmds.exactWorldBoundingBox(meshes, ignoreInvisible=not frame_shapes)
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


@contextmanager
def isolate_meshes(cmds, shapes):
    if not shapes:
        yield
        return
    visible = set(cmds.ls(type="mesh", long=True, noIntermediate=True, visible=True) or [])
    if not set(shapes) <= visible:
        raise ValueError("Make all requested mesh shapes visible before capturing")
    changed = []
    try:
        for node in sorted(visible - set(shapes)):
            attribute = node + ".visibility"
            if not cmds.getAttr(attribute, settable=True):
                raise ValueError(f"Cannot isolate the meshes while {attribute} is locked or connected")
            cmds.setAttr(attribute, False)
            changed.append(attribute)
        yield
    finally:
        for attribute in changed:
            cmds.setAttr(attribute, True)


def render_view(cmds, path, camera, width, height, timeout=600, executable=None, overwrite=False,
                frame_shapes=()):
    from .handlers import written

    dimensions(width, height)
    render = Path(executable or sys.executable).with_name("Render.exe" if os.name == "nt" else "Render")
    if not render.is_file():
        raise ValueError(f"Maya command-line renderer is unavailable: {render}")
    with view_camera(cmds, camera, width, height, frame_shapes) as shape, TemporaryDirectory(dir=path.parent) as temp:
        folder = Path(temp)
        scene = folder / "view.ma"
        cmds.file(scene.as_posix(), exportAll=True, force=True, type="mayaAscii",
                  preserveReferences=False)
        frame = str(cmds.currentTime(query=True))
        arguments = [str(render), "-r", "sw", "-proj", temp, "-rd", temp, "-im", "mcp_render",
                     "-of", "png", "-s", frame, "-e", frame, "-cam", shape.split("|")[-1],
                     "-x", str(width), "-y", str(height), str(scene)]
        log = folder / "render.log"
        with log.open("wb") as stream:
            result = subprocess.run(arguments, cwd=temp, stdout=stream, stderr=subprocess.STDOUT,
                                    timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        frames = list(folder.glob("mcp_render*.png*"))
        if result.returncode or len(frames) != 1:
            detail = log.read_bytes()[-1000:].decode(locale.getpreferredencoding(False), errors="replace")
            raise RuntimeError(f"Maya Software did not produce the requested PNG ({result.returncode}): {detail}")
        source = frames[0].rename(folder / "view.png")
        publish_png(source, path, width, height, overwrite)
    return {**written(path), "source": "mayaSoftware", "camera": camera}


def publish_png(source, target, width, height, overwrite=False):
    from maya.api import OpenMaya as om

    if not source.is_file():
        raise RuntimeError(f"Maya did not produce {source.name}")
    with source.open("rb") as stream:
        if stream.read(8) != b"\x89PNG\r\n\x1a\n":
            raise RuntimeError(f"Expected a PNG from Maya: {source.name}")
    image = om.MImage()
    image.readFromFile(source.as_posix())
    if tuple(image.getSize()) != (width, height):
        raise RuntimeError(f"Unexpected dimensions in {source.name}")
    publish_file(source, target, overwrite)


def capture_viewport(cmds, path, camera, width, height, overwrite=False, frame_shapes=()):
    from .handlers import written

    dimensions(width, height)
    panels = cmds.getPanel(type="modelPanel") or []
    if not panels:
        raise RuntimeError("No Maya model panel available")
    focused = cmds.getPanel(withFocus=True)
    panel = focused if focused in panels else panels[0]
    previous = cmds.modelPanel(panel, query=True, camera=True)
    image_format = cmds.getAttr("defaultRenderGlobals.imageFormat")
    with view_camera(cmds, camera, width, height, frame_shapes) as shape, TemporaryDirectory(dir=path.parent) as temp:
        target = Path(temp) / "viewport.png"
        try:
            cmds.modelPanel(panel, edit=True, camera=shape)
            cmds.setAttr("defaultRenderGlobals.imageFormat", 32)
            cmds.playblast(editorPanelName=panel, format="image", compression="png", viewer=False,
                           completeFilename=target.as_posix(), showOrnaments=False,
                           frame=cmds.currentTime(query=True), percent=100,
                           widthHeight=(width, height), offScreen=True, forceOverwrite=True)
            publish_png(target, path, width, height, overwrite)
        finally:
            cmds.modelPanel(panel, edit=True, camera=previous)
            cmds.setAttr("defaultRenderGlobals.imageFormat", image_format)
    return {**written(path), "source": "mayaViewport", "camera": camera}


def uv_snapshot(cmds, path, nodes, size, uv_set=None, bounds=None, overwrite=False):
    from .handlers import written

    dimensions(size, size)
    options = {"uvSetName": uv_set} if uv_set is not None else {}
    if bounds is not None:
        if (len(bounds) != 4 or any(not math.isfinite(v) for v in bounds) or
                bounds[0] >= bounds[2] or bounds[1] >= bounds[3]):
            raise ValueError("Bounds must be [min_u, min_v, max_u, max_v] with positive width and height")
        options.update(zip(("uMin", "vMin", "uMax", "vMax"), bounds))
    selection = cmds.ls(selection=True, long=True) or []
    with TemporaryDirectory(dir=path.parent) as temp:
        target = Path(temp) / "uv.png"
        try:
            cmds.select(nodes, replace=True)
            cmds.uvSnapshot(name=target.as_posix(), fileFormat="png", overwrite=True,
                            xResolution=size, yResolution=size,
                            redColor=244, greenColor=246, blueColor=250, antiAliased=True, **options)
            publish_png(target, path, size, size, overwrite)
        finally:
            cmds.select(selection, replace=True) if selection else cmds.select(clear=True)
    return {**written(path), "source": "mayaUvSnapshot"}

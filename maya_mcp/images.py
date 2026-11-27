from contextlib import contextmanager
import locale
import math
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from .capabilities import is_arnold_license_error
from .config import _is_ascii, staging_temp_root
from .policy import publish_file

PRESETS = {
    "front": (0, 0, 0), "back": (0, 180, 0), "side": (0, 90, 0),
    "left": (0, -90, 0), "top": (-90, 0, 0), "persp": (-14, 24, 0),
}
RENDERERS = {"mayaSoftware": "sw", "arnold": "arnold"}


def dimensions(width, height):
    if not all(type(v) is int and 16 <= v <= 4096 for v in (width, height)):
        raise ValueError("Image dimensions must be between 16 and 4096 pixels")


def compute_ortho_camera(span_x, span_y, width, height, padding=1.16):
    """Return ``(orthographicWidth, horizontalAperture, verticalAperture, filmFit)``.

    Matches the camera film-gate aspect ratio to ``width / height`` with
    horizontal ``filmFit=1`` so orthographic framing neither clips nor stretches
    on non-square portrait or landscape resolutions.
    """
    aspect = float(width) / float(height)
    ortho_width = float(padding) * max(float(span_x), float(span_y) * aspect, 0.01)
    horiz_aperture = 1.0
    vert_aperture = 1.0 / aspect
    return ortho_width, horiz_aperture, vert_aperture, 1


def _temp_base(target, root=None):
    parent = Path(target).parent
    if _is_ascii(str(parent)):
        return parent
    return staging_temp_root(root or parent)


@contextmanager
def _render_resolution(cmds, width, height):
    """Temporarily align defaultResolution with width/height and square pixels."""
    if not callable(getattr(cmds, "objExists", None)) or not cmds.objExists("defaultResolution"):
        yield
        return
    attrs = ("aspectLock", "width", "height", "deviceAspectRatio", "pixelAspect")
    saved = {}
    for attr in attrs:
        full = f"defaultResolution.{attr}"
        try:
            saved[full] = cmds.getAttr(full)
        except Exception:
            pass
    aspect = float(width) / float(height)
    try:
        if "defaultResolution.aspectLock" in saved:
            cmds.setAttr("defaultResolution.aspectLock", False)
        if "defaultResolution.width" in saved:
            cmds.setAttr("defaultResolution.width", width)
        if "defaultResolution.height" in saved:
            cmds.setAttr("defaultResolution.height", height)
        if "defaultResolution.deviceAspectRatio" in saved:
            cmds.setAttr("defaultResolution.deviceAspectRatio", aspect)
        if "defaultResolution.pixelAspect" in saved:
            cmds.setAttr("defaultResolution.pixelAspect", 1.0)
        yield
    finally:
        if "defaultResolution.aspectLock" in saved:
            cmds.setAttr("defaultResolution.aspectLock", False)
        for attr in ("width", "height", "deviceAspectRatio", "pixelAspect", "aspectLock"):
            full = f"defaultResolution.{attr}"
            if full in saved:
                try:
                    cmds.setAttr(full, saved[full])
                except Exception:
                    pass


@contextmanager
def view_camera(cmds, camera, width, height, frame_shapes=()):
    if camera not in PRESETS:
        if not isinstance(camera, str) or any(char in camera for char in "*?[]"):
            raise ValueError("Use an exact camera name or preset")
        matches = cmds.ls(camera, exactType="camera", long=True) or []
        if not matches:
            transforms = cmds.ls(camera, exactType="transform", long=True) or []
            if len(transforms) == 1:
                matches = [shape for shape in
                           cmds.listRelatives(transforms[0], shapes=True, fullPath=True) or []
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
        distance = 30 + 3 * max(v.length() for v in offsets)
        position = center + forward * distance
        cmds.xform(node, worldSpace=True, translation=tuple(position))
        ortho_width, h_ap, v_ap, film_fit = compute_ortho_camera(span_x, span_y, width, height)
        cmds.setAttr(shape + ".orthographicWidth", ortho_width)
        cmds.setAttr(shape + ".horizontalFilmAperture", h_ap)
        cmds.setAttr(shape + ".verticalFilmAperture", v_ap)
        cmds.setAttr(shape + ".filmFit", film_fit)
        cmds.setAttr(shape + ".farClipPlane", max(10000.0, distance * 4.0))
        cmds.setAttr(shape + ".nearClipPlane", max(0.01, min(0.1, distance * 1e-4)))
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


def _render_executable(executable=None):
    render_name = "Render.exe" if os.name == "nt" else "Render"
    candidates = []
    if executable:
        candidates.append(Path(executable).with_name(render_name))
    else:
        exe = Path(sys.executable)
        candidates.append(exe.with_name(render_name))
        location = os.environ.get("MAYA_LOCATION")
        if location:
            candidates.append(Path(location) / "bin" / render_name)
        candidates.append(exe.resolve().parent.parent / "bin" / render_name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValueError(f"Maya command-line renderer is unavailable: {candidates[0]}")


def validate_uv_bounds(bounds):
    if bounds is None:
        return None
    if (len(bounds) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in bounds)
            or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]):
        raise ValueError("Bounds must be [min_u, min_v, max_u, max_v] with positive width and height")
    return bounds


def _invoke_cli_render(render_bin, renderer_flag, folder, scene, frame, shape, width, height, timeout):
    for stale in folder.glob("mcp_render*.png*"):
        stale.unlink(missing_ok=True)
    arguments = [
        str(render_bin), "-r", renderer_flag, "-proj", str(folder), "-rd", str(folder),
        "-im", "mcp_render", "-of", "png", "-s", frame, "-e", frame, "-cam", shape,
        "-x", str(width), "-y", str(height), str(scene),
    ]
    log = folder / f"render_{renderer_flag}.log"
    with log.open("wb") as stream:
        result = subprocess.run(
            arguments, cwd=str(folder), stdout=stream, stderr=subprocess.STDOUT,
            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    frames = list(folder.glob("mcp_render*.png*"))
    detail = log.read_bytes()[-1500:].decode(locale.getpreferredencoding(False), errors="replace")
    return result.returncode, frames, detail


def render_view(cmds, path, camera, width, height, timeout=600, executable=None, overwrite=False,
                frame_shapes=(), renderer="mayaSoftware", fallback_to_software=False, root=None):
    from .handlers import written

    dimensions(width, height)
    if renderer not in RENDERERS:
        raise ValueError(f"Unsupported renderer {renderer!r}; choose one of {sorted(RENDERERS)}")
    if type(fallback_to_software) is not bool:
        raise ValueError("fallback_to_software must be a boolean")
    render = _render_executable(executable)
    warnings = []
    actual_renderer = renderer
    fallback = None

    with (
        view_camera(cmds, camera, width, height, frame_shapes) as shape,
        _render_resolution(cmds, width, height),
        TemporaryDirectory(dir=_temp_base(path, root=root)) as temp,
    ):
        folder = Path(temp)
        scene = folder / "view.ma"
        cmds.file(scene.as_posix(), exportAll=True, force=True, type="mayaAscii",
                  preserveReferences=False)
        frame = str(cmds.currentTime(query=True))

        rc, frames, detail = _invoke_cli_render(
            render, RENDERERS[renderer], folder, scene, frame, shape, width, height, timeout
        )
        arnold_unlicensed = renderer == "arnold" and is_arnold_license_error(detail)
        if rc or len(frames) != 1 or arnold_unlicensed:
            if renderer == "arnold":
                reason = (
                    "Arnold license unavailable (environment/RLM batch license missing)"
                    if arnold_unlicensed else f"Arnold render failed ({rc})"
                )
                if not fallback_to_software:
                    raise RuntimeError(
                        f"{reason}; no image was published. Use renderer='mayaSoftware' or pass "
                        f"fallback_to_software=True for an explicit Maya Software fallback. Detail: {detail}"
                    )
                warnings.append(
                    f"{reason}; fell back to mayaSoftware as requested by fallback_to_software=True."
                )
                actual_renderer = "mayaSoftware"
                fallback = "mayaSoftware"
                rc, frames, detail = _invoke_cli_render(
                    render, RENDERERS["mayaSoftware"], folder, scene, frame, shape, width, height, timeout
                )
            if rc or len(frames) != 1:
                raise RuntimeError(f"Maya Software did not produce the requested PNG ({rc}): {detail}")

        source = frames[0].rename(folder / "view.png")
        publish_png(source, path, width, height, overwrite)

    result = {
        **written(path),
        "source": actual_renderer,
        "renderer": actual_renderer,
        "requested_renderer": renderer,
        "camera": camera,
        "width": width,
        "height": height,
        "aspect_ratio": round(width / height, 6),
    }
    if fallback is not None:
        result["fallback"] = fallback
    if warnings:
        result["warnings"] = warnings
    return result


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


def capture_viewport(cmds, path, camera, width, height, overwrite=False, frame_shapes=(), root=None):
    from .handlers import written

    dimensions(width, height)
    panels = cmds.getPanel(type="modelPanel") or []
    if not panels:
        raise RuntimeError("No Maya model panel available")
    focused = cmds.getPanel(withFocus=True)
    panel = focused if focused in panels else panels[0]
    previous = cmds.modelPanel(panel, query=True, camera=True)
    image_format = cmds.getAttr("defaultRenderGlobals.imageFormat")
    with (
        view_camera(cmds, camera, width, height, frame_shapes) as shape,
        _render_resolution(cmds, width, height),
        TemporaryDirectory(dir=_temp_base(path, root=root)) as temp,
    ):
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
    return {
        **written(path),
        "mode": "gui",
        "is_gui_viewport": True,
        "requested_source": "mayaViewport",
        "source": "mayaViewport",
        "fallback": None,
        "panel": panel,
        "camera": camera,
        "width": width,
        "height": height,
        "aspect_ratio": round(width / height, 6),
    }


def uv_snapshot(cmds, path, nodes, size, uv_set=None, bounds=None, overwrite=False, root=None):
    from .handlers import written

    dimensions(size, size)
    options = {"uvSetName": uv_set} if uv_set is not None else {}
    if validate_uv_bounds(bounds) is not None:
        options.update(zip(("uMin", "vMin", "uMax", "vMax"), bounds))
    selection = cmds.ls(selection=True, long=True) or []
    with TemporaryDirectory(dir=_temp_base(path, root=root)) as temp:
        target = Path(temp) / "uv.png"
        try:
            cmds.select(nodes, replace=True)
            cmds.uvSnapshot(name=target.as_posix(), fileFormat="png", overwrite=True,
                            xResolution=size, yResolution=size,
                            redColor=244, greenColor=246, blueColor=250, antiAliased=True, **options)
            publish_png(target, path, size, size, overwrite)
        finally:
            cmds.select(selection, replace=True) if selection else cmds.select(clear=True)
    return {**written(path), "source": "mayaUvSnapshot", "size": size}

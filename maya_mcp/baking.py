"""Transfer Maps, projected geometry baking and Arnold Render to Texture."""

import math
import os
import struct
import subprocess
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

from .capabilities import has_command, is_arnold_license_error, require_plugin, transfer_command
from .images import _temp_base, dimensions, publish_png
from .policy import publish_file
from .topology import mesh_dag
from .uv import current_uv_set, preserve_selection


class Baking:
    def bake_map(self, source: str, target: str, kind: str, path: str, size: int = 512,
                 uv_set: Optional[str] = None, max_distance: Optional[float] = None,
                 ao_samples: int = 16, ao_distance: Optional[float] = None,
                 curvature_scale: Optional[float] = None, overwrite: bool = False,
                 allow_outside_root: bool = False):
        """Bake a high mesh to a low mesh's 1001 UV tile as a PNG. Distances use Maya world units."""
        from .handlers import written

        c = self.cmds
        _, high = self._mesh(source)
        low_node, low = self._mesh(target)
        kinds = {"normal", "color", "ao", "curvature"}
        if high == low or kind not in kinds:
            raise ValueError("Use distinct high/low meshes and kind normal/color/ao/curvature")
        dimensions(size, size)
        uv = self.uv_inspect(low_node, uv_set)
        uv_set = uv["uv_set"]
        if uv["unmapped_faces"] or uv["tiles"] != [{"u": 0, "v": 0, "udim": 1001}]:
            raise ValueError("Bake targets need complete, non-overlapping UVs in tile 1001")
        box = c.exactWorldBoundingBox(low)
        diagonal = math.sqrt(sum((a - b) ** 2 for a, b in zip(box[:3], box[3:])))
        if not diagonal:
            raise ValueError("The target mesh has zero world-space extent")
        max_distance = diagonal * 0.1 if max_distance is None else max_distance
        ao_distance = diagonal * 0.25 if ao_distance is None else ao_distance
        curvature_scale = diagonal * 0.2 if curvature_scale is None else curvature_scale
        if any(not 0 < value < math.inf for value in (max_distance, ao_distance, curvature_scale)):
            raise ValueError("Bake distances and curvature scale must be finite and positive")
        if type(ao_samples) is not int or not 1 <= ao_samples <= 64:
            raise ValueError("Use an AO sample count in 1-64")

        output = self._output(path, {".png"}, overwrite, allow_outside_root=allow_outside_root)
        stats, warnings = {}, []
        with TemporaryDirectory(prefix="bake-", dir=_temp_base(output, root=self.settings.root)) as folder:
            image = Path(folder) / "map.png"
            if kind in {"normal", "color"}:
                engine = transfer_command(c)
                if engine is None:
                    raise ValueError("Maya Transfer Maps is unavailable for high-to-low normal/color baking")
                getattr(c, engine)(
                    target=low, source=high, uvSet=uv_set, searchOffset=0.0,
                    maxSearchDistance=max_distance, mapOutput={"normal": "normal", "color": "diffuseRGB"}[kind],
                    mapWidth=size, mapHeight=size, mapSpace="tangent", mapMaterials=kind == "color",
                    filename=image.with_suffix("").as_posix(), fileFormat="png", superSampling=1)
            else:
                if size > 1024:
                    raise ValueError("Projected AO and curvature support up to 1024 pixels per side")
                engine = "mayaMeshProjection"
                stats = projected_bake(high, low, uv_set, kind, size, max_distance, ao_samples,
                                       ao_distance, curvature_scale, diagonal, image)
                if stats["covered_pixels"] != stats["projected_pixels"]:
                    warnings.append("Some low-UV texels had no high-mesh match; their alpha is zero.")
                if stats["overlap_pixels"]:
                    warnings.append("Overlapping UV texels were encountered; the first covering triangle was used.")
            publish_png(image, output, size, size, overwrite=overwrite)

        return {**written(output), "source": high, "target": low, "kind": kind, "engine": engine,
                "uv_set": uv_set, "resolution": size, **stats, "warnings": warnings}

    def bake_arnold_texture(self, name: str, kind: str, path: str, size: int = 512,
                            uv_set: Optional[str] = None, aa_samples: int = 3, overwrite: bool = False,
                            allow_outside_root: bool = False):
        """Bake AO, curvature or assigned material in the mesh's 1001 UV tile as an Arnold EXR (requires an Arnold license)."""
        from .handlers import written

        c = self.cmds
        node, shape = self._mesh(name)
        shaders = {"ao": "aiAmbientOcclusion", "curvature": "aiCurvature", "color": None}
        if kind not in shaders or type(aa_samples) is not int or not 1 <= aa_samples <= 8:
            raise ValueError("Use kind ao/curvature/color and AA samples in 1-8")
        dimensions(size, size)
        uv = self.uv_inspect(node, uv_set)
        if uv["unmapped_faces"] or uv["tiles"] != [{"u": 0, "v": 0, "udim": 1001}]:
            raise ValueError("Arnold Render to Texture requires complete UVs in tile 1001")
        output = self._output(path, {".exr"}, overwrite, create_parent=False,
                              allow_outside_root=allow_outside_root)
        require_plugin(c, "mtoa")
        if not has_command(c, "arnoldRenderToTexture"):
            raise ValueError("The loaded MtoA does not provide arnoldRenderToTexture")
        output.parent.mkdir(parents=True, exist_ok=True)

        with TemporaryDirectory(prefix="arnold-", dir=_temp_base(output, root=self.settings.root)) as folder, preserve_selection(c):
            shader = c.shadingNode(shaders[kind], asShader=True) if shaders[kind] else None
            try:
                c.select(node, replace=True)
                # Unit UVs are bounded above, so Arnold writes a single image.
                try:
                    c.arnoldRenderToTexture(folder=folder, resolution=size, aa_samples=aa_samples,
                                            uv_set=uv["uv_set"], all_udims=False, udims="1001",
                                            shader=shader or "")
                except RuntimeError as exc:
                    if is_arnold_license_error(str(exc)):
                        raise RuntimeError(
                            f"Arnold license state unavailable ({exc}); no image was published. "
                            "Arnold batch/headless licensing is controlled by the environment; "
                            "use native bake_map for PNG normal/color/ao/curvature baking without Arnold."
                        ) from exc
                    raise
                files = list(Path(folder).glob("*.exr"))
                if len(files) != 1:
                    raise RuntimeError(f"Expected a single Arnold EXR, got {len(files)}")
                module = Path(c.pluginInfo("mtoa", query=True, path=True)).parent.parent
                checker = module / "bin" / ("oiiotool.exe" if os.name == "nt" else "oiiotool")
                verify_exr(files[0], size, checker)
                publish_file(files[0], output, overwrite)
            finally:
                if shader is not None:
                    c.delete(shader)
        return {**written(output), "name": node, "kind": kind, "engine": "arnoldRenderToTexture",
                "uv_set": uv["uv_set"], "resolution": size}


def verify_exr(path, size, checker):
    """Reject empty or unlicensed Arnold outputs before publishing."""
    with path.open("rb") as stream:
        if stream.read(4) != b"\x76\x2f\x31\x01":
            raise RuntimeError("Arnold output is not an OpenEXR file")
        version = struct.unpack("<I", stream.read(4))[0]
        if version & 0x1800:
            raise RuntimeError("Expected a single-part flat OpenEXR")
        header = {}
        for _ in range(1024):
            name = _exr_string(stream)
            if not name:
                break
            _exr_string(stream)  # attribute type
            count = struct.unpack("<I", stream.read(4))[0]
            if count > 1024 * 1024:
                raise RuntimeError("Oversized Arnold EXR header attribute")
            header[name] = stream.read(count)
        else:
            raise RuntimeError("Oversized Arnold EXR header")
        license_state = header.get(b"arnold/license_state", b"").lower().rstrip(b"\0")
        if license_state in {b"invalid", b"unlicensed", b"failed"}:
            raise RuntimeError(
                f"Arnold license state {license_state.decode()}; no image was published. "
                "Use native bake_map for PNG baking when an Arnold batch license is unavailable."
            )
        x0, y0, x1, y1 = struct.unpack("<iiii", header.get(b"dataWindow", b""))
        if (x1 - x0 + 1, y1 - y0 + 1) != (size, size):
            raise RuntimeError("Arnold output has unexpected pixel dimensions")
        if version & 0x200:
            tile_width, tile_height, mode = struct.unpack("<IIB", header.get(b"tiles", b""))
            if not tile_width or not tile_height or mode & 0xf:
                raise RuntimeError("Unexpected Arnold EXR tile layout")
            chunks = math.ceil(size / tile_width) * math.ceil(size / tile_height)
            metadata_size = 20
        else:
            blocks = {0: 1, 1: 1, 2: 1, 3: 16, 4: 32, 5: 16, 6: 32, 7: 32, 8: 32, 9: 256}
            block = list(header.get(b"compression", b"\0"))[0]
            chunks = math.ceil(size / blocks[block])
            metadata_size = 8
        first_chunk = stream.tell() + 8 * chunks
        offsets = [struct.unpack("<Q", stream.read(8))[0] for _ in range(chunks)]
        file_size = path.stat().st_size
        for offset in offsets:
            if not first_chunk <= offset <= file_size - metadata_size:
                raise RuntimeError("Arnold EXR contains missing pixel data")
            stream.seek(offset + metadata_size - 4)
            bytes_in_chunk = struct.unpack("<I", stream.read(4))[0]
            if not bytes_in_chunk or offset + metadata_size + bytes_in_chunk > file_size:
                raise RuntimeError("Arnold EXR contains truncated pixel data")
    if checker.is_file():
        result = subprocess.run([str(checker), "--stats", str(path)], capture_output=True, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError("Arnold's OpenImageIO could not decode the EXR pixels")


def _exr_string(stream):
    chars = bytearray()
    for _ in range(256):
        char = stream.read(1)
        if char == b"\0":
            return bytes(chars)
        if not char:
            break
        chars.extend(char)
    raise RuntimeError("Invalid OpenEXR header string")


def projected_bake(high, low, uv_set, kind, size, distance, samples, ao_distance, scale, diagonal, path):
    from maya.api import OpenMaya as om

    high_path = mesh_dag(high)
    source, target = om.MFnMesh(high_path), om.MFnMesh(mesh_dag(low))
    space = om.MSpace.kWorld
    points = target.getPoints(space)
    us, vs = target.getUVs(uv_set)
    face_sizes, vertices = target.getVertices()
    uv_counts, uv_ids = target.getAssignedUVs(uv_set)
    tri_counts, tri_ids = target.getTriangles()
    pixels = bytearray(size * size * 4)
    coverage = bytearray(size * size)
    accel = source.autoUniformGridParams()
    epsilon = max(1e-8, diagonal * 1e-5)
    curvature = vertex_curvature(high_path, source, space) if kind == "curvature" else None
    offsets = hemisphere(samples) if kind == "ao" else []
    face_offset = uv_offset = tri_offset = covered = projected = overlaps = 0

    for face, count in enumerate(face_sizes):
        count_uv = uv_counts[face]
        ids_uv = uv_ids[uv_offset:uv_offset + count_uv]
        ids_vertex = vertices[face_offset:face_offset + count]
        triangles_end = tri_offset + 3 * tri_counts[face]
        uv_offset += count_uv
        face_offset += count
        if count_uv != count:
            raise ValueError("Every low-mesh face corner needs a UV")
        corner_uv = dict(zip(ids_vertex, ids_uv))
        for offset in range(tri_offset, triangles_end, 3):
            a, b, c = [points[i] for i in tri_ids[offset:offset + 3]]
            uv_ids_tri = [corner_uv[i] for i in tri_ids[offset:offset + 3]]
            coordinates = [(us[i], vs[i]) for i in uv_ids_tri]
            direction = (b - a) ^ (c - a)
            if not direction.length():
                continue
            direction.normalize()
            direction = om.MFloatVector(direction.x, direction.y, direction.z)
            for x, y, w1, w2 in rasterize(coordinates, size):
                index = (size - 1 - y) * size + x
                interior = min(w1, w2, 1 - w1 - w2) > 1e-10
                if coverage[index]:
                    overlaps += int(interior and coverage[index] == 2)
                    continue
                coverage[index] = 2 if interior else 1
                covered += 1
                point = a + (b - a) * w1 + (c - a) * w2
                start = om.MFloatPoint(point.x, point.y, point.z)
                hit = source.closestIntersection(start, direction, space, distance, True, accelParams=accel)
                if hit is not None:
                    sample, _, hit_face, hit_tri, h1, h2 = hit
                    sample = om.MPoint(sample.x, sample.y, sample.z)
                else:
                    sample, hit_face = source.getClosestPoint(point, space)
                    if sample.distanceTo(point) > distance:
                        continue
                if kind == "ao":
                    normal, _ = source.getClosestNormal(sample, space)
                    value = ambient_occlusion(source, sample, normal, offsets, ao_distance, epsilon, accel, space)
                else:
                    if hit is not None:
                        v0, v1, v2 = source.getPolygonTriangleVertices(hit_face, hit_tri)
                        mean = h1 * curvature[v0] + h2 * curvature[v1] + (1 - h1 - h2) * curvature[v2]
                    else:
                        verts = source.getPolygonVertices(hit_face)
                        mean = sum(curvature[i] for i in verts) / len(verts)
                    value = 0.5 + 0.5 * math.tanh(mean * scale)
                grey = round(255 * max(0, min(1, value)))
                pixels[4 * index:4 * index + 4] = bytes((grey, grey, grey, 255))
                projected += 1
        tri_offset = triangles_end

    if not projected:
        raise RuntimeError("No low-mesh texels hit the high mesh within the search distance")
    write_png(path, size, pixels)
    return {"covered_pixels": covered, "projected_pixels": projected, "overlap_pixels": overlaps}


def rasterize(points, size):
    (au, av), (bu, bv), (cu, cv) = points
    denom = (bu - au) * (cv - av) - (cu - au) * (bv - av)
    if abs(denom) < 1e-12:
        return
    left = max(0, math.floor(min(au, bu, cu) * size))
    right = min(size, math.ceil(max(au, bu, cu) * size))
    bottom = max(0, math.floor(min(av, bv, cv) * size))
    top = min(size, math.ceil(max(av, bv, cv) * size))
    for y in range(bottom, top):
        v = (y + 0.5) / size - av
        for x in range(left, right):
            u = (x + 0.5) / size - au
            w1 = (u * (cv - av) - v * (cu - au)) / denom
            w2 = (v * (bu - au) - u * (bv - av)) / denom
            if min(w1, w2, 1 - w1 - w2) >= -1e-10:
                yield x, y, w1, w2


def vertex_curvature(dag, mesh, space):
    from maya.api import OpenMaya as om

    points = mesh.getPoints(space)
    normals = [om.MVector(n.x, n.y, n.z) for n in mesh.getVertexNormals(True, space)]
    iterator = om.MItMeshVertex(dag)
    values = []
    while not iterator.isDone():
        index = iterator.index()
        bends = []
        for neighbor in iterator.getConnectedVertices():
            edge = points[neighbor] - points[index]
            squared = edge * edge
            if squared > 1e-16:
                bends.append(((normals[neighbor] - normals[index]) * edge) / squared)
        values.append(sum(bends) / len(bends) if bends else 0)
        iterator.next()
    return values


def hemisphere(count):
    samples = []
    for index in range(count):
        r = math.sqrt((index + 0.5) / count)
        angle = 2 * math.pi * index / ((1 + math.sqrt(5)) * 0.5)
        samples.append((r * math.cos(angle), r * math.sin(angle), math.sqrt(1 - r * r)))
    return samples


def ambient_occlusion(mesh, point, normal, samples, distance, epsilon, accel, space):
    from maya.api import OpenMaya as om

    normal = normal.normal()
    axis = om.MVector(0, 1, 0) if abs(normal.y) < 0.95 else om.MVector(1, 0, 0)
    tangent = (axis ^ normal).normal()
    bitangent = normal ^ tangent
    start = point + normal * epsilon
    start = om.MFloatPoint(start.x, start.y, start.z)
    blocked = 0
    for x, y, z in samples:
        ray = tangent * x + bitangent * y + normal * z
        hit = mesh.anyIntersection(start, om.MFloatVector(ray.x, ray.y, ray.z), space,
                                   distance, False, accelParams=accel)
        blocked += bool(hit and hit[1] > epsilon * 0.5)
    return 1 - blocked / len(samples)


def write_png(path, size, pixels):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    stride = size * 4
    scanlines = b"".join(b"\x00" + pixels[start:start + stride] for start in range(0, len(pixels), stride))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" +
                     chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) +
                     chunk(b"IDAT", zlib.compress(scanlines)) + chunk(b"IEND", b""))


TOOLS = (Baking.bake_map, Baking.bake_arnold_texture)

"""UV editing and tile inspection for explicit mesh transforms and UV sets."""

import math
from contextlib import contextmanager
from typing import List, Optional, Tuple

from .capabilities import has_command, require_plugin
from .topology import mesh_dag


@contextmanager
def preserve_selection(cmds):
    previous = cmds.ls(selection=True, long=True) or []
    try:
        yield
    finally:
        cmds.select(previous, replace=True) if previous else cmds.select(clear=True)


def current_uv_set(cmds, shape, uv_set=None):
    available = cmds.polyUVSet(shape, query=True, allUVSets=True) or []
    if uv_set is None:
        current = cmds.polyUVSet(shape, query=True, currentUVSet=True) or []
        uv_set = current[0] if current else None
    if uv_set not in available:
        raise ValueError(f"UV set {uv_set!r} does not exist on {shape}; available: {available}")
    return uv_set


@contextmanager
def using_uv_set(cmds, shapes, uv_set):
    previous = [(shape, current_uv_set(cmds, shape)) for shape in shapes]
    target = [(shape, current_uv_set(cmds, shape, uv_set)) for shape in shapes]
    try:
        for shape, name in target:
            if uv_set is not None:
                cmds.polyUVSet(shape, currentUVSet=True, uvSet=name)
        yield
    finally:
        for shape, name in previous:
            if uv_set is not None:
                cmds.polyUVSet(shape, currentUVSet=True, uvSet=name)


class UV:
    def uv_project(self, name: str, method: str, uv_set: Optional[str] = None, axis: str = "y",
                   faces: Optional[List[int]] = None):
        """Project all or indexed faces with planar, cylindrical, spherical or automatic mapping."""
        c = self.cmds
        node, shape = self._mesh(name)
        uv_set = current_uv_set(c, shape, uv_set)
        if method not in {"planar", "cylindrical", "spherical", "automatic"} or axis not in {"x", "y", "z"}:
            raise ValueError("Use planar/cylindrical/spherical/automatic and axis x/y/z")
        components = self._components(node, faces, "f", "face")[1] if faces is not None else [shape + ".f[*]"]
        if method == "automatic":
            c.polyAutoProjection(components, planes=6, uvSetName=uv_set, constructionHistory=False)
        else:
            c.polyProjection(components, type=method, mapDirection=axis, smartFit=True,
                             uvSetName=uv_set, constructionHistory=False)
        return {"name": node, "uv_set": uv_set, "projection": method}

    def uv_unfold(self, name: str, uv_set: Optional[str] = None, iterations: int = 8):
        """Unfold UV shells with Unfold3D when available, or with Maya's legacy unfold."""
        if not 1 <= iterations <= 100:
            raise ValueError("Use 1-100 iterations")
        c = self.cmds
        node, shape = self._mesh(name)
        uv_set = current_uv_set(c, shape, uv_set)
        warnings = []
        uvs = shape + ".map[*]"
        with using_uv_set(c, [shape], uv_set), preserve_selection(c):
            if not c.polyEvaluate(shape, uvcoord=True):
                raise ValueError("Project UVs before unfolding")
            c.select(uvs, replace=True)
            try:
                if not has_command(c, "u3dUnfold"):
                    require_plugin(c, "Unfold3D")
                c.u3dUnfold(iterations=iterations, pack=False)
                algorithm = "Unfold3D"
            except (AttributeError, RuntimeError, ValueError, TypeError) as exc:
                warnings.append(f"Unfold3D unavailable or failed: {exc}. Used legacy unfold.")
                c.unfold(uvs, iterations=iterations, applyToShell=True)
                algorithm = "unfold"
        return {"name": node, "uv_set": uv_set, "algorithm": algorithm, "warnings": warnings}

    def uv_layout(self, names: List[str], uv_set: Optional[str] = None,
                  padding: float = 0.02, tile: Tuple[int, int] = (0, 0)):
        """Pack shells across the given meshes into one tile, then apply the requested tile offset."""
        if not 0 <= padding <= 0.15 or len(tile) != 2 or any(type(i) is not int or abs(i) > 1000 for i in tile):
            raise ValueError("Use padding in [0, 0.15] and two integer tile coordinates within ±1000")
        c = self.cmds
        nodes = self._mesh_nodes(names)
        shapes = [self._mesh(node)[1] for node in nodes]
        sets = {current_uv_set(c, shape, uv_set) for shape in shapes}
        if len(sets) != 1:
            raise ValueError("Use a UV set with the same name on all selected meshes")
        uv_set = sets.pop()
        with using_uv_set(c, shapes, uv_set), preserve_selection(c):
            if any(not c.polyEvaluate(shape, uvcoord=True) for shape in shapes):
                raise ValueError("All meshes must have UVs before layout")
            c.polyMultiLayoutUV(shapes, layout=2, layoutMethod=1, scale=1, rotateForBestFit=1,
                                percentageSpace=100 * padding, uvSetName=uv_set)
            if any(tile):
                for shape in shapes:
                    c.polyEditUV(shape + ".map[*]", relative=True, uValue=tile[0], vValue=tile[1],
                                 uvSetName=uv_set)
        return {"names": nodes, "uv_set": uv_set, "tile": list(tile), "algorithm": "polyMultiLayoutUV"}

    def uv_sets(self, name: str, action: str = "list", uv_set: Optional[str] = None,
                new_set: Optional[str] = None):
        """List, create, activate, copy, rename or delete a mesh UV set."""
        c = self.cmds
        node, shape = self._mesh(name)
        known = c.polyUVSet(shape, query=True, allUVSets=True) or []
        if action == "create":
            if uv_set is None or uv_set in known:
                raise ValueError("Provide an unused uv_set name")
            c.polyUVSet(shape, create=True, uvSet=self._name(uv_set))
        elif action in {"activate", "copy", "rename", "delete"}:
            uv_set = current_uv_set(c, shape, uv_set)
            if action in {"copy", "rename"}:
                if new_set is None or new_set in known:
                    raise ValueError("Provide an unused new_set name")
                c.polyUVSet(shape, **{action: True, "uvSet": uv_set, "newUVSet": self._name(new_set)})
            elif action == "activate":
                c.polyUVSet(shape, currentUVSet=True, uvSet=uv_set)
            elif len(known) <= 1:
                raise ValueError("The mesh must keep at least one UV set")
            else:
                prior = current_uv_set(c, shape)
                if prior == uv_set:
                    c.polyUVSet(shape, currentUVSet=True, uvSet=next(n for n in known if n != uv_set))
                try:
                    c.polyUVSet(shape, delete=True, uvSet=uv_set)
                except RuntimeError:
                    c.polyUVSet(shape, currentUVSet=True, uvSet=prior)
                    raise
        elif action != "list":
            raise ValueError("Use list/create/activate/copy/rename/delete")
        return {"name": node, "current": current_uv_set(c, shape),
                "sets": c.polyUVSet(shape, query=True, allUVSets=True) or []}

    def uv_inspect(self, name: str, uv_set: Optional[str] = None):
        """Inspect assigned UVs, shells, tile coverage, UDIM eligibility and faces spanning tiles."""
        from maya.api import OpenMaya as om

        node, shape = self._mesh(name)
        uv_set = current_uv_set(self.cmds, shape, uv_set)
        dag = mesh_dag(shape)
        mesh = om.MFnMesh(dag)
        us, vs = mesh.getUVs(uv_set)
        counts, indexes = mesh.getAssignedUVs(uv_set)
        assigned = sorted(set(indexes))
        used_u, used_v = [us[i] for i in assigned], [vs[i] for i in assigned]
        bounds = [min(used_u), min(used_v), max(used_u), max(used_v)] if assigned else None
        shells = mesh.getUvShellsIds(uv_set)[0] if assigned else 0
        tiles, crossing, unmapped, cursor = set(), [], 0, 0
        for face, count in enumerate(counts):
            if not count:
                unmapped += 1
                continue
            ids = indexes[cursor:cursor + count]
            cursor += count
            lo_u, hi_u = _tile_range(us[i] for i in ids)
            lo_v, hi_v = _tile_range(vs[i] for i in ids)
            if lo_u != hi_u or lo_v != hi_v:
                crossing.append(face)
            if (hi_u - lo_u + 1) * (hi_v - lo_v + 1) > 256:
                raise ValueError("A UV face spans over 256 tiles. Repair the projection before inspecting.")
            tiles.update((u, v) for u in range(lo_u, hi_u + 1) for v in range(lo_v, hi_v + 1))
            if len(tiles) > 4096:
                raise ValueError("UVs span too many tiles to report")
        return {"name": node, "uv_set": uv_set, "uv_count": len(us), "assigned_uv_count": len(assigned),
                "shells": shells, "bounds": bounds, "unmapped_faces": unmapped,
                "tiles": [{"u": u, "v": v, "udim": 1001 + u + 10 * v if 0 <= u <= 9 and v >= 0 else None}
                          for u, v in sorted(tiles, key=lambda pair: pair[::-1])],
                "crossing_faces": len(crossing), "crossing_examples": crossing[:20]}


def _tile_range(coords):
    coords = list(coords)
    start = math.floor(min(coords) + 1e-6)
    return start, max(start, math.ceil(max(coords) - 1e-6) - 1)


TOOLS = (UV.uv_project, UV.uv_unfold, UV.uv_layout, UV.uv_sets, UV.uv_inspect)

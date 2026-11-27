"""Topology generation and inspection using Maya's native mesh APIs."""

import math
from collections import Counter, defaultdict
from itertools import product
from typing import List, Optional

from .capabilities import has_command


def mesh_dag(shape):
    from maya.api import OpenMaya as om

    selection = om.MSelectionList()
    selection.add(shape)
    return selection.getDagPath(0)


class Topology:
    def mesh_topology(self, name: str, symmetry_axis: Optional[str] = None, tolerance: float = 0.001):
        """Count face types and non-manifold components, optionally checking vertex symmetry about the world origin."""
        from maya.api import OpenMaya as om

        node, shape = self._mesh(name)
        c = self.cmds
        dag = mesh_dag(shape)
        mesh = om.MFnMesh(dag)
        counts, _ = mesh.getVertices()
        faces = Counter(counts)
        edges = c.polyInfo(shape, nonManifoldEdges=True) or []
        vertices = c.polyInfo(shape, nonManifoldVertices=True) or []
        result = {"name": node, "faces": mesh.numPolygons, "vertices": mesh.numVertices,
                  "triangles": faces[3], "quads": faces[4],
                  "ngons": sum(count for size, count in faces.items() if size > 4),
                  "non_manifold_edges": len(edges), "non_manifold_vertices": len(vertices),
                  "non_manifold_examples": (edges[:20] + vertices[:20])}
        if symmetry_axis is not None:
            if symmetry_axis not in {"x", "y", "z"} or not 1e-9 <= tolerance <= 100:
                raise ValueError("Use symmetry axis x/y/z and tolerance in [1e-9, 100]")
            result["symmetry"] = _symmetry(mesh.getPoints(om.MSpace.kWorld), symmetry_axis, tolerance)
        return result

    def mesh_reduce(self, source: str, name: str, target_faces: int, brief: bool = False):
        """Duplicate a mesh and reduce polygon count. Returns the actual face count."""
        source, shape = self._mesh(source)
        c = self.cmds
        current = c.polyEvaluate(shape, face=True)
        if not 4 <= target_faces < current:
            raise ValueError("Target face count must be at least 4 and below the source polygon count")
        with self._disposable_mesh(source, name) as node:
            c.polyReduce(node, version=1, percentage=100 * (current - target_faces) / current,
                         keepQuadsWeight=1, keepBorder=True, keepMapBorder=True, preserveTopology=True,
                         replaceOriginal=True, constructionHistory=False)
            c.delete(node, constructionHistory=True)
            result = self.get_object_info(node, brief=brief)
            if not 0 < result["faces"] < current:
                raise ValueError("Maya could not reduce this mesh while preserving topology and borders")
            return {**result, "algorithm": "polyReduce", "target_faces": target_faces}

    def mesh_retopo(self, source: str, name: str, target_faces: int, symmetry: bool = False,
                    allow_reduce_fallback: bool = True, brief: bool = False):
        """Generate a quad-oriented low mesh with polyRetopo, optionally degrading to polyReduce."""
        source, shape = self._mesh(source)
        c = self.cmds
        before = c.polyEvaluate(shape, face=True)
        if not 4 <= target_faces < before:
            raise ValueError("Target face count must be at least 4 and below the source polygon count")
        reason = "polyRetopo is unavailable in this Maya installation"
        if has_command(c, "polyRetopo"):
            try:
                with self._disposable_mesh(source, name) as node:
                    c.polyRetopo(node, targetFaceCount=target_faces, preprocessMesh=True,
                                 symmetry=symmetry, replaceOriginal=True, constructionHistory=False)
                    c.delete(node, constructionHistory=True)
                    result = self.get_object_info(node, brief=brief)
                    if not 0 < result["faces"] < before:
                        raise RuntimeError("polyRetopo did not produce a lower face count")
                    return {**result, "algorithm": "polyRetopo", "target_faces": target_faces}
            except (RuntimeError, TypeError) as exc:
                reason = str(exc)
        if not allow_reduce_fallback:
            raise ValueError(reason)
        result = self.mesh_reduce(source, name, target_faces, brief=brief)
        return {**result, "warnings": [f"{reason}. polyReduce preserves topology where possible and may leave triangles."]}

    def mesh_rebuild(self, source: str, name: str, edge_length: float, max_triangles: int = 100000,
                     brief: bool = False):
        """Rebuild a duplicate with polyRemesh using a world-space maximum edge length."""
        c = self.cmds
        if not has_command(c, "polyRemesh"):
            raise ValueError("polyRemesh is unavailable; use mesh_reduce to simplify the existing mesh")
        if not math.isfinite(edge_length) or edge_length <= 0 or not 4 <= max_triangles <= 1000000:
            raise ValueError("Use positive edge length and a triangle limit of 4-1000000")
        with self._disposable_mesh(self._mesh(source)[0], name) as node:
            c.polyRemesh(node, maxEdgeLength=edge_length, maxTriangleCount=max_triangles,
                         useRelativeValues=False, worldSpace=True, constructionHistory=False)
            return {**self.get_object_info(node, brief=brief), "algorithm": "polyRemesh"}

    def mesh_snap(self, name: str, target: str, vertices: List[int],
                  max_distance: Optional[float] = None):
        """Snap indexed vertices to closest world-space points on another mesh."""
        from maya.api import OpenMaya as om

        c = self.cmds
        node, components = self._components(name, vertices, "vtx", "vertex")
        target, shape = self._mesh(target)
        if target == node or (max_distance is not None and not 0 < max_distance < math.inf):
            raise ValueError("Use a different target and, if specified, a finite positive max_distance")
        mesh = om.MFnMesh(mesh_dag(shape))
        updates = []
        for vertex in components:
            point = om.MPoint(*c.pointPosition(vertex, world=True))
            projected, _ = mesh.getClosestPoint(point, om.MSpace.kWorld)
            if max_distance is None or point.distanceTo(projected) <= max_distance:
                updates.append((vertex, (projected.x, projected.y, projected.z)))
        for vertex, position in updates:
            c.xform(vertex, worldSpace=True, translation=position)
        return {"name": node, "target": target, "snapped": len(updates),
                "skipped": len(components) - len(updates)}

    def set_symmetry(self, enabled: bool, axis: str = "x", about: str = "world"):
        """Set Maya's modeling symmetry mode about the world or object origin."""
        if axis not in {"x", "y", "z"} or about not in {"world", "object"}:
            raise ValueError("Use x/y/z and world/object")
        self.cmds.symmetricModelling(edit=True, symmetry=enabled, axis=axis, about=about)
        return {"enabled": enabled, "axis": axis, "about": about}


def _symmetry(points, axis, tolerance):
    index = "xyz".index(axis)
    buckets = defaultdict(list)

    def cell(point):
        return tuple(math.floor(value / tolerance) for value in point)

    points = [(p.x, p.y, p.z) for p in points]
    for point in points:
        buckets[cell(point)].append(point)
    offsets = tuple(product((-1, 0, 1), repeat=3))
    missing = []
    for number, point in enumerate(points):
        mirrored = list(point)
        mirrored[index] *= -1
        bx, by, bz = cell(mirrored)
        if not any(sum((p - q) ** 2 for p, q in zip(mirrored, candidate)) <= tolerance ** 2
                   for dx, dy, dz in offsets for candidate in buckets.get((bx + dx, by + dy, bz + dz), ())):
            missing.append(number)
    return {"axis": axis, "tolerance": tolerance, "unmatched": len(missing), "examples": missing[:20]}


TOOLS = (Topology.mesh_topology, Topology.mesh_reduce, Topology.mesh_retopo,
         Topology.mesh_rebuild, Topology.mesh_snap, Topology.set_symmetry)

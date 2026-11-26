"""Polygon and NURBS operations shared by GUI and standalone Maya."""

from __future__ import annotations

import math
import sys
from contextlib import contextmanager
from typing import List, Tuple, TYPE_CHECKING

if TYPE_CHECKING or sys.version_info >= (3, 8):
    from typing import Literal

Vector = Tuple[float, float, float]


def finite_vector(values):
    if len(values) != 3 or any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        raise ValueError("Expected three finite coordinates")
    return values


class Modeling:
    def _mesh(self, name):
        transform = self._node(name)
        shapes = self.cmds.listRelatives(transform, shapes=True, type="mesh",
                                         noIntermediate=True, fullPath=True) or []
        if len(shapes) != 1:
            raise ValueError(f"Expected exactly one visible-output mesh shape under {transform}")
        return transform, shapes[0]

    def _mesh_nodes(self, names, minimum=1, maximum=128):
        nodes = [self._mesh(name)[0] for name in names]
        if not minimum <= len(nodes) <= maximum or len(set(nodes)) != len(nodes):
            raise ValueError(f"Expected {minimum}-{maximum} distinct meshes")
        return nodes

    def _identifiers(self, nodes):
        return self.cmds.ls(nodes, uuid=True) or []

    def _delete_ids(self, identifiers):
        if identifiers:
            remaining = self.cmds.ls(identifiers, long=True) or []
            if remaining:
                self.cmds.delete(remaining)

    @contextmanager
    def _disposable_mesh(self, source, name):
        c = self.cmds
        copy = c.duplicate(source, name=self._name(name), returnRootsOnly=True)[0]
        identifier = self._identifiers(copy)
        try:
            yield self._node(copy)
        except Exception:
            self._delete_ids(identifier)
            raise

    def _components(self, name, indices, kind, counter):
        node, shape = self._mesh(name)
        limit = self.cmds.polyEvaluate(shape, **{counter: True})
        if not indices or len(indices) > 10000 or any(type(i) is not int or not 0 <= i < limit for i in indices):
            raise ValueError(f"Provide 1-10000 valid {counter} indices (0 to {limit - 1})")
        return node, [f"{shape}.{kind}[{i}]" for i in sorted(set(indices))]

    def create_curve(self, name: str, points: List[Vector],
                     degree: Literal[1, 3] = 1, closed: bool = False):
        """Create a NURBS profile from world-space control vertices, optionally closing the endpoints."""
        c = self.cmds
        self._name(name)
        if degree not in (1, 3) or not degree + 1 <= len(points) <= 4096:
            raise ValueError("Use degree 1 or 3 and at least degree + 1 control vertices")
        for point in points:
            finite_vector(point)
        curve = self._node(c.curve(name=name, degree=degree, point=points))
        try:
            if closed:
                c.closeCurve(curve, replaceOriginal=True, constructionHistory=False,
                             preserveShape=True)
            return self.get_object_info(curve)
        except Exception:
            c.delete(curve)
            raise

    def loft_mesh(self, curves: List[str], name: str, u_divisions: int = 16, v_divisions: int = 8,
                  closed: bool = False):
        """Loft ordered NURBS profiles and tessellate the result into a polygon mesh."""
        c = self.cmds
        self._name(name)
        profiles = [self._node(curve) for curve in curves]
        if not 2 <= len(set(profiles)) == len(profiles) <= 64 or not all(
                1 <= count <= 256 for count in (u_divisions, v_divisions)):
            raise ValueError("Use 2-64 distinct curves and 1-256 divisions per axis")
        for profile in profiles:
            if not c.listRelatives(profile, shapes=True, type="nurbsCurve", noIntermediate=True):
                raise ValueError(f"Expected a NURBS curve: {profile}")
        surface = c.loft(profiles, constructionHistory=False, autoReverse=True,
                         close=closed, degree=1, polygon=0)[0]
        try:
            result = c.nurbsToPoly(surface, name=name, constructionHistory=False, explicitTessellationAttributes=True,
                                   format=2, polygonType=1, uType=1, uNumber=u_divisions,
                                   vType=1, vNumber=v_divisions)[0]
            return self.get_object_info(result)
        finally:
            c.delete(surface)

    def mesh_extrude(self, name: str, faces: List[int], distance: float,
                     divisions: int = 1, keep_together: bool = True):
        """Extrude indexed polygon faces along their local normals."""
        node, faces = self._components(name, faces, "f", "face")
        if not math.isfinite(distance) or not 1 <= divisions <= 64:
            raise ValueError("Use a finite distance and 1-64 divisions")
        self.cmds.polyExtrudeFacet(faces, localTranslateZ=distance, divisions=divisions,
                                   keepFacesTogether=keep_together, constructionHistory=False)
        return self.get_object_info(node)

    def mesh_bevel(self, name: str, edges: List[int], fraction: float = 0.15, segments: int = 2):
        """Bevel indexed polygon edges using a fraction of the available edge width."""
        node, edges = self._components(name, edges, "e", "edge")
        if not 0 < fraction <= 1 or not 1 <= segments <= 32:
            raise ValueError("Fraction must be in (0, 1] and segments in 1-32")
        self.cmds.polyBevel3(edges, fraction=fraction, segments=segments,
                             offsetAsFraction=True, constructionHistory=False)
        return self.get_object_info(node)

    def mesh_mirror(self, source: str, name: str, axis: Literal["x", "y", "z"] = "x",
                    direction: Literal["positive", "negative"] = "positive", merge: bool = True):
        """Duplicate a mesh and mirror the chosen half across the world-origin plane."""
        if axis not in {"x", "y", "z"} or direction not in {"positive", "negative"}:
            raise ValueError("Choose x/y/z and positive/negative")
        with self._disposable_mesh(self._mesh(source)[0], name) as node:
            self.cmds.polyMirrorFace(node, axis="xyz".index(axis), axisDirection=int(direction == "negative"),
                                     mirrorAxis=2, mirrorPosition=0, mergeMode=int(merge),
                                     cutMesh=True, constructionHistory=False)
            result = self.get_object_info(node)
            if not result["faces"]:
                raise ValueError("Mirror produced no faces; check the selected half and world origin")
            return result

    def array_objects(self, name: str, prefix: str, count: int, offset: Vector):
        """Create count independent copies, starting one world-space offset away from the source."""
        c = self.cmds
        self._name(prefix)
        source = self._node(name)
        if not 1 <= count <= 128:
            raise ValueError("Use 1-128 copies")
        finite_vector(offset)
        copies, ids = [], []
        try:
            for index in range(1, count + 1):
                copy = c.duplicate(source, name=f"{prefix}_{index:03d}", returnRootsOnly=True)[0]
                ids.extend(self._identifiers(copy))
                c.move(*(v * index for v in offset), copy, relative=True, worldSpace=True)
                copies.append(self._node(copy))
        except Exception:
            self._delete_ids(ids)
            raise
        return {"source": source, "objects": copies}

    def mesh_boolean(self, first: str, second: str, name: str,
                     operation: Literal["union", "difference", "intersection"] = "difference"):
        """Apply a polygon boolean to temporary copies. Difference subtracts second from first."""
        operations = {"union": 1, "difference": 2, "intersection": 3}
        if operation not in operations:
            raise ValueError("Unknown boolean operation")
        c = self.cmds
        fn = getattr(c, "polyCBoolOp", None) or c.polyBoolOp
        return self._merge_meshes([first, second], name, fn, operation=operations[operation])

    def mesh_combine(self, names: List[str], name: str):
        """Combine polygon shells from copies of two or more meshes."""
        return self._merge_meshes(names, name, self.cmds.polyUnite, mergeUVSets=1)

    def _merge_meshes(self, names, output, operation_fn, **options):
        c = self.cmds
        nodes = self._mesh_nodes(names, minimum=2)
        self._name(output)
        copies, ids = [], []
        try:
            for node in nodes:
                copy = c.duplicate(node, returnRootsOnly=True)[0]
                ids.extend(self._identifiers(copy))
                copies.append(self._node(copy))
            result = self._node(operation_fn(copies, name=output, constructionHistory=False, **options)[0])
            if not c.polyEvaluate(result, face=True):
                c.delete(result)
                raise ValueError("Operation produced an empty mesh")
            return self.get_object_info(result)
        finally:
            self._delete_ids(ids)

    def mesh_separate(self, source: str, name: str):
        """Duplicate a mesh, then split it into one transform per disconnected shell."""
        c = self.cmds
        source, shape = self._mesh(source)
        if c.polyEvaluate(shape, shell=True) < 2:
            raise ValueError("The mesh has only one connected shell")
        with self._disposable_mesh(source, name) as node:
            results = c.polySeparate(node, constructionHistory=False)
            paths = c.ls(results, long=True, type="transform") or []
            return {"objects": [path for path in paths if c.listRelatives(path, shapes=True, type="mesh")]}

    def group_objects(self, name: str, names: List[str]):
        """Create an Outliner group and reparent the specified transforms in world space."""
        self._name(name)
        nodes = [self._node(node) for node in names]
        if len(nodes) != len(set(nodes)) or len(nodes) > 128:
            raise ValueError("Use at most 128 distinct transforms")
        if any(node.startswith(other + "|") for node in nodes for other in nodes):
            raise ValueError("Group top-level members only; some listed nodes are descendants of others")
        c = self.cmds
        group = c.group(nodes, name=name, world=True) if nodes else c.group(name=name, empty=True, world=True)
        return self.get_object_info(group)


TOOLS = (Modeling.create_curve, Modeling.loft_mesh, Modeling.mesh_extrude, Modeling.mesh_bevel,
         Modeling.mesh_mirror, Modeling.array_objects, Modeling.mesh_boolean, Modeling.mesh_combine,
         Modeling.mesh_separate, Modeling.group_objects)

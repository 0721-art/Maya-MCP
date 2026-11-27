"""Polygon and NURBS operations shared by GUI and standalone Maya."""

from __future__ import annotations

import math
import sys
from contextlib import contextmanager
from typing import List, Optional, Tuple, TYPE_CHECKING

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
                     degree: Literal[1, 3] = 1, closed: bool = False, brief: bool = False):
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
                previous_sel = c.ls(selection=True, long=True) or []
                try:
                    c.select(clear=True)
                    c.closeCurve(curve, replaceOriginal=True, constructionHistory=True,
                                 preserveShape=degree > 1)
                    shapes = c.listRelatives(curve, shapes=True, type="nurbsCurve",
                                             noIntermediate=True, fullPath=True) or []
                    for shape in shapes:
                        c.getAttr(shape + ".form")
                    c.delete(curve, constructionHistory=True)
                finally:
                    c.select(previous_sel, replace=True) if previous_sel else c.select(clear=True)
            return self.get_object_info(curve, brief=brief)
        except Exception:
            c.delete(curve)
            raise

    def create_text(self, text: str, name: str, font: str = "Arial",
                    size: float = 1.0, extrude_depth: float = 0.1,
                    bevel_width: float = 0.0, bevel_depth: float = 0.0,
                    bevel_segments: int = 1, curve_samples: int = 4,
                    translation: Vector = (0, 0, 0), rotation: Vector = (0, 0, 0),
                    parent: Optional[str] = None, layer: Optional[str] = None,
                    brief: bool = False):
        """Create 3D polygon lettering from Maya textCurves and bevelPlus/planarSrf, preserving character holes."""
        c = self.cmds
        self._name(name)
        if not isinstance(text, str) or not text.strip() or len(text) > 256 or "\x00" in text:
            raise ValueError("Provide nonempty text up to 256 characters")
        if not isinstance(font, str) or not font.strip() or len(font) > 128 or "\x00" in font:
            raise ValueError("Provide a valid font name")
        if type(size) not in (int, float) or not math.isfinite(size) or size <= 0:
            raise ValueError("size must be a finite positive number")
        for label, value in (("extrude_depth", extrude_depth),
                             ("bevel_width", bevel_width),
                             ("bevel_depth", bevel_depth)):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{label} must be finite and non-negative")
        if type(bevel_segments) is not int or not 1 <= bevel_segments <= 16:
            raise ValueError("bevel_segments must be an integer in 1-16")
        if type(curve_samples) is not int or not 1 <= curve_samples <= 32:
            raise ValueError("curve_samples must be an integer in 1-32")
        finite_vector(translation)
        finite_vector(rotation)
        target_parent = self._node(parent) if parent else None
        target_layer = self._node(layer, "displayLayer") if layer else None

        root_group = c.textCurves(name=f"{name}_tmpText", font=font, text=text,
                                  constructionHistory=False)[0]
        temp_ids = list(self._identifiers(root_group))
        part_ids = []
        result_node = None
        try:
            box = c.exactWorldBoundingBox(root_group)
            raw_h = box[4] - box[1]
            if raw_h <= 1e-8:
                raise ValueError("Text produced no curve geometry")
            factor = float(size) / raw_h
            cx = (box[0] + box[3]) * 0.5
            cy = (box[1] + box[4]) * 0.5
            cz = (box[2] + box[5]) * 0.5
            c.move(-cx, -cy, -cz, root_group, relative=True, worldSpace=True)
            c.scale(factor, factor, factor, root_group, pivot=(0, 0, 0), absolute=True)
            c.makeIdentity(root_group, apply=True, translate=True, rotate=True, scale=True)

            char_groups = c.listRelatives(root_group, children=True, fullPath=True) or []
            parts = []
            for idx, group in enumerate(char_groups):
                curve_shapes = c.listRelatives(group, allDescendents=True, type="nurbsCurve",
                                               noIntermediate=True, fullPath=True) or []
                curve_xforms = list(dict.fromkeys(
                    c.listRelatives(curve_shapes, parent=True, fullPath=True) or []
                ))
                if not curve_xforms:
                    continue
                part_name = f"{name}_ch{idx}"
                if extrude_depth > 0 or bevel_depth > 0:
                    created = c.bevelPlus(
                        *curve_xforms, constructionHistory=False, name=part_name,
                        polygon=1, width=bevel_width, depth=bevel_depth,
                        extrudeDepth=extrude_depth, capSides=4, numberOfSides=bevel_segments,
                        normalsOutwards=True, polyOutMethod=2, polyOutCurveType=2,
                        polyOutCurveSamples=curve_samples,
                    )[0]
                else:
                    created = c.planarSrf(
                        *curve_xforms, constructionHistory=False, name=part_name, polygon=1,
                    )[0]
                full = self._node(created)
                parts.append(full)
                part_ids.extend(self._identifiers(full))

            if not parts:
                raise ValueError("Text produced no polygon geometry")
            if len(parts) == 1:
                result_node = self._node(c.rename(parts[0], name))
                part_ids = []
            else:
                united = c.polyUnite(parts, name=name, constructionHistory=False, mergeUVSets=1)[0]
                result_node = self._node(united)
                # Remaining source transforms (if any) are cleaned in finally via part_ids.
            # Center Z around 0 so placement matches other primitives.
            rbox = c.exactWorldBoundingBox(result_node)
            rcx = (rbox[0] + rbox[3]) * 0.5
            rcy = (rbox[1] + rbox[4]) * 0.5
            rcz = (rbox[2] + rbox[5]) * 0.5
            c.move(-rcx, -rcy, -rcz, result_node, relative=True, worldSpace=True)
            c.makeIdentity(result_node, apply=True, translate=True, rotate=True, scale=True)
            c.xform(result_node, centerPivots=True)
            return self.modify_object(result_node, translation=translation, rotation=rotation,
                                      parent=target_parent, layer=target_layer, brief=brief)
        except Exception:
            if result_node and c.objExists(result_node):
                c.delete(result_node)
            raise
        finally:
            self._delete_ids(temp_ids)
            self._delete_ids(part_ids)

    def extrude_curve(self, curves: List[str], name: str, depth: float = 0.2,
                      bevel_width: float = 0.0, bevel_depth: float = 0.0,
                      bevel_segments: int = 1, cap: bool = True,
                      curve_samples: int = 4, keep_curves: bool = True,
                      brief: bool = False):
        """Extrude one or more planar NURBS curves (outer outline plus optional hole curves) into a polygon mesh."""
        c = self.cmds
        self._name(name)
        profiles = [self._node(curve) for curve in curves]
        if not 1 <= len(set(profiles)) == len(profiles) <= 64:
            raise ValueError("Use 1-64 distinct NURBS curves")
        profile_shapes = []
        for profile in profiles:
            shapes = c.listRelatives(profile, shapes=True, type="nurbsCurve", noIntermediate=True, fullPath=True) or []
            if not shapes:
                raise ValueError(f"Expected a NURBS curve: {profile}")
            profile_shapes.append((profile, shapes[0]))
        for label, value in (("depth", depth), ("bevel_width", bevel_width), ("bevel_depth", bevel_depth)):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{label} must be finite and non-negative")
        if type(bevel_segments) is not int or not 1 <= bevel_segments <= 16:
            raise ValueError("bevel_segments must be an integer in 1-16")
        if type(curve_samples) is not int or not 1 <= curve_samples <= 32:
            raise ValueError("curve_samples must be an integer in 1-32")

        open_profiles = [
            profile for profile, shape in profile_shapes
            if c.getAttr(shape + ".form") == 0
        ]
        if open_profiles and (cap or depth == 0):
            raise ValueError(
                f"Capped/planar curve extrusion requires closed curves (create_curve(..., closed=True)); "
                f"open curve(s): {open_profiles}"
            )

        if depth == 0 and bevel_depth == 0:
            created = c.planarSrf(*profiles, constructionHistory=False, name=name, polygon=1)[0]
        elif open_profiles:
            surfaces = []
            try:
                for idx, profile in enumerate(profiles):
                    surf = c.extrude(
                        profile, constructionHistory=False, range=False, polygon=0,
                        extrudeType=0, useComponentPivot=0, fixedPath=False,
                        useProfileNormal=False, direction=(0, 0, 1), length=depth,
                    )[0]
                    surfaces.append(surf)
                meshes = []
                for idx, surf in enumerate(surfaces):
                    meshes.append(c.nurbsToPoly(
                        surf, name=f"{name}_part{idx}", constructionHistory=False,
                        explicitTessellationAttributes=True, format=2, polygonType=1,
                        uType=1, uNumber=max(4, curve_samples * 4), vType=1, vNumber=max(1, bevel_segments),
                    )[0])
                created = (c.rename(meshes[0], name) if len(meshes) == 1
                           else c.polyUnite(meshes, name=name, constructionHistory=False, mergeUVSets=1)[0])
            finally:
                for surf in surfaces:
                    if c.objExists(surf):
                        c.delete(surf)
        else:
            created = c.bevelPlus(
                *profiles, constructionHistory=False, name=f"{name}_bp", polygon=1,
                width=bevel_width, depth=bevel_depth, extrudeDepth=depth,
                capSides=4 if cap else 0, numberOfSides=bevel_segments,
                normalsOutwards=True, polyOutMethod=2, polyOutCurveType=2,
                polyOutCurveSamples=curve_samples,
            )[0]
            created = c.rename(created, name)
        result = self._node(created)
        if not c.polyEvaluate(result, face=True):
            c.delete(result)
            raise ValueError("Curve extrusion produced an empty mesh")
        if not keep_curves:
            c.delete(profiles)
        return self.get_object_info(result, brief=brief)

    def loft_mesh(self, curves: List[str], name: str, u_divisions: int = 16, v_divisions: int = 8,
                  closed: bool = False, brief: bool = False):
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
            return self.get_object_info(result, brief=brief)
        finally:
            c.delete(surface)

    def mesh_extrude(self, name: str, faces: List[int], distance: float,
                     divisions: int = 1, keep_together: bool = True, brief: bool = False):
        """Extrude indexed polygon faces along their local normals."""
        node, faces = self._components(name, faces, "f", "face")
        if not math.isfinite(distance) or not 1 <= divisions <= 64:
            raise ValueError("Use a finite distance and 1-64 divisions")
        self.cmds.polyExtrudeFacet(faces, localTranslateZ=distance, divisions=divisions,
                                   keepFacesTogether=keep_together, constructionHistory=False)
        return self.get_object_info(node, brief=brief)

    def mesh_bevel(self, name: str, edges: List[int], fraction: float = 0.15, segments: int = 2,
                   brief: bool = False):
        """Bevel indexed polygon edges using a fraction of the available edge width."""
        node, edges = self._components(name, edges, "e", "edge")
        if not 0 < fraction <= 1 or not 1 <= segments <= 32:
            raise ValueError("Fraction must be in (0, 1] and segments in 1-32")
        self.cmds.polyBevel3(edges, fraction=fraction, segments=segments,
                             offsetAsFraction=True, constructionHistory=False)
        return self.get_object_info(node, brief=brief)

    def mesh_mirror(self, source: str, name: str, axis: Literal["x", "y", "z"] = "x",
                    direction: Literal["positive", "negative"] = "positive", merge: bool = True,
                    brief: bool = False):
        """Duplicate a mesh and mirror the chosen half across the world-origin plane."""
        if axis not in {"x", "y", "z"} or direction not in {"positive", "negative"}:
            raise ValueError("Choose x/y/z and positive/negative")
        with self._disposable_mesh(self._mesh(source)[0], name) as node:
            self.cmds.polyMirrorFace(node, axis="xyz".index(axis), axisDirection=int(direction == "negative"),
                                     mirrorAxis=2, mirrorPosition=0, mergeMode=int(merge),
                                     cutMesh=True, constructionHistory=False)
            result = self.get_object_info(node, brief=brief)
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
                     operation: Literal["union", "difference", "intersection"] = "difference",
                     delete_inputs: bool = False, brief: bool = False):
        """Apply a polygon boolean to temporary copies. Difference subtracts second from first. Optionally delete inputs on success."""
        operations = {"union": 1, "difference": 2, "intersection": 3}
        if operation not in operations:
            raise ValueError("Unknown boolean operation")
        c = self.cmds
        fn = getattr(c, "polyCBoolOp", None) or c.polyBoolOp
        return self._merge_meshes(
            [first, second], name, fn, delete_inputs=delete_inputs,
            brief=brief, operation=operations[operation],
        )

    def mesh_combine(self, names: List[str], name: str, delete_inputs: bool = False,
                     brief: bool = False):
        """Combine polygon shells from copies of two or more meshes. Optionally delete inputs on success."""
        return self._merge_meshes(
            names, name, self.cmds.polyUnite, delete_inputs=delete_inputs,
            brief=brief, mergeUVSets=1,
        )

    def _merge_meshes(self, names, output, operation_fn, delete_inputs=False, brief=False, **options):
        if type(delete_inputs) is not bool:
            raise ValueError("delete_inputs must be a boolean")
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
        finally:
            self._delete_ids(ids)
        deleted = []
        if delete_inputs:
            existing = [n for n in nodes if c.objExists(n)]
            if existing:
                c.delete(existing)
                deleted = existing
        info = self.get_object_info(result, brief=brief)
        if delete_inputs:
            info = {**info, "deleted_inputs": deleted}
        return info

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

    def group_objects(self, name: str, names: List[str], brief: bool = False):
        """Create an Outliner group and reparent the specified transforms in world space."""
        self._name(name)
        nodes = [self._node(node) for node in names]
        if len(nodes) != len(set(nodes)) or len(nodes) > 128:
            raise ValueError("Use at most 128 distinct transforms")
        if any(node.startswith(other + "|") for node in nodes for other in nodes):
            raise ValueError("Group top-level members only; some listed nodes are descendants of others")
        c = self.cmds
        group = c.group(nodes, name=name, world=True) if nodes else c.group(name=name, empty=True, world=True)
        return self.get_object_info(group, brief=brief)


TOOLS = (Modeling.create_curve, Modeling.create_text, Modeling.extrude_curve, Modeling.loft_mesh,
         Modeling.mesh_extrude, Modeling.mesh_bevel, Modeling.mesh_mirror, Modeling.array_objects,
         Modeling.mesh_boolean, Modeling.mesh_combine, Modeling.mesh_separate, Modeling.group_objects)

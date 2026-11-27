import math
import os
import re
import shutil
import time
from collections import OrderedDict
from pathlib import Path
from tempfile import TemporaryDirectory

from . import images
from .baking import Baking, TOOLS as BAKING_TOOLS
from .capabilities import describe, require_plugin
from .config import _is_ascii, staging_temp_root
from .modeling import Modeling, TOOLS as MODELING_TOOLS, finite_vector
from .policy import execute_python_script, input_path, output_path, publish_file, run_commands
from .topology import Topology, TOOLS as TOPOLOGY_TOOLS
from .uv import UV, TOOLS as UV_TOOLS, current_uv_set

SAFE_CMDS_ALLOWLIST = (
    # Query / inspection
    "ls", "listRelatives", "objExists", "nodeType", "polyEvaluate", "polyInfo",
    "exactWorldBoundingBox", "getAttr", "listAttr", "listConnections", "currentTime",
    "pluginInfo", "about", "undoInfo",
    # Primitives & polygon editing
    "polyCube", "polySphere", "polyCylinder", "polyPlane", "polyTorus", "polyCone",
    "polyPipe", "polyPyramid", "polyHelix", "polyExtrudeFacet", "polyExtrudeEdge",
    "polyBevel3", "polyBevel", "polyCBoolOp", "polyBoolOp", "polyUnite", "polySeparate",
    "polySmooth", "polyMergeVertex", "polySoftEdge", "polyNormal", "polyTriangulate",
    "polyQuad", "polyMirrorFace", "polyUVSet", "polyProjection", "polyAutoProjection",
    "polyEditUV",
    # Curves, surfaces & text
    "curve", "closeCurve", "textCurves", "bevelPlus", "planarSrf", "extrude",
    "revolve", "loft", "reverseCurve", "nurbsToPoly",
    # Scene organization & transforms
    "xform", "move", "rotate", "scale", "makeIdentity", "parent", "group", "ungroup",
    "rename", "duplicate", "delete", "select", "setAttr",
    # Shading, display layers & plugins
    "shadingNode", "sets", "connectAttr", "createDisplayLayer", "editDisplayLayerMembers",
    "loadPlugin",
)

MAX_CHECKPOINTS = 20
_CHECKPOINTS: dict[tuple[int, str], OrderedDict[str, dict]] = {}


def _checkpoint_store(root: Path) -> OrderedDict[str, dict]:
    key = (os.getpid(), str(Path(root).resolve()))
    store = _CHECKPOINTS.get(key)
    if store is None:
        store = OrderedDict()
        _CHECKPOINTS[key] = store
    return store


def _checkpoint_dir(root: Path) -> Path:
    folder = staging_temp_root(root) / "checkpoints" / f"pid_{os.getpid()}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


class MayaHandlers(Modeling, Topology, UV, Baking):
    def __init__(self, cmds, settings):
        self.cmds = cmds
        self.settings = settings

    def commands(self):
        commands = {method.__name__: method for method in (
            self.get_maya_info, self.get_scene_info, self.get_object_info,
            self.create_object, self.modify_object, self.delete_object,
            self.set_material, self.create_layer, self.assign_layer,
            self.render_view, self.capture_viewport, self.uv_snapshot,
            self.save_scene, self.export_scene, self.import_asset,
            self.execute_maya_code, self.execute_script,
            self.scene_checkpoint, self.undo_scene)}
        commands.update({tool.__name__: getattr(self, tool.__name__)
                         for tool in (*MODELING_TOOLS, *TOPOLOGY_TOOLS, *UV_TOOLS, *BAKING_TOOLS)})
        return commands

    def get_maya_info(self):
        c = self.cmds
        undo_enabled = None
        if callable(getattr(c, "undoInfo", None)):
            try:
                undo_enabled = bool(c.undoInfo(query=True, state=True))
            except Exception:
                undo_enabled = None
        return {
            "version": c.about(version=True),
            "api_version": c.about(apiVersion=True),
            "mode": "headless" if c.about(batch=True) else "gui",
            "pid": os.getpid(),
            "scene": c.file(query=True, sceneName=True),
            "project_root": str(self.settings.root),
            "allowed_roots": [str(p) for p in self.settings.allowed_roots],
            "allow_external_paths": self.settings.allow_external_paths,
            "allow_code": self.settings.allow_code,
            "allow_script": self.settings.allow_script,
            "undo_enabled": undo_enabled,
            "plugins": c.pluginInfo(query=True, listPlugins=True) or [],
            **describe(c),
        }

    def get_scene_info(self, brief=False):
        if type(brief) is not bool:
            raise ValueError("brief must be a boolean")
        c = self.cmds
        nodes = c.ls(dag=True, long=True, type="transform") or []
        cam_shapes = c.ls(type="camera", long=True) or []
        cameras = set(c.listRelatives(cam_shapes, parent=True, fullPath=True) or []) if cam_shapes else set()
        meshes = c.ls(type="mesh", noIntermediate=True, long=True) or []
        scene_objects = [node for node in nodes if node not in cameras]
        mesh_count = len(meshes)
        vertices = sum(c.polyEvaluate(mesh, vertex=True) for mesh in meshes)
        triangles = sum(c.polyEvaluate(mesh, triangle=True) for mesh in meshes)
        if brief:
            return {
                "mode": "headless" if c.about(batch=True) else "gui",
                "version": c.about(version=True),
                "scene": c.file(query=True, sceneName=True),
                "object_count": len(scene_objects),
                "objects": scene_objects,
                "layer_names": c.ls(type="displayLayer") or [],
                "mesh_count": mesh_count,
                "vertices": vertices,
                "triangles": triangles,
            }
        return {
            **self.get_maya_info(),
            "selection": c.ls(selection=True, long=True) or [],
            "objects": [{"path": node, "parent": node.rsplit("|", 1)[0] or None}
                        for node in scene_objects],
            "layers": {layer: c.editDisplayLayerMembers(layer, query=True, fullNames=True) or []
                       for layer in c.ls(type="displayLayer") or []},
            "mesh_count": mesh_count,
            "vertices": vertices,
            "triangles": triangles,
        }

    def _node(self, name, node_type="transform"):
        if not isinstance(name, str) or any(char in name for char in "*?[]"):
            raise ValueError("Use an exact Maya node name or full DAG path")
        matches = self.cmds.ls(name, long=True, type=node_type) or []
        if len(matches) != 1:
            raise ValueError(f"Expected one {node_type} named {name!r}, found {len(matches)}")
        return matches[0]

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("Use an ASCII Maya name containing letters, digits and underscores")
        return name

    def get_object_info(self, name, brief=False):
        if type(brief) is not bool:
            raise ValueError("brief must be a boolean")
        c = self.cmds
        node = self._node(name)
        shapes = c.listRelatives(node, shapes=True, noIntermediate=True, fullPath=True) or []
        meshes = [shape for shape in shapes if c.nodeType(shape) == "mesh"]
        vertices = sum(c.polyEvaluate(mesh, vertex=True) for mesh in meshes)
        faces = sum(c.polyEvaluate(mesh, face=True) for mesh in meshes)
        if brief:
            return {
                "name": node,
                "bbox": c.exactWorldBoundingBox(node),
                "vertices": vertices,
                "faces": faces,
            }
        return {
            "name": node,
            "shapes": shapes,
            "children": c.listRelatives(node, children=True, type="transform", fullPath=True) or [],
            "translation": c.xform(node, query=True, translation=True, worldSpace=True),
            "rotation": c.xform(node, query=True, rotation=True, worldSpace=True),
            "scale": c.xform(node, query=True, scale=True, relative=True),
            "visible": c.getAttr(f"{node}.visibility"),
            "bbox": c.exactWorldBoundingBox(node),
            "vertices": vertices,
            "faces": faces,
            "triangles": sum(c.polyEvaluate(mesh, triangle=True) for mesh in meshes),
            "uv_sets": sorted({uv for mesh in meshes for uv in
                               (c.polyUVSet(mesh, query=True, allUVSets=True) or [])}),
            "materials": sorted({material for mesh in meshes
                                 for engine in c.listConnections(mesh, type="shadingEngine") or []
                                 for material in c.listConnections(engine + ".surfaceShader") or []}),
        }

    def _freeze_scale(self, node):
        if callable(getattr(self.cmds, "makeIdentity", None)):
            self.cmds.makeIdentity(node, apply=True, scale=True)

    def create_object(self, kind, name, size=(1, 1, 1), segments=16,
                      translation=(0, 0, 0), rotation=(0, 0, 0), parent=None, layer=None,
                      radius=None, bevel_segments=3, brief=False):
        c = self.cmds
        self._name(name)
        parent = self._node(parent) if parent else None
        layer = self._node(layer, "displayLayer") if layer else None
        if any(v <= 0 for v in finite_vector(size)) or type(segments) is not int or not 3 <= segments <= 256:
            raise ValueError("Provide three positive dimensions and 3–256 segments")
        if radius is not None and (type(radius) not in (int, float) or not math.isfinite(radius) or radius <= 0):
            raise ValueError("radius must be a finite positive number")
        if type(bevel_segments) is not int or not 1 <= bevel_segments <= 16:
            raise ValueError("bevel_segments must be an integer in 1–16")
        finite_vector(translation)
        finite_vector(rotation)
        w, h, d = size

        if kind == "cube":
            node = c.polyCube(name=name, width=w, height=h, depth=d, constructionHistory=False)[0]
        elif kind == "sphere":
            node = c.polySphere(name=name, radius=0.5, subdivisionsX=segments,
                                subdivisionsY=segments, constructionHistory=False)[0]
            c.scale(w, h, d, node)
        elif kind == "cylinder":
            node = c.polyCylinder(name=name, radius=0.5, height=h, subdivisionsX=segments,
                                  constructionHistory=False)[0]
            c.scale(w, 1, d, node)
        elif kind == "plane":
            node = c.polyPlane(name=name, width=w, height=d, subdivisionsX=1, subdivisionsY=1,
                               constructionHistory=False)[0]
        elif kind == "torus":
            outer_r = 0.5 * min(w, d)
            section_r = float(radius) if radius is not None else min(h * 0.5, outer_r * 0.5)
            if not 0 < section_r < outer_r:
                raise ValueError(f"torus radius ({section_r}) must be strictly between 0 and min(width, depth)/2 ({outer_r})")
            major_r = outer_r - section_r
            node = c.polyTorus(name=name, radius=major_r, sectionRadius=section_r,
                               subdivisionsAxis=segments, subdivisionsHeight=max(6, segments // 2),
                               constructionHistory=False)[0]
            sx = w / (2.0 * outer_r)
            sy = h / (2.0 * section_r)
            sz = d / (2.0 * outer_r)
            if any(abs(s - 1.0) > 1e-6 for s in (sx, sy, sz)):
                c.scale(sx, sy, sz, node)
                self._freeze_scale(node)
        elif kind == "rounded_box":
            bevel_r = float(radius) if radius is not None else min(w, h, d) * 0.15
            max_r = min(w, h, d) * 0.5
            if not 0 < bevel_r < max_r:
                raise ValueError(f"rounded_box radius ({bevel_r}) must be strictly between 0 and min(size)/2 ({max_r})")
            node = c.polyCube(name=name, width=w, height=h, depth=d, constructionHistory=False)[0]
            try:
                c.polyBevel3(node, offset=bevel_r, offsetAsFraction=False,
                             segments=bevel_segments, chamfer=True, constructionHistory=False)
            except Exception:
                if c.objExists(node):
                    c.delete(node)
                raise
        elif kind == "capsule":
            base_r = 0.5 * min(w, d)
            cap_subs = max(2, bevel_segments)
            if h >= 2.0 * base_r:
                node = c.polyCylinder(
                    name=name, radius=base_r, height=h, subdivisionsAxis=segments,
                    subdivisionsHeight=1, subdivisionsCaps=cap_subs,
                    roundCap=True, roundCapHeightCompensation=True,
                    constructionHistory=False,
                )[0]
                sx = w / (2.0 * base_r)
                sz = d / (2.0 * base_r)
                if abs(sx - 1.0) > 1e-6 or abs(sz - 1.0) > 1e-6:
                    c.scale(sx, 1.0, sz, node)
                    self._freeze_scale(node)
            else:
                node = c.polyCylinder(
                    name=name, radius=0.5, height=1.0, subdivisionsAxis=segments,
                    subdivisionsHeight=1, subdivisionsCaps=cap_subs,
                    roundCap=True, roundCapHeightCompensation=True,
                    constructionHistory=False,
                )[0]
                c.scale(w, h, d, node)
                self._freeze_scale(node)
        elif kind == "cone":
            node = c.polyCone(name=name, radius=0.5, height=h, subdivisionsAxis=segments,
                              constructionHistory=False)[0]
            if abs(w - 1.0) > 1e-6 or abs(d - 1.0) > 1e-6:
                c.scale(w, 1.0, d, node)
                self._freeze_scale(node)
        elif kind == "pipe":
            outer_r = 0.5 * min(w, d)
            thickness = float(radius) if radius is not None else outer_r * 0.35
            if not 0 < thickness < outer_r:
                raise ValueError(f"pipe thickness/radius ({thickness}) must be strictly between 0 and min(width, depth)/2 ({outer_r})")
            # Maya polyPipe height is half the specified height parameter.
            node = c.polyPipe(name=name, radius=outer_r, height=h * 2.0, thickness=thickness,
                              subdivisionsAxis=segments, constructionHistory=False)[0]
            sx = w / (2.0 * outer_r)
            sz = d / (2.0 * outer_r)
            if abs(sx - 1.0) > 1e-6 or abs(sz - 1.0) > 1e-6:
                c.scale(sx, 1.0, sz, node)
                self._freeze_scale(node)
        else:
            raise ValueError(
                "kind must be cube, sphere, cylinder, plane, torus, rounded_box, capsule, cone or pipe"
            )
        try:
            return self.modify_object(node, translation=translation, rotation=rotation,
                                      parent=parent, layer=layer, brief=brief)
        except Exception:
            if c.objExists(node):
                c.delete(node)
            raise

    def modify_object(self, name, new_name=None, translation=None, rotation=None,
                      scale=None, visible=None, parent=None, layer=None, brief=False):
        c = self.cmds
        node = self._node(name)
        target_parent = self._node(parent) if parent else None
        target_layer = self._node(layer, "displayLayer") if layer else None
        if new_name is not None:
            self._name(new_name)
        for vector in (translation, rotation, scale):
            if vector is not None:
                finite_vector(vector)
        if target_parent:
            if target_parent == node or target_parent.startswith(node + "|"):
                raise ValueError("Cannot parent a transform to itself or one of its descendants")
            if node.rsplit("|", 1)[0] != target_parent:
                identifier = c.ls(node, uuid=True)[0]
                c.parent(node, target_parent)
                node = c.ls(identifier, long=True)[0]
        if new_name is not None:
            renamed = c.rename(node, new_name)
            parent_path = node.rsplit("|", 1)[0]
            node = self._node(parent_path + "|" + renamed.rsplit("|", 1)[-1])
        if translation is not None:
            c.xform(node, worldSpace=True, translation=translation)
        if rotation is not None:
            c.xform(node, worldSpace=True, rotation=rotation)
        if scale is not None:
            c.xform(node, objectSpace=True, scale=scale)
        if visible is not None:
            c.setAttr(node + ".visibility", bool(visible))
        if target_layer:
            c.editDisplayLayerMembers(target_layer, node)
        return self.get_object_info(node, brief=brief)

    def delete_object(self, names):
        targets = list(dict.fromkeys(self._node(name) for name in names))
        top_level = [node for node in targets
                     if not any(node.startswith(other + "|") for other in targets if other != node)]
        if top_level:
            self.cmds.delete(top_level)
        return {"deleted": targets}

    def set_material(self, name=None, material="", color=None, roughness=None,
                     names=None, reuse=True, shader_type="blinn"):
        c = self.cmds
        raw_targets = []
        if name is not None:
            if isinstance(name, (list, tuple)):
                raw_targets.extend(name)
            else:
                raw_targets.append(name)
        if names is not None:
            raw_targets.extend(names)
        if not raw_targets:
            raise ValueError("Provide name or names for at least one target object")
        nodes = list(dict.fromkeys(self._node(target) for target in raw_targets))
        if len(nodes) > 512:
            raise ValueError("Assign at most 512 objects per set_material call")
        self._name(material)
        if type(reuse) is not bool:
            raise ValueError("reuse must be a boolean")
        if shader_type not in {"blinn", "lambert", "phong", "standardSurface"}:
            raise ValueError("shader_type must be blinn, lambert, phong or standardSurface")
        if color is not None:
            finite_vector(color)
            if any(not 0 <= v <= 1 for v in color):
                raise ValueError("RGB channels must be between 0 and 1")
        if roughness is not None and (
            type(roughness) not in (int, float)
            or not math.isfinite(roughness)
            or not 0 <= roughness <= 1
        ):
            raise ValueError("roughness must be between 0 and 1")

        def _apply_params(shader_node, actual_type, col, rough):
            if col is not None:
                attr = ".baseColor" if actual_type == "standardSurface" else ".color"
                c.setAttr(shader_node + attr, *col, type="double3")
            if rough is not None:
                if actual_type == "standardSurface":
                    c.setAttr(shader_node + ".specularRoughness", rough)
                elif actual_type == "blinn":
                    c.setAttr(shader_node + ".eccentricity", rough)
                elif actual_type == "phong":
                    # Map roughness [0, 1] -> cosinePower [100, 2]
                    c.setAttr(shader_node + ".cosinePower", max(2.0, 2.0 + (1.0 - rough) * 98.0))

        reused = False
        shader = None
        engine = None
        created_nodes = []
        try:
            if reuse and c.objExists(material):
                existing_type = c.nodeType(material) if callable(getattr(c, "nodeType", None)) else shader_type
                shader_types = {"blinn", "lambert", "phong", "phongE", "standardSurface",
                                "aiStandardSurface", "surfaceShader", "anisotropic", "openPBRSurface"}
                if existing_type not in shader_types:
                    raise ValueError(
                        f"Existing node {material!r} has type {existing_type!r}, not a surface shader; "
                        "choose a different material name or pass reuse=False"
                    )
                shader = material
                reused = True
                actual_type = existing_type
                engines = c.listConnections(shader + ".outColor", type="shadingEngine") or []
                if engines:
                    engine = engines[0]
                else:
                    engine = c.sets(renderable=True, noSurfaceShader=True, empty=True, name=shader + "SG")
                    created_nodes.append(engine)
                    c.connectAttr(shader + ".outColor", engine + ".surfaceShader", force=True)
                _apply_params(shader, actual_type, color, roughness)
            else:
                actual_type = shader_type
                shader = c.shadingNode(shader_type, asShader=True, name=material)
                created_nodes.append(shader)
                engine = c.sets(renderable=True, noSurfaceShader=True, empty=True, name=shader + "SG")
                created_nodes.append(engine)
                c.connectAttr(shader + ".outColor", engine + ".surfaceShader", force=True)
                _apply_params(
                    shader, actual_type,
                    (0.5, 0.5, 0.5) if color is None else color,
                    0.5 if roughness is None else roughness,
                )
            c.sets(nodes, edit=True, forceElement=engine)
        except Exception:
            for created in reversed(created_nodes):
                if created and c.objExists(created):
                    c.delete(created)
            raise
        return {
            "name": nodes[0],
            "names": nodes,
            "material": shader,
            "shading_group": engine,
            "shader_type": actual_type,
            "reused": reused,
        }

    def create_layer(self, name, names=(), visible=True, color=0):
        self._name(name)
        if type(color) is not int or not 0 <= color <= 31:
            raise ValueError("Display layer color must be between 0 and 31")
        nodes = [self._node(node) for node in names]
        c = self.cmds
        layer = c.createDisplayLayer(name=name, empty=True)
        if nodes:
            c.editDisplayLayerMembers(layer, nodes)
        c.setAttr(layer + ".visibility", bool(visible))
        c.setAttr(layer + ".color", color)
        return {"layer": layer, "members": nodes}

    def assign_layer(self, name, names):
        layer = self._node(name, "displayLayer")
        nodes = [self._node(node) for node in names]
        if nodes:
            self.cmds.editDisplayLayerMembers(layer, nodes)
        return {"layer": layer, "members": nodes}

    def _output(self, path, extensions, overwrite, create_parent=True, allow_outside_root=False):
        return output_path(
            self.settings.root, path, extensions, overwrite, create_parent=create_parent,
            allowed_roots=self.settings.allowed_roots,
            allow_external=self.settings.allow_external_paths,
            allow_outside_root=allow_outside_root,
        )

    def _input(self, path, extensions, allow_outside_root=False):
        return input_path(
            self.settings.root, path, extensions,
            allowed_roots=self.settings.allowed_roots,
            allow_external=self.settings.allow_external_paths,
            allow_outside_root=allow_outside_root,
        )

    def save_scene(self, path, overwrite=False, allow_outside_root=False):
        target = self._output(path, {".ma", ".mb"}, overwrite, create_parent=False,
                              allow_outside_root=allow_outside_root)
        c = self.cmds
        ext = target.suffix.lower()
        file_type = {".ma": "mayaAscii", ".mb": "mayaBinary"}[ext]
        previous = c.file(query=True, sceneName=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="save-", dir=images._temp_base(target, root=self.settings.root)) as folder:
            staged = Path(folder) / f"scene{ext}"
            c.file(rename=staged.as_posix())
            try:
                c.file(save=True, force=True, type=file_type)
                publish_file(staged, target, overwrite=overwrite)
            except Exception:
                c.file(rename=previous or "untitled")
                raise
        try:
            c.file(rename=target.as_posix())
        except Exception:
            c.file(rename=previous or "untitled")
        return written(target)

    def export_scene(self, path, names=(), overwrite=False, allow_outside_root=False):
        target = self._output(path, {".fbx", ".obj"}, overwrite, create_parent=False,
                              allow_outside_root=allow_outside_root)
        nodes = [self._node(name) for name in names]
        c = self.cmds
        ext = target.suffix.lower()
        plugin, file_type = (("fbxmaya", "FBX export") if ext == ".fbx"
                             else ("objExport", "OBJexport"))
        require_plugin(c, plugin)
        target.parent.mkdir(parents=True, exist_ok=True)
        selection = c.ls(selection=True, long=True) or []
        try:
            if nodes:
                c.select(nodes, replace=True)
            options = {"options": "groups=1;ptgroups=1;materials=0;smoothing=1;normals=1"} if plugin == "objExport" else {}
            with TemporaryDirectory(prefix="export-", dir=images._temp_base(target, root=self.settings.root)) as folder:
                staged = Path(folder) / f"export{ext}"
                c.file(staged.as_posix(), force=True, type=file_type, **options,
                       exportSelected=bool(nodes), exportAll=not nodes)
                if not staged.is_file() or not staged.stat().st_size:
                    raise RuntimeError(f"Maya did not produce a nonempty {ext} export")
                publish_file(staged, target, overwrite=overwrite)
            return {**written(target), "format": ext.lstrip("."), "plugin": plugin}
        finally:
            c.select(selection, replace=True) if selection else c.select(clear=True)

    def import_asset(self, path, namespace=None, group_name=None,
                     allow_outside_root=False, brief=False):
        if type(brief) is not bool:
            raise ValueError("brief must be a boolean")
        source = self._input(path, {".obj", ".fbx", ".ma", ".mb"},
                             allow_outside_root=allow_outside_root)
        if namespace is not None:
            self._name(namespace)
        if group_name is not None:
            self._name(group_name)
        c = self.cmds
        ext = source.suffix.lower()
        plugin_map = {
            ".fbx": ("fbxmaya", "FBX"),
            ".obj": ("objExport", "OBJ"),
            ".ma": (None, "mayaAscii"),
            ".mb": (None, "mayaBinary"),
        }
        plugin, file_type = plugin_map[ext]
        if plugin:
            require_plugin(c, plugin)

        before = set(c.ls(assemblies=True, long=True) or [])
        cam_shapes_before = set(c.ls(type="camera", long=True) or [])
        with TemporaryDirectory(prefix="import-", dir=staging_temp_root(self.settings.root)) as folder:
            if _is_ascii(str(source)):
                import_src = source
            else:
                import_src = Path(folder) / f"asset{ext}"
                shutil.copy2(source, import_src)
                if ext == ".obj":
                    mtl = source.with_suffix(".mtl")
                    if mtl.is_file():
                        shutil.copy2(mtl, Path(folder) / mtl.name)
            kwargs = {"i": True, "type": file_type, "ignoreVersion": True, "returnNewNodes": True}
            if namespace is not None:
                kwargs["namespace"] = namespace
            c.file(import_src.as_posix(), **kwargs)

        after = set(c.ls(assemblies=True, long=True) or [])
        cam_shapes_after = set(c.ls(type="camera", long=True) or [])
        default_cams = set(c.listRelatives(list(cam_shapes_before), parent=True, fullPath=True) or []) if cam_shapes_before else set()
        new_assemblies = sorted(
            node for node in (after - before)
            if node not in default_cams
        )
        group_path = None
        if group_name is not None:
            group = (c.group(new_assemblies, name=group_name, world=True)
                     if new_assemblies else c.group(name=group_name, empty=True, world=True))
            group_path = self._node(group)
            imported_nodes = [group_path]
        else:
            imported_nodes = new_assemblies

        objects = (
            imported_nodes if brief
            else [self.get_object_info(node) for node in imported_nodes]
        )
        return {
            "path": str(source),
            "format": ext.lstrip("."),
            "plugin": plugin,
            "group": group_path,
            "count": len(new_assemblies),
            "objects": objects,
            "new_cameras": sorted(cam_shapes_after - cam_shapes_before),
        }

    def _capture_shapes(self, names):
        return [self._mesh(name)[1] for name in self._mesh_nodes(names)] if names else []

    def render_view(self, path, camera="front", width=800, height=800, overwrite=False,
                    names=(), frame_names=(), renderer="mayaSoftware",
                    fallback_to_software=False, allow_outside_root=False):
        images.dimensions(width, height)
        if renderer not in images.RENDERERS:
            raise ValueError(f"Unsupported renderer {renderer!r}; choose one of {sorted(images.RENDERERS)}")
        target = self._output(path, {".png"}, overwrite, create_parent=False,
                              allow_outside_root=allow_outside_root)
        shapes, frame = self._capture_shapes(names), self._capture_shapes(frame_names)
        with images.isolate_meshes(self.cmds, shapes):
            target.parent.mkdir(parents=True, exist_ok=True)
            return images.render_view(
                self.cmds, target, camera, width, height,
                self.settings.render_timeout, self.settings.mayapy, overwrite, frame,
                renderer=renderer, fallback_to_software=fallback_to_software,
                root=self.settings.root,
            )

    def capture_viewport(self, path, camera="front", width=800, height=800, overwrite=False,
                         names=(), frame_names=(), allow_fallback=True, allow_outside_root=False):
        if type(allow_fallback) is not bool:
            raise ValueError("allow_fallback must be a boolean")
        c = self.cmds
        if c.about(batch=True):
            if not allow_fallback:
                raise ValueError(
                    "GUI viewport capture is unavailable in headless/batch Maya; "
                    "connect to Maya GUI mode or pass allow_fallback=True to render with Maya Software."
                )
            result = self.render_view(
                path, camera, width, height, overwrite, names, frame_names,
                allow_outside_root=allow_outside_root,
            )
            return {
                **result,
                "mode": "headless",
                "is_gui_viewport": False,
                "requested_source": "mayaViewport",
                "source": "mayaSoftware",
                "fallback": "render",
                "fallback_reason": "Maya is running in headless/batch mode without a GUI OpenGL/Viewport 2.0 modelPanel.",
                "warnings": [
                    "GUI viewport unavailable in headless Maya; returned a Maya Software render fallback "
                    "(shading/lighting/wireframe differs from Viewport 2.0)."
                ],
            }
        images.dimensions(width, height)
        output = self._output(path, {".png"}, overwrite, create_parent=False,
                              allow_outside_root=allow_outside_root)
        shapes, frame = self._capture_shapes(names), self._capture_shapes(frame_names)
        with images.isolate_meshes(c, shapes):
            output.parent.mkdir(parents=True, exist_ok=True)
            return images.capture_viewport(
                c, output, camera, width, height, overwrite, frame, root=self.settings.root
            )

    def uv_snapshot(self, path, names, size=1024, overwrite=False, uv_set=None,
                    bounds=None, allow_outside_root=False):
        images.dimensions(size, size)
        images.validate_uv_bounds(bounds)
        target = self._output(path, {".png"}, overwrite, create_parent=False,
                              allow_outside_root=allow_outside_root)
        nodes = self._mesh_nodes(names)
        if uv_set is not None:
            for node in nodes:
                current_uv_set(self.cmds, self._mesh(node)[1], uv_set)
        target.parent.mkdir(parents=True, exist_ok=True)
        return images.uv_snapshot(
            self.cmds, target, nodes, size, uv_set, bounds, overwrite, root=self.settings.root
        )

    def execute_maya_code(self, code):
        if not self.settings.allow_code:
            raise ValueError("execute_maya_code is disabled by MAYA_MCP_ALLOW_CODE")
        c = self.cmds
        allowed = {name: getattr(c, name) for name in SAFE_CMDS_ALLOWLIST
                   if callable(getattr(c, name, None))}
        return {"results": run_commands(code, allowed)}

    def execute_script(self, code, timeout=None, timeout_seconds=None):
        if not self.settings.allow_script:
            raise ValueError(
                "execute_script is disabled by MAYA_MCP_ALLOW_SCRIPT "
                "(set MAYA_MCP_ALLOW_SCRIPT=1 or pass --allow-script to enable full Maya Python scripts)"
            )
        requested = timeout_seconds if timeout_seconds is not None else timeout
        max_timeout = min(300.0, float(self.settings.render_timeout))
        effective_timeout = min(float(self.settings.timeout), 60.0) if requested is None else float(requested)
        if not math.isfinite(effective_timeout) or not 0 < effective_timeout <= max_timeout:
            raise ValueError(f"timeout must be in (0, {max_timeout}] seconds")

        namespace = {
            "cmds": self.cmds,
            "math": math,
            "Path": Path,
            "PROJECT_ROOT": str(self.settings.root),
        }
        try:
            import maya
            namespace["maya"] = maya
        except Exception:
            pass
        try:
            from maya import mel
            namespace["mel"] = mel
        except Exception:
            pass
        try:
            from maya.api import OpenMaya as om
            namespace["om"] = om
        except Exception:
            pass

        chunk_opened = False
        c = self.cmds
        if callable(getattr(c, "undoInfo", None)):
            try:
                if c.undoInfo(query=True, state=True):
                    c.undoInfo(openChunk=True, chunkName="mcp_execute_script")
                    chunk_opened = True
            except Exception:
                chunk_opened = False
        try:
            return execute_python_script(code, namespace, timeout=effective_timeout)
        finally:
            if chunk_opened:
                try:
                    c.undoInfo(closeChunk=True)
                except Exception:
                    pass

    def scene_checkpoint(self, action="create", name=None):
        if action not in {"create", "restore", "list", "delete"}:
            raise ValueError("action must be create, restore, list or delete")
        c = self.cmds
        store = _checkpoint_store(self.settings.root)

        if action == "list":
            return {
                "checkpoints": [
                    {
                        "name": cp_name,
                        "created_at": info["created_at"],
                        "bytes": info["bytes"],
                        "mesh_count": info["mesh_count"],
                        "object_count": info["object_count"],
                    }
                    for cp_name, info in store.items()
                ]
            }

        if action == "create":
            if name is not None:
                cp_name = self._name(name)
            else:
                idx = 1
                while f"cp_{idx:03d}" in store:
                    idx += 1
                cp_name = f"cp_{idx:03d}"
            cp_dir = _checkpoint_dir(self.settings.root)
            snap_path = cp_dir / f"{cp_name}.ma"
            previous = c.file(query=True, sceneName=True)
            c.file(rename=snap_path.as_posix())
            try:
                c.file(save=True, force=True, type="mayaAscii")
            finally:
                c.file(rename=previous or "untitled")
            if not snap_path.is_file() or not snap_path.stat().st_size:
                raise RuntimeError("Maya did not write a nonempty checkpoint snapshot")
            meshes = c.ls(type="mesh", noIntermediate=True, long=True) or []
            nodes = c.ls(dag=True, long=True, type="transform") or []
            entry = {
                "name": cp_name,
                "path": snap_path.as_posix(),
                "scene_name": previous or "",
                "created_at": round(time.time(), 3),
                "bytes": snap_path.stat().st_size,
                "mesh_count": len(meshes),
                "object_count": len(nodes),
            }
            if cp_name in store:
                store.move_to_end(cp_name)
            store[cp_name] = entry
            while len(store) > MAX_CHECKPOINTS:
                _, evicted = store.popitem(last=False)
                Path(evicted["path"]).unlink(missing_ok=True)
            return {
                "action": "create",
                "name": cp_name,
                "created_at": entry["created_at"],
                "bytes": entry["bytes"],
                "mesh_count": entry["mesh_count"],
                "object_count": entry["object_count"],
                "total_checkpoints": len(store),
            }

        if action == "restore":
            if not store:
                raise ValueError("No scene checkpoints exist to restore")
            if name is None:
                cp_name = next(reversed(store))
            else:
                cp_name = self._name(name)
                if cp_name not in store:
                    raise ValueError(f"Unknown checkpoint {cp_name!r}; available: {list(store)}")
            entry = store[cp_name]
            snap = Path(entry["path"])
            if not snap.is_file():
                raise ValueError(f"Checkpoint file for {cp_name!r} is missing")
            c.file(snap.as_posix(), open=True, force=True)
            c.file(rename=entry["scene_name"] or "untitled")
            meshes = c.ls(type="mesh", noIntermediate=True, long=True) or []
            nodes = c.ls(dag=True, long=True, type="transform") or []
            return {
                "action": "restore",
                "restored": cp_name,
                "scene": c.file(query=True, sceneName=True),
                "mesh_count": len(meshes),
                "object_count": len(nodes),
            }

        # action == "delete"
        if name is None:
            raise ValueError("Provide a checkpoint name to delete")
        cp_name = self._name(name)
        if cp_name not in store:
            raise ValueError(f"Unknown checkpoint {cp_name!r}; available: {list(store)}")
        removed = store.pop(cp_name)
        Path(removed["path"]).unlink(missing_ok=True)
        return {"action": "delete", "deleted": cp_name, "remaining": len(store)}

    def undo_scene(self, action="undo", steps=1):
        if action not in {"undo", "redo", "status"}:
            raise ValueError("action must be undo, redo or status")
        if type(steps) is not int or not 1 <= steps <= 50:
            raise ValueError("steps must be an integer in 1–50")
        c = self.cmds
        if not callable(getattr(c, "undoInfo", None)):
            raise ValueError("Maya undoInfo is unavailable in this session")
        enabled = bool(c.undoInfo(query=True, state=True))
        if action == "status":
            length = None
            try:
                length = c.undoInfo(query=True, length=True)
            except Exception:
                pass
            return {"action": "status", "enabled": enabled, "length": length}
        if not enabled:
            c.undoInfo(state=True, infinity=False, length=100)
            return {
                "action": action,
                "requested": steps,
                "performed": 0,
                "enabled": True,
                "warnings": ["Undo queue was disabled and has now been enabled for future operations."],
            }
        op = c.undo if action == "undo" else c.redo
        performed = 0
        warnings = []
        for _ in range(steps):
            try:
                op()
                performed += 1
            except RuntimeError as exc:
                warnings.append(f"Stopped after {performed} {action} step(s): {exc}")
                break
        return {
            "action": action,
            "requested": steps,
            "performed": performed,
            "enabled": True,
            "warnings": warnings,
        }


def written(path):
    path = Path(path)
    if not path.is_file() or not path.stat().st_size:
        raise RuntimeError(f"Maya did not produce a nonempty file at {path}")
    return {"path": str(path), "bytes": path.stat().st_size}

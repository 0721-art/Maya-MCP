import os
import re
from pathlib import Path

from . import images
from .policy import output_path, run_commands


class MayaHandlers:
    def __init__(self, cmds, settings):
        self.cmds = cmds
        self.settings = settings

    def commands(self):
        return {method.__name__: method for method in (
            self.get_maya_info, self.get_scene_info, self.get_object_info,
            self.create_object, self.modify_object, self.delete_object,
            self.set_material, self.create_layer, self.assign_layer,
            self.render_view, self.capture_viewport, self.uv_snapshot,
            self.save_scene, self.export_scene, self.execute_maya_code)}

    def get_maya_info(self):
        c = self.cmds
        return {"version": c.about(version=True), "api_version": c.about(apiVersion=True),
                "mode": "headless" if c.about(batch=True) else "gui", "pid": os.getpid(),
                "scene": c.file(query=True, sceneName=True),
                "plugins": c.pluginInfo(query=True, listPlugins=True) or []}

    def get_scene_info(self):
        c = self.cmds
        nodes = c.ls(dag=True, long=True, type="transform") or []
        cameras = set(c.listCameras() or [])
        meshes = c.ls(type="mesh", noIntermediate=True, long=True) or []
        return {**self.get_maya_info(), "selection": c.ls(selection=True, long=True) or [],
                "objects": [{"path": node, "parent": node.rsplit("|", 1)[0] or None}
                            for node in nodes if node.rsplit("|", 1)[-1] not in cameras],
                "layers": {layer: c.editDisplayLayerMembers(layer, query=True, fullNames=True) or []
                           for layer in c.ls(type="displayLayer") or []},
                "mesh_count": len(meshes),
                "vertices": sum(c.polyEvaluate(mesh, vertex=True) for mesh in meshes),
                "triangles": sum(c.polyEvaluate(mesh, triangle=True) for mesh in meshes)}

    def _node(self, name, node_type="transform"):
        if not isinstance(name, str) or any(char in name for char in "*?[]"):
            raise ValueError("Use an exact Maya node name or full DAG path")
        matches = self.cmds.ls(name, long=True, type=node_type) or []
        if len(matches) != 1:
            raise ValueError(f"Expected one {node_type} named {name!r}, found {len(matches)}")
        return matches[0]

    @staticmethod
    def _name(name):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("Use an ASCII Maya name containing letters, digits and underscores")
        return name

    def get_object_info(self, name):
        c = self.cmds
        node = self._node(name)
        shapes = c.listRelatives(node, shapes=True, noIntermediate=True, fullPath=True) or []
        meshes = [shape for shape in shapes if c.nodeType(shape) == "mesh"]
        return {"name": node, "shapes": shapes,
                "children": c.listRelatives(node, children=True, type="transform", fullPath=True) or [],
                "translation": c.xform(node, query=True, translation=True, worldSpace=True),
                "rotation": c.xform(node, query=True, rotation=True, worldSpace=True),
                "scale": c.xform(node, query=True, scale=True, relative=True),
                "visible": c.getAttr(f"{node}.visibility"), "bbox": c.exactWorldBoundingBox(node),
                "vertices": sum(c.polyEvaluate(mesh, vertex=True) for mesh in meshes),
                "faces": sum(c.polyEvaluate(mesh, face=True) for mesh in meshes),
                "triangles": sum(c.polyEvaluate(mesh, triangle=True) for mesh in meshes),
                "uv_sets": sorted({uv for mesh in meshes for uv in
                                   (c.polyUVSet(mesh, query=True, allUVSets=True) or [])}),
                "materials": sorted({material for mesh in meshes
                                     for engine in c.listConnections(mesh, type="shadingEngine") or []
                                     for material in c.listConnections(engine + ".surfaceShader") or []})}

    def create_object(self, kind, name, size=(1, 1, 1), segments=16,
                      translation=(0, 0, 0), rotation=(0, 0, 0), parent=None, layer=None):
        c = self.cmds
        self._name(name)
        parent = self._node(parent) if parent else None
        layer = self._node(layer, "displayLayer") if layer else None
        if len(size) != 3 or any(v <= 0 for v in size) or not 3 <= segments <= 256:
            raise ValueError("Provide three positive dimensions and 3–256 segments")
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
        else:
            raise ValueError("kind must be cube, sphere, cylinder or plane")
        try:
            return self.modify_object(node, translation=translation, rotation=rotation,
                                      parent=parent, layer=layer)
        except Exception:
            if c.objExists(node):
                c.delete(node)
            raise

    def modify_object(self, name, new_name=None, translation=None, rotation=None,
                      scale=None, visible=None, parent=None, layer=None):
        c = self.cmds
        node = self._node(name)
        target_parent = self._node(parent) if parent else None
        target_layer = self._node(layer, "displayLayer") if layer else None
        if new_name is not None:
            self._name(new_name)
        for vector in (translation, rotation, scale):
            if vector is not None and (len(vector) != 3 or any(
                    not isinstance(v, (int, float)) for v in vector)):
                raise ValueError("Transform vectors require three numbers")
        if target_parent and node.rsplit("|", 1)[0] != target_parent:
            c.parent(node, target_parent)
            node = self._node(target_parent + "|" + node.rsplit("|", 1)[-1])
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
        return self.get_object_info(node)

    def delete_object(self, names):
        targets = [self._node(name) for name in names]
        if targets:
            self.cmds.delete(targets)
        return {"deleted": targets}

    def set_material(self, name, material, color, roughness=0.5):
        c = self.cmds
        node = self._node(name)
        self._name(material)
        if len(color) != 3 or any(not 0 <= v <= 1 for v in color) or not 0 <= roughness <= 1:
            raise ValueError("RGB channels and roughness must be between 0 and 1")
        shader = c.shadingNode("blinn", asShader=True, name=material)
        engine = c.sets(renderable=True, noSurfaceShader=True, empty=True, name=shader + "SG")
        c.connectAttr(shader + ".outColor", engine + ".surfaceShader", force=True)
        c.setAttr(shader + ".color", *color, type="double3")
        c.setAttr(shader + ".eccentricity", roughness)
        c.sets(node, edit=True, forceElement=engine)
        return {"name": node, "material": shader, "shading_group": engine}

    def create_layer(self, name, names=(), visible=True, color=0):
        self._name(name)
        if not 0 <= color <= 31:
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

    def _output(self, path, extensions, overwrite):
        return output_path(self.settings.root, path, extensions, overwrite)

    def save_scene(self, path, overwrite=False):
        target = self._output(path, {".ma", ".mb"}, overwrite)
        c = self.cmds
        previous = c.file(query=True, sceneName=True)
        c.file(rename=target.as_posix())
        try:
            c.file(save=True, force=overwrite,
                   type={".ma": "mayaAscii", ".mb": "mayaBinary"}[target.suffix.lower()])
            return written(target)
        except Exception:
            c.file(rename=previous or "untitled")
            raise

    def export_scene(self, path, names=(), overwrite=False):
        target = self._output(path, {".fbx", ".obj"}, overwrite)
        nodes = [self._node(name) for name in names]
        c = self.cmds
        plugin, file_type = (("fbxmaya", "FBX export") if target.suffix.lower() == ".fbx"
                             else ("objExport", "OBJexport"))
        c.loadPlugin(plugin, quiet=True)
        selection = c.ls(selection=True, long=True) or []
        try:
            if nodes:
                c.select(nodes, replace=True)
            c.file(target.as_posix(), force=overwrite, type=file_type,
                   exportSelected=bool(nodes), exportAll=not nodes)
            return written(target)
        finally:
            c.select(selection, replace=True) if selection else c.select(clear=True)

    def render_view(self, path, camera="front", width=800, height=800, overwrite=False):
        path = self._output(path, {".png"}, overwrite)
        return images.render_view(self.cmds, path, camera, width, height)

    def capture_viewport(self, path, camera="front", width=800, height=800, overwrite=False):
        path = self._output(path, {".png"}, overwrite)
        c = self.cmds
        if c.about(batch=True):
            return {**images.render_view(c, path, camera, width, height), "fallback": "render"}
        return images.capture_viewport(c, path, camera, width, height)

    def uv_snapshot(self, path, names, size=1024, overwrite=False):
        path = self._output(path, {".png"}, overwrite)
        nodes = [self._node(name) for name in names]
        if not nodes:
            raise ValueError("Select at least one mesh transform for UV snapshot")
        return images.uv_snapshot(self.cmds, path, nodes, size)

    def execute_maya_code(self, code):
        if not self.settings.allow_code:
            raise ValueError("execute_maya_code is disabled by MAYA_MCP_ALLOW_CODE")
        c = self.cmds
        allowlist = ("ls", "listRelatives", "objExists", "polyCube", "polySphere", "polyCylinder",
                     "polyPlane", "polyEvaluate", "xform")
        return {"results": run_commands(code, {name: getattr(c, name) for name in allowlist})}


def written(path):
    path = Path(path)
    if not path.is_file() or not path.stat().st_size:
        raise RuntimeError(f"Maya did not produce a nonempty file at {path}")
    return {"path": str(path), "bytes": path.stat().st_size}

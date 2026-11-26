"""Reusable assignment configuration, expected deliverables, and acceptance checks."""

import json
import re
from pathlib import Path
from string import Formatter
from tempfile import TemporaryDirectory
from typing import Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from .policy import output_path, project_path, publish_file

GROUPS = ("deliverables", "renders", "exports", "docs")
Camera = Literal["front", "side", "back", "persp", "top", "left"]
BakeMap = Literal["normal", "color", "ao", "curvature"]


class AssignmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=180)
    work_name: str = Field(min_length=1, max_length=80)
    output_dir: str = "assignment"
    student_id: str | None = Field(default=None, max_length=50)
    student_name: str | None = Field(default=None, max_length=50)
    model_style: str | None = Field(default=None, max_length=180)
    requirements: list[str] = Field(default_factory=list, max_length=40)
    reference_images: list[str] = Field(default_factory=list, max_length=32)
    require_references: bool = True
    require_identity: bool = True
    naming_pattern: str = Field(default="{student_id}-{student_name}-{work_name}", max_length=200)
    scene_format: Literal["ma", "mb"] = "mb"
    separate_low_scene: bool = False
    export_formats: list[Literal["fbx", "obj"]] = Field(default_factory=lambda: ["fbx"], max_length=2)
    bake_maps: list[BakeMap] = Field(default_factory=lambda: ["normal", "ao", "curvature", "color"], max_length=4)
    views: list[Camera] = Field(default_factory=lambda: ["front", "side", "back"], max_length=6)
    comparison_views: list[Camera] = Field(default_factory=list, max_length=6)
    uv_snapshot: bool = True
    max_low_faces: int | None = Field(default=None, ge=4)
    require_quads: bool = False


def load_config(root, config=None, config_path=None):
    if (config is None) == (config_path is None):
        raise ValueError("Provide exactly one of config or config_path")
    if config is not None:
        return config
    source = project_path(root, config_path)
    if source.suffix.lower() != ".json" or source.stat().st_size > 256_000:
        raise ValueError("Assignment config must be a JSON file smaller than 256 KB")
    return AssignmentConfig.model_validate_json(source.read_text(encoding="utf-8-sig"))


def _filename(config, missing):
    fields = dict(student_id=config.student_id, student_name=config.student_name, work_name=config.work_name)
    for key, value in fields.items():
        fields[key] = (value or "").strip() or f"MISSING-{key.replace('_', '-').upper()}"
    if not config.work_name.strip() or not config.title.strip():
        raise ValueError("title and work_name cannot be blank")
    for _, name, format_spec, conversion in Formatter().parse(config.naming_pattern):
        if name is not None and (name not in fields or format_spec or conversion):
            raise ValueError("Naming fields are student_id, student_name and work_name, without formats or conversions")
    stem = config.naming_pattern.format_map(fields)
    if (not stem.strip() or len(stem) > 160 or re.search(r'[<>:"/\\|?*\x00-\x1f]', stem) or
            stem.endswith((" ", ".")) or
            stem.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                                           *(f"LPT{i}" for i in range(1, 10))}):
        raise ValueError("Naming pattern produces an invalid or reserved filename")
    for key in ("student_id", "student_name"):
        if not (getattr(config, key) or "").strip():
            missing.append(key)
    return stem


def _image_status(root, name):
    path = project_path(root, name)
    if not path.is_file():
        return "missing"
    try:
        with Image.open(path) as image:
            image.verify()
    except (OSError, SyntaxError, Image.DecompressionBombError):
        return "invalid"
    return "present"


def plan_assignment(root, config):
    for items in (config.export_formats, config.bake_maps, config.views, config.comparison_views):
        if len(items) != len(set(items)):
            raise ValueError("Do not repeat formats, maps or views")
    missing = []
    stem = _filename(config, missing)
    if not config.reference_images:
        missing.append("reference_images")
    references = [{"path": name, "status": _image_status(root, name)} for name in config.reference_images]
    if any(item["status"] != "present" for item in references):
        missing.append("reference_images")
    groups = {name: [] for name in GROUPS}

    def add(group, filename, stage, tool):
        absolute = project_path(root, Path(config.output_dir) / group / filename)
        relative = absolute.relative_to(root.resolve()).as_posix()
        groups[group].append({"path": relative, "stage": stage, "tool": tool})
        return relative

    add("deliverables", f"{stem}.{config.scene_format}", "mid_model", "save_scene")
    if config.separate_low_scene:
        add("deliverables", f"{stem}-low.{config.scene_format}", "low_model", "save_scene")
    if config.uv_snapshot:
        add("renders", f"{stem}-uv.png", "uv", "uv_snapshot")
    for kind in config.bake_maps:
        add("deliverables", f"{stem}-{kind}.png", "bake", "bake_map")
    if config.views:
        add("renders", f"{stem}-views.png", "capture", "compose_sheet")
    if config.comparison_views:
        add("renders", f"{stem}-comparison.png", "capture", "compose_sheet")
    for fmt in config.export_formats:
        add("exports", f"{stem}.{fmt}", "export", "export_scene")
    report_path = add("docs", "acceptance.json", "acceptance", "assignment_check")

    stages = [
        ("mid_model", True, ["create_object", "create_curve", "loft_mesh", "group_objects", "save_scene"]),
        ("low_model", True, ["mesh_retopo", "mesh_reduce", "mesh_topology", "mesh_snap"]),
        ("uv", True, ["uv_project", "uv_unfold", "uv_layout", "uv_inspect", "uv_snapshot"]),
        ("bake", bool(config.bake_maps), ["bake_map"]),
        ("capture", bool(config.views or config.comparison_views), ["capture_viewport", "render_view", "compose_sheet"]),
        ("export", bool(config.export_formats), ["export_scene"]),
        ("acceptance", True, ["assignment_check"]),
    ]
    warnings = []
    if missing:
        warnings.append("Missing or invalid assignment inputs: " + ", ".join(missing))
    return {"title": config.title, "work_name": config.work_name, "model_style": config.model_style,
            "requirements": config.requirements, "filename_stem": stem, "missing_inputs": missing,
            "reference_images": references, "bake_maps": config.bake_maps, "views": config.views,
            "comparison_views": config.comparison_views, "report_path": report_path,
            "stages": [{"id": name, "enabled": enabled, "tools": tools} for name, enabled, tools in stages],
            "deliverables": groups, "warnings": warnings}


def _check_file(root, name):
    path = project_path(root, name)
    if not path.is_file():
        return "missing", 0
    size = path.stat().st_size
    if not size:
        return "invalid", 0
    suffix = path.suffix.lower()
    if suffix == ".png":
        try:
            with Image.open(path) as image:
                return ("present" if image.format == "PNG" and _verify_png(image) else "invalid"), size
        except (OSError, SyntaxError, Image.DecompressionBombError):
            return "invalid", size
    with path.open("rb") as stream:
        start = stream.read(128)
        signatures = {".ma": (b"//Maya ASCII",), ".mb": (b"FOR4", b"FOR8"),
                      ".fbx": (b"Kaydara FBX Binary", b"; FBX")}
        if suffix in signatures:
            return ("present" if start.startswith(signatures[suffix]) else "invalid"), size
        if suffix == ".obj":
            stream.seek(0)
            found = set()
            for line in stream:
                token = line.lstrip()[:2]
                if token in {b"v ", b"f "}:
                    found.add(token)
                if len(found) == 2:
                    return "present", size
    return "invalid", size


def _verify_png(image):
    image.verify()
    return True


def check_assignment(root, config, inspect, low_meshes=(), write_report=False, overwrite=False):
    result = plan_assignment(root, config)
    warnings = result.pop("warnings")
    all_present = True
    for group in ("deliverables", "renders", "exports"):
        for item in result["deliverables"][group]:
            item["status"], item["bytes"] = _check_file(root, item["path"])
            all_present &= item["status"] == "present"

    topology_required = config.max_low_faces is not None or config.require_quads
    topology = []
    if len(low_meshes) > 64 or len(set(low_meshes)) != len(low_meshes):
        raise ValueError("Use at most 64 distinct meshes for acceptance inspection")
    for name in low_meshes:
        topology.append(inspect(name, config.require_quads))
    faces = sum(item.get("faces", 0) for item in topology)
    topology_ok = (bool(low_meshes) or not topology_required) and all(item["ok"] for item in topology)
    if config.max_low_faces is not None:
        topology_ok &= faces <= config.max_low_faces
    if topology_required and not low_meshes:
        warnings.append("Low-mesh names are required for configured face-count/quad acceptance.")
    if not topology_ok:
        warnings.append("Low-mesh topology or UV acceptance failed or is pending.")
    missing = set(result["missing_inputs"])
    blocking_inputs = (config.require_references and "reference_images" in missing) or (
        config.require_identity and bool(missing & {"student_id", "student_name"}))
    result.update(ready=bool(all_present and not blocking_inputs and topology_ok), topology=topology,
                  low_faces=faces if low_meshes else None, topology_ok=bool(topology_ok),
                  manual_review=["Compare the model with reference images and verify course-specific visual requirements."])
    report = result["deliverables"]["docs"][0]
    report["status"] = "written" if write_report else "planned"
    artifacts = []
    if write_report:
        target = output_path(root, result["report_path"], {".json"}, overwrite)
        for group in GROUPS:
            project_path(root, Path(config.output_dir) / group).mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=target.parent) as folder:
            temporary = Path(folder) / "acceptance.json"
            temporary.write_text(json.dumps({**result, "warnings": warnings}, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
            publish_file(temporary, target, overwrite)
        artifacts.append({"path": str(target), "bytes": target.stat().st_size})
    return {**result, "warnings": warnings, "artifacts": artifacts}

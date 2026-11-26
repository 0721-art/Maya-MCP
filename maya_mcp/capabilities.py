import sys


def has_command(cmds, name):
    return callable(getattr(cmds, name, None))


def transfer_command(cmds):
    # The Transfer Maps editor ships with surfaceSampler in tested Maya builds.
    if has_command(cmds, "transferMaps"):
        flags = cmds.help("transferMaps") or ""
        if all("-" + f in flags for f in ("target", "source", "mapOutput", "filename")):
            return "transferMaps"
    return "surfaceSampler" if has_command(cmds, "surfaceSampler") else None


def plugin_state(cmds, name):
    try:
        loaded = bool(cmds.pluginInfo(name, query=True, loaded=True))
    except RuntimeError:
        loaded = False
    paths = cmds.pluginInfo(query=True, listPluginsPath=True) or []
    from pathlib import Path
    import os

    directories = os.environ.get("MAYA_PLUG_IN_PATH", "").split(os.pathsep)
    suffixes = (".mll", ".so", ".bundle", ".py")
    available = loaded or any(Path(p).stem.casefold() == name.casefold() for p in paths)
    if not available:
        available = any((Path(path) / (name + ext)).exists()
                        for path in directories if path for ext in suffixes)
    return {"loaded": loaded, "available": available}


def require_plugin(cmds, name):
    try:
        if not plugin_state(cmds, name)["loaded"]:
            cmds.loadPlugin(name, quiet=True)
    except RuntimeError as exc:
        raise ValueError(f"Required Maya plugin {name} could not be loaded: {exc}") from exc


def describe(cmds):
    return {
        "python": sys.version.split()[0],
        "renderer": cmds.getAttr("defaultRenderGlobals.currentRenderer"),
        "capabilities": {
            "retopo": has_command(cmds, "polyRetopo"),
            "remesh": has_command(cmds, "polyRemesh"),
            "reduce": has_command(cmds, "polyReduce"),
            "transfer_maps": transfer_command(cmds),
            "uv_unfold": has_command(cmds, "unfold"),
            "arnold_texture": has_command(cmds, "arnoldRenderToTexture"),
            "plugins": {key: plugin_state(cmds, name) for key, name in
                        (("arnold", "mtoa"), ("fbx", "fbxmaya"), ("obj", "objExport"),
                         ("unfold3d", "Unfold3D"))},
        },
    }

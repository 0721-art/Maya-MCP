"""Locate actual Maya interpreters without importing Maya in the host process."""

import os
import re
import shutil
from pathlib import Path

YEARS = range(2022, 2027)
_VERSION = re.compile(r"(?<!\d)20(?:22|23|24|25|26)(?!\d)")


def version_from_path(path):
    match = _VERSION.search(str(path))
    return int(match.group()) if match else None


def registry_root(year):
    if os.name != "nt":
        return None
    import winreg

    key = r"SOFTWARE\Autodesk\Maya\{}\Setup\InstallPath".format(year)
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as handle:
                return Path(winreg.QueryValueEx(handle, "MAYA_INSTALL_LOCATION")[0])
        except OSError:
            continue
    return None


def discover():
    env = os.environ
    executable = "mayapy.exe" if os.name == "nt" else "mayapy"
    roots = [Path(p) for p in env.get("MAYA_MCP_INSTALL_ROOTS", "").split(os.pathsep) if p]
    if os.name == "nt":
        roots.append(Path(env.get("ProgramFiles", r"C:\Program Files")) / "Autodesk")
    else:
        roots.extend([Path("/usr/autodesk"), Path("/Applications/Autodesk")])
    path_exe = shutil.which(executable)
    explicit = env.get("MAYA_MCP_MAYAPY")
    explicit_year = version_from_path(env.get("MAYA_MCP_VERSION")) or version_from_path(explicit)
    location = env.get("MAYA_LOCATION")
    found = {}
    for year in YEARS:
        candidates = []
        if explicit and explicit_year == year:
            candidates.append(Path(explicit))
        if path_exe and version_from_path(path_exe) == year:
            candidates.append(Path(path_exe))
        registered = registry_root(year)
        if registered:
            candidates.append(registered / "bin" / executable)
        if location and version_from_path(location) == year:
            candidates.append(Path(location) / "bin" / executable)
        for root in roots:
            bases = [root / f"Maya{year}", root / f"Maya {year}" / f"Maya{year}"]
            if version_from_path(root) == year:
                bases.insert(0, root)
            for base in bases:
                candidates.extend((base / "bin" / executable,
                                   base / "Maya.app" / "Contents" / "bin" / executable))
        for candidate in candidates:
            if candidate.is_file():
                found[year] = candidate.resolve()
                break
    return found


def choose_mayapy(year=None):
    explicit = os.environ.get("MAYA_MCP_MAYAPY")
    if explicit:
        return Path(explicit).resolve()
    found = discover()
    if year is not None:
        return found.get(year)
    if found:
        return found[max(found)]
    path = shutil.which("mayapy")
    return Path(path).resolve() if path else None

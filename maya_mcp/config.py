import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .versions import YEARS, choose_mayapy


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 9876
    token: str = ""
    root: Path = field(default_factory=Path.cwd)
    allowed_roots: tuple[Path, ...] = ()
    allow_external_paths: bool = False
    timeout: float = 60
    render_timeout: float = 600
    allow_code: bool = False
    allow_script: bool = False
    mode: str = "auto"
    mayapy: Optional[Path] = None
    version: Optional[int] = None

    def __post_init__(self):
        if self.host != "127.0.0.1":
            raise ValueError("MAYA_MCP_HOST must be 127.0.0.1")
        if (type(self.port) is not int or not 0 <= self.port <= 65535
                or self.mode not in {"auto", "gui", "headless", "commandport"}):
            raise ValueError("Invalid Maya bridge port or mode")
        if any(type(value) not in (int, float) or not 0 < value <= 3600
               for value in (self.timeout, self.render_timeout)):
            raise ValueError("Timeouts must be in (0, 3600] seconds")
        if self.version is not None and (type(self.version) is not int or self.version not in YEARS):
            raise ValueError("MAYA_MCP_VERSION must be a year from 2022 to 2026")
        if any(type(flag) is not bool for flag in (self.allow_code, self.allow_script, self.allow_external_paths)):
            raise ValueError("Policy flags must be booleans")
        if not isinstance(self.allowed_roots, tuple) or any(not isinstance(p, Path) for p in self.allowed_roots):
            raise ValueError("allowed_roots must be a tuple of Path objects")

    @classmethod
    def from_env(cls):
        env = os.environ
        year = int(env["MAYA_MCP_VERSION"]) if env.get("MAYA_MCP_VERSION") else None
        mode = env.get("MAYA_MCP_MODE") or "auto"
        port_var = "MAYA_MCP_COMMAND_PORT" if mode == "commandport" else "MAYA_MCP_PORT"
        port = int(env.get(port_var) or "9876")
        allowed_roots = tuple(
            Path(entry).resolve()
            for entry in env.get("MAYA_MCP_ALLOWED_ROOTS", "").split(os.pathsep)
            if entry.strip()
        )
        return cls(
            host=env.get("MAYA_MCP_HOST") or "127.0.0.1",
            port=port,
            token=env.get("MAYA_MCP_TOKEN", ""),
            root=Path(env.get("MAYA_MCP_PROJECT_ROOT") or Path.cwd()).resolve(),
            allowed_roots=allowed_roots,
            allow_external_paths=env.get("MAYA_MCP_ALLOW_EXTERNAL_PATHS", "0") == "1",
            timeout=float(env.get("MAYA_MCP_TIMEOUT") or "60"),
            render_timeout=float(env.get("MAYA_MCP_RENDER_TIMEOUT") or "600"),
            allow_code=env.get("MAYA_MCP_ALLOW_CODE", "0") == "1",
            allow_script=env.get("MAYA_MCP_ALLOW_SCRIPT", "0") == "1",
            mode=mode,
            mayapy=choose_mayapy(year),
            version=year,
        )


def _is_ascii(text: str) -> bool:
    try:
        text.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def staging_temp_root(root) -> Path:
    """Return a writable temp directory, preferring an ASCII path for Maya C++ tools."""
    explicit = os.environ.get("MAYA_MCP_TMP")
    if explicit:
        temp = Path(explicit).resolve()
    else:
        temp = Path(root).resolve() / ".tmp" / "maya"
        if not _is_ascii(str(temp)):
            fallback = Path(tempfile.gettempdir()).resolve() / "maya_mcp"
            if _is_ascii(str(fallback)):
                temp = fallback
    temp.mkdir(parents=True, exist_ok=True)
    return temp


def maya_environment(root):
    """Give the child writable preferences and native temp directories."""
    temp = staging_temp_root(root)
    app_dir = temp / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=temp):
        pass
    return {**os.environ, "TMP": str(temp), "TEMP": str(temp),
            "MAYA_APP_DIR": str(app_dir), "PYTHONUTF8": "1"}

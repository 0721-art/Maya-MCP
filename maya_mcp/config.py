import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .versions import YEARS, choose_mayapy


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 9876
    token: str = ""
    root: Path = Path.cwd()
    timeout: float = 60
    render_timeout: float = 600
    allow_code: bool = False
    mode: str = "auto"
    mayapy: Optional[Path] = None
    version: Optional[int] = None

    def __post_init__(self):
        if self.host != "127.0.0.1":
            raise ValueError("MAYA_MCP_HOST must be 127.0.0.1")
        if not 0 <= self.port <= 65535 or self.mode not in {"auto", "gui", "headless", "commandport"}:
            raise ValueError("Invalid Maya bridge port or mode")
        if any(not 0 < value <= 3600 for value in (self.timeout, self.render_timeout)):
            raise ValueError("Timeouts must be in (0, 3600] seconds")
        if self.version is not None and self.version not in YEARS:
            raise ValueError("MAYA_MCP_VERSION must be a year from 2022 to 2026")

    @classmethod
    def from_env(cls):
        env = os.environ
        year = int(env["MAYA_MCP_VERSION"]) if env.get("MAYA_MCP_VERSION") else None
        mode = env.get("MAYA_MCP_MODE", "auto")
        port = int(env.get("MAYA_MCP_COMMAND_PORT" if mode == "commandport" else "MAYA_MCP_PORT", "9876"))
        return cls(
            host=env.get("MAYA_MCP_HOST", "127.0.0.1"),
            port=port,
            token=env.get("MAYA_MCP_TOKEN", ""),
            root=Path(env.get("MAYA_MCP_PROJECT_ROOT", Path.cwd())).resolve(),
            timeout=float(env.get("MAYA_MCP_TIMEOUT", "60")),
            render_timeout=float(env.get("MAYA_MCP_RENDER_TIMEOUT", "600")),
            allow_code=env.get("MAYA_MCP_ALLOW_CODE", "0") == "1",
            mode=mode,
            mayapy=choose_mayapy(year),
            version=year,
        )


def maya_environment(root):
    """Give the child writable preferences and native temp directories."""
    temp = Path(os.environ.get("MAYA_MCP_TMP", root / ".tmp" / "maya")).resolve()
    app_dir = temp / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=temp):
        pass
    return {**os.environ, "TMP": str(temp), "TEMP": str(temp),
            "MAYA_APP_DIR": str(app_dir), "PYTHONUTF8": "1"}

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 9876
    token: str = ""
    root: Path = Path.cwd()
    timeout: float = 60
    render_timeout: float = 600
    allow_code: bool = True
    mode: str = "auto"
    mayapy: Path = Path("D:/Maya 2026/Maya2026/bin/mayapy.exe")

    def __post_init__(self):
        if self.host != "127.0.0.1":
            raise ValueError("MAYA_MCP_HOST must be 127.0.0.1")
        if not 0 <= self.port <= 65535 or self.mode not in {"auto", "gui", "headless"}:
            raise ValueError("Invalid Maya bridge port or mode")
        if any(not 0 < value <= 3600 for value in (self.timeout, self.render_timeout)):
            raise ValueError("Timeouts must be in (0, 3600] seconds")

    @classmethod
    def from_env(cls):
        env = os.environ
        return cls(
            host=env.get("MAYA_MCP_HOST", "127.0.0.1"),
            port=int(env.get("MAYA_MCP_PORT", "9876")),
            token=env.get("MAYA_MCP_TOKEN", ""),
            root=Path(env.get("MAYA_MCP_PROJECT_ROOT", Path.cwd())).resolve(),
            timeout=float(env.get("MAYA_MCP_TIMEOUT", "60")),
            render_timeout=float(env.get("MAYA_MCP_RENDER_TIMEOUT", "600")),
            allow_code=env.get("MAYA_MCP_ALLOW_CODE", "1") == "1",
            mode=env.get("MAYA_MCP_MODE", "auto"),
            mayapy=Path(env.get("MAYA_MCP_MAYAPY", str(cls.mayapy))),
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

import logging
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path
from threading import RLock

from .client import BridgeClient, BridgeError
from .commandport import CommandPortClient
from .config import maya_environment
from .versions import version_from_path

logger = logging.getLogger(__name__)


class MayaSession:
    def __init__(self, settings):
        self.settings = settings
        self.client = (CommandPortClient(settings.host, settings.port, settings.token,
                                         settings.root, timeout=settings.timeout)
                       if settings.mode == "commandport"
                       else BridgeClient(settings.host, settings.port, settings.token))
        self.worker = None
        self.startup = RLock()
        self.ready = False
        self.closed = False

    def _probe(self, timeout):
        info = self.client.call("get_maya_info", timeout=timeout)
        expected = "gui" if self.settings.mode == "commandport" else self.settings.mode
        if expected != "auto" and info.get("mode") != expected:
            raise BridgeError("configuration", f"Connected bridge mode is {info.get('mode')}, expected {expected}")
        if self.settings.version and version_from_path(info.get("version")) != self.settings.version:
            raise BridgeError("configuration", f"Connected Maya {info.get('version')}, expected {self.settings.version}")
        return info

    def _ensure_bridge(self):
        with self.startup:
            if self.closed:
                raise BridgeError("connection", "Maya MCP session is shutting down")
            if self.ready and (self.worker is None or self.worker.poll() is None):
                return
            if self.settings.mode == "commandport":
                self._probe(2)
                self.ready = True
                return
            try:
                self._probe(2)
            except BridgeError as exc:
                if exc.kind != "connection" or self.settings.mode == "gui":
                    raise
            else:
                self.ready = True
                return
            self._start_worker()
            self.ready = True

    def _start_worker(self):
        s = self.settings
        if s.mode == "commandport":
            raise BridgeError("configuration", "commandport mode never starts a mayapy worker")
        if s.mayapy is None or not s.mayapy.is_file():
            raise BridgeError("configuration", "No matching mayapy executable. Set MAYA_MCP_MAYAPY or MAYA_MCP_INSTALL_ROOTS.")
        self.client.token = s.token or secrets.token_urlsafe(32)
        port = s.port
        if port == 0:
            with socket.socket() as probe:
                probe.bind((s.host, 0))
                port = probe.getsockname()[1]
            self.client.address = (s.host, port)
        env = maya_environment(s.root)
        pythonpath = str(Path(__file__).resolve().parent.parent)
        if env.get("PYTHONPATH"):
            pythonpath += os.pathsep + env["PYTHONPATH"]
        env.update(PYTHONPATH=pythonpath, MAYA_MCP_PROJECT_ROOT=str(s.root),
                   MAYA_MCP_MODE="headless", MAYA_MCP_PORT=str(port),
                   MAYA_MCP_TOKEN=self.client.token,
                   MAYA_MCP_TIMEOUT=str(s.timeout),
                   MAYA_MCP_RENDER_TIMEOUT=str(s.render_timeout),
                   MAYA_MCP_MAYAPY=str(s.mayapy),
                   MAYA_MCP_ALLOW_CODE="1" if s.allow_code else "0")
        if s.version is not None:
            env["MAYA_MCP_VERSION"] = str(s.version)
        else:
            env.pop("MAYA_MCP_VERSION", None)
        year = s.version or version_from_path(s.mayapy)
        if year:
            arnold = Path(env.get("ProgramFiles", r"C:\Program Files")) / "Autodesk" / "Arnold" / f"Maya{year}"
            if (arnold / "mtoa.mod").is_file():
                env["MAYA_MODULE_PATH"] = os.pathsep.join(filter(None, (str(arnold), env.get("MAYA_MODULE_PATH"))))
        log = Path(os.environ.get("MAYA_MCP_LOG") or (s.root / ".tmp" / "maya-worker.log"))
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as stream:
            self.worker = subprocess.Popen(
                [str(s.mayapy), "-m", "maya_mcp.worker"], cwd=s.root, env=env,
                stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 45
        try:
            while self.worker.poll() is None and time.monotonic() < deadline:
                try:
                    if self._probe(1)["pid"] == self.worker.pid:
                        return
                    raise BridgeError("configuration", "The configured port was taken during Maya startup")
                except BridgeError as exc:
                    if exc.kind not in {"connection", "timeout"}:
                        raise
                time.sleep(0.15)
            raise BridgeError("connection", f"Maya worker did not start; see {log}")
        except BaseException:
            self.close()
            raise

    def call(self, command, *, long=False, **params):
        self._ensure_bridge()
        timeout = self.settings.render_timeout if long else self.settings.timeout
        try:
            return self.client.envelope(command, timeout=timeout, **params)
        except BridgeError as exc:
            if exc.kind == "connection":
                self.ready = False
            raise

    def close(self, permanent=False):
        with self.startup:
            self.closed = self.closed or permanent
            if self.worker is not None:
                if self.worker.poll() is None:
                    self.worker.terminate()
                    try:
                        self.worker.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.worker.kill()
                        self.worker.wait()
                self.worker = None
            self.ready = False

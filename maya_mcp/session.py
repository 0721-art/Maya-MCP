import logging
import os
import subprocess
import time
from pathlib import Path
from threading import Lock

from .client import BridgeClient, BridgeError
from .config import maya_environment

logger = logging.getLogger(__name__)


class MayaSession:
    def __init__(self, settings):
        self.settings = settings
        self.client = BridgeClient(settings.host, settings.port, settings.token)
        self.worker = None
        self.startup = Lock()
        self.ready = False

    def _probe(self, timeout):
        info = self.client.call("get_maya_info", timeout=timeout)
        expected = self.settings.mode
        if expected != "auto" and info.get("mode") != expected:
            raise BridgeError("configuration", f"Connected bridge mode is {info.get('mode')}, expected {expected}")
        return info

    def _ensure_bridge(self):
        with self.startup:
            if self.ready and (self.worker is None or self.worker.poll() is None):
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
        if not s.mayapy.is_file():
            raise BridgeError("configuration", f"mayapy not found: {s.mayapy}")
        env = maya_environment(s.root)
        pythonpath = str(Path(__file__).resolve().parent.parent)
        if env.get("PYTHONPATH"):
            pythonpath += os.pathsep + env["PYTHONPATH"]
        env.update(PYTHONPATH=pythonpath, MAYA_MCP_PROJECT_ROOT=str(s.root),
                   MAYA_MCP_MODE="headless", MAYA_MCP_PORT=str(s.port),
                   MAYA_MCP_TOKEN=s.token, MAYA_MCP_ALLOW_CODE="1" if s.allow_code else "0")
        log = Path(os.environ.get("MAYA_MCP_LOG", s.root / ".tmp" / "maya-worker.log"))
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
            return self.client.call(command, timeout=timeout, **params)
        except BridgeError as exc:
            if exc.kind == "connection":
                self.ready = False
            raise

    def close(self):
        if self.worker is not None:
            if self.worker.poll() is None:
                self.worker.terminate()
                self.worker.wait(timeout=10)
            self.worker = None
        self.ready = False

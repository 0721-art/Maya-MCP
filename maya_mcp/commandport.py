"""Maya native commandPort transport.

Maya's ``cmds.commandPort`` service is a local, line-oriented Python socket.
The MCP host sends one self-contained Python line; that line imports the
repository copy of this module and dispatches one already-registered handler.
No Maya-side package installation or MCP SDK is required.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import socket
import time
from pathlib import Path
from uuid import uuid4

from .client import BridgeError
from .envelope import failure, success

BEGIN = "__MAYA_MCP_BEGIN__"
END = "__MAYA_MCP_END__"
REPLY_LIMIT = 32 * 1024 * 1024


def _script_for(request_b64: str, root: Path) -> str:
    """Build the single line sent to Maya's commandPort."""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        "from maya_mcp.commandport import dispatch\n"
        f"print({BEGIN!r} + dispatch({request_b64!r}) + {END!r})\n"
    )
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return "import base64;exec(compile(base64.b64decode(%r),'<maya_mcp_commandport>','exec'))\n" % encoded


def build_request(command: str, params: dict, token: str, root: Path,
                  timeout: float = 60.0, request_id: str | None = None) -> tuple[str, str]:
    """Return ``(request_id, commandPort_line)`` for one call."""
    request_id = request_id or uuid4().hex
    payload = {
        "id": request_id,
        "type": command,
        "params": params,
        "token": token,
        "root": str(root),
        "timeout_ms": max(1, int(timeout * 1000)),
    }
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    request_b64 = base64.b64encode(raw).decode("ascii")
    return request_id, _script_for(request_b64, root)


def parse_reply(text: str, request_id: str) -> dict:
    """Extract the JSON envelope between the commandPort markers."""
    start = text.find(BEGIN)
    end = text.find(END, start + len(BEGIN)) if start >= 0 else -1
    if start < 0 or end < 0:
        raise ValueError("Maya commandPort reply is missing the Maya-MCP markers")
    try:
        reply = json.loads(text[start + len(BEGIN):end])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid Maya commandPort JSON: {exc}") from exc
    if not isinstance(reply, dict):
        raise ValueError("Maya commandPort reply must be a JSON object")
    if reply.get("id") != request_id:
        raise ValueError("Mismatched Maya commandPort response ID")
    if reply.get("status") == "ok" and isinstance(reply.get("result"), dict):
        return reply
    error = reply.get("error")
    if reply.get("status") == "error" and isinstance(error, dict):
        raise BridgeError(error.get("kind", "internal"),
                          error.get("message", "Maya commandPort error"))
    raise BridgeError("protocol", "Invalid Maya commandPort response envelope")


def dispatch(request_b64: str) -> str:
    """Run inside Maya: decode one request and call a registered handler."""
    request = None
    try:
        request = json.loads(base64.b64decode(request_b64).decode("utf-8"))
        if not isinstance(request, dict):
            raise ValueError("Request must be a JSON object")
        token = request.get("token")
        expected = os.environ.get("MAYA_MCP_COMMAND_TOKEN", "")
        if not isinstance(token, str) or not token:
            raise ValueError("Missing commandPort token")
        if expected and not hmac.compare_digest(token, expected):
            raise ValueError("Failed commandPort authentication")
        command = request.get("type")
        params = request.get("params")
        root = Path(str(request.get("root") or ".")).resolve()
        if not isinstance(command, str) or not command:
            raise ValueError("Unknown command")
        if not isinstance(params, dict):
            raise ValueError("Expected object params")
        from maya import cmds
        from .handlers import MayaHandlers
        from .config import Settings

        settings = Settings(root=root, token=token, mode="gui")
        handlers = MayaHandlers(cmds, settings).commands()
        if command not in handlers:
            raise ValueError("Unknown command")
        result = handlers[command](**params)
        if not isinstance(result, dict):
            raise TypeError("Maya command must return an object")
        payload = {
            "id": request.get("id"),
            "status": "ok",
            "result": result,
            "maya": {"mode": "gui", "version": cmds.about(version=True)},
            **success(result),
        }
        return json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except Exception as exc:  # Keep the commandPort reply JSON even on failure.
        return json.dumps({
            "id": request.get("id") if isinstance(request, dict) else None,
            "status": "error",
            **failure(type(exc).__name__.lower(), str(exc)),
        }, ensure_ascii=False, allow_nan=False)


class CommandPortClient:
    """Host-side client for a Maya commandPort opened by ``cmds.commandPort``."""

    def __init__(self, host: str, port: int, token: str, root: Path, timeout: float = 60.0):
        if host != "127.0.0.1":
            raise ValueError("Maya commandPort must use 127.0.0.1")
        if not 0 < int(port) <= 65535:
            raise ValueError("Invalid Maya commandPort")
        self.address = (host, int(port))
        self.token = token
        self.root = Path(root).resolve()
        self.timeout = timeout

    def call(self, command: str, *, timeout: float | None = None, **params):
        return self.envelope(command, timeout=timeout, **params)["data"]

    def envelope(self, command: str, *, timeout: float | None = None, **params) -> dict:
        timeout = self.timeout if timeout is None else float(timeout)
        request_id, line = build_request(command, params, self.token, self.root, timeout)
        reply = self._exchange(line, timeout)
        return parse_reply(reply, request_id)

    def _exchange(self, line: str, timeout: float) -> str:
        try:
            with socket.create_connection(self.address, timeout=min(timeout, 5)) as conn:
                conn.settimeout(timeout + 2)
                conn.sendall(line.encode("utf-8"))
                chunks = []
                total = 0
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > REPLY_LIMIT:
                        raise ValueError("Maya commandPort reply exceeds 32 MiB")
                    chunks.append(chunk)
                    if END.encode("ascii") in b"".join(chunks):
                        break
        except TimeoutError as exc:
            raise BridgeError("timeout", "Maya commandPort response timed out") from exc
        except OSError as exc:
            raise BridgeError("connection", f"Cannot connect to Maya commandPort {self.address}: {exc}") from exc
        return b"".join(chunks).decode("utf-8", "replace")

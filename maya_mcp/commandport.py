"""Maya native commandPort transport.

Maya's ``cmds.commandPort`` service is a local, line-oriented Python socket.
The MCP host sends one self-contained Python expression; Maya adds this
repository root to ``sys.path`` if needed and dispatches one already-registered
handler. No Maya-side package installation or MCP SDK is required.
"""
from __future__ import annotations

import base64
import hmac
import json
import math
import os
import socket
import subprocess
from pathlib import Path
from uuid import uuid4

from .client import BridgeError
from .envelope import failure, success
from .protocol import FRAME_LIMIT, loads_strict

BEGIN = "__MAYA_MCP_BEGIN__"
END = "__MAYA_MCP_END__"
_END_BYTES = END.encode("ascii")
REQUEST_LIMIT = 65536
MAX_REQUEST_LIMIT = FRAME_LIMIT
REPLY_LIMIT = 32 * 1024 * 1024
REPO_ROOT = Path(__file__).resolve().parents[1]
_ENVELOPE_FIELDS = ("ok", "data", "error", "warnings", "artifacts")


def _markers(request_id: str | None) -> tuple[str, str]:
    if request_id:
        return f"{BEGIN}{request_id}:", f":{request_id}{END}"
    return BEGIN, END


def _default_request_limit() -> int:
    raw = os.environ.get("MAYA_MCP_COMMAND_BUFFER_SIZE")
    if not raw:
        return REQUEST_LIMIT
    limit = int(raw)
    if not 4096 <= limit <= MAX_REQUEST_LIMIT:
        raise ValueError(f"MAYA_MCP_COMMAND_BUFFER_SIZE must be between 4096 and {MAX_REQUEST_LIMIT}")
    return limit


def _script_for(request_b64: str, repo_root: Path = REPO_ROOT,
                request_id: str | None = None) -> str:
    """Build a single-line Python expression sent to Maya's commandPort.

    Evaluating as one expression returns the framed reply directly (working
    even when ``echoOutput=False``) while also printing it for ``echoOutput=True``.
    The reply payload is base64-encoded inside per-request markers so scene data
    or Maya stdout cannot collide with delimiters, and the outer line contains no
    literal ``BEGIN``/``END`` markers.
    """
    repo_str = str(Path(repo_root).resolve())
    open_marker, close_marker = _markers(request_id)
    inner = (
        "import base64, json, sys\n"
        f"_repo = {repo_str!r}\n"
        "if _repo not in sys.path:\n"
        "    sys.path.append(_repo)\n"
        "try:\n"
        "    from maya_mcp.commandport import dispatch\n"
        "    _payload = dispatch(_req)\n"
        "except Exception as _exc:\n"
        "    _payload = json.dumps({\n"
        f"        'id': {request_id!r},\n"
        "        'status': 'error',\n"
        "        'ok': False,\n"
        "        'data': None,\n"
        "        'error': {'kind': 'internal', 'message': f'{type(_exc).__name__}: {_exc}'},\n"
        "        'warnings': [],\n"
        "        'artifacts': [],\n"
        "    }, ensure_ascii=False, allow_nan=False)\n"
        "_encoded = base64.b64encode(_payload.encode('utf-8')).decode('ascii')\n"
        f"_r = {open_marker!r} + 'b64:' + _encoded + {close_marker!r}\n"
        "print(_r)\n"
    )
    encoded = base64.b64encode(inner.encode("utf-8")).decode("ascii")
    return (
        "(lambda _ns: (exec(compile(__import__('base64').b64decode(%r),"
        "'<maya_mcp_commandport>','exec'),_ns),_ns['_r'])[1])({'_req':%r})\n"
        % (encoded, request_b64)
    )


def build_request(command: str, params: dict, token: str, root: Path,
                  timeout: float = 60.0, request_id: str | None = None,
                  repo_root: Path = REPO_ROOT,
                  max_bytes: int = REQUEST_LIMIT) -> tuple[str, str]:
    """Return ``(request_id, commandPort_line)`` for one call."""
    if type(max_bytes) is not int or not 1024 <= max_bytes <= MAX_REQUEST_LIMIT:
        raise ValueError(f"max_bytes must be an integer between 1024 and {MAX_REQUEST_LIMIT}")
    request_id = request_id or uuid4().hex
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise ValueError("A string request id is required")
    payload = {
        "id": request_id,
        "type": command,
        "params": params,
        "token": token,
        "root": str(Path(root).resolve()),
        "timeout_ms": max(1, int(timeout * 1000)),
    }
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    request_b64 = base64.b64encode(raw).decode("ascii")
    line = _script_for(request_b64, repo_root=repo_root, request_id=request_id)
    line_bytes = len(line.encode("utf-8"))
    if line_bytes > max_bytes:
        raise ValueError(
            f"Maya commandPort request ({line_bytes} bytes) exceeds buffer limit ({max_bytes} bytes); "
            "increase Maya's commandPort bufferSize and MAYA_MCP_COMMAND_BUFFER_SIZE or use the TCP JSON bridge"
        )
    return request_id, line


def parse_reply(text: str, request_id: str) -> dict:
    """Extract and validate the JSON envelope between the commandPort markers."""
    open_marker, close_marker = _markers(request_id)
    start = text.find(open_marker)
    end = text.find(close_marker, start + len(open_marker)) if start >= 0 else -1
    if start >= 0 and end >= 0:
        body = text[start + len(open_marker):end]
    else:
        start = text.find(BEGIN)
        end = text.find(END, start + len(BEGIN)) if start >= 0 else -1
        if start < 0 or end < 0:
            snippet = text.replace("\x00", "").strip()[-200:]
            detail = f": {snippet}" if snippet else ""
            raise BridgeError("protocol", f"Maya commandPort reply is missing the Maya-MCP markers{detail}")
        body = text[start + len(BEGIN):end]
    try:
        if body.startswith("b64:"):
            body = base64.b64decode(body[4:].encode("ascii"), validate=True).decode("utf-8")
        reply = loads_strict(body)
    except (TypeError, ValueError) as exc:
        raise BridgeError("protocol", f"Invalid Maya commandPort JSON: {exc}") from exc
    reply_id = reply.get("id")
    error = reply.get("error")
    if reply.get("status") == "error" and isinstance(error, dict):
        if reply_id is not None and reply_id != request_id:
            raise BridgeError("protocol", "Mismatched Maya commandPort response ID")
        raise BridgeError(error.get("kind", "internal"),
                          error.get("message", "Maya commandPort error"))
    if reply_id != request_id:
        raise BridgeError("protocol", "Mismatched Maya commandPort response ID")
    if reply.get("status") == "ok" and isinstance(reply.get("result"), dict):
        return reply
    raise BridgeError("protocol", "Invalid Maya commandPort response envelope")


def _expected_token() -> str:
    return os.environ.get("MAYA_MCP_TOKEN") or os.environ.get("MAYA_MCP_COMMAND_TOKEN", "")


def _resolve_root(request: dict) -> Path:
    env_root = os.environ.get("MAYA_MCP_PROJECT_ROOT", "")
    req_root = request.get("root")
    if env_root:
        configured = Path(env_root).resolve()
        if req_root:
            if not isinstance(req_root, str):
                raise ValueError("Invalid project root")
            if Path(req_root).resolve() != configured:
                raise ValueError("Requested project root does not match Maya's MAYA_MCP_PROJECT_ROOT")
        return configured
    if req_root is not None:
        if not isinstance(req_root, str) or not req_root:
            raise ValueError("Invalid project root")
        return Path(req_root).resolve()
    return Path.cwd().resolve()


def dispatch(request_b64: str) -> str:
    """Run inside Maya: decode one request and call a registered handler."""
    request_id = None
    try:
        request = loads_strict(base64.b64decode(request_b64).decode("utf-8"))
        candidate_id = request.get("id")
        if not isinstance(candidate_id, str) or not candidate_id or len(candidate_id) > 128:
            raise ValueError("A string request id is required")
        request_id = candidate_id
        token = request.get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("Missing commandPort token")
        expected = _expected_token()
        if not expected:
            raise ValueError("Maya commandPort requires a nonempty MAYA_MCP_TOKEN in Maya's environment")
        if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
            raise ValueError("Failed commandPort authentication")
        command = request.get("type")
        params = request.get("params")
        timeout_ms = request.get("timeout_ms", 60_000)
        if not isinstance(command, str) or not command:
            raise ValueError("Unknown command")
        if not isinstance(params, dict) or type(timeout_ms) is not int or not 1 <= timeout_ms <= 3_600_000:
            raise ValueError("Expected object params and valid timeout_ms")
        root = _resolve_root(request)

        from maya import cmds
        from .config import Settings
        from .handlers import MayaHandlers

        env_render_timeout = float(os.environ.get("MAYA_MCP_RENDER_TIMEOUT") or "600")
        if not math.isfinite(env_render_timeout) or not 0 < env_render_timeout <= 3600:
            raise ValueError("MAYA_MCP_RENDER_TIMEOUT must be in (0, 3600] seconds")
        req_timeout = min(max(timeout_ms / 1000.0, 0.001), 3600.0)
        render_timeout = min(max(req_timeout, env_render_timeout), 3600.0)
        allow_code = os.environ.get("MAYA_MCP_ALLOW_CODE", "0") == "1"
        settings = Settings(
            root=root,
            token=token,
            timeout=req_timeout,
            render_timeout=render_timeout,
            allow_code=allow_code,
            mode="gui",
        )
        handlers = MayaHandlers(cmds, settings).commands()
        if command not in handlers:
            raise ValueError("Unknown command")
        result = handlers[command](**params)
        envelope = success(result)
        payload = {
            "id": request_id,
            "status": "ok",
            "result": result,
            "maya": {"mode": "gui", "version": cmds.about(version=True)},
            **envelope,
        }
        try:
            return json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            return json.dumps({
                "id": request_id,
                "status": "error",
                **failure("internal", str(exc)),
            }, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as exc:
        kind, message = "validation", str(exc)
    except (TimeoutError, subprocess.TimeoutExpired) as exc:
        kind, message = "timeout", str(exc)
    except Exception as exc:  # Keep the commandPort reply JSON even on failure.
        kind, message = "maya_error", f"{type(exc).__name__}: {exc}"
    return json.dumps({
        "id": request_id,
        "status": "error",
        **failure(kind, message),
    }, ensure_ascii=False, allow_nan=False)


class CommandPortClient:
    """Host-side client for a Maya commandPort opened by ``cmds.commandPort``."""

    def __init__(self, host: str, port: int, token: str, root: Path,
                 timeout: float = 60.0, repo_root: Path = REPO_ROOT,
                 max_request_bytes: int | None = None):
        if host != "127.0.0.1":
            raise ValueError("Maya commandPort must use 127.0.0.1")
        if type(port) is bool or not 0 < int(port) <= 65535:
            raise ValueError("Invalid Maya commandPort")
        if not isinstance(token, str) or not token:
            raise ValueError("commandport mode requires a nonempty MAYA_MCP_TOKEN")
        limit = _default_request_limit() if max_request_bytes is None else max_request_bytes
        if type(limit) is not int or not 1024 <= limit <= MAX_REQUEST_LIMIT:
            raise ValueError(f"max_request_bytes must be an integer between 1024 and {MAX_REQUEST_LIMIT}")
        self.address = (host, int(port))
        self.token = token
        self.root = Path(root).resolve()
        self.timeout = timeout
        self.repo_root = Path(repo_root).resolve()
        self.max_request_bytes = limit

    def call(self, command: str, *, timeout: float | None = None, **params):
        return self.envelope(command, timeout=timeout, **params)["data"]

    def envelope(self, command: str, *, timeout: float | None = None, **params) -> dict:
        timeout = self.timeout if timeout is None else float(timeout)
        try:
            request_id, line = build_request(
                command, params, self.token, self.root, timeout,
                repo_root=self.repo_root, max_bytes=self.max_request_bytes)
        except (TypeError, ValueError) as exc:
            raise BridgeError("validation", str(exc)) from exc
        end_marker = _markers(request_id)[1].encode("utf-8")
        reply = parse_reply(self._exchange(line, timeout, end_marker=end_marker), request_id)
        if reply.get("ok") is True and all(name in reply for name in _ENVELOPE_FIELDS):
            return {name: reply[name] for name in _ENVELOPE_FIELDS}
        return success(reply["result"])

    def _exchange(self, line: str, timeout: float, end_marker: bytes = _END_BYTES) -> str:
        buf = bytearray()
        marker_len = len(end_marker)
        try:
            with socket.create_connection(self.address, timeout=min(timeout, 5)) as conn:
                conn.settimeout(timeout + 2)
                conn.sendall(line.encode("utf-8"))
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    prev_len = len(buf)
                    buf.extend(chunk)
                    if len(buf) > REPLY_LIMIT:
                        raise BridgeError("protocol", "Maya commandPort reply exceeds 32 MiB")
                    scan_from = max(0, prev_len - marker_len + 1)
                    if buf.find(end_marker, scan_from) >= 0 or b"\x00" in chunk:
                        break
        except BridgeError:
            raise
        except TimeoutError as exc:
            raise BridgeError("timeout", "Maya commandPort response timed out") from exc
        except OSError as exc:
            raise BridgeError("connection", f"Cannot connect to Maya commandPort {self.address}: {exc}") from exc
        return bytes(buf).decode("utf-8", "replace")

import socket
from uuid import uuid4

from .protocol import encode, read_frame


class BridgeError(RuntimeError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class BridgeClient:
    def __init__(self, host, port, token=""):
        self.address = host, port
        self.token = token

    def call(self, command, *, timeout=60, **params):
        request_id = uuid4().hex
        frame = encode({"id": request_id, "type": command, "params": params,
                        "timeout_ms": max(1, int(timeout * 1000)), "token": self.token})
        try:
            conn = socket.create_connection(self.address, timeout=min(timeout, 5))
        except OSError as exc:
            raise BridgeError("connection", f"Cannot connect to Maya at {self.address}: {exc}") from exc
        try:
            with conn, conn.makefile("rb") as stream:
                conn.settimeout(timeout + 2)
                conn.sendall(frame)
                reply = read_frame(stream)
        except TimeoutError as exc:
            raise BridgeError("timeout", "Bridge response timed out; Maya may still be running") from exc
        except (OSError, ValueError) as exc:
            raise BridgeError("protocol", f"Invalid bridge response: {exc}") from exc
        if reply.get("id") != request_id:
            raise BridgeError("protocol", "Mismatched Maya response ID")
        if reply.get("status") == "ok" and isinstance(reply.get("result"), dict):
            return reply["result"]
        error = reply.get("error")
        if reply.get("status") == "error" and isinstance(error, dict):
            raise BridgeError(error.get("kind", "internal"), error.get("message", "Bridge error"))
        raise BridgeError("protocol", "Invalid Maya response envelope")

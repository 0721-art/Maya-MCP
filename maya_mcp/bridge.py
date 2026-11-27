import dataclasses
import hmac
import os
import socketserver
import subprocess
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from threading import BoundedSemaphore, Lock, Thread

from .config import Settings
from .envelope import failure, success
from .protocol import encode, read_frame


class RequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            response = self.server.dispatch(read_frame(self.rfile))
        except ValueError as exc:
            response = self.server.error(None, "validation", str(exc))
        except OSError:
            return
        try:
            data = encode(response)
        except (TypeError, ValueError) as exc:
            data = encode(self.server.error(response.get("id"), "internal", str(exc)))
        try:
            self.wfile.write(data)
        except OSError:
            pass


class Bridge(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self, settings, handlers, mode, version, schedule):
        self.settings = settings
        self.handlers = handlers
        self.metadata = {"mode": mode, "version": version}
        self.schedule = schedule
        self.command_lock = Lock()
        self.connections = BoundedSemaphore(8)
        super().__init__((settings.host, settings.port), RequestHandler)

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def error(self, request_id, kind, message):
        return {"id": request_id, "status": "error", "maya": self.metadata, **failure(kind, message)}

    def dispatch(self, request):
        start = time.monotonic()
        request_id = request.get("id")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            return self.error(None, "validation", "A string request id is required")
        token = request.get("token", "")
        if not isinstance(token, str) or not hmac.compare_digest(
                token.encode("utf-8"), self.settings.token.encode("utf-8")):
            return self.error(request_id, "validation", "Failed bridge authentication")
        command, params = request.get("type"), request.get("params")
        if not isinstance(command, str) or command not in self.handlers:
            return self.error(request_id, "validation", "Unknown command")
        timeout_ms = request.get("timeout_ms", 60_000)
        if not isinstance(params, dict) or type(timeout_ms) is not int or not 1 <= timeout_ms <= 3_600_000:
            return self.error(request_id, "validation", "Expected object params and valid timeout_ms")
        if not self.command_lock.acquire(blocking=False):
            return self.error(request_id, "busy", "Maya is already processing a command")

        future = Future()

        def run():
            if not future.set_running_or_notify_cancel():
                return
            try:
                if time.monotonic() - start >= timeout_ms / 1000:
                    raise FutureTimeoutError("Maya main-thread queue deadline expired")
                result = self.handlers[command](**params)
                envelope = success(result)
            except Exception as exc:
                future.set_exception(exc)
            except BaseException as exc:
                future.set_exception(RuntimeError(f"Interrupted: {type(exc).__name__}"))
                raise
            else:
                future.set_result((result, envelope))
            finally:
                self.command_lock.release()

        try:
            self.schedule(run)
        except Exception as exc:
            self.command_lock.release()
            return self.error(request_id, "internal", str(exc))
        try:
            result, envelope = future.result(timeout=max(0, timeout_ms / 1000 - (time.monotonic() - start)))
        except (FutureTimeoutError, TimeoutError, subprocess.TimeoutExpired) as exc:
            if future.done():
                message = str(exc) or "Maya command deadline expired"
            elif future.cancel():
                self.command_lock.release()
                message = "Maya main-thread queue deadline expired"
            else:
                message = "Maya command deadline expired; a started operation may still finish"
            return self.error(request_id, "timeout", message)
        except (ValueError, TypeError) as exc:
            return self.error(request_id, "validation", str(exc))
        except Exception as exc:
            return self.error(request_id, "maya_error", f"{type(exc).__name__}: {exc}")
        return {"id": request_id, "status": "ok", "result": result, "maya": self.metadata,
                "ms": round((time.monotonic() - start) * 1000), **envelope}


_gui = None


def start_gui(settings=None):
    """Call from the Maya Script Editor on the GUI main thread."""
    global _gui
    if _gui is not None:
        return _gui.server_address
    from maya import cmds, utils
    from .handlers import MayaHandlers

    if cmds.about(batch=True):
        raise RuntimeError("Use python -m maya_mcp.worker in standalone Maya")
    if settings is None:
        settings = Settings.from_env()
        if not os.environ.get("MAYA_MCP_MAYAPY") and not os.environ.get("MAYA_MCP_VERSION"):
            settings = dataclasses.replace(settings, mayapy=None)
    if not settings.token:
        raise ValueError("Set the same nonempty MAYA_MCP_TOKEN for Maya and the MCP server")
    bridge = Bridge(settings, MayaHandlers(cmds, settings).commands(), "gui",
                    cmds.about(version=True), utils.executeDeferred)
    Thread(target=bridge.serve_forever, daemon=True).start()
    _gui = bridge
    return bridge.server_address


def stop_gui():
    global _gui
    if _gui is not None:
        _gui.shutdown()
        _gui.server_close()
        _gui = None

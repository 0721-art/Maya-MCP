"""Bounded asynchronous jobs for the Maya tools that may take minutes."""

from collections import OrderedDict
from concurrent.futures import Future
from inspect import signature
from threading import Lock, Thread
from uuid import uuid4

from pydantic import TypeAdapter

from .envelope import failure, success


class MayaJobs:
    def __init__(self, tools):
        self.tools = tools
        self.results = OrderedDict()
        self.lock = Lock()

    def submit(self, name, arguments):
        tool = self.tools.get(name)
        if tool is None:
            return failure("validation", f"Pollable tools: {', '.join(sorted(self.tools))}")
        try:
            sig = signature(tool)
            bound = sig.bind(**arguments)
            values = {key: TypeAdapter(sig.parameters[key].annotation).validate_python(value)
                      for key, value in bound.arguments.items()}
        except (TypeError, ValueError) as exc:
            return failure("validation", str(exc))

        with self.lock:
            if self.results and not next(reversed(self.results.values())).done():
                return failure("busy", "A background Maya job is already active")
            job_id = uuid4().hex
            future = Future()
            self.results[job_id] = future
            if len(self.results) > 24:
                self.results.popitem(last=False)

        def run():
            future.set_running_or_notify_cancel()
            try:
                result = tool(**values)
            except Exception as exc:
                result = failure("internal", str(exc))
            future.set_result(result)

        Thread(target=run, name=f"maya-mcp-job-{job_id[:8]}", daemon=True).start()
        return success({"job_id": job_id, "state": "queued"})

    def status(self, job_id):
        with self.lock:
            future = self.results.get(job_id)
        if future is None:
            return failure("validation", "Unknown or expired job ID")
        if future.done():
            result = future.result()
            return success({"job_id": job_id, "state": "succeeded" if result["ok"] else "failed",
                            "result": result, "warnings": result["warnings"], "artifacts": result["artifacts"]})
        return success({"job_id": job_id, "state": "running" if future.running() else "queued"})

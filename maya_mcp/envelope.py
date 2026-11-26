"""Wire and MCP response fields shared with Maya's bundled Python."""


def success(data):
    if not isinstance(data, dict):
        raise TypeError("Maya command must return an object")
    data = dict(data)
    warnings = data.pop("warnings", [])
    artifacts = data.pop("artifacts", [])
    if not artifacts and "path" in data and "bytes" in data:
        artifacts = [{"path": data["path"], "bytes": data["bytes"]}]
    return {"ok": True, "data": data, "error": None, "warnings": warnings, "artifacts": artifacts}


def failure(kind, message):
    return {"ok": False, "data": None, "error": {"kind": kind, "message": message},
            "warnings": [], "artifacts": []}

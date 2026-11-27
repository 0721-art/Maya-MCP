import ast
import contextlib
import errno
import io
import math
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROTECTED_METADATA = {".git", ".agents", ".codex", ".aws"}
MAX_SCRIPT_LENGTH = 65_536
MAX_SCRIPT_OUTPUT = 65_536


def _has_protected_part(parts):
    return bool(PROTECTED_METADATA.intersection(part.rstrip(" .").casefold() for part in parts))


def _check_no_symlink_inside_root(root_resolved, raw):
    """Reject symlinked path components before resolving or collapsing ``..``.

    Checking unresolved components in order is important: a lexical
    ``normpath`` collapses ``linkdir/..`` before the symlink check, which would
    let a path traverse an intra-root directory symlink without noticing it.
    """
    root = Path(root_resolved)
    raw_path = Path(raw)

    if raw_path.is_absolute() or raw_path.drive:
        try:
            raw_path = raw_path.relative_to(root)
        except ValueError:
            # Caller either validated another allowed root or rejected outside root.
            return

    current = root
    for part in raw_path.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if current != root:
                current = current.parent
            continue
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Path cannot traverse or target a symlink: {current}")


def _check_no_symlink_absolute(raw):
    raw_path = Path(raw)
    if not raw_path.is_absolute():
        return
    current = Path(raw_path.anchor)
    for part in raw_path.parts[1:]:
        if part in ("", "."):
            continue
        if part == "..":
            if current != Path(raw_path.anchor):
                current = current.parent
            continue
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Path cannot traverse or target a symlink: {current}")


def _resolve_with_root(root, name, *, allowed_roots=(), allow_external=False, allow_outside_root=False):
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ValueError("File path must be a nonempty string")
    if type(allow_outside_root) is not bool:
        raise ValueError("allow_outside_root must be a boolean")
    primary = Path(root).resolve()
    roots = (primary, *(Path(entry).resolve() for entry in allowed_roots))
    raw = Path(name)
    if ":" in str(raw)[len(raw.drive):]:
        raise ValueError("File streams are not valid project paths")
    path = (primary / raw).resolve()

    for candidate_root in roots:
        try:
            parts = path.relative_to(candidate_root).parts
        except ValueError:
            continue
        if not parts or _has_protected_part(parts):
            raise ValueError("File path must be inside the project root and outside metadata")
        return path, candidate_root

    if not allow_external:
        raise ValueError(
            f"File path must be inside the project root and outside metadata "
            f"(root={primary}; configure MAYA_MCP_PROJECT_ROOT / MAYA_MCP_ALLOWED_ROOTS "
            f"or enable MAYA_MCP_ALLOW_EXTERNAL_PATHS=1 with allow_outside_root=True)"
        )
    if not allow_outside_root:
        raise ValueError(
            f"File path is outside the project root ({primary}); "
            f"pass allow_outside_root=True to explicitly confirm access outside the project root"
        )
    if not raw.is_absolute():
        raise ValueError("Paths outside the project root must be explicit absolute paths")
    if _has_protected_part(path.parts):
        raise ValueError("File path must be outside protected metadata directories")
    return path, None


def project_path(root, name, *, allowed_roots=(), allow_external=False, allow_outside_root=False):
    path, _ = _resolve_with_root(
        root, name, allowed_roots=allowed_roots,
        allow_external=allow_external, allow_outside_root=allow_outside_root,
    )
    return path


def output_path(root, name, extensions, overwrite=False, create_parent=True, *,
                allowed_roots=(), allow_external=False, allow_outside_root=False):
    if type(overwrite) is not bool:
        raise ValueError("overwrite must be a boolean")
    path, matched_root = _resolve_with_root(
        root, name, allowed_roots=allowed_roots,
        allow_external=allow_external, allow_outside_root=allow_outside_root,
    )
    if path.suffix.lower() not in extensions:
        raise ValueError(f"Expected a file with suffix in {sorted(extensions)}")
    if matched_root is not None:
        _check_no_symlink_inside_root(matched_root, Path(name))
    else:
        _check_no_symlink_absolute(Path(name))
    if (path.exists() or path.is_symlink()) and (not overwrite or path.is_symlink() or not path.is_file()):
        raise ValueError(f"Output already exists: {path}")
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def input_path(root, name, extensions, *, allowed_roots=(), allow_external=False, allow_outside_root=False):
    path, matched_root = _resolve_with_root(
        root, name, allowed_roots=allowed_roots,
        allow_external=allow_external, allow_outside_root=allow_outside_root,
    )
    if path.suffix.lower() not in extensions:
        raise ValueError(f"Expected a file with suffix in {sorted(extensions)}")
    if matched_root is not None:
        _check_no_symlink_inside_root(matched_root, Path(name))
    else:
        _check_no_symlink_absolute(Path(name))
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Input file does not exist or is not a regular file: {path}")
    return path


def publish_file(source, target, overwrite=False):
    source = Path(source)
    target = Path(target)
    if overwrite:
        try:
            source.replace(target)
        except OSError as exc:
            if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EACCES}:
                raise
            fd, tmp_name = tempfile.mkstemp(prefix=".mcp_pub_", dir=str(target.parent))
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as dst, source.open("rb") as src:
                    shutil.copyfileobj(src, dst)
                tmp_path.replace(target)
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
            source.unlink(missing_ok=True)
    else:
        try:
            os.link(str(source), str(target))
        except FileExistsError as exc:
            raise ValueError(f"Output already exists: {target}") from exc
        except OSError as exc:
            if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EACCES}:
                raise
            try:
                fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            except FileExistsError as exists_exc:
                raise ValueError(f"Output already exists: {target}") from exists_exc
            with os.fdopen(fd, "wb") as dst, source.open("rb") as src:
                shutil.copyfileobj(src, dst)


def run_commands(code, commands):
    """Interpret literal calls. Python source is never evaluated."""
    if not isinstance(code, str) or not code or "\x00" in code:
        raise ValueError("Code must be a nonempty string")
    if len(code) > 16_384:
        raise ValueError("Code is too long")
    try:
        tree = ast.parse(code)
        calls = []
        for statement in tree.body:
            call = statement.value if isinstance(statement, ast.Expr) else None
            if (not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute)
                    or not isinstance(call.func.value, ast.Name)
                    or call.func.value.id != "cmds" or call.func.attr not in commands
                    or any(kw.arg is None for kw in call.keywords)):
                raise ValueError("Expected an allowlisted cmds call with literal arguments")
            args = [ast.literal_eval(arg) for arg in call.args]
            kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}
            calls.append((commands[call.func.attr], args, kwargs))
    except (SyntaxError, RecursionError) as exc:
        raise ValueError("Invalid literal Maya commands") from exc
    return [fn(*args, **kwargs) for fn, args, kwargs in calls]


def _json_safe(value, depth=0):
    if depth > 32:
        return repr(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Script result contains non-finite float: {value}")
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in value.items()}
    return repr(value)


def execute_python_script(code, namespace, timeout=60.0, max_length=MAX_SCRIPT_LENGTH):
    """Execute full Maya Python source with size, output, and line-level deadline limits."""
    if not isinstance(code, str) or not code.strip() or "\x00" in code:
        raise ValueError("Script code must be a nonempty string without null bytes")
    if len(code) > max_length:
        raise ValueError(f"Script length ({len(code)}) exceeds limit ({max_length} characters)")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError("Script timeout must be in (0, 300] seconds")
    try:
        compiled = compile(code, "<maya_mcp_script>", "exec")
    except (SyntaxError, ValueError) as exc:
        raise ValueError(f"Invalid Python script syntax: {exc}") from exc

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    started = time.monotonic()
    deadline = started + float(timeout)
    previous_trace = sys.gettrace()
    counter = 0

    def _deadline_tracer(frame, event, arg):
        nonlocal counter
        counter += 1
        if (counter & 31) == 0 and time.monotonic() > deadline:
            raise TimeoutError(f"Maya script exceeded {timeout}s timeout")
        return _deadline_tracer

    exec_ns = dict(namespace)
    exec_ns.setdefault("result", None)
    try:
        sys.settrace(_deadline_tracer)
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            exec(compiled, exec_ns, exec_ns)
    finally:
        sys.settrace(previous_trace)

    raw_out = stdout_buf.getvalue()
    raw_err = stderr_buf.getvalue()
    truncated = len(raw_out) > MAX_SCRIPT_OUTPUT or len(raw_err) > MAX_SCRIPT_OUTPUT
    return {
        "result": _json_safe(exec_ns.get("result")),
        "stdout": raw_out[:MAX_SCRIPT_OUTPUT],
        "stderr": raw_err[:MAX_SCRIPT_OUTPUT],
        "truncated": truncated,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }

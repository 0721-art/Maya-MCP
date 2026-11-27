import ast
import errno
import os
import shutil
from pathlib import Path


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
            # project_path() rejects paths outside the project root.
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
            raise ValueError(f"Output path cannot traverse or target a symlink: {current}")


def output_path(root, name, extensions, overwrite=False, create_parent=True):
    if type(overwrite) is not bool:
        raise ValueError("overwrite must be a boolean")
    path = project_path(root, name)
    if path.suffix.lower() not in extensions:
        raise ValueError(f"Expected a file with suffix in {sorted(extensions)}")
    _check_no_symlink_inside_root(Path(root).resolve(), Path(name))
    if (path.exists() or path.is_symlink()) and (not overwrite or path.is_symlink() or not path.is_file()):
        raise ValueError(f"Output already exists: {path}")
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def project_path(root, name):
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ValueError("File path must be a nonempty string")
    root = Path(root).resolve()
    raw = Path(name)
    if ":" in str(raw)[len(raw.drive):]:
        raise ValueError("File streams are not valid project paths")
    path = (root / raw).resolve()
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = ()
    protected = {".git", ".agents", ".codex", ".aws"}
    if not parts or protected.intersection(part.rstrip(" .").casefold() for part in parts):
        raise ValueError("File path must be inside the project root and outside metadata")
    return path


def publish_file(source, target, overwrite=False):
    if overwrite:
        source.replace(target)
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

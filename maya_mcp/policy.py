import ast
from pathlib import Path


def output_path(root, name, extensions, overwrite=False):
    root = root.resolve()
    path = (root / Path(name)).resolve()
    protected = {".git", ".agents", ".codex", ".aws"}
    if not path.is_relative_to(root) or protected.intersection(path.relative_to(root).parts):
        raise ValueError("Output path must be inside the project root and outside metadata")
    if path.suffix.lower() not in extensions:
        raise ValueError(f"Expected a file with suffix in {sorted(extensions)}")
    if path.exists() and (not overwrite or not path.is_file()):
        raise ValueError(f"Output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


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

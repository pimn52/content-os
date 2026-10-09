"""Stdlib-only shared execution errors and exact-path checks; no app imports."""
from pathlib import Path
import stat


class ExecutionPreparationError(RuntimeError):
    """Named, path/credential-free application stop."""


class NativeExecutionUnsupported(ExecutionPreparationError):
    pass


NATIVE_SUFFIXES = frozenset((".dll", ".pyd", ".exe"))


def require_search_topology(files, directories, reserved, *, target_parent=None):
    """Owned direct startup/import roots plus the explicitly selected load dir.

    Inventory is still complete. Nested stdlib data is not a recursive Windows
    search directory; selecting a native target adds its exact parent instead.
    """
    parents = {Path("."), Path("Lib"), Path("DLLs"), Path("Lib/site-packages")}
    if target_parent is not None:
        parents.add(Path(target_parent))
    seen = set()
    for value in (*files, *directories):
        path = Path(value)
        if path.parent not in parents:
            continue
        name = path.name.casefold()
        if name.endswith((".local", ".manifest")):
            raise NativeExecutionUnsupported("execution_dll_redirection_unsupported")
        if value in files and path.suffix.casefold() in NATIVE_SUFFIXES:
            if name in reserved or name in seen:
                raise NativeExecutionUnsupported("execution_dll_basename_collision")
            seen.add(name)


def require_unique_native_origins(paths):
    """Loaded native names cannot resolve to two different physical sources."""
    seen = {}
    for path in paths:
        if path.suffix.casefold() not in NATIVE_SUFFIXES:
            continue
        name = path.name.casefold()
        if name in seen and seen[name] != path:
            raise NativeExecutionUnsupported("execution_dll_basename_collision")
        seen[name] = path


def _exact_path(path: Path, *, directory: bool) -> None:
    try:
        if not path.is_absolute() or path != path.resolve(strict=True):
            raise ValueError()
        mode = path.lstat().st_mode
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
            raise ValueError()
    except (OSError, ValueError, RuntimeError):
        raise ExecutionPreparationError("execution_runtime_path_invalid") from None

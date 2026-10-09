"""Conservative base/stdlib selection, not third-party/native closure."""
from pathlib import Path

from .runtime_tree import RuntimeFileSelection, RuntimeTreeSelection, _exact_path


def select_cpython312_base(base: Path):
    """Explicit non-venv Windows source layout; no metadata/cache inference.

    Root VC runtimes are selected bytes, not exempted system dependencies.
    No source-less bytecode; package roots are selected separately by adapters.
    This does not certify binary version, DLL origins or permission to execute.
    """
    _exact_path(base, directory=True)
    trees = (RuntimeTreeSelection(base / "Lib", "Lib", "cpython312-stdlib-source"),
             RuntimeTreeSelection(base / "DLLs", "DLLs"))
    files = tuple(RuntimeFileSelection(base / name, name,
        "interpreter" if name == "python.exe" else "dependency") for name in (
            "python.exe", "python312.dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll"))
    for tree in trees:
        _exact_path(tree.source, directory=True)
    for item in files:
        _exact_path(item.source, directory=False)
    return trees, files


def select_cpython312_runtime(*, base: Path, packages_root: Path):
    """Compose actual base with a complete explicit package root, no resolver.

    Preserve package data/metadata/native files; omit only source-backed cache.
    This creates no virtualenv, installs nothing and never imports packages.
    The adapter still owns application entry/native dependency closure.
    """
    trees, files = select_cpython312_base(base)
    _exact_path(packages_root, directory=True)
    return (*trees, RuntimeTreeSelection(packages_root, "Lib/site-packages", "cpython312-package-source")), files

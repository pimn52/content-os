"""Deterministic stdlib ZIP application; no vendor import or dependency install."""
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from .prepared import ExecutionPreparationError
from .runtime_tree import _exact_path

BUNDLE_SLOT = "entry/application.pyz"
BUNDLE_MAX_BYTES = 8 * 1024**2
MAIN_SOURCE = b"from app.providers.maintained_voice_child import main\nraise SystemExit(main())\n"


def bundle_bytes(main_source=MAIN_SOURCE):
    # Include all owned Python modules so imported application helpers do not
    # silently come from CWD or the editable development checkout. Third-party
    # packages remain in the separately observed maintained installation.
    root = Path(__file__).resolve().parents[1]
    _exact_path(root, directory=True)
    paths = sorted(root.rglob("*.py"))
    if not 0 < len(paths) <= 1024:
        raise ExecutionPreparationError("maintained_application_bundle_limit")
    entries = {"__main__.py": main_source}
    total = len(main_source)
    for path in paths:
        _exact_path(path, directory=False)
        size = path.stat().st_size
        if not 0 <= size <= 1024**2 or total + size > BUNDLE_MAX_BYTES:
            raise ExecutionPreparationError("maintained_application_bundle_limit")
        raw = path.read_bytes()
        if len(raw) != size:
            raise ExecutionPreparationError("maintained_application_source_changed")
        entries["app/" + path.relative_to(root).as_posix()] = raw
        total += size
    for name in ("app/__init__.py", "app/providers/__init__.py", "app/providers/maintained_voice_child.py"):
        if name not in entries:
            raise ExecutionPreparationError("maintained_application_source_missing")
    stream = BytesIO()
    with ZipFile(stream, "w", compression=ZIP_STORED) as archive:
        for name, raw in sorted(entries.items()):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, raw)
    result = stream.getvalue()
    if len(result) > BUNDLE_MAX_BYTES:
        raise ExecutionPreparationError("maintained_application_bundle_limit")
    return result

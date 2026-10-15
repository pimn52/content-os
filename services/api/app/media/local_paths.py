"""Existing portable local-media resolution shared by read and render boundaries."""
from pathlib import Path
import re
from urllib.parse import urlsplit


def is_remote_or_network_path(raw: str) -> bool:
    if re.match(r"^[A-Za-z]:[\\/]", raw):
        return False
    parsed = urlsplit(raw)
    return bool(parsed.scheme or parsed.netloc or raw.startswith("//") or raw.startswith("\\\\"))


def resolve_local_media_path(raw: str, data_root: Path | None) -> Path:
    value = Path(raw).expanduser()
    if not value.is_absolute() and data_root is not None:
        root = data_root.resolve()
        parts = value.parts
        path = (root.parent / value if parts and parts[0].casefold() == root.name.casefold() else root / value).resolve()
        if not path.is_relative_to(root):
            raise ValueError("relative media path escapes configured data root")
    else:
        path = value.resolve()
    return path


def existing_local_media_file(raw: str, data_root: Path) -> Path:
    if is_remote_or_network_path(raw):
        raise ValueError("remote or network media sources are not supported")
    path = resolve_local_media_path(raw, data_root)
    if not path.is_file():
        raise FileNotFoundError("asset source file not found")
    return path

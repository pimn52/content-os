"""Server-owned output paths for local render artifacts."""
from __future__ import annotations

from pathlib import Path
from uuid import UUID


def render_output_path(root: str | Path, project_id: UUID, render_id: UUID) -> Path:
    """Return an MP4 path made only from server-controlled UUID values."""
    safe_root = Path(root).resolve()
    path = (safe_root / str(project_id) / f"{render_id}.mp4").resolve()
    try:
        path.relative_to(safe_root)
    except ValueError as exc:  # defensive; UUID strings cannot traverse.
        raise RuntimeError("render output escaped its configured root") from exc
    return path

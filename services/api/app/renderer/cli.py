"""Command-line entry point for one validated local Remotion render."""
from __future__ import annotations

import argparse
from pathlib import Path

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import VideoSpec

from .remotion import RemotionRenderer, RendererError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a local Content OS VideoSpec with Remotion.")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--spec", required=True, type=Path, help="VideoSpec JSON file")
    parser.add_argument("--output", required=True, type=Path, help="Local .mp4 destination")
    parser.add_argument("--renderer-dir", default=Path("apps/renderer"), type=Path)
    parser.add_argument("--npm-command", default="npm")
    parser.add_argument("--timeout-seconds", default=600.0, type=float)
    args = parser.parse_args(argv)
    try:
        spec = VideoSpec.model_validate_json(args.spec.read_text(encoding="utf-8"))
        db = Database(args.database)
        try:
            RemotionRenderer(
                AssetRepository(db), ClipRepository(db), renderer_dir=args.renderer_dir,
                npm_command=args.npm_command, timeout_seconds=args.timeout_seconds,
            ).render(spec, args.output)
        finally:
            db.close()
    except (OSError, ValueError, RendererError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

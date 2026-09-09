"""Local renderer boundary for validated VideoSpec contracts."""

from .remotion import (
    LocalResourceError,
    RemotionRenderer,
    RenderInputError,
    RenderProcessError,
    RenderTimeout,
    RendererError,
    UnauthorizedVisualError,
)
from .outputs import render_output_path

__all__ = [
    "LocalResourceError",
    "RemotionRenderer",
    "RenderInputError",
    "RenderProcessError",
    "RenderTimeout",
    "RendererError",
    "UnauthorizedVisualError",
    "render_output_path",
]

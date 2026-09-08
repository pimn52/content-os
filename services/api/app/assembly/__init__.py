"""Pure local assembly from selected Clip candidates to VideoSpec contracts."""

from .video_spec import (
    AssetIdentityMismatch,
    AssetNotFound,
    CaptureGapSelected,
    CandidateNotFound,
    InsufficientSourceDuration,
    InvalidCandidateSelection,
    VideoSpecAssembler,
    VideoSpecAssemblyError,
    milliseconds_to_frames,
)

__all__ = [
    "AssetIdentityMismatch",
    "AssetNotFound",
    "CaptureGapSelected",
    "CandidateNotFound",
    "InsufficientSourceDuration",
    "InvalidCandidateSelection",
    "VideoSpecAssembler",
    "VideoSpecAssemblyError",
    "milliseconds_to_frames",
]

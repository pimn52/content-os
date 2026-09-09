"""Persistent local job-claiming primitives.

There is deliberately no polling daemon here.  A future runner can compose
these state transitions without making SQLite or lease details part of the
domain contract.
"""

from .handlers import AssetAnalysisJobHandler, AssetTranscriptionJobHandler, AssetVisionJobHandler, ExtractedKeyframeResolver, RenderVideoJobHandler
from .runner import JobExecutionError, JobRunner, LeaseLost, NoHandler
from .store import JobStore
from .targets import (
    AssetJobIdempotencyConflict,
    AssetJobTarget,
    AssetJobTargetError,
    AssetJobTargetStore,
    UnsupportedAssetJobType,
)
from .worker import JobWorker

__all__ = [
    "AssetAnalysisJobHandler",
    "AssetJobIdempotencyConflict",
    "AssetJobTarget",
    "AssetJobTargetError",
    "AssetJobTargetStore",
    "AssetTranscriptionJobHandler",
    "AssetVisionJobHandler",
    "ExtractedKeyframeResolver",
    "JobExecutionError",
    "JobRunner",
    "JobStore",
    "JobWorker",
    "LeaseLost",
    "NoHandler",
    "RenderVideoJobHandler",
    "UnsupportedAssetJobType",
]

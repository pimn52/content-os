"""Local media import, analysis, and derived-file primitives."""

from .extraction import (
    AudioExtraction,
    ExtractionError,
    FFmpegExtractionService,
    KeyframeExtraction,
    MediaExtractor,
)
from .ffprobe import FFProbeAdapter, ProbeError, ProbeMetadata
from .importer import MediaImportError, MediaImporter
from .pipeline import MediaAnalysisPipeline, MediaAnalysisResult, MediaAnalysisService, MediaPipelineError
from .segmentation import FFmpegSceneDetector, SceneDetector, SegmentationError

__all__ = [
    "AudioExtraction",
    "ExtractionError",
    "FFmpegExtractionService",
    "FFmpegSceneDetector",
    "FFProbeAdapter",
    "KeyframeExtraction",
    "MediaAnalysisPipeline",
    "MediaAnalysisResult",
    "MediaAnalysisService",
    "MediaExtractor",
    "MediaImportError",
    "MediaImporter",
    "MediaPipelineError",
    "ProbeError",
    "ProbeMetadata",
    "SceneDetector",
    "SegmentationError",
]

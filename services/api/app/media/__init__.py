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
from .transcripts import (
    ClipTranscriptPersistence,
    NoClipsForAsset,
    TranscriptMapper,
    TranscriptSegment,
    map_transcript_to_clips,
)

__all__ = [
    "AudioExtraction",
    "ClipTranscriptPersistence",
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
    "NoClipsForAsset",
    "ProbeError",
    "ProbeMetadata",
    "SceneDetector",
    "SegmentationError",
    "TranscriptMapper",
    "TranscriptSegment",
    "map_transcript_to_clips",
]

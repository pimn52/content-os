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
from .vision import ClipVisualMetadataPersistence, VisualMetadata, map_visual_metadata
from .vision_pipeline import MediaVisionPipeline, VisionAnalysisResult, VisionPipelineError

__all__ = [
    "AudioExtraction",
    "ClipTranscriptPersistence",
    "ClipVisualMetadataPersistence",
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
    "MediaVisionPipeline",
    "NoClipsForAsset",
    "ProbeError",
    "ProbeMetadata",
    "SceneDetector",
    "SegmentationError",
    "TranscriptMapper",
    "TranscriptSegment",
    "VisionAnalysisResult",
    "VisionPipelineError",
    "VisualMetadata",
    "map_transcript_to_clips",
    "map_visual_metadata",
]

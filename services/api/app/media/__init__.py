"""Local media import, analysis, and derived-file primitives."""

from .extraction import (
    AudioExtraction,
    ExtractionError,
    FFmpegExtractionService,
    KeyframeExtraction,
    MediaExtractor,
)
from .audio_importer import AudioImportError, AudioImporter
from .ffprobe import AudioProbeMetadata, FFProbeAdapter, ProbeError, ProbeMetadata
from .image_importer import ImageImportError, ImageImporter
from .importer import MediaImportError, MediaImporter
from .pipeline import MediaAnalysisPipeline, MediaAnalysisResult, MediaAnalysisService, MediaPipelineError
from .segmentation import FFmpegSceneDetector, SceneDetector, SegmentationError
from .subtitles import SubtitleParseError, parse_subtitle_file, parse_subtitle_text
from .transcripts import (
    AudioAssetNotFound,
    AudioTranscriptPersistence,
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
    "AudioAssetNotFound",
    "AudioImportError",
    "AudioImporter",
    "AudioProbeMetadata",
    "AudioTranscriptPersistence",
    "ClipTranscriptPersistence",
    "ClipVisualMetadataPersistence",
    "ExtractionError",
    "FFmpegExtractionService",
    "FFmpegSceneDetector",
    "FFProbeAdapter",
    "ImageImportError",
    "ImageImporter",
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
    "SubtitleParseError",
    "TranscriptMapper",
    "TranscriptSegment",
    "VisionAnalysisResult",
    "VisionPipelineError",
    "VisualMetadata",
    "map_transcript_to_clips",
    "map_visual_metadata",
    "parse_subtitle_file",
    "parse_subtitle_text",
]

"""Replaceable provider adapters kept outside the Content OS domain contracts."""

from .asr import (
    ASRAuthenticationError, ASRConfigurationError, ASRConnectionError, ASRError,
    ASRHTTPError, ASRInputError, ASRProvider, ASRProviderResponseError,
    ASRRateLimitError, ASRTimeout, FasterWhisperASRProvider,
    OpenAICompatibleASRProvider, TranscriptionResult, TranscriptionSegment,
)
from .vision import (
    ClipVisualAnalysis, OpenAICompatibleVisionProvider, VisionAuthenticationError,
    VisionConfigurationError, VisionConnectionError, VisionError, VisionHTTPError,
    VisionInputError, VisionProvider, VisionProviderResponseError, VisionRateLimitError,
    VisionTimeout,
)
from .embedding import (
    EmbeddingAuthenticationError, EmbeddingBatch, EmbeddingConfigurationError,
    EmbeddingConnectionError, EmbeddingError, EmbeddingHTTPError, EmbeddingInputError,
    EmbeddingProvider, EmbeddingProviderResponseError, EmbeddingRateLimitError,
    EmbeddingTimeout, OpenAICompatibleEmbeddingProvider,
)
from .scene_planner import (
    OpenAICompatibleScenePlanner, ScenePlanResult, ScenePlanner,
    ScenePlannerAuthenticationError, ScenePlannerConfigurationError,
    ScenePlannerConnectionError, ScenePlannerError, ScenePlannerHTTPError,
    ScenePlannerInputError, ScenePlannerProviderResponseError,
    ScenePlannerRateLimitError, ScenePlannerTimeout,
)
from .voice import (
    OmniVoiceProvider, VoiceAuthenticationError, VoiceConfigurationError,
    VoiceConnectionError, VoiceError, VoiceInputError, VoiceProvider,
    VoiceProviderResponseError, VoiceRateLimitError, VoiceSynthesisResult, VoiceTimeout,
)
from .talking import (
    TalkingAuthenticationError, TalkingConfigurationError, TalkingConnectionError,
    TalkingError, TalkingHeadProvider, TalkingInputError, TalkingProviderResponseError,
    TalkingRateLimitError, TalkingReference, TalkingSynthesisResult, TalkingTimeout,
)
from .latentsync import (
    LATENTSYNC_MODEL, LATENTSYNC_PROCESSING_RESOLUTION_PX, LATENTSYNC_PROVIDER,
    LATENTSYNC_STATED_MINIMUM_VRAM_GB, LatentSyncProvider, LatentSyncRuntimeMetadata,
)

__all__ = [name for name in globals() if not name.startswith("_")]

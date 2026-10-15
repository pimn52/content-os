"""Bounded child request/result contracts, shared without loading ASR."""
import hashlib
import json
from pathlib import Path
import wave
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .models import Sha256
from .asr_recipe import ASR_PARAMETERS

class ASRChildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    model_root: str
    audio_path: str
    audio_sha256: Sha256
    language: str = Field(min_length=1, max_length=30)

    def verify_audio(self):
        from app.providers.runtime_tree import _exact_path
        path = Path(self.audio_path)
        _exact_path(path, directory=False)
        if path != path.resolve() or path.suffix.lower() != ".wav":
            raise ValueError("maintained_asr_input_invalid")
        raw = path.read_bytes() if path.stat().st_size <= ASR_PARAMETERS["input_max_bytes"] else b""
        if not raw or hashlib.sha256(raw).hexdigest() != self.audio_sha256:
            raise ValueError("maintained_asr_input_changed")
        with wave.open(str(path), "rb") as audio:
            if (audio.getnchannels() != 1 or audio.getsampwidth() != 2
                or not 8000 <= audio.getframerate() <= 192000
                or not 0 < audio.getnframes() <= ASR_PARAMETERS["input_max_frames"]
                or len(audio.readframes(audio.getnframes())) != 2*audio.getnframes()):
                raise ValueError("maintained_asr_pcm_invalid")
            return (audio.getnframes()*1000 + audio.getframerate()-1)//audio.getframerate()

class ASRInterval(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=10000)
    @model_validator(mode="after")
    def ordered(self):
        if self.end_ms <= self.start_ms or not self.text.strip():
            raise ValueError("maintained_asr_interval_invalid")
        return self

class ASRChildResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    text: str = Field(max_length=100000)
    language: str | None = Field(default=None, max_length=30)
    segments: tuple[ASRInterval, ...] = Field(max_length=10000)
    words: tuple[ASRInterval, ...] = Field(max_length=10000)

    def transcription(self, duration_ms):
        from app.providers.asr import TranscriptionResult, TranscriptionSegment, TranscriptionWord
        for rows in (self.segments, self.words):
            previous = 0
            for row in rows:
                if row.end_ms > duration_ms or row.start_ms < previous:
                    raise ValueError("maintained_asr_timing_invalid")
                previous = row.start_ms
        if self.text != " ".join(row.text for row in self.segments):
            raise ValueError("maintained_asr_text_invalid")
        return TranscriptionResult(self.text,
            tuple(TranscriptionSegment(**row.model_dump()) for row in self.segments),
            self.language, tuple(TranscriptionWord(**row.model_dump()) for row in self.words))

    @classmethod
    def from_transcription(cls, value):
        return cls.model_validate_json(json.dumps(dict(text=value.text, language=value.language,
            segments=[dict(start_ms=r.start_ms, end_ms=r.end_ms, text=r.text) for r in value.segments],
            words=[dict(start_ms=r.start_ms, end_ms=r.end_ms, text=r.text) for r in value.words])))

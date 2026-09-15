"""Evidence-based promotion of a generated Talking asset for assembly.

The provider produces a pending ``AI_VIDEO`` asset.  Only an independent
container/audio/new-word evaluation may mark its automated QA state verified;
human likeness and visible sync judgment intentionally remain separate.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.domain.models import Asset, AudioAsset, SourceKind
from app.media.ffprobe import ProbeError, ProbeMetadata


class TalkingQaError(ValueError):
    pass


@dataclass(frozen=True)
class TalkingQaReport:
    automated_verified: bool
    checks: tuple[str, ...]
    evidence_reference: str
    playable: bool | None = None
    duration_drift_ms: int | None = None
    copy_coverage: float | None = None
    missing_token_count: int | None = None
    duplicate_token_count: int | None = None
    substitution_token_count: int | None = None

    def __post_init__(self) -> None:
        if not self.checks or any(not isinstance(item, str) or not item.strip() for item in self.checks):
            raise TalkingQaError("Talking QA requires one or more checks")
        if not isinstance(self.evidence_reference, str) or not self.evidence_reference.strip() or len(self.evidence_reference) > 500:
            raise TalkingQaError("Talking QA requires a traceable evidence reference")


@dataclass(frozen=True)
class TalkingHumanReview:
    """A recorded U-Talking decision after an automated technical pass.

    Automated audio/container checks cannot establish visible lip-sync,
    likeness, naturalness, gaze or performance fit.  A rejection must be
    durable so the exact asset cannot silently return to a later render.
    """

    approved: bool
    evidence_reference: str
    findings: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_reference, str) or not self.evidence_reference.strip() or len(self.evidence_reference) > 500:
            raise TalkingQaError("Talking human review requires a traceable evidence reference")
        if not self.findings or any(not isinstance(item, str) or not item.strip() for item in self.findings):
            raise TalkingQaError("Talking human review requires concrete findings")


class TalkingOutputProbe(Protocol):
    def probe(self, path: str | Path) -> ProbeMetadata: ...


def verify_talking_output(
    asset: Asset,
    narration: AudioAsset,
    probe: TalkingOutputProbe,
    *,
    evidence_reference: str,
    duration_tolerance_ms: int = 80,
) -> TalkingQaReport:
    """Run independent container/timing checks on a real Talking output.

    This deliberately does not infer visible lip-sync or creator likeness.
    Those remain the explicit U-Talking gate. Narration copy coverage is also
    owned by the upstream Voice QA record; this function requires that record
    to be verified instead of treating requested text as evidence.
    """
    if not isinstance(asset, Asset) or asset.source_kind is not SourceKind.AI_VIDEO:
        raise TalkingQaError("Talking output QA requires a generated AI video asset")
    generation = asset.metadata.get("talking_generation")
    if not isinstance(generation, dict):
        raise TalkingQaError("Talking output QA only applies to a Talking generation output")
    if not isinstance(narration, AudioAsset):
        raise TalkingQaError("Talking output QA requires a typed narration asset")
    if not isinstance(evidence_reference, str) or not evidence_reference.strip() or len(evidence_reference) > 500:
        raise TalkingQaError("Talking output QA requires a traceable evidence reference")
    if isinstance(duration_tolerance_ms, bool) or not isinstance(duration_tolerance_ms, int) or duration_tolerance_ms < 0:
        raise TalkingQaError("duration_tolerance_ms must be a non-negative integer")

    output = Path(asset.source_file)
    if not output.is_file() or output.stat().st_size == 0:
        return TalkingQaReport(False, ("output_file_missing_or_empty",), evidence_reference.strip(), playable=False)
    try:
        metadata = probe.probe(output)
    except (ProbeError, OSError, ValueError):
        return TalkingQaReport(False, ("output_probe_failed",), evidence_reference.strip(), playable=False)
    streams = metadata.metadata.get("streams") if isinstance(metadata.metadata, dict) else None
    stream_types = {
        stream.get("codec_type")
        for stream in streams
        if isinstance(stream, dict)
    } if isinstance(streams, list) else set()
    playable = metadata.duration_ms > 0 and {"video", "audio"}.issubset(stream_types)
    checks: list[str] = ["output_playable_video_audio" if playable else "output_not_playable_video_audio"]
    duration_drift_ms = abs(metadata.duration_ms - narration.duration_ms)
    checks.append(
        "output_duration_matches_narration"
        if duration_drift_ms <= duration_tolerance_ms
        else "output_duration_drift"
    )
    voice_generation = narration.metadata.get("voice_generation")
    voice_qa = voice_generation.get("qa") if isinstance(voice_generation, dict) else None
    copy_coverage = voice_qa.get("copy_coverage") if isinstance(voice_qa, dict) else None
    missing_token_count = voice_qa.get("missing_token_count") if isinstance(voice_qa, dict) else None
    duplicate_token_count = voice_qa.get("duplicate_token_count") if isinstance(voice_qa, dict) else None
    substitution_token_count = voice_qa.get("substitution_token_count") if isinstance(voice_qa, dict) else None
    voice_verified = isinstance(voice_generation, dict) and voice_generation.get("qa_state") == "verified"
    copy_verified = (
        voice_verified
        and isinstance(copy_coverage, (int, float))
        and not isinstance(copy_coverage, bool)
        and float(copy_coverage) >= 1.0
        and isinstance(missing_token_count, int)
        and not isinstance(missing_token_count, bool)
        and missing_token_count == 0
        and isinstance(duplicate_token_count, int)
        and not isinstance(duplicate_token_count, bool)
        and duplicate_token_count == 0
    )
    checks.append("narration_copy_qa_verified" if copy_verified else "narration_copy_qa_not_verified")
    verified = playable and duration_drift_ms <= duration_tolerance_ms and copy_verified
    return TalkingQaReport(
        verified,
        tuple(checks),
        evidence_reference.strip(),
        playable=playable,
        duration_drift_ms=duration_drift_ms,
        copy_coverage=float(copy_coverage) if isinstance(copy_coverage, (int, float)) and not isinstance(copy_coverage, bool) else None,
        missing_token_count=missing_token_count if isinstance(missing_token_count, int) and not isinstance(missing_token_count, bool) else None,
        duplicate_token_count=duplicate_token_count if isinstance(duplicate_token_count, int) and not isinstance(duplicate_token_count, bool) else None,
        substitution_token_count=substitution_token_count if isinstance(substitution_token_count, int) and not isinstance(substitution_token_count, bool) else None,
    )


def apply_talking_qa(asset: Asset, report: TalkingQaReport) -> Asset:
    """Return an updated generated Talking asset with auditable QA evidence.

    This function cannot create QA for arbitrary media: it accepts only the
    durable result of a Talking-generation handler.  A verified automated
    state permits technical timeline/render use, but records that the human
    likeness/naturalness/visible-sync gate is still pending.
    """
    if not isinstance(asset, Asset) or asset.source_kind is not SourceKind.AI_VIDEO:
        raise TalkingQaError("Talking QA requires a generated AI video asset")
    generation = asset.metadata.get("talking_generation")
    if not isinstance(generation, dict):
        raise TalkingQaError("Talking QA only applies to a Talking generation output")
    metadata = dict(asset.metadata)
    updated_generation = dict(generation)
    updated_generation["qa_state"] = "verified" if report.automated_verified else "failed"
    updated_generation["qa"] = {
        "automated_verified": report.automated_verified,
        "checks": list(report.checks),
        "evidence_reference": report.evidence_reference,
        "human_gate": "U-Talking likeness, naturalness, and visible lip-sync judgment required",
    }
    if report.playable is not None:
        updated_generation["qa"]["playable"] = report.playable
    if report.duration_drift_ms is not None:
        updated_generation["qa"]["duration_drift_ms"] = report.duration_drift_ms
    if report.copy_coverage is not None:
        updated_generation["qa"]["copy_coverage"] = report.copy_coverage
    if report.missing_token_count is not None:
        updated_generation["qa"]["missing_token_count"] = report.missing_token_count
    if report.duplicate_token_count is not None:
        updated_generation["qa"]["duplicate_token_count"] = report.duplicate_token_count
    if report.substitution_token_count is not None:
        updated_generation["qa"]["substitution_token_count"] = report.substitution_token_count
    metadata["talking_generation"] = updated_generation
    return asset.model_copy(update={"metadata": metadata})


def apply_talking_human_review(asset: Asset, review: TalkingHumanReview) -> Asset:
    """Persist the U-Talking decision without changing automated evidence."""
    if not isinstance(asset, Asset) or asset.source_kind is not SourceKind.AI_VIDEO:
        raise TalkingQaError("Talking human review requires a generated AI video asset")
    generation = asset.metadata.get("talking_generation")
    if not isinstance(generation, dict) or generation.get("qa_state") != "verified":
        raise TalkingQaError("Talking human review requires verified automated Talking QA")
    metadata = dict(asset.metadata)
    updated_generation = dict(generation)
    updated_generation["human_review_state"] = "approved" if review.approved else "rejected"
    updated_generation["human_review"] = {
        "approved": review.approved,
        "evidence_reference": review.evidence_reference,
        "findings": list(review.findings),
    }
    metadata["talking_generation"] = updated_generation
    return asset.model_copy(update={"metadata": metadata})

"""Evidence-based promotion of a generated Talking asset for assembly.

The provider produces a pending ``AI_VIDEO`` asset.  Only an independent
container/audio/new-word evaluation may mark its automated QA state verified;
human likeness and visible sync judgment intentionally remain separate.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.models import Asset, SourceKind


class TalkingQaError(ValueError):
    pass


@dataclass(frozen=True)
class TalkingQaReport:
    automated_verified: bool
    checks: tuple[str, ...]
    evidence_reference: str

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

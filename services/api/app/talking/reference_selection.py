"""Choose a suitable existing creator performance before asking a model to lip-sync."""
from __future__ import annotations

from collections.abc import Sequence

from app.domain.models import Clip, GazeDirection, TalkingPerformanceBrief, TalkingReferenceFit, TalkingReferenceSelection


def select_talking_reference(
    references: Sequence[Clip],
    brief: TalkingPerformanceBrief,
) -> TalkingReferenceSelection:
    """Return the strongest evidenced reference, otherwise a small capture gap.

    A missing assessment is not a pass.  This prevents an old, side-on or
    subtitle-burned clip from being silently substituted for a requested
    direct-to-camera performance.  The result is selection policy only: it
    does not claim that a lip-sync provider can manufacture gaze or emotion.
    """
    if isinstance(references, (str, bytes)) or not references:
        raise ValueError("Talking reference selection requires one or more Clips")
    if not isinstance(brief, TalkingPerformanceBrief):
        raise TypeError("Talking reference selection requires a TalkingPerformanceBrief")
    fits = [_fit(reference, brief) for reference in references]
    eligible = [fit for fit in fits if fit.eligible]
    if eligible:
        selected = max(eligible, key=lambda fit: (fit.score, str(fit.clip_id)))
        return TalkingReferenceSelection(fits=fits, selected_clip_id=selected.clip_id)
    return TalkingReferenceSelection(
        fits=fits,
        requires_capture=True,
        capture_instruction=_capture_instruction(brief),
    )


def _fit(clip: Clip, brief: TalkingPerformanceBrief) -> TalkingReferenceFit:
    if not isinstance(clip, Clip):
        raise TypeError("Talking references must be Clip contracts")
    reasons: list[str] = []
    if not clip.talking_candidate:
        reasons.append("The clip is not marked as a Talking candidate.")
    duration_ms = clip.end_ms - clip.start_ms
    if duration_ms < brief.minimum_reference_duration_ms:
        reasons.append(f"Reference duration {duration_ms}ms is below the requested {brief.minimum_reference_duration_ms}ms.")
    if clip.face_visibility is None or clip.face_visibility < 0.6:
        reasons.append("Face visibility is not sufficient evidence for a Talking reference.")
    if clip.mouth_visibility is None or clip.mouth_visibility < 0.6:
        reasons.append("Mouth visibility is not sufficient evidence for lip-sync.")
    assessment = clip.talking_reference_assessment
    if assessment is None:
        reasons.append("No start/end gaze, expression, or subtitle assessment is recorded.")
    else:
        _require_gaze(reasons, "start", brief.start_gaze, assessment.start_gaze)
        _require_gaze(reasons, "end", brief.end_gaze, assessment.end_gaze)
        if brief.require_no_burned_subtitles and assessment.burned_in_subtitles is not False:
            reasons.append("The reference is not verified free of burned-in subtitles.")
        desired = {label.casefold() for label in brief.expression_labels}
        observed = {label.casefold() for label in assessment.expression_labels}
        missing = sorted(desired - observed)
        if missing:
            reasons.append("Missing requested expression labels: " + ", ".join(missing) + ".")
    if reasons:
        return TalkingReferenceFit(clip_id=clip.id, eligible=False, reasons=reasons)
    quality = 0.5 if clip.quality_score is None else clip.quality_score
    face = 0.6 if clip.face_visibility is None else clip.face_visibility
    mouth = 0.6 if clip.mouth_visibility is None else clip.mouth_visibility
    return TalkingReferenceFit(
        clip_id=clip.id,
        eligible=True,
        score=min(1.0, quality * 0.4 + face * 0.3 + mouth * 0.3),
        reasons=["Meets the requested performance brief with recorded source evidence."],
    )


def _require_gaze(
    reasons: list[str],
    point: str,
    requested: GazeDirection | None,
    observed: GazeDirection | None,
) -> None:
    if requested is None:
        return
    if observed is not requested:
        reasons.append(f"The {point} gaze is not verified as {requested.value}.")


def _capture_instruction(brief: TalkingPerformanceBrief) -> str:
    details = [f"Record at least {brief.minimum_reference_duration_ms / 1000:g} seconds"]
    if brief.start_gaze is not None:
        details.append(f"start looking {brief.start_gaze.value}")
    if brief.end_gaze is not None:
        details.append(f"end looking {brief.end_gaze.value}")
    if brief.expression_labels:
        details.append("keep the expression " + ", ".join(brief.expression_labels))
    if brief.require_no_burned_subtitles:
        details.append("do not include burned-in subtitles")
    return "; ".join(details) + "."

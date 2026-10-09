"""One read-only production eligibility check for generated Talking visuals."""
from __future__ import annotations

from pathlib import Path
from uuid import UUID

from app.db import AssetRepository, AudioAssetRepository, ClipRepository, TalkingRunRepository, JobRepository
from app.domain.models import Asset, AudioAsset, Clip, SourceKind, TalkingRun, TranscriptSegment, TalkingGenerationJobPayload


def talking_visual_blocker(
    asset: Asset,
    clip: Clip,
    assets: AssetRepository,
    *,
    project_id: UUID,
    copy: str,
    master_audio_id: UUID | None = None,
    selected_duration_ms: int | None = None,
) -> str | None:
    """Return a stable blocker; only an admitted, context-matching Run is eligible.

    Generated child clips are review evidence, not a production TalkingRun.
    Persisted Run identity is checked instead of trusting copied Asset metadata.
    """
    if asset.source_kind is not SourceKind.AI_VIDEO:
        return None
    if clip.asset_id != asset.id or not asset.authorization_reference.strip():
        return "generated_talking_asset_identity_mismatch"
    metadata = asset.metadata.get("talking_run")
    if not isinstance(metadata, dict):
        generation = asset.metadata.get("talking_generation")
        if not isinstance(generation, dict):
            return "generated_talking_provenance_missing"
        if generation.get("qa_state") != "verified":
            return "generated_talking_qa_not_verified"
        if generation.get("human_review_state") != "approved":
            return "generated_talking_human_review_not_approved"
        return "generated_talking_run_admission_required"
    if any(metadata.get(key) != value for key, value in (
        ("admission_state", "admitted"),
        ("automated_qa_state", "verified"),
        ("continuity_review_state", "approved"),
    )):
        return "talking_run_not_admitted"
    try:
        run_id = UUID(str(metadata.get("run_id")))
    except (ValueError, TypeError, AttributeError):
        return "talking_run_identity_missing"
    run = TalkingRunRepository(assets.db).get(run_id)
    if run is None or not _run_metadata_matches(run, metadata):
        return "talking_run_record_mismatch"
    planned_child = any((job := JobRepository(assets.db).get(child.job_id)) is not None
                        and isinstance(job.payload, TalkingGenerationJobPayload) and job.payload.planned_context is not None
                        for child in run.child_evidence)
    if run.planned_origin_sha256 is not None or run.review_policy_version == 2 or planned_child:
        from app.db import TalkingSliceSeriesContinuityReviewRepository, TalkingSliceSeriesRepository
        from app.production_runs import ProductionRunService, _snapshot_sha256
        from app.talking.planned_admission import PlannedTalkingAdmissionError, PlannedTalkingAdmissionService
        from app.talking.review_policy import TalkingReviewPolicyService

        series = TalkingSliceSeriesRepository(assets.db).get(run.series_id)
        review = TalkingSliceSeriesContinuityReviewRepository(assets.db).get_by_series_id(run.series_id)
        if series is None or series.planned_origin is None or review is None or not review.approved:
            return "talking_run_planned_review_missing"
        data_root = Path(getattr(assets.db, "data_root", Path(assets.db.path).parent)).resolve()
        production = ProductionRunService(assets.db, data_root).get(series.project_id, series.planned_origin.run_id)
        if production is None or production.status not in {
            "awaiting_talking_dependencies", "render_pending", "render_running",
            "render_failed", "render_completed_awaiting_review",
        } or not any(
            item.scene_plan_id == series.planned_origin.scene_plan_id and item.talking_run_id == run.id
            for item in production.talking_source_bindings
        ):
            return "talking_run_planned_binding_mismatch"
        try:
            candidate, qa_sha = PlannedTalkingAdmissionService(assets.db, data_root).require_current_candidate(series)
            TalkingReviewPolicyService(assets.db).require_admitted_review(series, candidate, review, run)
        except (PlannedTalkingAdmissionError, ValueError, OSError):
            return "talking_run_planned_origin_stale"
        if (candidate.id != run.assembled_asset_id
            or review.id != run.continuity_review_id
            or review.preview_asset_id != candidate.id
            or review.preview_sha256 != candidate.content_hash
            or review.preview_qa_sha256 != qa_sha
            or review.planned_origin_sha256 != _snapshot_sha256(series.planned_origin.model_dump(mode="json"))
            or run.planned_origin_sha256 != review.planned_origin_sha256
            or run.reviewed_preview_sha256 != candidate.content_hash
            or metadata.get("planned_origin_sha256") != run.planned_origin_sha256
            or metadata.get("reviewed_preview_sha256") != candidate.content_hash):
            return "talking_run_planned_review_mismatch"
    if run.project_id != project_id:
        return "talking_run_project_mismatch"
    if master_audio_id is not None and run.master_narration_audio_id != master_audio_id:
        return "talking_run_master_mismatch"
    source = assets.get(run.assembled_asset_id)
    source_clip = ClipRepository(assets.db).get(run.assembled_clip_id)
    if (
        source is None or source_clip is None or source_clip.asset_id != source.id
        or source_clip.start_ms != 0 or source_clip.end_ms != source.duration_ms
    ):
        return "talking_run_source_missing"
    if (
        source.source_kind is not SourceKind.AI_VIDEO
        or source.authorization_reference != asset.authorization_reference
        or source.metadata.get("talking_run") != metadata
    ):
        return "talking_run_source_identity_mismatch"
    if asset.id == source.id:
        if clip.id != run.assembled_clip_id:
            return "talking_run_clip_mismatch"
        start_ms, end_ms = run.master_start_ms, run.master_end_ms
    else:
        derivation = asset.metadata.get("source_derivation")
        if not isinstance(derivation, dict) or any(derivation.get(key) != value for key, value in (
            ("source_asset_id", str(source.id)),
            ("source_clip_id", str(source_clip.id)),
            ("source_content_hash", source.content_hash),
            ("source_authorization_reference", source.authorization_reference),
        )):
            return "talking_run_derivation_mismatch"
        interval = derivation.get("source_interval")
        if not isinstance(interval, dict):
            return "talking_run_derivation_mismatch"
        offset_start, offset_end = interval.get("start_ms"), interval.get("end_ms")
        if (
            isinstance(offset_start, bool) or isinstance(offset_end, bool)
            or not isinstance(offset_start, int) or not isinstance(offset_end, int)
            or offset_start < 0 or offset_end <= offset_start
            or offset_end > source.duration_ms
            or clip.start_ms != 0 or clip.end_ms != asset.duration_ms
        ):
            return "talking_run_derivation_mismatch"
        start_ms = run.master_start_ms + offset_start
        end_ms = run.master_start_ms + offset_end
        if end_ms > run.master_end_ms:
            return "talking_run_derivation_mismatch"
    audio = AudioAssetRepository(assets.db).get(run.master_narration_audio_id)
    if audio is None:
        return "talking_run_master_missing"
    voice = audio.metadata.get("voice_generation")
    if isinstance(voice, dict) and (voice.get("qa_state") != "verified" or _voice_human_review_status(audio) != "approved"):
        return "talking_run_master_not_approved"
    if not _matches_interval_copy(copy, audio.transcript_segments, start_ms, end_ms):
        return "talking_run_copy_mismatch"
    if selected_duration_ms is not None and selected_duration_ms < max(
        segment.end_ms - start_ms for segment in audio.transcript_segments
        if segment.start_ms >= start_ms and segment.end_ms <= end_ms
    ):
        return "talking_run_speech_would_be_cut"
    return None


def _run_metadata_matches(run: TalkingRun, metadata: dict[str, object]) -> bool:
    return (
        run.admission_state == "admitted"
        and run.automated_qa_state == "verified"
        and metadata.get("series_id") == str(run.series_id)
        and metadata.get("master_narration_audio_id") == str(run.master_narration_audio_id)
        and metadata.get("continuity_review_id") == str(run.continuity_review_id)
        and metadata.get("master_start_ms") == run.master_start_ms
        and metadata.get("master_end_ms") == run.master_end_ms
        and metadata.get("review_policy_version", 1) == run.review_policy_version
    )


def _matches_interval_copy(copy: str, segments: list[TranscriptSegment], start_ms: int, end_ms: int) -> bool:
    from app.voice_qa import comparison_tokens

    target = "".join(comparison_tokens(copy))
    covered = [
        segment for segment in segments
        if segment.start_ms >= start_ms and segment.end_ms <= end_ms
    ]
    spoken = "".join(comparison_tokens(" ".join(segment.text for segment in covered)))
    return bool(target and spoken and target == spoken)


def _voice_human_review_status(audio: AudioAsset) -> str:
    from app.voice_qa import voice_human_review_status

    return voice_human_review_status(audio)


__all__ = ["talking_visual_blocker"]

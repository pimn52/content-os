"""One read-only production eligibility check for generated Talking visuals."""
from __future__ import annotations

from uuid import UUID

from app.db import AssetRepository, AudioAssetRepository, ClipRepository, TalkingRunRepository
from app.domain.models import Asset, AudioAsset, Clip, SourceKind, TalkingRun, TranscriptSegment


def talking_visual_blocker(
    asset: Asset,
    clip: Clip,
    assets: AssetRepository,
    *,
    project_id: UUID,
    copy: str,
    master_audio_id: UUID | None = None,
    selected_duration_ms: int | None = None,
    master_interval: tuple[int, int] | None = None,
    production_run_id: UUID | None = None,
) -> str | None:
    """Return a stable blocker; only an admitted, context-matching Run is eligible.

    Generated child clips are review evidence, not a production TalkingRun.
    Persisted Run identity is checked instead of trusting copied Asset metadata.
    """
    if asset.source_kind is not SourceKind.AI_VIDEO:
        return None
    if clip.asset_id != asset.id:
        return "generated_talking_asset_identity_mismatch"
    metadata = asset.metadata.get("talking_run")
    if not isinstance(metadata, dict):
        generation = asset.metadata.get("talking_generation")
        if not isinstance(generation, dict):
            return "generated_talking_provenance_missing"
        if generation.get("qa_state") != "verified":
            return "generated_talking_qa_not_verified"
        if production_run_id is not None:
            from pathlib import Path
            from app.production_runs import ProductionRunNotReady, ProductionRunService
            root = getattr(assets.db, "data_root", Path(assets.db.path).parent)
            try:
                ProductionRunService(assets.db, root).require_draft_talking_visual(
                    project_id, production_run_id, asset, clip, copy=copy,
                    master_audio_id=master_audio_id, selected_duration_ms=selected_duration_ms,
                    master_interval=master_interval,
                )
            except ProductionRunNotReady as exc:
                return exc.reasons[0]
            except ValueError:
                return "draft_talking_context_invalid"
            return None
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
    if run.project_id != project_id:
        return "talking_run_project_mismatch"
    if master_audio_id is not None and run.master_narration_audio_id != master_audio_id:
        return "talking_run_master_mismatch"
    from app.db import TalkingSliceSeriesContinuityReviewRepository
    from app.talking.review_policy import TalkingReviewPolicyService

    from app.db import TalkingSliceSeriesRepository
    series = TalkingSliceSeriesRepository(assets.db).get(run.series_id)
    if series is None:
        return "talking_run_source_missing"
    if series.planned_origin is not None:
        from pathlib import Path
        root = getattr(assets.db,"data_root",Path(assets.db.path).parent)
        origin = series.planned_origin
        if origin.source_admission_id is not None:
            from app.talking.source_admission import TalkingSourceAdmissionRepository
            permission = TalkingSourceAdmissionRepository(assets.db,root).get(origin.source_admission_id)
            if permission is None or not permission.active:
                return "talking_source_permission_revoked_or_missing"
        else:
            from app.production_runs import ProductionRunNotReady, ProductionRunService
            try:
                ProductionRunService(assets.db, root).require_current_planned_run_origin(
                    project_id, origin.run_id, origin.scene_plan_id, expected=origin, admitted_run_id=run.id,
                )
            except ProductionRunNotReady as exc:
                return exc.reasons[0]
            except (ValueError, OSError):
                return "talking_run_source_context_invalid"
    review = TalkingSliceSeriesContinuityReviewRepository(assets.db).get_by_series_id(run.series_id)
    if (review is None or review.id != run.continuity_review_id or not review.approved
        or review.project_id != project_id or review.review_policy_version != run.review_policy_version
        or review.child_job_ids != [child.job_id for child in run.child_evidence]):
        return "talking_run_review_mismatch"
    if run.review_policy_version == 2 and (review.dimensions is None or any(
        value != "pass" for value in review.dimensions.model_dump(exclude={"schema_version"}).values()
    )):
        return "talking_run_review_mismatch"
    try:
        TalkingReviewPolicyService(assets.db).require_clear(project_id, asset.content_hash)
    except ValueError as exc:
        return str(exc)
    source = assets.get(run.assembled_asset_id)
    source_clip = ClipRepository(assets.db).get(run.assembled_clip_id)
    if (
        source is None or source_clip is None or source_clip.asset_id != source.id
        or source_clip.start_ms != 0 or source_clip.end_ms != source.duration_ms
    ):
        return "talking_run_source_missing"
    if (
        source.source_kind is not SourceKind.AI_VIDEO
        or source.metadata.get("talking_run") != metadata
    ):
        return "talking_run_source_identity_mismatch"
    if review.preview_sha256 is not None and (review.preview_asset_id != source.id
        or review.preview_sha256 != source.content_hash):
        return "talking_run_review_mismatch"
    if review.preview_sha256 is not None:
        from pathlib import Path
        from app.media.audio_access import file_digest
        from app.media.local_paths import existing_local_media_file
        root = getattr(assets.db,"data_root",Path(assets.db.path).parent)
        try:
            if file_digest(existing_local_media_file(source.source_file,root)) != review.preview_sha256:
                return "talking_run_reviewed_bytes_changed"
        except (ValueError,OSError):
            return "talking_run_reviewed_bytes_changed"
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
    from app.assembly.video_spec import _require_generated_voice_qa
    try:
        _require_generated_voice_qa(audio,assets.db)
    except ValueError:
        return "talking_run_master_not_approved"
    if master_interval is not None:
        first, last = master_interval
        if first < start_ms or last > end_ms or last <= first:
            return "talking_run_interval_mismatch"
        start_ms, end_ms = first, last
    if not _matches_interval_copy(copy, audio.transcript_segments, start_ms, end_ms, audio=audio):
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


def _matches_interval_copy(copy: str, segments: list[TranscriptSegment], start_ms: int, end_ms: int, *, audio: AudioAsset | None = None) -> bool:
    from app.voice_qa import comparison_tokens

    target = "".join(comparison_tokens(copy))
    covered = [
        segment for segment in segments
        if segment.start_ms >= start_ms and segment.end_ms <= end_ms
    ]
    spoken = "".join(comparison_tokens(" ".join(segment.text for segment in covered)))
    if target and spoken and target == spoken:
        return True
    # 已核验旁白可沿用有界 ASR 同音替换的时间轴，不能把新文案塞进旧音频。
    if not target or not covered or audio is None:
        return False
    from app.voice_qa import verified_generated_copy_tokens
    expected = verified_generated_copy_tokens(audio)
    if expected is None or segments != audio.transcript_segments:
        return False
    first = sum(len(comparison_tokens(segment.text)) for segment in segments if segment.end_ms <= start_ms)
    size = sum(len(comparison_tokens(segment.text)) for segment in covered)
    return tuple(comparison_tokens(copy)) == expected[first:first + size]



__all__ = ["talking_visual_blocker"]

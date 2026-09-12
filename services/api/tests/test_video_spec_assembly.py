from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.assembly import (
    AssetIdentityMismatch,
    CaptureGapSelected,
    CandidateNotFound,
    InsufficientSourceDuration,
    InvalidCandidateSelection,
    NarrationTimelineError,
    VideoSpecAssembler,
    milliseconds_to_frames,
)
from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, ImageAssetRepository
from app.domain.models import (
    Asset,
    AudioAsset,
    CandidateAsset,
    Clip,
    CostCategory,
    ImageAsset,
    Project,
    RationalFps,
    ScenePlan,
    SourceKind,
    TranscriptSegment,
    UsageCost,
    VideoSpec,
    VisualIntent,
)


NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def _project(fps: RationalFps) -> Project:
    return Project(
        ip_profile_id=uuid4(), title="Assembly project", topic="Creator software workflow", fps=fps, created_at=NOW,
    )


def _scene(project: Project, number: int, duration_ms: int = 1_001) -> ScenePlan:
    return ScenePlan(
        project_id=project.id, scene_id=f"scene_{number:02d}", order=number,
        purpose="hook" if number == 0 else "explain", voice_text=f"Voice text for scene {number}.",
        duration_target_ms=duration_ms,
        visual_intent=VisualIntent(subject="creator", action="operating software", framing="close"),
        preferred_sources=[SourceKind.USER_ASSET], fallback_sources=[SourceKind.CAPTURE], caption_emphasis=["workflow"],
    )


def _asset(index: int, source_kind: SourceKind = SourceKind.USER_ASSET) -> Asset:
    return Asset(
        source_kind=source_kind, source_file=f"C:/media/source {index}.mp4", content_hash=(str(index) * 64)[:64],
        duration_ms=3_000, width=1080, height=1920, fps=RationalFps(numerator=30_000, denominator=1_001),
        authorization_reference=f"creator-rights-{index}", imported_at=NOW,
    )


def _clip(asset: Asset, start_ms: int = 0, end_ms: int = 1_500) -> Clip:
    return Clip(asset_id=asset.id, start_ms=start_ms, end_ms=end_ms, asset_duration_ms=asset.duration_ms)


def _candidate(scene: ScenePlan, asset: Asset, clip: Clip, *, recommended: bool = True) -> CandidateAsset:
    category = CostCategory.USER_ASSET if asset.source_kind == SourceKind.USER_ASSET else CostCategory.HISTORICAL_ASSET
    return CandidateAsset(
        scene_plan_id=scene.id, source_kind=asset.source_kind, asset_id=asset.id, clip_id=clip.id,
        match_score=0.9, why=["local continuous clip"], recommended=recommended,
        estimated_cost=UsageCost(category=category, amount=Decimal("0"), currency="USD"),
    )


def _setup(tmp_path: Path, fps: RationalFps) -> tuple[Database, VideoSpecAssembler, Project, list[ScenePlan], list[Asset], list[Clip]]:
    db = Database(tmp_path / "assembly.sqlite")
    asset_repo, clip_repo = AssetRepository(db), ClipRepository(db)
    assets = [_asset(1), _asset(2)]
    clips = [_clip(assets[0]), _clip(assets[1], 500, 2_000)]
    for asset in assets:
        asset_repo.create(asset)
    for clip in clips:
        clip_repo.create(clip)
    project = _project(fps)
    scenes = [_scene(project, 0), _scene(project, 1)]
    return db, VideoSpecAssembler(asset_repo, clip_repo), project, scenes, assets, clips


@pytest.mark.parametrize(
    ("fps", "expected_frames"),
    [(RationalFps(numerator=30_000, denominator=1_001), 30), (RationalFps(numerator=30, denominator=1), 31)],
)
def test_assemble_exact_rational_frame_timeline_and_round_trip(
    tmp_path: Path, fps: RationalFps, expected_frames: int
) -> None:
    db, assembler, project, scenes, assets, clips = _setup(tmp_path, fps)
    try:
        # Input order is intentionally reversed; ScenePlan.order owns timeline order.
        selected = {scenes[0].id: _candidate(scenes[0], assets[0], clips[0]), scenes[1].id: _candidate(scenes[1], assets[1], clips[1])}
        spec = assembler.assemble(project, list(reversed(scenes)), selected)

        assert [scene.scene_id for scene in spec.scenes] == ["scene_00", "scene_01"]
        assert [scene.start_frame for scene in spec.scenes] == [0, expected_frames]
        assert [scene.duration_frames for scene in spec.scenes] == [expected_frames, expected_frames]
        assert spec.scenes[0].caption == scenes[0].voice_text
        assert spec.scenes[0].visual.authorization_reference == assets[0].authorization_reference
        assert spec.scenes[1].visual.clip_start_ms == clips[1].start_ms
        assert spec.estimated_cost is not None and spec.estimated_cost.amount == 0 and spec.estimated_cost.currency == "USD"
        assert VideoSpec.model_validate_json(spec.model_dump_json()) == spec
        assert assembler.assemble(project, scenes, selected).model_dump(mode="json") == spec.model_dump(mode="json")
    finally:
        db.close()


def test_milliseconds_to_frames_is_exact_and_source_boundary_is_enforced(tmp_path: Path) -> None:
    ntsc = RationalFps(numerator=30_000, denominator=1_001)
    assert milliseconds_to_frames(1_001, ntsc) == 30
    assert milliseconds_to_frames(1_001, RationalFps(numerator=30, denominator=1)) == 31

    db, assembler, project, scenes, assets, clips = _setup(tmp_path, ntsc)
    try:
        boundary_scene = _scene(project, 0, duration_ms=1_500)
        valid = assembler.assemble(project, [boundary_scene], {boundary_scene.id: _candidate(boundary_scene, assets[0], clips[0])})
        assert valid.scenes[0].visual.clip_end_ms == 1_500
        short_scene = _scene(project, 0, duration_ms=1_501)
        with pytest.raises(InsufficientSourceDuration, match="target duration"):
            assembler.assemble(project, [short_scene], {short_scene.id: _candidate(short_scene, assets[0], clips[0])})
    finally:
        db.close()


def test_assembler_rejects_capture_nonrecommended_and_identity_mismatch(tmp_path: Path) -> None:
    db, assembler, project, scenes, assets, clips = _setup(tmp_path, RationalFps(numerator=30, denominator=1))
    try:
        scene = scenes[0]
        capture = CandidateAsset(
            scene_plan_id=scene.id, source_kind=SourceKind.CAPTURE, match_score=0,
            why=["capture gap"], recommended=True, requires_capture=True, estimated_cost=UsageCost(category=CostCategory.CAPTURE),
        )
        with pytest.raises(CaptureGapSelected):
            assembler.assemble(project, [scene], {scene.id: capture})

        alternate = _candidate(scene, assets[0], clips[0], recommended=False)
        with pytest.raises(InvalidCandidateSelection, match="recommended or explicit"):
            assembler.assemble(project, [scene], {scene.id: alternate})
        assert assembler.assemble(project, [scene], {scene.id: alternate}, explicit_scene_ids=[scene.id]).scenes[0].visual.clip_id == clips[0].id

        mismatched = _candidate(scene, assets[1], clips[0])
        with pytest.raises(AssetIdentityMismatch, match="do not match"):
            assembler.assemble(project, [scene], {scene.id: mismatched})
    finally:
        db.close()


def test_assembler_builds_local_typography_fallback_without_media(tmp_path: Path) -> None:
    db, assembler, project, scenes, _, _ = _setup(tmp_path, RationalFps(numerator=30, denominator=1))
    try:
        scene = scenes[0].model_copy(update={
            "preferred_sources": [SourceKind.TYPOGRAPHY],
            "fallback_sources": [SourceKind.CAPTURE],
        })
        candidate = CandidateAsset(
            scene_plan_id=scene.id,
            source_kind=SourceKind.TYPOGRAPHY,
            match_score=0,
            why=["editable local fallback"],
            recommended=True,
            estimated_cost=UsageCost(category=CostCategory.TYPOGRAPHY, amount=Decimal("0"), currency="USD"),
        )
        spec = assembler.assemble(project, [scene], {scene.id: candidate})
        visual = spec.scenes[0].visual
        assert visual.source_kind is SourceKind.TYPOGRAPHY
        assert visual.asset_id is None and visual.clip_id is None
        assert visual.authorization_reference == "local-typography"
    finally:
        db.close()


def test_assembler_builds_imported_static_image_visual(tmp_path: Path) -> None:
    db, _, project, scenes, _, _ = _setup(tmp_path, RationalFps(numerator=30, denominator=1))
    try:
        image = ImageAsset(
            source_kind=SourceKind.SCREENSHOT,
            source_file=str(tmp_path / "screen.png"),
            content_hash="b" * 64,
            width=800,
            height=600,
            authorization_reference="creator-screen",
            imported_at=NOW,
        )
        ImageAssetRepository(db).create(image)
        scene = scenes[0].model_copy(update={"preferred_sources": [SourceKind.SCREENSHOT], "fallback_sources": [SourceKind.CAPTURE]})
        candidate = CandidateAsset(
            scene_plan_id=scene.id,
            source_kind=SourceKind.SCREENSHOT,
            asset_id=image.id,
            match_score=0,
            why=["explicit static fallback"],
            recommended=True,
            estimated_cost=UsageCost(category=CostCategory.SCREENSHOT, amount=Decimal("0"), currency="USD"),
        )
        spec = VideoSpecAssembler(AssetRepository(db), ClipRepository(db), ImageAssetRepository(db)).assemble(project, [scene], {scene.id: candidate})
        assert spec.scenes[0].visual.asset_id == image.id
        assert spec.scenes[0].visual.source_kind is SourceKind.SCREENSHOT
    finally:
        db.close()


def test_assembler_attaches_existing_authorized_narration_per_scene(tmp_path: Path) -> None:
    db, _, project, scenes, assets, clips = _setup(tmp_path, RationalFps(numerator=30, denominator=1))
    audio_source = tmp_path / "voice.wav"
    audio_source.write_bytes(b"voice")
    audio = AudioAsset(
        source_kind=SourceKind.USER_ASSET, source_file=str(audio_source), content_hash="e" * 64,
        duration_ms=2_000, sample_rate=48_000, channels=1, authorization_reference="creator-voice", imported_at=NOW,
    )
    AudioAssetRepository(db).create(audio)
    try:
        assembler = VideoSpecAssembler(
            AssetRepository(db), ClipRepository(db), audios=AudioAssetRepository(db),
        )
        scene = scenes[0]
        spec = assembler.assemble(
            project, [scene], {scene.id: _candidate(scene, assets[0], clips[0])},
            narration_asset_ids={scene.id: audio.id},
        )
        assert spec.scenes[0].narration_asset_id == audio.id
        assert spec.scenes[0].narration_start_ms is None
        with pytest.raises(CandidateNotFound, match="narration audio"):
            assembler.assemble(
                project, [scene], {scene.id: _candidate(scene, assets[0], clips[0])},
                narration_asset_ids={scene.id: uuid4()},
            )
    finally:
        db.close()


def test_legacy_single_scene_narration_uses_measured_audio_duration(tmp_path: Path) -> None:
    db, _, project, scenes, assets, clips = _setup(tmp_path, RationalFps(numerator=30, denominator=1))
    audio_source = tmp_path / "scene-01.wav"
    audio_source.write_bytes(b"voice")
    audio = AudioAsset(
        source_kind=SourceKind.USER_ASSET, source_file=str(audio_source), content_hash="d" * 64,
        duration_ms=800, sample_rate=48_000, channels=1, authorization_reference="creator-voice", imported_at=NOW,
        transcript_segments=[
            TranscriptSegment(start_ms=0, end_ms=300, text="first narration"),
            TranscriptSegment(start_ms=350, end_ms=800, text="second narration"),
        ],
        transcript_source="narration.srt",
    )
    AudioAssetRepository(db).create(audio)
    try:
        assembler = VideoSpecAssembler(
            AssetRepository(db), ClipRepository(db), audios=AudioAssetRepository(db),
        )
        scene = scenes[0]
        spec = assembler.assemble(
            project, [scene], {scene.id: _candidate(scene, assets[0], clips[0])},
            narration_asset_ids={scene.id: audio.id}, narration_required=True,
        )
        rendered_scene = spec.scenes[0]
        assert rendered_scene.duration_frames == 24
        assert rendered_scene.narration_start_ms == 0
        assert rendered_scene.narration_end_ms == 800
        assert rendered_scene.visual.clip_start_ms == clips[0].start_ms
        assert rendered_scene.visual.clip_end_ms == clips[0].start_ms + 800
        assert [(caption.start_ms, caption.end_ms) for caption in rendered_scene.captions] == [(0, 300), (350, 800)]

    finally:
        db.close()


@pytest.mark.parametrize("fps", [RationalFps(numerator=30, denominator=1), RationalFps(numerator=30_000, denominator=1_001)])
def test_master_narration_uses_one_aligned_audio_for_multiple_scenes_and_fills_short_visual(
    tmp_path: Path, fps: RationalFps
) -> None:
    db, _, project, scenes, assets, clips = _setup(tmp_path, fps)
    audio_source = tmp_path / "full-narration.wav"
    audio_source.write_bytes(b"voice")
    audio = AudioAsset(
        source_kind=SourceKind.USER_ASSET, source_file=str(audio_source), content_hash="9" * 64,
        duration_ms=3_500, sample_rate=48_000, channels=1, authorization_reference="creator-voice", imported_at=NOW,
        transcript_segments=[
            TranscriptSegment(start_ms=0, end_ms=700, text="First new line."),
            TranscriptSegment(start_ms=800, end_ms=3_200, text="Second new line."),
        ],
        transcript_source="provider-alignment:local-test",
    )
    AudioAssetRepository(db).create(audio)
    try:
        assembler = VideoSpecAssembler(AssetRepository(db), ClipRepository(db), audios=AudioAssetRepository(db))
        first = scenes[0].model_copy(update={"voice_text": "First new line."})
        second = scenes[1].model_copy(update={"voice_text": "Second new line."})
        spec = assembler.assemble(
            project,
            [first, second],
            {first.id: _candidate(first, assets[0], clips[0]), second.id: _candidate(second, assets[1], clips[1])},
            master_narration_asset_id=audio.id,
            narration_required=True,
        )

        assert spec.master_narration is not None
        assert spec.master_narration.audio_asset_id == audio.id
        assert [scene.scene_id for scene in spec.scenes] == ["scene_00", "scene_01", "scene_01--1-fill-1"]
        intervals = [(scene.narration_start_ms, scene.narration_end_ms) for scene in spec.scenes]
        assert intervals[0] == (0, 700)
        assert all(start == previous_end for (_, previous_end), (start, _) in zip(intervals, intervals[1:]))
        assert intervals[-1][1] == audio.duration_ms
        assert all(end > start for start, end in intervals)
        assert spec.scenes[1].visual.source_kind is SourceKind.USER_ASSET
        assert spec.scenes[2].visual.source_kind is SourceKind.TYPOGRAPHY
        tail = spec.scenes[2]
        expected_tail_caption_end = min(3_200, tail.narration_end_ms) - max(800, tail.narration_start_ms)
        assert [(caption.start_ms, caption.end_ms) for caption in tail.captions] == [(0, expected_tail_caption_end)]
        assert spec.scenes[-1].start_frame + spec.scenes[-1].duration_frames == milliseconds_to_frames(audio.duration_ms, fps)
    finally:
        db.close()


def test_master_narration_rejects_audio_without_actual_timing(tmp_path: Path) -> None:
    db, _, project, scenes, assets, clips = _setup(tmp_path, RationalFps(numerator=30, denominator=1))
    audio_source = tmp_path / "unmapped.wav"
    audio_source.write_bytes(b"voice")
    audio = AudioAsset(
        source_kind=SourceKind.USER_ASSET, source_file=str(audio_source), content_hash="a" * 64,
        duration_ms=1_000, sample_rate=48_000, channels=1, authorization_reference="creator-voice", imported_at=NOW,
    )
    AudioAssetRepository(db).create(audio)
    try:
        assembler = VideoSpecAssembler(AssetRepository(db), ClipRepository(db), audios=AudioAssetRepository(db))
        scene = scenes[0]
        with pytest.raises(NarrationTimelineError, match="actual SRT/VTT"):
            assembler.assemble(
                project,
                [scene],
                {scene.id: _candidate(scene, assets[0], clips[0])},
                master_narration_asset_id=audio.id,
                narration_required=True,
            )
    finally:
        db.close()


@pytest.mark.parametrize("fps", [RationalFps(numerator=30, denominator=1), RationalFps(numerator=30_000, denominator=1_001)])
def test_new_script_narration_never_extends_past_original_clip_boundary(tmp_path: Path, fps: RationalFps) -> None:
    db, _, project, scenes, assets, clips = _setup(tmp_path, fps)
    source = tmp_path / "too-long.wav"
    source.write_bytes(b"voice")
    audio = AudioAsset(
        source_kind=SourceKind.USER_ASSET,
        source_file=str(source),
        content_hash="c" * 64,
        duration_ms=1_501,
        sample_rate=48_000,
        channels=1,
        authorization_reference="creator-voice",
        imported_at=NOW,
    )
    AudioAssetRepository(db).create(audio)
    try:
        assembler = VideoSpecAssembler(AssetRepository(db), ClipRepository(db), audios=AudioAssetRepository(db))
        scene = scenes[0]
        # This persisted Clip starts at 500 ms and has only 1,500 ms of
        # authorized source. The assembler must test that original boundary,
        # not overwrite it with start + narration duration first.
        with pytest.raises(InsufficientSourceDuration, match="narration duration exceeds"):
            assembler.assemble(
                project,
                [scene],
                {scene.id: _candidate(scene, assets[1], clips[1])},
                narration_asset_ids={scene.id: audio.id},
                narration_required=True,
            )
        assert ClipRepository(db).get(clips[1].id).end_ms == 2_000  # type: ignore[union-attr]
    finally:
        db.close()


def test_new_script_narration_rejects_nonzero_ntsc_clip_that_lacks_a_complete_frame(tmp_path: Path) -> None:
    db, _, project, scenes, assets, clips = _setup(tmp_path, RationalFps(numerator=30_000, denominator=1_001))
    source = tmp_path / "frame-boundary.wav"
    source.write_bytes(b"voice")
    audio = AudioAsset(
        source_kind=SourceKind.USER_ASSET,
        source_file=str(source),
        content_hash="b" * 64,
        duration_ms=1_500,
        sample_rate=48_000,
        channels=1,
        authorization_reference="creator-voice",
        imported_at=NOW,
    )
    AudioAssetRepository(db).create(audio)
    try:
        assembler = VideoSpecAssembler(AssetRepository(db), ClipRepository(db), audios=AudioAssetRepository(db))
        with pytest.raises(InsufficientSourceDuration, match="frame rate"):
            assembler.assemble(
                project,
                [scenes[0]],
                {scenes[0].id: _candidate(scenes[0], assets[1], clips[1])},
                narration_asset_ids={scenes[0].id: audio.id},
                narration_required=True,
            )
    finally:
        db.close()


def test_source_led_scene_uses_real_transcript_sentence_boundary_when_available(tmp_path: Path) -> None:
    db, assembler, project, scenes, assets, clips = _setup(tmp_path, RationalFps(numerator=30, denominator=1))
    try:
        scene = scenes[0].model_copy(update={"voice_text": "first words"})
        clips[0] = clips[0].model_copy(update={
            "transcript": "first words second thought",
            "transcript_segments": [
                TranscriptSegment(start_ms=100, end_ms=700, text="first words"),
                TranscriptSegment(start_ms=750, end_ms=1_400, text="second thought"),
            ],
        })
        ClipRepository(db).update(clips[0])
        spec = assembler.assemble(
            project, [scene], {scene.id: _candidate(scene, assets[0], clips[0])},
        )
        assert spec.scenes[0].visual.clip_start_ms == 100
        assert spec.scenes[0].visual.clip_end_ms == 700
        assert [(caption.start_ms, caption.end_ms, caption.text) for caption in spec.scenes[0].captions] == [(0, 600, "first words")]
        assert spec.scenes[0].duration_frames == 18
    finally:
        db.close()


def test_terminal_source_led_scene_carries_contiguous_real_asr_tail(tmp_path: Path) -> None:
    db, assembler, project, scenes, assets, clips = _setup(tmp_path, RationalFps(numerator=30, denominator=1))
    try:
        scene = scenes[0].model_copy(update={"purpose": "close", "voice_text": "first words"})
        clips[0] = clips[0].model_copy(update={
            "transcript_segments": [
                TranscriptSegment(start_ms=100, end_ms=700, text="first words"),
                TranscriptSegment(start_ms=700, end_ms=1_400, text="second thought"),
            ],
        })
        ClipRepository(db).update(clips[0])
        spec = assembler.assemble(project, [scene], {scene.id: _candidate(scene, assets[0], clips[0])})
        rendered_scene = spec.scenes[0]
        assert rendered_scene.visual.clip_start_ms == 100
        assert rendered_scene.visual.clip_end_ms == 1_400
        assert rendered_scene.duration_frames == 39
        assert [(caption.start_ms, caption.end_ms, caption.text) for caption in rendered_scene.captions] == [
            (0, 600, "first words"),
            (600, 1_300, "second thought"),
        ]
    finally:
        db.close()


def test_source_led_scene_matches_contiguous_transcript_segments(tmp_path: Path) -> None:
    db, assembler, project, scenes, assets, clips = _setup(tmp_path, RationalFps(numerator=30, denominator=1))
    try:
        scene = scenes[0].model_copy(update={"voice_text": "first words second thought"})
        clips[0] = clips[0].model_copy(update={
            "transcript_segments": [
                TranscriptSegment(start_ms=100, end_ms=700, text="first words"),
                TranscriptSegment(start_ms=750, end_ms=1_400, text="second thought"),
            ],
        })
        ClipRepository(db).update(clips[0])
        spec = assembler.assemble(project, [scene], {scene.id: _candidate(scene, assets[0], clips[0])})
        rendered_scene = spec.scenes[0]
        assert rendered_scene.visual.clip_start_ms == 100
        assert rendered_scene.visual.clip_end_ms == 1_400
        assert rendered_scene.duration_frames == 39
        assert [(caption.start_ms, caption.end_ms, caption.text) for caption in rendered_scene.captions] == [
            (0, 600, "first words"),
            (650, 1_300, "second thought"),
        ]
    finally:
        db.close()


def test_assembler_rejects_project_selection_and_order_identity_errors(tmp_path: Path) -> None:
    db, assembler, project, scenes, assets, clips = _setup(tmp_path, RationalFps(numerator=30, denominator=1))
    try:
        selected = {scenes[0].id: _candidate(scenes[0], assets[0], clips[0])}
        with pytest.raises(InvalidCandidateSelection, match="exactly one"):
            assembler.assemble(project, scenes, selected)
        foreign_scene = _scene(_project(RationalFps(numerator=30, denominator=1)), 0)
        with pytest.raises(InvalidCandidateSelection, match="supplied Project"):
            assembler.assemble(project, [foreign_scene], {foreign_scene.id: _candidate(foreign_scene, assets[0], clips[0])})
        broken_order = scenes[1].model_copy(update={"order": 2})
        with pytest.raises(InvalidCandidateSelection, match="contiguous"):
            assembler.assemble(
                project, [scenes[0], broken_order],
                {scenes[0].id: _candidate(scenes[0], assets[0], clips[0]), broken_order.id: _candidate(broken_order, assets[1], clips[1])},
            )
    finally:
        db.close()

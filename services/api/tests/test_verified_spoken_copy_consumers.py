"""已核验读音等价沿原识别时间轴消费，不改写识别事实。"""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.assembly.video_spec import (
    NarrationTimelineError,
    _audio_captions_for_interval,
    _master_narration_intervals,
)
from app.domain.models import AudioAsset, Project, RationalFps, ScenePlan, SourceKind, TranscriptSegment, VisualIntent
from app.project_drafts import _split_talking_master_clauses
from app.providers.asr import TranscriptionResult, TranscriptionSegment, TranscriptionWord
from app.talking.admission import _matches_interval_copy
from app.voice_qa import apply_voice_qa, verify_generated_voice


@pytest.fixture
def spoken_copy(tmp_path):
    source = tmp_path / "generated.bin"
    source.write_bytes(b"offline fixture; not generated speech")
    target = "视频，画质。"
    audio = AudioAsset(
        source_file=str(source), content_hash="a" * 64, duration_ms=4000,
        sample_rate=24000, channels=1, authorization_reference="fixture",
        imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {"target_text": target, "qa_state": "pending"}},
    )
    transcription = TranscriptionResult(
        "视频，话质。",
        (TranscriptionSegment(0, 1900, "视频"), TranscriptionSegment(2000, 3900, "话质")),
    )
    report = verify_generated_voice(audio, target, transcription)
    assert report.verified and report.substitution_token_count == 1
    audio = apply_voice_qa(audio, report, transcription, provider="fixture", model="fixture")
    project = Project(ip_profile_id=uuid4(), title="fixture", topic="fixture",
        fps=RationalFps(numerator=25, denominator=1), created_at=datetime.now(timezone.utc))
    scene = ScenePlan(project_id=project.id, scene_id="speech", order=0, purpose="fixture",
        voice_text=target, duration_target_ms=4000, visual_intent=VisualIntent(),
        preferred_sources=[SourceKind.AI_VIDEO], visual_requirement="creator_speaking",
        visual_requirement_reason="fixture")
    return audio, project, scene


def test_verified_phonetic_copy_drives_boundaries_captions_and_talking(spoken_copy):
    audio, _, scene = spoken_copy
    scenes = [scene.model_copy(update={"voice_text": "视频，"}),
        scene.model_copy(update={"voice_text": "画质。", "order": 1, "scene_id": "second"})]
    assert [(start, end) for _, start, end in _master_narration_intervals(audio, scenes)] == [(0, 1900), (1900, 4000)]
    assert [(item.start_ms, item.end_ms, item.text) for item in _audio_captions_for_interval(audio, 0, 4000)] == [
        (0, 1900, "视频，"), (2000, 3900, "画质。")]
    assert _matches_interval_copy("画质。", audio.transcript_segments, 2000, 4000, audio=audio)
    assert not _matches_interval_copy("音质。", audio.transcript_segments, 2000, 4000, audio=audio)
    assert [item.text for item in audio.transcript_segments] == ["视频", "话质"]


def test_verified_phonetic_copy_splits_only_at_real_clause_edges(spoken_copy):
    audio, project, scene = spoken_copy
    result = _split_talking_master_clauses(None, project, scene.voice_text, [scene], master=audio)
    assert [item.voice_text for item in result] == ["视频，", "画质。"]
    assert [item.duration_target_ms for item in result] == [1900, 2100]


def test_changed_qa_transcript_cannot_supply_target_captions_or_talking(spoken_copy):
    audio, _, scene = spoken_copy
    changed = audio.model_copy(update={"transcript_segments": [
        audio.transcript_segments[0], audio.transcript_segments[1].model_copy(update={"text": "花质"})]})
    with pytest.raises(NarrationTimelineError):
        _master_narration_intervals(changed, [scene])
    assert _audio_captions_for_interval(changed, 0, 4000)[1].text == "花质"
    assert not _matches_interval_copy("画质", changed.transcript_segments, 2000, 4000, audio=changed)


def test_scene_boundary_inside_asr_segment_remains_unalignable(spoken_copy):
    audio, _, scene = spoken_copy
    scenes = [scene.model_copy(update={"voice_text": "视"}),
        scene.model_copy(update={"voice_text": "频，画质。", "order": 1, "scene_id": "second"})]
    with pytest.raises(NarrationTimelineError):
        _master_narration_intervals(audio, scenes)


def test_qa_word_clauses_flow_through_schedule_talking_and_captions(tmp_path):
    from app.talking.slices import plan_talking_audio_slice

    source = tmp_path / 'observed.bin'
    source.write_bytes(b'offline timing fixture, not speech')
    copy = '同一画面，运动更复杂；编码也重要。'
    observed = '同一畫面運動更複雜編碼也重要'
    words = tuple(TranscriptionWord(i * 300, (i + 1) * 300, char) for i, char in enumerate(observed))
    audio = AudioAsset(source_file=str(source), content_hash='c' * 64,
        duration_ms=len(words) * 300, sample_rate=24000, channels=1,
        authorization_reference='fixture', imported_at=datetime.now(timezone.utc),
        metadata={'voice_generation': {'target_text': copy, 'qa_state': 'pending'}})
    transcription = TranscriptionResult(observed,
        (TranscriptionSegment(0, audio.duration_ms, observed),), words=words)
    report = verify_generated_voice(audio, copy, transcription)
    saved = apply_voice_qa(audio, report, transcription, provider='fixture', model='fixture')
    assert report.verified
    assert [s.text for s in saved.transcript_segments] == ['同一畫面', '運動更複雜', '編碼也重要']
    assert saved.metadata['voice_generation']['qa']['observed_segments'] == [
        dict(schema_version='0.1', start_ms=0, end_ms=audio.duration_ms, text=observed)]
    assert saved.metadata['voice_generation']['qa']['observed_text'] == observed
    assert saved.metadata['voice_clause_timing']['source_sha256'] == saved.content_hash
    project = Project(ip_profile_id=uuid4(), title='fixture', topic='fixture',
        fps=RationalFps(numerator=25, denominator=1), created_at=datetime.now(timezone.utc))
    scene = ScenePlan(project_id=project.id, scene_id='speech', order=0, purpose='fixture',
        voice_text=copy, duration_target_ms=audio.duration_ms, visual_intent=VisualIntent(),
        preferred_sources=[SourceKind.AI_VIDEO], visual_requirement='creator_speaking',
        visual_requirement_reason='fixture')
    split = _split_talking_master_clauses(None, project, copy, [scene], master=saved, max_segment_ms=1600)
    assert [s.voice_text for s in split] == ['同一画面，', '运动更复杂；', '编码也重要。']
    assert [(start, end) for _, start, end in _master_narration_intervals(saved, split)] == [
        (0, 1200), (1200, 2700), (2700, 4200)]
    plan = plan_talking_audio_slice(saved, start_segment_index=1, end_segment_index=2, max_duration_ms=1600)
    assert (plan.start_ms, plan.end_ms) == (1200, 2700)
    assert _matches_interval_copy(split[1].voice_text, saved.transcript_segments, 1200, 2700, audio=saved)
    assert ''.join(c.text for c in _audio_captions_for_interval(saved, 0, saved.duration_ms)) == copy


@pytest.mark.parametrize('invalid', ['different_words', 'overlap', 'inside_word'])
def test_qa_never_guesses_clause_time_without_matching_ordered_word_edges(tmp_path, invalid):
    source = tmp_path / 'observed.bin'
    source.write_bytes(b'offline timing fixture')
    copy = '画面，清晰。'
    audio = AudioAsset(source_file=str(source), content_hash='d' * 64, duration_ms=2000,
        sample_rate=24000, channels=1, authorization_reference='fixture', imported_at=datetime.now(timezone.utc),
        metadata={'voice_generation': {'target_text': copy, 'qa_state': 'pending'}})
    words = {'different_words': (TranscriptionWord(0, 900, '画质'), TranscriptionWord(1000, 2000, '清晰')),
        'overlap': (TranscriptionWord(0, 1100, '画面'), TranscriptionWord(1000, 2000, '清晰')),
        'inside_word': (TranscriptionWord(0, 2000, '画面清晰'),)}[invalid]
    transcription = TranscriptionResult(copy, (TranscriptionSegment(0, 2000, copy),), words=words)
    saved = apply_voice_qa(audio, verify_generated_voice(audio, copy, transcription), transcription,
        provider='fixture', model='fixture')
    assert len(saved.transcript_segments) == 1
    assert 'voice_clause_timing' not in saved.metadata


def test_qa_rejects_word_times_outside_their_corresponding_source_segment(tmp_path):
    from app.voice_qa import _timed_qa_clauses
    segments = [TranscriptSegment(start_ms=0, end_ms=1000, text='画面'),
        TranscriptSegment(start_ms=1200, end_ms=2000, text='清晰')]
    words = (TranscriptionWord(0, 500, '画'), TranscriptionWord(800, 1500, '面'),
        TranscriptionWord(1500, 1800, '清'), TranscriptionWord(1800, 2000, '晰'))
    assert _timed_qa_clauses('画，面清晰。', segments, words, 2000) == segments


def test_rechecking_qa_replaces_old_word_and_clause_evidence(spoken_copy):
    audio, _, _ = spoken_copy
    metadata = {**audio.metadata, 'voice_word_timing': {'source_sha256': audio.content_hash, 'words': []},
        'voice_clause_timing': {'source_sha256': audio.content_hash},
        'voice_boundary_alignment': {'state': 'aligned'}}
    audio = audio.model_copy(update={'metadata': metadata})
    transcription = TranscriptionResult('视频，话质。', (
        TranscriptionSegment(0, 1900, '视频'), TranscriptionSegment(2000, 3900, '话质')))
    saved = apply_voice_qa(audio, verify_generated_voice(audio, '视频，画质。', transcription),
        transcription, provider='fixture', model='new-observation')
    assert not {'voice_word_timing', 'voice_clause_timing', 'voice_boundary_alignment'}.intersection(saved.metadata)
    assert saved.metadata['voice_generation']['qa']['observed_text'] == transcription.text

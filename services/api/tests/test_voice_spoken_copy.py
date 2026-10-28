"""通用中文同音识别比较；夹具不代表真实语音质量。"""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.domain.models import AudioAsset, ScenePlan, SourceKind, VisualIntent
from app.production_runs import _unique_scene_speech_span
from app.providers.asr import TranscriptionResult, TranscriptionSegment, TranscriptionWord
from app.voice_qa import apply_voice_qa, verify_generated_voice, verified_generated_copy_tokens


def _audio(tmp_path, target, duration=2000):
    path = tmp_path / "fixture.bin"
    path.write_bytes(b"not speech; offline comparison fixture")
    return AudioAsset(source_file=str(path), content_hash="a" * 64,
        duration_ms=duration, sample_rate=24000, channels=1,
        authorization_reference="fixture", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {"target_text": target, "qa_state": "pending"}})


def test_single_reading_homophones_preserve_literal_evidence_and_timing(tmp_path):
    target, observed = "画面原图", "话面源图"
    audio = _audio(tmp_path, target)
    transcription = TranscriptionResult(observed, (TranscriptionSegment(0, 1800, observed),),
        words=(TranscriptionWord(0, 1800, observed),))
    report = verify_generated_voice(audio, target, transcription)
    assert report.verified and report.substitution_token_count == 2
    assert len(report.phonetic_equivalences) == 2
    saved = apply_voice_qa(audio, report, transcription, provider="fixture", model="fixture")
    assert saved.transcript_segments[0].text == observed
    assert saved.metadata["voice_generation"]["qa"]["unresolved_substitution_token_count"] == 0
    assert verified_generated_copy_tokens(saved) == tuple(target)
    scene = ScenePlan(project_id=uuid4(), scene_id="one", order=0, purpose="fixture",
        voice_text=target, duration_target_ms=2000, visual_intent=VisualIntent(),
        preferred_sources=[SourceKind.AI_VIDEO], visual_requirement="creator_speaking",
        visual_requirement_reason="fixture")
    assert _unique_scene_speech_span(saved, [scene], scene.id) == (0, 1800)
    changed_words = dict(saved.metadata["voice_word_timing"], words=[dict(start_ms=0, end_ms=1800, text="话面元图")])
    changed = saved.model_copy(update={"metadata": {**saved.metadata, "voice_word_timing": changed_words}})
    assert _unique_scene_speech_span(changed, [scene], scene.id) is None


@pytest.mark.parametrize("target,observed", [
    ("妈", "马"),  # 声调不同
    ("还款", "孩款"),  # 还为多音字，不能取默认读音放行
    ("码率", "马力"),
    ("H265", "H264"),
    ("15", "50"),
    ("画质", "话话质"),
    ("画质", "话"),
    ("画质", "质话"),
])
def test_phonetic_comparison_keeps_wrong_tones_polyphones_numbers_and_coverage_blocked(tmp_path, target, observed):
    audio = _audio(tmp_path, target)
    report = verify_generated_voice(audio, target, TranscriptionResult(observed, (TranscriptionSegment(0, 1800, observed),)))
    assert not report.verified


def test_multiple_polyphones_remain_literal_substitutions_without_raising_tolerance(tmp_path):
    target = "视频码率越高，画面就一定越清晰吗？其实不一定。码率只代表每秒能塞进多少数据，画面里本来就有的细节，它变不出来。素材本身早就压缩过，已经丢掉的细节，再高的码率也追不回来。"
    observed = target.replace("码率", "马律", 1)
    # The real prior shape contained two 马律 and one 辨, not a topic whitelist.
    observed = observed[:observed.rfind("码率")] + observed[observed.rfind("码率"):].replace("码率", "马律")
    observed = observed.replace("变", "辨")
    audio = _audio(tmp_path, target, 16000)
    transcription = TranscriptionResult(observed, (TranscriptionSegment(0, 15500, observed),))
    report = verify_generated_voice(audio, target, transcription)
    assert report.verified and report.substitution_token_count == 5
    # 辨 also has multiple dictionary readings, so it stays unresolved with 率.
    assert len(report.phonetic_equivalences) == 2
    assert "copy_substitutions_within_tolerance" in report.checks
    saved = apply_voice_qa(audio, report, transcription, provider="fixture", model="fixture")
    assert verified_generated_copy_tokens(saved) is not None


@pytest.mark.parametrize("field", ["source_sha256", "target_text_sha256", "transcript_sha256", "comparison_policy"])
def test_bound_voice_comparison_cannot_be_downgraded_or_reused_for_changed_input(tmp_path, field):
    target = "请把这一段完整画面保留并交给创作者审核"
    audio = _audio(tmp_path, target)
    transcription = TranscriptionResult(target, (TranscriptionSegment(0, 1800, target),))
    saved = apply_voice_qa(audio, verify_generated_voice(audio, target, transcription), transcription, provider="fixture", model="fixture")
    generation = dict(saved.metadata["voice_generation"])
    qa = dict(generation["qa"])
    qa.pop(field)
    generation["qa"] = qa
    changed = saved.model_copy(update={"metadata": {**saved.metadata, "voice_generation": generation}})
    assert verified_generated_copy_tokens(changed) is None


def test_homophones_cannot_override_silence_or_missing_timing(tmp_path):
    audio = _audio(tmp_path, "画质")
    report = verify_generated_voice(audio, "画质", TranscriptionResult("话质", (TranscriptionSegment(700, 1800, "话质"),)))
    assert not report.verified and "leading_silence_or_unrecognized_audio" in report.checks
    assert len(report.phonetic_equivalences) == 1

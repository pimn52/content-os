from datetime import datetime, timezone
import hashlib
from pathlib import Path
import subprocess
import wave

import pytest

from app.domain.models import AudioAsset, NarrationPace, NarrationPause, NarrationPerformanceCue, NarrationPerformanceCueKind, NarrationPerformancePlanSource, TranscriptSegment
from app.narration_performance import build_narration_performance_plan
from app.runtime import resolve_local_executable
from app.voice_observation import VoiceObservationError, observe_voice_performance


def _pcm_wav(path: Path, *, quiet: tuple[tuple[int, int], ...] = ()) -> None:
    rate = 16_000
    samples = [0 if any(start <= frame * 1000 // rate < end for start, end in quiet) else 8_000
               for frame in range(2 * rate)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"".join(value.to_bytes(2, "little", signed=True) for value in samples))


def _audio(path: Path, copy: str, segments: list[TranscriptSegment], *, plan: object = None) -> AudioAsset:
    generation: dict[str, object] = {
        "qa_state": "verified",
        "target_text": copy,
        "qa": {"qa_state": "verified", "copy_coverage": 1.0,
               "missing_token_count": 0, "duplicate_token_count": 0},
    }
    if plan is not None:
        generation["narration_performance"] = {
            "plan": plan.model_dump(mode="json"),
            "application_state": "adapter_applied_pending_quality_review",
        }
    return AudioAsset(
        source_file=str(path), content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
        duration_ms=2_000, sample_rate=16_000, channels=1,
        authorization_reference="test-rights", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": generation},
        transcript_source="independent-asr:test", transcript_segments=segments,
    )


def test_two_copies_produce_distinct_syntax_observations_without_mutating_audio(tmp_path: Path) -> None:
    first_path = tmp_path / "first.wav"
    second_path = tmp_path / "second.wav"
    _pcm_wav(first_path, quiet=((450, 550), (1_300, 1_550)))
    _pcm_wav(second_path, quiet=((900, 1_050),))
    first_copy = "先说。再说，继续。"
    second_copy = "今天做什么？先看素材。"
    first = _audio(first_path, first_copy, [
        TranscriptSegment(start_ms=0, end_ms=400, text="先说"),
        TranscriptSegment(start_ms=600, end_ms=1_200, text="再说"),
        TranscriptSegment(start_ms=1_600, end_ms=1_950, text="继续"),
    ])
    second = _audio(second_path, second_copy, [
        TranscriptSegment(start_ms=0, end_ms=850, text="今天做什么"),
        TranscriptSegment(start_ms=1_100, end_ms=1_950, text="先看素材"),
    ])
    first_bytes, second_bytes = first_path.read_bytes(), second_path.read_bytes()

    first_report = observe_voice_performance(first, first_copy, data_root=tmp_path)
    second_report = observe_voice_performance(second, second_copy, data_root=tmp_path)

    assert first_report["observation_version"] == "1.1"
    assert first_report["word_boundary_alignment"]["state"] == "unavailable"
    assert first_report["alignment_state"] == second_report["alignment_state"] == "aligned"
    assert first_report["source_sha256"] == first.content_hash
    assert second_report["source_sha256"] == second.content_hash
    assert first_report["copy_fingerprint"] != second_report["copy_fingerprint"]
    assert len(first_report["sentences"]) == len(second_report["sentences"]) == 2
    assert first_report["sentences"][0]["duration_ms"] == 400
    assert first_report["adjacent_sentence_pace"][0]["state"] == "measured"
    assert first_path.read_bytes() == first_bytes
    assert second_path.read_bytes() == second_bytes


def test_physical_quiet_is_not_an_asr_gap_and_plan_cue_is_not_a_receipt(tmp_path: Path) -> None:
    path = tmp_path / "voice.wav"
    _pcm_wav(path, quiet=((450, 550), (1_300, 1_550)))
    copy = "先说。再说，继续。"
    plan = build_narration_performance_plan(
        copy, delivery_goal="clear clauses", overall_pace=NarrationPace.CONVERSATIONAL,
        source=NarrationPerformancePlanSource.USER, evidence_refs=[], cues=[
            NarrationPerformanceCue(kind=NarrationPerformanceCueKind.PAUSE, start_char=3, end_char=3, pause=NarrationPause.BRIEF),
        ],
    )
    audio = _audio(path, copy, [
        TranscriptSegment(start_ms=0, end_ms=400, text="先说"),
        TranscriptSegment(start_ms=600, end_ms=1_200, text="再说"),
        TranscriptSegment(start_ms=1_600, end_ms=1_950, text="继续"),
    ], plan=plan)

    report = observe_voice_performance(audio, copy, data_root=tmp_path)

    terminal = next(item for item in report["syntax_boundaries"] if item["kind"] == "terminal" and item["char_index"] == 3)
    continuing = next(item for item in report["syntax_boundaries"] if item["kind"] == "continuing")
    assert terminal["asr_gap_ms"] == 200
    assert terminal["physical_quiet_ms"] == 100
    assert continuing["asr_gap_ms"] == 400
    assert continuing["physical_quiet_ms"] == 250
    assert report["diagnostics"][0]["kind"] == "review_punctuation_pause_hierarchy"
    assert report["plan_reference"]["state"] == "matching"
    assert report["plan_comparison_state"] == "intent_context_only"
    assert report["plan_reference"]["cue_intents"][0]["physical_quiet_ms_at_boundary"] == 100
    assert report["plan_reference"]["cue_intents"][0]["acoustic_realization_state"] == "not_assessed"

    generation = dict(audio.metadata["voice_generation"])
    generation["performance_plan"] = plan.model_dump(mode="json")
    del generation["narration_performance"]
    direct = audio.model_copy(update={"metadata": {"voice_generation": generation}})
    direct_report = observe_voice_performance(direct, copy, data_root=tmp_path)
    assert direct_report["plan_reference"]["state"] == "matching"
    assert direct_report["plan_reference"]["snapshot_locations"] == ["performance_plan"]


def test_observation_uses_separate_source_bound_word_evidence_without_replacing_segment_qa(tmp_path: Path) -> None:
    path = tmp_path / "voice.wav"
    _pcm_wav(path, quiet=((450, 550),))
    copy = "先说。再说。"
    audio = _audio(path, copy, [TranscriptSegment(start_ms=0, end_ms=1_950, text="先说再说")])
    metadata = dict(audio.metadata)
    metadata["voice_word_timing"] = {
        "version": "1.0", "source_sha256": audio.content_hash,
        "transcript_source": "independent-asr:test",
        "words": [{"start_ms": 0, "end_ms": 400, "text": "先说"},
                  {"start_ms": 600, "end_ms": 1_950, "text": "再说"}],
    }
    observed = observe_voice_performance(audio.model_copy(update={"metadata": metadata}), copy, data_root=tmp_path)
    assert observed["alignment_state"] == "aligned"
    assert observed["syntax_boundaries"][0]["timing_state"] == "unavailable"
    word_boundary = next(item for item in observed["word_boundary_alignment"]["boundaries"]
                         if item["char_index"] == 3)
    assert word_boundary["physical_quiet_ms"] == 100


def test_missing_or_uncertain_asr_preserves_only_pcm_facts(tmp_path: Path) -> None:
    path = tmp_path / "voice.wav"
    _pcm_wav(path, quiet=((450, 550),))
    copy = "先说。再说。"
    good = _audio(path, copy, [
        TranscriptSegment(start_ms=0, end_ms=400, text="先说"),
        TranscriptSegment(start_ms=600, end_ms=1_950, text="再说"),
    ])
    missing = good.model_copy(update={"transcript_source": None, "transcript_segments": []})
    uncertain = good.model_copy(update={"transcript_segments": [
        TranscriptSegment(start_ms=0, end_ms=400, text="先讲"),
        TranscriptSegment(start_ms=600, end_ms=1_950, text="再说"),
    ]})

    for asset in (missing, uncertain):
        report = observe_voice_performance(asset, copy, data_root=tmp_path)
        assert report["alignment_state"] == "unavailable"
        assert report["measurement"]["physical_quiet_intervals"]
        assert all(item["timing_state"] == "unavailable" for item in report["sentences"])
        assert all(item["physical_quiet_ms"] is None for item in report["syntax_boundaries"])
        assert report["diagnostics"] == []
        assert report["plan_comparison_state"] == "unavailable"


def test_boundary_inside_asr_segment_does_not_gain_invented_timing(tmp_path: Path) -> None:
    path = tmp_path / "voice.wav"
    _pcm_wav(path, quiet=((450, 550),))
    copy = "先说。再说。"
    audio = _audio(path, copy, [TranscriptSegment(start_ms=0, end_ms=1_950, text="先说再说")])

    report = observe_voice_performance(audio, copy, data_root=tmp_path)

    assert report["alignment_state"] == "aligned"
    assert report["sentences"][0]["timing_state"] == "unavailable"
    assert report["syntax_boundaries"][0]["physical_quiet_ms"] is None
    assert report["measurement"]["physical_quiet_intervals"][0]["duration_ms"] == 100


def test_quiet_crossing_contiguous_asr_edge_is_only_a_low_confidence_candidate(tmp_path: Path) -> None:
    path = tmp_path / "voice.wav"
    _pcm_wav(path, quiet=((450, 550),))
    copy = "先说。再说。"
    audio = _audio(path, copy, [
        TranscriptSegment(start_ms=0, end_ms=500, text="先说"),
        TranscriptSegment(start_ms=500, end_ms=1_950, text="再说"),
    ])

    report = observe_voice_performance(audio, copy, data_root=tmp_path)

    boundary = next(item for item in report["syntax_boundaries"] if item["char_index"] == 3)
    assert boundary["asr_gap_ms"] == 0
    assert boundary["physical_quiet_ms"] is None
    assert boundary["physical_quiet_candidate_ms"] == 100
    assert boundary["physical_quiet_candidate_provenance"] == "candidate_crosses_asr_edge"
    assert boundary["physical_quiet_candidate_confidence"] == "low"
    assert report["diagnostics"] == []


def test_verified_report_copy_and_hash_are_hard_gates(tmp_path: Path) -> None:
    path = tmp_path / "voice.wav"
    _pcm_wav(path)
    copy = "先说。再说。"
    audio = _audio(path, copy, [TranscriptSegment(start_ms=0, end_ms=900, text="先说"), TranscriptSegment(start_ms=1_000, end_ms=1_950, text="再说")])
    with pytest.raises(VoiceObservationError, match="exact copy"):
        observe_voice_performance(audio, "先讲。再说。", data_root=tmp_path)
    generation = dict(audio.metadata["voice_generation"])
    generation["qa"] = {**generation["qa"], "copy_coverage": 0.9}
    with pytest.raises(VoiceObservationError, match="independent Voice QA"):
        observe_voice_performance(audio.model_copy(update={"metadata": {"voice_generation": generation}}), copy, data_root=tmp_path)
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(VoiceObservationError, match="hash differs"):
        observe_voice_performance(audio, copy, data_root=tmp_path)


def test_portable_path_uses_data_root_not_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "content-os-data"
    path = root / "assets" / "voice.wav"
    _pcm_wav(path)
    copy = "写完。再读。"
    audio = _audio(path, copy, [TranscriptSegment(start_ms=0, end_ms=900, text="写完"), TranscriptSegment(start_ms=1_000, end_ms=1_950, text="再读")])
    audio = audio.model_copy(update={"source_file": "content-os-data/assets/voice.wav"})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    report = observe_voice_performance(audio, copy, data_root=root)
    assert report["source_sha256"] == audio.content_hash


def test_portable_path_must_not_escape_data_root(tmp_path: Path) -> None:
    root = tmp_path / "content-os-data"
    path = tmp_path / "outside.wav"
    _pcm_wav(path)
    copy = "写完。再读。"
    audio = _audio(path, copy, [TranscriptSegment(start_ms=0, end_ms=900, text="写完"), TranscriptSegment(start_ms=1_000, end_ms=1_950, text="再读")])
    audio = audio.model_copy(update={"source_file": "../outside.wav"})

    with pytest.raises(VoiceObservationError, match="escapes data_root"):
        observe_voice_performance(audio, copy, data_root=root)


def test_common_non_wav_source_is_decoded_with_explicit_pcm_provenance(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    destination = tmp_path / "source.mp3"
    _pcm_wav(source, quiet=((450, 750),))
    try:
        converted = subprocess.run(
            [resolve_local_executable("ffmpeg"), "-v", "error", "-y", "-i", str(source),
             "-c:a", "libmp3lame", str(destination)],
            capture_output=True, timeout=20, check=False,
        )
    except (FileNotFoundError, OSError):
        pytest.skip("local FFmpeg is unavailable")
    if converted.returncode != 0:
        pytest.skip("local FFmpeg lacks MP3 encoding")
    copy = "先说。再说。"
    audio = _audio(destination, copy, [
        TranscriptSegment(start_ms=0, end_ms=400, text="先说"),
        TranscriptSegment(start_ms=600, end_ms=1_950, text="再说"),
    ])
    report = observe_voice_performance(audio, copy, data_root=tmp_path)
    assert report["source_sha256"] == audio.content_hash
    assert report["measurement"]["source"] == "ffmpeg_decoded_pcm16_mono_10ms_rms"
    assert any(item["duration_ms"] >= 100 for item in report["measurement"]["physical_quiet_intervals"])

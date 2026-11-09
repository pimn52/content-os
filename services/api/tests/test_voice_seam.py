from datetime import datetime, timezone
import hashlib
from pathlib import Path
from uuid import uuid4
import wave

import pytest

from app.domain.models import ConsentRecord, VoiceProfile
from app.voice_recipe import parse_generation_recipe
from app.voice_seam import (
    VoiceSeamCompactionError, apply_voice_pause_policy, compact_measured_seam_quiet,
)


def _wav(path: Path, samples: list[int]) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(b"".join(value.to_bytes(2, "little", signed=True) for value in samples))


def test_compaction_removes_only_measured_quiet_inside_known_seam(tmp_path: Path) -> None:
    rate = 24_000
    samples = [10_000] * (rate * 8 // 10) + [0] * (rate * 16 // 100) + [10_000] * (rate * 8 // 10)
    raw, final = tmp_path / "raw.wav", tmp_path / "final.wav"
    _wav(raw, samples)
    result = compact_measured_seam_quiet(raw, final, [880])
    assert result.cuts == ((0, 860, 900),)
    assert result.raw_duration_ms == 1760 and result.final_duration_ms == 1720
    assert result.raw_sha256 == hashlib.sha256(raw.read_bytes()).hexdigest()
    assert result.final_sha256 == hashlib.sha256(final.read_bytes()).hexdigest()
    with wave.open(str(final), "rb") as wav:
        output = wav.readframes(wav.getnframes())
    original = b"".join(value.to_bytes(2, "little", signed=True) for value in samples)
    assert output == original[:860 * 24 * 2] + original[900 * 24 * 2:]


def test_compaction_rejects_nonquiet_or_same_output(tmp_path: Path) -> None:
    raw = tmp_path / "raw.wav"
    _wav(raw, [10_000] * (24_000 * 2))
    with pytest.raises(VoiceSeamCompactionError, match="measured quiet"):
        compact_measured_seam_quiet(raw, tmp_path / "final.wav", [1_000])
    with pytest.raises(VoiceSeamCompactionError, match="must not overwrite"):
        compact_measured_seam_quiet(raw, raw, [1_000])
    non_pcm = tmp_path / "not-a-wav.wav"
    non_pcm.write_bytes(b"not a wave file")
    with pytest.raises(VoiceSeamCompactionError, match="could not read PCM WAV"):
        compact_measured_seam_quiet(non_pcm, tmp_path / "final.wav", [1_000])


@pytest.mark.parametrize("margin_ms,retained_ms", [(60, 120), (100, 200), (140, 280)])
def test_measured_margin_retains_existing_quiet_without_inserting_silence(
    tmp_path: Path, margin_ms: int, retained_ms: int,
) -> None:
    # These are synthetic test values, not preferred punctuation durations.
    pcm = [10_000] * (800 * 24) + [0] * (280 * 24) + [10_000] * (800 * 24)
    raw, final = tmp_path / "raw.wav", tmp_path / "final.wav"
    _wav(raw, pcm)
    raw_sha = hashlib.sha256(raw.read_bytes()).hexdigest()
    result = compact_measured_seam_quiet(raw, final, [940], margin_ms=margin_ms)
    assert result.final_duration_ms == 1600 + retained_ms
    assert result.cuts == (() if retained_ms == 280 else ((0, 800 + margin_ms, 1080 - margin_ms),))
    with wave.open(str(final), "rb") as wav:
        output = wav.readframes(wav.getnframes())
    expected = [10_000] * (800 * 24) + [0] * (retained_ms * 24) + [10_000] * (800 * 24)
    assert output == b"".join(value.to_bytes(2, "little", signed=True) for value in expected)
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == raw_sha


def test_compaction_leaves_take_internal_quiet_untouched_and_enforces_cut_limit(tmp_path: Path) -> None:
    pcm = ([10_000] * (400 * 24) + [0] * (160 * 24) + [10_000] * (400 * 24)
           + [0] * (280 * 24) + [10_000] * (400 * 24))
    raw, final = tmp_path / "raw.wav", tmp_path / "final.wav"
    _wav(raw, pcm)
    original = raw.read_bytes()
    with pytest.raises(VoiceSeamCompactionError, match="bounded quiet cut"):
        compact_measured_seam_quiet(raw, final, [1100], max_cut_ms=150)
    assert not final.exists() and raw.read_bytes() == original

    result = compact_measured_seam_quiet(raw, final, [1100])
    assert result.cuts == ((0, 1020, 1180),)
    with wave.open(str(final), "rb") as wav:
        output = wav.readframes(wav.getnframes())
    source_pcm = b"".join(value.to_bytes(2, "little", signed=True) for value in pcm)
    assert output == source_pcm[:1020 * 24 * 2] + source_pcm[1180 * 24 * 2:]
    assert output[400 * 24 * 2:560 * 24 * 2] == bytes(160 * 24 * 2)
    assert raw.read_bytes() == original

@pytest.mark.parametrize('texts,role', [
    (['先保留真实素材。', '再补充表达。'], 'terminal'),
    (['先装重物；', '再检查背包平衡。'], 'terminal'),
    (['先整理素材，', '再检查来源。'], 'continuing'),
    (['没有边界', '继续说话'], 'no_break'),
])
def test_copy_roles_preserve_source_pause_without_duration_table(tmp_path, texts, role):
    from app.voice_seam import take_boundary_kinds
    raw, final = tmp_path/'raw.wav', tmp_path/'final.wav'
    _wav(raw, [10000]*(800*24)+[0]*(280*24)+[10000]*(800*24))
    kinds=take_boundary_kinds(texts)
    assert [kind.value for kind in kinds]==[role]
    result=compact_measured_seam_quiet(raw,final,[940],boundary_kinds=kinds)
    with wave.open(str(raw),'rb') as w: original=w.readframes(w.getnframes())
    with wave.open(str(final),'rb') as w: actual=w.readframes(w.getnframes())
    assert actual==(original[:860*48]+original[1020*48:] if role=='continuing' else original)
    assert result.final_duration_ms==(1720 if role=='continuing' else 1880)


def test_mixed_roles_keep_original_seam_indices_and_unknown_joins(tmp_path):
    from app.voice_seam import take_boundary_kinds
    raw, final=tmp_path/'raw.wav',tmp_path/'final.wav'
    _wav(raw,[10000]*(800*24)+[0]*(280*24)+[10000]*(800*24)+[0]*(280*24)+[10000]*(800*24))
    kinds=take_boundary_kinds(['句末。','句内，','继续说明。'])
    result=compact_measured_seam_quiet(raw,final,[940,2020],boundary_kinds=kinds)
    assert result.cuts==((1,1940,2100),)
    with pytest.raises(VoiceSeamCompactionError,match='roles must match'):
        compact_measured_seam_quiet(raw,tmp_path/'bad.wav',[940,2020],boundary_kinds=kinds[:1])
    assert not (tmp_path/'bad.wav').exists()

def test_forward_binding_and_unclassified_joins_do_not_guess_acoustic_pauses(tmp_path):
    from app.voice_seam import take_boundary_kinds
    raw, final=tmp_path/'raw.wav',tmp_path/'final.wav'
    _wav(raw,[10000]*(2000*24))
    kinds=take_boundary_kinds(['但是，','要看源质量。'])
    assert kinds[0].value=='forward_binding'
    result=compact_measured_seam_quiet(raw,final,[1000],boundary_kinds=kinds)
    assert result.cuts==() and result.raw_duration_ms==result.final_duration_ms==2000
    with wave.open(str(final),'rb') as w:assert w.readframes(w.getnframes())==b'\x10\x27'*(2000*24)
    with pytest.raises(VoiceSeamCompactionError,match='nonempty'):
        take_boundary_kinds([' ', 'test'])
    with pytest.raises(VoiceSeamCompactionError,match='roles must match'):
        compact_measured_seam_quiet(raw,tmp_path/'invalid.wav',[1000],boundary_kinds=['unknown'])


def _reviewed_recipe():
    clip_id, asset_id = uuid4(), uuid4()
    recipe = {
        "schema_version": "0.5", "recipe_id": "v60-human-approved", "version": 6,
        "quality_anchor_audio_id": str(uuid4()), "quality_anchor_sha256": "a" * 64,
        "quality_anchor_evidence_reference": "audio:human-approved-v60",
        "human_review": {"approved": True, "likeness": "pass", "naturalness": "pass",
            "emphasis": "pass", "pace": "pass", "pauses": "pass", "rhythm": "pass"},
        "reference_window": {
            "clip_id": str(clip_id), "asset_id": str(asset_id),
            "source_content_hash": "b" * 64, "start_ms": 0, "end_ms": 6000,
            "transcript_sha256": "c" * 64,
        },
        "runtime_label": "omnivoice-existing-maintained-personal-w13",
        "runtime_parameters_sha256": "d" * 64, "composition_policy": "paragraph_spans",
        "pause_hierarchy": {
            "schema_version": "1.0",
            "levels": {
                "phrase_internal": "continuous_no_cut",
                "comma": "short_continuing_boundary",
                "sentence": "natural_close_longer_than_comma",
                "paragraph": "allow_one_beat",
                "key_point": "allow_one_thinking_beat",
                "ending": "natural_landing",
            },
            "invariants": {
                "sentence_outranks_comma": True,
                "global_speed_is_not_pause_control": True,
                "insert_unmeasured_silence": False,
            },
            "evidence_refs": ["v19b-u-voice-punctuation-hierarchy-review"],
        },
        "seam_policy": {
            "scope": "paragraph_spans",
            "method": "pcm16_mono_24khz_10ms_rms_below_minus45dbfs",
            "eligible_boundary_kinds": ["terminal", "continuing"],
            "margin_ms_each_side": 60, "max_cut_ms": 300,
            "preserve_spoken_frames": True, "insert_silence": False,
            "evidence_reference": "content-os-data/evaluation-evidence/v60-length-completion/manifest.json",
        },
        "delivery_adjustments": {
            "schema_version": "1.4", "review_state": "pending_user_review",
            "syntax_rule_set_version": "zh-clause-binding-v1",
            "punctuation_overrides": [], "pronunciation_overrides": [], "generation_breaks": [],
            "copula_complement_break": {
                "enabled": False, "max_complement_chars": 12,
                "reason": "disabled in fixture", "evidence_reference": "fixture",
            },
            "contextual_pronunciation": {
                "enabled": False, "rule_set_version": "zh-common-polyphones-v1",
                "evidence_reference": "fixture",
            },
            "contrastive_correction_break": {
                "enabled": False, "max_negated_chars": 48,
                "reason": "disabled in fixture", "evidence_reference": "fixture",
            },
            "predicate_completion_break": {
                "enabled": False, "max_clause_chars": 60,
                "reason": "disabled in fixture", "evidence_reference": "fixture",
            },
        },
    }
    profile = VoiceProfile(
        name="Creator", provider="omnivoice", provider_profile_id="creator",
        reference_clip_ids=[clip_id],
        consent=ConsentRecord(subject_name="Creator", basis="self", confirmed=True,
            confirmed_at=datetime.now(timezone.utc)),
        generation_recipe=recipe, created_at=datetime.now(timezone.utc),
    )
    parsed = parse_generation_recipe(profile)
    assert parsed is not None
    return parsed


def test_reviewed_pause_policy_compacts_paragraph_quiet_without_touching_speech(tmp_path):
    rate = 24_000
    first = [10_000] * (rate * 8 // 10) + [0] * (rate * 14 // 100)
    second = [0] * (rate * 14 // 100) + [12_000] * (rate * 8 // 10)
    raw, final = tmp_path / "raw.wav", tmp_path / "final.wav"
    _wav(raw, first + second)
    text = "先说明白。\n\n再讲重点。"
    segments = [
        {"start_char": 0, "end_char": 5, "start_sample": 0, "end_sample": len(first)},
        {"start_char": 7, "end_char": len(text), "start_sample": len(first),
         "end_sample": len(first) + len(second)},
    ]

    applied = apply_voice_pause_policy(
        raw, final, recipe=_reviewed_recipe(), text=text,
        generation_segments=segments, sample_rate=rate,
    )

    assert applied.evidence["state"] == "applied"
    assert applied.evidence["cuts"][0]["boundary_kind"] == "terminal"
    assert applied.evidence["cuts"][0]["start_ms"] == 860
    assert applied.evidence["cuts"][0]["end_ms"] == 1020
    assert applied.evidence["global_speed_changed"] is False
    assert applied.evidence["inserted_silence_ms"] == 0
    assert applied.generation_segments[1]["final_start_sample"] == len(first) - rate * 8 // 100
    assert applied.generation_segments[1]["final_end_sample"] == (
        len(first) + len(second) - rate * 16 // 100
    )
    with wave.open(str(raw), "rb") as source, wave.open(str(final), "rb") as compacted:
        source_pcm = source.readframes(source.getnframes())
        final_pcm = compacted.readframes(compacted.getnframes())
    assert final_pcm == source_pcm[:860 * 24 * 2] + source_pcm[1020 * 24 * 2:]


def test_reviewed_pause_policy_preserves_a_focus_emphasis_seam(tmp_path):
    rate = 24_000
    first = [10_000] * (rate * 8 // 10) + [0] * (rate * 14 // 100)
    second = [0] * (rate * 14 // 100) + [12_000] * (rate * 8 // 10)
    raw, final = tmp_path / "raw.wav", tmp_path / "final.wav"
    _wav(raw, first + second)
    text = "最后，\n\n镜头按真实时长拆。"
    segments = [
        {"start_char": 0, "end_char": 3, "start_sample": 0, "end_sample": len(first)},
        {"start_char": 5, "end_char": len(text), "start_sample": len(first),
         "end_sample": len(first) + len(second)},
    ]

    applied = apply_voice_pause_policy(
        raw, final, recipe=_reviewed_recipe(), text=text,
        generation_segments=segments, sample_rate=rate,
        preserve_seam_indexes=(0,),
    )

    assert applied.evidence["state"] == "no_cut"
    assert applied.evidence["preserved_seam_indexes"] == [0]
    assert applied.evidence["skipped"] == [
        {"seam_index": 0, "reason": "focus_emphasis_boundary"}
    ]
    with wave.open(str(raw), "rb") as source, wave.open(str(final), "rb") as preserved:
        assert source.readframes(source.getnframes()) == preserved.readframes(preserved.getnframes())


def test_reviewed_pause_policy_does_not_cut_unpunctuated_or_forward_boundaries(tmp_path):
    rate = 24_000
    first = [10_000] * (rate * 8 // 10) + [0] * (rate * 14 // 100)
    second = [0] * (rate * 14 // 100) + [10_000] * (rate * 8 // 10)
    raw, final = tmp_path / "raw.wav", tmp_path / "final.wav"
    _wav(raw, first + second)
    text = "没有边界\n\n但是，继续说明。"
    segments = [
        {"start_char": 0, "end_char": 4, "start_sample": 0, "end_sample": len(first)},
        {"start_char": 6, "end_char": len(text), "start_sample": len(first),
         "end_sample": len(first) + len(second)},
    ]

    applied = apply_voice_pause_policy(
        raw, final, recipe=_reviewed_recipe(), text=text,
        generation_segments=segments, sample_rate=rate,
    )

    assert applied.evidence["state"] == "no_cut"
    assert applied.evidence["boundary_kinds"] == ["no_break"]
    assert applied.evidence["skipped"] == [
        {"seam_index": 0, "reason": "boundary_no_break_kept"},
    ]

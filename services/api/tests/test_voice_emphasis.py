from array import array
import hashlib
import math
from pathlib import Path
import sys
import wave

from app.domain.models import NarrationEmphasis
from app.voice_emphasis import (
    apply_focus_audio_emphasis,
    apply_focus_text_boundaries,
    automatic_emphasis_plan,
    find_voice_focus_rules,
)


COPY = (
    "结论先行，是因为它是整条片子的骨架。"
    "适用边界防止结论被误用。"
    "最后，镜头要按旁白的真实时长来拆。\n\n"
    "记住三步：先写完整结论和边界，再测旁白真实时长，最后按时长拆镜头。"
)


def test_focus_rules_cover_sequence_close_and_quantity_frame_without_single_examples():
    rules = find_voice_focus_rules(COPY)

    assert [(COPY[item.start_char:item.end_char], item.rule_id) for item in rules] == [
        ("最后，", "enumeration_terminal"),
        ("记住三步：", "quantity_frame"),
    ]
    assert {item.emphasis for item in rules} == {NarrationEmphasis.STRONG}


def test_focus_rules_keep_time_quantity_and_non_sequence_context_unemphasized():
    negative_copies = (
        "最后一天，我们重新检查了工作流程。",
        "结论已经明确。最后，终于把问题说清楚了。",
        "把三步并作一步，并不能节省真正的工作。",
        "这里有一点问题，需要继续检查。",
        "结论先行。最后，镜头要按真实时长来拆。",
    )

    assert all(find_voice_focus_rules(copy) == () for copy in negative_copies)


def test_automatic_plan_binds_exact_copy_and_provider_boundaries():
    plan = automatic_emphasis_plan(COPY)

    assert plan is not None
    assert [(COPY[cue.start_char:cue.end_char], cue.emphasis) for cue in plan.cues] == [
        ("最后，", NarrationEmphasis.STRONG),
        ("记住三步：", NarrationEmphasis.STRONG),
    ]
    application = apply_focus_text_boundaries(COPY, COPY, plan)
    assert application.applied[0]["rule_id"] == "enumeration_terminal"
    assert application.applied[1]["rule_id"] == "quantity_frame"
    assert "最后，\n\n镜头" in application.provider_text
    assert "记住三步：\n\n先写" in application.provider_text
    assert application.target_break_positions == (
        COPY.index("最后，"),
        COPY.index("最后，") + len("最后，"),
        COPY.index("记住三步：") + len("记住三步："),
    )


def test_focus_text_application_skips_a_phrase_changed_by_provider_delivery():
    plan = automatic_emphasis_plan(COPY)
    assert plan is not None
    provider_text = COPY.replace("最后，", "最终！")

    application = apply_focus_text_boundaries(COPY, provider_text, plan)

    assert [item["copy_text"] for item in application.applied] == ["记住三步："]
    assert application.skipped[0]["reason"] == "provider_text_phrase_changed_or_ambiguous"
    assert application.provider_text.count("\n\n") >= 2


def test_focus_audio_boost_changes_only_the_exact_focus_segment(tmp_path: Path):
    plan = automatic_emphasis_plan(COPY)
    assert plan is not None
    cue = plan.cues[0]
    assert COPY.index("最后，") == cue.start_char
    frames_per_segment = 2_400
    samples = array("h")
    for amplitude in (4_000, 8_000, 12_000):
        for index in range(frames_per_segment):
            samples.append(round(amplitude * math.sin(2 * math.pi * 220 * index / 24_000)))
    if sys.byteorder != "little":
        samples.byteswap()
    source = tmp_path / "provider.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24_000)
        wav.writeframes(samples.tobytes())
    generation_segments = [
        {"start_char": 0, "end_char": cue.start_char, "start_sample": 0, "end_sample": 2_400},
        {"start_char": cue.start_char, "end_char": cue.end_char,
         "start_sample": 2_400, "end_sample": 4_800},
        {"start_char": cue.end_char, "end_char": len(COPY),
         "start_sample": 4_800, "end_sample": 7_200},
    ]

    application = apply_focus_audio_emphasis(
        source, tmp_path / "focused.wav", copy=COPY, plan=plan,
        generation_segments=generation_segments, sample_rate=24_000,
    )

    assert application.focus_segment_indexes == (1,)
    assert application.evidence["duration_preserved"] is True
    assert application.evidence["applications"][0]["applied_gain_db"] > 1.0
    with wave.open(str(source), "rb") as original, wave.open(str(application.output_path), "rb") as focused:
        assert original.getnframes() == focused.getnframes() == 7_200
        original_pcm = array("h")
        focused_pcm = array("h")
        original_pcm.frombytes(original.readframes(original.getnframes()))
        focused_pcm.frombytes(focused.readframes(focused.getnframes()))
    assert original_pcm[:2_400] == focused_pcm[:2_400]
    assert original_pcm[4_800:] == focused_pcm[4_800:]
    assert original_pcm[2_400:4_800] != focused_pcm[2_400:4_800]


def test_focus_audio_rewrites_only_changed_part_provenance_for_voice_qa(tmp_path: Path):
    plan = automatic_emphasis_plan(COPY)
    assert plan is not None
    cue = plan.cues[0]
    root = tmp_path / "data"
    parts_dir = root / "generated" / "voice" / "job.parts"
    parts_dir.mkdir(parents=True)
    segment_values = []
    all_samples = array("h")
    for index, amplitude in enumerate((4_000, 8_000, 12_000, 5_000, 7_000, 9_000, 6_000)):
        values = array("h", (
            round(amplitude * math.sin(2 * math.pi * 220 * sample / 24_000))
            for sample in range(1_200)
        ))
        part = parts_dir / f"{index:03}.wav"
        with wave.open(str(part), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24_000)
            wav.writeframes(values.tobytes())
        all_samples.extend(values)
        segment_values.append(values)
    source = parts_dir / "provider.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24_000)
        wav.writeframes(all_samples.tobytes())
    boundaries = [
        (0, cue.start_char),
        (cue.start_char, cue.end_char),
        (cue.end_char, 70),
        (70, 100),
        (100, 140),
        (140, 175),
        (175, len(COPY)),
    ]
    generation_segments = []
    for index, ((start_char, end_char), values) in enumerate(zip(boundaries, segment_values)):
        part = parts_dir / f"{index:03}.wav"
        generation_segments.append({
            "start_char": start_char,
            "end_char": end_char,
            "start_sample": index * 1_200,
            "end_sample": (index + 1) * 1_200,
            "source_file": part.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(part.read_bytes()).hexdigest(),
        })

    application = apply_focus_audio_emphasis(
        source,
        parts_dir / "focused.wav",
        copy=COPY,
        plan=plan,
        generation_segments=generation_segments,
        sample_rate=24_000,
        data_root=root,
    )

    assert application.generation_segments[1]["source_file"].endswith(
        "001.focus-emphasis.wav"
    )
    assert application.generation_segments[1]["sha256"] != generation_segments[1]["sha256"]
    assert application.generation_segments[0] == generation_segments[0]

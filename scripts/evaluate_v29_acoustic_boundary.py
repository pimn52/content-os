"""Read-only audit of PCM quiet against independently recorded Composer seams.

Composer sample counts annotate *manufactured seams*, not natural-language
word endings. This script intentionally creates no candidate and no edit rule.
Run with PYTHONPATH=services/api and the local Python environment.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import wave

from app.voice_observation import _measure_quiet


ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _frames(path: Path) -> tuple[int, int]:
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("expected mono PCM16 WAV")
        return wav.getframerate(), wav.getnframes()


def annotated_seams(child_frames: list[int], pause_ms: list[int], sample_rate: int) -> list[tuple[int, int]]:
    """Return exact sample-frame ranges between independently preserved takes."""
    if len(child_frames) < 2 or len(child_frames) != len(pause_ms) or any(n <= 0 for n in child_frames):
        raise ValueError("invalid composition annotation")
    if sample_rate <= 0 or any(ms < 0 or ms * sample_rate % 1000 for ms in pause_ms):
        raise ValueError("pause does not map to whole PCM frames")
    offset = 0
    seams: list[tuple[int, int]] = []
    for frames, pause in zip(child_frames[:-1], pause_ms[:-1]):
        offset += frames
        begin = offset
        offset += pause * sample_rate // 1000
        seams.append((begin, offset))
    return seams


def _composition(name: str, child_paths: list[Path], child_hashes: list[str],
                 pause_ms: list[int], master_path: Path, master_hash: str) -> dict:
    for path, expected in zip(child_paths, child_hashes, strict=True):
        if _sha256(path) != expected:
            raise ValueError(f"child source hash changed: {path.name}")
    if _sha256(master_path) != master_hash:
        raise ValueError(f"master source hash changed: {master_path.name}")
    child_info = [_frames(path) for path in child_paths]
    rate, master_frames = _frames(master_path)
    if any(child_rate != rate for child_rate, _ in child_info):
        raise ValueError("composition sample rates differ")
    frames = [count for _, count in child_info]
    if sum(frames) + sum(pause_ms) * rate // 1000 != master_frames:
        raise ValueError("manifest children and pauses do not reproduce master frame count")
    seams = annotated_seams(frames, pause_ms, rate)
    _, quiet, _, _ = _measure_quiet(master_path)
    matches = []
    for index, (begin, end) in enumerate(seams):
        begin_ms, end_ms = begin * 1000 / rate, end * 1000 / rate
        crossing = [item for item in quiet if item["start_ms"] < end_ms and item["end_ms"] > begin_ms]
        matches.append({
            "index": index, "seam_begin_ms": round(begin_ms, 3), "seam_end_ms": round(end_ms, 3),
            "pcm_quiet_overlapping_seam": crossing,
        })
    return {
        "family": name, "source_sha256": master_hash, "sample_rate_hz": rate,
        "master_frames": master_frames, "annotated_seams": matches,
        "interior_quiet_intervals": [item for item in quiet if item["start_ms"] > 0 and item["end_ms"] < round(master_frames * 1000 / rate)],
    }


def evaluate(root: Path = ROOT) -> dict:
    evidence, data = root / "content-os-data" / "evaluation-evidence", root / "content-os-data"
    v23_dir, v24_dir = evidence / "v23-a-sentence-composition", evidence / "v24-pace-coherence"
    v23 = json.loads((v23_dir / "v23-a-sentence-composition.json").read_text(encoding="utf-8"))
    v24 = json.loads((v24_dir / "v24-pace-coherence.json").read_text(encoding="utf-8"))
    v23_children = [v23_dir / Path(item["path"]).name for item in v23["children"]]
    v24_children = [v24_dir / Path(v24["children"][0]["path"]).name,
                    v23_children[1], v24_dir / Path(v24["children"][1]["path"]).name]
    compositions = [
        _composition("V23-V24-same-copy", v23_children, [item["sha256"] for item in v23["children"]],
                     v23["master"]["added_pause_after_ms"], v23_dir / "v23-a-sentence-master.wav",
                     v23["master"]["sha256"]),
        _composition("V23-V24-same-copy", v24_children,
                     [v24["children"][0]["sha256"], v24["unchanged_a_child_sha256"], v24["children"][1]["sha256"]],
                     v24["master"]["added_pause_after_ms"], v24_dir / "v24-pace-coherence-master.wav",
                     v24["master"]["sha256"]),
    ]
    db_path = data / "content-os.sqlite3"
    with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT payload FROM audio_assets WHERE id = ?", (v24["master"]["audio_asset_id"],)).fetchone()
    if row is None:
        raise ValueError("V24 AudioAsset missing")
    master_asset = json.loads(row[0])
    if master_asset["content_hash"] != v24["master"]["sha256"]:
        raise ValueError("V24 AudioAsset hash differs from manifest")
    generation = master_asset["metadata"]["voice_generation"]
    span_ends = generation["composition"]["span_boundaries"][1:-1]
    boundaries = master_asset["metadata"]["voice_boundary_alignment"]["boundaries"]
    word_edge_vs_seam = []
    for char_index, seam in zip(span_ends, compositions[1]["annotated_seams"], strict=True):
        boundary = next(item for item in boundaries if item["char_index"] == char_index)
        edge = boundary["word_gap_start_ms"]
        word_edge_vs_seam.append({
            "copy_char_index": char_index, "asr_word_edge_ms": edge,
            "offset_from_composer_seam_begin_ms": round(edge - seam["seam_begin_ms"], 3),
            "offset_from_composer_next_take_ms": round(edge - seam["seam_end_ms"], 3),
        })
    shortest_matched_quiet = min(
        item["duration_ms"] for composition in compositions
        for seam in composition["annotated_seams"] for item in seam["pcm_quiet_overlapping_seam"]
    )
    prior = json.loads((evidence / "v26-timing-preflight" / "v26-timing-preflight.json").read_text(encoding="utf-8"))
    raw_controls = []
    for item in prior["different_copy_candidates"]:
        source = data / "assets" / "audio-originals" / f"{item['source_sha256']}.wav"
        if _sha256(source) != item["source_sha256"]:
            raise ValueError("different-copy source hash changed")
        _, quiet, duration, _ = _measure_quiet(source)
        internal = [q for q in quiet if q["start_ms"] > 0 and q["end_ms"] < duration]
        raw_controls.append({
            "audio_asset_id": item["audio_asset_id"], "source_sha256": item["source_sha256"],
            "interior_quiet_at_least_shortest_composed_join": [
                q for q in internal if q["duration_ms"] >= shortest_matched_quiet
            ],
            "boundary_truth": "unannotated_natural_take",
        })
    return {
        "method": "read_only_exact_composer_frames_vs_pcm_quiet_10ms_minus45dbfs",
        "shortest_matched_composed_join_quiet_ms": shortest_matched_quiet,
        "compositions": compositions, "different_copy_raw_controls": raw_controls,
        "v24_word_edge_vs_independent_composer_seam": word_edge_vs_seam,
        "generalization_state": "unsupported_no_independent_copy_boundary_annotations",
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))

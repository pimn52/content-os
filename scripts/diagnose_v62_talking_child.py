"""No-inference staging diagnostic for V61's failed final Talking child."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, JobRepository  # noqa: E402
from app.media import FFProbeAdapter  # noqa: E402
from app.providers.latentsync import LatentSyncProvider  # noqa: E402
from app.providers.talking import TalkingReference  # noqa: E402
from app.talking.slices import extract_talking_audio_slice, plan_talking_audio_slice  # noqa: E402

DATA = ROOT / "content-os-data"
EVIDENCE = DATA / "evaluation-evidence" / "v62-talking-child" / "staging.json"
FAILED_JOB_ID = UUID("16d88b58-e26a-4f1c-a5cb-07b84eba4104")


def source_path(source_file: str) -> Path:
    path = Path(source_file)
    if path.is_absolute():
        return path
    return DATA.parent / path if path.parts and path.parts[0].casefold() == DATA.name.casefold() else DATA / path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def main() -> None:
    if EVIDENCE.exists():
        raise ValueError("V62 no-inference staging diagnostic already exists")
    runtime = DATA / "latentsync-runtime310"
    repo = DATA / "latentsync"
    ffmpeg = runtime / "Library" / "bin" / "ffmpeg.exe"
    ffprobe = runtime / "Library" / "bin" / "ffprobe.exe"
    provider = LatentSyncProvider(
        runtime / "python.exe", repo, DATA / "latentsync-checkpoints" / "latentsync_unet.pt",
        runner_path=DATA / "latentsync-evaluation-20260915" / "run_latentsync_math.py",
        unet_config_path=repo / "configs" / "unet" / "stage2.yaml",
        ffmpeg_command=ffmpeg, inference_steps=20, guidance_scale=1.5,
        seed=1247, timeout_seconds=1200,
    )
    with Database(DATA / "content-os.sqlite3") as db:
        job = JobRepository(db).get(FAILED_JOB_ID)
        if job is None or job.status.value != "failed" or job.attempt != 1 or job.error_code != "talking_invalid_request":
            raise ValueError("V61 original failed Job changed")
        payload = job.payload
        master = AudioAssetRepository(db).get(payload.narration_audio_id)
        clip = ClipRepository(db).get(payload.reference_clip_id)
        asset = None if clip is None else AssetRepository(db).get(clip.asset_id)
        if master is None or clip is None or asset is None:
            raise ValueError("V61 exact source media is unavailable")
        master_path = source_path(master.source_file)
        asset_path = source_path(asset.source_file)
        if digest(master_path) != master.content_hash or digest(asset_path) != asset.content_hash:
            raise ValueError("Master or reference source hash changed")
        plan = plan_talking_audio_slice(
            master, start_segment_index=payload.slice_start_segment_index,
            end_segment_index=payload.slice_end_segment_index,
            max_duration_ms=payload.slice_max_duration_ms,
        )
        reference = TalkingReference(
            clip_id=clip.id, source_path=asset_path,
            start_ms=payload.reference_window_start_ms,
            end_ms=payload.reference_window_end_ms,
            subtitle_crop_bottom_ratio=0.18,
        )
        with TemporaryDirectory(prefix="v62-staging-", dir=DATA / "evaluation-evidence") as temporary:
            root = Path(temporary)
            narration = extract_talking_audio_slice(
                master.model_copy(update={"source_file": str(master_path)}), plan,
                root / "slice.wav", ffmpeg_command=ffmpeg,
            )
            model_duration_ms = provider._model_duration_ms(narration.duration_ms)
            provider._stage_reference(
                reference, root / "reference.mp4",
                reference.end_ms - reference.start_ms, model_duration_ms,
            )
            provider._prepare_model_audio(
                Path(narration.source_file), root / "narration-model.wav", narration.duration_ms,
            )
            probe = FFProbeAdapter(str(ffprobe))
            staged_video = probe.probe(root / "reference.mp4")
            staged_audio = probe.probe_audio(root / "narration-model.wav")
            if staged_video.duration_ms + 80 < plan.duration_ms or staged_audio.duration_ms + 80 < model_duration_ms:
                raise ValueError("last-child staged input is shorter than its required delivery/model duration")
            evidence = {
                "package": "V62", "status": "deterministic_input_staging_pass",
                "failed_job_id": str(FAILED_JOB_ID), "failed_provider_call_id": "15f62028-9ef4-4d43-ae8a-33c3cd7776e9",
                "master_audio_id": str(master.id), "master_sha256": master.content_hash,
                "source_asset_id": str(asset.id), "source_sha256": asset.content_hash,
                "master_interval_ms": [plan.start_ms, plan.end_ms],
                "reference_interval_ms": [reference.start_ms, reference.end_ms],
                "delivery_duration_ms": plan.duration_ms,
                "model_duration_ms": model_duration_ms,
                "staged_video_duration_ms": staged_video.duration_ms,
                "staged_audio_duration_ms": staged_audio.duration_ms,
                "inference_attempted": False,
                "conclusion": "Exact source and audio stage/decode within bounds. Original broad failure remains unattributed to a precise runtime phase; no claim that model inference succeeds.",
            }
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence))


if __name__ == "__main__":
    main()

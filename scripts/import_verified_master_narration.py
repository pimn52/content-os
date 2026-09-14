"""Persist one independently QA-verified composed narration through AudioImporter.

This is an evaluation bridge, not a provider adapter: it imports a local WAV
through the normal asset path, then persists the independent ASR timing and
the source-take manifest beside provider-neutral generation metadata.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services" / "api"))

from app.db import AudioAssetRepository, Database  # noqa: E402
from app.domain.models import TranscriptSegment  # noqa: E402
from app.media import AudioImporter, FFProbeAdapter  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--qa", required=True, type=Path)
    parser.add_argument("--composition", required=True, type=Path)
    parser.add_argument("--authorization-reference", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    qa = json.loads(args.qa.read_text(encoding="utf-8"))
    composition = json.loads(args.composition.read_text(encoding="utf-8"))
    if qa.get("qa", {}).get("verified") is not True:
        raise SystemExit("master narration QA is not verified")
    if composition.get("status") != "provisional_pending_master_asr_qa":
        raise SystemExit("composition manifest is invalid")
    audio_info = qa.get("audio")
    generation = qa.get("updated_asset_metadata", {}).get("voice_generation")
    transcription = qa.get("transcription")
    if not isinstance(audio_info, dict) or not isinstance(generation, dict) or not isinstance(transcription, dict):
        raise SystemExit("master narration evidence is malformed")
    source = Path(str(audio_info.get("path", ""))).resolve()
    if not source.is_file():
        raise SystemExit("master narration WAV is unavailable")
    db = Database(args.database.resolve())
    try:
        imported = AudioImporter(db, args.data_root.resolve(), FFProbeAdapter(resolve_local_executable("ffprobe"))).import_path(
            source, args.authorization_reference, language=transcription.get("language"),
        )
        metadata = dict(imported.metadata)
        metadata["voice_generation"] = generation
        metadata["master_narration_composition"] = {
            "source_evidence": composition.get("source_evidence"),
            "source_asset_ids": composition.get("source_asset_ids"),
            "expected_duration_ms": composition.get("expected_duration_ms"),
            "master_qa": str(args.qa.resolve()),
        }
        updated = imported.model_copy(update={
            "metadata": metadata,
            "transcript_segments": [TranscriptSegment(**item) for item in transcription.get("segments", [])],
            "transcript_source": f"independent-asr:{qa.get('provider_model')}",
        })
        with db.transaction():
            AudioAssetRepository(db).update(updated)
        print(updated.id)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

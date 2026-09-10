"""Import explicitly selected local media without any provider call."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.db import AssetRepository, Database
from app.domain.models import SourceKind
from app.media import FFProbeAdapter, MediaImporter


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("--db", type=Path, default=Path("content-os-data") / "content-os.sqlite3")
    parser.add_argument("--data-root", type=Path, default=Path("content-os-data"))
    parser.add_argument("--rights-ref", default="user-designated-local-materials-20260909")
    parser.add_argument("--source-kind", choices=(SourceKind.USER_ASSET.value, SourceKind.HISTORICAL_ASSET.value), default=SourceKind.HISTORICAL_ASSET.value)
    parser.add_argument("--ffprobe", required=True)
    args = parser.parse_args()
    source_kind = SourceKind(args.source_kind)
    with Database(args.db) as db:
        importer = MediaImporter(db, args.data_root, FFProbeAdapter(args.ffprobe))
        repository = AssetRepository(db)
        values = []
        for source in args.sources:
            asset = importer.import_path(source, args.rights_ref, source_kind=source_kind)
            metadata = {
                **asset.metadata,
                "r1_usage": "reference",
                "r1_identity": "unknown",
                "local_import_note": "user-designated local material; rights and identity remain unconfirmed",
            }
            asset = asset.model_copy(update={"metadata": metadata})
            repository.update(asset)
            values.append(asset.model_dump(mode="json"))
        print(json.dumps(values, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


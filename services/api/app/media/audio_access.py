"""Check stored audio authorization and bytes before playback."""
from pathlib import Path
import hashlib

from app.execution_admission import UseAdmissionError
from app.media.local_paths import existing_local_media_file


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require_ordinary_media(db, root, path, *, kind, subject_id, content_hash=None):
    """Keep stored media integrity checks independent from product authorization."""
    actual = file_digest(existing_local_media_file(str(path), Path(root).resolve()))
    if content_hash is not None and actual != content_hash:
        raise UseAdmissionError("media_content_hash_changed")


class AudioAccess:
    def __init__(self, db, root):
        self.db, self.root = db, Path(root).resolve()

    def inspect(self, audio, *, project_id=None, purpose=None, operation="review"):
        result = {"audio_id": str(audio.id), "state": "blocked", "reasons": [],
            "operation": "review", "project_id": str(project_id) if project_id else None}
        try:
            if operation != "review":
                raise UseAdmissionError("provider_use_operation_unsupported")
            source = existing_local_media_file(audio.source_file, self.root)
            actual = file_digest(source)
            if actual != audio.content_hash:
                raise UseAdmissionError("audio_content_hash_changed")
            result["state"] = "available"
            result["restricted"] = False
            return result, source
        except UseAdmissionError as exc:
            result["reasons"] = [str(exc)]
        except FileNotFoundError:
            result["reasons"] = ["audio_source_missing"]
        except (ValueError, OSError):
            result["reasons"] = ["audio_source_invalid"]
        return result, None

"""Creation-bound allowance shared by existing Talking and new Voice records."""
from uuid import UUID
from app.db import Database


def used_repairs(db: Database, run_id: UUID) -> int:
    # Existing records are counted directly; no backfill can reset their usage.
    return db.connection.execute(
        "SELECT (SELECT count(*) FROM talking_repairs WHERE run_id = ?) + "
        "(SELECT count(*) FROM voice_repairs WHERE run_id = ?) + "
        "(SELECT count(*) FROM presentation_repairs WHERE run_id = ?)",
        (str(run_id), str(run_id), str(run_id)),
    ).fetchone()[0]

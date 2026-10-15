"""Small hash-checked sidecars; ownership/slot indexes survive sidecar loss."""
import json
from app.domain.models import execution_canonical_sha256 as digest


class CompositionRepository:
    def __init__(self, db):
        self.db = db

    def get(self, kind, identity):
        row = self.db.connection.execute(
            "SELECT payload,payload_sha256 FROM evaluation_composition_records WHERE kind=? AND id=?",
            (kind, str(identity))).fetchone()
        if row is None:
            return None
        value = json.loads(row[0])
        if digest(value) != row[1] or value.get("id") != str(identity) or value.get("kind") != kind:
            raise ValueError("composition_record_changed")
        return value

    def save(self, kind, identity, value, *, update=False):
        value = dict(value, id=str(identity), kind=kind)
        if update:
            self.db.connection.execute("UPDATE evaluation_composition_records SET payload=?,payload_sha256=? WHERE kind=? AND id=?",
                (json.dumps(value), digest(value), kind, str(identity)))
        else:
            self.db.connection.execute("INSERT INTO evaluation_composition_records(kind,id,payload,payload_sha256) VALUES (?,?,?,?)",
                (kind, str(identity), json.dumps(value), digest(value)))
        return value

    def list(self, kind, project_id):
        return [self.get(kind, r[0]) for r in self.db.connection.execute(
            "SELECT id FROM evaluation_composition_records WHERE kind=? ORDER BY rowid DESC", (kind,))
            if self.get(kind, r[0]).get("project_id") == str(project_id)]

    def owner(self, *, run_id=None, job_id=None, render_id=None):
        name, value = next((k, v) for k, v in (("run_id",run_id),("job_id",job_id),("render_id",render_id)) if v is not None)
        return self.db.connection.execute(f"SELECT * FROM evaluation_composition_owners WHERE {name}=?", (str(value),)).fetchone()

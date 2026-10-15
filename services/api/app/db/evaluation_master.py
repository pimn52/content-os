"""Integrity checked sidecars; no changes to source media or execution receipts."""
from app.domain.models import execution_canonical_sha256 as digest
from app.domain.evaluation_master import EvaluationMasterUseAdmission, EvaluationRunMasterBindingV1


class EvaluationMasterRepository:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _read(row, model, columns):
        if row is None:
            return None
        try:
            value = model.model_validate_json(row["payload"])
            if digest(value.model_dump(mode="json")) != row["payload_sha256"]:
                raise ValueError()
            if any(str(getattr(value, key)) != row[key] for key in columns):
                raise ValueError()
            return value
        except (ValueError, KeyError, TypeError):
            raise ValueError("evaluation_master_record_invalid") from None

    def admission(self, admission_id):
        row = self.db.connection.execute("SELECT * FROM evaluation_master_use_admissions WHERE id=?", (str(admission_id),)).fetchone()
        return self._read(row, EvaluationMasterUseAdmission, ("id", "project_id", "idempotency_key"))

    def admissions(self, project_id):
        return [self.admission(r[0]) for r in self.db.connection.execute(
            "SELECT id FROM evaluation_master_use_admissions WHERE project_id=? ORDER BY rowid DESC", (str(project_id),))]

    def save_admission(self, value, *, revoke=False):
        raw, sha = value.model_dump_json(), digest(value.model_dump(mode="json"))
        if revoke:
            self.db.connection.execute("UPDATE evaluation_master_use_admissions SET payload=?,payload_sha256=? WHERE id=?", (raw, sha, str(value.id)))
        else:
            self.db.connection.execute("INSERT INTO evaluation_master_use_admissions(id,project_id,idempotency_key,payload_sha256,payload) VALUES (?,?,?,?,?)",
                (str(value.id), str(value.project_id), value.idempotency_key, sha, raw))
        return self.admission(value.id)

    def binding(self, run_id):
        row = self.db.connection.execute("SELECT * FROM evaluation_run_master_bindings WHERE run_id=?", (str(run_id),)).fetchone()
        value = self._read(row, EvaluationRunMasterBindingV1, ("run_id", "project_id", "request_sha256", "admission_id"))
        if value and digest(value.request) != value.request_sha256:
            raise ValueError("evaluation_master_request_changed")
        return value

    def bind(self, value):
        self.db.connection.execute("INSERT INTO evaluation_run_master_bindings(run_id,project_id,request_sha256,admission_id,payload_sha256,payload) VALUES (?,?,?,?,?,?)",
            (str(value.run_id), str(value.project_id), value.request_sha256, str(value.admission_id), digest(value.model_dump(mode="json")), value.model_dump_json()))
        return self.binding(value.run_id)

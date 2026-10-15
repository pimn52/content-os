"""Immutable operation/claim identities and append-only execution projections."""
from app.domain.models import execution_canonical_sha256 as digest
from app.domain.evaluation_talking import TalkingOperationRecord, EvaluationTalkingUseAdmission
from pydantic import TypeAdapter
from .evaluation_master import EvaluationMasterRepository


class _OperationParser:
    model_validate_json = staticmethod(TypeAdapter(TalkingOperationRecord).validate_json)


class EvaluationTalkingRepository:
    def __init__(self, db): self.db = db

    def admission(self, identity):
        row = self.db.connection.execute("SELECT * FROM evaluation_talking_use_admissions WHERE id=?", (str(identity),)).fetchone()
        return EvaluationMasterRepository._read(row, EvaluationTalkingUseAdmission, ("id", "project_id", "idempotency_key"))

    def save_admission(self, value, *, revoke=False):
        raw, sha = value.model_dump_json(), digest(value.model_dump(mode="json"))
        if revoke:
            self.db.connection.execute("UPDATE evaluation_talking_use_admissions SET payload=?,payload_sha256=? WHERE id=?", (raw,sha,str(value.id)))
        else:
            self.db.connection.execute("INSERT INTO evaluation_talking_use_admissions(id,project_id,idempotency_key,payload_sha256,payload) VALUES (?,?,?,?,?)",
                (str(value.id),str(value.project_id),value.idempotency_key,sha,raw))
        return self.admission(value.id)

    def operation(self, identity):
        row = self.db.connection.execute("SELECT * FROM evaluation_talking_operations WHERE id=?", (str(identity),)).fetchone()
        value = EvaluationMasterRepository._read(row, _OperationParser,
            ("id","project_id","master_run_id","request_sha256","inference_sha256","admission_id","generation_job_id","qa_job_id","preview_job_id","series_id"))
        if value and digest(value.request) != value.request_sha256:
            raise ValueError("evaluation_talking_request_changed")
        return value

    def insert(self, value, key):
        self.db.connection.execute("""INSERT INTO evaluation_talking_operations(id,project_id,master_run_id,idempotency_key,
            request_sha256,inference_sha256,admission_id,generation_job_id,qa_job_id,preview_job_id,series_id,payload_sha256,payload)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (str(value.id),str(value.project_id),str(value.master_run_id),key,value.request_sha256,
                value.inference_sha256,str(value.admission_id),str(value.generation_job_id),str(value.qa_job_id),str(value.preview_job_id),
                str(value.series_id),digest(value.model_dump(mode="json")),value.model_dump_json()))
        return self.operation(value.id)

    def for_job(self, identity):
        row = self.db.connection.execute("SELECT operation_id FROM evaluation_talking_owned_jobs WHERE job_id=?", (str(identity),)).fetchone()
        if row is None: return None
        value = self.operation(row[0])
        if value is None: raise ValueError("evaluation_talking_owner_missing")
        return value

    def result(self, identity, role):
        row = self.db.connection.execute("SELECT payload,payload_sha256 FROM evaluation_talking_results WHERE operation_id=? AND role=?", (str(identity),role)).fetchone()
        if row is None: return None
        import json
        payload = json.loads(row[0])
        if digest(payload) != row[1]: raise ValueError("evaluation_talking_result_changed")
        return payload

    def save_result(self, identity, role, payload):
        import json
        old = self.result(identity,role)
        if old is not None:
            if old != payload: raise ValueError("evaluation_talking_result_immutable")
            return old
        self.db.connection.execute("INSERT INTO evaluation_talking_results(operation_id,role,payload_sha256,payload) VALUES (?,?,?,?)",
            (str(identity),role,digest(payload),json.dumps(payload,sort_keys=True)))
        return payload

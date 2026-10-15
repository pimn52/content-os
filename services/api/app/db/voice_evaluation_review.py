"""Create/read-only review repository with explicit column/payload integrity."""
from app.domain.models import execution_canonical_sha256
from app.domain.voice_evaluation_review import EvaluationVoiceReviewRecord


class EvaluationVoiceReviewRepository:
    def __init__(self, db):
        self.db = db

    def get_by_audio(self, audio_id):
        row = self.db.connection.execute("SELECT * FROM voice_evaluation_human_reviews WHERE audio_id=?", (str(audio_id),)).fetchone()
        if row is None:
            return None
        try:
            value = EvaluationVoiceReviewRecord.model_validate_json(row["payload"])
            for key in ("id", "project_id", "audio_id", "qa_job_id"):
                if str(getattr(value, key)) != row[key]:
                    raise ValueError()
            if execution_canonical_sha256(value.model_dump(mode="json")) != row["payload_sha256"]:
                raise ValueError()
            value.validate_submission()
            return value
        except (ValueError, KeyError, TypeError):
            raise ValueError("voice_human_review_record_invalid") from None

    def create(self, value):
        value.validate_submission()
        self.db.connection.execute(
            "INSERT INTO voice_evaluation_human_reviews(id,project_id,audio_id,qa_job_id,payload_sha256,payload) VALUES (?,?,?,?,?,?)",
            (str(value.id), str(value.project_id), str(value.audio_id), str(value.qa_job_id),
             execution_canonical_sha256(value.model_dump(mode="json")), value.model_dump_json()))
        return self.get_by_audio(value.audio_id)

"""Durable use restrictions, separate from media QA and inference authority.

Sidecars leave legacy canonical payloads unchanged. These application-only
methods are not an issuance API: scope creation requires current operator
receipts, and no scope record enables the unfinished evaluation execution lane.
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import TypeAdapter, field_validator, model_validator

from app.db.database import Database
from app.domain.models import (ContractModel, ExecutionPurpose, ExecutionSpecificationRecord,
    ExecutionUseIdentity, Job, Sha256, ProviderUseLicenseReviewV2)
from app.execution_admission import ProviderUseAdmissionService, UseAdmissionError


SubjectKind = Literal["run", "job", "audio", "asset", "image", "talking_run", "preview", "render"]


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def job_use_identity(job: Job) -> str:
    """Status/attempt/lease changes are not changes to execution input."""
    return _digest({"project_id": str(job.project_id), "type": job.type.value,
        "idempotency_key": job.idempotency_key,
        "payload": None if job.payload is None else job.payload.model_dump(mode="json")})


class ExecutionUseBinding(ContractModel):
    admission_id: UUID
    capability_profile_id: UUID
    review_sha256: Sha256
    configuration_sha256: Sha256
    identity: ExecutionUseIdentity


class ExecutionUseBindingV2(ExecutionUseBinding):
    attestation_version: Literal[2]
    execution_specification: ExecutionSpecificationRecord
    execution_sha256: Sha256

    @field_validator("attestation_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError("execution_version_invalid")
        return value

    @model_validator(mode="after")
    def valid_execution(self):
        if (self.execution_specification.execution_sha256 != self.execution_sha256
            or self.execution_specification.identity != self.identity):
            raise ValueError("execution_attestation_mismatch")
        return self


class ExecutionUseScope(ContractModel):
    policy_version: Literal[1] = 1
    project_id: UUID
    purpose: ExecutionPurpose
    bindings: tuple[ExecutionUseBinding, ...] = ()

    @model_validator(mode="after")
    def valid_bindings(self):
        ids = tuple(str(binding.admission_id) for binding in self.bindings)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("execution_use_bindings_must_be_unique_and_sorted")
        if self.purpose == "commercial_production" and self.bindings:
            raise ValueError("evaluation_scope_not_commercial")
        if self.purpose == "internal_evaluation" and not self.bindings:
            raise ValueError("provider_use_admission_missing")
        return self

    @property
    def fingerprint(self) -> str:
        return _digest(self.model_dump(mode="json"))


class ExecutionUseScopeV2(ExecutionUseScope):
    policy_version: Literal[2]
    bindings: tuple[ExecutionUseBinding | ExecutionUseBindingV2, ...] = ()

    @field_validator("policy_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError("execution_version_invalid")
        return value

    @model_validator(mode="after")
    def contains_v2(self):
        if not any(isinstance(binding, ExecutionUseBindingV2) for binding in self.bindings):
            raise ValueError("execution_attestation_upgrade_required")
        return self


def _make_scope(project_id, purpose, bindings):
    if any(isinstance(binding, ExecutionUseBindingV2) for binding in bindings):
        return ExecutionUseScopeV2(policy_version=2, project_id=project_id, purpose=purpose, bindings=bindings)
    return ExecutionUseScope(project_id=project_id, purpose=purpose, bindings=bindings)


def _parse_scope(raw: str):
    return TypeAdapter(ExecutionUseScope | ExecutionUseScopeV2).validate_json(raw)


class WorkerExecutionUseSnapshot(ContractModel):
    """Legacy evidence-digest shape; cannot satisfy v2 execution observation."""
    identity: ExecutionUseIdentity
    configuration_sha256: Sha256


class WorkerExecutionUseSnapshotV2(ContractModel):
    """Independent selected-artifact observation; no evidence-row digest."""
    snapshot_version: Literal[2]
    identity: ExecutionUseIdentity
    execution_specification: ExecutionSpecificationRecord
    execution_sha256: Sha256

    @field_validator("snapshot_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError("execution_version_invalid")
        return value

    @model_validator(mode="after")
    def valid_execution(self):
        if (self.execution_sha256 != self.execution_specification.execution_sha256
            or self.identity != self.execution_specification.identity):
            raise ValueError("execution_attestation_mismatch")
        return self


class ExecutionScopeService:
    def __init__(self, db: Database, data_root: str | Path):
        self.db = db
        self.admissions = ProviderUseAdmissionService(db, data_root)

    def prepare(self, project_id: UUID, *, purpose: ExecutionPurpose = "commercial_production",
                admission_ids: tuple[UUID, ...] = ()) -> ExecutionUseScope:
        if purpose not in ("commercial_production", "internal_evaluation"):
            raise UseAdmissionError("execution_purpose_unsupported")
        if purpose == "commercial_production" and admission_ids:
            raise UseAdmissionError("evaluation_scope_not_commercial")
        if len(set(admission_ids)) != len(admission_ids):
            raise UseAdmissionError("provider_use_admission_duplicate")
        bindings = []
        for admission_id in sorted(admission_ids, key=str):
            receipt = self.admissions.get(project_id, admission_id)
            if receipt is None:
                raise UseAdmissionError("provider_use_admission_missing")
            report = receipt.review
            fields = dict(admission_id=receipt.id,
                capability_profile_id=report.capability_profile_id, review_sha256=receipt.review_sha256,
                configuration_sha256=report.configuration_sha256, identity=report.identity)
            if isinstance(report, ProviderUseLicenseReviewV2):
                bindings.append(ExecutionUseBindingV2(**fields, attestation_version=2,
                    execution_specification=report.execution_specification, execution_sha256=report.execution_sha256))
            else:
                bindings.append(ExecutionUseBinding(**fields))
        if purpose == "internal_evaluation" and not bindings:
            raise UseAdmissionError("provider_use_admission_missing")
        scope = _make_scope(project_id, purpose, tuple(bindings))
        self.require_current(scope, project_id=project_id, purpose=purpose, operation="generation")
        return scope

    def require_current(self, scope: ExecutionUseScope, *, project_id: UUID,
                        purpose: ExecutionPurpose, operation: str) -> None:
        if purpose not in ("commercial_production", "internal_evaluation"):
            raise UseAdmissionError("execution_purpose_unsupported")
        if operation not in ("generation", "derivation", "review", "download"):
            raise UseAdmissionError("provider_use_operation_unsupported")
        if scope.purpose == "internal_evaluation" and purpose != scope.purpose:
            raise UseAdmissionError("evaluation_scope_not_commercial")
        if scope.project_id != project_id:
            raise UseAdmissionError("provider_use_project_mismatch")
        for binding in scope.bindings:
            receipt = self.admissions.get(project_id, binding.admission_id)
            if receipt is None:
                raise UseAdmissionError("provider_use_admission_missing")
            if (receipt.review_sha256 != binding.review_sha256
                or receipt.review.configuration_sha256 != binding.configuration_sha256
                or receipt.review.identity != binding.identity):
                raise UseAdmissionError("provider_use_binding_changed")
            if isinstance(binding, ExecutionUseBindingV2):
                if (not isinstance(receipt.review, ProviderUseLicenseReviewV2)
                    or receipt.review.execution_sha256 != binding.execution_sha256
                    or receipt.review.execution_specification != binding.execution_specification):
                    raise UseAdmissionError("provider_use_binding_changed")
            elif isinstance(receipt.review, ProviderUseLicenseReviewV2):
                raise UseAdmissionError("execution_attestation_upgrade_required")
            decision = self.admissions.resolve(project_id, capability_profile_id=binding.capability_profile_id,
                identity=binding.identity, purpose=scope.purpose, operation=operation, admission_id=binding.admission_id)
            if decision.state != "allowed_for_scope":
                raise UseAdmissionError(decision.reasons[0])

    def get(self, kind: SubjectKind, subject_id: UUID) -> ExecutionUseScope | None:
        return read_subject_scope(self.db, kind, subject_id)

    def pin(self, kind: SubjectKind, subject_id: UUID, scope: ExecutionUseScope,
            *, operation: str, subject_sha256: str | None = None, content_hash: str | None = None) -> None:
        """Immutable application sidecar; caller must own the subject transaction."""
        scope = _parse_scope(scope.model_dump_json())
        self._pin(kind, subject_id, scope, operation=operation, subject_sha256=subject_sha256, content_hash=content_hash)

    def _pin(self, kind: SubjectKind, subject_id: UUID, scope: ExecutionUseScope,
            *, operation: str, subject_sha256: str | None, content_hash: str | None) -> None:
        """No caller-supplied permission fields can bypass typed scope validation."""
        if kind not in ("run", "job", "audio", "asset", "image", "talking_run", "preview", "render"):
            raise UseAdmissionError("execution_use_subject_unsupported")
        for digest in (subject_sha256, content_hash):
            if digest is not None:
                # Validate even under model_copy/update or direct service calls.
                from pydantic import TypeAdapter
                try:
                    TypeAdapter(Sha256).validate_python(digest)
                except ValueError:
                    raise UseAdmissionError("execution_use_hash_invalid") from None
        if kind == "job" and subject_sha256 is None:
            raise UseAdmissionError("execution_use_job_identity_required")
        with (nullcontext() if self.db.connection.in_transaction else self.db.transaction(immediate=True)):
            if kind == "job":
                from app.db.repositories import JobRepository
                job = JobRepository(self.db).get(subject_id)
                if job is None or job.project_id != scope.project_id or job_use_identity(job) != subject_sha256:
                    raise UseAdmissionError("execution_use_job_changed")
            self.require_current(scope, project_id=scope.project_id, purpose=scope.purpose, operation=operation)
            if content_hash is not None:
                for old in self.scopes_for_hash(content_hash):
                    if old.purpose == "internal_evaluation" and scope.purpose == "commercial_production":
                        raise UseAdmissionError("evaluation_scope_not_commercial")
                    if old.project_id != scope.project_id:
                        raise UseAdmissionError("provider_use_project_mismatch")
                    if not set(b.admission_id for b in old.bindings).issubset(b.admission_id for b in scope.bindings):
                        raise UseAdmissionError("execution_use_ancestor_dropped")
            row = self.db.connection.execute(
                "SELECT * FROM execution_use_scopes WHERE subject_kind=? AND subject_id=?", (kind, str(subject_id))).fetchone()
            if row is not None:
                if self.get(kind, subject_id) != scope or row["subject_sha256"] != subject_sha256:
                    raise UseAdmissionError("execution_use_scope_immutable")
            else:
                self.db.connection.execute(
                    "INSERT INTO execution_use_scopes(subject_kind,subject_id,project_id,fingerprint,payload,subject_sha256) VALUES (?,?,?,?,?,?)",
                    (kind, str(subject_id), str(scope.project_id), scope.fingerprint, scope.model_dump_json(), subject_sha256))
            if kind == "job":
                read_job_scope(self.db, job)  # Reject dropped owning-Run scope atomically.
            if content_hash is not None:
                hashes = self.db.connection.execute(
                    "SELECT content_hash FROM execution_use_content_origins WHERE subject_kind=? AND subject_id=?", (kind, str(subject_id))).fetchall()
                if hashes and any(row[0] != content_hash for row in hashes):
                    raise UseAdmissionError("execution_use_content_hash_immutable")
                self.db.connection.execute(
                    "INSERT OR IGNORE INTO execution_use_content_origins(content_hash,subject_kind,subject_id) VALUES (?,?,?)",
                    (content_hash, kind, str(subject_id)))

    def scopes_for_hash(self, content_hash: str) -> tuple[ExecutionUseScope, ...]:
        rows = self.db.connection.execute(
            "SELECT subject_kind,subject_id FROM execution_use_content_origins WHERE content_hash=? ORDER BY subject_kind,subject_id", (content_hash,))
        values = {}
        for row in rows:
            scope = self.get(row["subject_kind"], UUID(row["subject_id"]))
            if scope is None:
                raise UseAdmissionError("execution_use_origin_missing")
            values[scope.fingerprint] = scope
        return tuple(values.values())

    def require_content(self, content_hash: str, *, project_id: UUID, purpose: ExecutionPurpose = "commercial_production",
                        operation: str = "derivation") -> tuple[ExecutionUseScope, ...]:
        scopes = self.scopes_for_hash(content_hash)
        for scope in scopes:
            self.require_current(scope, project_id=project_id, purpose=purpose, operation=operation)
        return scopes

    def inherit(self, kind: SubjectKind, subject_id: UUID, *, project_id: UUID, purpose: ExecutionPurpose,
                parents: tuple[tuple[SubjectKind, UUID], ...], operation: str, content_hash: str | None = None,
                subject_sha256: str | None = None) -> ExecutionUseScope:
        """Merge ALL known ancestor grants, never select the least restricted one."""
        with (nullcontext() if self.db.connection.in_transaction else self.db.transaction(immediate=True)):
            scopes = []
            for parent_kind, parent_id in parents:
                scope = self.get(parent_kind, parent_id)
                if scope is None:
                    raise UseAdmissionError("execution_use_lineage_unknown")
                scopes.append(scope)
            if content_hash is not None:
                scopes.extend(self.scopes_for_hash(content_hash))
            if not scopes:
                raise UseAdmissionError("execution_use_lineage_unknown")
            bindings = {}
            for scope in scopes:
                self.require_current(scope, project_id=project_id, purpose=purpose, operation=operation)
                for binding in scope.bindings:
                    if binding.admission_id in bindings and bindings[binding.admission_id] != binding:
                        raise UseAdmissionError("provider_use_binding_changed")
                    bindings[binding.admission_id] = binding
            value = _make_scope(project_id, purpose,
                tuple(bindings[key] for key in sorted(bindings, key=str)))
            self.pin(kind, subject_id, value, operation=operation, content_hash=content_hash, subject_sha256=subject_sha256)
            return value

    def require_job_snapshot(self, job: Job) -> ExecutionUseScope | None:
        return read_job_scope(self.db, job)

    def require_worker_identity(self, job: Job, provider, *, capability_profile_id: UUID,
                                execution_admission_id: UUID | None = None) -> None:
        """Check an adapter's observed identity; this does NOT authorize inference.

        Real adapters lacking an actual artifact/configuration observation stay
        unsupported. Expected request/report values are not a runtime witness.
        This legacy observer may read evidence/runtime files. New prepared
        integration freezes it outside write locks through frozen_worker;
        transaction guards compare only the frozen identity/current DB facts.
        """
        scope = self.require_job_snapshot(job)
        if scope is None or scope.purpose != "internal_evaluation":
            raise UseAdmissionError("execution_use_scope_required")
        self.require_current(scope, project_id=job.project_id, purpose=scope.purpose, operation="generation")
        if scope.policy_version != 2:
            raise UseAdmissionError("execution_attestation_upgrade_required")
        if execution_admission_id is None:
            raise UseAdmissionError("execution_admission_binding_required")
        binding = next((value for value in scope.bindings if value.admission_id == execution_admission_id), None)
        if binding is None or binding.capability_profile_id != capability_profile_id:
            raise UseAdmissionError("execution_admission_binding_mismatch")
        if not isinstance(binding, ExecutionUseBindingV2):
            raise UseAdmissionError("execution_attestation_upgrade_required")
        observe = getattr(provider, "execution_use_snapshot", None)
        if not callable(observe):
            raise UseAdmissionError("execution_runtime_identity_unavailable")
        try:
            observed = observe()
            raw = observed.model_dump_json() if isinstance(observed, WorkerExecutionUseSnapshotV2) else json.dumps(observed, allow_nan=False)
            snapshot = WorkerExecutionUseSnapshotV2.model_validate_json(raw)
        except Exception:
            raise UseAdmissionError("execution_runtime_identity_unavailable") from None
        if (snapshot.identity != binding.identity
            or snapshot.execution_sha256 != binding.execution_sha256
            or getattr(provider, "provider_name", None) != snapshot.identity.provider
            or getattr(provider, "model", None) != snapshot.identity.model):
            raise UseAdmissionError("execution_runtime_identity_changed")

    def manifest(self, kind: SubjectKind, subject_id: UUID, *, content_hash: str | None = None) -> dict:
        """Historical restrictions remain visible even after revocation; no grant."""
        scopes = {scope.fingerprint: scope for scope in self.scopes_for_hash(content_hash)} if content_hash else {}
        subject = self.get(kind, subject_id)
        if subject is not None:
            scopes[subject.fingerprint] = subject
        return {"policy_version": 1, "lineage": "known" if scopes else "unknown",
            "commercial_authorized": False, "execution_authorized": False,
            "scopes": [scope.model_dump(mode="json") for scope in scopes.values()]}


def read_subject_scope(db: Database, kind: SubjectKind, subject_id: UUID) -> ExecutionUseScope | None:
    row = db.connection.execute(
        "SELECT * FROM execution_use_scopes WHERE subject_kind=? AND subject_id=?", (kind, str(subject_id))).fetchone()
    if row is None:
        return None  # Unknown/legacy lineage, NOT a commercial license grant.
    try:
        scope = _parse_scope(row["payload"])
    except ValueError:
        raise UseAdmissionError("execution_use_scope_invalid") from None
    if scope.fingerprint != row["fingerprint"] or str(scope.project_id) != row["project_id"]:
        raise UseAdmissionError("execution_use_scope_changed")
    return scope


def _job_owner_scopes(db: Database, job: Job) -> tuple[ExecutionUseScope, ...]:
    """Current durable ancestors, shared with the file-free reservation seam."""
    # Persisted ownership survives removal of an optional payload/origin
    # marker. A missing Job sidecar on a scoped Run is not a legacy Job.
    owner_scopes = []
    owners = db.connection.execute(
        "SELECT id FROM production_runs WHERE voice_job_id=? OR voice_qa_job_id=? OR render_job_id=?",
        (str(job.id),) * 3).fetchall()
    owners += db.connection.execute(
        """SELECT DISTINCT pr.id FROM production_runs pr, json_each(pr.talking_source_bindings) binding
           WHERE json_extract(binding.value,'$.talking_job_id')=?
              OR json_extract(binding.value,'$.talking_qa_job_id')=?
              OR json_extract(binding.value,'$.talking_preview_job_id')=?""", (str(job.id),) * 3).fetchall()
    for table, columns in (("voice_repairs", ("replacement_job_id", "qa_job_id")),
                           ("talking_repairs", ("replacement_job_id",)),
                           ("presentation_repairs", ("replacement_job_id",))):
        owners += db.connection.execute(f"SELECT run_id AS id FROM {table} WHERE " +
            " OR ".join(f"{column}=?" for column in columns), (str(job.id),) * len(columns)).fetchall()
    for owner in owners:
        owner_scope = read_subject_scope(db, "run", UUID(owner["id"]))
        if owner_scope is not None:
            owner_scopes.append(owner_scope)
    return tuple(owner_scopes)


def read_job_scope(db: Database, job: Job) -> ExecutionUseScope | None:
    scope = read_subject_scope(db, "job", job.id)
    owner_scopes = _job_owner_scopes(db, job)
    if scope is None:
        if owner_scopes:
            raise UseAdmissionError("execution_use_job_scope_missing")
        return None
    row = db.connection.execute(
        "SELECT subject_sha256 FROM execution_use_scopes WHERE subject_kind='job' AND subject_id=?", (str(job.id),)).fetchone()
    if scope.project_id != job.project_id or row[0] != job_use_identity(job):
        raise UseAdmissionError("execution_use_job_changed")
    for owner in owner_scopes:
        if (owner.project_id != scope.project_id or owner.purpose != scope.purpose
            or not set(b.admission_id for b in owner.bindings).issubset(b.admission_id for b in scope.bindings)):
            raise UseAdmissionError("execution_use_ancestor_dropped")
    return scope


def commercial_content_blocker(db: Database, content_hash: str) -> str | None:
    """Structural deny for known restrictions, including copied/reimported bytes.

    No report/CWD access or commercial permission inferred from absent lineage.
    Current evidence checks for evaluation consumption are a separate service.
    """
    rows = db.connection.execute(
        "SELECT subject_kind,subject_id FROM execution_use_content_origins WHERE content_hash=?", (content_hash,))
    try:
        for row in rows:
            scope = read_subject_scope(db, row["subject_kind"], UUID(row["subject_id"]))
            if scope is None:
                return "execution_use_origin_missing"
            if scope.purpose == "internal_evaluation":
                return "evaluation_scope_not_commercial"
    except (ValueError, KeyError):
        return "execution_use_scope_invalid"
    return None


def require_evaluation_lane_closed(db: Database, job: Job) -> None:
    """Fail closed BEFORE invoking handlers until the whole lane is integrated.

    This structural guard requires no evidence file reads or provider runtime.
    It also catches changed/stripped payloads via durable Job identity.
    """
    scope = read_job_scope(db, job)
    if scope is not None and scope.purpose == "internal_evaluation":
        root = getattr(db, "data_root", None)
        if root is not None:
            ExecutionScopeService(db, root).require_current(scope, project_id=job.project_id,
                purpose=scope.purpose, operation="generation")
        raise UseAdmissionError("evaluation_execution_not_integrated")
    require_commercial_job_inputs(db, job)


def job_input_media(db: Database, job: Job) -> tuple:
    try:
        return _job_input_media(db, job)
    except ValueError:
        # Revoked consent can make a persisted profile fail its domain model
        # validator. Do not expose that payload or skip the dependency gate.
        raise UseAdmissionError("execution_use_dependency_invalid") from None


def _job_input_media(db: Database, job: Job) -> tuple:
    """Inspect server-persisted dependencies, never caller-provided scope labels."""
    from app.db.repositories import (AssetRepository, AudioAssetRepository, ClipRepository,
        ImageAssetRepository, TalkingProfileRepository, VoiceProfileRepository)
    from app.domain.models import (RenderVideoJobPayload, TalkingGenerationJobPayload,
        TalkingQaJobPayload, TalkingRunPreviewJobPayload, VoiceGenerationJobPayload,
        VoiceQaJobPayload, VoicePaceCandidateJobPayload, VoiceBoundaryAlignmentJobPayload)
    assets, audios = AssetRepository(db), AudioAssetRepository(db)
    media = []

    def clip_media(clip_id):
        clip = ClipRepository(db).get(clip_id)
        if clip is not None and (asset := assets.get(clip.asset_id)) is not None:
            media.append(asset)

    def audio_media(audio_id):
        if audio_id is not None and (audio := audios.get(audio_id)) is not None:
            media.append(audio)

    payload = job.payload
    if isinstance(payload, VoiceGenerationJobPayload):
        profile = VoiceProfileRepository(db).get(payload.voice_profile_id)
        if profile is not None:
            for clip_id in profile.reference_clip_ids:
                clip_media(clip_id)
        if payload.reference_window is not None:
            clip_media(payload.reference_window.clip_id)
    elif isinstance(payload, TalkingGenerationJobPayload):
        clip_media(payload.reference_clip_id)
        audio_media(payload.narration_audio_id)
        profile = TalkingProfileRepository(db).get(payload.talking_profile_id)
        if profile is not None:
            for clip_id in profile.reference_clip_ids:
                clip_media(clip_id)
    elif isinstance(payload, (VoiceQaJobPayload, VoicePaceCandidateJobPayload, VoiceBoundaryAlignmentJobPayload)):
        audio_media(payload.narration_audio_id)
    elif isinstance(payload, TalkingQaJobPayload):
        audio_media(payload.master_audio_id)
        if (asset := assets.get(payload.output_asset_id)) is not None:
            media.append(asset)
    elif isinstance(payload, TalkingRunPreviewJobPayload):
        from app.db.repositories import TalkingSliceSeriesRepository
        series = TalkingSliceSeriesRepository(db).get(payload.series_id)
        if series is not None:
            audio_media(series.narration_audio_id)
            if series.planned_origin is not None:
                if (asset := assets.get(series.planned_origin.output_asset_id)) is not None:
                    media.append(asset)
    elif isinstance(payload, RenderVideoJobPayload):
        images = ImageAssetRepository(db)
        spec = payload.video_spec
        audio_media(None if spec.master_narration is None else spec.master_narration.audio_asset_id)
        for scene in spec.scenes:
            audio_media(scene.narration_asset_id)
            if scene.visual.asset_id is not None:
                asset = assets.get(scene.visual.asset_id) or images.get(scene.visual.asset_id)
                if asset is not None:
                    media.append(asset)
    for row in db.connection.execute("SELECT asset_id FROM asset_job_targets WHERE job_id=?", (str(job.id),)):
        if (asset := assets.get(UUID(row["asset_id"]))) is not None:
            media.append(asset)
    return tuple(media)


def require_commercial_job_inputs(db: Database, job: Job) -> None:
    for media in job_input_media(db, job):
        blocker = commercial_content_blocker(db, media.content_hash)
        if blocker is not None:
            raise UseAdmissionError(blocker)


def register_generated_import_scope(db: Database, kind: Literal["audio", "asset"], media, job: Job | None) -> None:
    """Application-only producer binding before an imported row becomes visible.

    Also runs on hash deduplication. Never trust a supplied Job snapshot over
    the durable producer; arbitrary user imports do not receive a producer.
    Caller owns the import transaction, including file placement.
    """
    if job is None:
        return
    from app.db.repositories import JobRepository
    from app.domain.models import JobType
    stored = JobRepository(db).get(job.id)
    if stored is None or job_use_identity(stored) != job_use_identity(job):
        raise UseAdmissionError("execution_use_job_changed")
    markers = {JobType.GENERATE_VOICE: ("audio", "voice_generation"),
               JobType.GENERATE_TALKING: ("asset", "talking_generation")}
    expected = markers.get(stored.type)
    if expected is None or expected[0] != kind:
        raise UseAdmissionError("execution_use_origin_kind_mismatch")
    metadata = dict(media.metadata)
    metadata[expected[1]] = {"job_id": str(stored.id), "project_id": str(stored.project_id)}
    inherit_generated_media_scope(db, kind, media.model_copy(update={"metadata": metadata}))


def inherit_generated_media_scope(db: Database, kind: Literal["audio", "asset"], media) -> None:
    """Record generated bytes atomically with their normal provenance write.

    Runs after server generation/preview provenance is set, not at arbitrary
    import. Legacy Jobs stay legacy; known hash restrictions survive reimport.
    Caller must include this in the same transaction as the media write.
    """
    from app.db.repositories import JobRepository
    from app.domain.models import JobType
    producers = []
    for marker in ("voice_generation", "talking_generation", "talking_run_preview"):
        provenance = media.metadata.get(marker)
        if not isinstance(provenance, dict):
            continue
        try:
            job_id = UUID(provenance["job_id"])
        except (KeyError, TypeError, ValueError):
            continue  # Preserve legacy evidence formats; do not grant rights.
        job = JobRepository(db).get(job_id)
        if job is not None and (scope := read_job_scope(db, job)) is not None:
            expected = {"voice_generation": ("audio", JobType.GENERATE_VOICE),
                "talking_generation": ("asset", JobType.GENERATE_TALKING),
                "talking_run_preview": ("asset", JobType.PREPARE_TALKING_RUN_PREVIEW)}[marker]
            if kind != expected[0] or job.type != expected[1]:
                raise UseAdmissionError("execution_use_origin_kind_mismatch")
            if provenance.get("project_id") not in (None, str(job.project_id)):
                raise UseAdmissionError("execution_use_origin_project_mismatch")
            producers.append((job, scope))
    if not producers:
        return
    root = getattr(db, "data_root", None)
    if root is None:
        raise UseAdmissionError("execution_scope_data_root_required")
    service = ExecutionScopeService(db, root)
    scopes = list(service.scopes_for_hash(media.content_hash))
    scopes.extend(scope for _, scope in producers)
    for job, _ in producers:
        for source in job_input_media(db, job):
            source_scopes = service.scopes_for_hash(source.content_hash)
            for dependency in source_scopes:
                service.require_current(dependency, project_id=producers[0][1].project_id,
                    purpose=producers[0][1].purpose, operation="derivation")
            scopes.extend(source_scopes)
    project_id, purpose = producers[0][1].project_id, producers[0][1].purpose
    bindings = {}
    for scope in scopes:
        service.require_current(scope, project_id=project_id, purpose=purpose, operation="generation")
        for binding in scope.bindings:
            if binding.admission_id in bindings and bindings[binding.admission_id] != binding:
                raise UseAdmissionError("provider_use_binding_changed")
            bindings[binding.admission_id] = binding
    merged = _make_scope(project_id, purpose,
        tuple(bindings[key] for key in sorted(bindings, key=str)))
    service.pin(kind, media.id, merged, operation="generation", content_hash=media.content_hash)

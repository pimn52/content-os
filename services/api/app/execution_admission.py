"""Inert purpose admission: no Jobs, provider calls or capability promotion."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
from typing import Literal
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from app.db import Database, ProjectRepository, ProviderMachineCapabilityProfileRepository, ProviderMachineSettingRepository
from app.domain.models import (ContractModel, ExecutionPurpose, ExecutionUseIdentity, ExecutionSpecificationV2, ExecutionSpecificationV3,
    ProviderMachineCapabilityProfile, ProviderUseAdmission, ProviderUseLicenseReview, ProviderUseLicenseReviewV2, ProviderUseOperation)
from app.runtime_inventory import RuntimeInventoryError, RuntimeInventoryStore, require_host_policy_inventory


class UseAdmissionError(ValueError):
    """Fixed public reason only; never expose report/exception/credential content."""


def configured_admission_actor(key: str | None) -> str | None:
    raw = os.environ.get("CONTENT_OS_EXECUTION_ADMISSION_KEY_HASHES")
    if not raw or not key or len(key) < 32:
        return None
    try:
        values = json.loads(raw)
    except ValueError:
        return None
    if (not isinstance(values, dict) or not values
        or any(not isinstance(a, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", a)
               or not isinstance(d, str) or not re.fullmatch(r"[0-9a-f]{64}", d)
               for a, d in values.items()) or len(set(values.values())) != len(values)):
        return None
    digest = hashlib.sha256(key.encode()).hexdigest()
    actors = [a for a, expected in values.items() if secrets.compare_digest(digest, expected)]
    return actors[0] if len(actors) == 1 else None


def configuration_digest(capability: ProviderMachineCapabilityProfile, setting=None) -> str:
    value = {"capability": capability.model_dump(mode="json", exclude={"updated_at", "last_verified_at"}),
             "setting": None if setting is None else setting.model_dump(mode="json", exclude={"updated_at"})}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _identity_matches(profile, identity) -> bool:
    return all(getattr(profile, k) == getattr(identity, k) for k in
               ("capability", "mode", "provider", "model", "runtime", "machine_id"))


class ProviderUseDecision(ContractModel):
    purpose: ExecutionPurpose = "commercial_production"
    state: Literal["allowed_for_scope", "blocked"]
    reasons: tuple[str, ...]
    admission_id: UUID | None = None
    evidence_identity: str | None = None
    dispatch_authorized: Literal[False] = False
    integration_status: Literal["not_integrated"] = "not_integrated"


def resolve_provider_use(*, project_id: UUID, capability: ProviderMachineCapabilityProfile | None,
    configuration_sha256: str | None, identity: ExecutionUseIdentity | None,
    purpose: ExecutionPurpose = "commercial_production", operation: ProviderUseOperation = "generation",
    admission: ProviderUseAdmission | None = None, evidence_current: bool = False,
    dependency_purposes: tuple[ExecutionPurpose, ...] = ()) -> ProviderUseDecision:
    """Pure truth-table evaluator. Eligibility never authorizes dispatch."""
    reasons = []
    if purpose not in ("commercial_production", "internal_evaluation"):
        raise UseAdmissionError("execution_purpose_unsupported")
    if operation not in ("generation", "derivation", "review", "download"):
        reasons.append("provider_use_operation_unsupported")
    if any(p not in ("commercial_production", "internal_evaluation") for p in dependency_purposes):
        reasons.append("dependency_use_scope_unknown")
    if capability is None or capability.readiness != "verified" or capability.quality_status != "verified" or not capability.evidence_reference:
        reasons.append("exact_capability_not_verified")
    if capability and identity and not _identity_matches(capability, identity):
        reasons.append("execution_identity_mismatch")
    if purpose == "commercial_production":
        if "internal_evaluation" in dependency_purposes or admission is not None:
            reasons.append("evaluation_scope_not_commercial")
        if capability is None or capability.commercial_status != "commercial_safe" or not capability.license_evidence_reference:
            reasons.append("provider_license_scope_unverified")
    else:
        if capability and capability.mode != "local":
            reasons.append("evaluation_local_only")
        if admission is None:
            reasons.append("provider_use_admission_missing")
        else:
            report = admission.review
            if admission.project_id != project_id:
                reasons.append("provider_use_project_mismatch")
            if admission.revoked_at is not None:
                reasons.append("provider_use_admission_revoked")
            if not evidence_current:
                reasons.append("provider_use_evidence_not_current")
            if (capability is None or report.capability_profile_id != capability.id
                or report.configuration_sha256 != configuration_sha256
                or identity is None or report.identity != identity):
                reasons.append("provider_use_identity_changed")
            if operation not in report.permitted_operations:
                reasons.append("provider_use_operation_not_permitted")
    return ProviderUseDecision(purpose=purpose, state="blocked" if reasons else "allowed_for_scope",
        reasons=tuple(dict.fromkeys(reasons)), admission_id=None if admission is None else admission.id,
        evidence_identity=None if admission is None else admission.review_sha256)


class ProviderUseAdmissionService:
    def __init__(self, db: Database, data_root: str | Path):
        self.db = db
        self.root = Path(data_root).resolve()

    def get(self, project_id: UUID, admission_id: UUID) -> ProviderUseAdmission | None:
        row = self.db.connection.execute("SELECT payload FROM provider_use_admissions WHERE id=? AND project_id=?",
            (str(admission_id), str(project_id))).fetchone()
        return None if row is None else ProviderUseAdmission.model_validate_json(row["payload"])

    def list(self, project_id: UUID) -> list[ProviderUseAdmission]:
        return [ProviderUseAdmission.model_validate_json(row["payload"]) for row in self.db.connection.execute(
            "SELECT payload FROM provider_use_admissions WHERE project_id=? ORDER BY rowid", (str(project_id),))]

    def _file(self, reference: str, *, maximum: int | None = None) -> bytes:
        # Reject Windows drives/UNC even if tested on another platform.
        parts = reference.replace("\\", "/").split("/")
        if Path(reference).is_absolute() or PureWindowsPath(reference).root or PureWindowsPath(reference).drive or ":" in reference or ".." in parts:
            raise UseAdmissionError("provider_use_evidence_path_invalid")
        portable = Path(*parts)
        source = (self.root.parent / portable if parts[0].casefold() == self.root.name.casefold()
                  else self.root / portable).resolve()
        if not source.is_relative_to(self.root) or not source.is_file():
            raise UseAdmissionError("provider_use_evidence_unavailable")
        try:
            with source.open("rb") as stream:
                data = stream.read((maximum or 2_000_000) + 1)
        except OSError:
            raise UseAdmissionError("provider_use_evidence_unavailable") from None
        if len(data) > (maximum or 2_000_000):
            raise UseAdmissionError("provider_use_evidence_too_large")
        return data

    def _report(self, reference: str, digest: str) -> ProviderUseLicenseReview:
        raw = self._file(reference, maximum=100_000)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise UseAdmissionError("provider_use_review_hash_changed")
        try:
            report = TypeAdapter(ProviderUseLicenseReview | ProviderUseLicenseReviewV2).validate_json(raw)
        except ValidationError:
            raise UseAdmissionError("provider_use_review_invalid") from None
        if isinstance(report, ProviderUseLicenseReviewV2) and isinstance(
                report.execution_specification, (ExecutionSpecificationV2, ExecutionSpecificationV3)):
            try:
                inventory = RuntimeInventoryStore(self.root).read(report.execution_specification.runtime_inventory)
                require_host_policy_inventory(report.execution_specification.host_runtime, inventory)
            except RuntimeInventoryError as error:
                raise UseAdmissionError(str(error)) from None
        terms = self._file(report.license_terms_reference)
        if not terms or hashlib.sha256(terms).hexdigest() != report.license_terms_sha256:
            raise UseAdmissionError("provider_use_terms_hash_changed")
        return report

    def current_evidence(self, admission: ProviderUseAdmission) -> bool:
        try:
            return self._report(admission.review_reference, admission.review_sha256) == admission.review
        except UseAdmissionError:
            return False

    def _configuration(self, profile):
        setting = ProviderMachineSettingRepository(self.db).get_by_scope_key(profile.scope_key)
        return configuration_digest(profile, setting)

    def adopt(self, project_id: UUID, *, idempotency_key: str, review_reference: str,
              review_sha256: str, operator_key: str | None) -> ProviderUseAdmission:
        actor = configured_admission_actor(operator_key)
        if actor is None:
            raise UseAdmissionError("execution_admission_operator_required")
        with self.db.transaction(immediate=True):
            if ProjectRepository(self.db).get(project_id) is None:
                raise UseAdmissionError("provider_use_project_missing")
            report = self._report(review_reference, review_sha256)
            if report.project_id != project_id:
                raise UseAdmissionError("provider_use_project_mismatch")
            profile = ProviderMachineCapabilityProfileRepository(self.db).get(report.capability_profile_id)
            if (profile is None or profile.mode != "local" or not _identity_matches(profile, report.identity)
                or report.configuration_sha256 != self._configuration(profile)):
                raise UseAdmissionError("provider_use_identity_changed")
            if (not profile.evidence_reference or profile.evidence_reference.casefold().startswith("fixture:")
                or profile.provenance_source.casefold().startswith("fixture")):
                raise UseAdmissionError("provider_use_capability_evidence_untrusted")
            row = self.db.connection.execute("SELECT payload FROM provider_use_admissions WHERE project_id=? AND idempotency_key=?",
                (str(project_id), idempotency_key)).fetchone()
            if row:
                old = ProviderUseAdmission.model_validate_json(row["payload"])
                if old.revoked_at is not None:
                    raise UseAdmissionError("provider_use_admission_revoked")
                if (old.actor_id != actor or old.review_reference != review_reference
                    or old.review_sha256 != review_sha256 or old.review != report):
                    raise UseAdmissionError("provider_use_idempotency_conflict")
                return old
            value = ProviderUseAdmission(project_id=project_id, idempotency_key=idempotency_key,
                review_reference=review_reference, review_sha256=review_sha256, review=report,
                actor_id=actor, created_at=datetime.now(timezone.utc))
            self.db.connection.execute("INSERT INTO provider_use_admissions(id,project_id,idempotency_key,payload) VALUES (?,?,?,?)",
                (str(value.id), str(project_id), idempotency_key, value.model_dump_json()))
            return value

    def revoke(self, project_id: UUID, admission_id: UUID, *, reason: str, operator_key: str | None) -> ProviderUseAdmission | None:
        actor = configured_admission_actor(operator_key)
        if actor is None:
            raise UseAdmissionError("execution_admission_operator_required")
        if not reason.strip():
            raise UseAdmissionError("provider_use_revocation_reason_required")
        with self.db.transaction(immediate=True):
            value = self.get(project_id, admission_id)
            if value is None or value.revoked_at is not None:
                return value
            value = value.model_copy(update={"revoked_at":datetime.now(timezone.utc), "revoked_by":actor, "revocation_reason":reason.strip()})
            self.db.connection.execute("UPDATE provider_use_admissions SET payload=? WHERE id=? AND project_id=?",
                (value.model_dump_json(), str(admission_id), str(project_id)))
            return value

    def resolve(self, project_id: UUID, *, capability_profile_id: UUID, identity: ExecutionUseIdentity | None,
                purpose: ExecutionPurpose = "commercial_production", operation: ProviderUseOperation = "generation",
                admission_id: UUID | None = None, dependency_purposes: tuple[ExecutionPurpose, ...] = ()) -> ProviderUseDecision:
        profile = ProviderMachineCapabilityProfileRepository(self.db).get(capability_profile_id)
        value = None if admission_id is None else self.get(project_id, admission_id)
        decision = resolve_provider_use(project_id=project_id, capability=profile,
            configuration_sha256=None if profile is None else self._configuration(profile), identity=identity,
            purpose=purpose, operation=operation, admission=value, evidence_current=value is not None and self.current_evidence(value),
            dependency_purposes=dependency_purposes)
        if admission_id is not None and value is None:
            return decision.model_copy(update={"state":"blocked", "reasons":tuple(dict.fromkeys((*decision.reasons, "provider_use_admission_missing")))})
        return decision

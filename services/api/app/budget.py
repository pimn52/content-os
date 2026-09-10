"""Deterministic local budget reservation and provider-call accounting."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal, Sequence
from uuid import UUID

from app.domain.models import BudgetPolicy, ProviderCallRecord, UsageCost


ProviderOperation = Literal["scene_planning", "asr", "vision", "embedding", "tts", "talking", "render"]
ProviderMode = Literal["assisted_test", "runtime"]
ProviderTerminalStatus = Literal["completed", "failed", "cancelled"]


class BudgetLimitError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProviderCallError(ValueError):
    """Credential-free errors from the provider-call ledger."""


class ProviderCallNotFoundError(ProviderCallError):
    pass


class ProviderCallTerminalError(ProviderCallError):
    pass


class ProviderCallIdentityConflictError(ProviderCallError):
    """One retry key was reused for a materially different provider input."""


@dataclass(frozen=True)
class BudgetSnapshot:
    currency: str
    known_amount: Decimal
    unknown_cost_calls: int
    calls: int
    reserved_calls: int


@dataclass(frozen=True)
class ProviderExecutionReservation:
    """One durable execution lease, or the earlier durable record to replay."""

    record: ProviderCallRecord
    owner: bool


def snapshot(records: Sequence[ProviderCallRecord], currency: str) -> BudgetSnapshot:
    known_amount = Decimal("0")
    unknown_cost_calls = 0
    calls = 0
    reserved_calls = 0
    for record in records:
        if record.status == "cancelled":
            # A reservation cancelled before execution releases its estimate.
            # If a provider supplied an actual charge, retain that charge.
            if record.actual_cost is None:
                continue
        calls += 1
        if record.status in {"reserved", "running"}:
            reserved_calls += 1
        cost = record.actual_cost or record.estimated_cost
        if cost.amount is None:
            unknown_cost_calls += 1
        elif cost.currency != currency:
            unknown_cost_calls += 1
        else:
            known_amount += cost.amount
    return BudgetSnapshot(currency, known_amount, unknown_cost_calls, calls, reserved_calls)


def enforce_reservation(
    policy: BudgetPolicy,
    records: Sequence[ProviderCallRecord],
    estimated_cost: UsageCost,
    *,
    ignore_existing_unknown_cost: bool = False,
) -> BudgetSnapshot:
    current = snapshot(records, policy.currency)
    if estimated_cost.amount is None and not policy.allow_unknown_cost:
        raise BudgetLimitError("unknown_cost", "provider price is unknown and the budget policy does not allow unknown cost")
    if current.unknown_cost_calls and not policy.allow_unknown_cost and not ignore_existing_unknown_cost:
        raise BudgetLimitError("existing_unknown_cost", "existing provider calls have unknown cost; reconcile them before continuing")
    if estimated_cost.amount is not None and estimated_cost.currency != policy.currency:
        raise BudgetLimitError("currency_mismatch", f"estimated cost currency must be {policy.currency}")
    if policy.max_calls is not None and current.calls + 1 > policy.max_calls:
        raise BudgetLimitError("call_limit", "provider call limit would be exceeded")
    if policy.max_amount is not None and estimated_cost.amount is not None and current.known_amount + estimated_cost.amount > policy.max_amount:
        raise BudgetLimitError("amount_limit", "provider amount limit would be exceeded")
    return current


def is_over_budget(policy: BudgetPolicy, records: Sequence[ProviderCallRecord]) -> bool:
    current = snapshot(records, policy.currency)
    if current.unknown_cost_calls and not policy.allow_unknown_cost:
        return True
    if policy.max_calls is not None and current.calls > policy.max_calls:
        return True
    return policy.max_amount is not None and current.known_amount > policy.max_amount


def provider_call_input_digest(value: object) -> str:
    """Hash canonical, credential-free input identity for a provider operation."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ProviderCallLedger:
    """Persist the reserve/execute/reconcile boundary on one local database.

    The global policy is a workspace-wide ceiling. A project policy is an
    additional, narrower ceiling; it never disables the global ceiling. Both
    scopes are checked under one SQLite write reservation. Policies are
    lifetime-of-local-ledger limits, not calendar-month claims.
    """

    def __init__(self, db: object) -> None:
        self.db = db

    def reserve(
        self,
        *,
        project_id: UUID,
        idempotency_key: str,
        operation: ProviderOperation,
        mode: ProviderMode,
        provider: str,
        model: str,
        input_source: str,
        estimated_cost: UsageCost,
        input_digest: str | None = None,
        allow_existing_unknown_cost: bool = False,
    ) -> ProviderCallRecord:
        """Create a manual reservation, or return the exact prior reservation.

        This method intentionally does not claim external execution. Runtime
        code must use :meth:`reserve_execution`, which writes ``running``
        before it touches a provider and therefore survives process crashes.
        """
        candidate = self._candidate(
            project_id=project_id,
            idempotency_key=idempotency_key,
            operation=operation,
            mode=mode,
            provider=provider,
            model=model,
            input_source=input_source,
            estimated_cost=estimated_cost,
            input_digest=input_digest,
            status="reserved",
        )
        from app.db import ProviderCallRepository

        repository = ProviderCallRepository(self.db)  # type: ignore[arg-type]
        with self.db.transaction(immediate=True):  # type: ignore[attr-defined]
            existing = repository.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                self._assert_same_identity(existing, candidate)
                return existing
            self._enforce_scopes(repository, project_id, candidate.estimated_cost, allow_existing_unknown_cost)
            repository.create(candidate)
        return candidate

    def reserve_execution(
        self,
        *,
        project_id: UUID,
        idempotency_key: str,
        operation: ProviderOperation,
        mode: ProviderMode,
        provider: str,
        model: str,
        input_source: str,
        input_digest: str,
        estimated_cost: UsageCost,
        allow_existing_unknown_cost: bool = False,
    ) -> ProviderExecutionReservation:
        """Durably claim the sole execution owner for one provider request.

        A completed record is replayable by the application service. A
        ``running`` record has unknown external side effects after a crash, so
        a retry receives that existing state instead of blindly sending a
        second billable request.
        """
        candidate = self._candidate(
            project_id=project_id,
            idempotency_key=idempotency_key,
            operation=operation,
            mode=mode,
            provider=provider,
            model=model,
            input_source=input_source,
            estimated_cost=estimated_cost,
            input_digest=input_digest,
            status="running",
        )
        from app.db import ProviderCallRepository

        repository = ProviderCallRepository(self.db)  # type: ignore[arg-type]
        with self.db.transaction(immediate=True):  # type: ignore[attr-defined]
            existing = repository.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                self._assert_same_identity(existing, candidate)
                return ProviderExecutionReservation(existing, owner=False)

            # A client may accidentally submit two distinct retry keys for the
            # same request. The digest covers provider/model, project, and
            # input; an active matching call still owns the only safe attempt.
            active = next(
                (
                    record
                    for record in repository.list_for_project(project_id)
                    if record.status in {"reserved", "running"}
                    and _execution_identity(record) == _execution_identity(candidate)
                ),
                None,
            )
            if active is not None:
                return ProviderExecutionReservation(active, owner=False)

            self._enforce_scopes(repository, project_id, candidate.estimated_cost, allow_existing_unknown_cost)
            repository.create(candidate)
        return ProviderExecutionReservation(candidate, owner=True)

    def finish(
        self,
        *,
        project_id: UUID,
        call_id: UUID,
        status: ProviderTerminalStatus,
        actual_cost: UsageCost | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_tokens: int | None = None,
        usage_observable: bool | None = None,
        price_date: date | None = None,
        error_code: str | None = None,
        result_payload: dict[str, object] | None = None,
    ) -> ProviderCallRecord:
        from app.db import ProviderCallRepository

        if actual_cost is not None and not isinstance(actual_cost, UsageCost):
            actual_cost = UsageCost.model_validate(actual_cost)
        repository = ProviderCallRepository(self.db)  # type: ignore[arg-type]
        with self.db.transaction(immediate=True):  # type: ignore[attr-defined]
            existing = repository.get(call_id)
            if existing is None or existing.project_id != project_id:
                raise ProviderCallNotFoundError("provider call not found")
            if existing.status in {"completed", "failed", "cancelled"}:
                if (
                    status != existing.status
                    or actual_cost != existing.actual_cost
                    or result_payload is not None and result_payload != existing.result_payload
                ):
                    raise ProviderCallTerminalError("provider call is already terminal")
                return existing
            value = ProviderCallRecord.model_validate({
                **existing.model_dump(mode="python"),
                "status": status,
                "actual_cost": actual_cost,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_tokens": cache_tokens,
                "usage_observable": usage_observable,
                "price_date": price_date,
                "error_code": error_code,
                "result_payload": result_payload,
                "completed_at": datetime.now(timezone.utc),
            })
            repository.update(value)
        return value

    def _candidate(
        self,
        *,
        project_id: UUID,
        idempotency_key: str,
        operation: ProviderOperation,
        mode: ProviderMode,
        provider: str,
        model: str,
        input_source: str,
        estimated_cost: UsageCost,
        input_digest: str | None,
        status: Literal["reserved", "running"],
    ) -> ProviderCallRecord:
        if not isinstance(estimated_cost, UsageCost):
            estimated_cost = UsageCost.model_validate(estimated_cost)
        digest = input_digest or provider_call_input_digest({
            "project_id": str(project_id),
            "operation": operation,
            "mode": mode,
            "provider": provider,
            "model": model,
            "input_source": input_source,
        })
        return ProviderCallRecord(
            project_id=project_id,
            created_at=datetime.now(timezone.utc),
            idempotency_key=idempotency_key,
            operation=operation,
            mode=mode,
            provider=provider,
            model=model,
            input_source=input_source,
            input_digest=digest,
            status=status,
            estimated_cost=estimated_cost,
        )

    def _assert_same_identity(self, existing: ProviderCallRecord, candidate: ProviderCallRecord) -> None:
        if _execution_identity(existing) != _execution_identity(candidate):
            raise ProviderCallIdentityConflictError("provider call idempotency key already belongs to another request")

    def _enforce_scopes(
        self,
        repository: object,
        project_id: UUID,
        estimated_cost: UsageCost,
        allow_existing_unknown_cost: bool,
    ) -> None:
        from app.db import BudgetPolicyRepository

        policies = BudgetPolicyRepository(self.db)  # type: ignore[arg-type]
        calls = repository
        global_policy = policies.get_by_project(None)
        project_policy = policies.get_by_project(project_id)
        if global_policy is not None:
            enforce_reservation(
                global_policy,
                calls.list_all(),
                estimated_cost,
                ignore_existing_unknown_cost=allow_existing_unknown_cost,
            )
        if project_policy is not None:
            enforce_reservation(
                project_policy,
                calls.list_for_project(project_id),
                estimated_cost,
                ignore_existing_unknown_cost=allow_existing_unknown_cost,
            )
        if global_policy is None and project_policy is None:
            # Default safety policy: a provider with unknown price must be
            # explicitly approved, while known-cost providers remain usable.
            enforce_reservation(
                BudgetPolicy(updated_at=datetime.now(timezone.utc)),
                calls.list_for_project(project_id),
                estimated_cost,
                ignore_existing_unknown_cost=allow_existing_unknown_cost,
            )


def _execution_identity(value: ProviderCallRecord) -> tuple[object, ...]:
    return (
        value.project_id,
        value.operation,
        value.mode,
        value.provider,
        value.model,
        value.input_source,
        value.input_digest or provider_call_input_digest({
            "project_id": str(value.project_id),
            "operation": value.operation,
            "mode": value.mode,
            "provider": value.provider,
            "model": value.model,
            "input_source": value.input_source,
        }),
    )

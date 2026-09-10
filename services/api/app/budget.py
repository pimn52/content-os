"""Deterministic local budget reservation and provider-call accounting."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal, Sequence
from uuid import UUID

from app.domain.models import BudgetPolicy, ProviderCallRecord, UsageCost


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


@dataclass(frozen=True)
class BudgetSnapshot:
    currency: str
    known_amount: Decimal
    unknown_cost_calls: int
    calls: int
    reserved_calls: int


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


class ProviderCallLedger:
    """Persist the reserve/execute/reconcile boundary on one DB connection.

    Provider adapters stay unaware of budgets.  Callers reserve immediately
    before invoking an adapter, then finish the record in both success and
    failure paths.  Unknown provider pricing is deliberately passed through
    to :func:`enforce_reservation`; only an explicit policy may allow it.
    """

    def __init__(self, db: object) -> None:
        self.db = db

    def reserve(
        self,
        *,
        project_id: UUID,
        idempotency_key: str,
        operation: Literal["scene_planning", "asr", "vision", "embedding", "tts", "talking", "render"],
        mode: Literal["assisted_test", "runtime"],
        provider: str,
        model: str,
    input_source: str,
    estimated_cost: UsageCost,
    allow_existing_unknown_cost: bool = False,
) -> ProviderCallRecord:
        from app.db import ProviderCallRepository

        if not isinstance(estimated_cost, UsageCost):
            estimated_cost = UsageCost.model_validate(estimated_cost)
        repository = ProviderCallRepository(self.db)  # type: ignore[arg-type]
        existing = repository.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            candidate = ProviderCallRecord.model_validate({
                "project_id": project_id,
                "idempotency_key": idempotency_key,
                "operation": operation,
                "mode": mode,
                "provider": provider,
                "model": model,
                "input_source": input_source,
                "estimated_cost": estimated_cost,
                "id": existing.id,
                "created_at": existing.created_at,
            })
            if _provider_call_identity(candidate) != _provider_call_identity(existing):
                raise ProviderCallError("provider call idempotency key already belongs to another request")
            return existing

        from app.db import BudgetPolicyRepository

        policies = BudgetPolicyRepository(self.db)  # type: ignore[arg-type]
        project_policy = policies.get_by_project(project_id)
        policy = project_policy or policies.get_by_project(None)
        effective = policy or BudgetPolicy(updated_at=datetime.now(timezone.utc))
        records = repository.list_for_project(project_id)
        enforce_reservation(
            effective,
            records,
            estimated_cost,
            ignore_existing_unknown_cost=allow_existing_unknown_cost,
        )
        value = ProviderCallRecord(
            project_id=project_id,
            created_at=datetime.now(timezone.utc),
            idempotency_key=idempotency_key,
            operation=operation,
            mode=mode,
            provider=provider,
            model=model,
            input_source=input_source,
            estimated_cost=estimated_cost,
        )
        try:
            with self.db.transaction():  # type: ignore[attr-defined]
                repository.create(value)
        except Exception as exc:
            # Keep the low-level UNIQUE error out of persisted/UI messages.
            if repository.get_by_idempotency_key(idempotency_key) is not None:
                raise ProviderCallError("provider call reservation already exists") from exc
            raise
        return value

    def finish(
        self,
        *,
        project_id: UUID,
        call_id: UUID,
        status: Literal["completed", "failed", "cancelled"],
        actual_cost: UsageCost | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_tokens: int | None = None,
        usage_observable: bool | None = None,
        price_date: date | None = None,
        error_code: str | None = None,
    ) -> ProviderCallRecord:
        from app.db import ProviderCallRepository

        if actual_cost is not None and not isinstance(actual_cost, UsageCost):
            actual_cost = UsageCost.model_validate(actual_cost)
        repository = ProviderCallRepository(self.db)  # type: ignore[arg-type]
        existing = repository.get(call_id)
        if existing is None or existing.project_id != project_id:
            raise ProviderCallNotFoundError("provider call not found")
        if existing.status in {"completed", "failed", "cancelled"}:
            if status != existing.status or actual_cost != existing.actual_cost:
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
            "completed_at": datetime.now(timezone.utc),
        })
        with self.db.transaction():  # type: ignore[attr-defined]
            repository.update(value)
        return value


def _provider_call_identity(value: ProviderCallRecord) -> tuple[object, ...]:
    return (
        value.project_id,
        value.idempotency_key,
        value.operation,
        value.mode,
        value.provider,
        value.model,
        value.input_source,
        value.estimated_cost,
    )

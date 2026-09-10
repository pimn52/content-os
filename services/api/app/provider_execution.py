"""One application boundary for billable/runtime provider execution.

Provider adapters remain credential-owning transport code. This module owns
the durable request identity, budget reservation, replay, and reconciliation
that surround every externally metered operation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar
from uuid import UUID

from app.budget import (
    BudgetLimitError,
    ProviderCallError,
    ProviderCallIdentityConflictError,
    ProviderCallLedger,
    ProviderOperation,
    provider_call_input_digest,
)
from app.costs import ProviderCostEstimator
from app.domain.models import CostCategory, ProviderCallRecord


ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class RuntimeProviderIdentity:
    """Stable public identity exposed by a runtime provider adapter."""

    provider: str
    model: str


class ProviderExecutionError(RuntimeError):
    code = "provider_execution_failed"


class ProviderExecutionIdempotencyConflict(ProviderExecutionError):
    code = "provider_idempotency_conflict"


class ProviderExecutionInProgress(ProviderExecutionError):
    code = "provider_execution_in_progress"


class ProviderExecutionRecordedFailure(ProviderExecutionError):
    code = "provider_execution_previously_failed"


class ProviderExecutionReplayUnavailable(ProviderExecutionError):
    code = "provider_execution_replay_unavailable"


class ProviderExecutionAccountingFailure(ProviderExecutionError):
    code = "provider_execution_accounting_failed"


def runtime_provider_identity(provider: object) -> RuntimeProviderIdentity | None:
    """Read adapter metadata structurally, without vendor-specific type checks.

    Test doubles and local deterministic services normally expose no identity;
    they do not claim to be a paid/runtime provider and therefore do not enter
    the external-call ledger. Every bundled network adapter exposes these two
    public fields.
    """
    provider_name = getattr(provider, "provider_name", None)
    model = getattr(provider, "model", None)
    if not isinstance(provider_name, str) or not provider_name.strip():
        return None
    if not isinstance(model, str) or not model.strip():
        return None
    return RuntimeProviderIdentity(provider_name.strip(), model.strip())


class ProviderExecutionService:
    """Run exactly one durable provider operation or replay its saved result."""

    def __init__(self, db: object, cost_estimator: ProviderCostEstimator) -> None:
        self._ledger = ProviderCallLedger(db)
        self._cost_estimator = cost_estimator

    def execute(
        self,
        *,
        project_id: UUID,
        idempotency_key: str,
        operation: ProviderOperation,
        provider: RuntimeProviderIdentity,
        input_source: str,
        category: CostCategory,
        input_document: object,
        action: Callable[[], ResultT],
        encode_result: Callable[[ResultT], object],
        decode_result: Callable[[object], ResultT],
        error_code: Callable[[Exception], str | None],
    ) -> ResultT:
        """Execute once, then persist a replayable result beside the ledger row.

        ``input_document`` must exclude credentials. Its digest binds the retry
        key to the complete request (including IP/script context supplied by
        callers), while the raw document itself is never persisted here.
        """
        digest = provider_call_input_digest({
            "project_id": str(project_id),
            "operation": operation,
            "provider": provider.provider,
            "model": provider.model,
            "input_source": input_source,
            "input": input_document,
        })
        try:
            estimate = self._cost_estimator.estimate(
                operation=operation,
                provider=provider.provider,
                model=provider.model,
                input_source=input_source,
                category=category,
            )
            reservation = self._ledger.reserve_execution(
                project_id=project_id,
                idempotency_key=idempotency_key,
                operation=operation,
                mode="runtime",
                provider=provider.provider,
                model=provider.model,
                input_source=input_source,
                input_digest=digest,
                estimated_cost=estimate,
            )
        except BudgetLimitError:
            raise
        except ProviderCallIdentityConflictError as exc:
            raise ProviderExecutionIdempotencyConflict("idempotency key belongs to a different provider request") from exc
        except ProviderCallError as exc:
            raise ProviderExecutionAccountingFailure("provider call could not be reserved") from exc

        if not reservation.owner:
            return self._replay(reservation.record, decode_result)

        try:
            result = action()
        except Exception as exc:
            self._finish_failure(project_id, reservation.record, error_code(exc))
            raise

        try:
            encoded = encode_result(result)
            # Reject opaque/non-JSON values before marking a provider call
            # complete: replay must be as durable as the external operation.
            json.dumps(encoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            payload: dict[str, object] = {"result": encoded}
        except Exception:
            self._finish_failure(project_id, reservation.record, "provider_result_not_replayable")
            raise
        try:
            self._ledger.finish(
                project_id=project_id,
                call_id=reservation.record.id,
                status="completed",
                usage_observable=False,
                result_payload=payload,
            )
        except ProviderCallError as exc:
            raise ProviderExecutionAccountingFailure("provider call result could not be reconciled") from exc
        return result

    def _replay(self, record: ProviderCallRecord, decode_result: Callable[[object], ResultT]) -> ResultT:
        if record.status == "completed":
            payload = record.result_payload
            if not isinstance(payload, dict) or "result" not in payload:
                raise ProviderExecutionReplayUnavailable("completed provider call has no replayable result")
            try:
                return decode_result(payload["result"])
            except Exception as exc:
                raise ProviderExecutionReplayUnavailable("completed provider result cannot be decoded") from exc
        if record.status in {"reserved", "running"}:
            raise ProviderExecutionInProgress("provider execution is already running or needs recovery")
        raise ProviderExecutionRecordedFailure("provider execution already ended without a replayable result")

    def _finish_failure(self, project_id: UUID, record: ProviderCallRecord, error_code: str | None) -> None:
        try:
            self._ledger.finish(
                project_id=project_id,
                call_id=record.id,
                status="failed",
                usage_observable=False,
                error_code=error_code or "provider_execution_failed",
            )
        except ProviderCallError as exc:
            raise ProviderExecutionAccountingFailure("provider call failure could not be reconciled") from exc

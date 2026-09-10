from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


def _project(client: TestClient) -> str:
    response = client.post("/projects", json={"title": "Budget test", "topic": "Local budget boundary"})
    assert response.status_code == 201
    return response.json()["id"]


def _cost(amount: str | None) -> dict[str, object]:
    value: dict[str, object] = {"category": "llm"}
    if amount is not None:
        value.update(amount=amount, currency="USD")
    return value


def test_provider_call_reservation_is_idempotent_and_budgeted(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "budget.sqlite3")) as client:
        project_id = _project(client)
        policy = client.put(f"/projects/{project_id}/budget", json={"currency": "USD", "max_amount": "1.00", "max_calls": 2})
        assert policy.status_code == 200

        request = {
            "idempotency_key": "llm-call-1",
            "operation": "scene_planning",
            "mode": "runtime",
            "provider": "openai-compatible",
            "model": "planner-local",
            "input_source": "project:brief",
            "estimated_cost": _cost("0.40"),
        }
        first = client.post(f"/projects/{project_id}/provider-calls/reserve", json=request)
        repeated = client.post(f"/projects/{project_id}/provider-calls/reserve", json=request)
        assert first.status_code == repeated.status_code == 201
        assert first.json()["record"]["id"] == repeated.json()["record"]["id"]
        assert repeated.json()["budget"]["snapshot"]["known_amount"] == "0.40"

        too_expensive = client.post(
            f"/projects/{project_id}/provider-calls/reserve",
            json={**request, "idempotency_key": "llm-call-too-expensive", "estimated_cost": _cost("0.70")},
        )
        assert too_expensive.status_code == 409
        assert too_expensive.json()["detail"] == "amount_limit"

        unknown = client.post(
            f"/projects/{project_id}/provider-calls/reserve",
            json={**request, "idempotency_key": "llm-call-unknown", "estimated_cost": _cost(None)},
        )
        assert unknown.status_code == 409
        assert unknown.json()["detail"] == "unknown_cost"

        completed = client.post(
            f"/projects/{project_id}/provider-calls/{first.json()['record']['id']}/complete",
            json={"status": "completed", "actual_cost": _cost("0.35"), "input_tokens": 100, "output_tokens": 20, "usage_observable": True, "price_date": "2026-09-09"},
        )
        assert completed.status_code == 200
        assert completed.json()["record"]["actual_cost"]["amount"] == "0.35"
        assert completed.json()["budget"]["snapshot"]["known_amount"] == "0.35"
        repeated_completion = client.post(
            f"/projects/{project_id}/provider-calls/{first.json()['record']['id']}/complete",
            json={"status": "completed", "actual_cost": _cost("0.35"), "input_tokens": 100, "output_tokens": 20, "usage_observable": True, "price_date": "2026-09-09"},
        )
        assert repeated_completion.status_code == 200

        second = client.post(
            f"/projects/{project_id}/provider-calls/reserve",
            json={**request, "idempotency_key": "llm-call-2", "estimated_cost": _cost("0.50")},
        )
        assert second.status_code == 201
        third = client.post(
            f"/projects/{project_id}/provider-calls/reserve",
            json={**request, "idempotency_key": "llm-call-3", "estimated_cost": _cost("0.01")},
        )
        assert third.status_code == 409
        assert third.json()["detail"] == "call_limit"


def test_budget_policy_can_be_global_and_unknown_cost_can_be_explicit(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "global-budget.sqlite3")) as client:
        project_id = _project(client)
        saved = client.put("/budget", json={"currency": "USD", "max_calls": 1, "allow_unknown_cost": True})
        assert saved.status_code == 200
        effective = client.get(f"/projects/{project_id}/budget")
        assert effective.status_code == 200
        assert effective.json()["policy_source"] == "global"
        reserved = client.post(
            f"/projects/{project_id}/provider-calls/reserve",
            json={
                "idempotency_key": "unknown-is-visible",
                "operation": "asr",
                "mode": "assisted_test",
                "provider": "openai-compatible",
                "model": "transcription",
                "input_source": "asset:audio",
                "estimated_cost": _cost(None),
            },
        )
        assert reserved.status_code == 201
        body = reserved.json()
        assert body["budget"]["snapshot"]["unknown_cost_calls"] == 1
        assert body["budget"]["over_budget"] is False


def test_global_budget_is_enforced_across_projects_and_project_policy_is_additional(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "scoped-global-budget.sqlite3")) as client:
        first_project = _project(client)
        second_project = _project(client)
        assert client.put("/budget", json={"currency": "USD", "max_calls": 2, "allow_unknown_cost": True}).status_code == 200
        assert client.put(
            f"/projects/{first_project}/budget",
            json={"currency": "USD", "max_calls": 1, "allow_unknown_cost": True},
        ).status_code == 200

        request = {
            "operation": "scene_planning",
            "mode": "runtime",
            "provider": "test-runtime",
            "model": "planner-v1",
            "input_source": "project:brief",
            "estimated_cost": _cost(None),
        }
        one = client.post(
            f"/projects/{first_project}/provider-calls/reserve",
            json={**request, "idempotency_key": "project-one"},
        )
        project_limited = client.post(
            f"/projects/{first_project}/provider-calls/reserve",
            json={**request, "idempotency_key": "project-one-over"},
        )
        two = client.post(
            f"/projects/{second_project}/provider-calls/reserve",
            json={**request, "idempotency_key": "project-two"},
        )
        global_limited = client.post(
            f"/projects/{second_project}/provider-calls/reserve",
            json={**request, "idempotency_key": "project-two-over"},
        )

        assert one.status_code == two.status_code == 201
        assert project_limited.status_code == global_limited.status_code == 409
        assert project_limited.json()["detail"] == "call_limit"
        assert global_limited.json()["detail"] == "call_limit"
        budget = client.get(f"/projects/{first_project}/budget").json()
        assert budget["period"] == "ledger_lifetime"
        assert budget["global_snapshot"]["calls"] == 2
        assert budget["project_snapshot"]["calls"] == 1

from datetime import date, datetime, timezone
from decimal import Decimal
import json

import pytest

from app.budget import BudgetLimitError, snapshot
from app.db import BudgetPolicyRepository, Database, ProviderCallRepository
from app.domain.models import BudgetPolicy, CostCategory
from app.provider_execution import ProviderExecutionService, RuntimeProviderIdentity
from app.providers.vision import (OpenAICompatibleVisionProvider, VisionHTTPResponse, VisionInputError,
    VisionProviderResponseError)
from test_provider_execution import _project


USAGE = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
    "prompt_tokens_details": {"cached_tokens": 200}}


class Transport:
    def __init__(self, *, usage=None, invalid_output=False):
        self.usage = USAGE if usage is None else usage
        self.invalid_output = invalid_output
        self.requests = []

    def post(self, endpoint, headers, body, timeout_seconds):
        self.requests.append({"endpoint": endpoint, "body": json.loads(body)})
        output = {"clips": [{"clip_id": "one", "description": "夹具中的操作画面",
            "content_consistent": True, "comparison_layout": "none", "subtitle_regions": [],
            "protected_regions": [], "matches": [], "reason": "仅作本地计费夹具"}]}
        document = {"choices": [{"finish_reason": "stop", "message": {
            "content": "invalid json" if self.invalid_output else json.dumps(output, ensure_ascii=False)}}], "usage": self.usage}
        return VisionHTTPResponse(200, json.dumps(document).encode(), {})


def provider(transport=None, **options):
    return OpenAICompatibleVisionProvider("fixture-credential", base_url="https://api.moonshot.cn/v1",
        model="kimi-k2.6", protocol="chat_completions", video_input=True,
        transport=transport or Transport(), **options)


def estimate(active):
    return active.cost_estimator().estimate(operation="vision", provider=active.provider_name,
        model=active.model, input_source="fixture", category=CostCategory.LLM)


def videos(tmp_path):
    path = tmp_path / "interval.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypfixture")
    return [{"clip_id": "one", "path": str(path)}]


def execute(db, project, active, paths, *, key="vision", encode=None, callback=None):
    return ProviderExecutionService(db, active.cost_estimator()).execute(project_id=project.id,
        idempotency_key=key, operation="vision", provider=RuntimeProviderIdentity(active.provider_name, active.model),
        input_source="fixture-intervals", category=CostCategory.LLM, input_document={"clip": "one"},
        action=lambda: active.analyze_intervals(paths, []),
        encode_result=encode or (lambda result: result.model_dump(mode="json")), decode_result=lambda value: value,
        error_code=lambda error: "invalid_video_analysis", usage_callback=callback or active.usage_accounting)


def policy(db, project, maximum):
    BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, currency="CNY", max_amount=maximum,
        max_calls=5, updated_at=datetime.now(timezone.utc)))


def test_video_budget_reserves_independent_input_and_output_ceiling_and_enforces_output_cap(tmp_path):
    transport = Transport()
    active = provider(transport, max_output_tokens=512)
    cost = estimate(active)
    assert cost.amount == Decimal("1.7178") and cost.currency == "CNY"
    assert "262144" in cost.note and "512" in cost.note and "上界" in cost.note
    active.analyze_intervals(videos(tmp_path), [])
    assert transport.requests[0]["body"]["max_tokens"] == 512
    assert len(transport.requests) == 1  # 没有隐式token计数或额外模型请求。
    with pytest.raises(VisionInputError, match="reserved output"):
        active._request({"model": active.model, "messages": [], "max_tokens": 513})
    assert len(transport.requests) == 1


@pytest.mark.parametrize("endpoint,model,protocol", [
    ("https://third-party.example/v1", "kimi-k2.6", "chat_completions"),
    ("https://api.moonshot.cn/v1", "unverified-model", "chat_completions"),
    ("https://api.moonshot.ai/v1", "kimi-k2.6", "chat_completions"),
    ("https://api.moonshot.cn/v1", "kimi-k2.6", "responses"),
])
def test_prices_are_not_applied_to_other_hosts_currencies_models_or_protocols(endpoint, model, protocol):
    active = OpenAICompatibleVisionProvider("fixture", base_url=endpoint, model=model, protocol=protocol)
    assert estimate(active).amount is None
    active.last_usage = {"input_tokens": 1000, "output_tokens": 100, "cached_tokens": 0}
    receipt = active.usage_accounting()
    assert receipt.actual_cost is None and receipt.price_date is None


def test_response_usage_prices_cached_input_separately_and_does_not_claim_invoice_charge(tmp_path):
    active = provider()
    active.analyze_intervals(videos(tmp_path), [])
    receipt = active.usage_accounting()
    assert (receipt.input_tokens, receipt.output_tokens, receipt.cache_tokens) == (1000, 100, 200)
    assert receipt.actual_cost.amount == Decimal("0.0082") and "0.00812" in receipt.actual_cost.note
    assert receipt.price_date == date(2026, 10, 25) and receipt.usage_observable
    assert "非账单实扣" in receipt.actual_cost.note
    assert estimate(active).amount == Decimal("2.0358")


@pytest.mark.parametrize("usage", [
    {"prompt_tokens": 1000, "completion_tokens": 100},  # cache未知不能假设全部未命中或全命中。
    {"prompt_tokens": 1000, "completion_tokens": 100, "cached_tokens": 1200},
    {"prompt_tokens": 1000, "completion_tokens": 100, "cached_tokens": 0,
        "prompt_tokens_details": {"cached_tokens": 200}},
    {"prompt_tokens": 1000.5, "completion_tokens": 100, "cached_tokens": 0},
    {"prompt_tokens": True, "completion_tokens": 100, "cached_tokens": 0},
    {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 999, "cached_tokens": 0},
])
def test_missing_inconsistent_or_noninteger_usage_does_not_produce_known_cost(tmp_path, usage):
    active = provider(Transport(usage=usage))
    active.analyze_intervals(videos(tmp_path), [])
    assert active.usage_accounting().actual_cost is None


def test_amount_limit_rejects_before_network_and_observed_usage_replaces_reservation(tmp_path):
    with Database(tmp_path / "budget.sqlite") as db:
        project = _project(db)
        paths, transport = videos(tmp_path), Transport()
        active = provider(transport)
        policy(db, project, Decimal("2.03"))
        with pytest.raises(BudgetLimitError) as failure:
            execute(db, project, active, paths)
        assert failure.value.code == "amount_limit" and transport.requests == []
        assert ProviderCallRepository(db).list_for_project(project.id) == []
        policy(db, project, Decimal("2.0358"))
        execute(db, project, active, paths)
        saved = ProviderCallRepository(db).list_for_project(project.id)
        assert len(saved) == 1 and saved[0].status == "completed"
        assert saved[0].estimated_cost.amount == Decimal("2.0358")
        assert saved[0].actual_cost.amount == Decimal("0.0082")
        assert (saved[0].input_tokens, saved[0].output_tokens, saved[0].cache_tokens) == (1000, 100, 200)
        assert snapshot(saved, "CNY").known_amount == Decimal("0.0082")
        # 已完成请求不再次调用网络，也不读取可能残留上一请求用量的适配器。
        execute(db, project, active, paths, callback=lambda error: pytest.fail("重放不应读取usage"))
        assert len(transport.requests) == 1


def test_parsing_failure_retains_billable_response_usage_but_pre_request_failure_does_not_reuse_it(tmp_path):
    active = provider(Transport(invalid_output=True))
    with Database(tmp_path / "failure.sqlite") as db:
        project = _project(db)
        policy(db, project, Decimal("3"))
        with pytest.raises(VisionProviderResponseError):
            execute(db, project, active, videos(tmp_path))
        record = ProviderCallRepository(db).list_for_project(project.id)[0]
        assert record.status == "failed" and record.actual_cost.amount == Decimal("0.0082")
        assert record.input_tokens == 1000 and record.cache_tokens == 200 and record.usage_observable
        # 本次请求尚未取得响应的错误不继承刚才失败响应的用量。
        receipt = active.usage_accounting(VisionInputError("proxy changed before request"))
        assert receipt.actual_cost is None and receipt.input_tokens is None and not receipt.usage_observable


def test_usage_unknown_keeps_full_reservation_and_currency_mismatch_does_not_convert(tmp_path):
    with Database(tmp_path / "unknown.sqlite") as db:
        project = _project(db)
        active = provider(Transport(usage={"prompt_tokens": 1000, "completion_tokens": 100}))
        policy(db, project, Decimal("3"))
        execute(db, project, active, videos(tmp_path))
        saved = ProviderCallRepository(db).list_for_project(project.id)
        assert saved[0].actual_cost is None and saved[0].input_tokens == 1000
        assert snapshot(saved, "CNY").known_amount == Decimal("2.0358")
        BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, currency="USD",
            max_amount=Decimal("10"), updated_at=datetime.now(timezone.utc)))
        with pytest.raises(BudgetLimitError) as failure:
            execute(db, project, active, videos(tmp_path), key="different-currency")
        assert failure.value.code == "existing_unknown_cost" or failure.value.code == "currency_mismatch"
        assert len(active.transport.requests) == 1


def test_unserializable_result_still_accounts_the_completed_provider_request(tmp_path):
    with Database(tmp_path / "serialization.sqlite") as db:
        project = _project(db)
        policy(db, project, Decimal("3"))
        active = provider()
        with pytest.raises(TypeError):
            execute(db, project, active, videos(tmp_path), encode=lambda result: object())
        record = ProviderCallRepository(db).list_for_project(project.id)[0]
        assert record.status == "failed" and record.error_code == "provider_result_not_replayable"
        assert record.actual_cost.amount == Decimal("0.0082") and record.input_tokens == 1000

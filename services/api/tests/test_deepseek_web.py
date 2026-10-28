from datetime import datetime, timezone
from io import BytesIO
import json
from urllib.error import HTTPError

import pytest

from app.costs import CatalogProviderCostEstimator
from app.db import Database, BudgetPolicyRepository, IPProfileRepository, ProjectRepository, ProviderCallRepository
from app.domain.models import BudgetPolicy, CostCategory, IPProfile, Project, RationalFps
from app.provider_execution import ProviderExecutionService, ProviderExecutionRecordedFailure, RuntimeProviderIdentity
from app.providers.deepseek_web import DeepSeekWebProvider, DeepSeekWebError, MAX_RESPONSE_BYTES


class Response(BytesIO):
    status = 200


def test_search_and_fetch_use_same_key_without_chat_or_media_request():
    requests = []
    def opener(request, timeout):
        requests.append(request)
        value = {"results": [{"title": "公开镜头", "url": "https://example.org/shot"}]} if request.full_url.endswith("search") else {
            "title": "公开镜头", "content": "[下载](https://example.org/shot.mp4)"}
        return Response(json.dumps(value, ensure_ascii=False).encode())
    provider = DeepSeekWebProvider("test-key", opener=opener)
    assert len(provider.search("星空 实拍")["results"]) == 1
    assert "shot.mp4" in provider.fetch("https://example.org/shot")["content"]
    assert [r.full_url for r in requests] == ["https://api.deepseek.com/v1/tools/search", "https://api.deepseek.com/v1/tools/fetch"]
    assert all(r.get_header("Authorization") == "Bearer test-key" for r in requests)
    assert all("test-key" not in r.data.decode() for r in requests)


@pytest.mark.parametrize("url", ["http://localhost/x", "https://127.0.0.1/x", "https://[::1]/x",
    "https://user:secret@example.org", "file:///tmp/video", "https://example.org:1234/x"])
def test_fetch_rejects_non_public_or_credentialled_urls_before_request(url):
    provider = DeepSeekWebProvider("test-key", opener=lambda *a, **k: pytest.fail("不应请求"))
    with pytest.raises(ValueError):
        provider.fetch(url)


@pytest.mark.parametrize("query,count", [("", 8), ("x" * 129, 8), ("x", True), ("x", 51)])
def test_invalid_search_does_not_spend(query, count):
    provider = DeepSeekWebProvider("test-key", opener=lambda *a, **k: pytest.fail("不应请求"))
    with pytest.raises(ValueError):
        provider.search(query, count=count)


@pytest.mark.parametrize("body,method", [(b'{}', "search"), (b'[]', "search"),
    (b'{"results":[{}]}', "search"), (b'{"title":"x"}', "fetch"),
    (b'not-json', "fetch"), (b'x' * (MAX_RESPONSE_BYTES + 1), "fetch")],
    ids=["missing_results", "wrong_envelope", "missing_identity", "missing_content", "invalid_json", "oversized"])
def test_malformed_and_oversized_responses_are_not_promoted(body, method):
    provider = DeepSeekWebProvider("test-key", opener=lambda *a, **k: Response(body))
    with pytest.raises(DeepSeekWebError):
        getattr(provider, method)("https://example.org" if method == "fetch" else "星云")


@pytest.mark.parametrize("body,code", [
    ({"title": "验证码拦截", "content": "请点击下方按钮完成验证"}, "deepseek_web_access_challenge"),
    ({"title": "正常标题", "content": "  "}, "deepseek_web_empty_content"),
])
def test_http_success_does_not_admit_access_challenge_or_empty_page(body, code):
    provider = DeepSeekWebProvider("test-key", opener=lambda *a, **k: Response(json.dumps(body).encode()))
    with pytest.raises(DeepSeekWebError) as error:
        provider.fetch("https://example.org")
    assert error.value.code == code and error.value.response_document == body


@pytest.mark.parametrize("operation", ["web_search", "web_fetch"])
def test_discovery_uses_existing_budget_and_durable_failure_without_retry(tmp_path, operation):
    calls = []
    def opener(request, timeout):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 403, "denied", {}, BytesIO(b'{"error":"test-key"}'))
    provider = DeepSeekWebProvider("test-key", opener=opener)
    with Database(tmp_path / "web.sqlite3") as db:
        profile = IPProfile(creator_name="检索验证")
        IPProfileRepository(db).create(profile)
        project = Project(ip_profile_id=profile.id, title="外部素材", topic="星云", fps=RationalFps(numerator=30, denominator=1),
            created_at=datetime.now(timezone.utc))
        ProjectRepository(db).create(project)
        BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, currency="CNY", max_calls=1, updated_at=datetime.now(timezone.utc)))
        service = ProviderExecutionService(db, CatalogProviderCostEstimator({(operation, "deepseek", operation): provider.cost()}))
        arguments = dict(project_id=project.id, idempotency_key="web-once", operation=operation,
            provider=RuntimeProviderIdentity("deepseek", operation), input_source="external_media_discovery",
            category=CostCategory.STOCK, input_document={"topic": "星云"},
            action=lambda: provider.search("星云") if operation == "web_search" else provider.fetch("https://example.org"),
            encode_result=lambda x: x, decode_result=lambda x: x, error_code=lambda e: e.code)
        with pytest.raises(DeepSeekWebError) as caught:
            service.execute(**arguments)
        assert "test-key" not in str(caught.value.response_document)
        with pytest.raises(ProviderExecutionRecordedFailure):
            service.execute(**arguments)
        record = ProviderCallRepository(db).list_for_project(project.id)[0]
        assert record.operation == operation and record.status == "failed"
        assert record.actual_cost is None and record.error_code == "deepseek_web_http_403"
        assert len(calls) == 1

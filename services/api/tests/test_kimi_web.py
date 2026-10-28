from datetime import datetime, timezone
from io import BytesIO
import json
from urllib.error import HTTPError, URLError

import pytest

from app.costs import CatalogProviderCostEstimator
from app.db import BudgetPolicyRepository, Database, IPProfileRepository, ProjectRepository, ProviderCallRepository
from app.domain.models import BudgetPolicy, CostCategory, IPProfile, Project, RationalFps
from app.provider_execution import ProviderExecutionRecordedFailure, ProviderExecutionService, RuntimeProviderIdentity
from app.providers.kimi_web import KimiWebError, KimiWebProvider, MAX_RESPONSE_BYTES


class Response(BytesIO):
    status = 200


def test_independent_search_and_fetch_keep_raw_evidence_and_use_no_chat_model():
    requests = []
    def opener(request, timeout):
        requests.append((request, timeout))
        value = {"search_results": [{"title": "Pouring coffee", "url": "https://coverr.co/videos/coffee-shot", "text": "正文"}]} if request.full_url.endswith("search") else {
            "title": "Coffee", "url": "https://coverr.co/videos/coffee-shot", "markdown": "![image1](https://kimi-web-img.kimi.ai/preview.png)"}
        return Response(json.dumps(value, ensure_ascii=False).encode())
    provider = KimiWebProvider("test-key", opener=opener)
    assert provider.search(" coffee pouring ", count=6)["search_results"][0]["text"] == "正文"
    assert "kimi-web-img" in provider.fetch("https://coverr.co/videos/coffee-shot")["markdown"]
    assert [request.full_url for request, _ in requests] == [
        "https://api.moonshot.cn/v1/tools/search", "https://api.moonshot.cn/v1/tools/fetch"]
    assert json.loads(requests[0][0].data) == {"text_query": "coffee pouring", "limit": 6,
        "include_content": True, "timeout_seconds": 30}
    assert all(request.get_header("Authorization") == "Bearer test-key" for request, _ in requests)
    assert all(timeout == 30 for _, timeout in requests)
    assert all("test-key" not in request.data.decode() for request, _ in requests)


@pytest.mark.parametrize("url", ["http://localhost/x", "https://localhost./x", "https://127.0.0.1/x", "https://[::1]/x",
    "https://user:secret@example.org", "file:///tmp/video", "https://example.org:1234/x"])
def test_invalid_fetch_does_not_make_request(url):
    provider = KimiWebProvider("test-key", opener=lambda *a, **k: pytest.fail("不应请求"))
    with pytest.raises(ValueError):
        provider.fetch(url)


@pytest.mark.parametrize("query,count,need_content", [("", 6, True), ("x" * 129, 6, True),
    ("x", True, True), ("x", 21, True), ("x", 6, "true")])
def test_invalid_search_does_not_make_request(query, count, need_content):
    provider = KimiWebProvider("test-key", opener=lambda *a, **k: pytest.fail("不应请求"))
    with pytest.raises(ValueError):
        provider.search(query, count=count, need_content=need_content)


@pytest.mark.parametrize("body,method,code", [
    (b'{}', "search", "kimi_web_invalid_response"), (b'[]', "search", "kimi_web_invalid_response"),
    (b'{"search_results":[{}]}', "search", "kimi_web_invalid_response"),
    (b'{"title":"x","markdown":"body"}', "fetch", "kimi_web_invalid_response"),
    (b'not-json test-key', "fetch", "kimi_web_invalid_response"),
    (b'x' * (MAX_RESPONSE_BYTES + 1), "fetch", "kimi_web_response_too_large"),
    (json.dumps({"url":"https://example.org", "title":"正常", "markdown":" "}).encode(), "fetch", "kimi_web_empty_content"),
    (json.dumps({"url":"https://example.org", "title":"安全验证", "markdown":"请完成验证"}).encode(), "fetch", "kimi_web_access_challenge"),
], ids=["missing_results", "wrong_envelope", "missing_identity", "missing_url", "invalid_json", "oversized", "empty", "challenge"])
def test_http_success_does_not_admit_malformed_or_challenge_response(body, method, code):
    provider = KimiWebProvider("test-key", opener=lambda *a, **k: Response(body))
    with pytest.raises(KimiWebError) as error:
        getattr(provider, method)("https://example.org" if method == "fetch" else "coffee")
    assert error.value.code == code
    assert "test-key" not in str(error.value.response_document)


def test_empty_search_is_a_valid_observable_result():
    provider = KimiWebProvider("test-key", opener=lambda *a, **k: Response(b'{"search_results":[]}'))
    assert provider.search("coffee") == {"search_results": []}


@pytest.mark.parametrize("exception", [URLError("test-key"), TimeoutError("test-key")])
def test_connection_failure_is_typed_redacted_and_not_retried(exception):
    attempts = []
    def opener(*args, **kwargs):
        attempts.append(1)
        raise exception
    provider = KimiWebProvider("test-key", opener=opener)
    with pytest.raises(KimiWebError) as error:
        provider.search("coffee")
    assert error.value.code == "kimi_web_connection_failed" and "test-key" not in str(error.value)
    assert attempts == [1]


@pytest.mark.parametrize("operation,model", [("web_search", "search-basic"), ("web_fetch", "fetch")])
def test_existing_ledger_records_failure_and_prevents_automatic_retry(tmp_path, operation, model):
    attempts = []
    def opener(request, timeout):
        attempts.append(request.full_url)
        raise HTTPError(request.full_url, 429, "test-key", {}, BytesIO(b'{"error":"test-key"}'))
    provider = KimiWebProvider("test-key", opener=opener)
    with Database(tmp_path / "kimi.sqlite3") as db:
        profile = IPProfile(creator_name="网页发现")
        IPProfileRepository(db).create(profile)
        project = Project(ip_profile_id=profile.id, title="外部素材", topic="咖啡", fps=RationalFps(numerator=30, denominator=1),
            created_at=datetime.now(timezone.utc))
        ProjectRepository(db).create(project)
        BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, currency="CNY", max_calls=1, updated_at=datetime.now(timezone.utc)))
        service = ProviderExecutionService(db, CatalogProviderCostEstimator({(operation, "moonshot", model): provider.cost()}))
        arguments = dict(project_id=project.id, idempotency_key="kimi-once", operation=operation,
            provider=RuntimeProviderIdentity("moonshot", model), input_source="external_media_discovery",
            category=CostCategory.STOCK, input_document={"topic": "咖啡"},
            action=lambda: provider.search("coffee") if operation == "web_search" else provider.fetch("https://example.org"),
            encode_result=lambda value: value, decode_result=lambda value: value, error_code=lambda error: error.code)
        with pytest.raises(KimiWebError) as error:
            service.execute(**arguments)
        assert "test-key" not in str(error.value.response_document)
        with pytest.raises(ProviderExecutionRecordedFailure):
            service.execute(**arguments)
        record = ProviderCallRepository(db).list_for_project(project.id)[0]
        assert record.operation == operation and record.status == "failed"
        assert record.actual_cost is None and record.error_code == "kimi_web_http_429"
        assert len(attempts) == 1

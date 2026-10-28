import json
from datetime import timedelta
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from app.db import AssetRepository, ClipRepository, Database, ProviderCallRepository
from app.domain.models import JobType
from app.external_media_connection import configured_external_sources, external_provider, external_source_connections
from app.jobs import JobRunner, JobStore
from app.jobs.handlers import AssetAnalysisJobHandler
from app.jobs.targets import AssetJobTargetStore
from app.main import create_app
from app.media import MediaImporter
from app.providers.commons_media import ExternalMediaError
from app.providers.coverr_media import CoverrMediaProvider
from app.providers.pexels_media import PexelsMediaProvider
from app.providers.pixabay_media import PixabayMediaProvider
from test_coverr_media import row as coverr_row
from test_external_media import _project
from test_pixabay_media import Probe, Response, VIDEO, row as pixabay_row
from test_pexels_media import row as pexels_row
from test_media_pipeline import _StaticSegmenter, _WritingExtractor
from app.media.pipeline import MediaAnalysisPipeline


SETTINGS = {
    "pexels": "CONTENT_OS_PEXELS_API_KEY_FILE",
    "pixabay": "CONTENT_OS_PIXABAY_API_KEY_FILE",
    "coverr": "CONTENT_OS_COVERR_API_KEY_FILE",
}


def clear_sources(monkeypatch):
    for name, setting in SETTINGS.items():
        monkeypatch.delenv(setting, raising=False)
        # 裸环境 Key 值不属于此配置路径。
        monkeypatch.setenv(setting.removesuffix("_FILE"), "ignored-environment-secret")


def test_unconfigured_sources_are_listed_but_only_keyless_source_is_automatic(monkeypatch):
    clear_sources(monkeypatch)
    connections = external_source_connections()
    assert [{key: source[key] for key in ("id", "label", "configured")} for source in connections] == [
        {"id": "pexels", "label": "Pexels", "configured": False},
        {"id": "pixabay", "label": "Pixabay", "configured": False},
        {"id": "coverr", "label": "Coverr", "configured": False},
        {"id": "wikimedia_commons", "label": "Wikimedia Commons", "configured": True},
    ]
    assert all(source["verification_scope"] == "configuration_only" for source in connections)
    assert all(source["capabilities"] == {
        "video_search": True, "original_download": True, "automatic_acquisition": True,
    } for source in connections)
    for source in connections[:3]:
        assert source["source_kind"] == "stock_api" and source["connection_status"] == "missing_credentials"
        assert source["live_checks"] == ["authentication", "account_quota", "response_schema", "download_rendition", "network_access"]
    assert connections[-1]["connection_status"] == "keyless"
    assert configured_external_sources() == ["wikimedia_commons"]


def test_configured_sources_preserve_order_without_reading_credentials(tmp_path, monkeypatch):
    clear_sources(monkeypatch)
    for name, setting in SETTINGS.items():
        path = tmp_path / (name + ".txt")
        path.write_text("fake-" + name, encoding="utf-8")
        monkeypatch.setenv(setting, str(path))
    monkeypatch.setattr("pathlib.Path.read_text", lambda *a, **k: pytest.fail("列出连接不应读取密钥"))
    assert all(source["connection_status"] == "configured" for source in external_source_connections()[:3])
    assert configured_external_sources() == ["pexels", "pixabay", "coverr", "wikimedia_commons"]
    monkeypatch.setenv(SETTINGS["pixabay"], str(tmp_path / "missing.txt"))
    monkeypatch.setenv(SETTINGS["coverr"], str(tmp_path))
    assert configured_external_sources() == ["pexels", "wikimedia_commons"]


def test_configured_source_order_can_reorder_disable_and_reject_invalid_policy(tmp_path, monkeypatch):
    clear_sources(monkeypatch)
    for name, setting in SETTINGS.items():
        path = tmp_path / (name + ".txt")
        path.write_text("fixture-key", encoding="utf-8")
        monkeypatch.setenv(setting, str(path))
    monkeypatch.setattr("pathlib.Path.read_text", lambda *a, **k: pytest.fail("来源策略不应读取密钥"))
    monkeypatch.setenv("CONTENT_OS_EXTERNAL_SOURCE_ORDER", " pixabay, coverr, pexels ")
    assert configured_external_sources() == ["pixabay", "coverr", "pexels"]
    monkeypatch.delenv(SETTINGS["coverr"])
    assert configured_external_sources() == ["pixabay", "pexels"]
    monkeypatch.setenv("CONTENT_OS_EXTERNAL_SOURCE_ORDER", "wikimedia_commons")
    assert configured_external_sources() == ["wikimedia_commons"]
    for invalid in ("", " , ", "pexels,pexels", "pixabay,unknown"):
        monkeypatch.setenv("CONTENT_OS_EXTERNAL_SOURCE_ORDER", invalid)
        with pytest.raises(ExternalMediaError, match="顺序配置无效"):
            configured_external_sources()


@pytest.mark.parametrize("name", ["pexels", "pixabay", "coverr"])
def test_factory_requires_file_for_search_and_can_import_saved_result_without_key(tmp_path, monkeypatch, name):
    clear_sources(monkeypatch)
    project = _project(tmp_path / (name + ".sqlite"))
    with pytest.raises(ExternalMediaError, match="配置"):
        external_provider(name, project)
    path = tmp_path / (name + ".txt")
    path.write_text("\ufefffake-" + name, encoding="utf-8")
    monkeypatch.setenv(SETTINGS[name], str(path))
    provider = external_provider(name, project)
    assert provider.provider_name == name and provider.orientation == "portrait"
    path.unlink()
    with pytest.raises(ExternalMediaError, match="不可读"):
        external_provider(name, project)
    imported = external_provider(name, project, for_import=True)
    assert imported.provider_name == name and imported.orientation == "portrait"


@pytest.mark.parametrize("name,provider_class,source_row", [
    ("pexels", PexelsMediaProvider, pexels_row),
    ("pixabay", PixabayMediaProvider, pixabay_row),
    ("coverr", CoverrMediaProvider, coverr_row),
])
def test_ordinary_api_search_cache_import_and_local_job_use_configured_source(
        tmp_path, monkeypatch, name, provider_class, source_row):
    clear_sources(monkeypatch)
    path = tmp_path / "source.sqlite"
    project = _project(path)
    key_file = tmp_path / (name + ".txt")
    key_file.write_text("fake-" + name, encoding="utf-8")
    monkeypatch.setenv(SETTINGS[name], str(key_file))
    monkeypatch.setenv("CONTENT_OS_DATA_ROOT", str(tmp_path / "data"))
    seen = []
    def opener(request, timeout):
        seen.append(request)
        endpoint = {"pexels": "api.pexels.com/", "pixabay": "/api/videos/", "coverr": "api.coverr.co/videos"}[name]
        if endpoint in request.full_url:
            return Response(json.dumps({"videos" if name == "pexels" else "hits": [source_row()]}).encode())
        return Response(VIDEO, mime="video/mp4")
    factory_path = ("app.external_media_connection.PexelsMediaProvider" if name == "pexels" else
        f"app.providers.{name}_media.{provider_class.__name__}")
    monkeypatch.setattr(factory_path,
        lambda key=None, **kwargs: provider_class(key, opener=opener, **kwargs))
    app = create_app(path, media_importer_factory=lambda db, root: MediaImporter(db, root, Probe()))
    with TestClient(app) as client:
        base = f"/projects/{project.id}/external-media"
        providers = client.get(base + "/providers").json()
        assert [p["id"] for p in providers] == ["pexels", "pixabay", "coverr", "wikimedia_commons"]
        assert next(p for p in providers if p["id"] == name)["configured"] is True
        request = {"query": "coffee making", "provider": name, "idempotency_key": "search"}
        found = client.post(base + "/search", json=request)
        assert found.status_code == 200, found.text
        result = found.json()
        assert result["provider"] == name and len(result["candidates"]) == 1
        candidate = result["candidates"][0]
        assert (candidate["width"], candidate["height"]) == (1080, 1920)
        assert candidate["admissible"] and candidate["page_url"] and candidate["license_url"]
        cached = client.post(base + "/search", json={**request, "idempotency_key": "search-again"}).json()
        assert cached["cached"] and cached["call_id"] == result["call_id"] and len(seen) == 1
        # 已取得版本凭入库快照使用，不把再次读取 Key 当作素材使用许可。
        key_file.unlink()
        payload = {"search_call_id": result["call_id"], "candidate_id": result["candidates"][0]["id"],
            "idempotency_key": "import"}
        response = client.post(base + "/imports", json=payload)
        assert response.status_code == 201, response.text
        imported = response.json()
        again = client.post(base + "/imports", json={**payload, "idempotency_key": "import-again"})
        assert again.status_code == 201 and again.json() == imported and len(seen) == 2
        asset = client.get("/assets/" + imported["asset_id"]).json()
        source = asset["metadata"]["external_sources"][0]
        assert source["candidate"]["provider"] == name and source["quality_state"] == "unassessed"
        assert source["content_nature"] == "unknown" and source["content_hash"] == asset["content_hash"]
        assert client.get("/jobs/" + imported["analysis_job_id"]).json()["status"] == "pending"
        assert client.get(base + "/searches").json()[0]["provider"] == name
        assert next(p for p in client.get(base + "/providers").json() if p["id"] == name)["configured"] is False
    with Database(path) as db:
        # 同一普通分析 Handler/Pipeline 消费三家的入库结果，无来源专属执行分支。
        analysis = MediaAnalysisPipeline(db, _StaticSegmenter([(0, 8000)]), _WritingExtractor(tmp_path / "cache"))
        completed = JobRunner(JobStore(db), {JobType.ANALYZE_ASSET: AssetAnalysisJobHandler(
            AssetJobTargetStore(db), AssetRepository(db), analysis)}, worker_id="stock-contract",
            lease_duration=timedelta(minutes=1), max_attempts=1,
        ).run_once(job_id=UUID(imported["analysis_job_id"]))
        assert completed is not None and completed.status.value == "completed"
        clips = ClipRepository(db).list_by_asset(UUID(imported["asset_id"]))
        assert [(clip.start_ms, clip.end_ms) for clip in clips] == [(0, 8000)]
        assert clips[0].visual_description is None  # 目录命中和本地分析不冒充用途质量核验。
        calls = ProviderCallRepository(db).list_for_project(project.id)
        assert len(calls) == 2 and {call.operation for call in calls} == {"stock_search", "stock_import"}
        assert all("fake-" + name not in call.model_dump_json() for call in calls)


def test_pixabay_query_limit_is_checked_before_provider_or_budget_reservation(tmp_path, monkeypatch):
    clear_sources(monkeypatch)
    path = tmp_path / "limit.sqlite"
    project = _project(path)
    monkeypatch.setenv("CONTENT_OS_DATA_ROOT", str(tmp_path / "data"))
    with TestClient(create_app(path)) as client:
        response = client.post(f"/projects/{project.id}/external-media/search", json={
            "query": "x" * 101, "provider": "pixabay", "idempotency_key": "too-long"})
        assert response.status_code == 422
    with Database(path) as db:
        assert ProviderCallRepository(db).list_for_project(project.id) == []

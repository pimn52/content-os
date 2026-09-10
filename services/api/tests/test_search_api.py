from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import Asset, Clip, ProjectFormat, RationalFps
from app.main import create_app
from app.providers.embedding import EmbeddingBatch
from app.search import ClipSearchService


def _seed(path: Path, root: Path) -> tuple[Asset, Clip, Clip]:
    db = Database(path)
    source = root / "source.mp4"
    source.write_bytes(b"source")
    asset = Asset(
        source_file=str(source), content_hash="d" * 64, duration_ms=2_000,
        width=320, height=240, fps=RationalFps(numerator=25, denominator=1),
        authorization_reference="rights", imported_at=datetime.now(timezone.utc),
    )
    AssetRepository(db).create(asset)
    computer = Clip(
        asset_id=asset.id, start_ms=0, end_ms=1_000, asset_duration_ms=2_000,
        transcript="操作软件", visual_description="本人坐在电脑前", objects=["电脑"],
        orientation=ProjectFormat.VERTICAL, talking_candidate=True,
    )
    road = Clip(
        asset_id=asset.id, start_ms=1_000, end_ms=2_000, asset_duration_ms=2_000,
        transcript="道路", visual_description="户外行驶", orientation=ProjectFormat.HORIZONTAL,
    )
    ClipRepository(db).create(computer)
    ClipRepository(db).create(road)
    ClipSearchService(db).index_clips(((computer, (1.0, 0.0)), (road, (0.0, 1.0))))
    db.close()
    return asset, computer, road


def test_search_api_returns_semantic_top_k_with_filters(tmp_path: Path) -> None:
    path = tmp_path / "search-api.sqlite"
    asset, computer, _ = _seed(path, tmp_path)

    class Provider:
        def embed(self, texts):
            assert texts == ("本人坐在电脑前操作软件",)
            return EmbeddingBatch(((1.0, 0.0),))

    with TestClient(create_app(path, embedding_provider=Provider())) as client:
        response = client.post("/clips/search", json={
            "query": "本人坐在电脑前操作软件", "top_k": 1,
            "asset_id": str(asset.id), "orientation": "vertical", "talking_candidate": True,
        })
        assert response.status_code == 200
        assert response.json()[0]["clip"]["id"] == str(computer.id)
        assert response.json()[0]["score"] == 1.0


def test_search_api_requires_runtime_provider_and_rejects_secrets(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "search-config.sqlite"
    _seed(path, tmp_path)
    monkeypatch.delenv("CONTENT_OS_EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(create_app(path)) as client:
        assert client.post("/clips/search", json={"query": "test"}).status_code == 503
        rejected = client.post("/clips/search", json={"query": "test", "api_key": "must-not-persist"})
        assert rejected.status_code == 422


def test_search_api_explicit_lexical_mode_reports_non_embedding_basis(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "search-lexical.sqlite"
    _, computer, _ = _seed(path, tmp_path)
    monkeypatch.setenv("CONTENT_OS_RETRIEVAL_MODE", "lexical")
    monkeypatch.delenv("CONTENT_OS_EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with TestClient(create_app(path)) as client:
        response = client.post("/clips/search", json={"query": "电脑 软件", "top_k": 1})

    assert response.status_code == 200
    result = response.json()[0]
    assert result["clip"]["id"] == str(computer.id)
    assert result["score_basis"] == "lexical_overlap"
    assert result["score"] > 0


def test_runtime_semantic_search_requires_project_budget_and_records_its_provider_call(tmp_path: Path) -> None:
    path = tmp_path / "search-budget.sqlite"
    _, computer, _ = _seed(path, tmp_path)
    calls: list[tuple[str, ...]] = []

    class Provider:
        provider_name = "test-runtime"
        model = "semantic-search-v1"

        def embed(self, texts):
            calls.append(tuple(texts))
            return EmbeddingBatch(((1.0, 0.0),))

    with TestClient(create_app(path, embedding_provider=Provider())) as client:
        missing_scope = client.post("/clips/search", json={"query": "本人坐在电脑前操作软件"})
        assert missing_scope.status_code == 422
        assert calls == []
        project = client.post("/projects", json={"title": "Semantic search", "topic": "budgeted search"}).json()
        assert client.put("/budget", json={"allow_unknown_cost": True}).status_code == 200
        response = client.post(
            "/clips/search",
            json={"query": "本人坐在电脑前操作软件", "project_id": project["id"]},
            headers={"Idempotency-Key": "semantic-search"},
        )
        assert response.status_code == 200
        assert response.json()[0]["clip"]["id"] == str(computer.id)
        assert len(calls) == 1
        records = client.get(f"/projects/{project['id']}/provider-calls").json()
        assert len(records) == 1
        assert records[0]["operation"] == "embedding"

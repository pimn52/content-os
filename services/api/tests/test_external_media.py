from datetime import datetime, timezone
from hashlib import sha1
from io import BytesIO
from urllib.error import HTTPError
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db import AssetRepository, ClipRepository, Database, IPProfileRepository, ProjectRepository
from app.domain.models import Clip, IPProfile, Project, RationalFps, ScenePlan, SourceKind, VisualIntent
from app.external_media import attribution_text
from app.main import create_app
from app.media import MediaImporter, ProbeMetadata
from app.providers.commons_media import CommonsMediaProvider, ExternalMediaError, MAX_VIDEO_BYTES
from app.search import ClipTextSearchService
from app.routing import AssetRouter


VIDEO = b"local-media-fixture"


def _page(**metadata):
    return {"pageid": 41, "title": "File:Galaxy collision.webm", "imageinfo": [{
        "url": "https://upload.wikimedia.org/wikipedia/commons/a/a1/Galaxy.webm",
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Galaxy.webm", "mime": "video/webm",
        "sha1": sha1(VIDEO).hexdigest(), "size": len(VIDEO), "width": 1280, "height": 720,
        "extmetadata": {key: {"value": value} for key, value in {"Artist": "<b>NASA</b>",
            "LicenseShortName": "Public domain", "Copyrighted": "False", "ImageDescription": "Galaxy collision in space",
            **metadata}.items()}}]}


@pytest.mark.parametrize("fields,allowed", [({}, True),
    ({"LicenseShortName": "CC BY 4.0", "Copyrighted": "True", "LicenseUrl": "https://creativecommons.org/licenses/by/4.0/"}, True),
    ({"LicenseShortName": "CC BY 3.0", "Copyrighted": "True", "LicenseUrl": "http://creativecommons.org/licenses/by/3.0/deed.en"}, True),
    ({"LicenseShortName": "CC BY-SA 4.0", "Copyrighted": "True", "LicenseUrl": "https://creativecommons.org/licenses/by-sa/4.0/"}, False),
    ({"LicenseShortName": "Unknown", "Copyrighted": "True"}, False),
    ({"Restrictions": "personality rights"}, False)])
def test_file_level_license_admission_precedes_download(fields, allowed):
    result = CommonsMediaProvider._candidate(_page(**fields))
    assert result.admissible is allowed
    assert result.author == "NASA"
    assert "https://commons.wikimedia.org" in result.attribution


def test_redirect_cannot_access_local_or_untrusted_host(tmp_path):
    opened = []
    def opener(request, timeout):
        opened.append(request.full_url)
        raise HTTPError(request.full_url, 302, "redirect", {"Location": "http://127.0.0.1/private"}, None)
    provider = CommonsMediaProvider(opener=opener)
    with pytest.raises(ExternalMediaError, match="地址"):
        provider.download(provider._candidate(_page()), tmp_path / "download")
    assert len(opened) == 1
    assert not (tmp_path / "download").exists()


def test_download_is_bounded_and_content_version_verified(tmp_path):
    class Response(BytesIO):
        headers = {"Content-Length": str(MAX_VIDEO_BYTES + 1)}
    provider = CommonsMediaProvider(opener=lambda *a, **k: Response(VIDEO))
    value = provider._candidate(_page())
    with pytest.raises(ExternalMediaError, match="大小限制"):
        provider.download(value, tmp_path / "download")
    assert not (tmp_path / "download").exists()
    Response.headers = {}
    provider = CommonsMediaProvider(opener=lambda *a, **k: Response(b"changed"))
    with pytest.raises(ExternalMediaError, match="版本不符"):
        provider.download(value, tmp_path / "download")


def test_download_verifies_exact_bytes_and_normalizes_public_tracking_url(tmp_path):
    requests = []
    class Response(BytesIO):
        headers = {"Content-Length": str(len(VIDEO))}
    def opener(request, timeout):
        requests.append(request)
        return Response(VIDEO)
    provider = CommonsMediaProvider(opener=opener)
    value = provider._candidate(_page()).model_copy(update={"download_url": "https://upload.wikimedia.org/wikipedia/commons/a/a1/Galaxy.webm?utm_source=commons"})
    output = tmp_path / "download"
    provider.download(value, output)
    assert output.read_bytes() == VIDEO
    assert requests[0].full_url.endswith("Galaxy.webm")
    assert len(requests) == 1


class Probe:
    def probe(self, path):
        return ProbeMetadata(6000, 1280, 720, RationalFps(numerator=30, denominator=1), False, {})


class Provider(CommonsMediaProvider):
    def __init__(self):
        self.search_count = self.download_count = 0
        self.video = self._candidate(_page())
    def search(self, query, *, limit=8):
        self.search_count += 1
        return [self.video]
    def download(self, candidate, destination):
        self.download_count += 1
        destination.write_bytes(VIDEO)


def _project(path):
    with Database(path) as db:
        profile = IPProfile(creator_name="素材验证")
        IPProfileRepository(db).create(profile)
        project = Project(ip_profile_id=profile.id, title="宇宙科普", topic="Galaxy collision",
            fps=RationalFps(numerator=30, denominator=1), created_at=datetime.now(timezone.utc))
        ProjectRepository(db).create(project)
        return project


def test_ui_api_import_is_durable_idempotent_and_usable_by_router(tmp_path, monkeypatch):
    path = tmp_path / "media.sqlite"
    project = _project(path)
    monkeypatch.setenv("CONTENT_OS_DATA_ROOT", str(tmp_path))
    provider = Provider()
    application = create_app(path, external_media_provider=provider,
        media_importer_factory=lambda db, root: MediaImporter(db, root, Probe()))
    with TestClient(application) as client:
        base = f"/projects/{project.id}/external-media"
        payload = {"query": "galaxy", "idempotency_key": "search-once"}
        found = client.post(base + "/search", json=payload)
        assert found.status_code == 200, found.text
        assert client.post(base + "/search", json=payload).json() == found.json()
        search = found.json()
        request = {"search_call_id": search["call_id"], "candidate_id": search["candidates"][0]["id"], "idempotency_key": "import-once"}
        imported = client.post(base + "/imports", json=request)
        assert imported.status_code == 201, imported.text
        value = imported.json()
        assert client.post(base + "/imports", json={**request, "idempotency_key": "same-media-new-click"}).json() == value
        assert provider.search_count == provider.download_count == 1
        asset = client.get(f"/assets/{value['asset_id']}").json()
        assert asset["source_kind"] == "stock"
        assert asset["metadata"]["external_sources"][0]["quality_state"] == "unassessed"
        assert client.get(f"/jobs/{value['analysis_job_id']}").json()["status"] == "pending"
        calls = client.get(f"/projects/{project.id}/provider-calls").json()
        assert [v["operation"] for v in calls] == ["stock_search", "stock_import"]
        assert all(v["status"] == "completed" and v["estimated_cost"]["amount"] == "0" for v in calls)
    # A restart recovers server-side results, local provenance and attribution.
    with TestClient(application) as client:
        assert client.get(base + "/searches").json()[0] == search
        assert client.post(base + "/imports", json=request).json() == value
    with Database(path) as db:
        asset = AssetRepository(db).list()[0]
        assert "NASA" in attribution_text([asset])
        # Clip segmentation is provided by the ordinary analysis job. Catalogue
        # hints remain separate from observed visual_description and ASR.
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=6000, asset_duration_ms=6000)
        ClipRepository(db).create(clip)
        hits = ClipTextSearchService(db).search("galaxy collision")
        assert hits[0].clip.visual_description is None and hits[0].score_basis == "lexical_overlap"
        scene = ScenePlan(project_id=project.id, scene_id="galaxy", order=0, purpose="宇宙现象",
            voice_text="Galaxy collision", duration_target_ms=3000, visual_intent=VisualIntent(subject="galaxy collision"),
            preferred_sources=[SourceKind.STOCK], fallback_sources=[SourceKind.USER_ASSET])
        result = AssetRouter(ClipTextSearchService(db), AssetRepository(db)).route(scene)
        assert result.candidates[0].asset_id == asset.id and result.candidates[0].recommended
        assert result.candidates[0].estimated_cost.category.value == "stock"


def test_unadmitted_and_forged_candidates_do_not_download(tmp_path, monkeypatch):
    path = tmp_path / "rejected.sqlite"
    project = _project(path)
    provider = Provider()
    provider.video = provider.video.model_copy(update={"admissible": False})
    monkeypatch.setenv("CONTENT_OS_DATA_ROOT", str(tmp_path))
    with TestClient(create_app(path, external_media_provider=provider)) as client:
        base = f"/projects/{project.id}/external-media"
        found = client.post(base + "/search", json={"query": "galaxy", "idempotency_key": "s"}).json()
        payload = {"search_call_id": found["call_id"], "candidate_id": found["candidates"][0]["id"], "idempotency_key": "i"}
        assert client.post(base + "/imports", json=payload).status_code == 422
        assert client.post(base + "/imports", json={**payload, "candidate_id": str(uuid4())}).status_code == 422
        assert client.post(base + "/imports", json={**payload, "download_url": "http://localhost/private"}).status_code == 422
        assert provider.download_count == 0
        assert len(client.get(f"/projects/{project.id}/provider-calls").json()) == 1


@pytest.mark.parametrize("exact,width,height,status", [(False,2560,1440,201),(True,2560,1440,422),(False,2560,2560,422)])
def test_catalogue_maximum_dimensions_are_not_treated_as_download_rendition(tmp_path,monkeypatch,exact,width,height,status):
    path=tmp_path/"geometry.sqlite";project=_project(path)
    provider=Provider()
    provider.video=provider.video.model_copy(update={"sha1":None,"width":width,"height":height,"dimensions_exact":exact})
    monkeypatch.setenv("CONTENT_OS_DATA_ROOT",str(tmp_path))
    app=create_app(path,external_media_provider=provider,media_importer_factory=lambda db,root:MediaImporter(db,root,Probe()))
    with TestClient(app) as client:
        base=f"/projects/{project.id}/external-media"
        found=client.post(base+"/search",json={"query":"galaxy","idempotency_key":"s"}).json()
        response=client.post(base+"/imports",json={"search_call_id":found["call_id"],"candidate_id":found["candidates"][0]["id"],"idempotency_key":"i"})
        assert response.status_code==status,response.text
        if status==201:
            asset=client.get(f"/assets/{response.json()['asset_id']}").json()
            assert (asset["width"],asset["height"])==(1280,720)
            record=asset["metadata"]["external_sources"][0]
            assert record["candidate"]["dimensions_exact"] is False
            assert record["downloaded_dimensions"]=={"width":1280,"height":720}


def test_budget_and_recorded_failure_prevent_unrequested_external_retries(tmp_path, monkeypatch):
    path = tmp_path / "budget.sqlite"
    project = _project(path)
    provider = Provider()
    monkeypatch.setenv("CONTENT_OS_DATA_ROOT", str(tmp_path))
    with TestClient(create_app(path, external_media_provider=provider)) as client:
        base = f"/projects/{project.id}/external-media"
        client.put(f"/projects/{project.id}/budget", json={"max_calls": 0}).raise_for_status()
        assert client.post(base + "/search", json={"query": "galaxy", "idempotency_key": "s"}).status_code == 409
        assert provider.search_count == 0
        client.put(f"/projects/{project.id}/budget", json={"max_calls": 2}).raise_for_status()
        found = client.post(base + "/search", json={"query": "galaxy", "idempotency_key": "s"}).json()
        def failed(candidate, destination):
            provider.download_count += 1
            raise ExternalMediaError("下载服务暂不可用")
        provider.download = failed
        payload = {"search_call_id": found["call_id"], "candidate_id": found["candidates"][0]["id"], "idempotency_key": "i"}
        assert client.post(base + "/imports", json=payload).status_code == 502
        assert client.post(base + "/imports", json=payload).status_code == 409
        assert provider.download_count == 1
        assert client.get("/assets").json() == []
        calls = client.get(f"/projects/{project.id}/provider-calls").json()
        assert calls[-1]["status"] == "failed" and calls[-1]["error_code"] == "external_media_failed"


def test_external_download_does_not_block_ordinary_ui_requests(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic
    path = tmp_path / "responsive.sqlite"
    project = _project(path)
    provider = Provider()
    started, release = Event(), Event()
    def download(candidate, destination):
        started.set()
        assert release.wait(5)
        destination.write_bytes(VIDEO)
    provider.download = download
    monkeypatch.setenv("CONTENT_OS_DATA_ROOT", str(tmp_path))
    application = create_app(path, external_media_provider=provider,
        media_importer_factory=lambda db,root: MediaImporter(db,root,Probe()))
    with TestClient(application) as client, ThreadPoolExecutor(max_workers=1) as pool:
        base = f"/projects/{project.id}/external-media"
        found = client.post(base+"/search",json={"query":"galaxy","idempotency_key":"s"}).json()
        future = pool.submit(client.post,base+"/imports",json={"search_call_id":found["call_id"],
            "candidate_id":found["candidates"][0]["id"],"idempotency_key":"i"})
        try:
            assert started.wait(2)
            start = monotonic()
            assert client.get("/health").status_code == 200
            assert monotonic()-start < 0.75
        finally:
            release.set()
        assert future.result(timeout=3).status_code == 201

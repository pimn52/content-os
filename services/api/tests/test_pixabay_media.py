from io import BytesIO
import json
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from app.db import AssetRepository, Database, ProviderCallRepository
from app.domain.models import RationalFps
from app.external_media import ExternalMediaService
from app.media import MediaImporter, ProbeMetadata
from app.providers.commons_media import ExternalMediaError
from app.providers.pixabay_media import PixabayMediaProvider
from test_external_media import _project


VIDEO = b"pixabay-local-video-fixture"


def row(**updates):
    return {"id": 41, "pageURL": "https://pixabay.com/videos/id-41/", "type": "film",
        "tags": "coffee, espresso, milk", "duration": 8, "user": "Creator", "videos": {
            "large": {"url": "https://cdn.pixabay.com/video/41_large.mp4", "width": 2160, "height": 3840, "size": 50000000},
            "medium": {"url": "https://cdn.pixabay.com/video/41_medium.mp4", "width": 1080, "height": 1920, "size": 1234567},
            "small": {"url": "https://cdn.pixabay.com/video/41_small.mp4", "width": 720, "height": 1280, "size": 600000}}, **updates}


class Response(BytesIO):
    def __init__(self, body, *, mime="application/json", length=None, status=200):
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Type": mime, "Content-Length": str(len(body) if length is None else length)}


def catalogue(rows):
    return Response(json.dumps({"hits": rows}).encode())


def test_video_request_uses_official_fields_and_postfilters_actual_orientation():
    seen = []
    wrong = row(id=42, videos={"medium": {"url": "https://cdn.pixabay.com/video/42.mp4",
        "width": 1920, "height": 1080, "size": 12345}})
    def opener(request, timeout):
        seen.append(request)
        result = catalogue([wrong, row()])
        result.headers.update({"X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "99", "X-RateLimit-Reset": "55"})
        return result
    provider = PixabayMediaProvider("dummy-key", opener=opener)
    values = provider.search("咖啡制作")
    assert len(values) == 1 and values[0].width == 1080 and values[0].height == 1920
    assert values[0].size_bytes == 1234567 and values[0].sha1 is None and values[0].duration_ms == 8000
    assert values[0].license_name == "Pixabay Content License" and "非画面核验" in values[0].description
    parsed = urlsplit(seen[0].full_url)
    assert parsed.path == "/api/videos/"
    params = parse_qs(parsed.query)
    assert params["key"] == ["dummy-key"] and params["q"] == ["咖啡制作"]
    assert params["per_page"] == ["32"]
    assert "orientation" not in params and params["safesearch"] == ["true"]
    assert provider.last_quota == {"limit": "100", "remaining": "99", "reset": "55"}
    assert provider.last_rejections == [{"source_id": "42", "reason": "no_matching_hd_version"}]
    assert "dummy-key" not in values[0].model_dump_json() + provider.search_scope


def test_single_catalogue_call_can_find_portrait_after_first_eight_but_returns_only_limit():
    seen=[]
    wide={"medium":{"url":"https://cdn.pixabay.com/video/wide.mp4","width":1920,"height":1080}}
    samples=[row(id=i+1,videos=wide) for i in range(8)]+[row(id=i+20) for i in range(24)]
    def opener(request,timeout):
        seen.append(request)
        return catalogue(samples)
    provider=PixabayMediaProvider("fixture-key",opener=opener)
    result=provider.search("coffee making",limit=3)
    assert [value.source_id.split(":",1)[0] for value in result]==["20","21","22"]
    assert len(seen)==1 and parse_qs(urlsplit(seen[0].full_url).query)["per_page"]==["32"]


@pytest.mark.parametrize("orientation,width,height", [("landscape", 1920, 1080), ("square", 1080, 1080)])
def test_landscape_and_square_filter_on_real_dimensions(orientation, width, height):
    sample = row(videos={"medium": {"url": "https://cdn.pixabay.com/video/41.mp4", "width": width, "height": height}})
    provider = PixabayMediaProvider("dummy-key", orientation=orientation, opener=lambda *a, **k: catalogue([row(), sample]))
    values = provider.search("coffee")
    assert len(values) == 1 and (values[0].width, values[0].height) == (width, height)


def test_hd_fallback_and_smallest_higher_rendition_avoid_unnecessary_4k():
    provider = PixabayMediaProvider()
    sample = row()
    del sample["videos"]["medium"]
    assert provider._candidate(sample).height == 1280
    sample["videos"] = {"large": sample["videos"]["large"], "medium": {
        "url": "https://cdn.pixabay.com/video/41_1440.mp4", "width": 1440, "height": 2560}}
    assert provider._candidate(sample).width == 1440


def test_candidate_identity_ignores_rotating_query_but_preserves_rendition_path():
    provider = PixabayMediaProvider()
    original = provider._candidate(row())
    sample = row()
    sample["videos"]["medium"]["url"] += "?signature=rotated"
    assert provider._candidate(sample).id == original.id
    sample["videos"]["medium"]["url"] = "https://cdn.pixabay.com/video/replaced_medium.mp4"
    assert provider._candidate(sample).id != original.id


@pytest.mark.parametrize("query", ["", "   ", "x" * 101, None])
def test_invalid_query_never_requests_or_silently_truncates(query):
    provider = PixabayMediaProvider("dummy-key", opener=lambda *a, **k: pytest.fail("不应请求"))
    with pytest.raises(ExternalMediaError) as error:
        provider.search(query)
    assert error.value.code == "pixabay_query_invalid"


def test_cache_is_account_and_orientation_scoped_and_keyless_import_does_not_allow_search():
    assert PixabayMediaProvider("one").search_scope != PixabayMediaProvider("two").search_scope
    assert PixabayMediaProvider("one").search_scope != PixabayMediaProvider("one", orientation="landscape").search_scope
    assert PixabayMediaProvider.cache_ttl_seconds == 86400
    with pytest.raises(ExternalMediaError, match="配置"):
        PixabayMediaProvider(opener=lambda *a, **k: pytest.fail("不应请求")).search("coffee")


def test_invalid_source_entries_are_isolated_and_never_forward_api_credentials():
    bad = row(id=42)
    bad["videos"]["medium"]["url"] = "https://cdn.pixabay.com/video/42.mp4?key=dummy-key"
    bad["videos"] = {"medium": bad["videos"]["medium"]}
    local = row(id=43, pageURL="http://127.0.0.1/private")
    provider = PixabayMediaProvider("dummy-key", opener=lambda *a, **k: catalogue([None, {"id": "dummy-key"}, bad, local, row()]))
    values = provider.search("coffee")
    assert len(values) == 1 and len(provider.last_rejections) == 4
    assert "dummy-key" not in json.dumps(provider.last_rejections)


@pytest.mark.parametrize("failure", ["429", "401", "network", "redirect"])
def test_api_failures_do_not_retry_or_expose_key_or_response(failure):
    seen = []
    def opener(request, timeout):
        seen.append(request)
        if failure == "network":
            raise URLError(request.full_url)
        code = 302 if failure == "redirect" else int(failure)
        raise HTTPError(request.full_url, code, "dummy-key", {"Location": "https://example.com/private",
            "X-RateLimit-Reset": "30"}, BytesIO(b"dummy-key private error body"))
    with pytest.raises(ExternalMediaError) as error:
        PixabayMediaProvider("dummy-key", opener=opener).search("coffee")
    assert len(seen) == 1 and "dummy-key" not in str(error.value) and "private" not in str(error.value)
    if failure == "429":
        assert error.value.code == "pixabay_http_429" and "30" in str(error.value)


@pytest.mark.parametrize("body", [b"not json", b'{}', b'{"hits": {}}'])
def test_invalid_catalogue_is_rejected(body):
    with pytest.raises(ExternalMediaError, match="目录响应"):
        PixabayMediaProvider("dummy-key", opener=lambda *a, **k: Response(body)).search("coffee")


def test_response_read_failure_cannot_leak_key_bearing_catalogue_url():
    class BrokenResponse(Response):
        def read(self, *args):
            raise OSError("https://pixabay.com/api/videos/?key=dummy-key")
    with pytest.raises(ExternalMediaError) as error:
        PixabayMediaProvider("dummy-key", opener=lambda *a, **k: BrokenResponse(b"")).search("coffee")
    assert "dummy-key" not in str(error.value) and "目录读取" in str(error.value)


@pytest.mark.parametrize("case", ["success", "html", "truncated", "partial", "oversize", "untrusted_redirect", "key_redirect"])
def test_download_is_bounded_complete_and_has_no_api_key(tmp_path, case):
    seen = []
    def opener(request, timeout):
        seen.append(request)
        if case in ("untrusted_redirect", "key_redirect"):
            target = "http://127.0.0.1/private" if case == "untrusted_redirect" else "https://cdn.pixabay.com/video/file.mp4?api_key=secret"
            raise HTTPError(request.full_url, 302, "redirect", {"Location": target}, None)
        response = Response(VIDEO, mime="text/html" if case == "html" else "video/mp4",
            length=len(VIDEO) + 1 if case == "truncated" else None, status=206 if case == "partial" else 200)
        return response
    provider = PixabayMediaProvider("dummy-key", opener=opener)
    candidate = provider._candidate(row())
    destination = tmp_path / "download.mp4"
    if case == "success":
        provider.download(candidate, destination)
        assert destination.read_bytes() == VIDEO
    else:
        with pytest.raises(ExternalMediaError):
            provider.download(candidate, destination, max_bytes=2 if case == "oversize" else 1024)
        assert not destination.exists()
    assert len(seen) == 1
    assert all("dummy-key" not in request.full_url and request.get_header("Authorization") is None for request in seen)


def test_media_can_follow_trusted_redirect_and_import_without_key(tmp_path):
    seen = []
    def opener(request, timeout):
        seen.append(request)
        if len(seen) == 1:
            raise HTTPError(request.full_url, 302, "redirect", {"Location": "https://cdn.pixabay.com/video/canonical.mp4"}, None)
        return Response(VIDEO, mime="video/mp4")
    provider = PixabayMediaProvider(opener=opener)
    destination = tmp_path / "download.mp4"
    provider.download(provider._candidate(row()), destination)
    assert destination.read_bytes() == VIDEO and len(seen) == 2


def test_expired_download_window_does_not_open_or_delete_existing_file(tmp_path):
    destination = tmp_path / "kept.mp4"
    destination.write_bytes(b"keep")
    provider = PixabayMediaProvider(opener=lambda *a, **k: pytest.fail("不应请求"))
    with pytest.raises(ExternalMediaError, match="窗口"):
        provider.download(provider._candidate(row()), destination, deadline=0)
    assert destination.read_bytes() == b"keep"


class Probe:
    def probe(self, path):
        return ProbeMetadata(8000, 1080, 1920, RationalFps(numerator=30, denominator=1), False, {})


def test_existing_service_caches_imports_tracks_provenance_and_uses_ordinary_analysis_job(tmp_path):
    path = tmp_path / "source.sqlite"
    project = _project(path)
    seen = []
    def opener(request, timeout):
        seen.append(request)
        return catalogue([row()]) if urlsplit(request.full_url).path == "/api/videos/" else Response(VIDEO, mime="video/mp4")
    with Database(path) as db:
        provider = PixabayMediaProvider("dummy-key", opener=opener)
        service = ExternalMediaService(db, tmp_path / "data", provider, MediaImporter(db, tmp_path / "data", Probe()))
        result = service.search(project.id, "coffee making", "one")
        cached = service.search(project.id, "coffee making", "two")
        assert cached.cached and cached.call_id == result.call_id and len(seen) == 1
        imported = service.import_video(project.id, result.call_id, result.candidates[0].id, "import")
        again = service.import_video(project.id, result.call_id, result.candidates[0].id, "different-import")
        assert imported == again and len(seen) == 2
        asset = AssetRepository(db).get(imported.asset_id)
        source = asset.metadata["external_sources"][0]
        assert source["quality_state"] == "unassessed" and source["content_nature"] == "unknown"
        assert source["candidate"]["provider"] == "pixabay" and source["candidate"]["sha1"] is None
        assert source["content_hash"] == asset.content_hash
        calls = ProviderCallRepository(db).list_for_project(project.id)
        assert len(calls) == 2 and {call.operation for call in calls} == {"stock_search", "stock_import"}
        assert all("dummy-key" not in call.model_dump_json() for call in calls)

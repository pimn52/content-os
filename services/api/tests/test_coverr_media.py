from io import BytesIO
import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from app.providers.commons_media import ExternalMediaError
from app.providers.coverr_media import CoverrMediaProvider


VIDEO = b"bounded-coverr-original-fixture"


def row(**updates):
    return {"id": "official-video-id", "updated_at": "2026-10-01T08:55:42.710Z", "title": "Making coffee",
        "description": "A person making coffee", "duration": 10.01, "max_width": 1080, "max_height": 1920,
        "urls": {"mp4_preview": "https://storage.coverr.co/videos/official-video-id/preview?token=preview",
                 "mp4_download": "https://storage.coverr.co/videos/official-video-id/download?token=signed-version"}, **updates}


class Response(BytesIO):
    def __init__(self, content, *, mime="application/json", length=None):
        super().__init__(content)
        self.status = 200
        self.headers = {"Content-Type": mime, "Content-Length": str(len(content) if length is None else length)}


def test_search_uses_official_auth_original_download_and_local_geometry_filter():
    seen = []
    def opener(request, timeout):
        seen.append(request)
        response = Response(json.dumps({"hits": [row(), row(max_width=1920, max_height=1080), None,
            row(id="bad / id"), row(urls={"mp4_preview": "https://storage.coverr.co/videos/id/preview"}),
            row(urls={"mp4_download": "https://example.org/file.mp4"})]}).encode())
        response.headers["X-Ratelimit-Remaining"] = "49"
        return response
    provider = CoverrMediaProvider("dummy-coverr-key", opener=opener)
    values = provider.search("making coffee")
    params = parse_qs(urlsplit(seen[0].full_url).query)
    assert params == {"query": ["making coffee"], "page": ["0"], "page_size": ["20"], "urls": ["true"], "sort": ["popular"]}
    assert seen[0].get_header("Authorization") == "Bearer dummy-coverr-key"
    assert "dummy-coverr-key" not in seen[0].full_url
    assert len(values) == 1 and values[0].source_id == "official-video-id"
    assert "/download?" in values[0].download_url and "/preview" not in values[0].download_url
    assert (values[0].width, values[0].height, values[0].duration_ms) == (1080, 1920, 10010)
    assert values[0].sha1 is None and values[0].size_bytes is None
    assert values[0].dimensions_exact is False
    assert "非画面核验" in values[0].description and "dummy-coverr-key" not in values[0].model_dump_json()
    assert len(provider.last_rejections) == 5 and provider.last_quota == {"remaining": "49"}
    assert "Coverr" in values[0].attribution and "official-video-id" in values[0].attribution


def test_single_catalogue_call_can_find_portrait_after_first_eight_but_returns_only_limit():
    seen=[]
    samples=[row(id=f"wide-{i}",max_width=1920,max_height=1080) for i in range(8)]
    samples += [row(id=f"portrait-{i}") for i in range(12)]
    def opener(request,timeout):
        seen.append(request)
        return Response(json.dumps({"hits":samples}).encode())
    provider=CoverrMediaProvider("fixture-key",opener=opener)
    result=provider.search("coffee making",limit=3)
    assert [value.source_id for value in result]==["portrait-0","portrait-1","portrait-2"]
    assert len(seen)==1 and parse_qs(urlsplit(seen[0].full_url).query)["page_size"]==["20"]


def test_signed_url_rotation_keeps_identity_but_source_version_and_geometry_do_not():
    provider = CoverrMediaProvider()
    original = provider._candidate(row())
    rotated = row(urls={"mp4_download": "https://storage.coverr.co/videos/official-video-id/download?token=rotated"})
    assert provider._candidate(rotated).id == original.id
    assert provider._candidate(row(updated_at="2026-10-02T00:00:00Z")).id != original.id
    assert provider._candidate(row(max_width=720, max_height=1280)).id != original.id
    assert provider.search_scope != CoverrMediaProvider("another-account").search_scope
    assert provider.search_scope != CoverrMediaProvider(orientation="landscape").search_scope
    assert 0 < provider.cache_ttl_seconds < 86400


def test_api_rate_limit_does_not_retry_leak_key_or_follow_redirect():
    seen = []
    def opener(request, timeout):
        seen.append(request)
        raise HTTPError(request.full_url, 429, "dummy-coverr-key", {"Retry-After": "60"}, None)
    with pytest.raises(ExternalMediaError, match="60") as error:
        CoverrMediaProvider("dummy-coverr-key", opener=opener).search("coffee")
    assert error.value.code == "coverr_http_429" and error.value.retry_after_seconds == 60
    assert "dummy-coverr-key" not in str(error.value) and len(seen) == 1
    def redirect(request, timeout):
        raise HTTPError(request.full_url, 302, "redirect", {"Location": "https://example.org/"}, None)
    with pytest.raises(ExternalMediaError) as error:
        CoverrMediaProvider("dummy-coverr-key", opener=redirect).search("coffee")
    assert error.value.code == "coverr_http_302"


@pytest.mark.parametrize("case", ["success", "html", "truncated", "untrusted_redirect", "partial_response", "size_limit", "deadline", "preview"])
def test_download_keeps_api_key_off_media_and_rejects_unusable_files(tmp_path, case):
    seen = []
    def opener(request, timeout):
        seen.append(request)
        if case == "untrusted_redirect":
            raise HTTPError(request.full_url, 302, "redirect", {"Location": "https://example.org/private"}, None)
        response = Response(VIDEO, mime="text/html" if case == "html" else "video/mp4",
            length=len(VIDEO) + 1 if case == "truncated" else None)
        if case == "partial_response":
            response.status = 206
        return response
    provider = CoverrMediaProvider("dummy-coverr-key", opener=opener)
    candidate = provider._candidate(row())
    if case == "preview":
        candidate = candidate.model_copy(update={"download_url": "https://storage.coverr.co/videos/id/preview?token=preview"})
    destination = tmp_path / "original.mp4"
    options = {"max_bytes": len(VIDEO) - 1} if case == "size_limit" else {"deadline": 0} if case == "deadline" else {}
    if case == "success":
        provider.download(candidate, destination, **options)
        assert destination.read_bytes() == VIDEO
    else:
        with pytest.raises(ExternalMediaError):
            provider.download(candidate, destination, **options)
        assert not destination.exists()
    assert all(request.get_header("Authorization") is None for request in seen)
    assert len(seen) == (0 if case in {"preview", "deadline"} else 1)


@pytest.mark.parametrize("body", [b"not-json", b"{}", b'{"hits":null}', b'{"hits":{}}'])
def test_invalid_directory_is_a_credential_safe_failure(body):
    with pytest.raises(ExternalMediaError) as error:
        CoverrMediaProvider("dummy-key", opener=lambda *a, **kw: Response(body)).search("coffee")
    assert error.value.code == "coverr_invalid_response" and "dummy-key" not in str(error.value)


@pytest.mark.parametrize("url", ["http://storage.coverr.co/x", "https://storage.coverr.co.evil.example/x",
    "https://127.0.0.1/x", "https://user:password@storage.coverr.co/x", "https://storage.coverr.co:444/x",
    "https://storage.coverr.co/x#fragment", "https://storage.coverr.co/x?api_key=secret"])
def test_candidate_rejects_untrusted_original_addresses(url):
    with pytest.raises(ExternalMediaError):
        CoverrMediaProvider()._candidate(row(urls={"mp4_download": url}))


def test_existing_destination_survives_rejected_download(tmp_path):
    destination = tmp_path / "existing.mp4"
    destination.write_bytes(b"keep-original")
    provider = CoverrMediaProvider(opener=lambda *a, **kw: pytest.fail("不要下载覆盖已有文件"))
    with pytest.raises(ExternalMediaError):
        provider.download(provider._candidate(row()), destination)
    assert destination.read_bytes() == b"keep-original"

from datetime import datetime, timezone
from io import BytesIO
import json
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

from app.db import Database, IPProfileRepository, ProjectRepository
from app.domain.models import IPProfile, Project, RationalFps
from app.main import create_app
from app.media import MediaImporter, ProbeMetadata
from app.providers.commons_media import ExternalMediaError
from app.providers.pexels_media import PexelsMediaProvider, page_video_id


VIDEO = b'bounded-pexels-fixture'


def row(**updates):
    return {'id': 41, 'url': 'https://www.pexels.com/video/making-coffee-41/',
        'duration': 8, 'user': {'name': 'Creator'}, 'video_files': [
            {'id': 42, 'file_type': 'video/mp4', 'width': 2160, 'height': 3840, 'link': 'https://videos.pexels.com/video-files/41/4k.mp4'},
            {'id': 43, 'file_type': 'video/mp4', 'width': 1080, 'height': 1920, 'link': 'https://videos.pexels.com/video-files/41/fullhd.mp4'},
            {'id': 44, 'file_type': 'video/mp4', 'width': 720, 'height': 1280, 'link': 'https://videos.pexels.com/video-files/41/hd.mp4'}], **updates}


class Response(BytesIO):
    def __init__(self, content, *, mime='application/json', length=None):
        super().__init__(content)
        self.status = 200
        self.headers = {'Content-Type': mime, 'Content-Length': str(len(content) if length is None else length)}


def test_catalogue_selects_full_hd_preserves_unknown_hash_and_quarantines_bad_items():
    seen = []
    def opener(request, timeout):
        seen.append(request)
        response = Response(json.dumps({'videos': [row(), None, row(id='invalid'), row(video_files=[{'file_type':'image/jpeg'}]),
            row(video_files=[{'id':45,'width':1080,'height':1920,'file_type':'video/mp4','link':'http://127.0.0.1/x'}])]}).encode())
        response.headers['X-Ratelimit-Remaining'] = '199'
        return response
    provider = PexelsMediaProvider('dummy-key', opener=opener)
    values = provider.search('making coffee')
    assert len(values) == 1
    assert values[0].source_id == '41:43' and values[0].width == 1080 and values[0].height == 1920
    assert values[0].sha1 is None and values[0].size_bytes is None and values[0].duration_ms == 8000
    assert 'orientation=portrait' in seen[0].full_url and seen[0].get_header('Authorization') == 'dummy-key'
    assert 'dummy-key' not in values[0].model_dump_json()
    assert len(provider.last_rejections) == 4 and provider.last_quota == {'remaining':'199'}
    assert '非画面核验' in values[0].description


@pytest.mark.parametrize('query,locale', [('washing hands under running water','en-US'),('流水洗手','zh-CN'),('咖啡 coffee','zh-CN')])
def test_search_locale_matches_query_language_without_changing_account_or_file_identity(query,locale):
    from urllib.parse import parse_qs,urlsplit
    seen=[]
    def opener(request,timeout):
        seen.append(request.full_url)
        return Response(json.dumps({'videos':[row()]}).encode())
    provider=PexelsMediaProvider('dummy-key',opener=opener)
    account=provider.search_scope
    result=provider.search(query)
    assert parse_qs(urlsplit(seen[0]).query)['locale']==[locale]
    assert provider.search_context(query)=={'locale':locale}
    assert provider.search_scope==account and result[0].id==provider._candidate(row()).id
    assert provider.search_context('https://www.pexels.com/zh-cn/video/41/')=={}


def test_rotating_public_url_does_not_change_selected_file_identity():
    provider = PexelsMediaProvider()
    original = provider._candidate(row())
    updated = row()
    updated['video_files'][1]['link'] += '?temporary=changed'
    assert provider._candidate(updated).id == original.id


@pytest.mark.parametrize('url', [
    'https://www.pexels.com/video/making-coffee-41/', 'https://pexels.com/video/41',
    'https://www.pexels.com:443/zh-cn/video/41/',
    'https://www.pexels.com/zh-tw/video/%E5%92%96%E5%95%A1-41/',
])
def test_specific_page_identity_supports_official_numeric_slug_and_localized_urls(url):
    assert page_video_id(url) == 41


@pytest.mark.parametrize('url', [
    'https://www.pexels.com/search/videos/coffee/', 'https://www.pexels.com/video/',
    'https://www.pexels.com/photo/coffee-41/', 'https://www.pexels.com/videos/41/',
    'https://www.pexels.com.evil.example/video/41/', 'http://www.pexels.com/video/41/',
    'https://user:secret@www.pexels.com/video/41/', 'https://www.pexels.com:444/video/41/',
    'https://www.pexels.com/video/41/?id=42', 'https://www.pexels.com/video/41/#preview',
    'https://www.pexels.com/video/%2f41/', 'https://www.pexels.com/video/coffee%2f-41/',
    'https://www.pexels.com/video/0/', 'https://www.pexels.com/video/41/more/',
    'file:///tmp/video/41', '//www.pexels.com/video/41/', 'www.pexels.com/video/41/',
    'https:/www.pexels.com/video/41/',
])
def test_invalid_or_non_detail_url_is_rejected_before_any_api_call(url):
    provider = PexelsMediaProvider('dummy-key', opener=lambda *a, **k: pytest.fail('不应请求'))
    with pytest.raises(ExternalMediaError) as error:
        provider.search(url)
    assert error.value.code == 'pexels_invalid_video_url'


def test_specific_page_uses_get_video_preserves_identity_quota_and_keeps_key_off_media(tmp_path):
    seen = []
    def opener(request, timeout):
        seen.append(request)
        if request.full_url.startswith('https://api.pexels.com/'):
            response = Response(json.dumps(row()).encode())
            response.headers.update({'X-Ratelimit-Limit':'200', 'X-Ratelimit-Remaining':'198', 'X-Ratelimit-Reset':'1800000000'})
            return response
        return Response(VIDEO, mime='video/mp4')
    provider = PexelsMediaProvider('dummy-key', opener=opener)
    result = provider.search('https://www.pexels.com/zh-cn/video/41/')
    assert len(result) == 1 and result[0].id == provider._candidate(row()).id
    assert result[0].source_id == '41:43' and (result[0].width, result[0].height) == (1080,1920)
    assert result[0].sha1 is None and result[0].size_bytes is None
    assert '来源详情定位（非画面核验）' in result[0].description
    assert seen[0].full_url == 'https://api.pexels.com/v1/videos/videos/41'
    assert seen[0].get_header('Authorization') == 'dummy-key'
    assert provider.last_quota == {'limit':'200','remaining':'198','reset':'1800000000'}
    target = tmp_path/'specific.mp4'
    provider.download(result[0], target)
    assert target.read_bytes() == VIDEO and len(seen) == 2
    assert seen[1].get_header('Authorization') is None and seen[1].full_url == result[0].download_url


def test_specific_url_has_its_own_bound_separate_from_keyword_length():
    seen=[]
    def opener(request,timeout):
        seen.append(request)
        return Response(json.dumps(row()).encode())
    provider=PexelsMediaProvider('dummy-key',opener=opener)
    url='https://www.pexels.com/video/'+('coffee-'*40)+'41/'
    assert len(url)>200 and len(provider.search(url))==1
    assert seen[0].full_url=='https://api.pexels.com/v1/videos/videos/41'
    with pytest.raises(ExternalMediaError) as error:
        provider.search('https://www.pexels.com/video/'+('coffee-'*150)+'41/')
    assert error.value.code=='pexels_invalid_video_url' and len(seen)==1


@pytest.mark.parametrize('updates', [
    {'id':42,'url':'https://www.pexels.com/video/42/'},
    {'url':'https://www.pexels.com/video/42/'}, {'id':'41'},
])
def test_get_video_never_admits_another_video_or_mismatched_returned_page(updates):
    seen = []
    def opener(request, timeout):
        seen.append(request)
        return Response(json.dumps(row(**updates)).encode())
    provider = PexelsMediaProvider('dummy-key', opener=opener)
    with pytest.raises(ExternalMediaError) as error:
        provider.search('https://www.pexels.com/video/41/')
    assert error.value.code == 'pexels_video_id_mismatch' and len(seen) == 1


def test_get_video_keeps_project_orientation_without_falling_back_to_topic_search():
    seen = []
    sample = row(video_files=[{'id':50,'file_type':'video/mp4','width':1920,'height':1080,
        'link':'https://videos.pexels.com/video-files/41/wide.mp4'}])
    def opener(request, timeout):
        seen.append(request)
        return Response(json.dumps(sample).encode())
    provider = PexelsMediaProvider('dummy-key', opener=opener)
    assert provider.search('https://www.pexels.com/video/41/') == []
    assert len(seen) == 1 and provider.last_rejections == [{'source_id':'41','reason':'no_matching_hd_version'}]


@pytest.mark.parametrize('status', [302,404,429])
def test_get_video_failure_never_redirects_credentials_retries_or_falls_back(status):
    seen = []
    def opener(request, timeout):
        seen.append(request)
        raise HTTPError(request.full_url,status,'dummy-key',{'Location':'https://example.org/','Retry-After':'60'},None)
    with pytest.raises(ExternalMediaError) as error:
        PexelsMediaProvider('dummy-key', opener=opener).search('https://www.pexels.com/video/41/')
    assert error.value.code == f'pexels_http_{status}' and len(seen) == 1
    assert 'dummy-key' not in str(error.value)


@pytest.mark.parametrize('body', [b'not-json', b'[]', b'{"videos":[]}', b'x'*(4*1024*1024+1)],
    ids=['invalid-json','wrong-envelope','missing-id','oversized'])
def test_get_video_retains_bounded_response_validation(body):
    provider = PexelsMediaProvider('dummy-key', opener=lambda *a, **k:Response(body))
    with pytest.raises(ExternalMediaError):
        provider.search('https://www.pexels.com/video/41/')


def test_api_rate_limit_does_not_retry_or_expose_key():
    seen = []
    def opener(request, timeout):
        seen.append(request)
        raise HTTPError(request.full_url, 429, 'dummy-key', {'Retry-After':'60'}, None)
    with pytest.raises(ExternalMediaError, match='60') as error:
        PexelsMediaProvider('dummy-key', opener=opener).search('coffee')
    assert error.value.code == 'pexels_http_429' and 'dummy-key' not in str(error.value) and len(seen) == 1


@pytest.mark.parametrize('case', ['success', 'html', 'truncated', 'untrusted_redirect', 'partial_response'])
def test_media_download_uses_no_api_key_and_rejects_incomplete_or_nonvideo_files(tmp_path, case):
    seen = []
    def opener(request, timeout):
        seen.append(request)
        if case == 'untrusted_redirect':
            raise HTTPError(request.full_url,302,'redirect',{'Location':'https://example.org/private'},None)
        response = Response(VIDEO, mime='text/html' if case == 'html' else 'video/mp4',
            length=len(VIDEO)+1 if case == 'truncated' else None)
        if case == 'partial_response':
            response.status = 206
        return response
    provider = PexelsMediaProvider('dummy-key', opener=opener)
    destination = tmp_path / 'download'
    if case == 'success':
        provider.download(provider._candidate(row()), destination)
        assert destination.read_bytes() == VIDEO
    else:
        with pytest.raises(ExternalMediaError):
            provider.download(provider._candidate(row()), destination)
        assert not destination.exists()
    assert all(request.get_header('Authorization') is None for request in seen) and len(seen) == 1


class Probe:
    def __init__(self, width=1080, height=1920):
        self.width, self.height = width, height
    def probe(self, path):
        return ProbeMetadata(8000, self.width, self.height, RationalFps(numerator=30, denominator=1), False, {})


@pytest.mark.parametrize('mismatch', [False, True], ids=['import', 'wrong-rendition'])
def test_ordinary_api_selects_configured_source_and_retains_provenance_before_analysis(tmp_path, monkeypatch, mismatch):
    path = tmp_path / 'source.sqlite'
    with Database(path) as db:
        profile = IPProfile(creator_name='来源验证')
        IPProfileRepository(db).create(profile)
        project = Project(ip_profile_id=profile.id, title='咖啡制作', topic='coffee making',
            fps=RationalFps(numerator=30,denominator=1), created_at=datetime.now(timezone.utc))
        ProjectRepository(db).create(project)
    key_file = tmp_path / 'key.txt'
    key_file.write_text('dummy-key',encoding='utf-8')
    monkeypatch.setenv('CONTENT_OS_PEXELS_API_KEY_FILE',str(key_file))
    monkeypatch.setenv('CONTENT_OS_DATA_ROOT',str(tmp_path / 'data'))
    seen = []
    def opener(request, timeout):
        seen.append(request)
        return Response(json.dumps({'videos':[row()]}).encode()) if 'api.pexels.com' in request.full_url else Response(VIDEO,mime='video/mp4')
    monkeypatch.setattr('app.external_media_connection.PexelsMediaProvider',lambda key=None,**kwargs: PexelsMediaProvider(key,opener=opener,**kwargs))
    application = create_app(path,media_importer_factory=lambda db,root: MediaImporter(db,root,Probe(720,1280) if mismatch else Probe()))
    with TestClient(application) as client:
        base = f'/projects/{project.id}/external-media'
        assert client.get(base+'/providers').json()[0] == {'id':'pexels','label':'Pexels','configured':True}
        request = {'query':'coffee making','provider':'pexels','idempotency_key':'search'}
        search = client.post(base+'/search',json=request)
        assert search.status_code == 200, search.text
        assert client.post(base+'/search',json=request).json() == search.json()
        assert len(seen) == 1
        result = search.json()
        payload = {'search_call_id':result['call_id'],'candidate_id':result['candidates'][0]['id'],'idempotency_key':'import'}
        # 已取得的准入快照和公开下载不依赖再次索要 API Key。
        key_file.unlink()
        response = client.post(base+'/imports',json=payload)
        if mismatch:
            assert response.status_code == 422 and '尺寸' in response.text
            assert client.get('/assets').json() == []
        else:
            assert response.status_code == 201, response.text
            assert client.post(base+'/imports',json={**payload,'idempotency_key':'again'}).json() == response.json()
            asset = client.get('/assets/'+response.json()['asset_id']).json()
            source = asset['metadata']['external_sources'][0]
            assert source['candidate']['sha1'] is None and source['quality_state'] == 'unassessed'
            assert source['content_hash'] == asset['content_hash'] and source['content_nature'] == 'unknown'
            assert source['version_verification'] == 'provider_file_id+probe+local_sha256'
            assert client.get('/jobs/'+response.json()['analysis_job_id']).json()['status'] == 'pending'
            assert client.get(base+'/searches').json()[0] == result
        assert len(seen) == 2

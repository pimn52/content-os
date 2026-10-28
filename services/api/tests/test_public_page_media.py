from io import BytesIO
import json
from urllib.error import HTTPError

import pytest

from app.providers.public_page_media import MAX_HTML_BYTES, PublicPageMediaError, PublicPageMediaProvider


COVERR = "https://coverr.co/videos/pouring-coffee-into-a-mug-fuenlxepfn"
HUBBLE = "https://esahubble.org/videos/heic1507a/"
FILE = "https://cdn.coverr.co/videos/coverr-pouring-coffee-into-a-mug-1176/1080p.mp4"


class Response(BytesIO):
    def __init__(self, data, *, mime="text/html", status=200, length=None):
        super().__init__(data)
        self.status = status
        self.headers = {"Content-Type":mime, "Content-Length":str(len(data) if length is None else length)}


def coverr_html(*, media_url=FILE, width=2048, height=1080, plus=False, premium=False):
    schema = {"@type":"VideoObject", "name":"Pouring coffee into a mug", "duration":"PT7S",
        "contentUrl":media_url, "description":"Coffee pouring"}
    record = {"slug":"pouring-coffee-into-a-mug-fuenlxepfn", "title":schema["name"], "duration":"6.589917",
        "isPremium":premium, "contributor":None, "urls":{"mp4Preview":"https://cdn.coverr.co/preview.mp4"},
        "defaultVariant":{"renditions":[{"id":"actual-version", "url":media_url,
            "width":width, "height":height, "isPlus":plus, "size":3702378}]}}
    unrelated = {"slug":"another-video", "title":"Another", "defaultVariant":{"renditions":[
        {"url":"https://cdn.coverr.co/videos/other/1080p.mp4", "width":1080,"height":1920,"isPlus":False}]}}
    return '<script type="application/ld+json">'+json.dumps(schema)+'</script><script id="ng-state" type="application/json">'+json.dumps({"detail":record,"recommendations":[unrelated]})+'</script>'


def hubble_html(**updates):
    schema = {"@type":"VideoObject", "name":"Galaxies", "contentUrl":"https://cdn.esahubble.org/archives/videos/hd/heic1507a.mp4",
        "width":1920, "height":1080, "duration":"PT40S", "author":[{"name":"NASA"},{"name":"ESA"},{"name":"W. Keel"}], **updates}
    return '<script type="application/ld+json">'+json.dumps(schema)+'</script>'


def test_coverr_source_admission_is_checked_before_any_network_or_download(tmp_path):
    provider = PublicPageMediaProvider(opener=lambda *a,**k:pytest.fail("不应请求"), orientation="any")
    with pytest.raises(PublicPageMediaError) as error:
        provider.search(COVERR)
    assert error.value.code == "public_page_source_not_admitted"
    candidate = provider.parse_html(COVERR, coverr_html())[0]
    assert not candidate.admissible and candidate.width == 2048 and candidate.sha1 is None
    with pytest.raises(PublicPageMediaError) as error:
        provider.download(candidate, tmp_path/'video')
    assert error.value.code == "public_page_source_not_admitted" and not (tmp_path/'video').exists()


def test_bound_structured_version_not_preview_recommendation_or_filename_guess():
    seen=[]
    def opener(request, timeout):
        seen.append((request,timeout))
        return Response(coverr_html().encode())
    provider=PublicPageMediaProvider(coverr_admitted=True,orientation="any",opener=opener)
    candidate=provider.search(COVERR)[0]
    assert candidate.download_url == FILE and (candidate.width,candidate.height)==(2048,1080)
    assert candidate.duration_ms==6590 and candidate.size_bytes==3702378 and candidate.admissible
    assert "未公开作者" in candidate.author and candidate.provider=="public_page"
    assert seen[0][0].get_header("Authorization") is None and 0<seen[0][1]<=30


def test_hubble_explicit_metadata_and_credit_can_be_parsed_without_key():
    provider=PublicPageMediaProvider(orientation="landscape",opener=lambda *a,**k:Response(hubble_html().encode()))
    candidate=provider.search(HUBBLE)[0]
    assert candidate.author=="NASA, ESA, W. Keel" and candidate.duration_ms==40000
    assert candidate.admissible and candidate.license_url=="https://esahubble.org/copyright/"
    assert candidate.size_bytes is None and candidate.sha1 is None


def test_hubble_player_geometry_is_not_promoted_to_encoded_file_geometry():
    html='<title>Galaxies</title><a href="https://cdn.esahubble.org/archives/videos/large_qt/heic1507a.mov" rel="shadowbox;width=720;height=405">Large QT</a>'
    with pytest.raises(PublicPageMediaError) as error:
        PublicPageMediaProvider(orientation="any").parse_html(HUBBLE,html)
    assert error.value.code=="public_page_missing_video_metadata"


@pytest.mark.parametrize("updates,code", [
    ({"width":None},"missing_dimensions"), ({"width":True},"missing_dimensions"),
    ({"duration":"bad"},"missing_duration"), ({"author":None},"missing_credit"),
    ({"contentUrl":"https://evil.test/shot.mp4"},"unsupported_media"),
    ({"contentUrl":"https://cdn.esahubble.org/archives/videos/preview/shot.mp4"},"unsupported_media"),
    ({"contentUrl":"https://cdn.esahubble.org/archives/videos/hd/unrelated.mp4"},"unbound_version"),
])
def test_missing_or_untrusted_original_metadata_is_explicit(updates,code):
    provider=PublicPageMediaProvider(orientation="any")
    with pytest.raises(PublicPageMediaError) as error:
        provider.parse_html(HUBBLE,hubble_html(**updates))
    assert error.value.code=="public_page_"+code


@pytest.mark.parametrize("updates", [{"plus":True},{"premium":True}])
def test_coverr_premium_version_is_not_mistaken_for_public_download(updates):
    with pytest.raises(PublicPageMediaError) as error:
        PublicPageMediaProvider(coverr_admitted=True,orientation="any").parse_html(COVERR,coverr_html(**updates))
    assert error.value.code=="public_page_premium_version"


def test_portrait_preference_does_not_crop_landscape_original():
    with pytest.raises(PublicPageMediaError) as error:
        PublicPageMediaProvider(coverr_admitted=True).parse_html(COVERR,coverr_html())
    assert error.value.code=="public_page_orientation_mismatch"


@pytest.mark.parametrize("url", ["https://coverr.co/stock-video-footage/coffee", "https://esahubble.org/videos/archive/",
    "https://coverr.co.evil.test/videos/a", "https://user:secret@coverr.co/videos/a", "http://coverr.co/videos/a"])
def test_unapproved_page_identity_is_rejected_before_network(url):
    provider=PublicPageMediaProvider(coverr_admitted=True,opener=lambda *a,**k:pytest.fail("不应请求"))
    with pytest.raises(PublicPageMediaError) as error:
        provider.search(url)
    assert error.value.code=="public_page_unsupported_source"


def test_html_limit_and_redirect_validation_are_bounded():
    provider=PublicPageMediaProvider(opener=lambda *a,**k:Response(b'x'*(MAX_HTML_BYTES+1)))
    with pytest.raises(PublicPageMediaError) as error:
        provider.search(HUBBLE)
    assert error.value.code=="public_page_html_too_large"
    attempts=[]
    def redirect(request,timeout):
        attempts.append(request.full_url)
        raise HTTPError(request.full_url,302,"redirect",{"Location":"https://evil.test/videos/a"},None)
    with pytest.raises(PublicPageMediaError) as error:
        PublicPageMediaProvider(opener=redirect).search(HUBBLE)
    assert error.value.code=="public_page_unsupported_source" and len(attempts)==1


@pytest.mark.parametrize("mode", ["success","html","partial","truncated","oversized"])
def test_download_uses_exact_original_and_cleans_only_new_partial_file(tmp_path,mode):
    provider=PublicPageMediaProvider(orientation="landscape")
    candidate=provider.parse_html(HUBBLE,hubble_html())[0]
    data=b'media-original'
    seen=[]
    def opener(request,timeout):
        seen.append(request)
        return Response(data,mime="text/html" if mode=="html" else "video/mp4",status=206 if mode=="partial" else 200,
            length=len(data)+1 if mode=="truncated" else (129*1024*1024 if mode=="oversized" else None))
    provider._opener=opener
    target=tmp_path/'media.mp4'
    if mode=="success":
        provider.download(candidate,target)
        assert target.read_bytes()==data
    else:
        with pytest.raises(PublicPageMediaError):provider.download(candidate,target)
        assert not target.exists()
    assert len(seen)==1 and seen[0].full_url==candidate.download_url
    assert seen[0].get_header("Authorization") is None


def test_rotating_media_query_does_not_change_identity_and_existing_file_is_preserved(tmp_path):
    provider=PublicPageMediaProvider(orientation="landscape")
    a=provider.parse_html(HUBBLE,hubble_html())[0]
    b=provider.parse_html(HUBBLE,hubble_html(contentUrl=a.download_url+'?token=rotated'))[0]
    assert a.id==b.id and a.source_id==b.source_id
    target=tmp_path/'media.mp4';target.write_bytes(b'keep')
    provider._opener=lambda *a,**k:Response(b'new',mime="video/mp4")
    with pytest.raises(PublicPageMediaError):provider.download(a,target)
    assert target.read_bytes()==b'keep'


def test_same_url_with_changed_source_version_is_not_reused():
    provider=PublicPageMediaProvider(coverr_admitted=True,orientation="any")
    html=coverr_html()
    first=provider.parse_html(COVERR,html)[0]
    updated=provider.parse_html(COVERR,html.replace('actual-version','updated-version'))[0]
    assert first.id!=updated.id and first.source_id!=updated.source_id


def test_complete_response_with_changed_catalogue_size_is_not_imported(tmp_path):
    provider=PublicPageMediaProvider(coverr_admitted=True,orientation="any")
    candidate=provider.parse_html(COVERR,coverr_html())[0]
    provider._opener=lambda *a,**k:Response(b'changed',mime="video/mp4")
    target=tmp_path/'changed.mp4'
    with pytest.raises(PublicPageMediaError) as error:provider.download(candidate,target)
    assert error.value.code=="public_page_version_changed" and not target.exists()


PUBLIC = "https://open-films.example/videos/coffee-making"
PUBLIC_FILE = "https://media.example/originals/coffee-making.mp4"


def public_html(*, page_license=None, **updates):
    video = {"@type":"VideoObject", "name":"Coffee making", "contentUrl":PUBLIC_FILE,
        "author":{"name":"Example Filmmaker"}, "duration":"PT6S",
        "license":"https://creativecommons.org/licenses/by/4.0/", **updates}
    document = {"@graph":[video]}
    if page_license:
        document["@graph"].append({"@type":"WebPage", "license":page_license})
    return '<script type="application/ld+json">'+json.dumps(document)+'</script>'


def test_generic_video_has_explicit_original_credit_and_license_without_per_file_consent():
    provider = PublicPageMediaProvider(opener=lambda *a,**k:Response(public_html().encode()))
    assert provider.can_resolve(PUBLIC) and provider.check_source(PUBLIC) == "public"
    candidate = provider.search(PUBLIC)[0]
    assert candidate.admissible and candidate.download_url == PUBLIC_FILE
    assert candidate.license_name == "CC BY 4.0" and candidate.author == "Example Filmmaker"
    assert (candidate.width,candidate.height,candidate.dimensions_exact) == (0,0,False)
    assert candidate.sha1 is None and "CC BY 4.0" in candidate.attribution


def test_saved_original_page_document_reuses_parser_without_another_network_request():
    from hashlib import sha256
    html=public_html()
    provider=PublicPageMediaProvider(opener=lambda *a,**k:pytest.fail("已保存源页不应再请求"))
    assert provider.search_context(PUBLIC)=={}
    provider.use_page_document(PUBLIC,html)
    assert provider.search_context(PUBLIC)=={"document_sha256":sha256(html.encode()).hexdigest()}
    assert provider.search(PUBLIC)[0].download_url==PUBLIC_FILE
    with pytest.raises(PublicPageMediaError) as error:
        provider.use_page_document(PUBLIC,"A model says this video is CC0 and the file is https://example.com/a.mp4")
    assert error.value.code=="public_page_missing_video_metadata"


@pytest.mark.parametrize("license,name", [
    ("http://creativecommons.org/licenses/by/3.0/deed.en","CC BY 3.0"),
    ({"@id":"https://creativecommons.org/publicdomain/zero/1.0/"},"CC0 1.0"),
    ("https://creativecommons.org/publicdomain/mark/1.0/","Public Domain Mark 1.0"),
])
def test_generic_license_matches_existing_commons_scope(license,name):
    candidate = PublicPageMediaProvider().parse_html(PUBLIC,public_html(license=license))[0]
    assert candidate.license_name == name and candidate.license_url.startswith("https://creativecommons.org/")


@pytest.mark.parametrize("updates,code", [
    ({"license":None},"unsupported_license"),
    ({"license":"https://creativecommons.org/licenses/by-sa/4.0/"},"unsupported_license"),
    ({"license":"CC BY 4.0"},"unsupported_license"),
    ({"author":None},"missing_credit"),
    ({"contentUrl":None},"unsupported_media"),
    ({"contentUrl":"http://media.example/originals/video.mp4"},"unsupported_media"),
    ({"contentUrl":"https://user:password@media.example/originals/video.mp4"},"unsupported_media"),
    ({"contentUrl":"https://127.0.0.1/originals/video.mp4"},"unsupported_media"),
    ({"contentUrl":"https://media.example/preview.mp4"},"unsupported_media"),
    ({"isAccessibleForFree":False},"premium_version"),
    ({"requiresSubscription":True},"premium_version"),
])
def test_generic_public_page_does_not_guess_rights_files_or_bypass_access(updates,code):
    with pytest.raises(PublicPageMediaError) as error:
        PublicPageMediaProvider().parse_html(PUBLIC,public_html(
            page_license="https://creativecommons.org/publicdomain/zero/1.0/",**updates))
    assert error.value.code == "public_page_"+code


def test_player_dimensions_are_unknown_until_probed_and_only_matching_encoding_is_exact():
    provider = PublicPageMediaProvider()
    candidate = provider.parse_html(PUBLIC,public_html(width=1920,height=1080))[0]
    assert not candidate.dimensions_exact and candidate.width == candidate.height == 0
    encoding = [{"contentUrl":"https://media.example/other.mp4", "width":1920,"height":1080},
        {"contentUrl":PUBLIC_FILE,"width":1080,"height":1920,"contentSize":"2000"}]
    candidate = provider.parse_html(PUBLIC,public_html(encoding=encoding))[0]
    assert candidate.dimensions_exact and (candidate.width,candidate.height,candidate.size_bytes)==(1080,1920,2000)
    with pytest.raises(PublicPageMediaError) as error:
        provider.parse_html(PUBLIC,public_html(encoding={"contentUrl":PUBLIC_FILE,"width":1280,"height":720}))
    assert error.value.code == "public_page_orientation_mismatch"


@pytest.mark.parametrize("url", ["http://example.com/video/a", "https://127.0.0.1/video/a",
    "https://192.168.1.2/video/a", "https://[::1]/video/a", "https://localhost/video/a",
    "https://user:password@example.com/video/a", "https://example.com/", "https://example.com/search",
    "https://example.com:8443/video/a", "https://example.com/video/a\\other"])
def test_generic_candidate_source_check_rejects_unsafe_or_non_specific_urls(url):
    provider = PublicPageMediaProvider(opener=lambda *a,**k:pytest.fail("不应请求"))
    assert not provider.can_resolve(url)
    with pytest.raises(PublicPageMediaError):
        provider.search(url)


def test_generic_redirect_to_private_url_stops_before_followup_request():
    requests = []
    def opener(request,timeout):
        requests.append(request.full_url)
        raise HTTPError(request.full_url,302,"redirect",{"Location":"https://127.0.0.1/video/a"},None)
    with pytest.raises(PublicPageMediaError):
        PublicPageMediaProvider(opener=opener).search(PUBLIC)
    assert requests == [PUBLIC]


@pytest.mark.parametrize("addresses", [["127.0.0.1"],["10.1.2.3"],["93.184.216.34","127.0.0.1"],["169.254.169.254"]])
def test_public_http_rejects_private_dns_even_when_mixed_with_public(monkeypatch,addresses):
    import socket
    from app.providers import public_media_http
    monkeypatch.setattr(public_media_http.socket,"getaddrinfo",lambda *a,**k:[
        (socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,"",(address,443)) for address in addresses])
    monkeypatch.setattr(public_media_http.socket,"socket",lambda *a,**k:pytest.fail("不能连接私网 DNS 结果"))
    with pytest.raises(PublicPageMediaError) as error:
        PublicPageMediaProvider().search(PUBLIC)
    assert error.value.code == "public_page_unsafe_url"


def test_public_https_connection_pins_checked_ip_and_verifies_original_tls_hostname(monkeypatch):
    import socket
    from app.providers import public_media_http
    calls=[]
    class Socket:
        def settimeout(self,value):calls.append(("timeout",value))
        def connect(self,address):calls.append(("connect",address))
        def do_handshake(self):calls.append(("handshake",))
        def close(self):calls.append(("close",))
    class Context:
        def wrap_socket(self,sock,*,server_hostname,do_handshake_on_connect):
            assert do_handshake_on_connect is False
            calls.append(("tls",server_hostname))
            return sock
    monkeypatch.setattr(public_media_http.socket,"socket",lambda *a:Socket())
    monkeypatch.setattr(public_media_http.socket,"getaddrinfo",lambda *a,**k:pytest.fail("连接时不能再次解析 DNS"))
    monkeypatch.setattr(public_media_http.ssl,"create_default_context",lambda:Context())
    checked=(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,"",("93.184.216.34",443))
    connection=public_media_http._PinnedHTTPSConnection("open-films.example",checked,5)
    connection.connect()
    assert ("connect",("93.184.216.34",443)) in calls and ("tls","open-films.example") in calls
    connection.close()


def test_public_http_has_a_total_deadline_while_waiting_for_response_headers(monkeypatch):
    from threading import Event
    from urllib.request import Request
    from app.providers import public_media_http
    expired=Event()
    class Connection:
        def __init__(self,*a):pass
        def request(self,*a,**k):pass
        def getresponse(self):
            assert expired.wait(.8),"请求总时限必须中断等候响应头"
            raise TimeoutError("deadline")
        def expire(self):expired.set()
        def close(self):pass
    monkeypatch.setattr(public_media_http,"_resolve_public",lambda *a:())
    monkeypatch.setattr(public_media_http,"_PinnedHTTPSConnection",Connection)
    with pytest.raises(TimeoutError):
        public_media_http.open_public_https(Request(PUBLIC),timeout=.02)
    assert expired.is_set()


def test_public_http_dns_wait_is_bounded_and_cancels_unstarted_resolution(monkeypatch):
    from app.providers import public_media_http
    cancelled=[]
    class Future:
        def result(self,timeout):
            assert timeout==.1
            raise public_media_http.DNSDeadline()
        def cancel(self):cancelled.append(True)
    class Pool:
        def submit(self,*a,**k):return Future()
    monkeypatch.setattr(public_media_http,"_DNS_POOL",Pool())
    with pytest.raises(TimeoutError,match="DNS"):
        public_media_http._resolve_public("open-films.example",.1)
    assert cancelled==[True]


@pytest.mark.parametrize("width,height,accepted", [(1080,1920,True),(1920,1080,False),(480,854,False)])
def test_generic_download_unknown_geometry_is_verified_before_durable_import(tmp_path,width,height,accepted):
    from datetime import datetime,timezone
    from hashlib import sha256
    from app.db import AssetRepository,Database,IPProfileRepository,ProjectRepository
    from app.domain.models import IPProfile,Project,RationalFps,SourceKind
    from app.external_media import ExternalMediaInputError,ExternalMediaService
    from app.media import MediaImporter,ProbeMetadata
    video=b"fixture-original-video"
    requests=[]
    def opener(request,timeout):
        requests.append(request.full_url)
        return Response(public_html().encode()) if request.full_url==PUBLIC else Response(video,mime="video/mp4")
    class Probe:
        def probe(self,path):
            assert path.read_bytes()==video
            return ProbeMetadata(6000,width,height,RationalFps(numerator=30,denominator=1),False,{})
    provider=PublicPageMediaProvider(opener=opener)
    with Database(tmp_path/"public.sqlite") as db:
        profile=IPProfile(creator_name="公开素材夹具")
        IPProfileRepository(db).create(profile)
        project=Project(ip_profile_id=profile.id,title="咖啡制作",topic="咖啡制作",
            fps=RationalFps(numerator=30,denominator=1),created_at=datetime.now(timezone.utc))
        ProjectRepository(db).create(project)
        service=ExternalMediaService(db,tmp_path,provider,MediaImporter(db,tmp_path,Probe()))
        found=service.search(project.id,PUBLIC,"fixture-search")
        if not accepted:
            with pytest.raises(ExternalMediaInputError,match="实际画幅或分辨率"):
                service.import_video(project.id,found.call_id,found.candidates[0].id,"fixture-import")
            assert AssetRepository(db).list()==[]
            return
        result=service.import_video(project.id,found.call_id,found.candidates[0].id,"fixture-import")
        asset=AssetRepository(db).get(result.asset_id)
        assert asset.source_kind==SourceKind.STOCK and asset.content_hash==sha256(video).hexdigest()
        record=asset.metadata["external_sources"][0]
        assert record["candidate"]["license_name"]=="CC BY 4.0"
        assert record["candidate"]["dimensions_exact"] is False
        assert record["downloaded_dimensions"]=={"width":width,"height":height}
        assert record["content_hash"]==asset.content_hash and record["quality_state"]=="unassessed"
        again=service.import_video(project.id,found.call_id,found.candidates[0].id,"fixture-import-again")
        assert again.asset_id==asset.id and requests==[PUBLIC,PUBLIC_FILE]

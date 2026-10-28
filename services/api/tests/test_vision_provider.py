from __future__ import annotations

import base64
import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from app.providers.vision import (
    ClipVisualAnalysis,
    OpenAICompatibleVisionProvider,
    VisionAuthenticationError,
    VisionConfigurationError,
    VisionConnectionError,
    VisionHTTPError,
    VisionInputError,
    VisionProviderResponseError,
    VisionRateLimitError,
    VisionTimeout,
    UrllibVisionTransport,
)


def _analysis() -> dict[str, object]:
    return {
        "visual_description": "A person works at a desk beside a laptop.",
        "people": ["person seated at a desk"],
        "objects": ["laptop", "desk"],
        "location": "office", "action": "working at a laptop", "shot_type": "medium shot",
        "quality_score": 0.8, "face_visibility": 0.7, "mouth_visibility": 0.6,
        "talking_candidate": True,
    }


def _response(analysis: dict[str, object] | None = None) -> dict[str, object]:
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(analysis or _analysis())}]}]}


def test_source_layout_and_subtitle_bounds_survive_structured_output():
    from app.providers.vision import _parse_response
    value={**_analysis(),'comparison_layout':'side_by_side_equal_halves','subtitle_regions':[[.3,.88,.4,.08]]}
    observed=_parse_response(json.dumps(_response(value)).encode())
    assert observed.comparison_layout=='side_by_side_equal_halves' and observed.subtitle_regions==[[.3,.88,.4,.08]]
    for regions in [[[.3,.88,.8,.08]],[[0,float('nan'),.1,.1]],[[0,.9,.1,-.2]]]:
        with pytest.raises(VisionProviderResponseError):_parse_response(json.dumps(_response({**value,'subtitle_regions':regions})).encode())


@contextmanager
def _fake_server(
    status: int,
    payload: object,
    response_headers: dict[str, str] | None = None,
) -> Iterator[tuple[str, list[tuple[str, dict[str, str], bytes]]]]:
    requests: list[tuple[str, dict[str, str], bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, dict(self.headers.items()), body))
            response = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            for name, value in (response_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _jpeg(tmp_path: Path) -> Path:
    path = tmp_path / "关键帧.JPG"
    path.write_bytes(b"\xff\xd8fake-jpeg-bytes\xff\xd9")
    return path


def _interval_result(clip_id='clip-one',scene_key='scene-one'):
    return dict(clip_id=clip_id,description='实际视频内持续存在的动作',content_consistent=True,
        comparison_layout='none',subtitle_regions=[[0,.9,1,.08]],protected_regions=[[.1,.1,.5,.5]],
        matches=[dict(scene_key=scene_key,supported=True,score=.9,reason='画面支持用途')],reason='测试响应')


def test_chat_video_batch_uploads_actual_mp4_and_preserves_interval_observations(tmp_path):
    path=tmp_path/'bounded.mp4';path.write_bytes(b'\x00\x00\x00\x18ftypisom contract fixture')
    result={'clips':[_interval_result()]}
    response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result)}}]}
    with _fake_server(200,response) as (url,requests):
        observed=OpenAICompatibleVisionProvider('fixture-key',base_url=url,protocol='chat_completions',
            video_input=True,thinking_type='disabled').analyze_intervals(
                [{'clip_id':'clip-one','path':str(path),'start_ms':5000,'end_ms':9000}],
                [{'scene_key':'scene-one','voice_text':'当前旁白'}])
    assert requests[0][0]=='/v1/chat/completions' and len(requests)==1
    body=json.loads(requests[0][2]); content=body['messages'][0]['content']
    video=next(item for item in content if item['type']=='video_url')['video_url']['url']
    assert base64.b64decode(video.split(',',1)[1])==path.read_bytes()
    assert body['thinking']=={'type':'disabled'}
    prompt=content[0]['text']
    assert '{"left":0.1,"top":0.2,"right":0.8,"bottom":0.9}' in prompt
    assert '无法可靠定位时填 null' in prompt
    assert 'TalkingMotionInterval' not in prompt  # 普通用途核验不承担动作专用协议的额外输入。
    assert observed.clips[0].regions_xywh('subtitle_regions')[0] == pytest.approx([0,.9,1,.08])
    assert observed.clips[0].matches[0].supported is True


def test_video_catalogue_observes_source_without_approving_a_future_script(tmp_path):
    path=tmp_path/'bounded.mp4';path.write_bytes(b'\x00\x00\x00\x18ftypisom fixture')
    item=_interval_result();item['matches']=[]
    response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'clips':[item]})}}]}
    with _fake_server(200,response) as (url,requests):
        provider=OpenAICompatibleVisionProvider('fixture-secret',base_url=url,protocol='chat_completions',video_input=True)
        result=provider.analyze_intervals([{'clip_id':'clip-one','path':str(path)}],[])
    assert result.clips[0].content_consistent and result.clips[0].matches==[] and len(requests)==1
    prompt=json.loads(requests[0][2])['messages'][0]['content'][0]['text']
    assert '没有待匹配脚本' in prompt


def test_motion_reference_uses_actual_video_and_explicit_continuous_subinterval_facts(tmp_path):
    path=tmp_path/'motion.mp4';path.write_bytes(b'\x00\x00\x00\x18ftypisom fixture')
    item=_interval_result();item['matches']=[]
    item['talking_intervals']=[dict(start_offset_ms=2000,end_offset_ms=12000,
        single_person_continuous=True,motion_continuous=True,head_clearance=True,
        face_visibility=.9,mouth_visibility=.85,quality_score=.8,burned_in_subtitles=False,
        start_gaze='camera',end_gaze='away',expression_labels=['平静'],reason='全区间单人完整可见')]
    response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'clips':[item]})}}]}
    with _fake_server(200,response) as (url,requests):
        provider=OpenAICompatibleVisionProvider('fixture-secret',base_url=url,protocol='chat_completions',video_input=True)
        result=provider.analyze_intervals([{'clip_id':'clip-one','path':str(path),
            'start_ms':10000,'end_ms':34000,'talking_motion_reference':True}],[])
    assert len(requests)==1
    motion=result.clips[0].talking_intervals[0]
    assert (motion.start_offset_ms,motion.end_offset_ms)==(2000,12000)
    content=json.loads(requests[0][2])['messages'][0]['content']
    assert any(part['type']=='video_url' for part in content)
    assert '不能只检查首尾或单帧' in content[0]['text']
    assert '不要求连续流利说话' in content[0]['text']


@pytest.mark.parametrize('bad', ['wrong_clip','duplicate','wrong_scene','truncated'])
def test_video_batch_rejects_changed_scope_and_incomplete_result_without_retry(tmp_path,bad):
    path=tmp_path/'bounded.mp4';path.write_bytes(b'\x00\x00\x00\x18ftypisom fixture')
    result={'clips':[_interval_result()]}
    if bad=='wrong_clip':result['clips'][0]['clip_id']='unknown'
    if bad=='duplicate':result['clips'].append(_interval_result())
    if bad=='wrong_scene':result['clips'][0]['matches'][0]['scene_key']='unknown'
    response={'choices':[{'finish_reason':'length' if bad=='truncated' else 'stop','message':{'content':json.dumps(result)}}]}
    with _fake_server(200,response) as (url,requests):
        provider=OpenAICompatibleVisionProvider('private-fixture-key',base_url=url,protocol='chat_completions',video_input=True)
        with pytest.raises(VisionProviderResponseError) as failure:
            provider.analyze_intervals([{'clip_id':'clip-one','path':str(path)}],[{'scene_key':'scene-one'}])
        assert 'private-fixture-key' not in str(failure.value) and len(requests)==1


@pytest.mark.parametrize('bad_geometry',[
    {'left':.3,'top':.2,'right':.6,'top_bottom':.9},
    {'left':.65,'top':.05,'right':.8,'0.120':.12},
    {'left':.5,'top':.9,'right':.4,'bottom':.95},
])
def test_invalid_candidate_is_isolated_without_losing_good_video_results_or_guessing_coordinates(tmp_path,bad_geometry):
    path=tmp_path/'bounded.mp4';path.write_bytes(b'\x00\x00\x00\x18ftypisom fixture')
    bad=_interval_result('clip-bad');bad['protected_regions']=[bad_geometry]
    response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'clips':[_interval_result(),bad]})}}]}
    with _fake_server(200,response) as (url,requests):
        provider=OpenAICompatibleVisionProvider('fixture-secret',base_url=url,protocol='chat_completions',video_input=True)
        result=provider.analyze_intervals([{'clip_id':clip_id,'path':str(path)} for clip_id in ['clip-one','clip-bad']],
            [{'scene_key':'scene-one'}])
    assert len(requests)==1 and result.clips[0].content_consistent and result.clips[0].matches[0].supported
    rejected=result.clips[1]
    assert not rejected.content_consistent and not rejected.matches and rejected.protected_regions is None
    assert provider.last_validation_issues[0]['fields']==['protected_regions']
    assert provider.last_validation_issues[0]['raw_output']['protected_regions']==[bad_geometry]


def test_chat_single_frame_still_uses_image_contract(tmp_path):
    response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(_analysis())}}]}
    with _fake_server(200,response) as (url,requests):
        observed=OpenAICompatibleVisionProvider('fixture-key',base_url=url,protocol='chat_completions').analyze(_jpeg(tmp_path))
    assert observed.objects==('laptop','desk')
    assert any(p['type']=='image_url' for p in json.loads(requests[0][2])['messages'][0]['content'])


def test_named_video_regions_remove_ambiguity_and_legacy_overflow_never_shrinks_picture_protection():
    from app.providers.vision import NormalizedVideoRegion, VideoIntervalBatch
    region=NormalizedVideoRegion(left=.2,top=.8,right=.7,bottom=.9)
    assert region.xywh()==pytest.approx([.2,.8,.5,.1])
    # 真实响应曾给出疑似 XYXY 的数组。旧 WH 与画面求交会覆盖两种解释，不猜测并缩小保护范围。
    for raw in [[.277,.916,.445,.964],[.529,.028,.715,.066],[.029,.028,.215,.066]]:
        box=NormalizedVideoRegion.model_validate(raw)
        assert box.left==raw[0] and box.top==raw[1]
        assert box.right>=raw[2] and box.bottom>=raw[3]
        assert box.right==min(1,raw[0]+raw[2]) and box.bottom==min(1,raw[1]+raw[3])
    schema=VideoIntervalBatch.model_json_schema()
    assert set(schema['$defs']['NormalizedVideoRegion']['properties'])=={'left','top','right','bottom'}


def test_openai_compatible_vision_request_uses_schema_and_local_jpeg(tmp_path: Path) -> None:
    image = _jpeg(tmp_path)
    with _fake_server(200, _response()) as (base_url, requests):
        result = OpenAICompatibleVisionProvider("  runtime-secret  ", base_url=base_url).analyze(image)

    assert isinstance(result, ClipVisualAnalysis)
    assert result.people == ("person seated at a desk",)
    assert result.objects == ("laptop", "desk")
    assert result.talking_candidate is True
    path, headers, body = requests[0]
    request = json.loads(body)
    assert path == "/v1/responses"
    assert headers["Authorization"] == "Bearer runtime-secret"
    assert request["model"] == "gpt-4.1-mini"
    assert request["store"] is False
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["schema"]["additionalProperties"] is False
    content = request["input"][0]["content"]
    assert content[0]["type"] == "input_text"
    assert "Do not identify" in content[0]["text"]
    assert content[1]["type"] == "input_image" and content[1]["detail"] == "low"
    prefix, encoded = content[1]["image_url"].split(",", 1)
    assert prefix == "data:image/jpeg;base64"
    assert base64.b64decode(encoded) == image.read_bytes()


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(401, VisionAuthenticationError), (429, VisionRateLimitError), (500, VisionHTTPError)],
)
def test_vision_errors_are_classified_without_key_leakage(
    tmp_path: Path, status: int, error_type: type[Exception]
) -> None:
    key = "vision-key-must-not-leak"
    with _fake_server(status, {"error": {"message": key}}) as (base_url, _):
        with pytest.raises(error_type) as raised:
            OpenAICompatibleVisionProvider(key, base_url=base_url).analyze(_jpeg(tmp_path))
    assert key not in str(raised.value)
    if status == 500:
        assert isinstance(raised.value, VisionHTTPError)
        assert raised.value.status_code == 500


def test_vision_does_not_follow_redirect_with_byok_header(tmp_path: Path) -> None:
    with _fake_server(200, _response()) as (target_url, target_requests):
        with _fake_server(302, {"redirect": "blocked"}, {"Location": f"{target_url}/responses"}) as (source_url, source_requests):
            with pytest.raises(VisionHTTPError, match="302"):
                OpenAICompatibleVisionProvider("redirect-secret", base_url=source_url).analyze(_jpeg(tmp_path))
    assert len(source_requests) == 1
    assert target_requests == []


def test_vision_rejects_nonlocal_or_invalid_jpeg_and_malformed_response(tmp_path: Path) -> None:
    with pytest.raises(VisionInputError, match="does not exist"):
        OpenAICompatibleVisionProvider("key").analyze("https://example.test/image.jpg")
    png = tmp_path / "image.png"
    png.write_bytes(b"not image")
    with pytest.raises(VisionInputError, match="JPEG"):
        OpenAICompatibleVisionProvider("key").analyze(png)
    broken = tmp_path / "image.jpg"
    broken.write_bytes(b"not a jpeg")
    with pytest.raises(VisionInputError, match="valid JPEG"):
        OpenAICompatibleVisionProvider("key").analyze(broken)
    invalid = _analysis()
    invalid["quality_score"] = 1.1
    with _fake_server(200, _response(invalid)) as (base_url, _):
        with pytest.raises(VisionProviderResponseError, match="invalid visual"):
            OpenAICompatibleVisionProvider("key", base_url=base_url).analyze(_jpeg(tmp_path))


def test_vision_configuration_and_injected_timeout_are_safe(tmp_path: Path) -> None:
    with pytest.raises(VisionConfigurationError, match="localhost"):
        OpenAICompatibleVisionProvider("key", base_url="http://remote.example/v1")
    with pytest.raises(VisionConfigurationError, match="detail"):
        OpenAICompatibleVisionProvider("key", detail="full")

    class TimeoutTransport:
        def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> object:
            raise TimeoutError("secret transport detail")

    key = "timeout-key"
    with pytest.raises(VisionTimeout) as raised:
        OpenAICompatibleVisionProvider(key, transport=TimeoutTransport()).analyze(_jpeg(tmp_path))  # type: ignore[arg-type]
    assert key not in str(raised.value)
    assert raised.value.transport_diagnostic['phase']=='request_open'
    assert raised.value.transport_diagnostic['request_bytes']>0

    class BrokenTransport:
        def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> object:
            raise RuntimeError(key)

    with pytest.raises(VisionConnectionError) as raised:
        OpenAICompatibleVisionProvider(key, transport=BrokenTransport()).analyze(_jpeg(tmp_path))  # type: ignore[arg-type]
    assert key not in str(raised.value)
    assert set(raised.value.transport_diagnostic)=={'phase','elapsed_ms','request_bytes'}


@pytest.mark.parametrize('wrapped,error_type',[(False,VisionTimeout),(True,VisionTimeout),('connection',VisionConnectionError)])
def test_urllib_timeout_classification_keeps_bounded_safe_diagnostic_without_retry(monkeypatch,wrapped,error_type):
    calls=[];clock=iter([10.0,10.125])
    monkeypatch.setattr('app.providers.vision.time',SimpleNamespace(monotonic=lambda:next(clock)))
    class Opener:
        def open(self,request,timeout):
            calls.append((request,timeout))
            error=ConnectionRefusedError('private upstream detail') if wrapped=='connection' else socket.timeout('private timeout detail')
            raise URLError(error) if wrapped else error
    monkeypatch.setattr('app.providers.vision.build_opener',lambda *a:Opener())
    body='{"私密":"fixture-secret"}'.encode()
    with pytest.raises(error_type) as failure:
        UrllibVisionTransport().post('https://api.example/v1/chat/completions',{'Authorization':'Bearer fixture-secret'},body,120)
    assert failure.value.transport_diagnostic=={'phase':'request_open','elapsed_ms':125,'request_bytes':len(body)}
    assert len(calls)==1 and calls[0][1]==120
    assert 'private' not in json.dumps(failure.value.transport_diagnostic)
    assert 'fixture-secret' not in str(failure.value)


def test_urllib_response_body_timeout_preserves_received_http_status(monkeypatch):
    class Response:
        status=200;headers={}
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self):raise socket.timeout('private body read detail')
    class Opener:
        def open(self,*a,**k):return Response()
    monkeypatch.setattr('app.providers.vision.build_opener',lambda *a:Opener())
    with pytest.raises(VisionTimeout) as failure:
        UrllibVisionTransport().post('https://api.example/v1/chat/completions',{},b'{}',120)
    assert failure.value.transport_diagnostic['phase']=='response_body'
    assert failure.value.transport_diagnostic['http_status']==200
    assert set(failure.value.transport_diagnostic)=={'phase','elapsed_ms','request_bytes','http_status'}


@pytest.mark.parametrize('status',[401,429,503])
def test_urllib_error_status_is_kept_without_reading_stalled_or_sensitive_error_body(monkeypatch,status):
    closed=[]
    class ErrorBody:
        def read(self,*a):pytest.fail('不能为读取错误正文而丢失已知HTTP状态')
        def close(self):closed.append(True)
    class Opener:
        def open(self,*a,**k):
            raise HTTPError('https://api.example/v1/chat/completions',status,'private detail',{'Retry-After':'3'},ErrorBody())
    monkeypatch.setattr('app.providers.vision.build_opener',lambda *a:Opener())
    response=UrllibVisionTransport().post('https://api.example/v1/chat/completions',{},b'{}',120)
    assert response.status_code==status and response.body==b'' and response.headers=={'Retry-After':'3'}
    assert closed


@pytest.mark.parametrize('phase',['request_upload','response_headers','connection','proxy_connection','unknown'])
def test_urllib_phase_uses_only_known_standard_library_traceback_functions(monkeypatch,phase):
    import http.client
    import ssl
    class BrokenFile:
        def readline(self,*a):raise socket.timeout('unpersisted read details')
        def close(self):pass
        def flush(self):pass
    class BrokenSocket:
        def makefile(self,*a,**k):return BrokenFile()
        def sendall(self,*a):
            if phase=='request_upload':raise socket.timeout('unpersisted upload detail')
        def close(self):pass
    def send():raise socket.timeout('test function name alone is not transport evidence')
    class Opener:
        def open(self,*a,**k):
            if phase=='request_upload':
                connection=http.client.HTTPConnection('unused.invalid');connection.sock=BrokenSocket()
                connection.send(b'fixture')
            elif phase=='response_headers':
                http.client.HTTPResponse(BrokenSocket()).begin()
            elif phase=='connection':
                class Handshake:
                    def do_handshake(self):raise socket.timeout('unpersisted handshake detail')
                ssl.SSLObject.do_handshake(SimpleNamespace(_sslobj=Handshake()))
            elif phase=='proxy_connection':
                connection=http.client.HTTPConnection('unused.invalid')
                connection.set_tunnel('unused.example',443)
                connection.sock=BrokenSocket()
                connection._tunnel()
            else:send()
    monkeypatch.setattr('app.providers.vision.build_opener',lambda *a:Opener())
    with pytest.raises(VisionTimeout) as failure:
        UrllibVisionTransport().post('https://api.example/v1/chat/completions',{},b'{}',120)
    expected='connection' if phase=='proxy_connection' else 'request_open' if phase=='unknown' else phase
    assert failure.value.transport_diagnostic['phase']==expected
    assert 'detail' not in json.dumps(failure.value.transport_diagnostic)


def test_custom_transport_diagnostic_is_filtered_to_safe_allowed_fields(tmp_path):
    class Transport:
        def post(self,*a):
            failure=VisionConnectionError('safe connection failure')
            failure.transport_diagnostic={'phase':'response_headers','elapsed_ms':7,'request_bytes':-2,
                'http_status':200,'traceback':'fixture-secret','proxy_url':'http://secret@private.example'}
            raise failure
    with pytest.raises(VisionConnectionError) as raised:
        OpenAICompatibleVisionProvider('fixture-secret',transport=Transport()).analyze(_jpeg(tmp_path))
    diagnostic=raised.value.transport_diagnostic
    assert diagnostic['phase']=='response_headers' and diagnostic['elapsed_ms']==7 and diagnostic['http_status']==200
    assert diagnostic['request_bytes']>0 and set(diagnostic)=={'phase','elapsed_ms','request_bytes','http_status'}


def test_visual_analysis_contract_is_immutable_and_strict() -> None:
    analysis = ClipVisualAnalysis(
        visual_description=None,
        people=(), objects=(), location=None, action=None, shot_type=None,
        quality_score=0, face_visibility=None, mouth_visibility=None, talking_candidate=False,
    )
    with pytest.raises(Exception):
        analysis.people = ("replacement",)  # type: ignore[misc]
    with pytest.raises(ValueError, match="immutable"):
        ClipVisualAnalysis(
            visual_description=None,
            people=["person"],  # type: ignore[arg-type]
            objects=(), location=None, action=None, shot_type=None,
            quality_score=None, face_visibility=None, mouth_visibility=None, talking_candidate=False,
        )
    with pytest.raises(ValueError, match="score"):
        ClipVisualAnalysis(
            visual_description=None,
            people=(), objects=(), location=None, action=None, shot_type=None,
            quality_score=float("nan"), face_visibility=None, mouth_visibility=None, talking_candidate=False,
        )

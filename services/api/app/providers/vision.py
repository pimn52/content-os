"""Provider-neutral visual analysis and a safe OpenAI-compatible adapter."""
from __future__ import annotations

import base64
import json
import math
import socket
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from ipaddress import ip_address
from pathlib import Path
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from typing import Literal

from app.costs import CatalogProviderCostEstimator, ProviderUsage, TokenPrice, UnknownProviderCostEstimator
from app.domain.models import CostCategory


class VisionError(RuntimeError):
    """Expected credential-safe vision provider errors."""


class VisionConfigurationError(VisionError):
    pass


class VisionInputError(VisionError):
    pass


class VisionTimeout(VisionError):
    transport_diagnostic: dict | None = None


class VisionConnectionError(VisionError):
    transport_diagnostic: dict | None = None


class VisionAuthenticationError(VisionError):
    pass


class VisionRateLimitError(VisionError):
    pass


class VisionHTTPError(VisionError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"vision provider returned HTTP {status_code}")


class VisionProviderResponseError(VisionError):
    pass


class SceneVisualMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    scene_key: str = Field(min_length=1, max_length=100)
    supported: bool
    score: float = Field(ge=0, le=1, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=500)
    fit: Literal["direct", "illustrative", "context", "unknown"] = "unknown"
    quality_adequate: bool | None = None
    subject_verified: bool | None = None


class NormalizedVideoRegion(BaseModel):
    """视频接口使用具名边界；旧数组按 WH 与画面求交，不缩小任何可能的可见区域。"""
    model_config = ConfigDict(extra="forbid", strict=True)
    left: float = Field(ge=0,le=1,allow_inf_nan=False)
    top: float = Field(ge=0,le=1,allow_inf_nan=False)
    right: float = Field(ge=0,le=1,allow_inf_nan=False)
    bottom: float = Field(ge=0,le=1,allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def legacy_visible_bounds(cls, values):
        if isinstance(values,(list,tuple)):
            if (len(values)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in values)
                or values[2]<=0 or values[3]<=0):
                raise ValueError("invalid legacy video region")
            x,y,width,height=values
            return dict(left=float(x),top=float(y),right=min(1.0,x+width),bottom=min(1.0,y+height))
        return values

    @model_validator(mode="after")
    def positive_region(self):
        if self.left>=self.right or self.top>=self.bottom:
            raise ValueError("empty video region")
        return self

    def xywh(self):
        return [self.left,self.top,self.right-self.left,self.bottom-self.top]


class TalkingMotionInterval(BaseModel):
    """整段视频中实际观察到的连续动作区间；起止相对上传片段。"""
    model_config = ConfigDict(extra="forbid", strict=True)
    start_offset_ms: int = Field(ge=0)
    end_offset_ms: int = Field(gt=0)
    single_person_continuous: bool
    motion_continuous: bool
    head_clearance: bool
    face_visibility: float = Field(ge=0, le=1, allow_inf_nan=False)
    mouth_visibility: float = Field(ge=0, le=1, allow_inf_nan=False)
    quality_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    burned_in_subtitles: bool | None
    start_gaze: Literal["camera", "away", "uncertain"]
    end_gaze: Literal["camera", "away", "uncertain"]
    expression_labels: list[str] = Field(max_length=20)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def positive_interval(self):
        if self.end_offset_ms <= self.start_offset_ms:
            raise ValueError("motion interval must be positive")
        return self


class VideoIntervalAnalysis(BaseModel):
    """对实际上传片段的机器观察，不冒充连续逐帧或人工认可。"""
    model_config = ConfigDict(extra="forbid", strict=True)
    clip_id: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1600)
    content_consistent: bool
    comparison_layout: Literal["unknown", "none", "side_by_side_equal_halves"]
    subtitle_regions: list[NormalizedVideoRegion] | None = Field(max_length=100)
    protected_regions: list[NormalizedVideoRegion] | None = Field(max_length=100)
    matches: list[SceneVisualMatch] = Field(max_length=24)
    reason: str = Field(min_length=1, max_length=500)
    talking_intervals: list[TalkingMotionInterval] | None = Field(default=None, max_length=12)

    def regions_xywh(self, field):
        regions=getattr(self,field)
        return None if regions is None else [NormalizedVideoRegion.model_validate(region).xywh() for region in regions]


class VideoIntervalBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    clips: list[VideoIntervalAnalysis] = Field(min_length=1, max_length=8)


def _parse_video_interval_output(output, clip_ids, scene_keys):
    """身份和请求范围必须完整；单个候选字段坏掉时隔离该候选，不猜测修值。"""
    document=json.loads(output)
    if not isinstance(document,dict) or set(document)!={"clips"} or not isinstance(document["clips"],list):
        raise VisionProviderResponseError("invalid video analysis document")
    rows=document["clips"]
    if (len(rows)!=len(clip_ids) or any(not isinstance(row,dict) or not isinstance(row.get("clip_id"),str) for row in rows)
        or sorted(row["clip_id"] for row in rows)!=sorted(clip_ids)):
        raise VisionProviderResponseError("video analysis changed the requested clips")
    observations,issues=[],[]
    for row in rows:
        matches=row.get("matches")
        if isinstance(matches,list):
            keys=[match.get("scene_key") for match in matches if isinstance(match,dict) and isinstance(match.get("scene_key"),str)]
            if len(keys)!=len(set(keys)) or any(key not in scene_keys for key in keys):
                raise VisionProviderResponseError("video analysis changed the requested scenes")
        try:
            observations.append(VideoIntervalAnalysis.model_validate(row))
        except ValidationError as error:
            issues.append({"clip_id":row["clip_id"],"code":"invalid_clip_fields",
                "fields":sorted({str(item["loc"][0]) for item in error.errors(include_input=False) if item["loc"]}),
                "raw_output":row})
            observations.append(VideoIntervalAnalysis(clip_id=row["clip_id"],description="该候选核验字段无效，未认可画面用途",
                content_consistent=False,comparison_layout="unknown",subtitle_regions=None,protected_regions=None,
                matches=[],reason="素材核验字段无效，已隔离该候选；其余候选按各自结果处理"))
    return VideoIntervalBatch(clips=observations),issues


@dataclass(frozen=True)
class ClipVisualAnalysis:
    """Non-identifying metadata compatible with existing ``Clip`` fields."""

    visual_description: str | None
    people: tuple[str, ...]
    objects: tuple[str, ...]
    location: str | None
    action: str | None
    shot_type: str | None
    quality_score: float | None
    face_visibility: float | None
    mouth_visibility: float | None
    talking_candidate: bool
    comparison_layout: str = "unknown"
    # Normalized source coordinates; None means unknown, () means none observed.
    subtitle_regions: tuple[tuple[float, float, float, float], ...] | None = None

    def __post_init__(self) -> None:
        _optional_text("visual_description", self.visual_description, 5_000)
        _text_items("people", self.people, 30, 500)
        _text_items("objects", self.objects, 100, 500)
        _optional_text("location", self.location, 500)
        _optional_text("action", self.action, 500)
        _optional_text("shot_type", self.shot_type, 100)
        _score("quality_score", self.quality_score)
        _score("face_visibility", self.face_visibility)
        _score("mouth_visibility", self.mouth_visibility)
        if not isinstance(self.talking_candidate, bool):
            raise ValueError("talking_candidate must be a boolean")
        validate_visual_geometry(self.comparison_layout, self.subtitle_regions)


def validate_visual_geometry(layout, regions):
    if layout not in {"unknown", "none", "side_by_side_equal_halves"}:
        raise ValueError("unsupported comparison layout")
    if regions is not None:
        if not isinstance(regions, (tuple, list)) or len(regions) > 100:
            raise ValueError("invalid subtitle regions")
        for box in regions:
            if (not isinstance(box, (tuple, list)) or len(box) != 4
                or any(isinstance(v, bool) or not isinstance(v, (int,float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in box)
                or box[2] <= 0 or box[3] <= 0 or box[0]+box[2] > 1 or box[1]+box[3] > 1):
                raise ValueError("invalid normalized subtitle rectangle")


class VisionProvider(Protocol):
    def analyze(self, keyframe_path: str | Path) -> ClipVisualAnalysis: ...


@dataclass(frozen=True)
class VisionHTTPResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str]


class VisionHTTPTransport(Protocol):
    def post(self, url: str, headers: Mapping[str, str], body: bytes, timeout_seconds: float) -> VisionHTTPResponse: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    """Never replay BYOK authorization to a redirect target."""

    def redirect_request(self, request: Request, fp: object, code: int, message: str, headers: object, new_url: str) -> None:
        return None


def _transport_phase(error, fallback="request_open"):
    """只读标准库已知函数名，不保存异常文本、路径或 traceback。"""
    frames=set()
    seen=set()
    pending=[error]
    while pending and len(seen)<4:
        current=pending.pop()
        if not isinstance(current,BaseException) or id(current) in seen:continue
        seen.add(id(current))
        traceback=current.__traceback__
        while traceback is not None:
            module=traceback.tb_frame.f_globals.get("__name__")
            if module in {"http.client","socket","ssl"}:
                frames.add((module,traceback.tb_frame.f_code.co_name))
            traceback=traceback.tb_next
        pending.extend((getattr(current,"reason",None),current.__cause__))
    if any(item in frames for item in {("http.client","connect"),("http.client","_tunnel"),
            ("socket","create_connection"),("socket","getaddrinfo"),("ssl","do_handshake"),("ssl","wrap_socket")}):
        return "connection"
    if any(item in frames for item in {("http.client","send"),("http.client","_send_output")}):
        return "request_upload"
    if any(item in frames for item in {("http.client","getresponse"),("http.client","_read_status")}):
        return "response_headers"
    return fallback


def _transport_failure(error_type,message,error,*,phase,started,request_bytes,http_status=None):
    failure=error_type(message)
    diagnostic={"phase":_transport_phase(error) if phase=="request_open" else phase,
        "elapsed_ms":max(0,round((time.monotonic()-started)*1000)),"request_bytes":request_bytes}
    if type(http_status) is int and 100<=http_status<=599:diagnostic["http_status"]=http_status
    failure.transport_diagnostic=diagnostic
    return failure


class UrllibVisionTransport:
    """Small stdlib-only HTTP boundary, independent from ASR."""

    def post(self, url: str, headers: Mapping[str, str], body: bytes, timeout_seconds: float) -> VisionHTTPResponse:
        request = Request(url, data=body, headers=dict(headers), method="POST")
        started=time.monotonic()
        phase,http_status="request_open",None
        try:
            opener = build_opener(_NoRedirectHandler())
            with opener.open(request, timeout=timeout_seconds) as response:  # noqa: S310 - base URL is validated.
                phase,http_status="response_body",response.status
                return VisionHTTPResponse(response.status, response.read(), dict(response.headers.items()))
        except HTTPError as exc:
            # 状态已确定；不读可能阻塞或回显凭据的错误正文。
            headers=dict(exc.headers.items()) if exc.headers else {}
            exc.close()
            return VisionHTTPResponse(exc.code,b"",headers)
        except socket.timeout as exc:
            raise _transport_failure(VisionTimeout,"vision request timed out",exc,phase=phase,started=started,
                request_bytes=len(body),http_status=http_status) from exc
        except URLError as exc:
            timed_out=isinstance(exc.reason,TimeoutError)
            raise _transport_failure(VisionTimeout if timed_out else VisionConnectionError,
                "vision request timed out" if timed_out else "could not connect to the vision provider",exc,
                phase=phase,started=started,request_bytes=len(body),http_status=http_status) from exc
        except OSError as exc:
            raise _transport_failure(VisionConnectionError,"could not connect to the vision provider",exc,
                phase=phase,started=started,request_bytes=len(body),http_status=http_status) from exc


_REQUIRED = (
    "visual_description", "people", "objects", "location", "action", "shot_type",
    "quality_score", "face_visibility", "mouth_visibility", "talking_candidate",
)
_ANALYSIS_SCHEMA: dict[str, object] = {
    "type": "object", "additionalProperties": False, "required": [*_REQUIRED,"comparison_layout","subtitle_regions"],
    "properties": {
        "visual_description": {"type": ["string", "null"], "maxLength": 5_000},
        "people": {"type": "array", "maxItems": 30, "items": {"type": "string", "minLength": 1, "maxLength": 500}},
        "objects": {"type": "array", "maxItems": 100, "items": {"type": "string", "minLength": 1, "maxLength": 500}},
        "location": {"type": ["string", "null"], "maxLength": 500},
        "action": {"type": ["string", "null"], "maxLength": 500},
        "shot_type": {"type": ["string", "null"], "maxLength": 100},
        "quality_score": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "face_visibility": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "mouth_visibility": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "talking_candidate": {"type": "boolean"},
        "comparison_layout": {"type":"string","enum":["unknown","none","side_by_side_equal_halves"]},
        "subtitle_regions": {"type":["array","null"],"maxItems":100,"items":{"type":"array","minItems":4,"maxItems":4,
            "items":{"type":"number","minimum":0,"maximum":1}}},
    },
}
_FIXED_PROMPT = (
    "Analyze this single local video keyframe for media-library retrieval. Return only the requested JSON schema. "
    "Describe visible people only with non-identifying visual roles or appearance. Do not identify, name, recognize, "
    "or guess identity. Do not infer sensitive attributes, including race, ethnicity, nationality, religion, health, "
    "disability, age, gender identity, sexual orientation, or mental state. Only state what is visibly supported. "
    "Report equal left/right comparison panes only when visible. Locate burned-in subtitle rectangles as normalized "
    "[x,y,width,height], including outlines/shadows; [] means none observed and null means unknown. "
    "Do not include bitrate labels or other evidence labels as subtitles. These are sampled frame facts, not crop-safety approval."
)


class OpenAICompatibleVisionProvider:
    """BYOK ``POST /v1/responses`` adapter for local JPEG keyframes only."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4.1-mini",
        detail: str = "low",
        timeout_seconds: float = 60.0,
        transport: VisionHTTPTransport | None = None,
        protocol: str = "responses",
        video_input: bool = False,
        thinking_type: str | None = None,
        max_output_tokens: int = 12288,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise VisionConfigurationError("vision API key must be supplied at runtime")
        normalized_base_url = _validate_base_url(base_url)
        if not isinstance(model, str) or not model.strip():
            raise VisionConfigurationError("vision model must not be empty")
        if detail not in {"low", "high", "auto"}:
            raise VisionConfigurationError("vision detail must be low, high, or auto")
        if isinstance(timeout_seconds, bool):
            raise VisionConfigurationError("vision timeout must be finite and positive")
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise VisionConfigurationError("vision timeout must be finite and positive") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise VisionConfigurationError("vision timeout must be finite and positive")
        self._api_key = api_key.strip()
        if protocol not in {"responses", "chat_completions"} or not isinstance(video_input, bool):
            raise VisionConfigurationError("invalid vision protocol or video capability")
        if thinking_type not in {None, "enabled", "disabled"}:
            raise VisionConfigurationError("invalid vision thinking mode")
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 32768:
            raise VisionConfigurationError("vision output token limit must be an integer from 1 to 32768")
        self.protocol, self.supports_video_input, self.thinking_type = protocol, video_input, thinking_type
        self.provider_name = "openai-compatible"
        self._endpoint = f"{normalized_base_url.rstrip('/')}/{'responses' if protocol == 'responses' else 'chat/completions'}"
        self.model = model.strip()
        self.detail = detail
        self.timeout_seconds = timeout
        self.max_output_tokens = max_output_tokens
        self.transport = transport or UrllibVisionTransport()
        self.last_usage: dict[str, int] = {}
        self.last_validation_issues: list[dict] = []

    def _token_price(self):
        parsed = urlsplit(self._endpoint)
        if (parsed.scheme != "https" or parsed.hostname != "api.moonshot.cn" or parsed.port not in (None, 443)
                or parsed.path != "/v1/chat/completions" or self.model != "kimi-k2.6"):
            return None
        # 官方页面价格表数据，2026-10-25核对；视频输入动态计算Token，不能按秒或输出上限猜输入量。
        return TokenPrice(Decimal("6.50"), Decimal("27.00"), Decimal("1.10"), "CNY", date(2026, 10, 25),
            "https://platform.kimi.com/docs/pricing/chat")

    def cost_estimator(self):
        price = self._token_price()
        if price is None:
            return UnknownProviderCostEstimator("此视觉提供商或模型未核实价格与输入上界")
        # 不额外调用token计数接口。按官方262144上下文上界全额预留输入，并独立预留输出cap。
        # 两者相加比共享上下文的实际最大量更保守；缓存折扣不用于派发前放大可用额度。
        upper = price.cost(input_tokens=262144, output_tokens=self.max_output_tokens, cache_tokens=0,
            provider=self.provider_name, category=CostCategory.LLM,
            note=f"单次保守金额上界，输入至多262144 Token、输出至多{self.max_output_tokens} Token，输入按未缓存；非实际用量或账单")
        return CatalogProviderCostEstimator({("vision", self.provider_name, self.model): upper})

    def usage_accounting(self, error: Exception | None = None) -> ProviderUsage:
        # 失败只读该异常绑定的本次响应，不能把上一请求的last_usage计到预检或网络失败上。
        usage = self.last_usage if error is None else getattr(error, "provider_usage", {})
        if not isinstance(usage, dict):
            usage = {}
        def count(*keys):
            value = next((usage[k] for k in keys if k in usage), None)
            return value if type(value) is int and 0 <= value <= 1_000_000_000 else None
        input_tokens, output_tokens = count("prompt_tokens", "input_tokens"), count("completion_tokens", "output_tokens")
        cached = count("cached_tokens")
        if usage.get("cache_tokens_conflict") or cached is not None and (input_tokens is None or cached > input_tokens):
            cached = None
        price, actual = self._token_price(), None
        total = count("total_tokens")
        consistent = input_tokens is not None and output_tokens is not None and (total is None or total == input_tokens + output_tokens)
        if price is not None and consistent and cached is not None:
            actual = price.cost(input_tokens=input_tokens, output_tokens=output_tokens, cache_tokens=cached,
                provider=self.provider_name, category=CostCategory.LLM,
                note="按本次响应usage及官方公开单价计算；非账单实扣")
        return ProviderUsage(actual_cost=actual, input_tokens=input_tokens, output_tokens=output_tokens,
            cache_tokens=cached, usage_observable=consistent, price_date=price.verified_on if price else None)

    def analyze(self, keyframe_path: str | Path) -> ClipVisualAnalysis:
        image = _local_jpeg_bytes(keyframe_path)
        image_url = "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
        body = {
            "model": self.model,
            "store": False,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": _FIXED_PROMPT},
                {"type": "input_image", "image_url": image_url, "detail": self.detail},
            ]}],
            "text": {"format": {"type": "json_schema", "name": "clip_visual_analysis", "strict": True, "schema": _ANALYSIS_SCHEMA}},
        }
        if self.protocol == "chat_completions":
            body = {"model": self.model, "messages": [{"role":"user", "content":[
                {"type":"text", "text": _FIXED_PROMPT + "\nJSON schema:\n" + json.dumps(_ANALYSIS_SCHEMA)},
                {"type":"image_url", "image_url":{"url":image_url}}]}], "response_format":{"type":"json_object"}}
            if self.thinking_type is not None:
                body["thinking"] = {"type": self.thinking_type}
        response = self._request(body)
        try:
            return _parse_response(response, protocol=self.protocol)
        except VisionProviderResponseError as error:
            error.provider_usage = dict(self.last_usage)
            raise

    def analyze_intervals(self, videos: list[dict], scenes: list[dict]) -> VideoIntervalBatch:
        """一次请求核对多个确切视频区间及其场景用途，不做内部重试。"""
        if not self.supports_video_input or self.protocol != "chat_completions":
            raise VisionConfigurationError("video interval analysis is not configured")
        if not 1 <= len(videos) <= 8 or not 0 <= len(scenes) <= 24:
            raise VisionInputError("video analysis batch exceeds its bounded scope")
        clip_ids = [item["clip_id"] for item in videos]
        scene_keys = {item["scene_key"] for item in scenes}
        if len(set(clip_ids)) != len(clip_ids) or len(scene_keys) != len(scenes):
            raise VisionInputError("video analysis IDs must be unique")
        from app.media.visual_facts import visual_review_task
        review_scenes = [{**scene, "review_task": visual_review_task(scene.get("visual_requirement"))} for scene in scenes]
        output_schema=VideoIntervalBatch.model_json_schema()
        match_schema=output_schema["$defs"]["SceneVisualMatch"]
        match_schema["required"]=list(match_schema["properties"])
        if not scenes:
            output_schema["$defs"]["VideoIntervalAnalysis"]["properties"]["matches"]["maxItems"]=0
        motion_requested = any(item.get("talking_motion_reference") is True for item in videos)
        if not motion_requested:
            output_schema["$defs"]["VideoIntervalAnalysis"]["properties"].pop("talking_intervals", None)
            output_schema["$defs"].pop("TalkingMotionInterval", None)
        motion_instructions = (
            "仅对输入 talking_motion_reference=true 的片段填写 talking_intervals，其他片段填 null。"
            "这是已由创作者登记的本人动作参考，不要识别或猜测人物身份，也不要求连续流利说话、有声或原片说出新旁白。"
            "给出整个上传片段内可确认的最长连续单人区间，起止相对上传视频的0毫秒；不能只检查首尾或单帧。"
            "遇切镜、换人、明显遮嘴、离开画面须断开；未看清或无法评估连续性时不列区间。"
            "face_visibility、mouth_visibility、quality_score 按该连续区间的最低可辨识度给分；"
            "single_person_continuous、motion_continuous、head_clearance 分别说明全段单人、动作连续、头脸完整。"
            "burned_in_subtitles 不确定填 null；start_gaze/end_gaze 只描述区间边界实际视线。"
            "不把静止照片、循环、倒放、明显变脸或多镜头拼接列为连续动作参考；整段没有合格区间填 []。\n"
        ) if motion_requested else ""
        content = [{"type":"text", "text": (
            "检查以下静音视频原片段，输出 JSON。每个 clip_id 都必须返回且只能返回一次。"
            "description 必须在整个上传区间成立；夹有无关画面或无法判断时 content_consistent=false。"
            "检查实际动态画面，不能根据文件名、旁白或主题臆造原片。"
            "subtitle_regions 是整段烧录对白字幕的区域并集，包含描边；未知填 null，无字幕填 []。"
            "protected_regions 是整个区间关键人物、操作、证据及参数标签的区域并集；不清楚填 null。"
            "码率标签、图表轴与证据说明是关键画面，不能当成可删的字幕。"
            "矩形必须使用具名对象 {left,top,right,bottom}，不能使用四元数组；每个边界归一化到 0..1。"
            '合法区域 JSON 示例：{"left":0.1,"top":0.2,"right":0.8,"bottom":0.9}。'
            "四个键必须分别完整输出；top_bottom 等合并键无效。left<right 且 top<bottom；"
            "示例坐标只说明格式，不代表任何上传画面，不能照抄；无法可靠定位时填 null。"
            "matches 只列画面确实适合的 scene_key；不适合可不列，不能为凑素材而认可。"
            "动作证据必须支持场景主张；普通应景画面不必证明旁白全部知识，不得冒充实测结果。"
            "每个场景的 review_task 由产品按原规划生成，核验时沿用该表达任务，不得新增拍摄或实测要求。"
            "action_evidence 核验明确要求的真实对象、操作或结果；只有直接证据可用，环境交代不能替代。"
            "explanatory 核验原 visual_intent 和 visual_requirement_reason 中的画面作用；"
            "viewer_takeaway 是旁白希望传达的理解，不自动要求每个镜头独立证明全部知识或因果。"
            "若原规划明确使用日常使用场景承接建议，真实且应景的使用画面可按 context 评价，"
            "不能仅因未展示全部参数设置或实测因果而拒绝；但不能把这段画面说成进行了操作或证明了效果。"
            "若原规划明确要求实际编码界面、具体操作、测量或对比结果，即使标为 explanatory 也必须保留这些要求。"
            "context 只有确实完成原定环境交代、过渡或观看情境且不误导时才可 supported=true；"
            "仅同属一个主题、出现相关设备或随便一段日常画面不够，不能替代原计划的关键画面。"
            "subject_verified 只在视频能辨认场景要求的具体对象时为 true；不确定填 null，明确不符为 false。"
            "先描述实际可辨认的对象和动作，再判断用途，不能根据场景用途补出画面没有的事实。"
            "例如仅见杯中搅动、无法辨认饮品或制作器具，不能因主题是咖啡就认定是咖啡制作；"
            "action_evidence 只有 subject_verified=true 且 fit=direct 才能 supported=true，未知对象或相似手势不作直接证据。"
            "结合 viewer_takeaway 判断表达任务：fit=direct 表示直接展示所需对象、机制或证据，"
            "illustrative 表示准确的辅助说明，context 仅交代环境或气氛，unknown 表示无法判断。"
            "画面仅出现相关设备不等于解释了原理。quality_adequate 判断可辨识度与必要细节，未知填 null。"
            "AI 补充画面只能说明或示意，不能证明真实事件、本人亲历或实测；来源不能从文件名猜测。"
            "同时核对计划中的文字强调与图形标签，不能新增未经支持的实测数字或与旁白矛盾。"
            "不得识别或猜测人物身份、敏感属性。机器核验不等于人工质量认可。\n"
            + motion_instructions
            + "场景用途：" + json.dumps(review_scenes, ensure_ascii=False)
            + "\n输出 JSON schema：" + json.dumps(output_schema, ensure_ascii=False))}]
        if not scenes:
            content[0]["text"] += "\n当前只准备素材目录，没有待匹配脚本。描述整个区间实际可见内容、字幕和关键区域；matches 必须为空。"
        total = 0
        for item in videos:
            path = Path(item["path"])
            data = path.read_bytes()
            total += len(data)
            if path.suffix.lower() != ".mp4" or len(data) < 12 or data[4:8] != b"ftyp" or total > 48*1024*1024:
                raise VisionInputError("video analysis requires bounded local MP4 files")
            content.extend([{"type":"text", "text":json.dumps({k:v for k,v in item.items() if k != "path"}, ensure_ascii=False)},
                {"type":"video_url", "video_url":{"url":"data:video/mp4;base64,"+base64.b64encode(data).decode("ascii")}}])
        body = {"model":self.model, "messages":[{"role":"user", "content":content}],
            "response_format":{"type":"json_object"}, "max_tokens":self.max_output_tokens}
        if self.thinking_type is not None:
            body["thinking"] = {"type":self.thinking_type}
        response = self._request(body)
        try:
            output = _response_text(response, self.protocol)
            result,issues = _parse_video_interval_output(output,clip_ids,scene_keys)
            self.last_validation_issues=json.loads(json.dumps(issues,ensure_ascii=False).replace(self._api_key,"[redacted]"))
        except (ValueError, TypeError, VisionProviderResponseError):
            failure = VisionProviderResponseError("invalid video interval analysis")
            failure.provider_usage = dict(self.last_usage)
            try:
                partial = json.loads(response)["choices"][0]["message"]["content"]
                if isinstance(partial,str) and len(partial) <= 1_000_000:
                    failure.response_document = {"video_analysis_output":partial.replace(self._api_key,"[redacted]")}
            except (ValueError,TypeError,KeyError,IndexError):
                pass
            raise failure from None
        return result

    def _request(self, body: dict) -> bytes:
        self.last_usage = {}
        self.last_validation_issues = []
        if self.protocol == "chat_completions":
            body = dict(body)
            limit = body.get("max_tokens", self.max_output_tokens)
            if type(limit) is not int or not 1 <= limit <= self.max_output_tokens:
                raise VisionInputError("vision request exceeds its reserved output token limit")
            body["max_tokens"] = limit
        serialized=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        started=time.monotonic()
        try:
            response = self.transport.post(
                self._endpoint,
                {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json", "Accept": "application/json"},
                serialized,
                self.timeout_seconds,
            )
        except VisionError as exc:
            if isinstance(exc,(VisionTimeout,VisionConnectionError)):
                diagnostic=_transport_failure(type(exc),str(exc),exc,phase="request_open",started=started,
                    request_bytes=len(serialized)).transport_diagnostic
                previous=exc.transport_diagnostic
                if isinstance(previous,dict):
                    if previous.get("phase") in {"request_open","response_headers","response_body","connection","request_upload"}:
                        diagnostic["phase"]=previous["phase"]
                    if type(previous.get("elapsed_ms")) is int and previous["elapsed_ms"]>=0:
                        diagnostic["elapsed_ms"]=previous["elapsed_ms"]
                    if type(previous.get("http_status")) is int and 100<=previous["http_status"]<=599:
                        diagnostic["http_status"]=previous["http_status"]
                exc.transport_diagnostic=diagnostic
            raise
        except TimeoutError as exc:
            raise _transport_failure(VisionTimeout,"vision request timed out",exc,phase="request_open",started=started,
                request_bytes=len(serialized)) from exc
        except OSError as exc:
            raise _transport_failure(VisionConnectionError,"could not connect to the vision provider",exc,
                phase="request_open",started=started,request_bytes=len(serialized)) from exc
        except Exception as exc:
            raise _transport_failure(VisionConnectionError,"vision transport request failed",exc,
                phase="request_open",started=started,request_bytes=len(serialized)) from None
        _raise_for_status(response.status_code)
        try:
            usage = json.loads(response.body).get("usage", {})
            if isinstance(usage,dict):
                self.last_usage = {key:value for key,value in usage.items()
                    if key in {"prompt_tokens","completion_tokens","total_tokens","input_tokens","output_tokens","cached_tokens"}
                    and type(value) is int and 0 <= value <= 1_000_000_000}
                caches = [usage["cached_tokens"]] if "cached_tokens" in usage else []
                for key in ("prompt_tokens_details", "input_tokens_details"):
                    details = usage.get(key)
                    if isinstance(details, dict) and "cached_tokens" in details:
                        caches.append(details["cached_tokens"])
                if caches:
                    if all(type(value) is int and 0 <= value <= 1_000_000_000 for value in caches) and len(set(caches)) == 1:
                        self.last_usage["cached_tokens"] = caches[0]
                    else:
                        self.last_usage.pop("cached_tokens", None)
                        self.last_usage["cache_tokens_conflict"] = 1
        except (ValueError,AttributeError,TypeError):
            pass
        return response.body


OpenAICompatibleVisualAnalysisAdapter = OpenAICompatibleVisionProvider


def _local_jpeg_bytes(value: str | Path) -> bytes:
    path = Path(value)
    if not path.is_file():
        raise VisionInputError("vision keyframe does not exist")
    if path.suffix.lower() not in {".jpg", ".jpeg"}:
        raise VisionInputError("vision keyframe must be a local JPEG file")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise VisionInputError("vision keyframe cannot be read") from exc
    if len(data) < 4 or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise VisionInputError("vision keyframe is not a valid JPEG file")
    return data


def _raise_for_status(status_code: object) -> None:
    if isinstance(status_code, bool) or not isinstance(status_code, int) or not 100 <= status_code <= 599:
        raise VisionProviderResponseError("vision transport returned an invalid HTTP status")
    if 200 <= status_code < 300:
        return
    if status_code in {401, 403}:
        raise VisionAuthenticationError("vision authentication was rejected")
    if status_code == 429:
        raise VisionRateLimitError("vision provider rate limit was reached")
    raise VisionHTTPError(status_code)


def _response_text(body: bytes, protocol: str = "responses") -> str:
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VisionProviderResponseError("vision provider returned malformed JSON") from exc
    if protocol == "chat_completions":
        choices = document.get("choices") if isinstance(document, dict) else None
        if (not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict) or choices[0].get("finish_reason") != "stop"
            or not isinstance(choices[0].get("message"), dict) or not isinstance(choices[0]["message"].get("content"), str)):
            raise VisionProviderResponseError("vision response is incomplete or malformed")
        return choices[0]["message"]["content"]
    if not isinstance(document, dict) or not isinstance(document.get("output"), list):
        raise VisionProviderResponseError("vision provider response has no output message")
    texts = [
        content.get("text")
        for output in document["output"]
        if isinstance(output, dict) and output.get("type") == "message" and isinstance(output.get("content"), list)
        for content in output["content"]
        if isinstance(content, dict) and content.get("type") == "output_text" and isinstance(content.get("text"), str)
    ]
    if len(texts) != 1:
        raise VisionProviderResponseError("vision provider response has no structured output text")
    return texts[0]


def _parse_response(body: bytes, *, protocol: str = "responses") -> ClipVisualAnalysis:
    try:
        value = json.loads(_response_text(body, protocol))
    except json.JSONDecodeError as exc:
        raise VisionProviderResponseError("vision provider returned invalid structured output") from exc
    if (not isinstance(value, dict) or not set(_REQUIRED) <= set(value)
        or set(value)-set(_REQUIRED)-{"comparison_layout","subtitle_regions"}):
        raise VisionProviderResponseError("vision provider returned an incomplete visual analysis")
    try:
        return ClipVisualAnalysis(
            visual_description=value["visual_description"], people=_tuple_of_strings(value["people"]),
            objects=_tuple_of_strings(value["objects"]), location=value["location"], action=value["action"],
            shot_type=value["shot_type"], quality_score=value["quality_score"],
            face_visibility=value["face_visibility"], mouth_visibility=value["mouth_visibility"],
            talking_candidate=value["talking_candidate"],
            comparison_layout=value.get("comparison_layout","unknown"),
            subtitle_regions=value.get("subtitle_regions"),
        )
    except (TypeError, ValueError) as exc:
        raise VisionProviderResponseError("vision provider returned an invalid visual analysis") from exc


def _tuple_of_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("expected an array")
    return tuple(value)


def _optional_text(name: str, value: object, maximum: int) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > maximum):
        raise ValueError(f"{name} must be a non-empty string within its maximum length or None")


def _text_items(name: str, value: object, maximum_count: int, maximum_length: int) -> None:
    if not isinstance(value, tuple) or len(value) > maximum_count:
        raise ValueError(f"{name} must be an immutable list within its maximum length")
    if any(not isinstance(item, str) or not item.strip() or len(item) > maximum_length for item in value):
        raise ValueError(f"{name} items must be non-empty strings within their maximum length")


def _score(name: str, value: object) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
        raise ValueError(f"{name} must be a finite score from 0 to 1 or None")


def _validate_base_url(base_url: object) -> str:
    if not isinstance(base_url, str) or not base_url or base_url != base_url.strip():
        raise VisionConfigurationError("vision base URL must not be empty")
    if any(character.isspace() or ord(character) < 32 for character in base_url):
        raise VisionConfigurationError("vision base URL is invalid")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise VisionConfigurationError("vision base URL is invalid") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise VisionConfigurationError("vision base URL is invalid")
    if parsed.scheme.lower() == "http" and not _is_loopback_host(parsed.hostname):
        raise VisionConfigurationError("HTTP vision endpoints must use a localhost or loopback host")
    return base_url


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False

"""Pixabay 视频接口；画幅依真实尺寸筛选，API Key 不随原文件下载外发。"""
from decimal import Decimal
from hashlib import sha256
import json
import math
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import NAMESPACE_URL, uuid5

from app.domain.external_media import ExternalVideo
from app.domain.models import CostCategory, UsageCost
from app.providers.commons_media import ExternalMediaError, MAX_VIDEO_BYTES


CATALOGUE_SAMPLE_SIZE = 32  # 一次请求扩大画幅筛选池；官方 per_page 上限为 200。


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _url(value, *, media=False):
    try:
        parsed = urlsplit(value)
        hosts = {"cdn.pixabay.com"} if media else {"pixabay.com", "www.pixabay.com"}
        if (parsed.scheme != "https" or parsed.hostname not in hosts
                or parsed.port not in (None, 443) or parsed.username or parsed.password
                or parsed.fragment or any(ord(c) < 33 for c in value)
                or any(k.casefold() in {"key", "api_key", "apikey", "authorization", "access_token"}
                       for k, _ in parse_qsl(parsed.query))
                or media and not parsed.path.lower().endswith(".mp4")):
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise ExternalMediaError("Pixabay 素材地址不受支持") from None


class PixabayMediaProvider:
    provider_name = "pixabay"
    model = "video-api-v1"
    cache_ttl_seconds = 86400
    quota_reset_kind = "seconds"

    def __init__(self, api_key=None, *, orientation="portrait", opener=None):
        if api_key is not None and (not api_key or len(api_key.split()) != 1
                                   or any(ord(c) < 33 for c in api_key)):
            raise ExternalMediaError("Pixabay Key 文件须只包含密钥")
        if orientation not in ("portrait", "landscape", "square"):
            raise ValueError("unsupported orientation")
        self._api_key, self.orientation = api_key, orientation
        self.search_scope = sha256((api_key or "").encode()).hexdigest() + ":" + orientation
        self.last_quota, self.last_rejections = {}, []
        self._opener = opener or build_opener(_NoRedirect()).open

    def cost(self, currency):
        return UsageCost(category=CostCategory.STOCK, amount=Decimal("0"), currency=currency,
            provider=self.provider_name, note="Pixabay 免费素材接口与下载；遵守账号限额及按需获取限制。")

    def _open(self, url, *, catalogue=False):
        for _ in range(4):
            if catalogue:
                parsed = urlsplit(url)
                if parsed.scheme != "https" or parsed.hostname != "pixabay.com" or not self._api_key:
                    raise ExternalMediaError("请先配置 Pixabay Key 文件")
            else:
                _url(url, media=True)
            try:
                return self._opener(Request(url, headers={"Accept": "application/json" if catalogue else "video/mp4",
                    "User-Agent": "ContentOS/0.1", "Accept-Encoding": "identity"}), timeout=30)
            except HTTPError as error:
                status, location = error.code, error.headers.get("Location")
                retry = error.headers.get("Retry-After") or error.headers.get("X-RateLimit-Reset")
                error.close()
                if not catalogue and status in (301, 302, 303, 307, 308) and location:
                    url = urljoin(url, location)
                    continue
                message = f"Pixabay 请求失败（HTTP {status}），未自动重试"
                if status == 429:
                    message += "；账号已限流" + (f"，{retry} 秒后可恢复" if retry and retry.isdigit() else "")
                exc = ExternalMediaError(message)
                exc.code = f"pixabay_http_{status}"
                if status == 429 and retry and retry.isdigit() and len(retry) <= 10:
                    exc.retry_after_seconds = int(retry)
                raise exc from None
            except (URLError, OSError, TimeoutError):
                # Pixabay 将 Key 放在查询参数；异常不得回传请求 URL 或服务端错误正文。
                raise ExternalMediaError("Pixabay 连接失败或超时，未自动重试") from None
        raise ExternalMediaError("Pixabay 下载重定向过多")

    def search(self, query, *, limit=8):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 100:
            error = ExternalMediaError("Pixabay 查询须为 1 至 100 字符，请使用具体对象与动作")
            error.code = "pixabay_query_invalid"
            raise error
        if not self._api_key:
            raise ExternalMediaError("请先配置 Pixabay Key 文件")
        if type(limit) is not int or limit < 1:
            raise ValueError("invalid result limit")
        result_limit = min(limit, 12)
        self.last_quota, self.last_rejections = {}, []
        # 视频接口没有 orientation 参数，不能沿用图片接口的画幅筛选条件。
        params = {"key": self._api_key, "q": query.strip(), "lang": "zh", "video_type": "all",
            "min_width": 720, "min_height": 720, "per_page": CATALOGUE_SAMPLE_SIZE, "safesearch": "true"}
        try:
            with self._open("https://pixabay.com/api/videos/?" + urlencode(params), catalogue=True) as response:
                if response.status != 200:
                    raise ExternalMediaError("Pixabay 视频目录响应状态无效")
                raw = response.read(4 * 1024 * 1024 + 1)
                self.last_quota = {name: response.headers.get(header) for name, header in
                    [("limit", "X-RateLimit-Limit"), ("remaining", "X-RateLimit-Remaining"), ("reset", "X-RateLimit-Reset")]
                    if isinstance(response.headers.get(header), str) and response.headers.get(header).isdigit()}
        except (URLError, OSError, TimeoutError):
            raise ExternalMediaError("Pixabay 目录读取失败或超时，未自动重试") from None
        if len(raw) > 4 * 1024 * 1024:
            raise ExternalMediaError("Pixabay 检索响应过大")
        try:
            document = json.loads(raw)
            rows = document["hits"]
            if not isinstance(rows, list):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise ExternalMediaError("Pixabay 视频目录响应无效") from None
        values = []
        for row in rows[:CATALOGUE_SAMPLE_SIZE]:
            source_id = str(row.get("id", ""))[:100] if isinstance(row, dict) else ""
            source_id = source_id.replace(self._api_key, "[redacted]")
            try:
                value = self._candidate(row, query.strip())
                if value is None:
                    self.last_rejections.append({"source_id": source_id, "reason": "no_matching_hd_version"})
                else:
                    values.append(value)
            except (ValueError, TypeError, KeyError, ExternalMediaError, AttributeError):
                self.last_rejections.append({"source_id": source_id, "reason": "invalid_source_record"})
        return values[:result_limit]

    def _candidate(self, row, query=""):
        video_id = row["id"]
        if type(video_id) is not int or video_id < 1 or not isinstance(row["videos"], dict):
            raise ValueError()
        versions = []
        for name in ("large", "medium", "small", "tiny"):
            version = row["videos"].get(name)
            if not isinstance(version, dict):
                continue
            w, h = version.get("width"), version.get("height")
            if type(w) is not int or type(h) is not int or min(w, h) < 720:
                continue
            if (self.orientation == "portrait" and h <= w or self.orientation == "landscape" and w <= h
                    or self.orientation == "square" and w != h):
                continue
            try:
                media_url = _url(version["url"], media=True)
                if self._api_key and self._api_key in media_url:
                    raise ExternalMediaError("Pixabay 下载地址包含接口凭据")
            except (ExternalMediaError, KeyError):
                continue
            versions.append((name, version))
        if not versions:
            return None
        suitable = [v for v in versions if min(v[1]["width"], v[1]["height"]) <= 1080]
        name, version = (max(suitable, key=lambda v: v[1]["width"] * v[1]["height"]) if suitable else
            min(versions, key=lambda v: v[1]["width"] * v[1]["height"]))
        media_url, page = _url(version["url"], media=True), _url(row["pageURL"])
        if self._api_key and self._api_key in page:
            raise ExternalMediaError("Pixabay 来源地址包含接口凭据")
        # 查询参数可能轮换；文件路径、版本名和实际尺寸标识确切来源版本。
        identity = f"{video_id}:{name}:{version['width']}x{version['height']}:" + sha256(urlsplit(media_url).path.encode()).hexdigest()[:20]
        def text(value):
            value = str(value)
            return value.replace(self._api_key, "[redacted]") if self._api_key else value
        title, author = text(row.get("tags") or f"Pixabay 视频 {video_id}")[:500], text(row.get("user") or "Pixabay")[:500]
        seconds, size = row.get("duration"), version.get("size")
        duration = round(seconds * 1000) if type(seconds) in (int, float) and math.isfinite(seconds) and seconds > 0 else None
        return ExternalVideo(id=uuid5(NAMESPACE_URL, "pixabay:" + identity), provider=self.provider_name,
            source_id=identity, title=title, description="Pixabay 目录（非画面核验）：" + title + "\n检索线索（非画面核验）：" + text(query),
            page_url=page, download_url=media_url, mime="video/mp4", size_bytes=size if type(size) is int and size > 0 else None,
            width=version["width"], height=version["height"], sha1=None, duration_ms=duration, author=author,
            license_name="Pixabay Content License", license_url="https://pixabay.com/service/license-summary/",
            attribution=f"{title} — {author} · Pixabay · {page}", admissible=True,
            admission_reason="沿用 Pixabay 来源许可；目录体积为估算值，入库后核验文件与用途，不代表画面质量通过")

    def download(self, candidate, destination: Path, *, max_bytes=MAX_VIDEO_BYTES, deadline=None):
        if candidate.provider != self.provider_name or not candidate.admissible:
            raise ExternalMediaError("请使用已保存的 Pixabay 视频条目")
        size, start, created = 0, time.monotonic(), False
        max_bytes = min(max_bytes, MAX_VIDEO_BYTES)
        deadline = min(deadline if deadline is not None else start + 90, start + 90)
        if deadline <= start:
            raise ExternalMediaError("Pixabay 下载窗口已耗尽")
        try:
            with self._open(candidate.download_url) as response:
                if response.status != 200:
                    raise ExternalMediaError("Pixabay 未返回完整文件响应")
                mime = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                length = response.headers.get("Content-Length")
                if mime not in ("video/mp4", "application/octet-stream"):
                    raise ExternalMediaError("Pixabay 未返回视频原文件")
                if length and (not length.isdigit() or not 0 < int(length) <= max_bytes):
                    raise ExternalMediaError("Pixabay 文件大小超过下载限制")
                with destination.open("xb") as output:
                    created = True
                    while chunk := response.read(256 * 1024):
                        size += len(chunk)
                        if size > max_bytes or time.monotonic() > deadline:
                            raise ExternalMediaError("Pixabay 下载超过大小或时间限制")
                        output.write(chunk)
                if size == 0 or length and size != int(length):
                    raise ExternalMediaError("Pixabay 文件下载不完整")
        except (OSError, URLError, TimeoutError):
            if created:
                destination.unlink(missing_ok=True)
            raise ExternalMediaError("Pixabay 文件读取失败或超时，未自动重试") from None
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise

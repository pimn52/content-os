"""Coverr 视频目录与原文件下载；连接设置负责套餐和集中准入。"""
from decimal import Decimal
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import NAMESPACE_URL, uuid5

from app.domain.external_media import ExternalVideo
from app.domain.models import CostCategory, UsageCost
from app.providers.commons_media import ExternalMediaError, MAX_VIDEO_BYTES


CATALOGUE_SAMPLE_SIZE = 20  # 使用官方文档明确列出的 page_size，不猜更大上限。


def _failure(code, message):
    error = ExternalMediaError(message)
    error.code = code
    return error


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _url(value, *, media=False):
    try:
        parsed = urlsplit(value)
        allowed = {"storage.coverr.co", "cdn.coverr.co"} if media else {"coverr.co", "www.coverr.co"}
        if (parsed.scheme != "https" or parsed.hostname not in allowed or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment or any(ord(c) < 33 for c in value)
                or any(key.casefold() in {"key", "api_key", "apikey", "authorization", "access_token"}
                       for key, _ in parse_qsl(parsed.query))
                or media and re.search(r"/preview(?:/|$)", parsed.path, re.IGNORECASE)):
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise _failure("coverr_untrusted_url", "Coverr 原文件地址不受支持") from None


class CoverrMediaProvider:
    provider_name = "coverr"
    model = "video-api-v1"
    cache_ttl_seconds = 900

    def __init__(self, api_key=None, *, orientation="portrait", opener=None):
        if api_key is not None and (not api_key or len(api_key.split()) != 1 or any(ord(c) < 33 for c in api_key)):
            raise _failure("coverr_invalid_key", "Coverr Key 文件须只包含密钥")
        if orientation not in ("portrait", "landscape", "square"):
            raise ValueError("unsupported orientation")
        self._api_key, self.orientation = api_key, orientation
        self.search_scope = sha256((api_key or "").encode()).hexdigest() + ":" + orientation
        self.last_quota, self.last_rejections = {}, []
        self._opener = opener or build_opener(_NoRedirect()).open

    def cost(self, currency):
        return UsageCost(category=CostCategory.STOCK, amount=Decimal("0"), currency=currency,
            provider=self.provider_name, note="Coverr 已配置套餐内请求；套餐费用及来源限额在连接处管理。")

    def _open(self, url, *, catalogue=False):
        for _ in range(4):
            if catalogue:
                parsed = urlsplit(url)
                if parsed.scheme != "https" or parsed.hostname != "api.coverr.co" or not self._api_key:
                    raise _failure("coverr_not_configured", "请先配置 Coverr Key 文件")
                headers = {"Authorization": "Bearer " + self._api_key, "Accept": "application/json"}
            else:
                _url(url, media=True)
                headers = {}
            try:
                return self._opener(Request(url, headers={**headers, "User-Agent": "ContentOS/0.1",
                    "Accept-Encoding": "identity"}), timeout=30)
            except HTTPError as error:
                status, location = error.code, error.headers.get("Location")
                retry = error.headers.get("Retry-After", "")
                error.close()
                if not catalogue and status in (301, 302, 303, 307, 308) and location:
                    url = urljoin(url, location)
                    continue
                failure = _failure(f"coverr_http_{status}", f"Coverr 请求失败（HTTP {status}），未自动重试")
                if status == 429 and retry.isdigit() and len(retry) <= 10:
                    failure.retry_after_seconds = int(retry)
                    failure.args = (str(failure) + f"；账号已限流，请在 {retry} 秒后恢复",)
                raise failure from None
            except (URLError, OSError, TimeoutError):
                raise _failure("coverr_connection_failed", "Coverr 连接失败或超时，未自动重试") from None
        raise _failure("coverr_redirect_limit", "Coverr 下载重定向过多")

    def search(self, query, *, limit=8):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
            raise _failure("coverr_invalid_query", "请输入具体画面关键词")
        if type(limit) is not int or limit < 1:
            raise ValueError("invalid result limit")
        result_limit = min(limit, 12)
        self.last_quota, self.last_rejections = {}, []
        params = {"query": query.strip(), "page": 0, "page_size": CATALOGUE_SAMPLE_SIZE,
            "urls": "true", "sort": "popular"}
        try:
            with self._open("https://api.coverr.co/videos?" + urlencode(params), catalogue=True) as response:
                if response.status != 200:
                    raise _failure("coverr_invalid_response", "Coverr 视频目录响应状态无效")
                raw = response.read(4 * 1024 * 1024 + 1)
                self.last_quota = {name: response.headers.get(header) for name, header in
                    [("limit", "X-Ratelimit-Limit"), ("remaining", "X-Ratelimit-Remaining"), ("reset", "X-Ratelimit-Reset")]
                    if response.headers.get(header) is not None}
        except (URLError, OSError, TimeoutError):
            raise _failure("coverr_connection_failed", "Coverr 目录读取失败或超时，未自动重试") from None
        if len(raw) > 4 * 1024 * 1024:
            raise _failure("coverr_response_too_large", "Coverr 检索响应过大")
        try:
            rows = json.loads(raw)["hits"]
            if not isinstance(rows, list):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise _failure("coverr_invalid_response", "Coverr 视频目录响应无效") from None
        values, self.last_rejections = [], []
        for row in rows[:CATALOGUE_SAMPLE_SIZE]:
            try:
                value = self._candidate(row)
                if value is None:
                    reason = "no_matching_hd_original"
                else:
                    values.append(value.model_copy(update={"description": value.description
                        + "\n检索线索（非画面核验）：" + query.strip()}))
                    continue
            except (ValueError, TypeError, KeyError, AttributeError, ExternalMediaError):
                reason = "invalid_source_record"
            self.last_rejections.append({"source_id": str(row.get("id", ""))[:100] if isinstance(row, dict) else "",
                "reason": reason})
        return values[:result_limit]

    def _candidate(self, row):
        source_id, updated = row["id"], row["updated_at"]
        if (not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", source_id)
                or not isinstance(updated, str) or not 1 <= len(updated) <= 100):
            raise ValueError("invalid source identity")
        width, height = row["max_width"], row["max_height"]
        if type(width) is not int or type(height) is not int or min(width, height) < 720:
            return None
        if (self.orientation == "portrait" and height <= width
                or self.orientation == "landscape" and width <= height
                or self.orientation == "square" and width != height):
            return None
        # mp4_download 是原文件且由该地址登记下载；预览地址不能替代它。
        download = _url(row["urls"]["mp4_download"], media=True)
        title = row.get("title") or source_id
        description = row.get("description") or ""
        if not isinstance(title, str) or not isinstance(description, str):
            raise ValueError("invalid catalogue text")
        # 目录示例没有具体作品页；保留源 ID，避免拼造未核对的 slug 链接。
        page = _url(row.get("page_url") or "https://coverr.co/")
        duration = row.get("duration")
        duration_ms = (max(1, round(duration * 1000)) if type(duration) in (int, float)
            and math.isfinite(duration) and duration > 0 else None)
        return ExternalVideo(id=uuid5(NAMESPACE_URL, f"coverr:{source_id}:{updated}:{width}:{height}"),
            provider=self.provider_name, source_id=source_id, title=title[:500], description=description[:5000],
            page_url=page, download_url=download, mime="video/mp4", size_bytes=None,
            width=width, height=height, dimensions_exact=False, sha1=None, duration_ms=duration_ms, author="Coverr",
            license_name="Coverr API configured plan", license_url="https://coverr.co/developers",
            attribution=f"{title[:500]} — Coverr · source {source_id} · {page}", admissible=True,
            admission_reason="沿用来源集中准入；所示为目录最大尺寸，原文件编码尺寸在入库时探测，仍须核验内容与用途")

    def download(self, candidate, destination: Path, *, max_bytes=MAX_VIDEO_BYTES, deadline=None):
        if candidate.provider != self.provider_name or not candidate.admissible:
            raise _failure("coverr_invalid_candidate", "请使用已保存的 Coverr 视频条目")
        size, start = 0, time.monotonic()
        max_bytes = min(max_bytes, MAX_VIDEO_BYTES)
        deadline = min(deadline if deadline is not None else start + 90, start + 90)
        if max_bytes <= 0 or time.monotonic() > deadline:
            raise _failure("coverr_download_limit", "Coverr 下载额度或时间窗口已用完")
        output = None
        try:
            output = destination.open("xb")
            with output, self._open(candidate.download_url) as response:
                if response.status != 200:
                    raise _failure("coverr_partial_response", "Coverr 未返回完整文件响应")
                mime = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                length = response.headers.get("Content-Length")
                if mime not in ("video/mp4", "application/octet-stream"):
                    raise _failure("coverr_not_video", "Coverr 未返回视频原文件")
                if length and (not length.isdigit() or not 0 < int(length) <= max_bytes):
                    raise _failure("coverr_file_too_large", "Coverr 文件大小超过下载限制")
                while chunk := response.read(256 * 1024):
                    size += len(chunk)
                    if size > max_bytes or time.monotonic() > deadline:
                        raise _failure("coverr_download_limit", "Coverr 下载超过大小或时限")
                    output.write(chunk)
            if size == 0 or length and size != int(length):
                raise _failure("coverr_incomplete_file", "Coverr 文件下载不完整")
        except (OSError, URLError, TimeoutError):
            if output is not None:
                destination.unlink(missing_ok=True)
            raise _failure("coverr_download_failed", "Coverr 文件读取失败或超时，未自动重试") from None
        except BaseException:
            if output is not None:
                destination.unlink(missing_ok=True)
            raise

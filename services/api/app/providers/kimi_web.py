"""Kimi 独立网页发现接口；不调用聊天模型，也不下载或认可媒体。"""
from decimal import Decimal
from ipaddress import ip_address
import json
import math
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.domain.models import CostCategory, UsageCost


MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class KimiWebError(RuntimeError):
    def __init__(self, code, message, *, response_document=None):
        super().__init__(message)
        self.code = code
        self.response_document = response_document


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class KimiWebProvider:
    provider_name = "moonshot"
    model = "search-basic"

    def __init__(self, api_key: str, *, timeout_seconds: float = 30, opener=None):
        if (not isinstance(api_key, str) or not api_key or len(api_key.split()) != 1
                or any(ord(c) < 33 or ord(c) > 126 for c in api_key)):
            raise ValueError("Moonshot 凭据格式无效")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 120):
            raise ValueError("网页请求时限无效")
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._opener = opener or build_opener(_NoRedirect()).open

    @staticmethod
    def cost() -> UsageCost:
        return UsageCost(category=CostCategory.STOCK, amount=Decimal("0.01"), currency="CNY",
            provider="moonshot", note="Kimi Basic/Fetch 官方公开单价估计；成功且有结果/正文时按次计费，非账单实扣。")

    def _post(self, operation: str, document: dict) -> dict:
        request = Request("https://api.moonshot.cn/v1/tools/" + operation,
            data=json.dumps(document, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": "ContentOS/0.1"})
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                status, raw = response.status, response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            status = error.code
            try:
                raw = error.read(8192)
            finally:
                error.close()
            safe = raw.decode("utf-8", errors="replace").replace(self._api_key, "[redacted]")
            raise KimiWebError(f"kimi_web_http_{status}", f"网页服务返回 HTTP {status}",
                response_document={"http_status": status, "body": safe}) from None
        except (OSError, URLError, TimeoutError):
            raise KimiWebError("kimi_web_connection_failed", "网页服务连接失败或超时；未自动重试") from None
        if status != 200:
            raise KimiWebError(f"kimi_web_http_{status}", f"网页服务返回 HTTP {status}")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise KimiWebError("kimi_web_response_too_large", "网页服务响应超过大小限制")
        safe = raw.decode("utf-8", errors="replace").replace(self._api_key, "[redacted]")
        try:
            value = json.loads(safe)
        except ValueError:
            raise KimiWebError("kimi_web_invalid_response", "网页服务未返回有效 JSON",
                response_document={"body": safe[:8192]}) from None
        if not isinstance(value, dict):
            raise KimiWebError("kimi_web_invalid_response", "网页服务响应结构无效")
        return value

    def search(self, query: str, *, count: int = 8, need_content: bool = True) -> dict:
        # 当前组件的有界请求策略，并非平台能力上限；与 DeepSeek 适配器使用同一调用形状。
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 128:
            raise ValueError("搜索词须为 1 至 128 个字符")
        if type(count) is not int or not 1 <= count <= 20 or type(need_content) is not bool:
            raise ValueError("搜索参数无效")
        value = self._post("search", {"text_query": query.strip(), "limit": count,
            "include_content": need_content, "timeout_seconds": min(int(self.timeout_seconds), 30)})
        results = value.get("search_results")
        if not isinstance(results, list) or any(not isinstance(row, dict)
                or not isinstance(row.get("url"), str) or not isinstance(row.get("title"), str) for row in results):
            raise KimiWebError("kimi_web_invalid_response", "搜索结果结构无效", response_document=value)
        return value

    def fetch(self, url: str) -> dict:
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").rstrip(".").casefold()
            if (len(url) > 8192 or parsed.scheme not in ("https", "http") or not host
                    or parsed.username or parsed.password or parsed.port not in (None, 80, 443)
                    or host == "localhost" or host.endswith((".local", ".localhost"))
                    or any(ord(c) < 33 for c in url)):
                raise ValueError
            try:
                numeric = ip_address(host)
            except ValueError:
                numeric = None
            if numeric is not None and not numeric.is_global:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("网页地址须为不含凭据的公开 HTTP/HTTPS 地址") from None
        value = self._post("fetch", {"url": url})
        if (not isinstance(value.get("url"), str) or not isinstance(value.get("title"), str)
                or not isinstance(value.get("markdown"), str)):
            raise KimiWebError("kimi_web_invalid_response", "网页正文结构无效", response_document=value)
        if not value["markdown"].strip():
            raise KimiWebError("kimi_web_empty_content", "未取得网页正文", response_document=value)
        if value["title"].strip().casefold() in {
                "验证码拦截", "安全验证", "人机验证", "just a moment...", "attention required! | cloudflare"}:
            raise KimiWebError("kimi_web_access_challenge", "来源返回访问验证页，未取得素材页面；保留缺口",
                response_document=value)
        return value

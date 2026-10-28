"""API、Worker 共用的网页工具连接；配置凭据不等于服务已实测。"""
from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Protocol
from urllib.parse import urlsplit

from app.domain.models import UsageCost
from app.providers.deepseek_web import DeepSeekWebProvider
from app.providers.kimi_web import KimiWebProvider


class WebDiscoveryProvider(Protocol):
    provider_name: str
    _api_key: str

    def search(self, query: str, *, count: int = 8, need_content: bool = True) -> dict: ...
    def fetch(self, url: str) -> dict: ...
    def cost(self) -> UsageCost: ...


class WebConnectionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Adapter:
    label: str
    factory: Callable[[str], WebDiscoveryProvider]
    credential_setting: str
    search: bool
    fetch: bool
    estimated_request_cny: str | None
    fallback_prefix: str = ""
    fallback_host: str = ""


_ADAPTERS: dict[str, _Adapter] = {}


def register_web_provider(name: str, factory: Callable[[str], WebDiscoveryProvider], *,
        label: str, credential_setting: str, search: bool = True, fetch: bool = True,
        estimated_request_cny: str | None = None) -> None:
    """仅显式注册已实现网页工具协议的适配器；聊天模型或 Key 不会自动获得能力。"""
    if (not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) or name in _ADAPTERS
            or not callable(factory) or not label or not credential_setting.endswith("_API_KEY_FILE")
            or type(search) is not bool or type(fetch) is not bool or not (search or fetch)):
        raise WebConnectionError("web_provider_registration_invalid", "网页工具适配器注册无效或重复")
    _ADAPTERS[name] = _Adapter(label, factory, credential_setting, search, fetch, estimated_request_cny)


def _adapter(name: str) -> _Adapter:
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise WebConnectionError("web_provider_unsupported", "此连接尚未注册网页搜索/抓取能力") from None


def _credential_file(adapter: _Adapter) -> str:
    path = os.environ.get(adapter.credential_setting, "").strip()
    if path:
        return path
    if adapter.fallback_prefix:
        try:
            if urlsplit(os.environ.get(adapter.fallback_prefix + "_BASE_URL", "")).hostname == adapter.fallback_host:
                return os.environ.get(adapter.fallback_prefix + "_API_KEY_FILE", "").strip()
        except ValueError:
            pass
    return ""


def web_provider_connections() -> list[dict]:
    """供普通 UI 和自动制作读取；configured 只指文件存在，不代判鉴权成功。"""
    values = []
    for name, adapter in _ADAPTERS.items():
        path = _credential_file(adapter)
        try:
            configured = bool(path and Path(path).is_file() and 0 < Path(path).stat().st_size <= 8192)
        except (OSError, ValueError):
            configured = False
        values.append({"id": name, "label": adapter.label, "configured": configured,
            "search": adapter.search, "fetch": adapter.fetch,
            "estimated_request_cny": adapter.estimated_request_cny})
    return values


def configured_web_providers() -> list[str]:
    """返回配置顺序中的可搜索连接；不构造提供商，也不读取或输出 Key 内容。"""
    default = ",".join(_ADAPTERS)
    order = [name.strip() for name in os.environ.get("CONTENT_OS_WEB_PROVIDER_ORDER", default).split(",") if name.strip()]
    if not order or len(order) != len(set(order)) or set(order) - _ADAPTERS.keys():
        raise WebConnectionError("web_provider_order_invalid", "网页搜索连接顺序配置无效")
    available = {row["id"] for row in web_provider_connections() if row["configured"] and row["search"]}
    return [name for name in order if name in available]


def web_provider(name: str, *, capability: str = "search") -> WebDiscoveryProvider:
    adapter = _adapter(name)
    if capability not in {"search", "fetch"} or not getattr(adapter, capability, False):
        raise WebConnectionError("web_capability_unavailable", "此连接未提供所需网页工具能力")
    path = _credential_file(adapter)
    try:
        if not path or not 0 < Path(path).stat().st_size <= 8192:
            raise ValueError
        key = Path(path).read_text(encoding="utf-8-sig").strip()
        if not key or len(key.split()) != 1 or any(ord(char) < 33 for char in key):
            raise ValueError
        provider = adapter.factory(key)
    except (OSError, UnicodeError, ValueError):
        raise WebConnectionError("web_credentials_invalid", "网页发现凭据文件缺失或无效，请修复连接") from None
    if (provider.provider_name != name or not callable(getattr(provider, capability, None))
            or not callable(getattr(provider, "cost", None)) or not isinstance(getattr(provider, "_api_key", None), str)):
        raise WebConnectionError("web_adapter_invalid", "网页工具适配器未实现声明的能力")
    return provider


_ADAPTERS.update({
    "deepseek": _Adapter("DeepSeek", DeepSeekWebProvider, "CONTENT_OS_DEEPSEEK_WEB_API_KEY_FILE",
        True, True, "0.005", "CONTENT_OS_LLM", "api.deepseek.com"),
    "moonshot": _Adapter("Kimi Basic", KimiWebProvider, "CONTENT_OS_KIMI_WEB_API_KEY_FILE",
        True, True, "0.01", "CONTENT_OS_VISION", "api.moonshot.cn"),
})

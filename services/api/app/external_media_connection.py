"""API 与 Worker 共用的素材连接；凭据只从配置文件读取。"""
import os
from pathlib import Path

from app.providers.commons_media import CommonsMediaProvider, ExternalMediaError
from app.providers.pexels_media import PexelsMediaProvider


_KEY_SOURCES = (
    ("pexels", "Pexels", "CONTENT_OS_PEXELS_API_KEY_FILE"),
    ("pixabay", "Pixabay", "CONTENT_OS_PIXABAY_API_KEY_FILE"),
    ("coverr", "Coverr", "CONTENT_OS_COVERR_API_KEY_FILE"),
)


def external_source_connections():
    """列出公共能力与本机配置；不探网，也不覆盖历史真实调用结论。"""
    values = []
    for name, label, setting in _KEY_SOURCES:
        configured = os.environ.get(setting, "").strip()
        try:
            connected = bool(configured and Path(configured).is_file())
        except (OSError, ValueError):
            connected = False
        values.append({"id": name, "label": label, "configured": connected,
            "source_kind": "stock_api", "connection_status": "configured" if connected else "missing_credentials",
            "verification_scope": "configuration_only",
            "capabilities": {"video_search": True, "original_download": True, "automatic_acquisition": True},
            # 这是接入验收卡点清单，不是该账号或历史操作的通过/失败状态。
            "live_checks": ["authentication", "account_quota", "response_schema", "download_rendition", "network_access"]})
    return values + [{"id": "wikimedia_commons", "label": "Wikimedia Commons", "configured": True,
        "source_kind": "public_catalogue", "connection_status": "keyless",
        "verification_scope": "configuration_only",
        "capabilities": {"video_search": True, "original_download": True, "automatic_acquisition": True},
        "live_checks": ["response_schema", "download_rendition", "network_access"]}]


def configured_external_sources():
    # 检查路径而不预读密钥、不构造尚未配置的提供商；配置状态不代表鉴权已验证。
    available = {value["id"] for value in external_source_connections() if value["configured"]}
    order = [name.strip() for name in os.environ.get("CONTENT_OS_EXTERNAL_SOURCE_ORDER",
        "pexels,pixabay,coverr,wikimedia_commons").split(",") if name.strip()]
    if not order or len(order)!=len(set(order)) or set(order)-{"pexels","pixabay","coverr","wikimedia_commons"}:
        raise ExternalMediaError("外部素材来源顺序配置无效")
    return [name for name in order if name in available]


def external_provider(name, project, *, for_import=False):
    if name == "public_page":
        from app.providers.public_page_media import PublicPageMediaProvider
        orientation = {"vertical":"portrait","horizontal":"landscape","square":"square"}[project.format.value]
        return PublicPageMediaProvider(orientation=orientation)
    if name == "wikimedia_commons":
        return CommonsMediaProvider()
    source = next((value for value in _KEY_SOURCES if value[0] == name), None)
    if source is None:
        raise ExternalMediaError("此素材来源尚未接入")
    _, label, setting = source
    key = None
    if not for_import:
        configured = os.environ.get(setting)
        try:
            key = Path(configured).read_text(encoding="utf-8-sig").strip() if configured else None
        except (OSError, UnicodeError, ValueError):
            raise ExternalMediaError(f"{label} Key 文件不可读，请修复连接设置") from None
        if not key:
            raise ExternalMediaError(f"请先配置 {label} Key 文件")
    orientation = {"vertical": "portrait", "horizontal": "landscape", "square": "square"}[project.format.value]
    if name == "pexels":
        return PexelsMediaProvider(key, orientation=orientation)
    if name == "pixabay":
        from app.providers.pixabay_media import PixabayMediaProvider
        return PixabayMediaProvider(key, orientation=orientation)
    from app.providers.coverr_media import CoverrMediaProvider
    return CoverrMediaProvider(key, orientation=orientation)

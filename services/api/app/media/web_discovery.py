"""网页搜索结果分流；分类仅供选择要解析的页面，不证明文件、许可或用途。"""
from copy import deepcopy
from dataclasses import dataclass
from ipaddress import ip_address
import re
from typing import Literal
from urllib.parse import unquote, urlsplit


PageKind = Literal["video_detail", "collection", "image", "document", "challenge", "unknown", "invalid"]
_PRIORITY = {"video_detail": 0, "unknown": 1, "collection": 2, "image": 3,
             "document": 4, "challenge": 5, "invalid": 6}
_CHALLENGE_TITLES = {"验证码拦截", "安全验证", "人机验证", "just a moment...",
                     "attention required! | cloudflare"}


@dataclass(frozen=True)
class DiscoveredPage:
    url: str
    title: str
    kind: PageKind
    evidence: tuple[str, ...]
    original_position: int
    raw: dict


def is_page_candidate(page: DiscoveredPage) -> bool:
    """未知具体页只获得核实资格，不能因此被标记为视频详情或可用素材。"""
    return page.kind == "video_detail" or (page.kind == "unknown" and bool(urlsplit(page.url).path.strip("/")))


def classify_search_results(document: dict) -> list[DiscoveredPage]:
    """保留服务端原行与同类顺序；优先具体视频页，不拼接任何下载地址。"""
    rows = document.get("results", document.get("search_results"))
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("网页搜索结果结构无效")
    pages = [classify_search_result(row, position=i) for i, row in enumerate(rows)]
    return sorted(pages, key=lambda page: _PRIORITY[page.kind])


def classify_search_result(row: dict, *, position: int = 0) -> DiscoveredPage:
    url, title = row.get("url"), row.get("title")
    raw = deepcopy(row)
    url = url if isinstance(url, str) else ""
    title = title if isinstance(title, str) else ""

    def result(kind: PageKind, reason: str) -> DiscoveredPage:
        return DiscoveredPage(url, title, kind, (reason,), position, raw)

    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        if (not host or parsed.scheme not in ("http", "https") or parsed.username or parsed.password
                or parsed.port not in (None, 80, 443) or any(ord(c) < 33 for c in url)
                or host == "localhost" or host.endswith((".local", ".localhost"))):
            raise ValueError
        try:
            numeric = ip_address(host)
        except ValueError:
            numeric = None
        if numeric is not None and not numeric.is_global:
            raise ValueError
    except ValueError:
        return result("invalid", "地址不是可解析的公开 HTTP/HTTPS 页面")

    path = unquote(parsed.path).casefold()
    mime = str(row.get("mime", "")).casefold().split(";", 1)[0]
    clean_title = title.strip().casefold()
    body = "\n".join(str(row.get(key, "")) for key in ("content", "text", "snippet"))
    if clean_title in _CHALLENGE_TITLES:
        return result("challenge", "标题明确为访问验证页")
    if mime.startswith("image/") or re.search(r"\.(?:png|jpe?g|gif|webp|svg|avif)$", path):
        return result("image", "来源 MIME 或文件后缀为图片")
    if mime == "application/pdf" or re.search(r"\.(?:pdf|docx?|pptx?)$", path):
        return result("document", "来源 MIME 或文件后缀为文档")
    if re.search(r"/(?:search|so|archive|category|categories|viewall|set|free-videos|stock-video-footage)(?:/|$)", path):
        return result("collection", "URL 明确标注检索、分类或合集")
    if re.search(r"/(?:topic-|tupian-shipin/)", path):
        return result("collection", "URL 标注主题或素材列表")
    if host in {"699pic.com", "www.699pic.com"} and path.startswith("/media/video-"):
        return result("collection", "摄图已观察的分类路径；数字编号不证明具体条目")
    if host in {"aigei.com", "www.aigei.com", "m.aigei.com"} and path.startswith("/video/index/"):
        return result("collection", "爱给视频分类索引")

    # 仅对已知来源的详情路径结合具体标题/单件元数据分流，仍须解析源站。
    detail_title = bool(clean_title) and clean_title not in {"videos", "video archive", "视频", "免费视频"}
    if detail_title and host in {"esahubble.org", "www.esahubble.org"} and re.fullmatch(r"/videos/[a-z0-9_-]+/?", path):
        return result("video_detail", "Hubble 具体视频路径与标题一致；原文件待解析")
    if detail_title and host in {"pexels.com", "www.pexels.com"} and re.fullmatch(r"/(?:[a-z-]+/)?video/[^/]+/?", path):
        return result("video_detail", "Pexels 视频详情路径与标题；原文件待解析")
    if detail_title and host in {"pixabay.com", "www.pixabay.com", "coverr.co", "www.coverr.co"}:
        if re.fullmatch(r"/(?:[a-z-]+/)?videos/[^/]+/?", path) and not re.search(r"\bvideos\b|免费视频", clean_title):
            return result("video_detail", "来源视频详情路径与具体标题；原文件待解析")
    if detail_title and host in {"aigei.com", "www.aigei.com", "m.aigei.com"} and re.fullmatch(r"/item/[^/]+\.html", path):
        markers = [bool(re.search(pattern, body, re.IGNORECASE)) for pattern in (
            r"(?:资源|作品|本站)编号\s*[:：]?\s*[a-z]?\d+", r"(?:格式\s*[:：]?\s*mp4|mp4\s*格式)",
            r"(?:分辨率\s*[:：]?\s*\d+\s*[×x*]\s*\d+|时长\s*[:：]?\s*\d+秒)")]
        if sum(markers) >= 2:
            return result("video_detail", "爱给条目同时带单件编号与视频参数；下载与许可待源站核验")
    if path.startswith("/images/") and host in {"esahubble.org", "www.esahubble.org"}:
        return result("image", "Hubble 图片条目")
    if (re.search(r"\bvideos\b", clean_title) or "视频素材大全" in clean_title
            or "视频模板大全" in body or re.search(r"(?:为您找到|共计)\s*\d+", body)
            or len(re.findall(r"\b\d{2}:\d{2}", body)) >= 3):
        return result("collection", "标题或正文描述多条视频/检索列表")
    return result("unknown", "页面用途待核实；原文件及来源元数据待解析，不推断文件地址")

"""解析公开详情页的明确原文件与许可；不猜下载地址，不执行网页脚本。"""
from decimal import Decimal
from hashlib import sha256
from html.parser import HTMLParser
from http.client import HTTPException
import json
import math
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request
from uuid import NAMESPACE_URL, uuid5

from app.domain.external_media import ExternalVideo
from app.domain.models import CostCategory, UsageCost
from app.providers.commons_media import ExternalMediaError, MAX_VIDEO_BYTES
from app.providers.public_media_http import UnsafePublicURL, open_public_https, validate_public_https_url


MAX_HTML_BYTES = 8 * 1024 * 1024
_PAGE_HOSTS = {"coverr.co": "coverr", "www.coverr.co": "coverr",
               "esahubble.org": "hubble", "www.esahubble.org": "hubble"}
_MEDIA_HOSTS = {"coverr": {"cdn.coverr.co"}, "hubble": {"cdn.esahubble.org"}}


class PublicPageMediaError(ExternalMediaError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _fail(reason, message):
    raise PublicPageMediaError("public_page_" + reason, message)


def _page(value):
    try:
        parsed = urlsplit(value)
        source = _PAGE_HOSTS.get(parsed.hostname)
        if source is None:
            parsed, host = validate_public_https_url(value)
            # 具体页才可解析；搜索结果页、目录、登录入口不视为素材。
            parts = parsed.path.strip("/").split("/")
            if (not parsed.path.strip("/") or parts[-1].casefold() in
                    {"search", "archive", "viewall", "videos", "login", "signin", "signup"}
                    or any(host.startswith(known + ".") for known in _PAGE_HOSTS)):
                raise ValueError
            return "public", parsed
        if (parsed.scheme != "https" or not source or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or len(value) > 1000 or any(ord(c) < 33 for c in value)
                or not re.fullmatch(r"/videos/[A-Za-z0-9_-]{1,300}/?", parsed.path)
                or parsed.path.strip("/").split("/")[-1] in {"search", "archive", "viewall"}):
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        _fail("unsupported_source", "请提供不含凭据的公网 HTTPS 视频具体页，不能使用目录或登录入口")
    return source, parsed


def _media(value, source):
    try:
        if source == "public":
            parsed, _ = validate_public_https_url(value)
            if not parsed.path.strip("/"):
                raise ValueError
            return parsed
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in _MEDIA_HOSTS[source]
                or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment
                or any(ord(c) < 33 for c in value)
                or not parsed.path.startswith("/videos/" if source == "coverr" else "/archives/videos/")
                or not re.search(r"\.(?:mp4|mov|m4v)$", parsed.path, re.IGNORECASE)
                or re.search(r"(?:^|[/_.-])(?:preview|thumbnail)(?:[/_.-]|$)", parsed.path, re.IGNORECASE)):
            raise ValueError
    except (TypeError, ValueError, AttributeError, KeyError):
        _fail("unsupported_media", "原文件地址不属于该来源的受支持媒体域名或文件版本")
    return parsed


def _walk(value):
    pending = [(value, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > 40:
            continue
        if isinstance(node, dict):
            yield node
            pending.extend((child, depth + 1) for child in reversed(list(node.values())))
        elif isinstance(node, list):
            pending.extend((child, depth + 1) for child in reversed(node))


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts, self.current = [], None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            attributes = dict(attrs)
            if attributes.get("type") == "application/ld+json" or attributes.get("id") in {"__NEXT_DATA__", "ng-state"}:
                self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            try:
                self.scripts.append(json.loads("".join(self.current)))
            except (ValueError, RecursionError):
                pass
            self.current = None


class PublicPageMediaProvider:
    provider_name = "public_page"
    model = "public-video-page"
    cache_ttl_seconds = 86400

    def __init__(self, *, coverr_admitted=False, orientation="portrait", opener=None):
        if type(coverr_admitted) is not bool or orientation not in {"portrait", "landscape", "square", "any"}:
            raise ValueError("公开来源配置无效")
        self.coverr_admitted, self.orientation = coverr_admitted, orientation
        self.search_scope = f"{orientation}:coverr={int(coverr_admitted)}"
        self.last_quota, self.last_rejections = {}, []
        self._opener = opener or open_public_https
        self._page_documents = {}

    def cost(self, currency):
        return UsageCost(category=CostCategory.STOCK, amount=Decimal("0"), currency=currency,
            provider=self.provider_name, note="公开详情页读取与下载，无供应商请求费。")

    @staticmethod
    def can_resolve(url):
        """无网络能力检查；不表示已读到文件、许可或质量通过。"""
        try:
            _page(url)
            return True
        except PublicPageMediaError:
            return False

    def check_source(self, url):
        source, _ = _page(url)
        if source == "coverr" and not self.coverr_admitted:
            _fail("source_not_admitted", "Coverr 免费网页许可尚未纳入本产品供给；请在来源级配置确认，不逐片索权")
        return source

    def use_page_document(self, url, content):
        """复用已经取得的源页 HTML；不把搜索摘要或模型描述当作源元数据。"""
        self.check_source(url)
        self.parse_html(url, content)
        # 单次自动取材只检查少量具体页，避免保存无限网页正文。
        if url not in self._page_documents and len(self._page_documents) >= 8:
            _fail("document_limit", "本轮已保存八份公开具体页正文")
        self._page_documents[url] = content

    def search_context(self, url):
        content = self._page_documents.get(url)
        return {"document_sha256": sha256(content.encode("utf-8")).hexdigest()} if content is not None else {}

    def _open(self, url, *, source, deadline, media=False):
        original = urlsplit(url)
        for _ in range(4):
            if media:
                _media(url, source)
            else:
                current_source, current = _page(url)
                if source != "public" and current_source == "public":
                    _fail("unsupported_source", "已知来源不跳转到其它站点")
                if current_source != source or current.path.rstrip("/") != original.path.rstrip("/"):
                    _fail("redirect_mismatch", "公开页跳转到了不同素材或来源")
                if source == "public" and (current.hostname != original.hostname or current.query != original.query):
                    _fail("redirect_mismatch", "公开详情页跳转到了其它条目或主机")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _fail("timeout", "公开来源读取超过时限，未自动重试")
            try:
                return self._opener(Request(url, headers={"User-Agent": "ContentOS/0.1",
                    "Accept-Encoding": "identity"}), timeout=min(30, remaining))
            except HTTPError as error:
                status, location = error.code, error.headers.get("Location")
                error.close()
                if status in {301, 302, 303, 307, 308} and location:
                    url = urljoin(url, location)
                    continue
                _fail(f"http_{status}", f"公开来源返回 HTTP {status}，未自动重试")
            except (OSError, URLError, TimeoutError, HTTPException):
                _fail("connection_failed", "公开来源连接失败或超时，未自动重试")
            except UnsafePublicURL:
                _fail("unsafe_url", "公开来源指向本机、私网或不安全地址，已停止读取")
        _fail("redirect_limit", "公开来源重定向过多")

    def search(self, query_url, *, limit=8):
        source = self.check_source(query_url)  # 来源准入必须在任何网络请求之前完成。
        if query_url in self._page_documents:
            return self.parse_html(query_url, self._page_documents[query_url])[:max(0, min(limit, 8))]
        deadline, raw = time.monotonic() + 30, bytearray()
        try:
            with self._open(query_url, source=source, deadline=deadline) as response:
                if response.status != 200:
                    _fail("incomplete_page", "详情页未返回完整 HTTP 200 响应")
                if response.headers.get("Content-Type", "").split(";", 1)[0].lower() not in {"text/html", "application/xhtml+xml"}:
                    _fail("not_html", "来源未返回 HTML 详情页")
                while chunk := response.read(min(65536, MAX_HTML_BYTES + 1 - len(raw))):
                    raw.extend(chunk)
                    if len(raw) > MAX_HTML_BYTES:
                        _fail("html_too_large", "公开详情页超过 8 MiB 上限")
                    if time.monotonic() > deadline:
                        _fail("timeout", "公开详情页超过 30 秒窗口")
        except (OSError, URLError, TimeoutError, HTTPException):
            _fail("connection_failed", "公开详情页读取失败或超时，未自动重试")
        return self.parse_html(query_url, bytes(raw).decode("utf-8", errors="replace"))[:max(0, min(limit, 8))]

    def parse_html(self, page_url, html):
        """纯解析也用于离线检查；未准入来源的结果始终标记不可入库。"""
        source, parsed = _page(page_url)
        if not isinstance(html, str) or len(html.encode("utf-8")) > MAX_HTML_BYTES:
            _fail("html_too_large", "公开详情页超过 8 MiB 上限")
        self.last_rejections = []
        if re.search(r"<title[^>]*>\s*(?:安全验证|验证码拦截|Just a moment\.\.\.)\s*</title", html, re.IGNORECASE):
            _fail("access_challenge", "来源返回访问验证页，不绕过登录或验证码")
        if re.search(r'<input\b[^>]*\btype\s*=\s*[\"\']?password\b', html, re.IGNORECASE):
            _fail("access_challenge", "来源页面包含登录门槛，不使用登录态或绕过访问限制")
        parser = _Page()
        parser.feed(html)
        nodes = [node for document in parser.scripts for node in _walk(document)]
        objects = [node for node in nodes if node.get("@type") == "VideoObject"
                   or isinstance(node.get("@type"), list) and "VideoObject" in node["@type"]]
        if not objects:
            _fail("missing_video_metadata", "详情页没有受支持的 VideoObject；播放器尺寸及 HD 标签不作为原文件尺寸")
        values, seen = [], set()
        for obj in objects:
            try:
                value = self._candidate(page_url, source, parsed, obj, nodes)
                if value.id not in seen:
                    values.append(value)
                    seen.add(value.id)
            except PublicPageMediaError as error:
                self.last_rejections.append({"source_id": parsed.path, "reason": error.code})
        if not values:
            reason = self.last_rejections[0]["reason"] if self.last_rejections else "public_page_unsupported_metadata"
            raise PublicPageMediaError(reason, "详情页不能提供当前要求的明确原文件版本；原因：" + reason)
        return values

    def _candidate(self, page_url, source, parsed, obj, nodes):
        if source == "public":
            return self._public_candidate(page_url, parsed, obj, nodes)
        url = obj.get("contentUrl")
        media = _media(url, source)
        if source == "hubble" and Path(media.path).stem.casefold() != parsed.path.strip("/").split("/")[-1].casefold():
            _fail("unbound_version", "原文件标识与当前 Hubble 视频条目不一致")
        title = str(obj.get("name") or "").strip()
        if not title:
            _fail("missing_title", "缺少具体视频标题")
        metadata, duration, author = obj, obj.get("duration"), obj.get("author")
        if source == "coverr":
            records = [node for node in nodes if node.get("slug") == parsed.path.strip("/").split("/")[-1]
                       and isinstance(node.get("defaultVariant"), dict) and node.get("title") == title]
            versions = [(record, version) for record in records for version in record["defaultVariant"].get("renditions", [])
                        if isinstance(version, dict) and version.get("url") == url]
            if not versions:
                _fail("unbound_version", "未找到同一条目及原文件 URL 对应的版本参数")
            record, metadata = versions[0]
            if record.get("isPremium") is not False or metadata.get("isPlus") is not False:
                _fail("premium_version", "该公开文件版本没有明确免费标记，需采用正式来源配置")
            duration, author = record.get("duration"), record.get("contributor")
        width, height = metadata.get("width"), metadata.get("height")
        if type(width) is not int or type(height) is not int or not 0 < min(width, height) <= max(width, height) <= 32768:
            _fail("missing_dimensions", "缺少绑定该原文件的编码宽高，不从播放器或分辨率名称推测")
        if ((self.orientation == "portrait" and height <= width) or (self.orientation == "landscape" and width <= height)
                or (self.orientation == "square" and width != height)):
            _fail("orientation_mismatch", "原文件画幅不符合项目要求，不自动裁切或转横版")
        milliseconds = _duration(duration)
        if milliseconds is None:
            _fail("missing_duration", "缺少该视频的明确时长")
        size = metadata.get("size")
        if size is not None and (type(size) is not int or not 0 < size <= MAX_VIDEO_BYTES):
            _fail("file_too_large", "原文件版本超过下载上限或大小字段无效")
        author = _author(author) or ("Coverr（页面未公开作者）" if source == "coverr" else "ESA/Hubble（页面未公开完整署名）")
        if source == "hubble" and "未公开完整署名" in author:
            _fail("missing_credit", "ESA/Hubble 要求完整可见署名，结构化数据未提供完整署名")
        version = sha256(json.dumps({"url":media._replace(query="").geturl(),
            "source_version":metadata.get("id") or metadata.get("@id"),
            "revision":metadata.get("updatedAt") or metadata.get("uploadDate"),
            "width":width, "height":height, "size":size}, sort_keys=True).encode()).hexdigest()
        source_id = f"{source}:{parsed.path}:{version}"
        admitted = source != "coverr" or self.coverr_admitted
        return ExternalVideo(id=uuid5(NAMESPACE_URL, "public_page:" + source_id), provider=self.provider_name,
            source_id=source_id, title=title, description=str(obj.get("description") or title), page_url=page_url,
            download_url=url, mime={".mov":"video/quicktime", ".m4v":"video/x-m4v"}.get(Path(media.path).suffix.lower(), "video/mp4"),
            size_bytes=size, width=width, height=height, sha1=None, duration_ms=milliseconds, author=author,
            license_name="Coverr License" if source == "coverr" else "CC BY 4.0（ESA/Hubble 可见完整署名）",
            license_url="https://coverr.co/license" if source == "coverr" else "https://esahubble.org/copyright/",
            attribution=f"{title} — {author} · {page_url}", admissible=admitted,
            admission_reason=("来源级许可已配置；下载后核验哈希、实际尺寸与用途，不代表质量通过" if admitted
                else "仅技术解析；Coverr 免费网页供给尚未在产品来源级准入，不进入下载与入库"))

    def _public_candidate(self, page_url, parsed, obj, nodes):
        if obj.get("isAccessibleForFree") not in (None, True) or obj.get("requiresSubscription") not in (None, False):
            _fail("premium_version", "公开元数据标记需订阅或付费，不绕过访问限制")
        url = obj.get("contentUrl")
        media = _media(url, "public")
        title = str(obj.get("name") or "").strip()[:500]
        if not title:
            _fail("missing_title", "缺少具体视频标题")
        # 编码信息只能来自与 contentUrl 一致的 encoding，不采用播放器宽高。
        encodings = obj.get("encoding", [])
        if isinstance(encodings, dict):
            encodings = [encodings]
        metadata = next((item for item in encodings if isinstance(item, dict)
            and item.get("contentUrl") == url), {}) if isinstance(encodings, list) else {}
        if metadata.get("isAccessibleForFree") not in (None, True) or metadata.get("requiresSubscription") not in (None, False):
            _fail("premium_version", "该原文件版本需要订阅或付费")
        author_value = obj.get("author") or obj.get("creator")
        if isinstance(author_value, dict) and set(author_value) == {"@id"}:
            author_value = next((node for node in nodes if node.get("@id") == author_value["@id"]
                and node.get("name")), None)
        author = _author(author_value)[:2000]
        if not author or re.match(r"https?://", author):
            _fail("missing_credit", "原文件缺少明确作者署名")
        license_name, license_url = _public_license(metadata.get("license") or obj.get("license"))
        width, height = metadata.get("width"), metadata.get("height")
        exact = (type(width) is int and type(height) is int
            and 0 < min(width, height) <= max(width, height) <= 32768)
        if exact:
            if min(width, height) < 720:
                _fail("insufficient_resolution", "原文件短边低于 720，不作为当前视频素材")
            _check_orientation(width, height, self.orientation)
        else:
            width, height = 0, 0
        size = metadata.get("contentSize") or metadata.get("size")
        if isinstance(size, str) and size.isdigit():
            size = int(size)
        if size is not None and (type(size) is not int or not 0 < size <= MAX_VIDEO_BYTES):
            _fail("file_too_large", "原文件大小字段不明确或超过下载上限")
        suffix = Path(media.path).suffix.lower()
        mime = {".mp4":"video/mp4", ".mov":"video/quicktime", ".m4v":"video/x-m4v",
                ".webm":"video/webm", ".ogv":"video/ogg", ".ogg":"video/ogg"}.get(suffix)
        declared_mime = metadata.get("encodingFormat") or obj.get("encodingFormat")
        if mime is None and declared_mime in {"video/mp4", "video/quicktime", "video/x-m4v", "video/webm", "video/ogg"}:
            mime = declared_mime
        if mime is None or re.search(r"(?:^|[/_.-])(?:preview|thumbnail)(?:[/_.-]|$)", media.path, re.IGNORECASE):
            _fail("unsupported_media", "contentUrl 不是受支持的明确原视频文件")
        milliseconds = _duration(metadata.get("duration") or obj.get("duration"))
        version = sha256(json.dumps({"page":page_url, "url":media._replace(query="").geturl(),
            "source_version":metadata.get("@id") or obj.get("@id"),
            "revision":metadata.get("uploadDate") or obj.get("uploadDate"), "width":width, "height":height,
            "size":size, "duration_ms":milliseconds, "license":license_url}, sort_keys=True).encode()).hexdigest()
        source_id = f"public:{parsed.hostname}:{version}"
        return ExternalVideo(id=uuid5(NAMESPACE_URL, "public_page:" + source_id), provider=self.provider_name,
            source_id=source_id, title=title, description=str(obj.get("description") or title)[:5000],
            page_url=page_url, download_url=url, mime=mime, size_bytes=size, width=width, height=height,
            dimensions_exact=exact, duration_ms=milliseconds, author=author, license_name=license_name,
            license_url=license_url, attribution=f"{title} — {author} · {license_name} · {page_url} · {license_url}",
            admissible=True, admission_reason="明确原文件与开放许可已登记；下载后核验真实画幅、分辨率和哈希，用途另行核验")

    def download(self, candidate, destination: Path, *, max_bytes=MAX_VIDEO_BYTES, deadline=None):
        source = self.check_source(candidate.page_url)
        if candidate.provider != self.provider_name or not candidate.admissible:
            _fail("not_admitted", "请使用已保存且来源级准入的公开视频记录")
        _media(candidate.download_url, source)
        size, created, start = 0, False, time.monotonic()
        max_bytes = min(max_bytes, MAX_VIDEO_BYTES)
        deadline = min(deadline if deadline is not None else start + 90, start + 90)
        try:
            with self._open(candidate.download_url, source=source, media=True, deadline=deadline) as response:
                length = response.headers.get("Content-Length")
                mime = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if response.status != 200 or mime not in {candidate.mime, "application/octet-stream"}:
                    _fail("not_original_file", "来源未返回完整视频文件响应")
                if length and (not length.isdigit() or not 0 < int(length) <= max_bytes):
                    _fail("file_too_large", "原文件超出当前下载大小限制")
                with destination.open("xb") as output:
                    created = True
                    while chunk := response.read(256 * 1024):
                        size += len(chunk)
                        if size > max_bytes or time.monotonic() > deadline:
                            _fail("download_limit", "原文件下载超过大小或时间限制")
                        output.write(chunk)
            if not size or length and size != int(length):
                _fail("incomplete_file", "原文件下载不完整")
            if candidate.size_bytes is not None and size != candidate.size_bytes:
                _fail("version_changed", "下载文件大小与保存的来源版本不一致，请刷新来源记录")
        except BaseException as error:
            if created:
                destination.unlink(missing_ok=True)
            if isinstance(error, (OSError, URLError, TimeoutError, HTTPException)):
                _fail("connection_failed", "原文件读取失败或超时，未自动重试")
            raise


def _duration(value):
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, str) and value.startswith("PT"):
            match = re.fullmatch(r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", value)
            if not match or not any(match.groups()):
                return None
            seconds = sum(float(part or 0) * scale for part, scale in zip(match.groups(), (3600, 60, 1)))
        else:
            seconds = float(value)
        return round(seconds * 1000) if math.isfinite(seconds) and 0 < seconds <= 86400 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _author(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("name") or "").strip()
    if isinstance(value, list):
        return ", ".join(filter(None, (_author(item) for item in value)))
    return ""


def _check_orientation(width, height, orientation):
    if ((orientation == "portrait" and height <= width) or (orientation == "landscape" and width <= height)
            or (orientation == "square" and width != height)):
        _fail("orientation_mismatch", "原文件画幅不符合项目要求，不自动裁切或转横版")


def _public_license(value):
    """只读该 VideoObject/原文件自己的明确许可，不继承网页整体许可。"""
    for entry in value if isinstance(value, list) else [value]:
        url = (entry.get("@id") or entry.get("url")) if isinstance(entry, dict) else entry
        try:
            parsed = urlsplit(url)
            if (parsed.scheme not in {"http", "https"} or parsed.hostname != "creativecommons.org"
                    or parsed.port not in (None, 80, 443) or parsed.username or parsed.password):
                continue
            path = re.sub(r"/(?:deed\.[a-zA-Z_-]+|legalcode)/?$", "/", parsed.path)
            match = re.fullmatch(r"/licenses/by/(1\.0|2\.0|2\.5|3\.0|4\.0)/?", path)
            if match:
                return "CC BY " + match[1], "https://creativecommons.org/licenses/by/" + match[1] + "/"
            if path.rstrip("/") == "/publicdomain/zero/1.0":
                return "CC0 1.0", "https://creativecommons.org/publicdomain/zero/1.0/"
            if path.rstrip("/") == "/publicdomain/mark/1.0":
                return "Public Domain Mark 1.0", "https://creativecommons.org/publicdomain/mark/1.0/"
        except (TypeError, ValueError, AttributeError):
            pass
    _fail("unsupported_license", "原文件没有明确 CC0、公共领域标记或 CC BY 许可，不从模型或网页整体文字推测")

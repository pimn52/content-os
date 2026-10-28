"""Commons video catalogue and bounded public downloads; no creator credentials."""
from __future__ import annotations

import hashlib
import json
import re
import time
from html import unescape
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import NAMESPACE_URL, uuid5

from decimal import Decimal
from app.domain.external_media import ExternalVideo
from app.domain.models import CostCategory, UsageCost


MAX_VIDEO_BYTES = 128 * 1024 * 1024
API = "https://commons.wikimedia.org/w/api.php"


class ExternalMediaError(RuntimeError):
    code = "external_media_failed"

def _text(value: object, limit: int = 2000) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", str(value or "")))).strip()[:limit]


def _safe_url(url: str, *, media: bool = False) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ExternalMediaError("外部素材地址不受支持") from None
    allowed = {"upload.wikimedia.org"} if media else {"commons.wikimedia.org"}
    if (parsed.scheme != "https" or parsed.hostname not in allowed or port not in (None, 443)
            or parsed.username or parsed.password or parsed.fragment
            or any(ord(c) < 33 for c in url)):
        raise ExternalMediaError("外部素材地址不受支持")
    return parsed._replace(query="").geturl() if media else url


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CommonsMediaProvider:
    provider_name = "wikimedia_commons"
    model = "public-video-catalogue"

    def __init__(self, *, opener: Callable | None = None):
        self._opener = opener or build_opener(_NoRedirect()).open

    def _open(self, url: str, *, media: bool = False):
        for _ in range(4):
            url = _safe_url(url, media=media)
            try:
                headers = {"User-Agent": "ContentOS/0.1 (local creator tool; on-demand public media)"}
                if not media:
                    headers["Accept"] = "application/json"
                return self._opener(Request(url, headers=headers), timeout=30)
            except HTTPError as exc:
                if exc.code in {301, 302, 303, 307, 308} and exc.headers.get("Location"):
                    url = urljoin(url, exc.headers["Location"])
                    exc.close()
                    continue
                status = exc.code
                exc.close()
                stage = "下载" if media else "目录"
                retry = exc.headers.get("Retry-After", "")
                recovery = f"请在 {retry} 秒后手动重试" if retry.isdigit() else "请稍后手动重试"
                error = ExternalMediaError(f"外部素材{stage}服务请求失败（HTTP {status}），{recovery}")
                error.code = f"external_media_{'download' if media else 'catalogue'}_http_{status}"
                if status == 429 and retry.isdigit() and len(retry) <= 10:
                    error.retry_after_seconds = int(retry)
                raise error from None
            except (URLError, TimeoutError, OSError):
                raise ExternalMediaError("外部素材服务连接失败，请稍后手动重试") from None
        raise ExternalMediaError("外部素材地址重定向过多")

    def _query(self, params: dict) -> list[ExternalVideo]:
        query = {"action": "query", "prop": "imageinfo", "iiprop": "url|size|mime|sha1|extmetadata",
                 "format": "json", "formatversion": 2, **params}
        with self._open(API + "?" + urlencode(query)) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ExternalMediaError("外部检索响应过大")
        try:
            body = json.loads(raw)
            if "error" in body:
                raise ValueError()
            pages = body.get("query", {}).get("pages", [])
            return [value for page in pages if (value := self._candidate(page)) is not None]
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ExternalMediaError("外部素材目录响应无效") from None

    def cost(self, currency: str) -> UsageCost:
        return UsageCost(category=CostCategory.STOCK, amount=Decimal("0"), currency=currency,
                         provider=self.provider_name, note="Public Commons API/download; no provider charge.")

    def search(self, query: str, *, limit: int = 8) -> list[ExternalVideo]:
        # Quotes keep user keywords from changing the namespace/file-type constraint.
        words = re.sub(r'["\\<>|{}\[\]:]', " ", query).strip()
        if len(words) < 2:
            raise ExternalMediaError("请输入具体画面关键词")
        return self._query({"generator": "search", "gsrnamespace": 6, "gsrlimit": min(limit, 12),
                            "gsrsearch": f'"{words}" filetype:video'})

    @staticmethod
    def _candidate(page: dict) -> ExternalVideo | None:
        infos = page.get("imageinfo") or []
        if not infos:
            return None
        info = infos[0]
        mime = info.get("mime", "")
        if not (mime.startswith("video/") or mime == "application/ogg"):
            return None
        metadata = info.get("extmetadata", {})
        field = lambda name: _text((metadata.get(name) or {}).get("value"))
        license_name, license_url, author = field("LicenseShortName"), field("LicenseUrl"), field("Artist")
        if license_url.startswith("//"):
            license_url = "https:" + license_url
        cc = urlsplit(license_url)
        if cc.hostname == "creativecommons.org" and cc.scheme in {"http", "https"}:
            path = re.sub(r"/(?:deed\.[a-zA-Z_-]+|legalcode)/?$", "/", cc.path)
            license_url = cc._replace(scheme="https", path=path, query="", fragment="").geturl()
        else:
            license_url = ""
        cc = urlsplit(license_url)
        attribution_license = (cc.hostname == "creativecommons.org" and cc.scheme == "https"
                               and re.fullmatch(r"/licenses/by/(1\.0|2\.0|2\.5|3\.0|4\.0)/?", cc.path))
        public_domain = ((field("Copyrighted").casefold() == "false" and license_name.casefold() == "public domain")
                         or cc.hostname == "creativecommons.org" and cc.scheme == "https"
                         and cc.path.rstrip("/") in {"/publicdomain/zero/1.0", "/publicdomain/mark/1.0"})
        rights_ok = bool(public_domain or attribution_license and author)
        size = int(info.get("size", 0))
        restrictions = field("Restrictions")
        url = _safe_url(info["url"], media=True)
        page_url = _safe_url(info.get("descriptionurl") or
                             "https://commons.wikimedia.org/wiki/" + str(page["title"]).replace(" ", "_"))
        sha1 = info.get("sha1", "")
        if not re.fullmatch(r"[0-9a-f]{40}", sha1):
            return None
        if not license_url and public_domain:
            license_url = "https://commons.wikimedia.org/wiki/Commons:Public_domain"
        admissible = rights_ok and not restrictions and 0 < size <= MAX_VIDEO_BYTES
        reason = ("许可明确，入库后自动继承" if admissible else "文件超过 128 MiB，请选择较短素材" if size > MAX_VIDEO_BYTES
                  else "此文件存在额外用途限制" if restrictions else "此许可暂未接通发布规则，请选择公共领域、CC0 或 CC BY")
        title = _text(page["title"], 500)
        attribution = f"{title} — {author or 'Wikimedia Commons'} · {license_name} · {page_url} · {license_url}"
        return ExternalVideo(id=uuid5(NAMESPACE_URL, f"commons:{page['pageid']}:{sha1}"), provider="wikimedia_commons", source_id=str(page["pageid"]),
            title=title, description=field("ImageDescription"), page_url=page_url, download_url=url,
            mime=mime, size_bytes=size, width=int(info.get("width", 0)), height=int(info.get("height", 0)),
            sha1=sha1, author=author, license_name=license_name, license_url=license_url,
            attribution=attribution, admissible=admissible, admission_reason=reason)

    def download(self, candidate: ExternalVideo, destination: Path, *, max_bytes=MAX_VIDEO_BYTES, deadline=None) -> None:
        if not candidate.admissible:
            raise ExternalMediaError(candidate.admission_reason)
        max_bytes = min(max_bytes, MAX_VIDEO_BYTES)
        deadline = min(deadline if deadline is not None else time.monotonic()+180, time.monotonic()+180)
        digest, size = hashlib.sha1(), 0
        try:
            with self._open(candidate.download_url, media=True) as response, destination.open("xb") as output:
                header_size = response.headers.get("Content-Length")
                if header_size and int(header_size) > max_bytes:
                    raise ExternalMediaError("外部素材超过下载大小限制")
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes or time.monotonic() > deadline:
                        raise ExternalMediaError("外部素材下载超过大小或时长限制")
                    digest.update(chunk)
                    output.write(chunk)
            if size != candidate.size_bytes or digest.hexdigest() != candidate.sha1:
                raise ExternalMediaError("素材内容与目录版本不符，请重新搜索")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

"""Pexels 正式视频 API；沿用现有 Key，原文件下载不携带 API 凭据。"""
from decimal import Decimal
from pathlib import Path
from hashlib import sha256
import json
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import NAMESPACE_URL, uuid5

from app.domain.external_media import ExternalVideo
from app.domain.models import CostCategory, UsageCost
from app.providers.commons_media import ExternalMediaError, MAX_VIDEO_BYTES


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _url(value, *, media=False):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        allowed = (host in {'videos.pexels.com', 'player.vimeo.com'} or host.endswith(('.vimeocdn.com', '.akamaized.net'))) if media else host in {'www.pexels.com', 'pexels.com'}
        if (parsed.scheme != 'https' or not allowed or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment or any(ord(c) < 33 for c in value)):
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise ExternalMediaError('Pexels 素材地址不受支持') from None


def page_video_id(value):
    """只从明确的官方视频详情页取 ID；不请求网页、不猜媒体地址。"""
    try:
        parsed = urlsplit(value)
        if (not isinstance(value, str) or len(value) > 1000 or parsed.scheme != 'https'
                or parsed.hostname not in {'www.pexels.com', 'pexels.com'} or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(ord(c) < 33 for c in value)):
            raise ValueError()
        path = re.fullmatch(r'/(?:[a-z]{2}(?:-[a-z]{2})?/)?video/([^/]+)/?', parsed.path)
        if path is None:
            raise ValueError()
        slug = unquote(path.group(1), errors='strict')
        identity = re.fullmatch(r'(?:[\w-]+-)?([1-9][0-9]{0,17})', slug)
        if identity is None:
            raise ValueError()
        return int(identity.group(1))
    except (ValueError, TypeError, AttributeError, UnicodeError):
        error = ExternalMediaError('请选择不含查询参数或片段的 Pexels 视频详情页，目录和不可信地址不支持')
        error.code = 'pexels_invalid_video_url'
        raise error from None


def _page_query(query):
    return bool(re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:|^//|^[^/\s]+\.[^/\s]+(?:/|$)', query))


class PexelsMediaProvider:
    provider_name = 'pexels'
    model = 'video-api-v1'
    cache_ttl_seconds = 86400
    quota_reset_kind = 'epoch'

    def __init__(self, api_key=None, *, orientation='portrait', opener=None):
        if api_key is not None and (not api_key or len(api_key.split()) != 1 or any(ord(c) < 33 for c in api_key)):
            raise ExternalMediaError('Pexels Key 文件须只包含密钥')
        if orientation not in ('portrait', 'landscape', 'square'):
            raise ValueError('unsupported orientation')
        self._api_key, self.orientation = api_key, orientation
        self.search_scope = sha256((api_key or '').encode()).hexdigest() + ':' + orientation
        self.last_quota = {}
        self.last_rejections = []
        self._opener = opener or build_opener(_NoRedirect()).open

    def cost(self, currency):
        return UsageCost(category=CostCategory.STOCK, amount=Decimal('0'), currency=currency,
            provider=self.provider_name, note='Pexels 免费视频 API 与下载；仍遵守账号限额。')

    def search_context(self, query):
        # locale 描述查询语言，不能把英文查询固定按中文解释；详情 ID 不需要语言参数。
        return {} if _page_query(query) else {'locale': 'zh-CN' if re.search(r'[\u3400-\u9fff]', query) else 'en-US'}

    def _open(self, url, *, catalogue=False):
        for _ in range(4):
            if catalogue:
                if urlsplit(url).hostname != 'api.pexels.com' or not self._api_key:
                    raise ExternalMediaError('请先配置 Pexels Key 文件')
                headers = {'Authorization': self._api_key, 'Accept': 'application/json'}
            else:
                _url(url, media=True)
                headers = {}
            try:
                return self._opener(Request(url, headers={**headers, 'User-Agent': 'ContentOS/0.1',
                    'Accept-Encoding': 'identity'}), timeout=30)
            except HTTPError as error:
                status, location, retry = error.code, error.headers.get('Location'), error.headers.get('Retry-After')
                reset = error.headers.get('X-Ratelimit-Reset')
                error.close()
                if not catalogue and status in (301,302,303,307,308) and location:
                    url = urljoin(url, location)
                    continue
                message = 'Pexels 请求失败（HTTP ' + str(status) + '），未自动重试'
                if status == 429:
                    message += '；账号已限流' + ('，请在 ' + retry + ' 秒后手动恢复' if retry and retry.isdigit() else '')
                exc = ExternalMediaError(message)
                exc.code = f'pexels_http_{status}'
                if status == 429:
                    if retry and retry.isdigit() and len(retry) <= 10:
                        exc.retry_after_seconds = int(retry)
                    elif reset and reset.isdigit() and len(reset) <= 12:
                        exc.retry_after_seconds = max(0, math.ceil(int(reset)-time.time()))
                raise exc from None
            except (URLError, OSError, TimeoutError):
                raise ExternalMediaError('Pexels 连接失败或超时，未自动重试') from None
        raise ExternalMediaError('Pexels 下载重定向过多')

    def search(self, query, *, limit=8):
        if not isinstance(query, str) or not query.strip():
            raise ExternalMediaError('请输入具体画面关键词')
        query = query.strip()
        self.last_quota, self.last_rejections = {}, []
        # URL 只作确切身份定位，不能把目录/错误 URL 当成关键词再次搜索。
        is_page = _page_query(query)
        video_id = page_video_id(query) if is_page else None
        if video_id is None:
            if len(query) > 200:
                raise ExternalMediaError('请输入具体画面关键词')
            params = {'query': query, 'per_page': min(max(limit, 1), 12),
                'orientation': self.orientation, **self.search_context(query)}
            endpoint = 'https://api.pexels.com/v1/videos/search?' + urlencode(params)
        else:
            endpoint = f'https://api.pexels.com/v1/videos/videos/{video_id}'
        with self._open(endpoint, catalogue=True) as response:
            if response.status != 200:
                raise ExternalMediaError('Pexels 视频目录未返回完整 HTTP 200 响应')
            raw = response.read(4 * 1024 * 1024 + 1)
            self.last_quota = {name: response.headers.get(header) for name,header in
                [('limit','X-Ratelimit-Limit'),('remaining','X-Ratelimit-Remaining'),('reset','X-Ratelimit-Reset')]
                if response.headers.get(header) is not None}
        if len(raw) > 4 * 1024 * 1024:
            raise ExternalMediaError('Pexels 检索响应过大')
        try:
            document = json.loads(raw)
            if not isinstance(document, dict):
                raise ValueError()
            if video_id is None:
                rows = document['videos']
                if not isinstance(rows, list):
                    raise ValueError()
            else:
                if type(document.get('id')) is not int or document['id'] != video_id:
                    error = ExternalMediaError('Pexels 返回的视频 ID 与指定详情页不一致，不选用其他视频')
                    error.code = 'pexels_video_id_mismatch'
                    raise error
                if page_video_id(document['url']) != video_id:
                    error = ExternalMediaError('Pexels 返回的详情页与指定视频 ID 不一致')
                    error.code = 'pexels_video_id_mismatch'
                    raise error
                rows = [document]
        except (ValueError, TypeError, KeyError):
            raise ExternalMediaError('Pexels 视频目录响应无效') from None
        values = []
        self.last_rejections = []
        for row in rows:
            if not isinstance(row, dict):
                self.last_rejections.append({'source_id': '', 'reason': 'invalid_source_record'})
                continue
            try:
                value = self._candidate(row)
                if value is not None:
                    source_note = '\n来源详情定位（非画面核验）：' if video_id is not None else '\n检索线索（非画面核验）：'
                    value = value.model_copy(update={'description': value.description + source_note + query})
                    values.append(value)
                else:
                    self.last_rejections.append({'source_id': str(row.get('id', ''))[:100], 'reason': 'no_matching_hd_version'})
            except (ValueError, TypeError, KeyError, ExternalMediaError):
                # 隔离异常条目，不把图片预览、未知版本或不受信任地址当成视频。
                self.last_rejections.append({'source_id': str(row.get('id', ''))[:100] if isinstance(row, dict) else '',
                    'reason': 'invalid_source_record'})
                continue
        return values

    def _candidate(self, row):
        files = []
        for file in row.get('video_files', []):
            if not isinstance(file, dict) or file.get('file_type') != 'video/mp4':
                continue
            w, h = file.get('width'), file.get('height')
            if type(w) is not int or type(h) is not int or min(w,h) < 720:
                continue
            if self.orientation == 'portrait' and h <= w or self.orientation == 'landscape' and w <= h or self.orientation == 'square' and w != h:
                continue
            try:
                _url(file['link'], media=True)
                if type(file.get('id')) is not int:
                    continue
            except (KeyError, ExternalMediaError):
                continue
            files.append(file)
        if not files:
            return None
        # 够用时优先 Full HD，避免仅因 4K 标签增加下载时间；不足时选最好的 HD。
        full_hd = [f for f in files if min(f['width'],f['height']) >= 1080]
        chosen = min(full_hd, key=lambda f:f['width']*f['height']) if full_hd else max(files, key=lambda f:f['width']*f['height'])
        video_id, file_id = row['id'], chosen['id']
        if type(video_id) is not int:
            return None
        page = _url(row['url'])
        author = str((row.get('user') or {}).get('name') or 'Pexels')[:500]
        title = row.get('title') or urlsplit(page).path.rstrip('/').rsplit('/',1)[-1].replace('-', ' ')
        title = str(title)[:500]
        seconds = row.get('duration')
        duration = round(seconds*1000) if type(seconds) in (int,float) and math.isfinite(seconds) and seconds > 0 else None
        return ExternalVideo(id=uuid5(NAMESPACE_URL, f'pexels:{video_id}:{file_id}'), provider='pexels',
            source_id=f'{video_id}:{file_id}', title=title, description='Pexels 视频目录：' + title,
            page_url=page, download_url=chosen['link'], mime='video/mp4', size_bytes=None,
            width=chosen['width'], height=chosen['height'], sha1=None, duration_ms=duration,
            author=author, license_name='Pexels License', license_url='https://www.pexels.com/license/',
            attribution=f'{title} — {author} · Pexels · {page}', admissible=True,
            admission_reason='沿用 Pexels 来源许可；入库后核验实际内容、尺寸及用途，不代表视觉质量通过')

    def download(self, candidate, destination: Path, *, max_bytes=MAX_VIDEO_BYTES, deadline=None):
        if candidate.provider != 'pexels' or not candidate.admissible:
            raise ExternalMediaError('请使用已保存的 Pexels 视频条目')
        size, start = 0, time.monotonic()
        max_bytes = min(max_bytes, MAX_VIDEO_BYTES)
        deadline = min(deadline if deadline is not None else start+90, start+90)
        try:
            with self._open(candidate.download_url) as response, destination.open('xb') as output:
                if response.status != 200:
                    raise ExternalMediaError('Pexels 未返回完整文件响应')
                mime = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
                length = response.headers.get('Content-Length')
                if mime not in ('video/mp4','application/octet-stream'):
                    raise ExternalMediaError('Pexels 未返回视频原文件')
                if length and (not length.isdigit() or not 0 < int(length) <= max_bytes):
                    raise ExternalMediaError('Pexels 文件大小超过下载限制')
                while chunk := response.read(256 * 1024):
                    size += len(chunk)
                    if size > max_bytes or time.monotonic() > deadline:
                        raise ExternalMediaError('Pexels 下载超过大小或 90 秒时限')
                    output.write(chunk)
            if size == 0 or length and size != int(length):
                raise ExternalMediaError('Pexels 文件下载不完整')
        except (OSError, URLError, TimeoutError):
            destination.unlink(missing_ok=True)
            raise ExternalMediaError('Pexels 文件读取失败或超时，未自动重试') from None
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

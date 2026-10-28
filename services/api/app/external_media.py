"""Search and admit external footage through the existing import/job/usage path."""
from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel

from app.costs import CatalogProviderCostEstimator
from app.db import AssetRepository, BudgetPolicyRepository, Database, ProviderCallRepository
from app.domain.external_media import ExternalVideo
from app.domain.models import Asset, CostCategory, Job, JobStatus, JobType, SourceKind, UsageCost
from app.jobs.targets import AssetJobTargetStore
from app.media.importer import MediaImporter
from app.media.local_paths import existing_local_media_file
from app.provider_execution import ProviderExecutionService, RuntimeProviderIdentity
from app.provider_execution import ProviderExecutionIdempotencyConflict, ProviderExecutionInProgress, ProviderExecutionRecordedFailure
from app.budget import provider_call_input_digest
from app.providers.commons_media import ExternalMediaError


class ExternalMediaProvider(Protocol):
    provider_name: str
    model: str
    def cost(self, currency: str) -> UsageCost: ...
    def search(self, query: str, *, limit: int = 8) -> list[ExternalVideo]: ...
    def download(self, candidate: ExternalVideo, destination: Path) -> None: ...


class ExternalMediaInputError(ValueError):
    pass


class ExternalSearchResult(BaseModel):
    call_id: UUID
    query: str
    candidates: list[ExternalVideo]
    provider: str | None = None
    cached: bool = False


class ExternalImportResult(BaseModel):
    asset_id: UUID
    analysis_job_id: UUID
    attribution: str


class ExternalMediaService:
    def __init__(self, db: Database, root: Path, provider: ExternalMediaProvider, importer: MediaImporter, *, reservation_guard=None):
        self.db, self.root, self.provider, self.importer = db, root, provider, importer
        self.assets, self.calls = AssetRepository(db), ProviderCallRepository(db)
        self.reservation_guard = reservation_guard

    def _execute(self, project_id: UUID, key: str, operation: str, document, action, encode, decode):
        policies = BudgetPolicyRepository(self.db)
        policy = policies.get_by_project(project_id) or policies.get_by_project(None)
        currency = policy.currency if policy else "USD"
        price = self.provider.cost(currency)
        identity = RuntimeProviderIdentity(self.provider.provider_name, self.provider.model)
        estimator = CatalogProviderCostEstimator({(operation, identity.provider, identity.model): price})
        def guard():
            # 在现有调用预留事务内检查；缓存和已完成请求不经过此处。
            if operation == "stock_search":
                self._check_search_cooldown()
            if self.reservation_guard is not None:
                self.reservation_guard(operation)
        return ProviderExecutionService(self.db, estimator).execute(
            project_id=project_id, idempotency_key=key, operation=operation,
            provider=identity, input_source="external_media", category=CostCategory.STOCK,
            input_document=document, action=action, encode_result=encode, decode_result=decode,
            error_code=lambda error: self._recorded_error_code(error, operation), reservation_guard=guard)

    @staticmethod
    def _account_scope(connection_scope):
        # 同一个账号不同画幅共享来源配额；完整条件仍用于搜索缓存。
        scope = str(connection_scope) if connection_scope is not None else "public"
        account, separator, orientation = scope.rpartition(":")
        return account if separator and orientation in {"portrait", "landscape", "square"} else scope

    def _account_hash(self):
        scope = self._account_scope(getattr(self.provider, "search_scope", None))
        return hashlib.sha256((self.provider.provider_name + ":" + scope).encode()).hexdigest()[:32]

    def _recorded_error_code(self, error, operation):
        code = str(getattr(error, "code", "external_media_failed"))
        if operation != "stock_search" or not code.endswith("http_429"):
            return code[:100]
        retry = getattr(error, "retry_after_seconds", None)
        # 来源未给窗口时短暂冷却，避免新任务连续撞同一个限流；不重试原请求。
        seconds = retry if type(retry) in (int, float) and math.isfinite(retry) and 0 <= retry <= 10**10 else 60
        until = math.ceil(datetime.now(timezone.utc).timestamp() + seconds)
        suffix = f":cd:{until}:{self._account_hash()}"
        return code[:100-len(suffix)] + suffix

    def _check_search_cooldown(self):
        """来源余额只作已知窗口内的保守上界；现有账目原子预留并发消耗。"""
        now = datetime.now(timezone.utc).timestamp()
        scope = self._account_scope(getattr(self.provider, "search_scope", None))
        account_hash = self._account_hash()
        calls = [call for call in self.calls.list()
            if call.operation == "stock_search" and call.provider == self.provider.provider_name]
        known = []
        for call in calls:
            result = (call.result_payload or {}).get("result", {})
            tagged = call.idempotency_key.startswith("external-search:") and call.idempotency_key.endswith(f":quota:{account_hash}")
            matched = (isinstance(result, dict) and call.status == "completed"
                and self._account_scope(result.get("connection_scope")) == scope)
            if tagged or matched:
                known.append(call)
            recorded = re.fullmatch(r".*:cd:(\d{1,12}):([0-9a-f]{32})", call.error_code or "")
            if call.status == "failed" and recorded and recorded[2] == account_hash:
                if call not in known:
                    known.append(call)
                if int(recorded[1]) > now:
                    self._raise_source_cooldown(int(recorded[1])-now)
        observations = sorted((call for call in known if call.status == "completed" and call.completed_at),
            key=lambda call: (call.completed_at, str(call.id)), reverse=True)
        for call in observations:
            result = call.result_payload["result"]
            quota = result.get("quota", {})
            if not isinstance(quota, dict):
                continue
            remaining = self._quota_number(quota.get("remaining"))
            reset = self._quota_number(quota.get("reset"))
            if remaining is None or remaining != int(remaining) or reset is None:
                continue
            reset_kind = getattr(self.provider, "quota_reset_kind", None)
            if reset_kind not in {"epoch", "seconds"}:
                continue
            until = reset if reset_kind == "epoch" else call.completed_at.timestamp()+reset
            if until <= now:
                return  # 已知窗口已结束，新窗口余额未知，不伪造新的配额。
            # 请求开始至响应期间的并发调用也可能尚未反映在该余额里。
            # 无新余额的失败/完成记录保留消耗；running 记录重启后仍占用，不能自动重发。
            consumed = sum(other.id != call.id and other.status != "cancelled"
                and (other.status in {"reserved", "running"}
                     or other.completed_at is not None and other.completed_at >= call.created_at)
                for other in known)
            if consumed >= remaining:
                self._raise_source_cooldown(until-now)
            return

    @staticmethod
    def _raise_source_cooldown(seconds):
        error = ExternalMediaError("此素材来源正在冷却或已被其他任务预留，可使用缓存或其他已连接来源")
        error.code = "external_source_cooldown"
        error.retry_after_seconds = math.ceil(seconds)
        raise error

    @staticmethod
    def _quota_number(value):
        if isinstance(value, str) and value.isdigit() and len(value) <= 12:
            return int(value)
        if type(value) in (int, float) and 0 <= value <= 10**12 and math.isfinite(value):
            return value
        return None

    def search(self, project_id: UUID, query: str, key: str, *, refresh: bool = False) -> ExternalSearchResult:
        query = " ".join(query.split())
        scope = getattr(self.provider, "search_scope", None)
        context = self.provider.search_context(query) if callable(getattr(self.provider, "search_context", None)) else {}
        document = {"query": query, **({"connection_scope": scope} if scope else {}), **context}
        ttl = getattr(self.provider, "cache_ttl_seconds", 0)
        digest = provider_call_input_digest({"project_id": str(project_id), "operation": "stock_search",
            "provider": self.provider.provider_name, "model": self.provider.model, "input_source": "external_media", "input": document})
        request_digest = provider_call_input_digest({"input": digest, "refresh": refresh})
        alias = self.db.connection.execute("SELECT * FROM external_search_replays WHERE idempotency_key=?", (key,)).fetchone()
        if alias:
            if alias["project_id"] != str(project_id) or alias["input_digest"] != request_digest:
                raise ProviderExecutionIdempotencyConflict("external search request changed")
            return self._search_snapshot(self.calls.get(UUID(alias["call_id"])), query, cached=bool(alias["cache_hit"]))
        own = self.calls.get_by_idempotency_key(key)
        if own is None and ttl and not refresh:
            cutoff = datetime.now(timezone.utc)-timedelta(seconds=ttl)
            cached = next((c for c in reversed(self.calls.list_for_project(project_id))
                if c.input_digest == digest and c.status == "completed" and c.completed_at and c.completed_at >= cutoff), None)
            if cached:
                self._bind_search(key, project_id, request_digest, cached.id, cached=True)
                return self._search_snapshot(cached, query, cached=True)
        execution_key = key
        if own is None:
            # 同一时间窗仍共用执行租约；账号标记使无结果的并发预留可跨重启识别。
            suffix = hashlib.sha256(key.encode()).hexdigest()
            if ttl and not refresh:
                suffix = str(int(datetime.now(timezone.utc).timestamp())//ttl)
            previous_key = f"external-search:{digest}:{suffix}" if ttl else key
            execution_key = (previous_key if self.calls.get_by_idempotency_key(previous_key) is not None
                else f"external-search:{digest}:{suffix}:quota:{self._account_hash()}")
        try:
            values = self._execute(project_id, execution_key, "stock_search", document,
            lambda: self.provider.search(query), lambda values: {"query": query, "provider": self.provider.provider_name,
                "connection_scope": scope, **context,
                "quota": getattr(self.provider, "last_quota", {}), "rejections": getattr(self.provider, "last_rejections", []),
                "candidates": [v.model_dump(mode="json") for v in values]},
            lambda values: [ExternalVideo.model_validate(v) for v in values["candidates"]])
        finally:
            call = self.calls.get_by_idempotency_key(execution_key)
            if call is not None and call.input_digest == digest:
                # 失败和执行中也绑定请求；重启不能把同一请求变成新的网络尝试。
                self._bind_search(key, project_id, request_digest, call.id)
        return ExternalSearchResult(call_id=call.id, query=query, candidates=values, provider=self.provider.provider_name)

    def _bind_search(self, key, project_id, digest, call_id, *, cached=False):
        with self.db.transaction(immediate=True):
            self.db.connection.execute("INSERT OR IGNORE INTO external_search_replays VALUES (?,?,?,?,?,?)",
                (key, str(project_id), digest, str(call_id), datetime.now(timezone.utc).isoformat(), int(cached)))
            bound = self.db.connection.execute("SELECT * FROM external_search_replays WHERE idempotency_key=?", (key,)).fetchone()
            if bound["project_id"] != str(project_id) or bound["input_digest"] != digest or bound["call_id"] != str(call_id):
                raise ProviderExecutionIdempotencyConflict("external search key belongs to another request")

    def _search_snapshot(self, call, query, *, cached):
        if call is None or call.status in {"reserved", "running"}:
            raise ProviderExecutionInProgress("external search is still running")
        if call.status != "completed":
            raise ProviderExecutionRecordedFailure("external search previously failed; explicit refresh required")
        values = (call.result_payload or {}).get("result", {})
        return ExternalSearchResult(call_id=call.id, query=query, provider=call.provider, cached=cached,
            candidates=[ExternalVideo.model_validate(v) for v in values["candidates"]])

    def import_video(self, project_id: UUID, search_call_id: UUID, candidate_id: UUID, key: str, *, download_limits=None) -> ExternalImportResult:
        call = self.calls.get(search_call_id)
        if (call is None or call.project_id != project_id or call.operation != "stock_search"
                or call.status != "completed" or call.provider != self.provider.provider_name):
            raise ExternalMediaInputError("请从当前项目的检索结果选择素材")
        values = [ExternalVideo.model_validate(value) for value in (call.result_payload or {}).get("result", {}).get("candidates", [])]
        candidate = next((v for v in values if v.id == candidate_id), None)
        if candidate is None:
            raise ExternalMediaInputError("素材不在已保存的检索结果中")
        if not candidate.admissible:
            raise ExternalMediaInputError(candidate.admission_reason)
        asset = self._existing(candidate)
        if asset is None:
            asset_id = self._execute(project_id, key, "stock_import",
                {"search_call_id": str(search_call_id), "candidate_id": str(candidate.id)},
                lambda: str(self._download_import(candidate, call.id, download_limits=download_limits).id), str, UUID)
            asset = self.assets.get(UUID(str(asset_id)))
        if asset is None:
            raise ExternalMediaInputError("素材记录不存在，请重新导入")
        self._verify_file(asset)
        now = datetime.now(timezone.utc)
        # An import schedules the same local analysis as Inbox; no separate clip pipeline.
        job = AssetJobTargetStore(self.db).enqueue(Job(
            type=JobType.ANALYZE_ASSET, status=JobStatus.PENDING, attempt=0,
            idempotency_key=f"external-analysis:{asset.id}", project_id=None,
            created_at=now, updated_at=now), asset.id)
        return ExternalImportResult(asset_id=asset.id, analysis_job_id=job.id, attribution=candidate.attribution)

    def _existing(self, candidate: ExternalVideo) -> Asset | None:
        for asset in self.assets.list():
            records = asset.metadata.get("external_sources", [])
            if isinstance(records, list) and any(isinstance(record, dict)
                    and isinstance(record.get("candidate"), dict) and record["candidate"].get("id") == str(candidate.id) for record in records):
                self._verify_file(asset)
                return asset
        return None

    def _verify_file(self, asset: Asset) -> None:
        try:
            path = existing_local_media_file(asset.source_file, self.root)
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        except (OSError, ValueError):
            raise ExternalMediaInputError("已入库素材文件缺失，请修复本地文件后重试") from None
        if digest != asset.content_hash:
            raise ExternalMediaInputError("已入库素材内容变化，请重新导入")

    def _download_import(self, candidate: ExternalVideo, call_id: UUID, *, download_limits=None) -> Asset:
        # The server-owned search snapshot is the admission record. Download
        # verifies its exact content version; do not fetch permission again.
        current = candidate
        directory = self.root / "assets" / "external-downloads"
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f"{uuid4()}.part"
        try:
            self.provider.download(current, temporary, **(download_limits or {}))
            if current.sha1 is None:
                # 无源站摘要时不编造 SHA-1，先核对真实文件几何，再由普通入库计算 SHA-256。
                probe = self.importer.probe.probe(temporary)
                exact = (probe.width, probe.height) == (current.width, current.height)
                unknown_public_geometry = (current.provider == "public_page" and not current.dimensions_exact
                    and current.width == 0 and current.height == 0)
                bounded = (min(probe.width, probe.height) >= 720
                    and probe.width <= current.width and probe.height <= current.height
                    and current.height > 0
                    and abs(probe.width/probe.height-current.width/current.height) <= .04)
                if unknown_public_geometry:
                    orientation = getattr(self.provider, "orientation", "portrait")
                    valid = (min(probe.width, probe.height) >= 720 and max(probe.width, probe.height) <= 32768
                        and (orientation == "any" or orientation == "portrait" and probe.height > probe.width
                            or orientation == "landscape" and probe.width > probe.height
                            or orientation == "square" and probe.width == probe.height))
                    if not valid:
                        raise ExternalMediaInputError("公开原文件的实际画幅或分辨率不符合项目要求，不自动裁切或转横版")
                elif current.dimensions_exact and not exact or not current.dimensions_exact and not bounded:
                    raise ExternalMediaInputError("下载文件尺寸与选定版本不符，请重新检索该来源")
            asset = self.importer.import_path(temporary, current.page_url, source_kind=SourceKind.STOCK)
            with self.db.transaction():
                asset = self.assets.get(asset.id)
                previous = asset.metadata.get("external_sources", [])
                records = list(previous) if isinstance(previous, list) else []
                record = {"candidate": current.model_dump(mode="json"), "search_call_id": str(call_id),
                          "acquired_at": datetime.now(timezone.utc).isoformat(),
                          "content_hash": asset.content_hash, "quality_state": "unassessed",
                          "version_verification": "remote_sha1+local_sha256" if current.sha1 else "provider_file_id+probe+local_sha256",
                          "downloaded_dimensions": {"width": asset.width, "height": asset.height},
                          "content_nature": "unknown"}
                if not any(isinstance(v, dict) and isinstance(v.get("candidate"), dict) and v["candidate"].get("id") == str(current.id) for v in records):
                    records.append(record)
                asset = self.assets.update(asset.model_copy(update={"metadata": {**asset.metadata,
                    "external_sources": records}}))
            return asset
        finally:
            temporary.unlink(missing_ok=True)


def external_catalogue_text(asset: Asset) -> str:
    """Catalogue hints are separate from observed Clip visual metadata and ASR."""
    records = asset.metadata.get("external_sources", [])
    return " ".join(str(candidate.get(field, "")) for record in records if isinstance(record, dict)
                    for candidate in [record.get("candidate", {})] if isinstance(candidate, dict)
                    for field in ("title", "description"))[:5000] if isinstance(records, list) else ""


def attribution_text(assets: list[Asset]) -> str:
    lines: list[str] = []
    for asset in assets:
        records = asset.metadata.get("external_sources", [])
        for record in records if isinstance(records, list) else []:
            if isinstance(record, dict) and isinstance(record.get("candidate"), dict):
                candidate = record["candidate"]
                # One saved source is sufficient for a content-identical asset.
                credit = candidate.get("attribution")
                if isinstance(credit, str) and credit not in lines:
                    lines.append(credit + " · 本片可能节选、剪辑、缩放或叠加字幕")
                break
    return "素材来源与署名\n\n" + "\n\n".join(lines) + "\n" if lines else ""

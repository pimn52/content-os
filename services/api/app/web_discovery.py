"""网页发现入口；返回素材页线索，不把正文或代理图片当视频文件。"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID

from app.budget import provider_call_input_digest
from app.costs import CatalogProviderCostEstimator
from app.db import BudgetPolicyRepository, ProviderCallRepository
from app.domain.models import CostCategory, UsageCost
from app.media.web_discovery import classify_search_results, is_page_candidate
from app.providers.public_page_media import PublicPageMediaProvider
from app.providers.commons_media import ExternalMediaError
from app.external_media_connection import external_source_connections
from app.web_discovery_connection import WebConnectionError
from app.provider_execution import (ProviderExecutionService, RuntimeProviderIdentity,
    ProviderExecutionIdempotencyConflict, ProviderExecutionInProgress, ProviderExecutionRecordedFailure)


def page_resolution(page):
    """只报告已接解析器和来源准入，不把支持入口称为文件已取得。"""
    if not is_page_candidate(page):
        return {"can_resolve":False,"resolution_code":"not_video_detail",
            "resolution_reason":"仅具体页面可尝试原文件解析，目录、文档和验证页不作为素材"}
    from urllib.parse import urlsplit
    if urlsplit(page.url).hostname in {"pexels.com", "www.pexels.com"}:
        from app.providers.pexels_media import page_video_id
        try:
            page_video_id(page.url)
        except ExternalMediaError as error:
            return {"can_resolve":False,"resolution_code":error.code,"resolution_reason":str(error)}
        connected = any(source["id"] == "pexels" and source["configured"] for source in external_source_connections())
        return {"can_resolve":connected,"resolver_provider":"pexels",
            "resolution_code":"connected_catalogue" if connected else "pexels_not_configured",
            "resolution_reason":"使用已连接的 Pexels 正式接口取得原文件信息，无需付费抓正文" if connected
                else "该视频页可通过 Pexels 正式接口获取，请先连接 Pexels Key"}
    try:
        PublicPageMediaProvider().check_source(page.url)
    except ExternalMediaError as error:
        return {"can_resolve":False,"resolution_code":error.code,
            "resolution_reason":("该来源的原文件网页解析尚未接通；已连接的图库请通过素材搜索入口获取"
                if error.code == "public_page_unsupported_source" else str(error))}
    if page.kind == "unknown":
        return {"can_resolve":True,"resolver_provider":"public_page","resolution_code":"page_metadata_pending",
            "resolution_reason":"页面用途尚未核实；仅尝试读取原文件及来源元数据，不代表已有视频或可用素材"}
    return {"can_resolve":True,"resolver_provider":"public_page","resolution_code":"resolver_available",
        "resolution_reason":"已接该来源的公开页解析；页面仍须提供原文件、编码尺寸和完整署名"}


class WebDiscoveryService:
    def __init__(self, db, provider, *, reservation_guard=None, supports_fetch=True):
        self.db, self.provider = db, provider
        self.calls = ProviderCallRepository(db)
        self.reservation_guard, self.supports_fetch = reservation_guard, supports_fetch

    def search(self, project_id, query, key, *, refresh=False):
        query = " ".join(query.split())
        if not 2 <= len(query) <= 128:
            raise ValueError("网页检索词须为 2 至 128 字符")
        call, cached = self._run(project_id, key, "web_search", {"query":query},
            lambda:self.provider.search(query, count=8, need_content=True), refresh=refresh)
        return self.search_snapshot(call,cached=cached)

    @staticmethod
    def search_snapshot(call,*,cached=False):
        result=call.result_payload["result"]
        pages=classify_search_results(result["document"])
        return {"call_id":str(call.id),"provider":call.provider,"query":result.get("query",""),"cached":cached,
            "pages":[{"url":p.url,"title":p.title,"kind":p.kind,"evidence":p.evidence,
                **page_resolution(p)} for p in pages]}

    def fetch(self, project_id, search_id, url, key):
        search = self.calls.get(search_id)
        if not search or search.project_id != project_id or search.operation != "web_search" or search.status != "completed":
            raise ValueError("请选择当前项目已保存的网页检索结果")
        if search.provider != self.provider.provider_name:
            raise ValueError("抓取连接须与已保存搜索来源一致")
        pages = classify_search_results(search.result_payload["result"].get("document", search.result_payload["result"]))
        page = next((p for p in pages if p.url == url and is_page_candidate(p)), None)
        if page is None:
            raise ValueError("仅抓取已保存的具体页面；目录、文档、图片或验证页不继续付费抓取")
        existing=page.raw.get("content") or page.raw.get("text")
        if isinstance(existing,str) and existing.strip():
            return {"call_id":str(search.id),"provider":search.provider,"url":url,"cached":True,
                "title":page.title,"content":existing,"content_source":"saved_search"}
        call, cached = self._run(project_id, key, "web_fetch", {"url":url,"search_call_id":str(search_id)},
            lambda:self.provider.fetch(url))
        raw = call.result_payload["result"]["document"]
        return {"call_id":str(call.id),"provider":call.provider,"url":url,"cached":cached,
            "title":raw.get("title", ""),"content":raw.get("content",raw.get("markdown",""))}

    def _run(self, project_id, key, operation, document, action, *, refresh=False):
        provider = self.provider
        model = "fetch" if operation == "web_fetch" else getattr(provider,"model","search")
        # 只保存账号摘要，密钥不进入输入、账目或返回结果。
        scope = sha256(provider._api_key.encode()).hexdigest()
        document = {**document,"connection_scope":scope}
        identity = RuntimeProviderIdentity(provider.provider_name,model)
        digest = provider_call_input_digest({"project_id":str(project_id),"operation":operation,
            "provider":identity.provider,"model":model,"input_source":"external_web","input":document})
        request_digest = provider_call_input_digest({"input":digest,"refresh":refresh})
        alias_key = f"web:{key}"
        alias = self.db.connection.execute("SELECT * FROM external_search_replays WHERE idempotency_key=?",(alias_key,)).fetchone()
        if alias:
            if alias["project_id"] != str(project_id) or alias["input_digest"] != request_digest:
                raise ProviderExecutionIdempotencyConflict("web discovery request changed")
            return self._completed(self.calls.get(UUID(alias["call_id"]))),bool(alias["cache_hit"])
        cutoff = datetime.now(timezone.utc)-timedelta(hours=1)
        cached = None if refresh else next((c for c in reversed(self.calls.list_for_project(project_id))
            if c.input_digest==digest and c.status=="completed" and c.completed_at and c.completed_at>=cutoff),None)
        if cached:
            self._bind(alias_key,project_id,request_digest,cached.id,True)
            return cached,True
        bucket = int(datetime.now(timezone.utc).timestamp())//3600
        suffix = sha256(key.encode()).hexdigest() if refresh else str(bucket)
        execution_key = f"web:{digest}:{suffix}"
        existing = self.calls.get_by_idempotency_key(execution_key)
        if existing:
            if existing.input_digest != digest:
                raise ProviderExecutionIdempotencyConflict("web discovery request changed")
            self._bind(alias_key,project_id,request_digest,existing.id,False)
            return self._completed(existing),False
        if operation == "web_fetch" and not self.supports_fetch:
            raise WebConnectionError("web_capability_unavailable", "此连接未提供网页正文抓取能力；未发起请求")
        price = provider.cost()
        policies = BudgetPolicyRepository(self.db)
        policy = policies.get_by_project(project_id) or policies.get_by_project(None)
        if policy and policy.currency != price.currency:
            price = UsageCost(category=CostCategory.STOCK,amount=None,currency=policy.currency,
                provider=provider.provider_name,note="来源按次价格币种与项目不同，未猜测汇率")
        estimate = CatalogProviderCostEstimator({(operation,identity.provider,model):price})
        try:
            ProviderExecutionService(self.db,estimate).execute(project_id=project_id,idempotency_key=execution_key,
                operation=operation,provider=identity,input_source="external_web",category=CostCategory.STOCK,
                input_document=document,action=action,encode_result=lambda value:{"document":value,"query":document.get("query")},
                decode_result=lambda value:value["document"],error_code=lambda exc:getattr(exc,"code","web_discovery_failed"),
                reservation_guard=(lambda:self.reservation_guard(operation)) if self.reservation_guard is not None else None)
        finally:
            call = self.calls.get_by_idempotency_key(execution_key)
            if call and call.input_digest==digest:
                self._bind(alias_key,project_id,request_digest,call.id,False)
        return self._completed(call),False

    def _bind(self,key,project_id,digest,call_id,cached):
        with self.db.transaction(immediate=True):
            self.db.connection.execute("INSERT OR IGNORE INTO external_search_replays VALUES (?,?,?,?,?,?)",
                (key,str(project_id),digest,str(call_id),datetime.now(timezone.utc).isoformat(),int(cached)))
            row=self.db.connection.execute("SELECT * FROM external_search_replays WHERE idempotency_key=?",(key,)).fetchone()
            if (row["project_id"],row["input_digest"],row["call_id"])!=(str(project_id),digest,str(call_id)):
                raise ProviderExecutionIdempotencyConflict("web request belongs to another input")

    @staticmethod
    def _completed(call):
        if not call or call.status in {"reserved","running"}:
            raise ProviderExecutionInProgress("web request running or awaiting recovery")
        if call.status != "completed":
            raise ProviderExecutionRecordedFailure("web request failed; no automatic retry")
        return call

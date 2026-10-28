from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from hashlib import sha256
from threading import Barrier, Event
from urllib.error import HTTPError

import pytest

from app.budget import BudgetLimitError
from app.db import Database, ProviderCallRepository
from app.external_media import ExternalMediaService
from app.media import MediaImporter
from app.providers.commons_media import CommonsMediaProvider, ExternalMediaError
from app.providers.coverr_media import CoverrMediaProvider
from app.providers.pexels_media import PexelsMediaProvider
from app.providers.pixabay_media import PixabayMediaProvider
from app.provider_execution import ProviderExecutionRecordedFailure
from test_external_media import Provider, Probe, _project


class Stock(Provider):
    def __init__(self, name="pexels", account="account-a", orientation="portrait"):
        super().__init__()
        self.provider_name = name
        self.model = "fixture-video-api"
        self.search_scope = sha256(account.encode()).hexdigest() + ":" + orientation
        self.cache_ttl_seconds = 86400
        self.quota_reset_kind = "seconds" if name == "pixabay" else "epoch"
        self.last_quota = {}
        self.error = None
    def search(self, query, **kwargs):
        self.search_count += 1
        if self.error:
            raise self.error
        return [self.video.model_copy(update={"provider": self.provider_name})]


def service(db, root, provider, **kwargs):
    return ExternalMediaService(db, root, provider, MediaImporter(db, root, Probe()), **kwargs)


def advance_clock(monkeypatch, seconds):
    future = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return future
    monkeypatch.setattr("app.external_media.datetime", Clock)


def failure(code="pexels_http_429", seconds=60):
    error = ExternalMediaError("safe source error")
    error.code = code
    error.retry_after_seconds = seconds
    return error


def test_quota_exhaustion_cools_new_searches_but_allows_cache_and_import(tmp_path):
    path = tmp_path / "quota.sqlite"
    project = _project(path)
    source = Stock()
    source.last_quota = {"remaining": "0", "reset": str(int(datetime.now(timezone.utc).timestamp()) + 600)}
    guarded = []
    with Database(path) as db:
        media = service(db, tmp_path, source, reservation_guard=lambda operation: guarded.append(db.connection.in_transaction))
        first = media.search(project.id, "galaxy", "first")
        cached = media.search(project.id, "galaxy", "cached")
        assert cached.cached and cached.call_id == first.call_id
        with pytest.raises(ExternalMediaError) as error:
            media.search(project.id, "new query", "blocked")
        assert error.value.code == "external_source_cooldown"
        assert len(ProviderCallRepository(db).list()) == 1 and source.search_count == 1
        result = ProviderCallRepository(db).get(first.call_id).result_payload["result"]
        assert result["connection_scope"] == source.search_scope
        imported = media.import_video(project.id, first.call_id, first.candidates[0].id, "import")
        assert imported.asset_id and source.download_count == 1
        assert guarded == [True, True]  # 原 guard 仍在调用预留事务内；下载不被搜索配额拦截。


def test_429_cooldown_survives_restart_and_project_change_but_not_account_change(tmp_path, monkeypatch):
    path = tmp_path / "failure.sqlite"
    project, other = _project(path), _project(path)
    source = Stock()
    source.error = failure()
    with Database(path) as db:
        with pytest.raises(ExternalMediaError) as error:
            service(db, tmp_path, source).search(project.id, "first query", "failed")
        assert error.value.code == "pexels_http_429"
        recorded = ProviderCallRepository(db).list()[0]
        assert recorded.error_code.startswith("pexels_http_429:cd:") and len(recorded.error_code) <= 100
        assert "account-a" not in recorded.error_code
    with Database(path, startup_mode="existing") as db:
        same = Stock(orientation="landscape")
        with pytest.raises(ExternalMediaError) as error:
            service(db, tmp_path, same).search(other.id, "different query", "other-project")
        assert error.value.code == "external_source_cooldown" and same.search_count == 0
        changed = Stock(account="account-b")
        service(db, tmp_path, changed).search(other.id, "different query", "other-account")
        assert changed.search_count == 1
        advance_clock(monkeypatch, 65)
        service(db, tmp_path, same).search(other.id, "different query", "window-ended")
        assert same.search_count == 1


def test_pixabay_reset_seconds_are_relative_to_recorded_response(tmp_path, monkeypatch):
    path = tmp_path / "seconds.sqlite"
    project = _project(path)
    source = Stock(name="pixabay")
    source.last_quota = {"remaining": "0", "reset": "30"}
    with Database(path) as db:
        media = service(db, tmp_path, source)
        media.search(project.id, "first query", "first")
        with pytest.raises(ExternalMediaError) as error:
            media.search(project.id, "new query", "blocked")
        assert 0 < error.value.retry_after_seconds <= 30
        advance_clock(monkeypatch, 31)
        media.search(project.id, "new query", "allowed")
        assert source.search_count == 2


def test_non_rate_failure_and_application_guard_remain_distinct(tmp_path):
    path = tmp_path / "errors.sqlite"
    project = _project(path)
    source = Stock()
    source.error = failure("pexels_http_403")
    with Database(path) as db:
        with pytest.raises(ExternalMediaError) as error:
            service(db, tmp_path, source).search(project.id, "first query", "forbidden")
        assert error.value.code == "pexels_http_403"
        assert ProviderCallRepository(db).list()[0].error_code == "pexels_http_403"
        def no_budget(operation):
            raise BudgetLimitError("external_material_tail_budget", "keep production budget")
        with pytest.raises(BudgetLimitError) as error:
            service(db, tmp_path, source, reservation_guard=no_budget).search(project.id, "new query", "budget")
        assert error.value.code == "external_material_tail_budget"
        assert source.search_count == 1 and len(ProviderCallRepository(db).list()) == 1


@pytest.mark.parametrize("make_provider,code", [
    (lambda opener: PexelsMediaProvider("dummy-key", opener=opener), "pexels_http_429"),
    (lambda opener: PixabayMediaProvider("dummy-key", opener=opener), "pixabay_http_429"),
    (lambda opener: CoverrMediaProvider("dummy-key", opener=opener), "coverr_http_429"),
    (lambda opener: CommonsMediaProvider(opener=opener), "external_media_catalogue_http_429"),
])
def test_provider_preserves_safe_retry_window_without_changing_public_error_code(make_provider, code):
    seen = []
    def opener(request, timeout):
        seen.append(request)
        raise HTTPError(request.full_url, 429, "dummy-key", {"Retry-After": "45"}, None)
    with pytest.raises(ExternalMediaError) as error:
        make_provider(opener).search("coffee")
    assert error.value.code == code and error.value.retry_after_seconds == 45
    assert "dummy-key" not in str(error.value) and len(seen) == 1


def seed_one_remaining(path, root, project):
    source = Stock()
    source.last_quota = {"remaining": "1", "reset": str(int(datetime.now(timezone.utc).timestamp()) + 600)}
    with Database(path, startup_mode="existing") as db:
        return service(db, root, source).search(project.id, "cached seed", "seed")


def test_two_connections_atomically_reserve_last_source_call_and_keep_cache_available(tmp_path):
    path = tmp_path / "concurrent.sqlite"
    project = _project(path)
    seed = seed_one_remaining(path, tmp_path, project)
    ready, started, release = Barrier(2), Event(), Event()
    attempts = []
    class Blocking(Stock):
        def search(self, query, **kwargs):
            attempts.append(query)
            started.set()
            assert release.wait(5), "fixture source was not released"
            return super().search(query, **kwargs)
    def run(index):
        with Database(path, startup_mode="existing") as db:
            source = Blocking(orientation="portrait" if index == 0 else "landscape")
            ready.wait(timeout=5)
            try:
                return service(db, tmp_path, source).search(project.id, f"query {index}", f"concurrent-{index}")
            except ExternalMediaError as error:
                return error.code
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(run, index) for index in range(2)]
        try:
            finished, pending = wait(futures, timeout=5, return_when=FIRST_COMPLETED)
            assert len(finished) == 1 and len(pending) == 1
            assert next(iter(finished)).result() == "external_source_cooldown"
            assert started.wait(1) and len(attempts) == 1
            with Database(path, startup_mode="existing") as db:
                cached = service(db, tmp_path, Stock()).search(project.id, "cached seed", "cached-during-reservation")
                assert cached.cached and cached.call_id == seed.call_id
                calls = ProviderCallRepository(db).list()
                assert len(calls) == 2 and sum(call.status == "running" for call in calls) == 1
        finally:
            release.set()
        results = [future.result(timeout=5) for future in futures]
        assert sum(result == "external_source_cooldown" for result in results) == 1
    # 完成但缺少新配额的请求仍保留消耗；重启不把余额恢复为 1。
    with Database(path, startup_mode="existing") as db:
        with pytest.raises(ExternalMediaError) as error:
            service(db, tmp_path, Stock()).search(project.id, "after restart", "restart")
        assert error.value.code == "external_source_cooldown"
        assert len(ProviderCallRepository(db).list()) == 2


@pytest.mark.parametrize("reason", ["configuration", "budget"])
def test_guard_rejection_does_not_consume_source_reservation(tmp_path, reason):
    path = tmp_path / "rejected.sqlite"
    project = _project(path)
    seed_one_remaining(path, tmp_path, project)
    def reject(operation):
        if reason == "budget":
            raise BudgetLimitError("external_material_tail_budget", "preserve production budget")
        error = ExternalMediaError("connection is not configured")
        error.code = "external_not_configured"
        raise error
    with Database(path, startup_mode="existing") as db:
        with pytest.raises((BudgetLimitError, ExternalMediaError)):
            service(db, tmp_path, Stock(), reservation_guard=reject).search(project.id, "rejected", "rejected")
        assert len(ProviderCallRepository(db).list()) == 1
        result = service(db, tmp_path, Stock()).search(project.id, "allowed", "allowed")
        assert result.call_id and len(ProviderCallRepository(db).list()) == 2


def test_failed_search_keeps_uncertain_consumption_until_reset_and_other_account_can_continue(tmp_path, monkeypatch):
    path = tmp_path / "failed-reservation.sqlite"
    project = _project(path)
    seed_one_remaining(path, tmp_path, project)
    source = Stock()
    source.error = failure("pexels_http_503")
    with Database(path, startup_mode="existing") as db:
        with pytest.raises(ExternalMediaError) as error:
            service(db, tmp_path, source).search(project.id, "failed request", "failed-request")
        assert error.value.code == "pexels_http_503"
    with Database(path, startup_mode="existing") as db:
        media = service(db, tmp_path, Stock())
        with pytest.raises(ProviderExecutionRecordedFailure):
            media.search(project.id, "failed request", "failed-request")
        with pytest.raises(ExternalMediaError) as error:
            media.search(project.id, "another request", "another-request")
        assert error.value.code == "external_source_cooldown"
        assert source.search_count == 1
        other = service(db, tmp_path, Stock(account="account-b")).search(project.id, "another account", "other-account")
        assert other.call_id
        advance_clock(monkeypatch, 601)
        assert media.search(project.id, "new window", "new-window").call_id


@pytest.mark.parametrize("quota", [{"remaining": "1"}, {"remaining": "unknown", "reset": "600"},
    {"remaining": 1.5, "reset": "600"}])
def test_unknown_quota_is_not_promoted_to_a_known_reservation_limit(tmp_path, quota):
    path = tmp_path / "unknown.sqlite"
    project = _project(path)
    source = Stock()
    source.last_quota = quota
    with Database(path, startup_mode="existing") as db:
        media = service(db, tmp_path, source)
        assert media.search(project.id, "first query", "first").call_id
        assert media.search(project.id, "second query", "second").call_id
        assert source.search_count == 2


def test_legacy_canonical_failure_is_replayed_instead_of_creating_a_tagged_retry(tmp_path):
    path = tmp_path / "legacy.sqlite"
    project = _project(path)
    source = Stock()
    source.error = failure("pexels_http_503")
    with Database(path, startup_mode="existing") as db:
        media = service(db, tmp_path, source)
        with pytest.raises(ExternalMediaError):
            media.search(project.id, "legacy query", "original-request")
        recorded = ProviderCallRepository(db).list()[0]
        legacy_key = recorded.idempotency_key.rsplit(":quota:", 1)[0]
        # 模拟部署前存在的原 canonical key：升级不能把同一失败变为新网络请求。
        db.connection.execute("UPDATE provider_call_records SET idempotency_key=?, "
            "payload=json_set(payload,'$.idempotency_key',?) WHERE id=?",
            (legacy_key, legacy_key, str(recorded.id)))
        db.connection.execute("DELETE FROM external_search_replays")
        with pytest.raises(ProviderExecutionRecordedFailure):
            media.search(project.id, "legacy query", "another-request")
        assert source.search_count == 1 and len(ProviderCallRepository(db).list()) == 1

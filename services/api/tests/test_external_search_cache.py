from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.db import Database, ProviderCallRepository
from app.main import create_app
from app.media import MediaImporter
from app.providers.commons_media import ExternalMediaError
from test_external_media import Provider, Probe, _project


def setup(tmp_path, monkeypatch):
    path = tmp_path/'cache.sqlite'
    project = _project(path)
    provider = Provider()
    provider.cache_ttl_seconds = 86400
    provider.search_scope = 'account-a:portrait'
    monkeypatch.setenv('CONTENT_OS_DATA_ROOT',str(tmp_path/'data'))
    app = create_app(path,external_media_provider=provider,
        media_importer_factory=lambda db,root:MediaImporter(db,root,Probe()))
    return path,project,provider,app


def future_clock(monkeypatch):
    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(timezone.utc)+timedelta(days=2)
    monkeypatch.setattr('app.external_media.datetime',Future)


def test_cross_request_cache_survives_restart_and_exhausted_budget(tmp_path,monkeypatch):
    path,project,provider,app = setup(tmp_path,monkeypatch)
    base = f'/projects/{project.id}/external-media'
    with TestClient(app) as client:
        client.put(f'/projects/{project.id}/budget',json={'max_calls':1}).raise_for_status()
        first = client.post(base+'/search',json={'query':'galaxy','idempotency_key':'first'}).json()
        second = client.post(base+'/search',json={'query':'  galaxy  ','idempotency_key':'second'}).json()
        assert second['call_id'] == first['call_id'] and second['cached'] is True
        assert provider.search_count == 1
        assert client.post(base+'/search',json={'query':'galaxy','idempotency_key':'first'}).json() == first
    with TestClient(app) as client:
        assert client.post(base+'/search',json={'query':'galaxy','idempotency_key':'second'}).json() == second
        assert provider.search_count == 1
    with Database(path) as db:
        assert len(ProviderCallRepository(db).list_for_project(project.id)) == 1
        assert db.connection.execute('SELECT COUNT(*) FROM external_search_replays').fetchone()[0] == 2


def test_cache_expiration_refresh_and_account_change_are_distinct_from_request_replay(tmp_path,monkeypatch):
    _,project,provider,app = setup(tmp_path,monkeypatch)
    base = f'/projects/{project.id}/external-media/search'
    with TestClient(app) as client:
        first = client.post(base,json={'query':'galaxy','idempotency_key':'first'}).json()
        future_clock(monkeypatch)
        assert client.post(base,json={'query':'galaxy','idempotency_key':'first'}).json() == first
        expired = client.post(base,json={'query':'galaxy','idempotency_key':'expired'}).json()
        assert expired['call_id'] != first['call_id'] and provider.search_count == 2
        refreshed = client.post(base,json={'query':'galaxy','idempotency_key':'refresh','refresh':True}).json()
        assert refreshed['call_id'] != expired['call_id'] and provider.search_count == 3
        provider.search_scope = 'account-b:portrait'
        assert client.post(base,json={'query':'galaxy','idempotency_key':'first'}).status_code == 409
        changed = client.post(base,json={'query':'galaxy','idempotency_key':'changed'}).json()
        assert changed['call_id'] != refreshed['call_id'] and provider.search_count == 4


def test_failed_request_stays_failed_across_restart_and_time_until_explicit_new_refresh(tmp_path,monkeypatch):
    _,project,provider,app = setup(tmp_path,monkeypatch)
    base = f'/projects/{project.id}/external-media/search'
    original = provider.search
    attempts = []
    def fail(query,**kwargs):
        attempts.append(query)
        raise ExternalMediaError('source unavailable')
    provider.search = fail
    with TestClient(app) as client:
        assert client.post(base,json={'query':'galaxy','idempotency_key':'failed'}).status_code == 502
        assert client.post(base,json={'query':'galaxy','idempotency_key':'new-key'}).status_code == 409
        assert len(attempts) == 1
    future_clock(monkeypatch)
    provider.search = original
    with TestClient(app) as client:
        assert client.post(base,json={'query':'galaxy','idempotency_key':'failed'}).status_code == 409
        assert provider.search_count == 0
        assert client.post(base,json={'query':'galaxy','idempotency_key':'recover','refresh':True}).status_code == 200
        assert provider.search_count == 1


def test_cache_does_not_cross_project_or_request_identity(tmp_path,monkeypatch):
    path,first,provider,app = setup(tmp_path,monkeypatch)
    other = _project(path)
    with TestClient(app) as client:
        found = client.post(f'/projects/{first.id}/external-media/search',json={'query':'galaxy','idempotency_key':'one'}).json()
        assert client.post(f'/projects/{other.id}/external-media/search',json={'query':'galaxy','idempotency_key':'one'}).status_code == 409
        second = client.post(f'/projects/{other.id}/external-media/search',json={'query':'galaxy','idempotency_key':'two'}).json()
        assert second['call_id'] != found['call_id'] and provider.search_count == 2


def test_changed_query_locale_does_not_reuse_old_catalogue_or_reset_account_quota(tmp_path,monkeypatch):
    path,project,provider,app=setup(tmp_path,monkeypatch)
    base=f'/projects/{project.id}/external-media/search'
    scope=provider.search_scope
    with TestClient(app) as client:
        old=client.post(base,json={'query':'washing hands','idempotency_key':'old'}).json()
        provider.search_context=lambda query:{'locale':'en-US'}
        revised=client.post(base,json={'query':'washing hands','idempotency_key':'revised'}).json()
        cached=client.post(base,json={'query':'washing hands','idempotency_key':'cached'}).json()
        assert revised['call_id']!=old['call_id'] and cached['call_id']==revised['call_id']
        assert provider.search_count==2 and provider.search_scope==scope
        assert client.post(base,json={'query':'washing hands','idempotency_key':'old'}).status_code==409
    with Database(path) as db:
        payload=ProviderCallRepository(db).get(UUID(revised['call_id'])).result_payload['result']
        assert payload['locale']=='en-US' and payload['connection_scope']==scope

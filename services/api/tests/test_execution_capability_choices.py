from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.db import Database, ProviderMachineCapabilityProfileRepository
from app.domain.models import ProviderMachineCapabilityProfile
from app.main import create_app


PATH = "/execution-settings/capabilities"


def profile(provider: str, machine: str, **changes) -> ProviderMachineCapabilityProfile:
    return ProviderMachineCapabilityProfile(
        capability="voice", mode="local", provider=provider, model="fixture-model",
        runtime="fixture-runtime", machine_id=machine, readiness="configured",
        provenance_source="fixture:explicitly-persisted", updated_at=datetime.now(timezone.utc),
        **changes,
    )


def test_empty_choices_do_not_seed_probe_or_dispatch(tmp_path):
    path = tmp_path / "choices.sqlite3"
    with TestClient(create_app(path)) as client:
        assert client.get(PATH).json() == []
        assert client.get(PATH).json() == []
    with Database(path) as db:
        assert ProviderMachineCapabilityProfileRepository(db).list() == []
        for table in ("jobs", "provider_call_records"):
            assert db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_choices_preserve_identity_unknowns_license_and_deterministic_scopes(tmp_path):
    path = tmp_path / "choices.sqlite3"
    # Deliberately insert out of scope order; no runtime is contacted.
    items = [profile("z-fixture", "machine-b", commercial_status="non_commercial_only"),
             profile("a-fixture", "machine-b"), profile("a-fixture", "machine-a")]
    with TestClient(create_app(path)) as client:
        with Database(path) as db, db.transaction():
            repo = ProviderMachineCapabilityProfileRepository(db)
            for item in items:
                repo.save(item)
        response = client.get(PATH)
        assert response.status_code == 200
        expected = [item.model_dump(mode="json") for item in sorted(items, key=lambda item: (item.scope_key, str(item.id)))]
        assert response.json() == expected
        assert client.get(PATH).json() == expected
        assert expected[0]["quality_status"] == "unknown"
        assert expected[0]["evidence_reference"] is None
        assert expected[0]["license_evidence_reference"] is None
        assert expected[-1]["commercial_status"] == "non_commercial_only"
    with TestClient(create_app(path)) as restarted:
        assert restarted.get(PATH).json() == expected


def test_choices_read_current_evidence_without_cached_eligibility(tmp_path):
    path = tmp_path / "choices.sqlite3"
    item = profile("fixture", "machine")
    with TestClient(create_app(path)) as client:
        with Database(path) as db, db.transaction():
            ProviderMachineCapabilityProfileRepository(db).save(item)
        assert client.get(PATH).json()[0]["commercial_status"] == "unknown"
        changed = item.model_copy(update={"commercial_status": "non_commercial_only", "evidence_reference": "fixture:negative-review"})
        with Database(path) as db, db.transaction():
            ProviderMachineCapabilityProfileRepository(db).save(changed)
        assert client.get(PATH).json() == [changed.model_dump(mode="json")]
        with Database(path) as db:
            assert db.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
            assert db.connection.execute("SELECT COUNT(*) FROM provider_call_records").fetchone()[0] == 0


def test_choices_preserve_existing_private_access_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_OS_ACCESS_TOKEN", "fixture-private-token")
    with TestClient(create_app(tmp_path / "private.sqlite3")) as client:
        assert client.get(PATH).status_code == 401
        assert client.get(PATH, headers={"Authorization": "Bearer wrong-fixture-token"}).status_code == 401
        response = client.get(PATH, headers={"Authorization": "Bearer fixture-private-token"})
        assert response.status_code == 200
        assert response.json() == []

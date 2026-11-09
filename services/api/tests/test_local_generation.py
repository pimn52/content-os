from datetime import datetime, timezone

from app.db import Database, ProviderMachineCapabilityProfileRepository, ProviderMachineSettingRepository
from app.domain.models import ProviderMachineCapabilityProfile, ProviderMachineSetting
from app.production_runs import ProductionRunService
from app.providers.local_generation import ensure_runtime_capability_profile


def _configured(capability="voice"):
    return ProviderMachineCapabilityProfile(capability=capability, mode="local", provider="local-model",
        model="installed-model", runtime="installed-runtime", machine_id="this-machine",
        readiness="configured", quality_status="unknown", evidence_reference="deployment:runtime",
        commercial_status="non_commercial_only", license_evidence_reference="deployment:model-license",
        provenance_source="fixture", updated_at=datetime.now(timezone.utc))


def test_configured_runtime_is_durable_without_evidence_or_commercial_gate(tmp_path):
    class Provider:
        provider_name = "local-model"
        model = "installed-model"
        is_local = True
        repair_execution = {"runtime": "installed-runtime", "machine_id": "this-machine"}

    with Database(tmp_path / "runtime.sqlite") as db:
        repository = ProviderMachineCapabilityProfileRepository(db)
        first = ensure_runtime_capability_profile(repository, Provider(), "voice")
        second = ensure_runtime_capability_profile(repository, Provider(), "voice")
        assert first is not None and second is not None and second.id == first.id
        stored = repository.get(first.id)
        assert stored.readiness == "configured"
        assert stored.evidence_reference is None
        assert stored.commercial_status == "unknown"
        assert stored.license_evidence_reference is None


def test_runtime_profile_selection_does_not_reuse_newer_stale_runtime(tmp_path):
    class StaleProvider:
        provider_name = "local-model"
        model = "installed-model"
        is_local = True
        repair_execution = {"runtime": "old-runtime", "machine_id": "this-machine"}

    class CurrentProvider:
        provider_name = "local-model"
        model = "installed-model"
        is_local = True
        repair_execution = {"runtime": "current-runtime", "machine_id": "this-machine"}

    with Database(tmp_path / "runtime.sqlite") as db:
        repository = ProviderMachineCapabilityProfileRepository(db)
        stale = ensure_runtime_capability_profile(repository, StaleProvider(), "voice")
        current = ensure_runtime_capability_profile(repository, CurrentProvider(), "voice")
        assert stale is not None and current is not None
        assert current.id != stale.id
        assert current.runtime == "current-runtime"


def test_talking_attempt_limit_uses_saved_setting_without_claiming_verified_capability(tmp_path):
    with Database(tmp_path / "limits.sqlite") as db:
        profile = _configured("talking")
        ProviderMachineCapabilityProfileRepository(db).save(profile)
        service = ProductionRunService(db, tmp_path)
        assert service._talking_duration_limit(profile) is None
        ProviderMachineSettingRepository(db).save(ProviderMachineSetting(capability=profile.capability,
            mode=profile.mode, provider=profile.provider, model=profile.model, runtime=profile.runtime,
            machine_id=profile.machine_id, values={"max_talking_duration_ms": 2320}, updated_at=datetime.now(timezone.utc)))
        assert service._talking_duration_limit(profile) == 2320
        limit, source = service._talking_duration_policy(profile, 4_000)
        assert limit == 2320
        assert source.startswith("machine-setting:max_talking_duration_ms:")
        assert service._talking_duration_limit(profile.model_copy(update={"machine_id": "other-machine"})) is None
        retained = ProviderMachineCapabilityProfileRepository(db).get(profile.id)
        assert retained.readiness == "configured" and retained.verified_parameters == {}


def test_latentsync_missing_machine_setting_starts_from_adjustable_conservative_default(tmp_path):
    with Database(tmp_path / "duration-default.sqlite") as db:
        profile = _configured("talking").model_copy(update={
            "provider": "latentsync",
            "model": "LatentSync-1.5",
            "runtime": "another-machine-runtime",
            "machine_id": "another-machine",
        })
        ProviderMachineCapabilityProfileRepository(db).save(profile)
        service = ProductionRunService(db, tmp_path)

        limit, source = service._talking_duration_policy(profile, 12_000)

        assert limit == 4460
        assert source == "default:max_talking_duration_ms"
        assert ProviderMachineCapabilityProfileRepository(db).get(profile.id).verified_parameters == {}

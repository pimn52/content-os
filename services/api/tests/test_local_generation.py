from datetime import datetime, timezone

import pytest

from app.db import Database, ProviderMachineCapabilityProfileRepository, ProviderMachineSettingRepository
from app.domain.models import ProviderMachineCapabilityProfile, ProviderMachineSetting
from app.production_runs import ProductionRunService
from app.providers.local_generation import local_generation_ready, local_generation_licensed


def _configured(capability="voice"):
    return ProviderMachineCapabilityProfile(capability=capability, mode="local", provider="local-model",
        model="installed-model", runtime="installed-runtime", machine_id="this-machine",
        readiness="configured", quality_status="unknown", evidence_reference="deployment:runtime",
        commercial_status="non_commercial_only", license_evidence_reference="deployment:model-license",
        provenance_source="fixture", updated_at=datetime.now(timezone.utc))


@pytest.mark.parametrize("changes,ready,licensed", [
    ({}, True, True),
    ({"evidence_reference": None}, False, True),
    ({"mode": "remote"}, False, True),
    ({"readiness": "implemented"}, False, True),
    ({"commercial_status": "unknown"}, True, False),
    ({"license_evidence_reference": None}, True, False),
])
def test_local_generation_distinguishes_execution_inputs_and_output_quality(changes, ready, licensed):
    profile = _configured().model_copy(update=changes)
    assert local_generation_ready(profile) is ready
    assert local_generation_licensed(profile) is licensed
    assert profile.readiness == changes.get("readiness", "configured")
    assert profile.quality_status == "unknown"


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
        retained = ProviderMachineCapabilityProfileRepository(db).get(profile.id)
        assert retained.readiness == "configured" and retained.verified_parameters == {}

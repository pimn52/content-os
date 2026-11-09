from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import create_app


PATH = "/execution-settings/talking/latentsync/LatentSync-1.5/local-compatibility/test-machine"
VERIFIED_PATH = "/execution-settings/talking/latentsync/LatentSync-1.5/local-compatibility/asus-rtx3060-laptop-6gb"


def test_advanced_settings_schema_saves_resolves_and_resets_at_provider_machine_scope(tmp_path) -> None:
    with TestClient(create_app(tmp_path / "settings.sqlite3")) as client:
        schemas = client.get("/execution-settings/schemas")
        assert schemas.status_code == 200
        latentsync = next(item for item in schemas.json()["schemas"] if item["provider"] == "latentsync")
        assert latentsync["features"][0]["implementation_owner"] == "content_os_adapter"
        parameters = {item["key"]: item for item in latentsync["parameters"]}
        assert parameters["max_talking_duration_ms"]["default"] == 4460
        assert parameters["max_talking_duration_ms"]["value_type"] == "integer"
        assert "按目标机器的有界样本调整" in parameters["max_talking_duration_ms"]["help_text"]

        automatic = client.get(PATH)
        assert automatic.status_code == 200
        values = {item["key"]: item for item in automatic.json()["resolution"]["parameters"]}
        assert values["inference_steps"] == {
            "key": "inference_steps", "value": 20, "source": "provider_default", "reason": "provider-known conservative default"
        }
        assert automatic.json()["saved_override"] is None

        saved = client.put(PATH, json={"values": {"inference_steps": 24}})
        assert saved.status_code == 200
        values = {item["key"]: item for item in saved.json()["resolution"]["parameters"]}
        assert values["inference_steps"]["value"] == 24
        assert values["inference_steps"]["source"] == "user_override"
        assert saved.json()["saved_override"]["machine_id"] == "test-machine"

        reloaded = client.get(PATH)
        reloaded_values = {item["key"]: item for item in reloaded.json()["resolution"]["parameters"]}
        assert reloaded_values["inference_steps"]["source"] == "user_override"
        assert client.put(PATH, json={"values": {"inference_steps": 101}}).status_code == 422
        tuned_duration = client.put(PATH, json={"values": {"max_talking_duration_ms": 4800}})
        assert tuned_duration.status_code == 200
        duration_values = {item["key"]: item for item in tuned_duration.json()["resolution"]["parameters"]}
        assert duration_values["max_talking_duration_ms"]["value"] == 4800
        assert duration_values["max_talking_duration_ms"]["source"] == "user_override"
        assert client.put(PATH, json={"values": {"max_talking_duration_ms": 4800.5}}).status_code == 422

        assert client.delete(PATH).status_code == 204
        reset = client.get(PATH)
        assert reset.json()["saved_override"] is None
        reset_values = {item["key"]: item for item in reset.json()["resolution"]["parameters"]}
        assert reset_values["inference_steps"]["source"] == "provider_default"


def test_current_runtime_scope_is_selectable_with_adjustable_duration_default(tmp_path, monkeypatch) -> None:
    runtime = "latentsync-1.5-dual-cpython312-conda310-w52-targeted-typed"
    machine_id = "LAPTOP-UO337HJH"
    config = tmp_path / "talking.json"
    config.write_text(json.dumps({
        "config_version": 2,
        "runtime_options": {"runtime_label": runtime, "machine_id": machine_id},
    }), encoding="utf-8")
    monkeypatch.setenv("CONTENT_OS_TALKING_PROVIDER", "maintained-latentsync")
    monkeypatch.setenv("CONTENT_OS_MAINTAINED_TALKING_CONFIG", str(config))

    with TestClient(create_app(tmp_path / "active-scope.sqlite3")) as client:
        response = client.get("/execution-settings/schemas")
        assert response.status_code == 200
        assert response.json()["active_scopes"] == [{
            "capability": "talking",
            "provider": "latentsync",
            "model": "LatentSync-1.5",
            "runtime": runtime,
            "machine_id": machine_id,
            "source": "current_runtime_configuration",
        }]
        schema = next((item for item in response.json()["schemas"]
                       if item["provider"] == "latentsync" and item["runtime"] == runtime), None)
        assert schema is not None
        parameters = {item["key"]: item for item in schema["parameters"]}
        assert parameters["max_talking_duration_ms"]["default"] == 4460

        path = f"/execution-settings/talking/latentsync/LatentSync-1.5/{runtime}/{machine_id}"
        view = client.get(path)
        assert view.status_code == 200
        values = {item["key"]: item for item in view.json()["resolution"]["parameters"]}
        assert values["max_talking_duration_ms"]["source"] == "provider_default"
        assert values["max_talking_duration_ms"]["value"] == 4460

        saved = client.put(path, json={"values": {"max_talking_duration_ms": 5120}})
        assert saved.status_code == 200
        saved_values = {item["key"]: item for item in saved.json()["resolution"]["parameters"]}
        assert saved_values["max_talking_duration_ms"]["value"] == 5120
        assert saved_values["max_talking_duration_ms"]["source"] == "user_override"


def test_terminal_closeout_uses_a_provider_baseline_without_promoting_cross_machine_quality(tmp_path) -> None:
    with TestClient(create_app(tmp_path / "capabilities.sqlite3")) as client:
        verified = client.get(VERIFIED_PATH)
        assert verified.status_code == 200
        parameters = {item["key"]: item for item in verified.json()["resolution"]["parameters"]}
        assert parameters["trailing_silence_lookahead_ms"]["value"] == 600
        assert parameters["trailing_silence_lookahead_ms"]["source"] == "local_verified"
        schema_parameters = {item["key"]: item for item in verified.json()["schema"]["parameters"]}
        assert "最后一个音节的字内口型对齐" in schema_parameters["trailing_silence_lookahead_ms"]["help_text"]
        assert verified.json()["capability_profile"]["evidence_reference"].endswith("gate-d6g-600ms-lookahead-face-visible-closeout.mp4")
        assert verified.json()["features"] == [{
            "feature": "terminal_face_closeout",
            "support": "verified",
            "reason": "provider+machine capability profile evidence",
        }]

        other = client.get(PATH)
        other_parameters = {item["key"]: item for item in other.json()["resolution"]["parameters"]}
        assert other_parameters["trailing_silence_lookahead_ms"]["value"] == 600
        assert other_parameters["trailing_silence_lookahead_ms"]["source"] == "provider_default"
        assert other.json()["capability_profile"] is None
        assert other.json()["features"][0]["support"] == "available"

        tuned = client.put(PATH, json={"values": {"trailing_silence_lookahead_ms": 750}})
        tuned_parameters = {item["key"]: item for item in tuned.json()["resolution"]["parameters"]}
        assert tuned_parameters["trailing_silence_lookahead_ms"]["source"] == "user_override"
        assert client.delete(PATH).status_code == 204
        reset = client.get(PATH)
        reset_parameters = {item["key"]: item for item in reset.json()["resolution"]["parameters"]}
        assert reset_parameters["trailing_silence_lookahead_ms"]["source"] == "provider_default"


def test_omnivoice_declares_narration_performance_intent_unknown_instead_of_a_speed_claim(tmp_path) -> None:
    path = "/execution-settings/voice/omnivoice/official-pretrained/local-cuda/test-machine"
    with TestClient(create_app(tmp_path / "voice-performance-feature.sqlite3")) as client:
        schema = client.get("/execution-settings/schemas")
        omnivoice = next(item for item in schema.json()["schemas"] if item["provider"] == "omnivoice")
        assert omnivoice["features"] == [{
            "feature": "narration_performance_intent",
            "support": "unknown",
            "implementation_owner": "none",
            "help_text": "当前 OmniVoice 适配器没有把 Content OS 的重音、语速、停顿和节奏意图安全映射到本地运行时。speed 只是 Provider 参数；它不能替代该产品能力，也不构成可控演说表现证据。",
            "parameter_keys": [],
        }]
        view = client.get(path)
        assert view.status_code == 200
        assert view.json()["features"] == [{
            "feature": "narration_performance_intent",
            "support": "unknown",
            "reason": "no Content OS adapter implementation or local capability evidence",
        }]

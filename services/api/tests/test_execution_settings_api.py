from __future__ import annotations

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
        assert reloaded.json()["resolution"]["parameters"][0]["source"] == "user_override"
        assert client.put(PATH, json={"values": {"inference_steps": 101}}).status_code == 422

        assert client.delete(PATH).status_code == 204
        reset = client.get(PATH)
        assert reset.json()["saved_override"] is None
        assert reset.json()["resolution"]["parameters"][0]["source"] == "provider_default"


def test_terminal_closeout_uses_a_provider_baseline_without_promoting_cross_machine_quality(tmp_path) -> None:
    with TestClient(create_app(tmp_path / "capabilities.sqlite3")) as client:
        verified = client.get(VERIFIED_PATH)
        assert verified.status_code == 200
        parameters = {item["key"]: item for item in verified.json()["resolution"]["parameters"]}
        assert parameters["trailing_silence_lookahead_ms"]["value"] == 600
        assert parameters["trailing_silence_lookahead_ms"]["source"] == "local_verified"
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

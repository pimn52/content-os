from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.runtime import inspect_runtime_capabilities, resolve_local_executable


def test_capability_inspection_distinguishes_configuration_and_development() -> None:
    env = {
        "CONTENT_OS_LLM_API_KEY": "secret-that-must-not-be-returned",
        "CONTENT_OS_LLM_MODEL": "local-planner",
    }
    capabilities = inspect_runtime_capabilities(env, executable_lookup=lambda command: f"C:/{command}.exe")
    by_key = {value.key: value for value in capabilities}

    assert by_key["scene_planning"].status == "ready"
    assert by_key["scene_planning"].model == "local-planner"
    assert by_key["asr"].status == "provider_not_configured"
    assert by_key["tts"].status == "not_developed"
    assert "secret" not in " ".join(value.detail for value in capabilities)


def test_runtime_readiness_endpoint_is_side_effect_free_and_secret_free(tmp_path, monkeypatch) -> None:
    for name in (
        "OPENAI_API_KEY",
        "CONTENT_OS_LLM_API_KEY",
        "CONTENT_OS_ASR_API_KEY",
        "CONTENT_OS_VISION_API_KEY",
        "CONTENT_OS_EMBEDDING_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONTENT_OS_LLM_API_KEY", "endpoint-secret")
    monkeypatch.setattr("app.runtime.shutil.which", lambda command: f"C:/{command}.exe")

    with TestClient(create_app(tmp_path / "runtime.sqlite3")) as client:
        response = client.get("/runtime/readiness")

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_ready"] is False
    assert {value["key"] for value in body["capabilities"]} >= {"local_media", "render", "scene_planning", "tts", "talking"}
    assert all("endpoint-secret" not in str(value) for value in body.values())
    assert next(value for value in body["capabilities"] if value["key"] == "scene_planning")["status"] == "ready"


def test_lexical_retrieval_readiness_is_explicit_without_claiming_embedding(tmp_path) -> None:
    capabilities = inspect_runtime_capabilities(
        {"CONTENT_OS_RETRIEVAL_MODE": "lexical"},
        executable_lookup=lambda command: f"C:/{command}.exe",
    )
    by_key = {value.key: value for value in capabilities}
    assert by_key["retrieval"].status == "ready"
    assert by_key["retrieval"].provider == "local"
    assert by_key["embedding"].status == "provider_not_configured"
    assert "embedding" not in by_key["retrieval"].detail.lower()


def test_local_executable_resolution_prefers_explicit_override_and_bundled_tools(monkeypatch) -> None:
    explicit = r"C:\tools\custom-ffprobe.exe"
    assert resolve_local_executable("ffprobe", {"CONTENT_OS_FFPROBE": explicit}, executable_lookup=lambda _: None) == explicit

    monkeypatch.setattr("app.runtime.shutil.which", lambda command: None)
    bundled = resolve_local_executable("ffprobe", {}, executable_lookup=lambda _: None)
    assert bundled.endswith("compositor-win32-x64-msvc\\ffprobe.exe")


def test_local_asr_readiness_is_explicit_and_does_not_need_provider_key(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.runtime.importlib.util.find_spec",
        lambda name: object() if name == "faster_whisper" else None,
    )
    capabilities = inspect_runtime_capabilities(
        {
            "CONTENT_OS_ASR_PROVIDER": "local",
            "CONTENT_OS_ASR_LOCAL_MODEL": "tiny",
        },
        executable_lookup=lambda command: f"C:/{command}.exe",
    )
    asr = next(value for value in capabilities if value.key == "asr")
    assert asr.status == "ready"
    assert asr.provider == "faster-whisper"
    assert asr.model == "tiny"
    assert "key" not in asr.detail.lower()

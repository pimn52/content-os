import json

import pytest

from app import web_discovery_connection as connections
from app.providers.deepseek_web import DeepSeekWebProvider


@pytest.fixture(autouse=True)
def isolated_connections(monkeypatch):
    monkeypatch.setattr(connections, "_ADAPTERS", connections._ADAPTERS.copy())
    for setting in ("CONTENT_OS_DEEPSEEK_WEB_API_KEY_FILE", "CONTENT_OS_KIMI_WEB_API_KEY_FILE",
            "CONTENT_OS_LLM_BASE_URL", "CONTENT_OS_LLM_API_KEY_FILE", "CONTENT_OS_LLM_API_KEY",
            "CONTENT_OS_VISION_BASE_URL", "CONTENT_OS_VISION_API_KEY_FILE", "CONTENT_OS_VISION_API_KEY",
            "CONTENT_OS_WEB_PROVIDER_ORDER"):
        monkeypatch.delenv(setting, raising=False)


@pytest.mark.parametrize("name,prefix,host", [
    ("deepseek", "CONTENT_OS_LLM", "api.deepseek.com"),
    ("moonshot", "CONTENT_OS_VISION", "api.moonshot.cn"),
])
def test_existing_file_connections_share_capability_contract_without_live_call(tmp_path, monkeypatch, name, prefix, host):
    key = tmp_path / "credential.txt"
    key.write_text("fixture-file-secret", encoding="utf-8")
    monkeypatch.setenv(prefix + "_BASE_URL", "https://" + host + "/v1")
    monkeypatch.setenv(prefix + "_API_KEY_FILE", str(key))
    monkeypatch.setenv(prefix + "_API_KEY", "inline-key-must-not-be-used")
    values = connections.web_provider_connections()
    row = next(value for value in values if value["id"] == name)
    assert row["configured"] and row["search"] and row["fetch"]
    assert connections.configured_web_providers() == [name]
    provider = connections.web_provider(name)
    assert provider.provider_name == name and provider._api_key == "fixture-file-secret"
    assert callable(provider.search) and callable(provider.fetch) and provider.cost().amount is not None
    assert "fixture-file-secret" not in json.dumps(values)


def test_connection_order_is_shared_configurable_and_does_not_promote_chat_keys(tmp_path, monkeypatch):
    key = tmp_path / "credential.txt"
    key.write_text("fixture-secret", encoding="utf-8")
    monkeypatch.setenv("CONTENT_OS_LLM_API_KEY", "inline-chat-key")
    monkeypatch.setenv("CONTENT_OS_LLM_BASE_URL", "https://other-chat.example/v1")
    monkeypatch.setenv("CONTENT_OS_LLM_API_KEY_FILE", str(key))
    assert connections.configured_web_providers() == []
    with pytest.raises(connections.WebConnectionError, match="尚未注册"):
        connections.web_provider("other-chat")
    for setting in ("CONTENT_OS_DEEPSEEK_WEB_API_KEY_FILE", "CONTENT_OS_KIMI_WEB_API_KEY_FILE"):
        monkeypatch.setenv(setting, str(key))
    assert connections.configured_web_providers() == ["deepseek", "moonshot"]
    monkeypatch.setenv("CONTENT_OS_WEB_PROVIDER_ORDER", "moonshot,deepseek")
    assert connections.configured_web_providers() == ["moonshot", "deepseek"]
    monkeypatch.setenv("CONTENT_OS_WEB_PROVIDER_ORDER", "moonshot,unknown")
    with pytest.raises(connections.WebConnectionError) as error:
        connections.configured_web_providers()
    assert error.value.code == "web_provider_order_invalid"


def test_registered_adapter_declares_capabilities_and_is_not_tied_to_known_brands(tmp_path, monkeypatch):
    seen = []
    class SearchOnly:
        provider_name = "other-web"
        cost = staticmethod(DeepSeekWebProvider.cost)
        def __init__(self, key):
            self._api_key = key
            seen.append(key)
        def search(self, query, **kwargs):
            return {"results": []}
    connections.register_web_provider("other-web", SearchOnly, label="网页工具",
        credential_setting="CONTENT_OS_OTHER_WEB_API_KEY_FILE", fetch=False)
    key = tmp_path / "other.txt"
    key.write_text("fixture-other-secret", encoding="utf-8")
    monkeypatch.setenv("CONTENT_OS_OTHER_WEB_API_KEY_FILE", str(key))
    assert connections.configured_web_providers() == ["other-web"]
    row = connections.web_provider_connections()[-1]
    assert row["configured"] and row["search"] and not row["fetch"]
    assert seen == []  # 状态和顺序查询不构造 provider，更不会探测网络。
    with pytest.raises(connections.WebConnectionError) as error:
        connections.web_provider("other-web", capability="fetch")
    assert error.value.code == "web_capability_unavailable" and seen == []
    assert connections.web_provider("other-web").provider_name == "other-web"
    assert seen == ["fixture-other-secret"]


@pytest.mark.parametrize("content", ["", "two separate keys", "x" * 8193])
def test_invalid_file_never_falls_back_to_inline_secret(tmp_path, monkeypatch, content):
    key = tmp_path / "invalid.txt"
    key.write_text(content, encoding="utf-8")
    monkeypatch.setenv("CONTENT_OS_DEEPSEEK_WEB_API_KEY_FILE", str(key))
    monkeypatch.setenv("CONTENT_OS_LLM_API_KEY", "inline-secret")
    with pytest.raises(connections.WebConnectionError) as error:
        connections.web_provider("deepseek")
    assert error.value.code == "web_credentials_invalid"
    assert "inline-secret" not in str(error.value)

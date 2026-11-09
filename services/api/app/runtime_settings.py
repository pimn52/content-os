"""普通界面保存本机运行路径；只存路径和非密钥配置。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator

from app.db import ProviderMachineSettingRepository
from app.domain.models import ProviderMachineSetting


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    planner_key_file: str = ""
    planner_base_url: str = ""
    planner_model: str = ""
    planner_protocol: str = ""
    planner_chat_output_mode: str = ""
    planner_thinking_type: str = ""
    planner_reasoning_effort: str = ""
    planner_max_output_tokens: str = ""
    planner_timeout_seconds: str = ""
    vision_key_file: str = ""
    vision_base_url: str = ""
    vision_model: str = ""
    vision_protocol: str = ""
    vision_video_input: str = ""
    vision_thinking_type: str = ""
    pexels_key_file: str = ""
    voice_config_file: str = ""
    talking_config_file: str = ""
    voice_qa_model_path: str = ""

    @field_validator("*", mode="before")
    @classmethod
    def clean(cls, value, info):
        if not isinstance(value, str) or len(value) > 2000:
            raise ValueError("运行设置必须是简短文本")
        value = value.strip()
        if not value:
            return ""
        if info.field_name.endswith(("_file", "_path")):
            if not Path(value).is_absolute():
                raise ValueError("路径必须是本机绝对路径")
        elif info.field_name.endswith("_base_url"):
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("服务地址不能包含凭据、查询参数或片段")
        elif value.lower().startswith(("sk-", "bearer ")):
            raise ValueError("不能保存密钥值，请填写密钥文件路径")
        if info.field_name == "vision_video_input" and value not in {"true", "false"}:
            raise ValueError("视频输入必须是 true 或 false")
        if info.field_name == "vision_thinking_type" and value not in {"enabled", "disabled"}:
            raise ValueError("视觉思考模式必须是 enabled 或 disabled")
        if info.field_name == "planner_reasoning_effort" and value not in {"none", "low", "high", "max"}:
            raise ValueError("规划推理强度必须是 none、low、high 或 max")
        if info.field_name in {"planner_max_output_tokens", "planner_timeout_seconds"}:
            limit = 131072 if info.field_name == "planner_max_output_tokens" else 1800
            if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= limit:
                raise ValueError(f"数值必须是 1 到 {limit} 之间的整数")
        return value


_IDENTITY = dict(capability="runtime", mode="local", provider="content-os", model="settings", runtime="local", machine_id="workspace")
_SCOPE = ":".join((_IDENTITY["capability"], _IDENTITY["mode"], _IDENTITY["provider"], _IDENTITY["model"], _IDENTITY["runtime"], _IDENTITY["machine_id"]))
_ENV = {
    "planner_key_file": "CONTENT_OS_LLM_API_KEY_FILE", "planner_base_url": "CONTENT_OS_LLM_BASE_URL",
    "planner_model": "CONTENT_OS_LLM_MODEL", "planner_protocol": "CONTENT_OS_LLM_PROTOCOL",
    "planner_chat_output_mode": "CONTENT_OS_LLM_CHAT_OUTPUT_MODE", "planner_thinking_type": "CONTENT_OS_LLM_THINKING_TYPE",
    "planner_reasoning_effort": "CONTENT_OS_LLM_REASONING_EFFORT",
    "planner_max_output_tokens": "CONTENT_OS_LLM_MAX_OUTPUT_TOKENS", "planner_timeout_seconds": "CONTENT_OS_LLM_TIMEOUT_SECONDS",
    "vision_key_file": "CONTENT_OS_VISION_API_KEY_FILE", "vision_base_url": "CONTENT_OS_VISION_BASE_URL",
    "vision_model": "CONTENT_OS_VISION_MODEL", "vision_protocol": "CONTENT_OS_VISION_PROTOCOL",
    "vision_video_input": "CONTENT_OS_VISION_VIDEO_INPUT", "vision_thinking_type": "CONTENT_OS_VISION_THINKING_TYPE",
    "pexels_key_file": "CONTENT_OS_PEXELS_API_KEY_FILE", "voice_config_file": "CONTENT_OS_MAINTAINED_VOICE_CONFIG",
    "talking_config_file": "CONTENT_OS_MAINTAINED_TALKING_CONFIG", "voice_qa_model_path": "CONTENT_OS_VOICE_QA_ASR_MODEL",
}


def load_runtime_settings(db) -> RuntimeSettings:
    stored = ProviderMachineSettingRepository(db).get_by_scope_key(_SCOPE)
    return RuntimeSettings.model_validate(stored.values) if stored else RuntimeSettings()


def save_runtime_settings(db, settings: RuntimeSettings) -> RuntimeSettings:
    for name in ("planner_key_file", "vision_key_file", "pexels_key_file", "voice_config_file", "talking_config_file"):
        value = getattr(settings, name)
        if value and not Path(value).is_file():
            raise ValueError(f"{name} 必须指向已存在的文件")
    if settings.voice_qa_model_path and not Path(settings.voice_qa_model_path).is_dir():
        raise ValueError("voice_qa_model_path 必须指向已存在的模型目录")
    current = ProviderMachineSettingRepository(db).get_by_scope_key(_SCOPE)
    value = ProviderMachineSetting(**_IDENTITY, **({"id": current.id} if current else {}),
        values=settings.model_dump(), updated_at=datetime.now(timezone.utc))
    with db.transaction(immediate=True):
        ProviderMachineSettingRepository(db).save(value)
    return settings


def runtime_environment(settings: RuntimeSettings, environment) -> dict[str, str]:
    result = dict(environment)
    for name, value in settings.model_dump().items():
        if value:
            result[_ENV[name]] = value
    if settings.voice_config_file:
        result["CONTENT_OS_VOICE_PROVIDER"] = "omnivoice-maintained"
    if settings.talking_config_file:
        result["CONTENT_OS_TALKING_PROVIDER"] = "maintained-latentsync"
    if settings.voice_qa_model_path:
        result["CONTENT_OS_ASR_PROVIDER"] = "local"
        result["CONTENT_OS_ASR_LOCAL_MODEL"] = settings.voice_qa_model_path
    return result

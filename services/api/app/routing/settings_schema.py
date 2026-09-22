"""Provider-owned Advanced Settings schemas for admitted local benchmarks.

The common UI consumes these serializable descriptions. Provider-specific keys
live here rather than leaking into universal domain contracts or ScenePlans.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .execution import CapabilityFeature, CapabilityType, ExecutionMode, ExecutionProfileKey, FeatureImplementationOwner, FeatureSupport, ParameterValue


@dataclass(frozen=True)
class ParameterSchema:
    key: str
    label: str
    value_type: str
    default: ParameterValue | None
    minimum: float | None = None
    maximum: float | None = None
    help_text: str = ""

    def as_dict(self) -> dict[str, ParameterValue | None]:
        return {
            "key": self.key,
            "label": self.label,
            "value_type": self.value_type,
            "default": self.default,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "help_text": self.help_text,
        }


@dataclass(frozen=True)
class FeatureSchema:
    """An admitted adapter's implementation of a provider-neutral behavior."""

    feature: CapabilityFeature
    support: FeatureSupport
    implementation_owner: FeatureImplementationOwner
    help_text: str = ""
    parameter_keys: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "feature": self.feature.value,
            "support": self.support.value,
            "implementation_owner": self.implementation_owner.value,
            "help_text": self.help_text,
            "parameter_keys": list(self.parameter_keys),
        }


@dataclass(frozen=True)
class ProviderSettingsSchema:
    capability: CapabilityType
    mode: ExecutionMode
    provider: str
    model: str
    runtime: str
    commercial_status: str
    parameters: tuple[ParameterSchema, ...]
    features: tuple[FeatureSchema, ...] = ()
    # Adapter-declared input runway, not an Advanced Setting.  It preserves
    # enough source frames for a provider's frame grid while keeping delivery
    # boundaries owned by the narration series.
    reference_frame_alignment_context_ms: int | None = None

    def profile_key(self, machine_id: str) -> ExecutionProfileKey:
        return ExecutionProfileKey(self.capability, self.mode, self.provider, self.model, self.runtime, machine_id)

    @property
    def defaults(self) -> Mapping[str, ParameterValue]:
        return {item.key: item.default for item in self.parameters if item.default is not None}

    def as_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability.value,
            "mode": self.mode.value,
            "provider": self.provider,
            "model": self.model,
            "runtime": self.runtime,
            "commercial_status": self.commercial_status,
            "parameters": [item.as_dict() for item in self.parameters],
            "features": [item.as_dict() for item in self.features],
        }


LOCAL_BENCHMARK_SCHEMAS: tuple[ProviderSettingsSchema, ...] = (
    ProviderSettingsSchema(
        capability=CapabilityType.TALKING,
        mode=ExecutionMode.LOCAL,
        provider="latentsync",
        model="LatentSync-1.5",
        runtime="local-compatibility",
        commercial_status="non_commercial_only",
        parameters=(
            ParameterSchema("inference_steps", "推理步数", "number", 20, 1, 100, "更多步数可能提高质量，也会增加本机运行时间。"),
            ParameterSchema("guidance_scale", "引导强度", "number", 1.5, 0, 20, "Provider 的本地兼容参数；需通过质量审核后才可提升为本机证据。"),
            ParameterSchema("trailing_silence_lookahead_ms", "结束静音前瞻（毫秒）", "number", 600, 1, 2000, "解决“声音已经说完、嘴巴却还在继续说”：让 LatentSync 在模型输入中提前看到说完后的静音，再把成片精确裁到口播结束。静音不会进入成片。600ms 是 Content OS 为当前 LatentSync 1.5 适配器提供的保守基线，不是所有 Provider 的通用参数，也不等于已在每台电脑上通过人审。"),
        ),
        features=(FeatureSchema(
            CapabilityFeature.TERMINAL_FACE_CLOSEOUT,
            FeatureSupport.AVAILABLE,
            FeatureImplementationOwner.CONTENT_OS_ADAPTER,
            "这是 Content OS 对“声音已结束、嘴巴仍像在说话”问题的收口保护。当前 LatentSync 适配器通过模型输入静音上下文实现；上游 Provider 无需知道或声明该问题。是否自然收口仍需本机质量证据与人工审核。",
            ("trailing_silence_lookahead_ms",),
        ),),
        reference_frame_alignment_context_ms=640,
    ),
    ProviderSettingsSchema(
        capability=CapabilityType.VOICE,
        mode=ExecutionMode.LOCAL,
        provider="omnivoice",
        model="official-pretrained",
        runtime="local-cuda",
        commercial_status="non_commercial_only",
        parameters=(
            ParameterSchema("num_step", "推理步数", "number", 32, 1, 100, "更多步数增加生成时间；官方预训练权重仅作非商业 benchmark。"),
            ParameterSchema("speed", "语速", "number", 1.0, 0.5, 2, "仅改变 Provider 生成参数；生成结果仍必须通过 Voice QA 与人审。"),
        ),
        features=(FeatureSchema(
            CapabilityFeature.NARRATION_PERFORMANCE_INTENT,
            FeatureSupport.UNKNOWN,
            FeatureImplementationOwner.NONE,
            "当前 OmniVoice 适配器没有把 Content OS 的重音、语速、停顿和节奏意图安全映射到本地运行时。speed 只是 Provider 参数；它不能替代该产品能力，也不构成可控演说表现证据。",
        ),),
    ),
)


def list_provider_settings_schemas() -> tuple[ProviderSettingsSchema, ...]:
    return LOCAL_BENCHMARK_SCHEMAS


def find_provider_settings_schema(capability: str, provider: str, model: str, runtime: str) -> ProviderSettingsSchema | None:
    return next((schema for schema in LOCAL_BENCHMARK_SCHEMAS if (schema.capability.value, schema.provider, schema.model, schema.runtime) == (capability, provider, model, runtime)), None)


def find_unique_provider_settings_schema(capability: str, provider: str) -> ProviderSettingsSchema | None:
    """Find a provider's only admitted schema, refusing an ambiguous choice.

    A Talking profile names its provider but deliberately does not carry an
    adapter model/runtime.  The job API can resolve this safely while there is
    exactly one admitted schema; adding another requires an explicit product
    selection instead of silently picking one.
    """

    matches = tuple(
        schema for schema in LOCAL_BENCHMARK_SCHEMAS
        if (schema.capability.value, schema.provider) == (capability, provider)
    )
    return matches[0] if len(matches) == 1 else None


def validate_schema_values(schema: ProviderSettingsSchema, values: Mapping[str, ParameterValue]) -> dict[str, ParameterValue]:
    allowed = {item.key: item for item in schema.parameters}
    normalized: dict[str, ParameterValue] = {}
    for key, value in values.items():
        if key not in allowed:
            raise ValueError(f"unknown setting {key!r} for {schema.provider}")
        parameter = allowed[key]
        if parameter.value_type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} must be a number")
            numeric = float(value)
            if parameter.minimum is not None and numeric < parameter.minimum or parameter.maximum is not None and numeric > parameter.maximum:
                raise ValueError(f"{key} is outside the supported range")
        normalized[key] = value
    if not normalized:
        raise ValueError("at least one setting value is required")
    return normalized

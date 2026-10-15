"""Maintained-process trust records; neither package metadata nor execution rights."""
import hashlib
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .execution_runtime import (Digest, RuntimeContract, RuntimeInventory, RuntimeInventoryFile,
    RuntimeInventoryDescriptor, MAX_RUNTIME_DIRECTORIES, MAX_RUNTIME_MANIFEST_BYTES,
    MAX_RUNTIME_FILE_BYTES, runtime_path)


TRUST_POLICY = "maintained-local-process-v1"
MAX_MAINTAINED_RUNTIME_FILES = 100_000


class MaintainedRuntimeInventoryDescriptor(RuntimeContract):
    inventory_version: Literal[2]
    sha256: Digest
    size_bytes: int = Field(gt=0, le=MAX_RUNTIME_MANIFEST_BYTES)
    file_count: int = Field(ge=3, le=MAX_MAINTAINED_RUNTIME_FILES)
    directory_count: int = Field(ge=0, le=MAX_RUNTIME_DIRECTORIES)
    total_file_bytes: int = Field(gt=0, le=MAX_RUNTIME_FILE_BYTES)
    _version = field_validator("inventory_version", mode="before")(
        RuntimeInventoryDescriptor.exact_version.__func__)


class MaintainedRuntimeInventory(RuntimeContract):
    """Versioned capacity; canonical membership/path semantics remain shared."""
    inventory_version: Literal[2]
    directories: tuple[str, ...] = Field(max_length=MAX_RUNTIME_DIRECTORIES)
    files: tuple[RuntimeInventoryFile, ...] = Field(min_length=3, max_length=MAX_MAINTAINED_RUNTIME_FILES)
    _version = field_validator("inventory_version", mode="before")(
        RuntimeInventoryDescriptor.exact_version.__func__)
    _directories = field_validator("directories")(RuntimeInventory.valid_directories.__func__)
    _membership = model_validator(mode="after")(RuntimeInventory.complete_membership)
    canonical_bytes = RuntimeInventory.canonical_bytes

    @property
    def descriptor(self) -> MaintainedRuntimeInventoryDescriptor:
        raw = self.canonical_bytes()
        return MaintainedRuntimeInventoryDescriptor(inventory_version=2,
            sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw), file_count=len(self.files),
            directory_count=len(self.directories), total_file_bytes=sum(item.size_bytes for item in self.files))


class MaintainedEvidenceReference(RuntimeContract):
    reference: str
    sha256: Digest
    _reference = field_validator("reference")(runtime_path)


class MaintainedDistribution(RuntimeContract):
    root: Literal["environment", "base"]
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=100)
    publisher: str = Field(min_length=1, max_length=200)
    source: MaintainedEvidenceReference

    @field_validator("name", "version", "publisher")
    @classmethod
    def exact_labels(cls, value):
        if value != value.strip():
            raise ValueError("maintained_origin_label_invalid")
        return value


class _MaintainedInstallationOrigin(RuntimeContract):
    """An explicit operator attestation, not an automatically verified installer."""
    trust_policy: Literal["maintained-local-process-v1"]
    evidence_class: Literal["operator_verified"]
    actor: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")
    runtime_inventory_sha256: Digest
    distributions: tuple[MaintainedDistribution, ...] = Field(min_length=1, max_length=256)
    startup_hooks: tuple[str, ...] = Field(max_length=256)
    accepts_local_operator_vendor_os_trust: Literal[True]
    no_concurrent_runtime_updates: Literal[True]
    network_isolation: Literal["not_asserted", "external_control"]
    network_control: MaintainedEvidenceReference | None = None

    @field_validator("accepts_local_operator_vendor_os_trust", "no_concurrent_runtime_updates", mode="before")
    @classmethod
    def exact_acceptance(cls, value):
        if value is not True:
            raise ValueError("maintained_trust_acceptance_required")
        return value

    @field_validator("startup_hooks")
    @classmethod
    def valid_hooks(cls, values):
        return tuple(runtime_path(value) for value in values)

    @model_validator(mode="after")
    def coherent_origin(self):
        names = [(item.root, item.name.casefold()) for item in self.distributions]
        if len(set(names)) != len(names) or len(set(self.startup_hooks)) != len(self.startup_hooks):
            raise ValueError("maintained_origin_duplicate")
        if (self.network_isolation == "external_control") != (self.network_control is not None):
            raise ValueError("maintained_network_control_required")
        return self


class MaintainedRuntimeOrigin(_MaintainedInstallationOrigin):
    """Legacy diagnostic-only origin; serialized fields/meaning remain unchanged."""
    diagnostic_scope: Literal["imports_synthetic_pcm_only"]


class MaintainedGenerationOrigin(_MaintainedInstallationOrigin):
    """Distinct operator statement; installation trust is not a use license."""
    origin_version: Literal[2]
    generation_scope: Literal["voice_clone_only"]
    accepts_generation_runtime_trust: Literal[True]

    @field_validator("origin_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError("maintained_generation_origin_version_invalid")
        return value

    _generation_acceptance = field_validator("accepts_generation_runtime_trust", mode="before")(
        _MaintainedInstallationOrigin.exact_acceptance.__func__)


class MaintainedHostObservation(RuntimeContract):
    """Current OS/device observation, without the former native-loader claims."""
    os_family: Literal["windows"]
    architecture: Literal["amd64"]
    os_version: str = Field(min_length=1, max_length=100)
    host_sha256: Digest
    layout_sha256: Digest
    device: Literal["cpu", "cuda:0"]
    gpu_uuid: str | None = Field(default=None, pattern=r"^GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
    driver_version: str | None = Field(default=None, pattern=r"^[0-9]+(?:\.[0-9]+){1,3}$")
    driver_probe_sha256: Digest | None = None

    @model_validator(mode="after")
    def explicit_device(self):
        details = (self.gpu_uuid, self.driver_version, self.driver_probe_sha256)
        if self.device == "cpu" and any(value is not None for value in details):
            raise ValueError("maintained_cpu_driver_mismatch")
        if self.device == "cuda:0" and any(value is None for value in details):
            raise ValueError("maintained_cuda_observation_required")
        return self


class MaintainedASROrigin(_MaintainedInstallationOrigin):
    """Explicit transcription installation trust, separate from Voice authority."""
    origin_version: Literal[3]
    transcription_scope: Literal["voice_evaluation_qa_only"]
    accepts_transcription_runtime_trust: Literal[True]

    _version = field_validator("origin_version", mode="before")(MaintainedGenerationOrigin.exact_version.__func__)
    _acceptance = field_validator("accepts_transcription_runtime_trust", mode="before")(
        _MaintainedInstallationOrigin.exact_acceptance.__func__)


class MaintainedTalkingOrigin(_MaintainedInstallationOrigin):
    """Talking-specific installation trust; never inherited from Voice/ASR."""
    origin_version: Literal[4]
    generation_scope: Literal["evaluation_talking_run_only"]
    accepts_talking_runtime_trust: Literal[True]

    _version = field_validator("origin_version", mode="before")(MaintainedGenerationOrigin.exact_version.__func__)
    _acceptance = field_validator("accepts_talking_runtime_trust", mode="before")(
        _MaintainedInstallationOrigin.exact_acceptance.__func__)


class DualRuntimeDistribution(RuntimeContract):
    root: Literal["environment", "base", "engine"]
    kind: Literal["pip", "conda", "cpython"]
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=100)
    build: str = Field(default="", max_length=100)
    publisher: str = Field(min_length=1, max_length=200)
    source: MaintainedEvidenceReference

    _labels = field_validator("name", "version", "publisher")(MaintainedDistribution.exact_labels.__func__)

    @model_validator(mode="after")
    def explicit_build(self):
        if (self.kind == "conda") != bool(self.build) or self.build != self.build.strip():
            raise ValueError("maintained_conda_build_required")
        return self


class DualRuntimeComponentOrigin(RuntimeContract):
    role: Literal["application", "engine"]
    inventory_sha256: Digest
    distributions: tuple[DualRuntimeDistribution, ...] = Field(min_length=1, max_length=512)
    startup_hooks: tuple[str, ...] = Field(max_length=256)
    _hooks = field_validator("startup_hooks")(_MaintainedInstallationOrigin.valid_hooks.__func__)

    @model_validator(mode="after")
    def role_membership(self):
        keys = [(d.root, d.kind, d.name.casefold()) for d in self.distributions]
        if len(set(keys)) != len(keys) or len(set(self.startup_hooks)) != len(self.startup_hooks):
            raise ValueError("maintained_origin_duplicate")
        allowed = {"environment", "base"} if self.role == "application" else {"engine"}
        if any(d.root not in allowed for d in self.distributions):
            raise ValueError("maintained_component_role_invalid")
        return self


class MaintainedDualTalkingOrigin(RuntimeContract):
    origin_version: Literal[5]
    trust_policy: Literal["maintained-local-process-v1"]
    evidence_class: Literal["operator_verified"]
    actor: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")
    generation_scope: Literal["evaluation_talking_run_only"]
    accepts_local_operator_vendor_os_trust: Literal[True]
    no_concurrent_runtime_updates: Literal[True]
    accepts_talking_runtime_trust: Literal[True]
    network_isolation: Literal["not_asserted"]
    components: tuple[DualRuntimeComponentOrigin, DualRuntimeComponentOrigin]
    _version = field_validator("origin_version", mode="before")(MaintainedGenerationOrigin.exact_version.__func__)
    _acceptance = field_validator("accepts_local_operator_vendor_os_trust", "no_concurrent_runtime_updates",
        "accepts_talking_runtime_trust", mode="before")(_MaintainedInstallationOrigin.exact_acceptance.__func__)

    @model_validator(mode="after")
    def both_roles(self):
        if {c.role for c in self.components} != {"application", "engine"}:
            raise ValueError("maintained_component_role_invalid")
        return self

"""Versioned runtime inventory/host structures; validation grants no authority."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_RUNTIME_FILES = 50_000
MAX_RUNTIME_DIRECTORIES = 20_000
MAX_RUNTIME_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_RUNTIME_FILE_BYTES = 64 * 1024**3
MAX_RUNTIME_PATH_DEPTH = 32
OS_POLICY_SLOT = "Lib/site-packages/app/providers/windows_os_policy.json"
OS_POLICY_LIMIT = 64 * 1024
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class RuntimeContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def runtime_path(value: str) -> str:
    if (not value or len(value) > 500 or value != unicodedata.normalize("NFC", value)
        or any(character in '\\:<>"|?*' or unicodedata.category(character).startswith("C") for character in value)):
        raise ValueError("runtime_path_invalid")
    parts = value.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
        prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³"}
    if (len(parts) > MAX_RUNTIME_PATH_DEPTH or any(not part or part in (".", "..")
        or len(part) > 255 or part != part.strip() or part.endswith(".")
        or part.split(".", 1)[0].rstrip(" .").upper() in reserved for part in parts)):
        raise ValueError("runtime_path_invalid")
    return value


class RuntimeInventoryFile(RuntimeContract):
    role: Literal["interpreter", "entrypoint", "dependency"]
    name: str
    size_bytes: int = Field(ge=0, le=MAX_RUNTIME_FILE_BYTES)
    sha256: Digest

    _name = field_validator("name")(runtime_path)

    @model_validator(mode="after")
    def valid_empty_file(self):
        if (self.role != "dependency" and self.size_bytes == 0
            or self.size_bytes == 0 and self.sha256 != hashlib.sha256(b"").hexdigest()):
            raise ValueError("runtime_empty_file_invalid")
        return self


class RuntimeInventoryDescriptor(RuntimeContract):
    inventory_version: Literal[1]
    sha256: Digest
    size_bytes: int = Field(gt=0, le=MAX_RUNTIME_MANIFEST_BYTES)
    file_count: int = Field(ge=3, le=MAX_RUNTIME_FILES)
    directory_count: int = Field(ge=0, le=MAX_RUNTIME_DIRECTORIES)
    total_file_bytes: int = Field(gt=0, le=MAX_RUNTIME_FILE_BYTES)

    @field_validator("inventory_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError("runtime_version_invalid")
        return value


class RuntimeInventory(RuntimeContract):
    inventory_version: Literal[1]
    directories: tuple[str, ...] = Field(max_length=MAX_RUNTIME_DIRECTORIES)
    files: tuple[RuntimeInventoryFile, ...] = Field(min_length=3, max_length=MAX_RUNTIME_FILES)

    _version = field_validator("inventory_version", mode="before")(RuntimeInventoryDescriptor.exact_version.__func__)

    @field_validator("directories")
    @classmethod
    def valid_directories(cls, values):
        return tuple(runtime_path(value) for value in values)

    @model_validator(mode="after")
    def complete_membership(self):
        names = (*self.directories, *(item.name for item in self.files))
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("runtime_members_duplicate")
        directories = set(self.directories)
        for name in names:
            parts = name.split("/")
            if any("/".join(parts[:index]) not in directories for index in range(1, len(parts))):
                raise ValueError("runtime_parent_missing")
        roles = [item.role for item in self.files]
        if roles.count("interpreter") != 1 or not {"entrypoint", "dependency"}.issubset(roles):
            raise ValueError("runtime_required_role_missing")
        if sum(item.size_bytes for item in self.files) > MAX_RUNTIME_FILE_BYTES:
            raise ValueError("runtime_size_limit")
        return self

    def canonical_bytes(self) -> bytes:
        value = self.model_dump(mode="json")
        value["directories"].sort()
        value["files"].sort(key=lambda item: item["name"])
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False).encode("utf-8")
        if len(raw) > MAX_RUNTIME_MANIFEST_BYTES:
            raise ValueError("runtime_manifest_size_limit")
        return raw

    @property
    def descriptor(self) -> RuntimeInventoryDescriptor:
        raw = self.canonical_bytes()
        return RuntimeInventoryDescriptor(inventory_version=1, sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw), file_count=len(self.files), directory_count=len(self.directories),
            total_file_bytes=sum(item.size_bytes for item in self.files))


class HostDriverLibrary(RuntimeContract):
    role: Literal["driver", "probe"]
    slot: str
    size_bytes: int = Field(gt=0)
    sha256: Digest
    _slot = field_validator("slot")(runtime_path)


class CPUHostDevice(RuntimeContract):
    kind: Literal["cpu"]


class CUDAHostDevice(RuntimeContract):
    kind: Literal["cuda"]
    uuid: str = Field(pattern=r"^GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    pci_address: str = Field(pattern=r"^[0-9a-f]{8}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
    driver_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+){1,3}$", max_length=50)
    libraries: tuple[HostDriverLibrary, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def required_driver(self):
        if (sum(item.role == "driver" for item in self.libraries) != 1
            or len({item.slot.casefold() for item in self.libraries}) != len(self.libraries)):
            raise ValueError("host_driver_invalid")
        return self


class HostRuntimeObservation(RuntimeContract):
    observation_recipe: str = Field(min_length=1, max_length=100)
    observation_version: int = Field(gt=0)
    trust_recipe: Literal["windows-os-selected-driver-v1", "windows-os-selected-driver-v2"]
    os_family: Literal["windows"]
    architecture: Literal["amd64"]
    build: int = Field(gt=0)
    revision: int = Field(ge=0)
    device: Annotated[CPUHostDevice | CUDAHostDevice, Field(discriminator="kind")]

    @field_validator("observation_recipe")
    @classmethod
    def exact_recipe(cls, value):
        if value != value.strip():
            raise ValueError("host_recipe_invalid")
        return value

    def canonical(self):
        value = self.model_dump(mode="json")
        if isinstance(self.device, CUDAHostDevice):
            value["device"]["libraries"].sort(key=lambda item: item["slot"])
        return value

    @model_validator(mode="after")
    def v2_recipe_pair(self):
        if self.trust_recipe == "windows-os-selected-driver-v2" and (
            self.observation_recipe != "windows-platform-ubr-selected-device" or self.observation_version != 2):
            raise ValueError("host_recipe_version_invalid")
        return self


class HostComponentFile(RuntimeContract):
    windows_relative_path: str
    size_bytes: int = Field(gt=0, le=64 * 1024 * 1024)
    sha256: Digest
    _path = field_validator('windows_relative_path')(runtime_path)

    @model_validator(mode='after')
    def component_store_only(self):
        if not self.windows_relative_path.startswith('WinSxS/'):
            raise ValueError('host_component_path_invalid')
        return self


class HostCommonControlsBinding(RuntimeContract):
    name: Literal['Microsoft.Windows.Common-Controls']
    token: Literal['6595b64144ccf1df']
    architecture: Literal['amd64']
    version: str = Field(pattern=r'^6\.[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}$')
    language: str = Field(pattern=r'^(?:none|neutral|[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*)$')
    directory: str
    code: HostComponentFile
    manifest: HostComponentFile
    policy: HostComponentFile | None
    metadata_versions: tuple[int, int, int, int]
    path_types: tuple[int, int, int, int, int]
    _directory = field_validator('directory')(runtime_path)

    @model_validator(mode='after')
    def exact_paths(self):
        import re
        version = tuple(int(item) for item in self.version.split('.'))
        if (version == (6, 0, 0, 0) or max(version) > 65535 or
                '.'.join(str(item) for item in version) != self.version or
                not re.fullmatch('WinSxS/amd64_microsoft\\.windows\\.common-controls_' + self.token +
                    '_' + re.escape(self.version) + '_' + re.escape(self.language) + '_[0-9a-f]{16}', self.directory) or
                self.code.windows_relative_path != self.directory + '/comctl32.dll' or
                self.manifest.windows_relative_path != 'WinSxS/Manifests/' + self.directory.split('/')[1] + '.manifest' or
                any(not 0 <= value <= 0xffffffff for value in (*self.metadata_versions, *self.path_types))):
            raise ValueError('host_component_binding_invalid')
        if self.policy is not None:
            path = self.policy.windows_relative_path
            if (not path.startswith('WinSxS/Manifests/amd64_policy.6.0.microsoft.windows.common-controls_' + self.token + '_')
                    or not path.endswith('.manifest') or path.count('/') != 2):
                raise ValueError('host_component_binding_invalid')
        return self


class HostRuntimeObservationV3(RuntimeContract):
    """Inert typed carrier; old parsers and current execution spec reject v3.

    It becomes an execution witness only after owned host/prepared/child seams
    independently establish and compare this exact binding and contexts.
    """
    observation_recipe: Literal['windows-platform-ubr-component-bindings']
    observation_version: Literal[3]
    trust_recipe: Literal['windows-os-component-bindings-v3']
    os_family: Literal['windows']
    architecture: Literal['amd64']
    build: int = Field(gt=0)
    revision: int = Field(ge=0)
    device: CPUHostDevice
    component_policy_version: Literal[1]
    manifest_contract_sha256: Literal['bd76c737191489222cc219b605648fee50ae2721a7d1af7ebd756b429dee7ec2']
    common_controls: HostCommonControlsBinding

    def canonical(self):
        return self.model_dump(mode='json')

    @field_validator('observation_version', 'component_policy_version', mode='before')
    @classmethod
    def exact_numeric_versions(cls, value):
        if type(value) is not int:
            raise ValueError('host_recipe_version_invalid')
        return value


class HostComponentIdentityPolicy2(RuntimeContract):
    name: Literal['Microsoft.Windows.Common-Controls', 'Microsoft.Windows.Common-Controls.Resources']
    token: Literal['6595b64144ccf1df']
    architecture: Literal['amd64']
    version: str
    language: str
    encoded_language: str | None

    @model_validator(mode='after')
    def concrete_identity(self):
        import re
        if not re.fullmatch(r'6\.(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})', self.version):
            raise ValueError('component_identity_invalid')
        if self.version == '6.0.0.0' or max(map(int, self.version.split('.'))) > 65535:
            raise ValueError('component_identity_invalid')
        if self.encoded_language is None:
            if self.name != 'Microsoft.Windows.Common-Controls' or self.language != 'neutral':
                raise ValueError('component_language_invalid')
        elif (not re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', self.encoded_language) or
                self.language != self.encoded_language.lower()):
            raise ValueError('component_language_invalid')
        return self


class HostResourceDataPolicy2(RuntimeContract):
    machine: Literal['0x14c', '0x8664']
    optional_magic: Literal['0x10b', '0x20b']
    leaf_count: int = Field(gt=0, le=256)

    @model_validator(mode='after')
    def architecture_pair(self):
        if (self.machine, self.optional_magic) not in (('0x14c', '0x10b'), ('0x8664', '0x20b')):
            raise ValueError('component_data_invalid')
        return self


class HostComponentAssemblyPolicy2(RuntimeContract):
    identity: HostComponentIdentityPolicy2
    directory: str
    file: HostComponentFile
    manifest: HostComponentFile
    flags: int
    metadata_versions: tuple[int, int, int, int]
    path_types: tuple[int, int]
    data: HostResourceDataPolicy2 | None
    _directory = field_validator('directory')(runtime_path)

    @model_validator(mode='after')
    def exact_component(self):
        import re
        resource = self.identity.name.endswith('.Resources')
        parts = self.directory.split('/')
        names = ('microsoft.windows.common-controls.resources', 'microsoft.windows.c..-controls.resources') if resource else (
            'microsoft.windows.common-controls',)
        tokens = parts[-1].split('_')
        if (len(parts) != 2 or parts[0] != 'WinSxS' or len(tokens) != 6 or
                tokens[0] != 'amd64' or tokens[1] not in names or tokens[2] != self.identity.token or
                tokens[3] != self.identity.version or tokens[4] != (self.identity.language if self.identity.encoded_language else 'none') or
                not re.fullmatch('[0-9a-f]{16}', tokens[5]) or
                self.file.windows_relative_path != self.directory + '/comctl32.dll' + ('.mui' if resource else '') or
                self.manifest.windows_relative_path != 'WinSxS/Manifests/' + parts[1] + '.manifest' or
                self.flags != 0 or self.metadata_versions != (1, 0, 0, 0) or self.path_types != (2, 1) or
                resource != (self.data is not None)):
            raise ValueError('component_binding_invalid')
        return self


class CommonControlsBindingPolicy2(RuntimeContract):
    component_policy_version: Literal[2]
    component_policy_sha256: Digest
    manifest_contract_sha256: Literal['bd76c737191489222cc219b605648fee50ae2721a7d1af7ebd756b429dee7ec2']
    source_sha256: Digest
    source_role: Literal['dll', 'bootstrap_exe']
    query_role: Literal['created', 'effective', 'associated']
    root_flags: int = Field(ge=0, le=0xffffffff)
    context_flags: int
    root_path_types: tuple[int, int, int]
    common_controls: HostComponentAssemblyPolicy2
    resource: HostComponentAssemblyPolicy2 | None

    @field_validator('component_policy_version', mode='before')
    @classmethod
    def exact_policy_version(cls, value):
        if type(value) is not int: raise ValueError('component_policy_invalid')
        return value

    @model_validator(mode='after')
    def exact_roster(self):
        if (self.context_flags != 0 or self.root_path_types != (2, 1, 2) or
                self.common_controls.identity.name != 'Microsoft.Windows.Common-Controls' or
                (self.resource is not None and self.resource.identity.name != 'Microsoft.Windows.Common-Controls.Resources')):
            raise ValueError('component_roster_invalid')
        return self


class HostRuntimeObservationV3Policy2(RuntimeContract):
    """Standalone inert policy2 branch; no subclass acceptance by old parsers."""
    observation_recipe: Literal['windows-platform-ubr-component-bindings']
    observation_version: Literal[3]
    trust_recipe: Literal['windows-os-component-bindings-v3']
    os_family: Literal['windows']
    architecture: Literal['amd64']
    build: int = Field(gt=0)
    revision: int = Field(ge=0)
    device: CPUHostDevice
    component_policy_version: Literal[2]
    component_policy_sha256: Digest
    binding: CommonControlsBindingPolicy2

    @field_validator('observation_version', 'component_policy_version', mode='before')
    @classmethod
    def exact_versions(cls, value):
        if type(value) is not int: raise ValueError('host_recipe_version_invalid')
        return value

    @model_validator(mode='after')
    def owned_digest_pair(self):
        if self.component_policy_sha256 != self.binding.component_policy_sha256:
            raise ValueError('component_policy_digest_mismatch')
        return self

    def canonical(self):
        return self.model_dump(mode='json')

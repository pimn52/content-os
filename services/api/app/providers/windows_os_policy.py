"""Owned finite OS profile; callers/receipts/loaded paths never select it."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from pydantic import Field, ValidationInfo, field_validator, model_validator

from app.domain.execution_runtime import (OS_POLICY_LIMIT, OS_POLICY_SLOT, RuntimeContract,
    RuntimeInventory, runtime_path)
from app.runtime_inventory import RuntimeInventoryError, require_os_policy_entry
from .prepared import NativeExecutionUnsupported
from .runtime_tree import _exact_path


UCRT_REFERENCE = "https://learn.microsoft.com/en-us/cpp/windows/universal-crt-deployment"
SERVICING_REFERENCE = (
    "https://support.microsoft.com/en-us/servicing/os/windows/safeos-du/2026/03/"
    "kb5083482-safe-os-dynamic-update-for-windows-11-versions-24h2-and-25h2-march-26-2026"
)
SERVICING_COMPONENTS = frozenset({"gdi32full.dll", "msvcp_win.dll", "sechost.dll", "win32u.dll"})
UCRT_CONTRACT_REFERENCE = "https://support.microsoft.com/en-us/servicing/os/windows/2020/04/update-for-universal-c-runtime-in-windows"
PATH_CONTRACT_REFERENCE = "https://learn.microsoft.com/en-us/uwp/win32-and-com/win32-apis"
# Exact reviewed KB2999226 x64 names; not all API sets or all KB contents.
UCRT_CONTRACTS = frozenset('api-ms-win-crt-' + name + '-l1-1-0.dll' for name in (
    'conio', 'convert', 'environment', 'filesystem', 'heap', 'locale',
    'math', 'private', 'process', 'runtime', 'stdio', 'string', 'time', 'utility'))


class OSMember(RuntimeContract):
    name: str = Field(min_length=1, max_length=255)
    evidence_url: str = Field(min_length=1, max_length=1000)

    @field_validator("name")
    @classmethod
    def exact_name(cls, value):
        runtime_path(value)
        if not re.fullmatch(r"[a-z0-9_][a-z0-9_-]*\.dll", value):
            raise ValueError("os_member_name_invalid")
        return value

    @field_validator("evidence_url")
    @classmethod
    def primary_reference(cls, value, info: ValidationInfo):
        # Reference syntax is not semantic evidence or authority to select a
        # profile. Reviewed exceptions are exact name/URL pairs.
        if any(ord(char) <= 32 or ord(char) >= 127 for char in value) or any(
            char in value for char in ("\\", "%")
        ):
            raise ValueError("os_member_evidence_invalid")
        url = urlsplit(value)
        segments = url.path.split("/")[1:]
        name = info.data.get("name")
        windows_reference = (url.netloc == "learn.microsoft.com"
            and url.path.startswith("/en-us/windows/"))
        reviewed_exception = (name == "ucrtbase.dll" and value == UCRT_REFERENCE) or (
            name in SERVICING_COMPONENTS and value == SERVICING_REFERENCE) or (
            name in UCRT_CONTRACTS and value == UCRT_CONTRACT_REFERENCE) or (
            name == 'api-ms-win-core-path-l1-1-0.dll' and value == PATH_CONTRACT_REFERENCE)
        if (url.scheme != "https" or url.query or url.fragment
            or "?" in value or "#" in value
            or any(segment in ("", ".", "..", "answers", "questions", "community") for segment in segments)
            or not (windows_reference or reviewed_exception)):
            raise ValueError("os_member_evidence_invalid")
        return value


class WindowsOSPolicy(RuntimeContract):
    policy_version: int
    architecture: str
    components: tuple[OSMember, ...] = Field(min_length=1, max_length=128)
    api_contracts: tuple[OSMember, ...] = Field(max_length=256)

    @model_validator(mode="after")
    def exact_profile(self):
        if self.policy_version != 1 or self.architecture != "amd64":
            raise ValueError("os_policy_version_unsupported")
        physical = tuple(item.name for item in self.components)
        contracts = tuple(item.name for item in self.api_contracts)
        if (physical != tuple(sorted(set(physical))) or contracts != tuple(sorted(set(contracts)))
            or set(physical) & set(contracts)
            or any(name.startswith(("api-", "ext-")) for name in physical)
            or any(not re.fullmatch(r"(?:api|ext)-[a-z0-9]+(?:-[a-z0-9]+)+\.dll", name) for name in contracts)):
            raise ValueError("os_policy_members_invalid")
        return self

    @property
    def component_names(self): return frozenset(item.name for item in self.components)

    @property
    def contract_names(self): return frozenset(item.name for item in self.api_contracts)

    def canonical_bytes(self):
        return json.dumps(self.model_dump(mode="json"), sort_keys=True,
            separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


def parse_os_policy(payload: bytes) -> WindowsOSPolicy:
    try:
        if type(payload) is not bytes or not 0 < len(payload) <= OS_POLICY_LIMIT:
            raise ValueError()
        value = WindowsOSPolicy.model_validate_json(payload)
        if value.canonical_bytes() != payload:
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise NativeExecutionUnsupported("execution_os_policy_invalid") from None


def _read(path: Path, size: int = OS_POLICY_LIMIT) -> bytes:
    try:
        _exact_path(path, directory=False)
        with path.open("rb") as source:
            payload = source.read(size + 1)
        if not 0 < len(payload) <= size:
            raise ValueError()
        return payload
    except (OSError, ValueError):
        raise NativeExecutionUnsupported("execution_os_policy_unavailable") from None


def owned_os_policy_bytes() -> bytes:
    # Fixed actual implementation resource; never expected receipt selection.
    payload = _read(Path(__file__).with_suffix(".json"))
    parse_os_policy(payload)
    return payload


def read_prepared_os_policy(*, runtime_root: Path, inventory: RuntimeInventory) -> bytes:
    try:
        _exact_path(runtime_root, directory=True)
        entry = require_os_policy_entry(inventory)
        payload = _read(runtime_root / OS_POLICY_SLOT, entry.size_bytes)
        if len(payload) != entry.size_bytes or hashlib.sha256(payload).hexdigest() != entry.sha256:
            raise ValueError()
        parse_os_policy(payload)
        if payload != owned_os_policy_bytes():
            raise ValueError()
        return payload
    except (ValueError, RuntimeInventoryError):
        raise NativeExecutionUnsupported("execution_os_policy_identity_changed") from None

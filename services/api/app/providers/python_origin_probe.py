"""One-use owned base-only origin diagnostic, never a production invocation."""
from __future__ import annotations

import json
from pathlib import Path
import secrets
import subprocess
from tempfile import TemporaryFile
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.domain.execution_runtime import HostRuntimeObservation, OS_POLICY_SLOT, RuntimeContract
from .cpython_base import select_cpython312_base
from .host_runtime import prepare_host_runtime_v2
from .prepared import ExecutionPreparationError, NativeExecutionUnsupported, PendingInvocation
from .python_startup import startup_owned_files
from .runtime_tree import OwnedRuntimeFile, _exact_path, _scan_private, prepare_runtime_tree
from .windows_loader import require_no_redirection
from .windows_os_policy import owned_os_policy_bytes

ENTRY = "_content_os_origin_probe.py"
TREE_LIMIT = 256 * 1024 * 1024
INPUT_LIMIT = 16 * 1024 * 1024 + 64 * 1024
OUTPUT_LIMIT = 64 * 1024


class OriginProbeReportV1(RuntimeContract):
    probe_version: Literal[1]
    nonce: str = Field(pattern="^[0-9a-f]{64}$")
    passed: bool
    stage: Literal["tree", "host", "baseline", "search", "recheck", "complete"]
    inventory_sha256: str = Field(pattern="^(?:[0-9a-f]{64})?$")
    host_runtime: HostRuntimeObservation | None
    native_count: int = Field(ge=0, le=2048)
    python_count: int = Field(ge=0, le=2048)
    code: str = Field(pattern="^(?:execution_[a-z0-9_]+)?$", max_length=100)
    unknown_module: str = Field(max_length=255)

    @field_validator("probe_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 1: raise ValueError("probe_version_invalid")
        return value


class BlockedOrigin(RuntimeContract):
    kind: Literal["native", "python"]
    name: str = Field(pattern="^[a-z0-9_][a-z0-9_.-]{0,254}$")
    category: Literal["system_exact", "private_inventory", "private_unlisted", "outside"]
    origin_id: str = Field(pattern="^[0-9a-f]{64}$")
    reason: Literal["os_named_wrong_origin", "unlisted_origin"]


class OriginProbeReport(OriginProbeReportV1):
    probe_version: Literal[2]
    blocked_origins: tuple[BlockedOrigin, ...] = Field(max_length=256)

    @field_validator("probe_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 2: raise ValueError("probe_version_invalid")
        return value

    @model_validator(mode="after")
    def coherent_evidence(self):
        rows = self.blocked_origins
        if bool(rows) != (self.code == "execution_loaded_origin_unsupported"):
            raise ValueError("blocked_origins_invalid")
        keys = tuple((row.kind, row.name, row.category, row.origin_id) for row in rows)
        if keys != tuple(sorted(set(keys))): raise ValueError("blocked_origins_invalid")
        if rows:
            if (self.passed or self.stage not in ("baseline", "recheck") or self.code != "execution_loaded_origin_unsupported"
                or self.unknown_module != rows[0].name
                or sum(row.kind == "native" for row in rows) > self.native_count
                or sum(row.kind == "python" for row in rows) > self.python_count):
                raise ValueError("blocked_origins_invalid")
        payload = json.dumps([row.model_dump(mode="json") for row in rows],
            sort_keys=True, separators=(",", ":")).encode()
        if len(payload) > 48 * 1024: raise ValueError("blocked_origins_invalid")
        return self


def owned_probe_files():
    directory = Path(__file__).parent
    sources = (("runtime_primitives.py", "Lib/site-packages/_content_os_host/runtime_primitives.py"),
        ("windows_native.py", "Lib/site-packages/_content_os_host/windows_native.py"),
        ("python_origin_child.py", ENTRY))
    owned = list(startup_owned_files())
    owned.append(OwnedRuntimeFile("Lib/site-packages/_content_os_host/__init__.py", b'"""Owned stdlib-only host diagnostic."""\n'))
    for name, destination in sources:
        source = directory / name
        _exact_path(source, directory=False)
        with source.open("rb") as file: payload = file.read(1024 * 1024 + 1)
        owned.append(OwnedRuntimeFile(destination, payload, "entrypoint" if destination == ENTRY else "dependency"))
    owned.append(OwnedRuntimeFile(OS_POLICY_SLOT, owned_os_policy_bytes()))
    return tuple(owned)


class PreparedOriginProbe:
    """Fixed diagnostic tree/CPU host/argv; no target, device or launch override."""
    _probe_version = 2
    _report_type = OriginProbeReport

    def _validate_extra(self, report):
        pass
    def __init__(self, tree, host, invocation):
        self._tree, self._host, self._invocation = tree, host, invocation
        self._inventory = tree.inventory.model_dump_json()
        self._nonce, self._state = secrets.token_hex(32), "prepared"

    @property
    def inventory(self): return self._tree.inventory

    @property
    def invocation(self): return self._invocation

    @property
    def host_observation(self): return self._host.observation

    def verify(self):
        if self._state != "prepared":
            raise ExecutionPreparationError("execution_preparation_consumed")
        try:
            self._tree.verify()
            self._host.verify()
        except (OSError, ValueError, RuntimeError):
            self._state = "failed"
            raise

    def run_probe(self):
        if self._state != "prepared":
            raise ExecutionPreparationError("execution_preparation_consumed")
        try:
            self.verify()
            require_no_redirection(self)
            request = {"probe_version": self._probe_version, "nonce": self._nonce,
                "inventory": self.inventory.model_dump(mode="json"), "host_runtime": self.host_observation.canonical()}
            request["inventory"]["directories"].sort()
            request["inventory"]["files"].sort(key=lambda entry: entry["name"])
            payload = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                allow_nan=False).encode("utf-8")
            if len(payload) > INPUT_LIMIT:
                raise ExecutionPreparationError("execution_origin_probe_input_limit")
            self._tree.claim_for_launch()
            self._state = "consumed"
            # Owned child emits <=64KiB. Spooling avoids unbounded PIPE memory;
            # readback is bounded, stderr never carries child paths to callers.
            with TemporaryFile() as output:
                result = subprocess.run(list(self.invocation.argv), cwd=self.invocation.cwd,
                    env=dict(self.invocation.environment), input=payload, stdout=output,
                    stderr=subprocess.DEVNULL, timeout=self.invocation.timeout_seconds,
                    shell=False, check=False)
                output.seek(0)
                raw = output.read(OUTPUT_LIMIT + 1)
            if not 0 < len(raw) <= OUTPUT_LIMIT:
                raise ExecutionPreparationError("execution_origin_probe_output_limit")
            try:
                report = self._report_type.model_validate_json(raw)
                if (report.nonce != self._nonce or type(result.returncode) is not int
                    or result.returncode != (0 if report.passed else 1)):
                    raise ValueError()
                if report.passed and (report.stage != "complete" or report.code or report.unknown_module
                    or report.inventory_sha256 != self.inventory.descriptor.sha256
                    or report.host_runtime is None or report.host_runtime.canonical() != self.host_observation.canonical()
                    or not report.native_count or not report.python_count):
                    raise ValueError()
                if not report.passed and (not report.code or report.stage == "complete"):
                    raise ValueError()
                if any(character in report.unknown_module for character in "\\/:\r\n\0"):
                    raise ValueError()
                if self._probe_version == 2 and report.blocked_origins and (report.inventory_sha256 != self.inventory.descriptor.sha256
                    or report.host_runtime is None or report.host_runtime.canonical() != self.host_observation.canonical()):
                    raise ValueError()
                self._validate_extra(report)
            except (ValueError, TypeError):
                raise ExecutionPreparationError("execution_origin_probe_protocol_invalid") from None
            # Entire actual private tree, not an expected-selected file subset.
            current = _scan_private(self.invocation.cwd, {entry.name: entry.role for entry in self.inventory.files}, TREE_LIMIT)
            if current.descriptor != self.inventory.descriptor:
                raise ExecutionPreparationError("execution_fixed_runtime_changed")
            self._host.verify()
            return report
        except subprocess.TimeoutExpired:
            self._state = "failed"
            raise ExecutionPreparationError("execution_prepared_timeout") from None
        except OSError:
            self._state = "failed"
            raise ExecutionPreparationError("execution_prepared_launch_failed") from None
        except Exception:
            self._state = "failed"
            raise

    def close(self):
        self._state = "closed"
        self._tree.close()

    def __enter__(self): return self
    def __exit__(self, *_args): self.close()


def prepare_origin_probe(*, base: Path, staging_parent: Path, work_directory: Path,
                         max_bytes: int = TREE_LIMIT, timeout_seconds: float = 15):
    """Select full real stdlib/base + owned code, never expected receipt files.

    No package/model selection or public Worker/provider route. Launching this
    read-only diagnostic still requires an explicitly declared experiment.
    """
    tree = None
    try:
        if type(max_bytes) is not int or not 0 < max_bytes <= TREE_LIMIT or (
            type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 15):
            raise ExecutionPreparationError("execution_origin_probe_bounds_invalid")
        _exact_path(work_directory, directory=True)
        trees, files = select_cpython312_base(base)
        tree = prepare_runtime_tree(trees=trees, files=files, staging_parent=staging_parent,
            max_bytes=max_bytes, max_files=10000, owned_files=owned_probe_files())
        if len(tree.inventory.directories) > 3000 or work_directory.is_relative_to(tree.root):
            raise ExecutionPreparationError("execution_origin_probe_bounds_invalid")
        host = prepare_host_runtime_v2(runtime_root=tree.root, inventory=tree.inventory, device="cpu")
        environment = (("SystemRoot", str(host.windows_root)), ("WINDIR", str(host.windows_root)),
            ("TEMP", str(work_directory)), ("TMP", str(work_directory)),
            ("HF_HUB_OFFLINE", "1"), ("TRANSFORMERS_OFFLINE", "1"))
        invocation = PendingInvocation((str(tree.root / "python.exe"), "-I", "-S", "-B",
            str(tree.root / ENTRY)), tree.root, environment, timeout_seconds)
        result = PreparedOriginProbe(tree, host, invocation)
        result.verify()
        return result
    except Exception:
        if tree is not None: tree.close()
        raise

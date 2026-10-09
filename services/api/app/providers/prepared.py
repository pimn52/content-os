"""Local selected-file preparation primitives; never an execution permission.

An adapter recipe must independently establish its complete loader closure
before using this seam. Merely supplying files (or a license report) cannot do
that. Current OmniVoice/LatentSync recipes remain unsupported. No model is
imported, loaded or downloaded here. Preparation belongs outside the ledger
transaction; invocation belongs after the application's durable reservation.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import Callable, Mapping

from app.domain.models import ExecutionArtifact, ExecutionMachineObservation, ExecutionSpecification


# Preserve public error imports without importing app/domain in owned children.
from .runtime_primitives import ExecutionPreparationError, NativeExecutionUnsupported


@dataclass(frozen=True)
class SelectedArtifact:
    group: str  # model or runtime
    role: str
    name: str  # logical relative name; independent of installation location
    source: Path


@dataclass(frozen=True)
class PendingInvocation:
    argv: tuple[str, ...]
    cwd: Path
    environment: tuple[tuple[str, str], ...]
    timeout_seconds: float


InvocationBuilder = Callable[[Mapping[tuple[str, str], Path], Mapping[str, object]], PendingInvocation]


def _regular_file(path: Path) -> None:
    # Reject aliases, including symlinked parents / Windows junctions. A path
    # must not redirect between verification and the selected-file copy.
    if path != path.resolve() or not stat.S_ISREG(path.lstat().st_mode):
        raise ExecutionPreparationError("execution_artifact_unavailable")


def _hash_file(path: Path) -> tuple[int, str]:
    _regular_file(path)
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


class PreparedExecution:
    """Single-use fixed files, detached snapshot and one pending invocation.

    It does not certify dependency completeness, machine observation, consent,
    budget or license. Only an audited adapter may supply those observations.
    Read-only copies protect against accidental changes, not a hostile host
    administrator. Every copy is rehashed before launch; loss of the fixed
    condition stops, with no fallback/retry. Caller owns ledger failure marking.
    """

    __slots__ = ("_temporary", "_specification_json", "_files", "_invocation", "_state")

    def __init__(self, temporary: TemporaryDirectory, specification: ExecutionSpecification,
                 files: tuple[tuple[Path, ExecutionArtifact], ...], invocation: PendingInvocation):
        self._temporary = temporary
        # Deep detachment: Pydantic frozen models still contain a mutable dict.
        self._specification_json = specification.model_dump_json()
        self._files = files
        self._invocation = invocation
        self._state = "prepared"

    @property
    def specification(self) -> ExecutionSpecification:
        return ExecutionSpecification.model_validate_json(self._specification_json)

    @property
    def invocation(self) -> PendingInvocation:
        return self._invocation

    def execution_use_snapshot(self) -> dict:
        self.verify_fixed_files()
        spec = self.specification
        return {"snapshot_version": 2, "identity": spec.identity.model_dump(mode="json"),
                "execution_specification": spec.model_dump(mode="json"),
                "execution_sha256": spec.execution_sha256}

    def verify_fixed_files(self) -> None:
        if self._state != "prepared":
            raise ExecutionPreparationError("execution_preparation_consumed")
        try:
            for path, artifact in self._files:
                if _hash_file(path) != (artifact.size_bytes, artifact.sha256):
                    raise ExecutionPreparationError("execution_fixed_artifact_changed")
        except (OSError, ValueError, ExecutionPreparationError):
            self._state = "failed"
            raise ExecutionPreparationError("execution_fixed_artifact_changed") from None

    def execute(self) -> subprocess.CompletedProcess:
        """Application-only: MUST follow a successful durable reservation.

        No argument/env/command overrides are accepted. Even a failed launch
        consumes this object; worker retry requires a new authorized preparation.
        """
        try:
            self.verify_fixed_files()
        except ExecutionPreparationError:
            self._state = "failed"
            raise
        self._state = "consumed"
        invocation = self._invocation
        try:
            return subprocess.run(list(invocation.argv), cwd=invocation.cwd,
                env=dict(invocation.environment), timeout=invocation.timeout_seconds,
                shell=False, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise ExecutionPreparationError("execution_prepared_timeout") from None
        except OSError:
            raise ExecutionPreparationError("execution_prepared_launch_failed") from None

    def close(self) -> None:
        self._state = "closed"
        # Windows cannot remove read-only files. Only these exact private
        # copies are made writable; never chmod/delete original artifacts.
        for path, _ in self._files:
            if path.exists() and path == path.resolve() and not path.is_symlink():
                path.chmod(stat.S_IWRITE | stat.S_IREAD)
        self._temporary.cleanup()

    def __enter__(self) -> PreparedExecution:
        return self

    def __exit__(self, *_args) -> None:
        self.close()


def prepare_selected_files(*, capability: str, provider: str, model: str,
                           runtime: str, machine_id: str, recipe: str, recipe_version: int,
                           machine: ExecutionMachineObservation,
                           parameters: Mapping[str, object], artifacts: tuple[SelectedArtifact, ...],
                           build_invocation: InvocationBuilder, staging_parent: Path,
                           max_bytes: int) -> PreparedExecution:
    """Copy/hash actual selections and build a command from those same copies.

    No expected hash/report/receipt input. The trusted recipe supplies a finite
    complete selection; this primitive deliberately makes no completeness claim.
    max_bytes is a preparation resource bound, not a provider-call budget.
    """
    temporary = None
    copied: list[tuple[Path, ExecutionArtifact]] = []
    try:
        if type(max_bytes) is not int or max_bytes <= 0 or not 1 <= len(artifacts) <= 1024:
            raise ExecutionPreparationError("execution_preparation_invalid")
        # Detach strict scalar values before any file I/O or builder effects.
        params = dict(parameters)
        if (not params or any(type(key) is not str or not key or key != key.strip() for key in params)
            or any(type(value) not in (str, int, float, bool)
                   or type(value) is float and not math.isfinite(value) for value in params.values())):
            raise ExecutionPreparationError("execution_preparation_invalid")
        selections = []
        keys = set()
        for selected in artifacts:
            if selected.group not in ("model", "runtime"):
                raise ExecutionPreparationError("execution_preparation_invalid")
            # Reuse canonical manifest validation; no caller-supplied hashes.
            ExecutionArtifact(role=selected.role, name=selected.name, size_bytes=1, sha256="0" * 64)
            key = (selected.group, selected.name.casefold())
            if key in keys:
                raise ExecutionPreparationError("execution_preparation_invalid")
            keys.add(key)
            path = Path(os.path.abspath(selected.source))
            _regular_file(path)
            selections.append((selected, path))
        temporary = TemporaryDirectory(prefix="content-os-prepared-", dir=staging_parent)
        base = Path(temporary.name).resolve()
        total = 0
        paths = {}
        for selected, source_path in selections:
            target = base / selected.group / selected.name
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with source_path.open("rb") as source, target.open("xb") as target_file:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    size += len(block)
                    total += len(block)
                    if total > max_bytes:
                        raise ExecutionPreparationError("execution_preparation_size_limit")
                    target_file.write(block)
                    digest.update(block)
            artifact = ExecutionArtifact(role=selected.role, name=selected.name,
                size_bytes=size, sha256=digest.hexdigest())
            copied.append((target, artifact))
            if _hash_file(target) != (size, artifact.sha256):
                raise ExecutionPreparationError("execution_fixed_artifact_changed")
            # Preserve executable bits on POSIX; never grant write permission.
            target.chmod(stat.S_IREAD | (source_path.stat().st_mode & 0o111))
            paths[(selected.group, selected.name)] = target
        spec = ExecutionSpecification(schema_version=1, capability=capability, provider=provider,
            model=model, runtime=runtime, machine_id=machine_id, observation_recipe=recipe,
            observation_version=recipe_version, machine=machine, parameters=params,
            model_artifacts=tuple(item for (selected, _), (_, item) in zip(selections, copied) if selected.group == "model"),
            runtime_artifacts=tuple(item for (selected, _), (_, item) in zip(selections, copied) if selected.group == "runtime"))
        invocation = build_invocation(MappingProxyType(paths), MappingProxyType(params))
        if (not isinstance(invocation, PendingInvocation)
            or not isinstance(invocation.argv, tuple) or not invocation.argv
            or any(not isinstance(value, str) or "\0" in value for value in invocation.argv)
            or not isinstance(invocation.environment, tuple)
            or any(not isinstance(pair, tuple) or len(pair) != 2 or any(not isinstance(v, str) or "\0" in v for v in pair)
                   or not pair[0] or "=" in pair[0] for pair in invocation.environment)
            or len({key for key, _ in invocation.environment}) != len(invocation.environment)
            or type(invocation.timeout_seconds) not in (int, float)
            or not 0 < invocation.timeout_seconds < float("inf")):
            raise ExecutionPreparationError("execution_preparation_invalid")
        # The actual executable must be one of the fixed interpreter artifacts,
        # not PATH lookup or the original installation. Recipe owns imports.
        interpreters = {str(path) for path, entry in copied if entry.role == "interpreter"}
        if invocation.argv[0] not in interpreters or invocation.cwd != base:
            raise ExecutionPreparationError("execution_invocation_not_fixed")
        prepared = PreparedExecution(temporary, spec, tuple(copied), invocation)
        prepared.verify_fixed_files()
        return prepared
    except Exception as exc:
        if temporary is not None:
            for path, _ in copied:
                if path.exists() and path == path.resolve() and not path.is_symlink():
                    path.chmod(stat.S_IWRITE | stat.S_IREAD)
            temporary.cleanup()
        if isinstance(exc, ExecutionPreparationError):
            raise
        raise ExecutionPreparationError("execution_preparation_invalid") from None

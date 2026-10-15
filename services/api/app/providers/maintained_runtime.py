"""Inert maintained OmniVoice preparation and a single-use synthetic diagnostic.

No Worker/HTTP entrypoint, model load, generation or permission issuance.
Normal vendor/OS loading is trusted; this is not an OS sandbox.
"""
from __future__ import annotations

from email.parser import BytesParser
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import signal
import subprocess
from tempfile import TemporaryDirectory
import textwrap
from uuid import UUID
import wave

from app.domain.execution_runtime import (MAX_RUNTIME_DIRECTORIES, MAX_RUNTIME_MANIFEST_BYTES,
    MAX_RUNTIME_FILE_BYTES, RuntimeInventoryFile)
from app.domain.maintained_runtime import (MaintainedEvidenceReference, MaintainedHostObservation,
    MaintainedRuntimeOrigin, MaintainedRuntimeInventory, MAX_MAINTAINED_RUNTIME_FILES, TRUST_POLICY)
from app.domain.models import (ExecutionArtifact, ExecutionMachineObservation,
    ExecutionSpecificationV4, execution_canonical_sha256)
from app.runtime_inventory import RuntimeInventoryError
from .omnivoice_local import OmniVoiceLocalParameters, _json_object
from .prepared import ExecutionPreparationError
from .runtime_tree import _digest, _exact_path


logger = logging.getLogger(__name__)


_PROBE_BODY = '''import json, os, sys, wave, platform
from pathlib import Path
import numpy as np
import soundfile as sf
import torch
from omnivoice.models.omnivoice import OmniVoice
roots = [Path(p).resolve() for p in sys.argv[1:3]]
imports = {}
for name, module in list(sys.modules.items()):
    # Module-instance metadata identifies loaded files. PyTorch's dynamic
    # namespaces inherit class-level placeholder __file__ values, not sources.
    source = vars(module).get("__file__")
    if source and source not in ("frozen", "<frozen>"):
        path = Path(source).resolve()
        if path != Path(__file__).resolve() and not any(path.is_relative_to(root) for root in roots):
            raise RuntimeError("maintained_import_outside_roots")
        imports[name] = str(path)
samples = np.array([-32768, -16384, -1, 0, 1, 16384, 32767] * 40, dtype=np.int16)
sf.write("synthetic.wav", samples, 24000, format="WAV", subtype="PCM_16")
decoded, rate = sf.read("synthetic.wav", dtype="int16")
if rate != 24000 or not np.array_equal(decoded, samples):
    raise RuntimeError("maintained_pcm_roundtrip_failed")
if os.environ["CONTENT_OS_PROBE_DEVICE"] == "cuda:0" and not torch.cuda.is_available():
    raise RuntimeError("maintained_cuda_unavailable")
report = dict(python=sys.version, python_version=platform.python_version(), torch=torch.__version__, soundfile=sf.__version__,
              numpy=np.__version__, libsndfile=sf.__libsndfile_version__,
              model_loaded=False, frames=len(samples), imports=imports)
raw = json.dumps(report, sort_keys=True).encode()
if len(raw) > 1048576:
    raise RuntimeError("maintained_probe_report_limit")
Path("probe-result.json").write_bytes(raw)
'''
PROBE_SOURCE = ("import json, sys\nfrom pathlib import Path\ndef main():\n" + textwrap.indent(_PROBE_BODY, "    ") + '''
try:
    main()
except Exception as error:
    missing = getattr(error, "name", None)
    if missing is not None and (len(missing) > 200 or not all(c.isalnum() or c in "._" for c in missing)):
        missing = None
    Path("probe-error.json").write_text(json.dumps(dict(error_type=type(error).__name__, missing_module=missing)))
    raise SystemExit(1)
''').encode("utf-8")


def _distinct_json(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))


def _evidence_bytes(data_root, reference):
    root = Path(data_root)
    _exact_path(root, directory=True)
    path = root / reference.reference
    try:
        _exact_path(path, directory=False)
        if not path.is_relative_to(root) or path.stat().st_size > 1024 * 1024:
            raise ValueError()
        with path.open("rb") as source:
            raw = source.read(1024 * 1024 + 1)
        if not raw or len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise ValueError()
        return raw
    except (OSError, ValueError, ExecutionPreparationError):
        raise RuntimeInventoryError("maintained_origin_evidence_changed") from None


def require_maintained_origin(data_root, reference, inventory):
    return _require_origin(data_root, reference, inventory, MaintainedRuntimeOrigin)


def _require_origin(data_root, reference, inventory, origin_type):
    """Current evidence bytes, not an independently observed runtime witness."""
    try:
        reference = MaintainedEvidenceReference.model_validate_json(reference.model_dump_json())
        raw = _evidence_bytes(data_root, reference)
        origin = origin_type.model_validate_json(json.dumps(_distinct_json(raw)))
        if origin.runtime_inventory_sha256 != inventory.descriptor.sha256:
            raise ValueError()
        hooks = sorted(item.name for item in inventory.files if
            item.name.endswith((".pth", "._pth")) or item.name.rsplit("/", 1)[-1] in ("sitecustomize.py", "usercustomize.py"))
        if sorted(origin.startup_hooks) != hooks:
            raise ValueError()
        for distribution in origin.distributions:
            _evidence_bytes(data_root, distribution.source)
        if origin.network_control is not None:
            _evidence_bytes(data_root, origin.network_control)
        return origin
    except RuntimeInventoryError:
        raise
    except (ValueError, TypeError, AttributeError):
        raise RuntimeInventoryError("maintained_origin_invalid") from None


def observe_inventory(environment, base, *, max_bytes):
    """Legacy diagnostic observer; its owned entry and hashes stay unchanged."""
    return _observe_inventory(environment, base, max_bytes=max_bytes,
        entry_name="entry/probe.py", entry_source=PROBE_SOURCE)


def _observe_inventory(environment, base, *, max_bytes, entry_name, entry_source):
    """Complete file membership, including executable bytecode; no PE inspection."""
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_RUNTIME_FILE_BYTES:
        raise ExecutionPreparationError("maintained_inventory_limit_invalid")
    roots = (Path(environment), Path(base))
    for root in roots:
        _exact_path(root, directory=True)
    if roots[0].is_relative_to(roots[1]) or roots[1].is_relative_to(roots[0]):
        raise ExecutionPreparationError("maintained_runtime_roots_overlap")
    cfg = roots[0] / "pyvenv.cfg"
    _exact_path(cfg, directory=False)
    if cfg.stat().st_size > 8192:
        raise ExecutionPreparationError("maintained_venv_invalid")
    values = {}
    for line in cfg.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            if key.strip() in values:
                raise ExecutionPreparationError("maintained_venv_invalid")
            values[key.strip()] = value.strip()
    if values.get("home") != str(roots[1]) or values.get("include-system-site-packages") != "false":
        raise ExecutionPreparationError("maintained_venv_base_mismatch")
    # Reject an oversized installation before reading every file's contents.
    # The later observation still checks current membership and every byte.
    file_count, directory_count = 1, 1  # owned entrypoint and entry directory
    observed_bytes = len(entry_source)
    manifest_bytes = len(json.dumps(dict(inventory_version=2, directories=[], files=[]),
        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    manifest_bytes += len(json.dumps("entry").encode())
    def file_manifest_size(name, role, size):
        # Fixed-width placeholder measures JSON size only; it is never retained as evidence.
        return len(json.dumps(dict(name=name, role=role, size_bytes=size, sha256="0" * 64),
            sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    manifest_bytes += file_manifest_size(entry_name, "entrypoint", len(entry_source))
    def count_error(_):
        raise ExecutionPreparationError("maintained_inventory_unavailable")
    for prefix, root in zip(("environment", "base"), roots):
        directory_count += 1
        manifest_bytes += 1 + len(json.dumps(prefix).encode())
        for directory, folders, files in os.walk(root, followlinks=False, onerror=count_error):
            directory_count += len(folders)
            file_count += len(files)
            if file_count > MAX_MAINTAINED_RUNTIME_FILES:
                raise ExecutionPreparationError("maintained_inventory_limit")
            if directory_count > MAX_RUNTIME_DIRECTORIES:
                raise ExecutionPreparationError("maintained_inventory_member_limit")
            current = Path(directory)
            for name in folders:
                logical = prefix + "/" + (current / name).relative_to(root).as_posix()
                manifest_bytes += 1 + len(json.dumps(logical, ensure_ascii=False).encode())
            for name in files:
                path = current / name
                size = path.stat().st_size
                observed_bytes += size
                logical = prefix + "/" + path.relative_to(root).as_posix()
                role = "interpreter" if logical == "environment/Scripts/python.exe" else "dependency"
                manifest_bytes += 1 + file_manifest_size(logical, role, size)
            if observed_bytes > max_bytes:
                raise ExecutionPreparationError("maintained_inventory_limit")
            if manifest_bytes > MAX_RUNTIME_MANIFEST_BYTES:
                raise ExecutionPreparationError("maintained_inventory_manifest_limit")
    logger.info("Maintained inventory pre-count: files=%d directories=%d bytes=%d manifest_bytes=%d",
        file_count, directory_count, observed_bytes, manifest_bytes)
    directories, entries, total = {"entry"}, [], len(entry_source)
    for prefix, root in zip(("environment", "base"), roots):
        directories.add(prefix)
        def walk_error(_):
            raise ExecutionPreparationError("maintained_inventory_unavailable")
        for directory, folders, files in os.walk(root, followlinks=False, onerror=walk_error):
            current = Path(directory)
            _exact_path(current, directory=True)
            for name in folders:
                path = current / name
                _exact_path(path, directory=True)
                directories.add(prefix + "/" + path.relative_to(root).as_posix())
                if len(directories) > MAX_RUNTIME_DIRECTORIES:
                    raise ExecutionPreparationError("maintained_inventory_member_limit")
            for name in sorted(files):
                path = current / name
                logical = prefix + "/" + path.relative_to(root).as_posix()
                if len(entries) >= MAX_MAINTAINED_RUNTIME_FILES - 1 or path.stat().st_size > max_bytes - total:
                    raise ExecutionPreparationError("maintained_inventory_limit")
                size, digest = _digest(path, max_bytes - total)
                total += size
                role = "interpreter" if logical == "environment/Scripts/python.exe" else "dependency"
                entries.append(RuntimeInventoryFile(name=logical, role=role, size_bytes=size, sha256=digest))
                if len(entries) % 1000 == 0:
                    logger.info("Maintained inventory hash progress: files=%d/%d bytes=%d", len(entries), file_count - 1, total)
    entries.append(RuntimeInventoryFile(name=entry_name, role="entrypoint",
        size_bytes=len(entry_source), sha256=hashlib.sha256(entry_source).hexdigest()))
    return MaintainedRuntimeInventory(inventory_version=2, directories=tuple(sorted(directories)), files=tuple(entries))


def observe_host(environment, base, model_root, data_root, *, device, gpu_uuid):
    if platform.system() != "Windows" or platform.machine().lower() != "amd64":
        raise ExecutionPreparationError("maintained_host_unsupported")
    layout = {key: str(value) for key, value in zip(("environment", "base", "model", "data_root"),
        (environment, base, model_root, data_root))}
    layout["system_root"] = str(_system_root())
    driver = {}
    if device == "cuda:0":
        # A mature OS/vendor diagnostic, not a native loader or caller callback.
        # Use its normal trusted Windows environment. A replacement two-variable
        # environment breaks NVML initialization on the supported local host.
        # This host query is separate from the isolated provider diagnostic child.
        tool = _system_root() / "System32/nvidia-smi.exe"
        _, tool_digest = _digest(tool, 64 * 1024 * 1024)
        try:
            result = subprocess.run([str(tool), "--query-gpu=uuid,driver_version", "--format=csv,noheader"],
                shell=False, check=False, capture_output=True, timeout=10)
            if result.returncode != 0 or len(result.stdout) > 8192:
                raise ValueError()
            rows = [line.split(",") for line in result.stdout.decode("ascii").splitlines()]
            selected = [row for row in rows if len(row) == 2 and row[0].strip() == gpu_uuid]
            if len(selected) != 1:
                raise ValueError()
            driver = dict(gpu_uuid=gpu_uuid, driver_version=selected[0][1].strip(), driver_probe_sha256=tool_digest)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise ExecutionPreparationError("maintained_cuda_observation_unavailable") from None
    elif gpu_uuid is not None:
        raise ExecutionPreparationError("maintained_cpu_driver_mismatch")
    return MaintainedHostObservation(os_family="windows", architecture="amd64", os_version=platform.version(),
        host_sha256=execution_canonical_sha256({"host": platform.node(), "os": platform.version(),
            "processor": platform.processor(), "architecture": platform.machine()}),
        layout_sha256=execution_canonical_sha256(layout), device=device, **driver)


def _system_root():
    # Standard-library registry access; never the caller's SystemRoot/PATH override.
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
            value, _ = winreg.QueryValueEx(key, "SystemRoot")
        result = Path(value)
        _exact_path(result, directory=True)
        return result
    except (OSError, ValueError):
        raise ExecutionPreparationError("maintained_system_root_unavailable") from None


def _distribution_versions(environment, base):
    cfg = dict(line.split("=", 1) for line in (environment / "pyvenv.cfg").read_text().splitlines() if "=" in line)
    result = {"base/cpython": next((v.strip() for k, v in cfg.items() if k.strip() == "version"), "")}
    for prefix, root in zip(("environment", "base"), (environment, base)):
        for path in (root / "Lib/site-packages").glob("*.dist-info/METADATA"):
            if path.stat().st_size > 1024 * 1024:
                raise ExecutionPreparationError("maintained_distribution_metadata_limit")
            metadata = BytesParser().parsebytes(path.read_bytes())
            name, version = metadata.get("Name"), metadata.get("Version")
            key = prefix + "/" + (name.casefold() if name else "")
            if (not name or not version or key in result
                or len(metadata.get_all("Name", [])) != 1 or len(metadata.get_all("Version", [])) != 1):
                raise ExecutionPreparationError("maintained_distribution_metadata_invalid")
            result[key] = version
    return result


def _check_startup_roots(origin, environment, base):
    """Reject declared path additions outside the inventoried roots before import.

    Executable hook lines are explicitly operator-accepted code, not sandboxed.
    """
    roots = {"environment": environment, "base": base}
    for logical in origin.startup_hooks:
        prefix, relative = logical.split("/", 1)
        if prefix not in roots:
            raise ExecutionPreparationError("maintained_startup_root_unsupported")
        path = roots[prefix] / relative
        if not path.name.endswith((".pth", "._pth")):
            continue
        if path.stat().st_size > 1024 * 1024:
            raise ExecutionPreparationError("maintained_startup_hook_limit")
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#") or line.startswith(("import ", "import\t")):
                continue
            addition = (path.parent / line.rstrip()).resolve()
            if not any(addition.is_relative_to(root) for root in roots.values()):
                raise ExecutionPreparationError("maintained_startup_root_unsupported")


def _model_artifacts(model_root, limit):
    # Maintained recipe's explicit catalog. Legacy/native selectors retain theirs.
    required = {"config.json": "configuration", "model.safetensors": "weights",
        "tokenizer.json": "tokenizer", "tokenizer_config.json": "configuration",
        "audio_tokenizer/config.json": "configuration", "audio_tokenizer/model.safetensors": "auxiliary_weights",
        "audio_tokenizer/preprocessor_config.json": "configuration"}
    optional = {"generation_config.json": "configuration", "special_tokens_map.json": "tokenizer",
        "vocab.json": "tokenizer", "merges.txt": "tokenizer", "chat_template.jinja": "tokenizer",
        "audio_tokenizer/generation_config.json": "configuration"}
    metadata = {"README.md", "LICENSE", "LICENSE.txt", ".gitattributes"}
    selected = {}
    _exact_path(model_root, directory=True)
    for root in (model_root, model_root / "audio_tokenizer"):
        _exact_path(root, directory=True)
        for path in root.iterdir():
            name = path.relative_to(model_root).as_posix()
            if name == "audio_tokenizer" and path.is_dir():
                continue
            _exact_path(path, directory=False)
            if path.name in metadata:
                continue
            role = required.get(name) or optional.get(name)
            if role is None:
                raise ExecutionPreparationError("maintained_model_artifact_unsupported")
            if path.stat().st_size <= 0:
                raise ExecutionPreparationError("omnivoice_local_artifact_missing")
            selected[name] = (role, path)
    if not required.keys() <= selected.keys() or (("vocab.json" in selected) != ("merges.txt" in selected)):
        raise ExecutionPreparationError("omnivoice_local_artifact_missing")
    configs = {name: _json_object(path) for name, (_, path) in selected.items()
        if name.endswith(".json") and name != "tokenizer.json"}
    if (configs["config.json"].get("model_type") != "omnivoice"
        or configs["audio_tokenizer/config.json"].get("model_type") != "higgs_audio_v2_tokenizer"
        or configs["tokenizer_config.json"].get("tokenizer_class") not in ("Qwen2Tokenizer", "Qwen2TokenizerFast")
        or configs["tokenizer_config.json"].get("tokenizer_file", "tokenizer.json") not in (None, "tokenizer.json")):
        raise ExecutionPreparationError("maintained_model_configuration_unsupported")
    result, total = [], 0
    for name, (role, path) in sorted(selected.items()):
        size, digest = _digest(path, limit - total)
        total += size
        result.append(ExecutionArtifact(role=role, name=name, size_bytes=size, sha256=digest))
    return tuple(result)


def stop_maintained_process_tree(child):
    """Shared existing diagnostic recovery; no new process framework."""
    if os.name == "nt":
        tool = _system_root() / "System32/taskkill.exe"
        result = subprocess.run([str(tool), "/PID", str(child.pid), "/T", "/F"],
            shell=False, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if result.returncode != 0:
            raise ExecutionPreparationError("maintained_process_tree_stop_unverified")
    else:
        os.killpg(child.pid, signal.SIGKILL)
    child.wait(timeout=10)


class PreparedMaintainedRuntime:
    """One retained observation/diagnostic; deliberately no provider execute method."""
    def __init__(self, environment, base, model_root, data_root, temporary, spec, limit, gpu_uuid, operator_key):
        self._roots = (environment, base, model_root, data_root)
        self._temporary, self._spec_json, self._limit = temporary, spec.model_dump_json(), limit
        self._gpu_uuid, self._operator_key, self._state = gpu_uuid, operator_key, "prepared"

    @property
    def specification(self):
        return ExecutionSpecificationV4.model_validate_json(self._spec_json)

    def verify(self):
        if self._state != "prepared":
            raise ExecutionPreparationError("maintained_preparation_consumed")
        try:
            environment, base, model_root, data_root = self._roots
            spec = self.specification
            inventory = observe_inventory(environment, base, max_bytes=self._limit)
            origin = require_maintained_origin(data_root, spec.runtime_origin, inventory)
            if origin.network_isolation != "not_asserted":
                raise ExecutionPreparationError("maintained_external_network_control_unverified")
            from app.execution_admission import configured_admission_actor
            if configured_admission_actor(self._operator_key) != origin.actor:
                raise ExecutionPreparationError("maintained_diagnostic_operator_required")
            _check_startup_roots(origin, environment, base)
            if _distribution_versions(environment, base) != {d.root + "/" + d.name.casefold(): d.version for d in origin.distributions}:
                raise ExecutionPreparationError("maintained_distribution_origin_mismatch")
            artifacts = _model_artifacts(model_root, self._limit)
            host = observe_host(*self._roots, device=spec.parameters["device"], gpu_uuid=self._gpu_uuid)
            entry = Path(self._temporary.name) / "probe.py"
            _exact_path(entry.parent, directory=True)
            _exact_path(entry, directory=False)
            if (inventory.descriptor != spec.runtime_inventory or host != spec.host_runtime
                or sorted(artifacts, key=lambda a: a.name) != sorted(spec.model_artifacts, key=lambda a: a.name)
                or entry.read_bytes() != PROBE_SOURCE):
                raise ExecutionPreparationError("maintained_prepared_identity_changed")
            return spec
        except Exception as error:
            self._state = "failed"
            if isinstance(error, (ExecutionPreparationError, RuntimeInventoryError)):
                raise
            raise ExecutionPreparationError("maintained_prepared_identity_changed") from None

    def execution_use_snapshot(self):
        spec = self.verify()
        return dict(snapshot_version=2, identity=spec.identity.model_dump(mode="json"),
            execution_specification=spec.model_dump(mode="json"), execution_sha256=spec.execution_sha256)

    def _stop_tree(self, child):
        stop_maintained_process_tree(child)

    def probe_import_pcm(self):
        """Only explicit origin/operator-authorized diagnostics; never generation."""
        spec = self.verify()
        self._state = "consumed"
        environment, base, _, _ = self._roots
        private = Path(self._temporary.name)
        system = str(_system_root())
        env = {"SystemRoot": system, "PATH": str(base) + os.pathsep + str(Path(system) / "System32"),
            "TEMP": str(private), "TMP": str(private), "HF_HOME": str(private / "cache"),
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
            "GRADIO_ANALYTICS_ENABLED": "False", "CONTENT_OS_PROBE_DEVICE": spec.parameters["device"]}
        if self._gpu_uuid is not None:
            env["CUDA_VISIBLE_DEVICES"] = self._gpu_uuid
        child = None
        try:
            child = subprocess.Popen([str(environment / "Scripts/python.exe"), "-I", "-B", str(private / "probe.py"),
                str(environment), str(base)], cwd=private, env=env, shell=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}))
            code = child.wait(timeout=60)
            if code != 0:
                error_file = private / "probe-error.json"
                if error_file.is_file():
                    _exact_path(error_file, directory=False)
                    if error_file.stat().st_size <= 4096:
                        failure = _distinct_json(error_file.read_bytes())
                        if failure.get("error_type") == "ModuleNotFoundError":
                            raise ExecutionPreparationError("maintained_probe_dependency_missing")
                raise ExecutionPreparationError("maintained_probe_failed")
            report_path, audio = private / "probe-result.json", private / "synthetic.wav"
            for path, limit in ((report_path, 1024 * 1024), (audio, 2048)):
                _exact_path(path, directory=False)
                if path.stat().st_size > limit:
                    raise ExecutionPreparationError("maintained_probe_output_limit")
            report = _distinct_json(report_path.read_bytes())
            versions = _distribution_versions(environment, base)
            if (not isinstance(report, dict) or report.get("frames") != 280
                or report.get("python_version") != versions["base/cpython"]
                or any(report.get(name) != versions.get("environment/" + name) for name in ("torch", "numpy", "soundfile"))):
                raise ExecutionPreparationError("maintained_probe_version_mismatch")
            imports = report.get("imports")
            if not isinstance(imports, dict) or not {"torch", "numpy", "soundfile", "omnivoice.models.omnivoice"}.issubset(imports):
                raise ExecutionPreparationError("maintained_probe_import_report_invalid")
            inventory = observe_inventory(environment, base, max_bytes=self._limit)
            files = {item.name for item in inventory.files}
            for source in imports.values():
                if not isinstance(source, str):
                    raise ValueError()
                path = Path(source)
                _exact_path(path, directory=False)
                if path == private / "probe.py":
                    continue
                logical = next((prefix + "/" + path.relative_to(root).as_posix()
                    for prefix, root in (("environment", environment), ("base", base)) if path.is_relative_to(root)), None)
                if logical not in files:
                    raise ExecutionPreparationError("maintained_probe_import_outside_inventory")
            with wave.open(str(audio), "rb") as source:
                expected = [-32768, -16384, -1, 0, 1, 16384, 32767] * 40
                import struct
                if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getnframes()) != (1, 2, 24000, 280):
                    raise ValueError()
                if list(struct.unpack("<280h", source.readframes(280))) != expected or report.get("model_loaded") is not False:
                    raise ValueError()
            # Imports must not change the observed installation; -B prevents new bytecode writes.
            self._state = "prepared"
            self.verify()
            self._state = "consumed"
            return report
        except BaseException as error:
            self._state = "failed"
            if child is not None and child.poll() is None:
                self._stop_tree(child)
            if isinstance(error, subprocess.TimeoutExpired):
                raise ExecutionPreparationError("maintained_probe_timeout") from None
            if isinstance(error, (ExecutionPreparationError, KeyboardInterrupt)):
                raise
            raise ExecutionPreparationError("maintained_probe_failed") from None

    def close(self):
        self._state = "closed"
        self._temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def prepare_maintained_omnivoice(*, environment, base, model_root, data_root, staging_parent,
        origin_reference, operator_key, parameters, installation_id, runtime_label, machine_id,
        gpu_uuid=None, max_bytes=MAX_RUNTIME_FILE_BYTES):
    """Select actual roots first; no expected execution digest, loader or callback."""
    temporary = None
    try:
        environment, base, model_root, data_root, staging_parent = map(Path,
            (environment, base, model_root, data_root, staging_parent))
        for root in (model_root, data_root, staging_parent):
            _exact_path(root, directory=True)
        if (not model_root.is_relative_to(data_root) or not staging_parent.is_relative_to(data_root)
            or any(staging_parent.is_relative_to(r) for r in (environment, base, model_root))):
            raise ExecutionPreparationError("maintained_staging_boundary_invalid")
        if type(parameters) is not dict or parameters.get("audio_tokenizer_precision") != "float32":
            raise ExecutionPreparationError("maintained_effective_parameters_required")
        kwargs = {k: v for k, v in parameters.items() if k != "audio_tokenizer_precision"}
        effective = OmniVoiceLocalParameters.model_validate(kwargs).model_dump()
        effective["audio_tokenizer_precision"] = "float32"
        inventory = observe_inventory(environment, base, max_bytes=max_bytes)
        origin = require_maintained_origin(data_root, origin_reference, inventory)
        if origin.network_isolation != "not_asserted":
            raise ExecutionPreparationError("maintained_external_network_control_unverified")
        from app.execution_admission import configured_admission_actor
        if configured_admission_actor(operator_key) != origin.actor:
            raise ExecutionPreparationError("maintained_diagnostic_operator_required")
        _check_startup_roots(origin, environment, base)
        if _distribution_versions(environment, base) != {d.root + "/" + d.name.casefold(): d.version for d in origin.distributions}:
            raise ExecutionPreparationError("maintained_distribution_origin_mismatch")
        host = observe_host(environment, base, model_root, data_root, device=effective["device"], gpu_uuid=gpu_uuid)
        artifacts = _model_artifacts(model_root, max_bytes)
        spec = ExecutionSpecificationV4(schema_version=4, trust_policy=TRUST_POLICY, capability="voice", mode="local",
            provider="omnivoice", model=str(model_root), runtime=runtime_label, machine_id=machine_id,
            observation_recipe="omnivoice-maintained-import-pcm", observation_version=1, model_artifacts=artifacts,
            runtime_inventory=inventory.descriptor, runtime_origin=origin_reference, host_runtime=host,
            machine=ExecutionMachineObservation(installation_id=UUID(str(installation_id)), host_sha256=host.host_sha256,
                device_sha256=execution_canonical_sha256(host.model_dump(mode="json"))), parameters=effective)
        temporary = TemporaryDirectory(prefix="maintained-probe-", dir=staging_parent)
        (Path(temporary.name) / "probe.py").write_bytes(PROBE_SOURCE)
        prepared = PreparedMaintainedRuntime(environment, base, model_root, data_root, temporary, spec, max_bytes, gpu_uuid, operator_key)
        prepared.verify()
        return prepared
    except Exception as error:
        if temporary is not None:
            temporary.cleanup()
        if isinstance(error, (ExecutionPreparationError, RuntimeInventoryError)):
            raise
        raise ExecutionPreparationError("maintained_preparation_invalid") from None

"""Bounded local Voice child. Installation observations are not per-job proof."""
import hashlib
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import wave

from .maintained_runtime import _observe_inventory, _require_origin, _system_root, stop_maintained_process_tree
from app.domain.maintained_runtime import MaintainedGenerationOrigin
from .maintained_voice_bundle import BUNDLE_SLOT, bundle_bytes
from .maintained_voice_child import MaintainedVoiceChildRequest, request_bytes
from .omnivoice_entry_v2 import read_reference
from .omnivoice_local import _json_object
from .prepared import ExecutionPreparationError, PendingInvocation
from .runtime_tree import _exact_path


def observe_child_inventory(environment, base, *, max_bytes):
    return _observe_inventory(environment, base, max_bytes=max_bytes,
        entry_name=BUNDLE_SLOT, entry_source=bundle_bytes())


def require_child_origin(data_root, reference, inventory):
    origin = _require_origin(data_root, reference, inventory, MaintainedGenerationOrigin)
    raw = bundle_bytes()
    entries = [row for row in inventory.files if row.role == "entrypoint"]
    if (len(entries) != 1 or entries[0].name != BUNDLE_SLOT or entries[0].size_bytes != len(raw)
        or entries[0].sha256 != hashlib.sha256(raw).hexdigest()):
        raise ExecutionPreparationError("maintained_voice_bundle_identity_changed")
    return origin


def _validate_request(request, options):
    root, model = Path(request.data_root), Path(request.model_root)
    inputs = request.inputs
    target, source = Path(inputs.output_path), Path(inputs.reference_audio)
    for path in (root, model, target.parent):
        _exact_path(path, directory=True)
    _exact_path(source, directory=False)
    if (root != Path(options["data_root"]) or model != Path(options["model_root"])
        or target != target.resolve() or target.suffix.lower() != ".wav"
        or not target.is_relative_to(root) or not source.is_relative_to(root)
        or any(target.is_relative_to(Path(options[key])) for key in ("environment", "base", "model_root"))
        or target == source or (target.exists() and not options.get("replay_only", False))):
        raise ExecutionPreparationError("maintained_voice_input_boundary_invalid")
    if request.parameters.model_dump() != options["parameters"]:
        raise ExecutionPreparationError("maintained_voice_input_parameters_changed")
    rate = _json_object(model / "audio_tokenizer/preprocessor_config.json").get("sampling_rate")
    if type(rate) is not int or not 0 < rate <= 192_000:
        raise ExecutionPreparationError("maintained_voice_input_rate_invalid")
    read_reference(inputs, rate)
    return rate


class PreparedMaintainedVoiceChild:
    """One private request and fixed command; authorization stays in the Worker.

    This does not observe or certify the installed Python environment/weights.
    Same-user process is not an OS sandbox.
    """
    def __init__(self, temporary, options, request, timeout_seconds):
        self._temporary, self._options = temporary, options
        self._request_raw = request_bytes(request)
        self._state = "prepared"
        private = Path(temporary.name)
        system = _system_root()
        env = {"SystemRoot": str(system), "PATH": str(options["base"]) + os.pathsep + str(system / "System32"),
            "TEMP": str(private), "TMP": str(private), "HF_HOME": str(private / "cache"),
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
            "GRADIO_ANALYTICS_ENABLED": "False"}
        if options["gpu_uuid"] is not None:
            env["CUDA_VISIBLE_DEVICES"] = options["gpu_uuid"]
        self._invocation = PendingInvocation(argv=(str(Path(options["environment"]) / "Scripts/python.exe"),
            "-I", "-B", str(private / "application.pyz"), str(private / "request.json")),
            cwd=private, environment=tuple(sorted(env.items())), timeout_seconds=timeout_seconds)

    @property
    def invocation(self): return self._invocation

    @property
    def input_sha256(self): return hashlib.sha256(self._request_raw).hexdigest()

    def request_snapshot(self):
        """Detached request for application Job binding, never permission."""
        return MaintainedVoiceChildRequest.model_validate_json(self._request_raw)

    def _verify_owned_files(self):
        private = Path(self._temporary.name)
        for name, expected in (("application.pyz", bundle_bytes()), ("request.json", self._request_raw)):
            path = private / name
            _exact_path(path, directory=False)
            if path.stat().st_size != len(expected) or path.read_bytes() != expected:
                raise ExecutionPreparationError("maintained_voice_child_identity_changed")

    def verify(self):
        if self._state != "prepared":
            raise ExecutionPreparationError("maintained_preparation_consumed")
        try:
            self._verify_owned_files()
            request = MaintainedVoiceChildRequest.model_validate_json(self._request_raw)
            _validate_request(request, self._options)
            return request
        except Exception:
            self._state = "failed"
            raise ExecutionPreparationError("maintained_voice_child_identity_changed") from None

    def execute(self):
        if self._options.get("replay_only", False):
            raise ExecutionPreparationError("maintained_voice_replay_only")
        request = self.verify()
        self._state = "consumed"
        child = None
        try:
            call = self.invocation
            child = subprocess.Popen(list(call.argv), cwd=call.cwd, env=dict(call.environment), shell=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}))
            if child.wait(timeout=call.timeout_seconds) != 0:
                raise ExecutionPreparationError("maintained_voice_child_failed")
            request = MaintainedVoiceChildRequest.model_validate_json(self._request_raw)
            target = Path(request.inputs.output_path)
            _exact_path(target, directory=False)
            if not 44 < target.stat().st_size <= request.parameters.output_max_bytes:
                raise ExecutionPreparationError("maintained_voice_child_output_invalid")
            rate = _json_object(Path(request.model_root) / "audio_tokenizer/preprocessor_config.json")["sampling_rate"]
            with wave.open(str(target), "rb") as audio:
                frames = audio.getnframes()
                if (audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != rate
                    or not 0 < frames <= request.parameters.output_max_frames
                    or len(audio.readframes(frames)) != frames * 2):
                    raise ExecutionPreparationError("maintained_voice_child_output_invalid")
            self._verify_owned_files()
            read_reference(request.inputs, rate)
            return target
        except BaseException as error:
            self._state = "failed"
            if child is not None and child.poll() is None:
                stop_maintained_process_tree(child)
            if isinstance(error, subprocess.TimeoutExpired):
                raise ExecutionPreparationError("maintained_voice_child_timeout") from None
            if isinstance(error, (ExecutionPreparationError, KeyboardInterrupt)):
                raise
            raise ExecutionPreparationError("maintained_voice_child_failed") from None

    def close(self):
        self._state = "closed"
        self._temporary.cleanup()

    def __enter__(self): return self
    def __exit__(self, *_args): self.close()


def prepare_maintained_voice_child(*, environment, base, model_root, data_root, staging_parent,
        parameters, inputs, timeout_seconds=600, gpu_uuid=None, replay_only=False):
    temporary = None
    try:
        if type(replay_only) is not bool:
            raise ExecutionPreparationError("maintained_voice_replay_mode_invalid")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
            raise ExecutionPreparationError("maintained_voice_child_timeout_invalid")
        request = MaintainedVoiceChildRequest.model_validate_json(MaintainedVoiceChildRequest(
            data_root=str(data_root), model_root=str(model_root), parameters=parameters, inputs=inputs).model_dump_json())
        options = dict(environment=environment, base=base, model_root=model_root, data_root=data_root,
            staging_parent=staging_parent, parameters=request.parameters.model_dump(),
            gpu_uuid=gpu_uuid, replay_only=replay_only)
        _validate_request(request, options)
        _exact_path(Path(environment) / "Scripts/python.exe", directory=False)
        _exact_path(Path(base), directory=True)
        temporary = TemporaryDirectory(prefix="maintained-voice-child-", dir=staging_parent)
        private = Path(temporary.name)
        (private / "application.pyz").write_bytes(bundle_bytes())
        (private / "request.json").write_bytes(request_bytes(request))
        prepared = PreparedMaintainedVoiceChild(temporary, options, request, timeout_seconds)
        prepared.verify()
        return prepared
    except Exception as error:
        if temporary is not None: temporary.cleanup()
        if isinstance(error, ExecutionPreparationError): raise
        raise ExecutionPreparationError("maintained_voice_child_preparation_invalid") from None

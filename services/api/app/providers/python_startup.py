"""M2 fixed Windows CPython startup/command seam, synthetic acceptance only."""
from __future__ import annotations

from pathlib import Path
import subprocess

from .prepared import ExecutionPreparationError, PendingInvocation
from .prepared import NativeExecutionUnsupported
from .runtime_tree import (OwnedRuntimeFile, PreparedRuntimeTree, RuntimeFileSelection,
    RuntimeTreeSelection, _exact_path, prepare_runtime_tree)


IMPORT_ROOTS = ("Lib", "DLLs", "Lib/site-packages")
BOOTSTRAP = "content_os_bootstrap.py"


def startup_owned_files() -> tuple[OwnedRuntimeFile, ...]:
    # CPython Windows can use either DLL-named or executable-named _pth.
    # Both are fixed identically. Neither contains an `import site` directive.
    config = b"Lib\nDLLs\nLib/site-packages\n"
    source = Path(__file__).with_name("python_bootstrap.py")
    _exact_path(source, directory=False)
    payload = source.read_bytes()
    return (OwnedRuntimeFile("python312._pth", config),
        OwnedRuntimeFile("python._pth", config), OwnedRuntimeFile(BOOTSTRAP, payload, "entrypoint"))


class PreparedPythonCommand:
    """One frozen invocation/tree; application must reserve before execute.

    No execution override, fallback or retry. This is NOT a Worker authority
    snapshot: model bytes, host/DLL/device observations and gates are pending.
    """
    def __init__(self, tree: PreparedRuntimeTree, invocation: PendingInvocation):
        self._tree, self._invocation = tree, invocation
        self._state = "prepared"

    @property
    def inventory(self):
        return self._tree.inventory

    @property
    def invocation(self):
        return self._invocation

    def verify(self):
        if self._state != "prepared":
            raise ExecutionPreparationError("execution_preparation_consumed")
        try:
            return self._tree.verify()
        except ExecutionPreparationError:
            self._state = "failed"
            raise

    def execute(self):
        if self._state != "prepared":
            raise ExecutionPreparationError("execution_preparation_consumed")
        try:
            self._tree.claim_for_launch()
        except ExecutionPreparationError:
            self._state = "failed"
            raise
        self._state = "consumed"
        try:
            return subprocess.run(list(self.invocation.argv), cwd=self.invocation.cwd,
                env=dict(self.invocation.environment), timeout=self.invocation.timeout_seconds,
                shell=False, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise ExecutionPreparationError("execution_prepared_timeout") from None
        except OSError:
            raise ExecutionPreparationError("execution_prepared_launch_failed") from None

    def close(self):
        self._state = "closed"
        self._tree.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def prepare_python_command(*, trees: tuple[RuntimeTreeSelection, ...],
                           files: tuple[RuntimeFileSelection, ...], staging_parent: Path,
                           max_bytes: int, windows_root: Path, work_directory: Path,
                           arguments: tuple[str, ...], timeout_seconds: float) -> PreparedPythonCommand:
    """Closed 3.12 non-venv layout; no ambient env or expected receipt inputs.

    windows_root must eventually come from M3's trusted OS observer, not user
    labels. Validating a directory here does not establish OS/DLL authority.
    Initial recipe conservatively rejects bytecode-containing source trees.
    """
    tree = None
    try:
        if (not isinstance(arguments, tuple) or len(arguments) > 32
            or any(type(arg) is not str or "\0" in arg or len(arg) > 100_000 for arg in arguments)
            or sum(len(arg.encode("utf-16-le")) // 2 for arg in arguments) > 24_000
            or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds < float("inf")):
            raise ExecutionPreparationError("execution_python_command_invalid")
        _exact_path(windows_root, directory=True)
        _exact_path(windows_root / "System32", directory=True)
        _exact_path(work_directory, directory=True)
        tree = prepare_runtime_tree(trees=trees, files=files, staging_parent=staging_parent,
            max_bytes=max_bytes, owned_files=startup_owned_files())
        names = {entry.name: entry for entry in tree.inventory.files}
        required = {"python.exe", "python312.dll", "python3.dll", "Lib/os.py",
            "Lib/encodings/__init__.py", "Lib/site-packages/app/providers/omnivoice_entry.py"}
        if (not required.issubset(names) or any(names[name].size_bytes == 0 for name in required)
            or names["python.exe"].role != "interpreter"
            or not set(IMPORT_ROOTS).issubset(tree.inventory.directories)
            or any(name.casefold().endswith(("._pth", "pyvenv.cfg"))
                   and name not in {"python._pth", "python312._pth"} for name in names)
            or work_directory.is_relative_to(tree.root) or windows_root.is_relative_to(tree.root)):
            raise ExecutionPreparationError("execution_python_layout_unsupported")
        # Explicit replacement env, never merge os.environ/PATH/PYTHONPATH.
        environment = (("SystemRoot", str(windows_root)), ("WINDIR", str(windows_root)),
            ("TEMP", str(work_directory)), ("TMP", str(work_directory)),
            ("HF_HUB_OFFLINE", "1"), ("TRANSFORMERS_OFFLINE", "1"),
            ("HF_HOME", str(work_directory / "hf")), ("TORCH_HOME", str(work_directory / "torch")))
        invocation = PendingInvocation((str(tree.root / "python.exe"), "-I", "-S", "-B",
            str(tree.root / BOOTSTRAP), *arguments), tree.root, environment, timeout_seconds)
        tree.verify()
        return PreparedPythonCommand(tree, invocation)
    except Exception as error:
        if tree is not None:
            tree.close()
        if isinstance(error, ExecutionPreparationError):
            raise
        raise ExecutionPreparationError("execution_python_command_invalid") from None


class PreparedPythonCommandV2:
    """One private command/profile/host; native loader lifecycle still closed."""
    def __init__(self, command, host):
        self._command, self._host, self._state = command, host, "prepared"

    @property
    def inventory(self): return self._command.inventory

    @property
    def invocation(self): return self._command.invocation

    @property
    def host_observation(self): return self._host.observation

    @property
    def system_directory(self): return self._host.windows_root / "System32"

    def verify(self):
        if self._state != "prepared":
            raise ExecutionPreparationError("execution_preparation_consumed")
        try:
            self._command.verify()
            self._host.verify()
            return self.inventory
        except (ValueError, RuntimeError):
            self._state = "failed"
            raise

    def verify_loaded_origins(self, loaded_paths):
        self.verify()
        try:
            self._host.verify_loaded_origins(runtime_root=self.invocation.cwd,
                inventory=self.inventory, loaded_paths=loaded_paths)
        except (ValueError, RuntimeError):
            self._state = "failed"
            raise

    def execute(self):
        self.verify()
        self._state = "failed"
        raise NativeExecutionUnsupported("execution_native_loader_lifecycle_unsupported")

    def close(self):
        self._state = "closed"
        self._command.close()

    def __enter__(self): return self

    def __exit__(self, *_args): self.close()


def prepare_python_command_v2(*, device: str, physical_uuid: str | None = None, **selection):
    from .host_runtime import prepare_host_runtime_v2
    command = prepare_python_command(**selection)
    try:
        host = prepare_host_runtime_v2(runtime_root=command.invocation.cwd,
            inventory=command.inventory, device=device, physical_uuid=physical_uuid)
        if selection["windows_root"] != host.windows_root:
            raise NativeExecutionUnsupported("execution_os_system_directory_unsupported")
        env = dict(command.invocation.environment)
        env.update(host.child_environment)
        command._invocation = PendingInvocation(command.invocation.argv, command.invocation.cwd,
            tuple(env.items()), command.invocation.timeout_seconds)
        result = PreparedPythonCommandV2(command, host)
        result.verify()
        return result
    except Exception:
        command.close()
        raise

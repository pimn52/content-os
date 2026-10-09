"""Shared stdlib-only Windows CPU facts/origins; no provider/model imports."""
from dataclasses import dataclass
from pathlib import Path
import sys
from types import ModuleType

from .runtime_primitives import NativeExecutionUnsupported, _exact_path

MODULE_LIMIT = 2048
PATH_LIMIT = 32768
SYSTEM32 = 0x800
TARGET_FLAGS = SYSTEM32 | 0x100


@dataclass(frozen=True)
class WindowsFacts:
    root: Path
    native_machine: int
    process_machine: int
    build: int
    revision: int
    system_directory: Path | None = None


def _read_windows_facts() -> WindowsFacts:
    import ctypes
    import winreg

    # kernel32 is an explicit trusted OS component, not PATH/user DLL search.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_root = kernel.GetWindowsDirectoryW
    get_root.argtypes, get_root.restype = (ctypes.c_wchar_p, ctypes.c_uint), ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_root(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise ValueError()
    root = Path(buffer.value)
    get_system = kernel.GetSystemDirectoryW
    get_system.argtypes, get_system.restype = (ctypes.c_wchar_p, ctypes.c_uint), ctypes.c_uint
    length = get_system(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise ValueError()
    system_directory = Path(buffer.value)
    get_process = kernel.GetCurrentProcess
    get_process.argtypes, get_process.restype = (), ctypes.c_void_p
    get_machine = kernel.IsWow64Process2
    get_machine.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort), ctypes.POINTER(ctypes.c_ushort))
    get_machine.restype = ctypes.c_int
    process, native = ctypes.c_ushort(), ctypes.c_ushort()
    if not get_machine(get_process(), ctypes.byref(process), ctypes.byref(native)):
        raise ValueError()
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion", 0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        revision, kind = winreg.QueryValueEx(key, "UBR")
        if kind != winreg.REG_DWORD:
            raise ValueError()
    major, minor, build = sys.getwindowsversion().platform_version
    if major != 10 or minor != 0:
        raise ValueError()
    return WindowsFacts(root, native.value, process.value, build, revision, system_directory)


class WindowsModuleReader:
    """Bounded same-process Kernel32 APIs; no Psapi/driver DLL is loaded."""
    def __init__(self, system_directory: Path):
        if sys.platform != "win32":
            raise NativeExecutionUnsupported("execution_loader_platform_unsupported")
        import ctypes
        self._ct = ctypes
        try:
            _exact_path(system_directory / "kernel32.dll", directory=False)
            self._kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=SYSTEM32)
            self._bind()
            if self._path(self._kernel._handle) != system_directory / "kernel32.dll":
                raise ValueError()
        except (OSError, ValueError, AttributeError, RuntimeError):
            raise NativeExecutionUnsupported("execution_loader_api_unavailable") from None

    def _bind(self):
        ct, kernel = self._ct, self._kernel
        signatures = {
            "GetCurrentProcess": ((), ct.c_void_p),
            "K32EnumProcessModules": ((ct.c_void_p, ct.POINTER(ct.c_void_p), ct.c_uint32,
                ct.POINTER(ct.c_uint32)), ct.c_int),
            "GetModuleFileNameW": ((ct.c_void_p, ct.c_wchar_p, ct.c_uint32), ct.c_uint32),
            "SetDefaultDllDirectories": ((ct.c_uint32,), ct.c_int),
            "LoadLibraryExW": ((ct.c_wchar_p, ct.c_void_p, ct.c_uint32), ct.c_void_p),
            "FreeLibrary": ((ct.c_void_p,), ct.c_int),
            "GetCurrentActCtx": ((ct.POINTER(ct.c_void_p),), ct.c_int),
            "ReleaseActCtx": ((ct.c_void_p,), None),
        }
        for name, (args, result) in signatures.items():
            function = getattr(kernel, name)
            function.argtypes, function.restype = args, result

    def _path(self, handle):
        if not handle:
            raise ValueError()
        buffer = self._ct.create_unicode_buffer(PATH_LIMIT)
        count = self._kernel.GetModuleFileNameW(handle, buffer, len(buffer))
        if not 0 < count < len(buffer) or count != len(buffer.value):
            raise ValueError()
        path = Path(buffer.value)
        _exact_path(path, directory=False)
        return path

    def _snapshot(self):
        ct = self._ct
        modules = (ct.c_void_p * MODULE_LIMIT)()
        needed = ct.c_uint32()
        size = ct.sizeof(modules)
        process = self._kernel.GetCurrentProcess()
        if not process or not self._kernel.K32EnumProcessModules(process, modules, size, ct.byref(needed)):
            raise ValueError()
        width = ct.sizeof(ct.c_void_p)
        if not 0 < needed.value <= size or needed.value % width:
            raise ValueError()
        handles = tuple(modules[:needed.value // width])
        if any(not value for value in handles) or len(set(handles)) != len(handles):
            raise ValueError()
        paths = tuple(self._path(value) for value in handles)
        if len(set(paths)) != len(paths):
            raise ValueError()
        # Handles + paths together detect address/name changes between samples.
        return tuple(sorted(zip(handles, paths), key=lambda item: item[0]))

    def native_origins(self):
        try:
            first, second = self._snapshot(), self._snapshot()
            if first != second:
                raise ValueError()
            return tuple(path for _, path in first)
        except (OSError, ValueError, RuntimeError, AttributeError):
            raise NativeExecutionUnsupported("execution_module_observation_unavailable") from None

    def restrict_search(self):
        if not self._kernel.SetDefaultDllDirectories(SYSTEM32):
            raise NativeExecutionUnsupported("execution_dll_search_unsupported")

    def require_no_activation(self):
        context = self._ct.c_void_p()
        if not self._kernel.GetCurrentActCtx(self._ct.byref(context)):
            raise NativeExecutionUnsupported("execution_activation_context_unavailable")
        if context.value:
            self._kernel.ReleaseActCtx(context)
            raise NativeExecutionUnsupported("execution_activation_context_unsupported")

    def load(self, path):
        handle = self._kernel.LoadLibraryExW(str(path), None, TARGET_FLAGS)
        if not handle:
            raise NativeExecutionUnsupported("execution_target_load_failed")
        return handle

    def release(self, handle):
        if not self._kernel.FreeLibrary(handle):
            raise NativeExecutionUnsupported("execution_target_release_failed")


PYTHON_ORIGIN_CHECKPOINTS = frozenset(('registry', 'count', 'module', 'attributes',
    'missing_file', 'file_value', 'exact_path', 'empty'))


class PythonOriginUnsupported(NativeExecutionUnsupported):
    """Diagnostic only: unchanged rejection, no arbitrary path/value disclosure."""
    def __init__(self, checkpoint, name=''):
        super().__init__('execution_python_origin_unsupported')
        if checkpoint not in PYTHON_ORIGIN_CHECKPOINTS:
            raise ValueError('execution_python_diagnostic_invalid')
        parts = name.split('.') if type(name) is str and len(name) <= 255 else ()
        safe = bool(parts) and all(part and part.isascii() and part.isidentifier() for part in parts)
        self.diagnostic = dict(checkpoint=checkpoint, module_name=name if safe else '')


def python_origins(modules):
    """Observe real module objects, not a supplied path claim; namespace unsupported."""
    return _python_origins(modules)


def _python_origins(modules, owned_fileless=None):
    """Private protocol4 hook; default callers retain the original strict gate."""
    checkpoint, name = 'registry', ''
    try:
        items = tuple(modules.items())
        checkpoint = 'count'
        if not 1 <= len(items) <= MODULE_LIMIT:
            raise ValueError()
        paths = set()
        for name, module in items:
            checkpoint = 'module'
            if not isinstance(name, str) or not isinstance(module, ModuleType):
                raise ValueError()
            checkpoint = 'attributes'
            file = getattr(module, "__file__", None)
            origin = getattr(getattr(module, "__spec__", None), "origin", None)
            if file is None:
                checkpoint = 'missing_file'
                if origin not in ("built-in", "frozen"):
                    owner_path = owned_fileless(name, module) if owned_fileless is not None else None
                    if owner_path is None: raise ValueError()
                    _exact_path(owner_path, directory=False)
                    paths.add(owner_path)
                continue
            checkpoint = 'file_value'
            if type(file) is not str or not file or len(file) >= PATH_LIMIT:
                raise ValueError()
            path = Path(file)
            checkpoint = 'exact_path'
            _exact_path(path, directory=False)
            paths.add(path)
        checkpoint, name = 'empty', ''
        if not paths:
            raise ValueError()
        return tuple(sorted(paths))
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        raise PythonOriginUnsupported(checkpoint, name) from None

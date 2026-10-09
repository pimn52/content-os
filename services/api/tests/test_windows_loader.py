"""Synthetic prepared files, ABI fakes and ordered gates; no live native calls."""
import ctypes
import struct
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.providers import host_runtime, windows_loader as loader
from app.providers.prepared import NativeExecutionUnsupported
from app.providers.python_startup import prepare_python_command_v2
from app.providers.windows_os_policy import owned_os_policy_bytes, parse_os_policy
from test_python_startup import layout
from test_pe_imports import image


@pytest.fixture
def prepared(layout, monkeypatch):
    system = layout["windows_root"] / "System32"
    for name in parse_os_policy(owned_os_policy_bytes()).component_names:
        (system / name).write_bytes(b"synthetic OS")
    monkeypatch.setattr(host_runtime.sys, "platform", "win32")
    monkeypatch.setattr(host_runtime, "_read_windows_facts", lambda:
        host_runtime.WindowsFacts(system.parent, 0x8664, 0, 26100, 1, system))
    source = layout["trees"][0].source / "site-packages/app/providers"
    (source / "windows_os_policy.json").write_bytes(owned_os_policy_bytes())
    (source / "target.dll").write_bytes(image())
    with prepare_python_command_v2(device="cpu", **layout) as command:
        yield command, system, source
    assert not list(layout["staging_parent"].iterdir())


def modules(command):
    module = ModuleType("__main__")
    module.__file__ = str(command.invocation.cwd / "content_os_bootstrap.py")
    return {"__main__": module}


class Reader:
    def __init__(self, command, system):
        self.paths = (command.invocation.cwd / "python.exe", system / "kernel32.dll")
        self.calls, self.problem = [], None

    def native_origins(self):
        self.calls.append("origins")
        return self.paths

    def restrict_search(self):
        self.calls.append("restrict")
        if self.problem == "restrict": raise NativeExecutionUnsupported("execution_dll_search_unsupported")

    def require_no_activation(self):
        self.calls.append("activation")
        if self.problem == "activation": raise NativeExecutionUnsupported("execution_activation_context_unsupported")

    def load(self, path):
        self.calls.append("load")
        if self.problem == "load": raise NativeExecutionUnsupported("execution_target_load_failed")
        if self.problem != "missing": self.paths += (path,)
        if self.problem == "post": self.paths += (path.parent / "unknown.dll",)
        return 99

    def release(self, handle): self.calls.append(("release", handle))


def test_ordered_baseline_search_preload_postload_and_release(prepared):
    command, system, _ = prepared
    reader = Reader(command, system)
    target = command.invocation.cwd / "Lib/site-packages/app/providers/target.dll"
    with loader.ControlledLoader(command, reader, modules(command)) as session:
        assert session.load(target) == 99
        with pytest.raises(NativeExecutionUnsupported, match="consumed"): session.load(target)
    assert reader.calls == ["origins", "activation", "restrict", "origins", "origins", "activation", "load", "origins", ("release", 99)]


@pytest.mark.parametrize("problem", ["baseline", "activation", "restrict", "python", "load", "post", "missing", "changed", "selection"])
def test_failure_consumes_and_cannot_expose_handle(prepared, problem):
    command, system, _ = prepared
    reader, observed = Reader(command, system), modules(command)
    target = command.invocation.cwd / "Lib/site-packages/app/providers/target.dll"
    if problem == "baseline": reader.paths += (system / "unknown.dll",)
    if problem == "python": observed["foreign"] = ModuleType("foreign")
    reader.problem = problem
    session = loader.ControlledLoader(command, reader, observed)
    with pytest.raises(RuntimeError):
        session.begin()
        if problem == "changed": target.write_bytes(b"changed")
        session.load(target if problem != "selection" else Path("relative.dll"))
    with pytest.raises(NativeExecutionUnsupported, match="consumed"): session.begin()
    if problem in ("baseline", "activation", "restrict", "python", "changed", "selection"):
        assert "load" not in reader.calls
    if problem in ("post", "missing"): assert ("release", 99) in reader.calls


@pytest.mark.parametrize("problem", ["empty", "duplicate", "list", "overflow"])
def test_reader_claim_bounds_stop_before_search(prepared, problem):
    command, system, _ = prepared
    reader = Reader(command, system)
    reader.paths = {"empty": (), "duplicate": reader.paths * 2, "list": list(reader.paths),
        "overflow": tuple(Path(f"unknown{i}.dll") for i in range(2049))}[problem]
    with pytest.raises(NativeExecutionUnsupported, match="observation_unavailable"):
        loader.ControlledLoader(command, reader, modules(command)).begin()
    assert reader.calls == ["origins"]


@pytest.mark.parametrize("name", ["python.exe.local", "python.exe.manifest", "target.dll.manifest", "kernel32.dll", "version.dll"])
def test_inventoried_redirection_and_same_basename_still_reject(layout, monkeypatch, name):
    # Add before preparation, so this is not merely an extra-tree-file test.
    system = layout["windows_root"] / "System32"
    for member in parse_os_policy(owned_os_policy_bytes()).component_names:
        (system / member).write_bytes(b"OS")
    monkeypatch.setattr(host_runtime.sys, "platform", "win32")
    monkeypatch.setattr(host_runtime, "_read_windows_facts", lambda:
        host_runtime.WindowsFacts(system.parent, 0x8664, 0, 26100, 1, system))
    source = layout["trees"][0].source / "site-packages/app/providers"
    (source / "windows_os_policy.json").write_bytes(owned_os_policy_bytes())
    (source / "target.dll").write_bytes(image())
    added = source / name
    added.parent.mkdir(parents=True, exist_ok=True)
    added.write_bytes(b"inventoried but unsupported")
    with prepare_python_command_v2(device="cpu", **layout) as command:
        reader = Reader(command, system)
        session = loader.ControlledLoader(command, reader, modules(command))
        session.begin()
        with pytest.raises(NativeExecutionUnsupported, match="redirection_unsupported|basename_collision"):
            session.load(command.invocation.cwd / "Lib/site-packages/app/providers/target.dll")
        assert "load" not in reader.calls


def test_unknown_static_dependency_rejects_before_target(layout, monkeypatch):
    system = layout["windows_root"] / "System32"
    for member in parse_os_policy(owned_os_policy_bytes()).component_names:
        (system / member).write_bytes(b"OS")
    monkeypatch.setattr(host_runtime.sys, "platform", "win32")
    monkeypatch.setattr(host_runtime, "_read_windows_facts", lambda:
        host_runtime.WindowsFacts(system.parent, 0x8664, 0, 26100, 1, system))
    source = layout["trees"][0].source / "site-packages/app/providers"
    (source / "windows_os_policy.json").write_bytes(owned_os_policy_bytes())
    (source / "target.dll").write_bytes(image(delayed=("unknown.dll",)))
    with prepare_python_command_v2(device="cpu", **layout) as command:
        reader = Reader(command, system)
        session = loader.ControlledLoader(command, reader, modules(command))
        session.begin()
        with pytest.raises(NativeExecutionUnsupported, match="import_closure_unsupported"):
            session.load(command.invocation.cwd / "Lib/site-packages/app/providers/target.dll")
        assert "load" not in reader.calls


@pytest.mark.parametrize("problem", ["none", "object", "namespace", "relative", "overflow"])
def test_python_reader_refuses_unknown_or_ambiguous_origins(prepared, problem):
    command, _, _ = prepared
    values = modules(command)
    if problem == "none": values["unknown"] = None
    elif problem == "object": values["unknown"] = object()
    elif problem == "namespace": values["unknown"] = ModuleType("namespace")
    elif problem == "relative": values["__main__"].__file__ = "relative.py"
    else: values.update({f"m{i}": values["__main__"] for i in range(2048)})
    with pytest.raises(NativeExecutionUnsupported, match="python_origin_unsupported"):
        loader.python_origins(values)


def test_builtin_frozen_and_real_files_are_observed(prepared):
    command, _, _ = prepared
    values = modules(command)
    for name in ("built-in", "frozen"):
        module = ModuleType(name)
        module.__spec__ = SimpleNamespace(origin=name)
        values[name] = module
    assert loader.python_origins(values) == (command.invocation.cwd / "content_os_bootstrap.py",)


class Function:
    def __init__(self, body): self.body = body
    def __call__(self, *args): return self.body(*args)


def abi_reader(tmp_path, problem=None):
    """Use actual ctypes buffers/widths but fake kernel functions only."""
    path = tmp_path / "observed.dll"
    path.write_bytes(b"synthetic")
    calls = []
    def enumerate_(process, buffer, size, needed):
        calls.append("enum")
        count = {"overflow": size + ctypes.sizeof(ctypes.c_void_p), "empty": 0, "alignment": 1}.get(problem,
            ctypes.sizeof(ctypes.c_void_p))
        ctypes.cast(needed, ctypes.POINTER(ctypes.c_uint32))[0] = count
        buffer[0] = 11 if problem != "null" else None
        if problem == "changed" and len(calls) > 1: buffer[0] = 12
        return 0 if problem == "api" else 1
    def filename(handle, buffer, size):
        buffer.value = str(path)
        return {"truncated": size, "path_error": 0}.get(problem, len(str(path)))
    reader = object.__new__(loader.WindowsModuleReader)
    reader._ct = ctypes
    reader._kernel = SimpleNamespace(GetCurrentProcess=Function(lambda: 1),
        K32EnumProcessModules=Function(enumerate_), GetModuleFileNameW=Function(filename),
        SetDefaultDllDirectories=Function(lambda flags: calls.append(("flags", flags)) or 1),
        LoadLibraryExW=Function(lambda path, reserved, flags: calls.append(("load_flags", flags, reserved)) or 88),
        FreeLibrary=Function(lambda handle: calls.append(("free", handle)) or 1))
    reader._kernel.GetCurrentActCtx = Function(lambda pointer: 1)
    reader._kernel.ReleaseActCtx = Function(lambda handle: calls.append("release_context"))
    reader._bind()
    return reader, path, calls


def test_kernel_reader_abi_exact_flags_and_bounded_double_snapshot(tmp_path):
    reader, path, calls = abi_reader(tmp_path)
    assert reader.native_origins() == (path,)
    reader.restrict_search()
    reader.require_no_activation()
    assert reader.load(path) == 88
    reader.release(88)
    assert calls == ["enum", "enum", ("flags", 0x800), ("load_flags", 0x900, None), ("free", 88)]
    assert reader._kernel.K32EnumProcessModules.restype is ctypes.c_int
    assert reader._kernel.LoadLibraryExW.restype is ctypes.c_void_p


@pytest.mark.parametrize("problem", ["overflow", "empty", "alignment", "null", "changed", "api", "truncated", "path_error"])
def test_native_reader_rejects_incomplete_or_unstable_observations(tmp_path, problem):
    reader, _, calls = abi_reader(tmp_path, problem)
    with pytest.raises(NativeExecutionUnsupported, match="observation_unavailable"):
        reader.native_origins()
    assert len(calls) <= 2


def test_actual_factory_refuses_parent_before_creating_reader(prepared, monkeypatch):
    command, _, _ = prepared
    from app.providers import python_bootstrap
    monkeypatch.setattr(python_bootstrap, "verify_startup", lambda system: "C:\\not-the-prepared-child")
    monkeypatch.setattr(loader, "WindowsModuleReader", lambda *args: pytest.fail("reader must not start"))
    with pytest.raises(NativeExecutionUnsupported, match="child_mismatch"):
        loader.begin_current_child(command)


@pytest.mark.parametrize("problem", ["active", "failed"])
def test_active_context_or_unknown_context_is_not_a_search_permission(tmp_path, problem):
    reader, _, calls = abi_reader(tmp_path)
    def active(pointer):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = 123
        return problem != "failed"
    reader._kernel.GetCurrentActCtx = Function(active)
    with pytest.raises(NativeExecutionUnsupported, match="activation_context"):
        reader.require_no_activation()
    assert calls == (["release_context"] if problem == "active" else [])


@pytest.mark.parametrize("where", ["target", "sibling"])
def test_embedded_resource_cannot_be_loaded_even_with_known_imports(layout, monkeypatch, where):
    system = layout["windows_root"] / "System32"
    for member in parse_os_policy(owned_os_policy_bytes()).component_names:
        (system / member).write_bytes(b"OS")
    monkeypatch.setattr(host_runtime.sys, "platform", "win32")
    monkeypatch.setattr(host_runtime, "_read_windows_facts", lambda:
        host_runtime.WindowsFacts(system.parent, 0x8664, 0, 26100, 1, system))
    source = layout["trees"][0].source / "site-packages/app/providers"
    (source / "windows_os_policy.json").write_bytes(owned_os_policy_bytes())
    payload = image()
    struct.pack_into("<II", payload, 152 + 112 + 2 * 8, 4096, 16)
    (source / "target.dll").write_bytes(payload if where == "target" else image())
    if where == "sibling": (source / "sibling.dll").write_bytes(payload)
    with prepare_python_command_v2(device="cpu", **layout) as command:
        reader = Reader(command, system)
        session = loader.ControlledLoader(command, reader, modules(command))
        session.begin()
        with pytest.raises(NativeExecutionUnsupported, match="resources_unsupported"):
            session.load(command.invocation.cwd / "Lib/site-packages/app/providers/target.dll")
        assert "load" not in reader.calls


def test_current_child_factory_uses_owned_reader_and_actual_module_mapping(prepared, monkeypatch):
    command, system, _ = prepared
    from app.providers import python_bootstrap
    observed = modules(command)
    monkeypatch.setattr(loader, "sys", SimpleNamespace(executable=command.invocation.argv[0], modules=observed))
    monkeypatch.setattr(python_bootstrap, "verify_startup", lambda value: str(command.invocation.cwd))
    readers = []
    def factory(location):
        assert location == system
        reader = Reader(command, system)
        readers.append(reader)
        return reader
    monkeypatch.setattr(loader, "WindowsModuleReader", factory)
    session = loader.begin_current_child(command)
    assert readers[0].calls == ["origins", "activation", "restrict", "origins"]
    session.close()


@pytest.mark.parametrize("wrong_origin", [False, True])
def test_kernel_constructor_is_system32_only_and_checks_actual_kernel_path(tmp_path, monkeypatch, wrong_origin):
    reader, path, calls = abi_reader(tmp_path)
    system = tmp_path / "System32"
    system.mkdir()
    kernel_path = system / "kernel32.dll"
    kernel_path.write_bytes(b"synthetic kernel")
    def filename(handle, buffer, size):
        buffer.value = str(path if wrong_origin else kernel_path)
        return len(buffer.value)
    reader._kernel.GetModuleFileNameW = Function(filename)
    reader._kernel._handle = 123
    monkeypatch.setattr(loader.sys, "platform", "win32")
    def win_dll(name, **kwargs):
        assert name == "kernel32.dll" and kwargs == {"use_last_error": True, "winmode": 0x800}
        return reader._kernel
    monkeypatch.setattr(ctypes, "WinDLL", win_dll)
    if wrong_origin:
        with pytest.raises(NativeExecutionUnsupported, match="loader_api_unavailable"):
            loader.WindowsModuleReader(system)
    else:
        assert loader.WindowsModuleReader(system)._kernel is reader._kernel
    assert calls == []

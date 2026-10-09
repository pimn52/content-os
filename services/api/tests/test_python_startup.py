"""Synthetic Windows layout/fake sys+subprocess only; no real Python child."""
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from app.providers import python_bootstrap as bootstrap
from app.providers.prepared import ExecutionPreparationError
from app.providers.python_startup import prepare_python_command
from app.providers.runtime_tree import RuntimeFileSelection, RuntimeTreeSelection


def child():
    root = "C:\\private\\runtime"
    return SimpleNamespace(platform="win32", version_info=(3, 12, 14),
        flags=SimpleNamespace(isolated=1, ignore_environment=1, no_site=1, no_user_site=1, dont_write_bytecode=1),
        executable=root + "\\python.exe", _base_executable=root + "\\python.exe",
        prefix=root, base_prefix=root, exec_prefix=root, base_exec_prefix=root,
        path=[root + "\\" + name for name in bootstrap.IMPORT_ROOTS], modules={},
        argv=[root + "\\content_os_bootstrap.py", "--inputs", "synthetic"])


@pytest.mark.parametrize("problem", ["platform", "version", "isolated", "ignore_environment", "no_site",
    "no_user_site", "dont_write_bytecode", "executable", "base", "exec_base", "redirector", "path",
    "path_order", "relative", "site", "sitecustomize", "usercustomize", "entry"])
def test_startup_refuses_unfixed_state_before_any_dispatch(problem, monkeypatch):
    system = child()
    if problem == "platform": system.platform = "linux"
    elif problem == "version": system.version_info = (3, 13, 0)
    elif problem in ("isolated", "ignore_environment", "no_site", "no_user_site", "dont_write_bytecode"):
        setattr(system.flags, problem, 0)
    elif problem == "executable": system.executable = "C:\\ambient\\venv-python.exe"
    elif problem == "base": system.base_prefix = "C:\\original-python"
    elif problem == "exec_base": system.base_exec_prefix = "C:\\original-python"
    elif problem == "redirector": system._base_executable = "C:\\original-python\\python.exe"
    elif problem == "path": system.path.append("C:\\ambient")
    elif problem == "path_order": system.path.reverse()
    elif problem == "relative": system.path[0] = "Lib"
    elif problem in ("site", "sitecustomize", "usercustomize"): system.modules[problem] = object()
    else: system.argv[0] = "C:\\ambient\\entry.py"
    monkeypatch.setattr(bootstrap, "sys", system)
    import runpy
    dispatched = []
    monkeypatch.setattr(runpy, "run_module", lambda *args, **kwargs: dispatched.append(args))
    with pytest.raises(SystemExit, match="execution_python_startup_invalid"):
        bootstrap.main()
    assert dispatched == []


def test_validated_child_dispatches_only_owned_entry_and_preserves_application_args(monkeypatch):
    system = child()
    assert bootstrap.verify_startup(system) == "C:\\private\\runtime"
    monkeypatch.setattr(bootstrap, "sys", system)
    import runpy
    calls = []
    monkeypatch.setattr(runpy, "run_module", lambda *args, **kwargs: calls.append((args, kwargs, system.argv.copy())))
    bootstrap.main()
    assert calls == [(("app.providers.omnivoice_entry",), {"run_name": "__main__", "alter_sys": True},
                     ["app.providers.omnivoice_entry", "--inputs", "synthetic"])]


@pytest.fixture
def layout(tmp_path):
    base, staging, system, work = [tmp_path / name for name in ("base", "staging", "windows", "work")]
    for path in (base, staging, system / "System32", work): path.mkdir(parents=True)
    lib, dlls = base / "Lib", base / "DLLs"
    for path in (lib / "encodings", lib / "site-packages/app/providers", dlls): path.mkdir(parents=True)
    for name in ("os.py", "encodings/__init__.py", "site-packages/app/providers/omnivoice_entry.py"):
        (lib / name).write_bytes(b"synthetic source")
    for name in ("python.exe", "python312.dll", "python3.dll"):
        (base / name).write_bytes(b"synthetic binary")
    return dict(trees=(RuntimeTreeSelection(lib, "Lib"), RuntimeTreeSelection(dlls, "DLLs")),
        files=tuple(RuntimeFileSelection(base / name, name, "interpreter" if name == "python.exe" else "dependency")
                    for name in ("python.exe", "python312.dll", "python3.dll")),
        staging_parent=staging, max_bytes=20_000, windows_root=system,
        work_directory=work, arguments=("--inputs", "synthetic"), timeout_seconds=10)


def test_fixed_startup_files_inventory_command_and_environment_not_ambient(layout, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "C:\\ambient-import")
    monkeypatch.setenv("PATH", "C:\\ambient-dlls")
    monkeypatch.setenv("CUDA_HOME", "C:\\ambient-cuda")
    with prepare_python_command(**layout) as command:
        invocation = command.invocation
        root = invocation.cwd
        assert invocation.argv == (str(root / "python.exe"), "-I", "-S", "-B",
            str(root / "content_os_bootstrap.py"), "--inputs", "synthetic")
        assert {"python._pth", "python312._pth", "content_os_bootstrap.py"}.issubset(
            {item.name for item in command.inventory.files})
        for name in ("python._pth", "python312._pth"):
            assert (root / name).read_bytes() == b"Lib\nDLLs\nLib/site-packages\n"
        env = dict(invocation.environment)
        assert not {"PATH", "PYTHONPATH", "PYTHONHOME", "CUDA_HOME", "PYTHONUSERBASE"} & set(env)
        assert env["SystemRoot"] == str(layout["windows_root"])
        assert env["HF_HUB_OFFLINE"] == env["TRANSFORMERS_OFFLINE"] == "1"
        calls = []
        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0)
        monkeypatch.setattr(subprocess, "run", fake_run)
        assert command.execute().returncode == 0
        assert calls[0][0] == list(invocation.argv)
        assert calls[0][1]["env"] == env and calls[0][1]["shell"] is False
        with pytest.raises(ExecutionPreparationError, match="consumed"):
            command.execute()
    assert list(layout["staging_parent"].iterdir()) == []


@pytest.mark.parametrize("change", ["bootstrap", "pth", "new_pth", "module"])
def test_fixed_startup_changes_block_before_child_launch(layout, monkeypatch, change):
    with prepare_python_command(**layout) as command:
        root = command.invocation.cwd
        name = {"bootstrap": "content_os_bootstrap.py", "pth": "python312._pth",
            "new_pth": "Lib/new.pth", "module": "Lib/site-packages/app/providers/omnivoice_entry.py"}[change]
        (root / name).write_bytes(b"changed")
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))
        with pytest.raises(ExecutionPreparationError, match="fixed_runtime_changed"):
            command.execute()
        assert calls == []
        with pytest.raises(ExecutionPreparationError, match="consumed"):
            command.execute()


@pytest.mark.parametrize("problem", ["missing_dll", "missing_stdlib", "pyvenv", "foreign_pth", "collision",
    "nul", "argument_budget", "timeout"])
def test_unsupported_layout_and_invalid_command_cleanup(layout, problem):
    lib = layout["trees"][0].source
    if problem == "missing_dll": (layout["files"][1].source).unlink()
    elif problem == "missing_stdlib": (lib / "os.py").unlink()
    elif problem == "pyvenv": (lib / "pyvenv.cfg").write_bytes(b"home=outside")
    elif problem == "foreign_pth": (lib / "python311._pth").write_bytes(b"outside")
    elif problem == "collision":
        source = lib / "bootstrap-collision"
        source.write_bytes(b"foreign bootstrap")
        layout["files"] += (RuntimeFileSelection(source, "content_os_bootstrap.py", "entrypoint"),)
    elif problem == "nul": layout["arguments"] = ("bad\0arg",)
    elif problem == "argument_budget": layout["arguments"] = ("x" * 24_001,)
    else: layout["timeout_seconds"] = True
    with pytest.raises(ExecutionPreparationError):
        prepare_python_command(**layout)
    assert list(layout["staging_parent"].iterdir()) == []


@pytest.mark.parametrize("failure", ["launch", "timeout"])
def test_launch_failure_consumes_without_retry_or_private_error_leak(layout, monkeypatch, failure):
    with prepare_python_command(**layout) as command:
        def fail(*args, **kwargs):
            if failure == "launch": raise OSError("private path")
            raise subprocess.TimeoutExpired("private command", 10)
        monkeypatch.setattr(subprocess, "run", fail)
        with pytest.raises(ExecutionPreparationError, match="execution_prepared_" + ("launch_failed" if failure == "launch" else "timeout")):
            command.execute()
        with pytest.raises(ExecutionPreparationError, match="consumed"):
            command.execute()

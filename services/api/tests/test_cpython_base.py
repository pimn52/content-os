"""Synthetic base/cache policy; never launches a real interpreter."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.providers.cpython_base import select_cpython312_base, select_cpython312_runtime
from app.providers.prepared import ExecutionPreparationError
from app.providers.runtime_tree import OwnedRuntimeFile, RuntimeTreeSelection, prepare_runtime_tree
from app.providers import python_bootstrap
from app.providers.python_startup_probe import verify_module_origins
from test_python_startup import child


@pytest.fixture
def base(tmp_path):
    base = tmp_path / "base"
    (base / "Lib/__pycache__").mkdir(parents=True)
    (base / "Lib/site-packages").mkdir()
    (base / "DLLs").mkdir()
    (base / "Lib/os.py").write_bytes(b"source")
    (base / "Lib/__pycache__/os.cpython-312.pyc").write_bytes(b"not copied")
    (base / "Lib/site-packages/unselected.py").write_bytes(b"third party excluded")
    for name in ("python.exe", "python312.dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        (base / name).write_bytes(b"binary")
    return base


def prepare(base, tmp_path, **kwargs):
    trees, files = select_cpython312_base(base)
    return prepare_runtime_tree(trees=trees, files=files, staging_parent=tmp_path,
        owned_files=(OwnedRuntimeFile("entry.py", b"entry", "entrypoint"),), max_bytes=1000, **kwargs)


def test_explicit_base_policy_preserves_source_and_native_files_excludes_source_backed_cache(base, tmp_path):
    with prepare(base, tmp_path) as result:
        names = {item.name for item in result.inventory.files}
        assert "Lib/os.py" in names and "vcruntime140.dll" in names
        assert not any("__pycache__" in name or "site-packages" in name for name in names)
        assert result.verify().descriptor == result.inventory.descriptor
        (result.root / "Lib/new.pyc").write_bytes(b"unlisted bytecode")
        with pytest.raises(ExecutionPreparationError, match="fixed_runtime_changed"):
            result.verify()
    assert (base / "Lib/__pycache__/os.cpython-312.pyc").read_bytes() == b"not copied"


@pytest.mark.parametrize("problem", ["missing_source", "wrong_tag", "nested_cache", "extra_data", "loose_pyc",
    "missing_vc", "count", "unknown_policy"])
def test_no_sourceless_or_ambiguous_cache_or_unfixed_vc_fallback(base, tmp_path, problem):
    if problem == "missing_source": (base / "Lib/os.py").unlink()
    elif problem == "wrong_tag": (base / "Lib/__pycache__/other.cpython-311.pyc").write_bytes(b"x")
    elif problem == "nested_cache": (base / "Lib/__pycache__/nested").mkdir()
    elif problem == "extra_data": (base / "Lib/__pycache__/config.json").write_bytes(b"x")
    elif problem == "loose_pyc": (base / "Lib/only.pyc").write_bytes(b"x")
    elif problem == "missing_vc": (base / "vcruntime140.dll").unlink()
    elif problem == "count":
        with pytest.raises(ExecutionPreparationError, match="member_limit"):
            prepare(base, tmp_path, max_files=3)
        return
    else:
        trees, files = select_cpython312_base(base)
        with pytest.raises(ExecutionPreparationError, match="source_policy_invalid"):
            prepare_runtime_tree(trees=(RuntimeTreeSelection(trees[0].source, "Lib", "guess-minimal"),),
                files=files, staging_parent=tmp_path, max_bytes=1000)
        return
    with pytest.raises(ExecutionPreparationError):
        prepare(base, tmp_path)
    assert not list(tmp_path.glob("content-os-runtime-*"))


def test_probe_guard_observes_actual_argv_without_rewriting_or_enabling_dispatch():
    system = child()
    system.argv[0] = "C:\\private\\runtime\\_content_os_startup_probe.py"
    original = list(system.argv)
    assert python_bootstrap.verify_startup(system, entry_name="_content_os_startup_probe.py")
    assert system.argv == original
    with pytest.raises(SystemExit, match="startup_invalid"):
        python_bootstrap.verify_startup(system)
    with pytest.raises(SystemExit, match="startup_invalid"):
        python_bootstrap.verify_startup(system, entry_name="arbitrary.py")


def test_owned_main_witness_is_not_an_import_root_exemption():
    root = "C:\\private\\runtime"
    modules = {"__main__": SimpleNamespace(__file__=root + "\\_content_os_startup_probe.py"),
               "json": SimpleNamespace(__file__=root + "\\Lib\\json\\__init__.py")}
    assert verify_module_origins(modules, root, (root + "\\Lib",)) == [
        "Lib\\json\\__init__.py", "_content_os_startup_probe.py"]
    for name, path in (("shadow", root + "\\shadow.py"), ("__main__", root + "\\other.py"),
                       ("json", "C:\\original\\Lib\\json.py"), ("shadow", root + "\\Lib\\..\\shadow.py")):
        with pytest.raises(SystemExit, match="probe_origin_invalid"):
            verify_module_origins({name: SimpleNamespace(__file__=path)}, root, (root + "\\Lib",))


def test_base_plus_explicit_complete_packages_preserves_metadata_data_native_and_source_cache(base, tmp_path):
    packages = tmp_path / "packages"
    (packages / "pkg/__pycache__").mkdir(parents=True)
    (packages / "pkg.dist-info").mkdir()
    for name, payload in (("pkg/module.py", b"source"), ("pkg/data.bin", b"data"),
        ("pkg/native.pyd", b"native"), ("pkg.dist-info/METADATA", b"metadata"),
        ("pkg/__pycache__/module.cpython-312.pyc", b"omitted cache")):
        (packages / name).write_bytes(payload)
    trees, files = select_cpython312_runtime(base=base, packages_root=packages)
    with prepare_runtime_tree(trees=trees, files=files, staging_parent=tmp_path, max_bytes=1000,
        owned_files=(OwnedRuntimeFile("entry.py", b"entry", "entrypoint"),)) as fixed:
        names = {item.name for item in fixed.inventory.files}
        assert {"Lib/site-packages/pkg/data.bin", "Lib/site-packages/pkg/native.pyd",
                "Lib/site-packages/pkg.dist-info/METADATA"}.issubset(names)
        assert not any("__pycache__" in name or "unselected.py" in name for name in names)
        assert fixed.verify().descriptor == fixed.inventory.descriptor
        (fixed.root / "Lib/site-packages/new.pth").write_bytes(b"new")
        with pytest.raises(ExecutionPreparationError, match="fixed_runtime_changed"):
            fixed.verify()


def test_arbitrary_nested_package_overlay_is_not_allowed(base, tmp_path):
    packages = tmp_path / "packages"
    packages.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    trees, files = select_cpython312_runtime(base=base, packages_root=packages)
    with pytest.raises(ExecutionPreparationError, match="selection_overlap"):
        prepare_runtime_tree(trees=(*trees, RuntimeTreeSelection(extra, "Lib/site-packages/override")),
            files=files, staging_parent=tmp_path, max_bytes=1000)

"""M2 private tree primitive: synthetic files only, no interpreter/model launch."""
import os
from pathlib import Path

import pytest

from app.providers.prepared import ExecutionPreparationError
from app.providers.runtime_tree import (RuntimeFileSelection, RuntimeTreeSelection,
    prepare_runtime_tree)


@pytest.fixture
def layout(tmp_path):
    source, staging = tmp_path / "source", tmp_path / "staging"
    source.mkdir()
    staging.mkdir()
    package = source / "pkg"
    package.mkdir()
    (package / "empty").mkdir()
    (package / "__init__.py").write_bytes(b"")
    (package / "module.py").write_bytes(b"original")
    (package / "data.bin").write_bytes(b"data")
    (source / "python.exe").write_bytes(b"synthetic interpreter")
    (source / "bootstrap.py").write_bytes(b"synthetic bootstrap")
    return dict(trees=(RuntimeTreeSelection(package, "Lib/pkg"),), files=(
        RuntimeFileSelection(source / "python.exe", "python.exe", "interpreter"),
        RuntimeFileSelection(source / "bootstrap.py", "bootstrap.py", "entrypoint")),
        staging_parent=staging, max_bytes=1000)


def test_complete_copy_preserves_data_empty_files_dirs_and_source_permissions(layout):
    source = layout["trees"][0].source
    original_modes = {path: path.stat().st_mode for path in source.rglob("*")}
    with prepare_runtime_tree(**layout) as prepared:
        assert prepared.verify().descriptor == prepared.inventory.descriptor
        assert "Lib/pkg/empty" in prepared.inventory.directories
        assert (prepared.root / "Lib/pkg/__init__.py").read_bytes() == b""
        assert (prepared.root / "Lib/pkg/data.bin").read_bytes() == b"data"
        assert prepared.inventory.descriptor.file_count == 5
        root = prepared.root
        assert prepared.claim_for_launch() == root
        with pytest.raises(ExecutionPreparationError, match="consumed"):
            prepared.claim_for_launch()
    assert not root.exists()
    assert all(path.stat().st_mode == mode for path, mode in original_modes.items())
    assert not list(layout["staging_parent"].iterdir())


@pytest.mark.parametrize("change", ["new_module", "pth", "pyc", "empty_dir", "remove", "bytes", "case"])
def test_complete_membership_and_bytes_rechecked_even_same_size_mtime(layout, change):
    with prepare_runtime_tree(**layout) as prepared:
        package = prepared.root / "Lib/pkg"
        if change in ("new_module", "pth", "pyc"):
            name = {"new_module": "shadow.py", "pth": "startup.pth", "pyc": "shadow.pyc"}[change]
            (package / name).write_bytes(b"new")
        elif change == "empty_dir": (package / "new_empty").mkdir()
        elif change == "remove": (package / "data.bin").unlink()
        elif change == "case": (package / "module.py").rename(package / "Module.py")
        else:
            path = package / "module.py"
            previous = path.stat()
            path.write_bytes(b"modified")
            os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        with pytest.raises(ExecutionPreparationError, match="fixed_runtime_changed"):
            prepared.verify()
        with pytest.raises(ExecutionPreparationError, match="consumed"):
            prepared.claim_for_launch()


def test_same_contents_different_source_locations_have_same_identity(layout, tmp_path):
    with prepare_runtime_tree(**layout) as first:
        digest = first.inventory.descriptor
    alternate = tmp_path / "other"
    alternate.mkdir()
    import shutil
    original = layout["trees"][0].source.parent
    shutil.copytree(original, alternate / "source")
    source = alternate / "source"
    different = {**layout, "trees": (RuntimeTreeSelection(source / "pkg", "Lib/pkg"),),
        "files": tuple(RuntimeFileSelection(source / item.source.name, item.destination, item.role)
                       for item in layout["files"])}
    with prepare_runtime_tree(**different) as second:
        assert second.inventory.descriptor == digest


@pytest.mark.parametrize("problem", ["traversal", "overlap", "alias_name", "bytecode", "size", "missing_role"])
def test_invalid_layout_never_leaves_private_directory(layout, problem):
    if problem == "traversal":
        layout["trees"] = (RuntimeTreeSelection(layout["trees"][0].source, "../escape"),)
    elif problem == "overlap":
        layout["files"] += (RuntimeFileSelection(layout["files"][0].source, "Lib/pkg/shadow", "dependency"),)
    elif problem == "alias_name":
        (layout["trees"][0].source / "CON.txt").write_bytes(b"x")
    elif problem == "bytecode":
        (layout["trees"][0].source / "shadow.pyc").write_bytes(b"x")
    elif problem == "size": layout["max_bytes"] = 1
    else:
        item = layout["files"][0]
        layout["files"] = (RuntimeFileSelection(item.source, item.destination, "dependency"), layout["files"][1])
    with pytest.raises(ExecutionPreparationError):
        prepare_runtime_tree(**layout)
    assert list(layout["staging_parent"].iterdir()) == []


def test_source_mutated_during_copy_stops_and_cleans_only_private_tree(layout, monkeypatch):
    original = Path.open
    package = layout["trees"][0].source
    fired = False
    def mutate(path, mode="r", *args, **kwargs):
        nonlocal fired
        if mode == "xb" and not fired:
            fired = True
            (package / "new.py").write_bytes(b"added while copying")
        return original(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", mutate)
    with pytest.raises(ExecutionPreparationError, match="source_changed"):
        prepare_runtime_tree(**layout)
    assert fired and (package / "new.py").exists()
    assert list(layout["staging_parent"].iterdir()) == []


def test_symlink_source_rejected_without_following(layout, tmp_path):
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"private external")
    try:
        (layout["trees"][0].source / "link").symlink_to(outside)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable")
    with pytest.raises(ExecutionPreparationError, match="path_invalid"):
        prepare_runtime_tree(**layout)
    assert outside.read_bytes() == b"private external"
    assert list(layout["staging_parent"].iterdir()) == []

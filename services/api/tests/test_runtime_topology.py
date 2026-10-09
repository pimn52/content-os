"""Owned search topology is separate from complete byte inventory."""
from pathlib import Path

import pytest

from app.providers.runtime_primitives import (
    NativeExecutionUnsupported, require_search_topology, require_unique_native_origins,
)


def test_inactive_members_are_retained_not_recursive_search_candidates():
    require_search_topology(("python.exe", "Lib/venv/scripts/nt/python.exe",
        "Lib/data/unused.dll.manifest", "Lib/data/kernel32.dll"),
        ("Lib/data/unused.local",), {"kernel32.dll"})


@pytest.mark.parametrize("directory", ["", "Lib/", "DLLs/", "Lib/site-packages/"])
@pytest.mark.parametrize("member", ["kernel32.dll", "python.exe", "target.dll.manifest", "target.dll.local"])
def test_fixed_direct_roots_cannot_be_shrunk(directory, member):
    files = ("python.exe", directory + member)
    # Duplicate root inventory names are separately invalid inventories.
    if files[0] == files[1]:
        files = ("python.exe", "DLLs/python.exe")
    with pytest.raises(NativeExecutionUnsupported):
        require_search_topology(files, (), {"kernel32.dll"})


@pytest.mark.parametrize("member", ["kernel32.dll", "python.exe", "target.dll.manifest"])
def test_explicit_target_parent_is_added_not_substituted(member):
    files = ("python.exe", "Lib/vendor/" + member)
    require_search_topology(files, (), {"kernel32.dll"})
    with pytest.raises(NativeExecutionUnsupported):
        require_search_topology(files, (), {"kernel32.dll"}, target_parent=Path("Lib/vendor"))


def test_direct_redirection_directory_stops():
    with pytest.raises(NativeExecutionUnsupported, match="redirection"):
        require_search_topology(("python.exe",), ("python.exe.local",), set())


def test_loaded_inactive_same_name_is_not_admitted():
    with pytest.raises(NativeExecutionUnsupported, match="basename_collision"):
        require_unique_native_origins((Path("C:/private/python.exe"),
            Path("C:/private/Lib/venv/scripts/nt/PYTHON.EXE")))
    require_unique_native_origins((Path("C:/private/pkg/__init__.py"),
        Path("C:/private/other/__init__.py")))

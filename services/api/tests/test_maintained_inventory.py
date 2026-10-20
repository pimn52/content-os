"""Capacity/version migration; synthetic inventories and temporary stores only."""
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from app.domain.execution_runtime import (RuntimeInventory, RuntimeInventoryFile, RuntimeInventoryDescriptor,
    MAX_RUNTIME_FILES, MAX_RUNTIME_DIRECTORIES, MAX_RUNTIME_MANIFEST_BYTES, MAX_RUNTIME_FILE_BYTES)
from app.domain.maintained_runtime import MaintainedRuntimeInventory, MaintainedRuntimeInventoryDescriptor
from app.domain.models import ExecutionSpecificationRecord
from app.runtime_inventory import RuntimeInventoryStore, RuntimeInventoryError
from test_runtime_inventory import inventory

EMPTY = hashlib.sha256(b"").hexdigest()


def v2(value=None):
    value = inventory(2) if value is None else value
    return MaintainedRuntimeInventory(inventory_version=2, directories=value.directories, files=value.files)


@pytest.fixture(scope="module")
def large():
    files = (RuntimeInventoryFile(name="p.exe", role="interpreter", size_bytes=1, sha256="a"*64),
        RuntimeInventoryFile(name="e.py", role="entrypoint", size_bytes=1, sha256="b"*64),
        *(RuntimeInventoryFile(name=f"m{i:x}", role="dependency", size_bytes=0, sha256=EMPTY) for i in range(99998)))
    return MaintainedRuntimeInventory(inventory_version=2, directories=(), files=files)


@pytest.mark.parametrize("count", [56015, 100000])
def test_v2_capacity_and_canonical_budget(large, count):
    value = MaintainedRuntimeInventory(inventory_version=2, directories=(), files=large.files[:count])
    assert value.descriptor.file_count == count
    assert len(value.canonical_bytes()) <= MAX_RUNTIME_MANIFEST_BYTES


def test_old_limit_and_new_limit_are_independent(large):
    assert MAX_RUNTIME_FILES == 50000
    with pytest.raises(ValidationError):
        RuntimeInventory(inventory_version=1, directories=(), files=large.files[:50001])
    with pytest.raises(ValidationError):
        MaintainedRuntimeInventory(inventory_version=2, directories=(), files=(*large.files,
            RuntimeInventoryFile(name="extra", role="dependency", size_bytes=0, sha256=EMPTY)))
    with pytest.raises(ValidationError):
        RuntimeInventoryDescriptor(inventory_version=1, sha256="a"*64, size_bytes=1,
            file_count=50001, directory_count=0, total_file_bytes=1)


@pytest.mark.parametrize("version", [True, False, 2.0, "2", 1, 3])
def test_v2_version_is_exact_integer(version):
    value = v2().model_dump(mode="json")
    value["inventory_version"] = version
    with pytest.raises(ValidationError):
        MaintainedRuntimeInventory.model_validate_json(json.dumps(value))
    descriptor = v2().descriptor.model_dump(mode="json")
    descriptor["inventory_version"] = version
    with pytest.raises(ValidationError):
        MaintainedRuntimeInventoryDescriptor.model_validate_json(json.dumps(descriptor))


def test_other_capacity_limits_remain(large):
    small = v2()
    with pytest.raises(ValidationError):
        MaintainedRuntimeInventory(inventory_version=2, directories=tuple(f"d{i}" for i in range(MAX_RUNTIME_DIRECTORIES+1)), files=small.files)
    files = list(small.files)
    files[0] = RuntimeInventoryFile(name=files[0].name, role=files[0].role,
        size_bytes=MAX_RUNTIME_FILE_BYTES, sha256=files[0].sha256)
    with pytest.raises(ValidationError, match="runtime_size_limit"):
        MaintainedRuntimeInventory(inventory_version=2, directories=small.directories, files=tuple(files))
    # Valid paths and count can still exceed the independently enforced JSON budget.
    long_files = tuple(RuntimeInventoryFile(name="long"+"x"*100+f"{i:x}", role=item.role,
        size_bytes=item.size_bytes, sha256=item.sha256) for i,item in enumerate(large.files))
    long = MaintainedRuntimeInventory(inventory_version=2, directories=(), files=long_files)
    with pytest.raises(ValueError, match="runtime_manifest_size_limit"):
        long.canonical_bytes()
    with pytest.raises(ValidationError):
        MaintainedRuntimeInventoryDescriptor(inventory_version=2, sha256="a"*64, size_bytes=MAX_RUNTIME_MANIFEST_BYTES+1,
            file_count=3, directory_count=0, total_file_bytes=1)


def test_version_changes_digest_but_order_does_not():
    old, new = inventory(2), v2()
    assert old.descriptor.sha256 != new.descriptor.sha256
    reordered = MaintainedRuntimeInventory(inventory_version=2, directories=tuple(reversed(new.directories)), files=tuple(reversed(new.files)))
    assert reordered.descriptor == new.descriptor


@pytest.mark.parametrize("change", ["parent", "duplicate", "traversal", "empty", "extra"])
def test_v2_preserves_membership_path_and_role_validation(change):
    raw = v2().model_dump(mode="json")
    if change == "parent": raw["directories"] = []
    if change == "duplicate": raw["files"].append(raw["files"][-1])
    if change == "traversal": raw["files"][-1]["name"] = "../escape"
    if change == "empty": raw["files"][0]["size_bytes"] = 0
    if change == "extra": raw["untrusted_override"] = 1000000
    with pytest.raises(ValidationError):
        MaintainedRuntimeInventory.model_validate_json(json.dumps(raw))


def test_store_explicit_versions_and_conflict(tmp_path):
    store, old, new = RuntimeInventoryStore(tmp_path), inventory(2), v2()
    old_d, new_d = store.install(old), store.install_v2(new)
    assert store.read(old_d).canonical_bytes() == old.canonical_bytes()
    assert store.read_v2(new_d).canonical_bytes() == new.canonical_bytes()
    assert store.install_v2(new) == new_d
    for operation, value in [(store.install,new),(store.read,new_d),(store.install_v2,old),(store.read_v2,old_d)]:
        with pytest.raises(RuntimeInventoryError, match="inventory_invalid"):
            operation(value)
    path = store._path(new_d)
    path.write_bytes(b"conflicting retained bytes")
    with pytest.raises(RuntimeInventoryError, match="inventory_changed"):
        store.install_v2(new)
    assert path.read_bytes() == b"conflicting retained bytes"


def test_v2_request_cache_reuses_parse_but_rechecks_content(tmp_path, monkeypatch):
    value, store, cache = v2(), RuntimeInventoryStore(tmp_path), {}
    descriptor = value.descriptor
    path = store._path(descriptor)
    path.parent.mkdir(parents=True)
    path.write_bytes(value.canonical_bytes())
    reads = 0
    original = store._read

    def count_parse(*args):
        nonlocal reads
        reads += 1
        return original(*args)

    monkeypatch.setattr(store, "_read", count_parse)
    first = store.read_v2(descriptor, parsed_cache=cache)
    second = store.read_v2(descriptor, parsed_cache=cache)
    assert first is second and reads == 1

    path.write_bytes(b"changed after first validation")
    with pytest.raises(RuntimeInventoryError, match="inventory_changed"):
        store.read_v2(descriptor, parsed_cache=cache)


@pytest.mark.parametrize("variant", ["duplicate", "whitespace", "version", "counts"])
def test_store_rejects_mixed_or_noncanonical_payload(tmp_path, variant):
    value, store = v2(), RuntimeInventoryStore(tmp_path)
    raw = value.canonical_bytes()
    if variant == "duplicate": raw = raw.replace(b'"inventory_version":2', b'"inventory_version":2,"inventory_version":2')
    if variant == "whitespace": raw += b" "
    if variant == "version": raw = raw.replace(b'"inventory_version":2', b'"inventory_version":1')
    d = value.descriptor.model_dump(mode="json")
    d.update(sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
    if variant == "counts": d["file_count"] -= 1
    d = MaintainedRuntimeInventoryDescriptor.model_validate(d)
    path = store._path(d); path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
    with pytest.raises(RuntimeInventoryError, match="inventory_invalid"):
        store.read_v2(d)


def test_schema4_v1_golden_and_v2_migration(tmp_path):
    golden = json.loads((Path(__file__).parent/"fixtures/execution_schema4_inventory_v1.json").read_text(encoding="utf-8"))
    adapter = TypeAdapter(ExecutionSpecificationRecord)
    old = adapter.validate_json(json.dumps(golden["input"]))
    assert old.model_dump_json() == golden["serialized"]
    assert old.execution_sha256 == golden["execution_sha256"]
    assert old.model_artifact_sha256 == golden["model_artifact_sha256"]
    raw = old.model_dump(mode="json")
    raw["runtime_inventory"] = v2().descriptor.model_dump(mode="json")
    new = adapter.validate_json(json.dumps(raw))
    assert new.runtime_inventory.inventory_version == 2
    assert new.execution_sha256 != old.execution_sha256
    store = RuntimeInventoryStore(tmp_path)
    assert store.read_v2(store.install_v2(v2())).descriptor == v2().descriptor
    for version in [True, 2.0, "2", 3]:
        bad = json.loads(json.dumps(raw));bad["runtime_inventory"]["inventory_version"] = version
        with pytest.raises(ValidationError): adapter.validate_json(json.dumps(bad))
    legacy = json.loads((Path(__file__).parent/"fixtures/execution_schema_legacy.json").read_text(encoding="utf-8"))
    for case in legacy["cases"]:
        if case["input"]["schema_version"] in (2,3):
            bad = dict(case["input"], runtime_inventory=v2().descriptor.model_dump(mode="json"))
            with pytest.raises(ValidationError): adapter.validate_json(json.dumps(bad))

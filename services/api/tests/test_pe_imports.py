"""Synthetic PE bytes only; nothing loaded or executed."""
import hashlib
import struct

import pytest

from app.providers.pe_imports import (PE_LIMIT, parse_pe_imports, read_pe_imports,
    require_known_imports)
from app.providers.prepared import NativeExecutionUnsupported


def image(ordinary=("KERNEL32.dll",), delayed=()):
    data = bytearray(4096)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 128)
    data[128:132] = b"PE\0\0"
    struct.pack_into("<HH", data, 132, 0x8664, 1)
    struct.pack_into("<H", data, 148, 240)
    optional = 152
    struct.pack_into("<H", data, optional, 0x20b)
    struct.pack_into("<I", data, optional + 60, 512)
    struct.pack_into("<I", data, optional + 108, 16)
    section = optional + 240
    struct.pack_into("<IIII", data, section + 8, 3584, 4096, 3584, 512)
    strings = 2048
    for index, names, width, raw in ((1, ordinary, 20, 512), (13, delayed, 32, 1024)):
        if not names: continue
        struct.pack_into("<II", data, optional + 112 + index * 8, raw + 3584, (len(names) + 1) * width)
        for position, name in enumerate(names):
            entry = raw + position * width
            if index == 13: struct.pack_into("<I", data, entry, 1)
            struct.pack_into("<I", data, entry + (4 if index == 13 else 12), strings + 3584)
            encoded = name.encode() + b"\0"
            data[strings:strings + len(encoded)] = encoded
            strings += len(encoded)
    return data


def test_ordinary_and_delay_imports_are_combined_not_loaded():
    value = parse_pe_imports(bytes(image(("KERNEL32.dll", "kernel32.DLL"), ("nvcuda.dll",))))
    assert value.ordinary == ("kernel32.dll",)
    assert value.delayed == ("nvcuda.dll",)
    assert value.dependencies == ("kernel32.dll", "nvcuda.dll")
    require_known_imports(value, allowed=frozenset(value.dependencies))


def test_no_imports_is_not_a_native_execution_permission():
    assert parse_pe_imports(bytes(image((), ()))).dependencies == ()


@pytest.mark.parametrize("name", ["api-ms-win-core-file-l1-1-0.dll", "vcruntime140.dll", "nvapi64.dll"])
@pytest.mark.parametrize("delayed", [False, True])
def test_unknown_api_sets_user_libraries_and_delayed_dependencies_stop(name, delayed):
    value = parse_pe_imports(bytes(image(() if delayed else (name,), (name,) if delayed else ())))
    with pytest.raises(NativeExecutionUnsupported, match="driver_import_closure_unsupported"):
        require_known_imports(value, allowed=frozenset(("kernel32.dll",)))


@pytest.mark.parametrize("name", ["../evil.dll", "C:\\evil.dll", "nested/evil.dll", "evil..dll", "evil.exe", "é.dll", ""])
def test_unsafe_import_names_do_not_become_paths(name):
    with pytest.raises(NativeExecutionUnsupported, match="pe_imports_unsupported"):
        parse_pe_imports(bytes(image((name,))))


@pytest.mark.parametrize("problem", ["mz", "pe", "x86", "pe32", "sections", "headers", "directories",
    "optional", "raw", "rva", "virtual_tail", "unterminated", "hidden", "table_size", "delay_va", "delay_flags",
    "overlap", "truncated", "large"])
def test_malformed_or_unsupported_images_fail_closed(problem):
    data = image(delayed=("nvcuda.dll",))
    writes = {
        "mz": (0, "<H", 0), "pe": (128, "<I", 0), "x86": (132, "<H", 0x14c),
        "pe32": (152, "<H", 0x10b), "sections": (134, "<H", 97),
        "headers": (212, "<I", 400), "directories": (260, "<I", 17),
        "optional": (148, "<H", 112), "raw": (392 + 20, "<I", 4096),
        "rva": (512 + 12, "<I", 0xffffffff), "virtual_tail": (512 + 12, "<I", 8000),
        "unterminated": (272 + 4, "<I", 20), "hidden": (552, "<I", 1),
        "table_size": (272 + 4, "<I", 21), "delay_va": (1024, "<I", 0),
        "delay_flags": (1024, "<I", 3),
    }
    if problem in writes:
        offset, fmt, value = writes[problem]
        struct.pack_into(fmt, data, offset, value)
        if problem == "hidden": struct.pack_into("<I", data, 276, 60)
    elif problem == "overlap":
        struct.pack_into("<H", data, 134, 2)
        data[432:472] = data[392:432]
    elif problem == "truncated": data = data[:350]
    elif problem == "large": data = bytearray(PE_LIMIT + 1)
    with pytest.raises(NativeExecutionUnsupported, match="pe_imports_unsupported"):
        parse_pe_imports(bytes(data))


def test_names_must_terminate_inside_raw_section():
    data = image()
    struct.pack_into("<I", data, 524, 7679)
    data[-1] = 65
    with pytest.raises(NativeExecutionUnsupported, match="pe_imports_unsupported"):
        parse_pe_imports(bytes(data))


def test_same_read_bytes_are_hash_checked_before_parsing(tmp_path):
    path = tmp_path / "fixed.dll"
    payload = bytes(image())
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    assert read_pe_imports(path, size_bytes=len(payload), sha256=digest).ordinary == ("kernel32.dll",)
    path.write_bytes(bytes(image(("nvapi64.dll",))))
    with pytest.raises(NativeExecutionUnsupported, match="pe_source_changed"):
        read_pe_imports(path, size_bytes=len(payload), sha256=digest)


def test_default_cuda_reader_checks_static_imports_before_unsupported_loader(tmp_path):
    from app.domain.execution_runtime import HostDriverLibrary
    from app.providers.host_runtime import DriverSource, _read_cuda_facts

    path = tmp_path / "nvcuda.dll"
    payload = bytes(image(delayed=("nvapi64.dll",)))
    path.write_bytes(payload)
    descriptor = HostDriverLibrary(role="driver", slot="driver/nvcuda.dll", size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest())
    with pytest.raises(NativeExecutionUnsupported, match="driver_import_closure_unsupported"):
        _read_cuda_facts(windows_root=tmp_path, driver_sources=(DriverSource(descriptor.slot, path),),
            libraries=(descriptor,))

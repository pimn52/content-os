"""Bounded static AMD64 PE imports, never a loader or completeness certificate.

Recipe prechecks ordinary and delay imports before a native load. Export
forwarders, dynamic LoadLibrary and API-set resolution still need separate
closure; a successful static check cannot authorize execution.
Format: https://learn.microsoft.com/en-us/windows/win32/debug/pe-format
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import struct

from .runtime_primitives import NativeExecutionUnsupported, _exact_path

PE_LIMIT = 64 * 1024 * 1024
IMPORT_LIMIT = 512


@dataclass(frozen=True)
class PEImports:
    ordinary: tuple[str, ...]
    delayed: tuple[str, ...]

    @property
    def dependencies(self):
        return tuple(sorted(set(self.ordinary + self.delayed)))


def parse_pe_imports(payload: bytes) -> PEImports:
    """Reject unsupported/truncated/ambiguous layouts instead of guessing RVAs."""
    try:
        if type(payload) is not bytes or not 64 <= len(payload) <= PE_LIMIT:
            raise ValueError()

        def span(offset, size):
            if offset < 0 or size < 0 or offset + size > len(payload):
                raise ValueError()
            return payload[offset:offset + size]

        def u16(offset): return struct.unpack("<H", span(offset, 2))[0]
        def u32(offset): return struct.unpack("<I", span(offset, 4))[0]

        pe = u32(60)
        if payload[:2] != b"MZ" or pe < 64 or span(pe, 4) != b"PE\0\0" or u16(pe + 4) != 0x8664:
            raise ValueError()
        count, optional_size = u16(pe + 6), u16(pe + 20)
        optional = pe + 24
        if not 1 <= count <= 96 or optional_size < 112 or u16(optional) != 0x20b:
            raise ValueError()
        span(optional, optional_size)
        directory_count = u32(optional + 108)
        if directory_count > 16 or optional_size < 112 + 8 * directory_count:
            raise ValueError()
        headers = u32(optional + 60)
        section_table = optional + optional_size
        if not section_table + count * 40 <= headers <= len(payload):
            raise ValueError()
        sections = []
        for index in range(count):
            offset = section_table + index * 40
            virtual_size, rva, raw_size, raw = struct.unpack("<IIII", span(offset + 8, 16))
            extent = max(virtual_size, raw_size)
            if extent and (rva < headers or rva + extent > 0x100000000):
                raise ValueError()
            if raw_size:
                if raw < headers: raise ValueError()
                span(raw, raw_size)
            for vr, ve, rr, rs in sections:
                if (extent and ve and rva < vr + ve and vr < rva + extent) or (
                    raw_size and rs and raw < rr + rs and rr < raw + raw_size):
                    raise ValueError()
            sections.append((rva, extent, raw, raw_size))

        def location(rva, size):
            if rva <= 0 or size <= 0: raise ValueError()
            if rva + size <= headers: return rva, headers - rva
            matches = [(raw + rva - vr, rs - (rva - vr)) for vr, _ve, raw, rs in sections
                       if vr <= rva and rva + size <= vr + rs]
            if len(matches) != 1: raise ValueError()
            return matches[0]

        def name(rva):
            offset, available = location(rva, 1)
            data = span(offset, min(available, 256))
            end = data.find(b"\0")
            if not 0 < end <= 255: raise ValueError()
            result = data[:end].decode("ascii").lower()
            if not re.fullmatch(r"[a-z0-9_][a-z0-9_.-]*\.dll", result) or ".." in result:
                raise ValueError()
            return result

        def imports(directory, width, delayed):
            if directory >= directory_count: return ()
            address, size = struct.unpack("<II", span(optional + 112 + directory * 8, 8))
            if address == size == 0: return ()
            if not address or not width <= size <= (IMPORT_LIMIT + 1) * width or size % width:
                raise ValueError()
            offset, _available = location(address, size)
            result = []
            for position in range(0, size, width):
                descriptor = span(offset + position, width)
                if not any(descriptor):
                    # Nothing may be hidden after the table terminator.
                    if any(span(offset + position, size - position)): raise ValueError()
                    return tuple(sorted(set(result)))
                if len(result) >= IMPORT_LIMIT: raise ValueError()
                if delayed and struct.unpack_from("<I", descriptor)[0] != 1:
                    # Legacy VA-based descriptors and reserved flags unsupported.
                    raise ValueError()
                result.append(name(struct.unpack_from("<I", descriptor, 4 if delayed else 12)[0]))
            raise ValueError()  # Missing mandatory zero descriptor.

        return PEImports(imports(1, 20, False), imports(13, 32, True))
    except (ValueError, TypeError, struct.error, UnicodeError):
        raise NativeExecutionUnsupported("execution_pe_imports_unsupported") from None


def read_pe_imports(path: Path, *, size_bytes: int, sha256: str) -> PEImports:
    """One bounded file read; parse the same bytes compared to the observation."""
    try:
        _exact_path(path, directory=False)
        if type(size_bytes) is not int or not 0 < size_bytes <= PE_LIMIT:
            raise ValueError()
        with path.open("rb") as source:
            payload = source.read(size_bytes + 1)
        if len(payload) != size_bytes or hashlib.sha256(payload).hexdigest() != sha256:
            raise ValueError()
        return parse_pe_imports(payload)
    except (OSError, ValueError):
        raise NativeExecutionUnsupported("execution_pe_source_changed") from None


def require_known_imports(value: PEImports, *, allowed: frozenset[str]) -> None:
    if set(value.dependencies) - allowed:
        # No API-set/PATH/System32 fallback and no load-first approval.
        raise NativeExecutionUnsupported("execution_driver_import_closure_unsupported")

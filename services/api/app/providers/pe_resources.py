"""Bounded byte-only numeric PE resources; classifications never authorize load."""
from dataclasses import dataclass
import hashlib
import struct

from .native_manifest import CPYTHON_COMMON_CONTROLS
from .pe_imports import parse_pe_imports
from .runtime_primitives import NativeExecutionUnsupported

RESOURCE_LIMIT = 1024 * 1024
RESOURCE_NODE_LIMIT = 512
RESOURCE_LEAF_LIMIT = 256
MANIFEST_LIMIT = 64 * 1024


class PEView:
    """Reuse existing AMD64 header/section validation without changing imports."""
    def __init__(self, payload):
        self.imports = parse_pe_imports(payload)
        self.payload = payload
        self.pe = self.u32(60)
        self.optional = self.pe + 24
        self.directory_count = self.u32(self.optional + 108)
        self.headers = self.u32(self.optional + 60)
        start = self.optional + self.u16(self.pe + 20)
        self.sections = tuple(struct.unpack_from('<IIII', payload, start + i * 40 + 8)
            for i in range(self.u16(self.pe + 6)))

    def span(self, offset, size):
        if offset < 0 or size < 0 or offset + size > len(self.payload):
            raise ValueError()
        return self.payload[offset:offset + size]

    def u16(self, offset): return struct.unpack('<H', self.span(offset, 2))[0]
    def u32(self, offset): return struct.unpack('<I', self.span(offset, 4))[0]

    def directory(self, index):
        if index >= self.directory_count: return 0, 0
        return struct.unpack('<II', self.span(self.optional + 112 + index * 8, 8))

    def locate(self, rva, size, *, headers=True):
        if rva <= 0 or size <= 0 or rva + size > 0x100000000:
            raise ValueError()
        if headers and rva + size <= self.headers: return rva
        candidates = [raw + rva - start for _, start, length, raw in self.sections
            if start <= rva and rva + size <= start + length]
        if len(candidates) != 1: raise ValueError()
        self.span(candidates[0], size)
        return candidates[0]


@dataclass(frozen=True)
class ResourceClassification:
    kind: str
    leaf_count: int
    manifest_sha256: str | None = None


@dataclass(frozen=True)
class ResourceLeaf:
    ids: tuple[int, int, int]
    data: bytes
    codepage: int


def classify_pe_resources(payload: bytes, *, role: str = 'dll'):
    """Only the exact reviewed manifest becomes pending binding, never allowed."""
    return classify_resource_view(PEView(payload), role=role)


def classify_resource_view(view, *, role: str = 'dll'):
    """Same classification for a bounded byte view; no new manifest allowance."""
    leaves = read_resource_leaves(view, role=role)
    if leaves is None:
        return ResourceClassification('no_resources', 0)
    manifests = []
    for leaf in leaves:
        if leaf.ids[0] == 24:
            expected = (24, 2 if role == 'dll' else 1, 1033)
            if (leaf.ids != expected or leaf.codepage or len(leaf.data) > MANIFEST_LIMIT
                    or leaf.data != CPYTHON_COMMON_CONTROLS or manifests):
                raise NativeExecutionUnsupported('execution_pe_resources_unsupported')
            manifests.append(hashlib.sha256(leaf.data).hexdigest())
    if manifests:
        return ResourceClassification('requires_os_sxs_binding', len(leaves), manifests[0])
    return ResourceClassification('data_resources', len(leaves))


def read_resource_leaves(view, *, role='dll'):
    """Bounded structure/data only. Returned manifests still need owned policy."""
    return _read_resource_leaves(view, role=role)


def read_private_resource_leaves(view):
    """Explicit private DLL representation; no legacy classification change."""
    return _read_resource_leaves(view, role='dll', string_tables=True)


def _string_table(data, codepage):
    if codepage != 0: raise ValueError()
    position = 0
    for _ in range(16):
        if position + 2 > len(data): raise ValueError()
        length = struct.unpack_from('<H', data, position)[0]
        position += 2
        end = position + length * 2
        if end > len(data): raise ValueError()
        data[position:end].decode('utf-16-le', errors='strict')
        position = end
    if position != len(data): raise ValueError()


def _read_resource_leaves(view, *, role='dll', string_tables=False):
    try:
        if role not in ('dll', 'bootstrap_exe'):
            raise ValueError()
        is_dll = bool(view.u16(view.pe + 22) & 0x2000)
        if is_dll != (role == 'dll'): raise ValueError()
        rva, size = view.directory(2)
        if rva == size == 0: return None
        if not rva or not 16 <= size <= RESOURCE_LIMIT: raise ValueError()
        origin = view.locate(rva, size, headers=False)
        occupied, seen, leaves = [], set(), []
        allowed_types = {16, 24} if role == 'dll' else {3, 14, 16, 24}
        if string_tables: allowed_types.add(6)

        def claim(offset, length):
            if offset < 0 or length <= 0 or offset + length > size:
                raise ValueError()
            if any(offset < hi and lo < offset + length for lo, hi in occupied):
                raise ValueError()
            occupied.append((offset, offset + length))

        def walk(offset, ids):
            if len(ids) >= 3 or offset % 4 or offset in seen or len(seen) >= RESOURCE_NODE_LIMIT:
                raise ValueError()
            seen.add(offset)
            if not 0 <= offset <= size - 16: raise ValueError()
            flags, _, _, _, named, numbered = struct.unpack('<IIHHHH', view.span(origin + offset, 16))
            if flags or named or numbered > RESOURCE_LEAF_LIMIT: raise ValueError()
            if not numbered and ids: raise ValueError()
            claim(offset, 16 + numbered * 8)
            previous = -1
            for index in range(numbered):
                identity, pointer = struct.unpack('<II', view.span(origin + offset + 16 + index * 8, 8))
                if not previous < identity <= 0xffff: raise ValueError()
                previous = identity
                path = (*ids, identity)
                if not ids and identity not in allowed_types: raise ValueError()
                child = pointer & 0x7fffffff
                if len(path) < 3:
                    if not pointer & 0x80000000: raise ValueError()
                    walk(child, path)
                else:
                    if pointer & 0x80000000 or child % 4 or len(leaves) >= RESOURCE_LEAF_LIMIT:
                        raise ValueError()
                    claim(child, 16)
                    data_rva, length, codepage, reserved = struct.unpack('<IIII', view.span(origin + child, 16))
                    if reserved: raise ValueError()
                    claim(data_rva - rva, length)
                    data = view.span(origin + data_rva - rva, length)
                    if path[0] == 6: _string_table(data, codepage)
                    leaves.append(ResourceLeaf(path, data, codepage))
        walk(0, ())
        return tuple(leaves)
    except (ValueError, TypeError, struct.error, UnicodeError):
        raise NativeExecutionUnsupported('execution_pe_resources_unsupported') from None


def private_export_dependencies(payload: bytes):
    """An export directory may forward to other DLLs even with no import table."""
    return export_dependencies_view(PEView(payload))


def export_dependencies_view(view, *, directory_limit=RESOURCE_LIMIT, export_limit=16384,
                             suffixes=('.dll',)):
    """Finite data representation; the legacy caller retains its exact limits."""
    import re
    try:
        rva, size = view.directory(0)
        if rva == size == 0: return ()
        if not rva or not 40 <= size <= directory_limit: raise ValueError()
        origin = view.locate(rva, size, headers=False)
        count = view.u32(origin + 20)
        address = view.u32(origin + 28)
        if count > export_limit: raise ValueError()
        if not count:
            if address: raise ValueError()
            return ()
        table = view.locate(address, count * 4, headers=False)
        names = set()
        for index in range(count):
            function = view.u32(table + index * 4)
            if not function: continue
            if rva <= function < rva + size:
                offset = view.locate(function, 1, headers=False)
                raw = view.span(offset, min(256, rva + size - function))
                end = raw.find(b'\0')
                if not 0 < end < 256: raise ValueError()
                module, symbol = raw[:end].decode('ascii').rsplit('.', 1)
                name = module.lower() if module.lower().endswith(suffixes) else module.lower() + '.dll'
                if (not re.fullmatch(r'[a-z0-9_][a-z0-9_.-]*', name)
                        or not name.endswith(suffixes) or '..' in name):
                    raise ValueError()
                if not re.fullmatch(r'(?:[A-Za-z_?][A-Za-z0-9_?$@]*|#[0-9]+)', symbol): raise ValueError()
                names.add(name)
            else:
                # Exported zero-initialized data can live in a mapped virtual
                # section tail with no raw file bytes. Nothing is read there.
                if not any(start <= function < start + max(virtual, length)
                        for virtual, start, length, _raw in view.sections):
                    raise ValueError()
        return tuple(sorted(names))
    except (ValueError, TypeError, struct.error, UnicodeError):
        raise NativeExecutionUnsupported('execution_pe_forwarders_unsupported') from None

"""Bounded PE resource data inspection. It never gives executable authority."""
from dataclasses import dataclass
import struct

from .runtime_primitives import NativeExecutionUnsupported

FILE_LIMIT = 64 * 1024 * 1024
RESOURCE_LIMIT = 1024 * 1024
NODE_LIMIT = 512
LEAF_LIMIT = 256


@dataclass(frozen=True)
class ResourceDataEvidence:
    machine: str
    optional_magic: str
    leaf_count: int


def inspect_resource_data(payload: bytes):
    """Conservative data subset, including PE32 MUI in an AMD64 assembly.

    Names/data remain opaque. No resource mapping, signature execution, imports
    or system API calls are performed. The owning component manifest binds use.
    """
    try:
        if type(payload) is not bytes or not 64 <= len(payload) <= FILE_LIMIT:
            raise ValueError()

        def span(offset, length):
            if offset < 0 or length < 0 or offset + length > len(payload):
                raise ValueError()
            return payload[offset:offset + length]

        def u16(offset): return struct.unpack('<H', span(offset, 2))[0]
        def u32(offset): return struct.unpack('<I', span(offset, 4))[0]

        pe = u32(60)
        if payload[:2] != b'MZ' or pe < 64 or span(pe, 4) != b'PE\0\0':
            raise ValueError()
        machine, count = u16(pe + 4), u16(pe + 6)
        opt, optional_size = pe + 24, u16(pe + 20)
        span(opt, optional_size)
        magic = u16(opt)
        if (machine, magic) not in ((0x14c, 0x10b), (0x8664, 0x20b)):
            raise ValueError()
        minimum = 96 if magic == 0x10b else 112
        if (not 1 <= count <= 96 or optional_size < minimum or
                not u16(pe + 22) & 0x2000 or u32(opt + 4) or u32(opt + 16)):
            raise ValueError()
        directory_count = u32(opt + minimum - 4)
        if not 3 <= directory_count <= 16 or optional_size < minimum + 8 * directory_count:
            raise ValueError()
        headers, image_size = u32(opt + 60), u32(opt + 56)
        table = opt + optional_size
        if not table + count * 40 <= headers <= len(payload) or image_size < headers:
            raise ValueError()
        sections = []
        for index in range(count):
            pos = table + index * 40
            virtual_size, rva, raw_size, raw = struct.unpack('<IIII', span(pos + 8, 16))
            flags = u32(pos + 36)
            extent = max(virtual_size, raw_size)
            if flags & (0x20 | 0x20000000):
                raise ValueError()
            if extent and (rva < headers or rva + extent > image_size or rva + extent > 0xffffffff):
                raise ValueError()
            if raw_size:
                if raw < headers: raise ValueError()
                span(raw, raw_size)
            for vr, ve, rr, rs in sections:
                if (extent and ve and rva < vr + ve and vr < rva + extent) or (
                        raw_size and rs and raw < rr + rs and rr < raw + raw_size):
                    raise ValueError()
            sections.append((rva, extent, raw, raw_size))

        def locate(rva, length):
            if rva <= 0 or length <= 0: raise ValueError()
            matches = [(raw + rva - vr, raw, rs) for vr, _, raw, rs in sections
                       if vr <= rva and rva + length <= vr + rs]
            if len(matches) != 1: raise ValueError()
            offset, _, _ = matches[0]
            span(offset, length)
            return offset

        directories = []
        for index in range(directory_count):
            rva, length = struct.unpack('<II', span(opt + minimum + index * 8, 8))
            if bool(rva) != bool(length) or (index not in (2, 4, 6) and (rva or length)):
                raise ValueError()
            if rva:
                if index == 4:
                    # Certificate table is file-relative and outside mapped data.
                    if rva < headers or rva % 8 or any(rva < raw + size and raw < rva + length
                                     for _, _, raw, size in sections if size):
                        raise ValueError()
                    span(rva, length)
                else:
                    locate(rva, length)
                    if index == 6:
                        if length % 28 or length > 28 * 256: raise ValueError()
                        for pos in range(locate(rva, length), locate(rva, length) + length, 28):
                            data_size, data_rva, data_raw = struct.unpack('<III', span(pos + 16, 12))
                            if data_size:
                                if data_raw < headers: raise ValueError()
                                span(data_raw, data_size)
                                if data_rva and locate(data_rva, data_size) != data_raw:
                                    raise ValueError()
            directories.append((rva, length))
        resource_rva, resource_size = directories[2]
        if not 16 <= resource_size <= RESOURCE_LIMIT:
            raise ValueError()
        origin = locate(resource_rva, resource_size)
        occupied, visited, leaves = [], set(), []

        def claim(offset, length):
            if offset < 0 or length <= 0 or offset + length > resource_size or any(
                    offset < hi and lo < offset + length for lo, hi in occupied):
                raise ValueError()
            occupied.append((offset, offset + length))

        def walk(offset, ids):
            if offset % 4 or offset in visited or len(visited) >= NODE_LIMIT or len(ids) >= 3:
                raise ValueError()
            visited.add(offset)
            raw = span(origin + offset, 16)
            flags, _, _, _, named, numbered = struct.unpack('<IIHHHH', raw)
            if flags or not 1 <= named + numbered <= LEAF_LIMIT:
                raise ValueError()
            claim(offset, 16 + 8 * (named + numbered))
            previous_named, previous_id = '', -1
            for index in range(named + numbered):
                identity, pointer = struct.unpack('<II', span(origin + offset + 16 + index * 8, 8))
                if index < named:
                    if len(ids) == 2: raise ValueError()
                    if not identity & 0x80000000: raise ValueError()
                    name_pos = identity & 0x7fffffff
                    if name_pos % 2: raise ValueError()
                    length = u16(origin + name_pos)
                    if not 1 <= length <= 256: raise ValueError()
                    claim(name_pos, 2 + 2 * length)
                    name = span(origin + name_pos + 2, 2 * length).decode('utf-16-le')
                    if '\0' in name or name <= previous_named: raise ValueError()
                    previous_named = name
                    identity = name
                else:
                    if not previous_id < identity <= 0xffff: raise ValueError()
                    previous_id = identity
                if not ids and identity in (24, 'RT_MANIFEST'):
                    raise ValueError()
                path = (*ids, identity)
                child = pointer & 0x7fffffff
                if len(path) < 3:
                    if not pointer & 0x80000000: raise ValueError()
                    walk(child, path)
                else:
                    if pointer & 0x80000000 or child % 4 or len(leaves) >= LEAF_LIMIT:
                        raise ValueError()
                    claim(child, 16)
                    data_rva, length, _, reserved = struct.unpack('<IIII', span(origin + child, 16))
                    if reserved: raise ValueError()
                    claim(data_rva - resource_rva, length)
                    span(origin + data_rva - resource_rva, length)
                    leaves.append(path)
        walk(0, ())
        return ResourceDataEvidence(hex(machine), hex(magic), len(leaves))
    except (ValueError, TypeError, struct.error, UnicodeError):
        raise NativeExecutionUnsupported('execution_component_resource_data_unsupported') from None

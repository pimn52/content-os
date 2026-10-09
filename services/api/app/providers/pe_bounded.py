"""Independent bounded AMD64 PE file data view, without native load authority.

Legacy 64MiB byte parser and native graph limits stay unchanged. This reader
hashes the whole same file before/after bounded random metadata reads. It does
not install, resolve imports, map executable images or authorize dispatch.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import struct

from .pe_imports import IMPORT_LIMIT, PEImports
from .pe_resources import classify_resource_view, export_dependencies_view
from .runtime_primitives import NativeExecutionUnsupported, _exact_path

FILE_LIMIT = 256*1024**2
SPAN_LIMIT = 1024**2
READ_LIMIT = 16*1024**2
EXPORT_DIRECTORY_LIMIT = 8*1024**2
EXPORT_LIMIT = 65536

# Diagnostic-only profile: the GNU-built import directory encloses lookup/name
# data after its null descriptor. This is not permission to prepare/load it.
_IMPORT_ENVELOPE_SOURCE = (20589056,
    'ed4f167a5330424524f45258e7ca2c8d7f03c0cba1f30ee4985553ac1f88ecf8')


class _PEFile:
    def __init__(self, path, size, digest):
        self.path, self.size, self.digest = path, size, digest
        self.source, self.state, self.read_bytes = None, 'new', 0
        try:
            if (not isinstance(path, Path) or type(size) is not int or not 64 <= size <= FILE_LIMIT
                    or type(digest) is not str or not re.fullmatch(r'[a-f0-9]{64}', digest)):
                raise ValueError()
            _exact_path(path, directory=False)
            # Rehash the actual FD bytes, never BufferedReader's old metadata
            # cache after a same-size write with restored timestamps.
            self.source = path.open('rb', buffering=0)
            self.signature = self._signature(os.fstat(self.source.fileno()))
            self._verify()
            self.state = 'open'
        except Exception:
            if self.source is not None: self.source.close()
            self.state = 'closed'
            raise NativeExecutionUnsupported('execution_pe_file_source_invalid') from None

    @staticmethod
    def _signature(value):
        # CPython3.12 Windows path-stat ctime is creation time while fd-stat
        # ctime may report change time; compare the common identity fields.
        # Whole-byte hashes before/after remain required, including unchanged
        # timestamps and same-size writes.
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    def _verify(self):
        _exact_path(self.path, directory=False)
        if (self.signature != self._signature(self.path.stat())
                or self.signature != self._signature(os.fstat(self.source.fileno()))
                or self.signature[2] != self.size):
            raise ValueError()
        self.source.seek(0)
        digest, count = hashlib.sha256(), 0
        for block in iter(lambda: self.source.read(1024**2), b''):
            count += len(block)
            if count > self.size: raise ValueError()
            digest.update(block)
        if count != self.size or digest.hexdigest() != self.digest:
            raise ValueError()
        if (self.signature != self._signature(self.path.stat())
                or self.signature != self._signature(os.fstat(self.source.fileno()))):
            raise ValueError()

    def bounds(self, offset, size):
        if (type(offset) is not int or type(size) is not int or offset < 0 or size <= 0
                or offset + size > self.size):
            raise ValueError()

    def span(self, offset, size):
        if self.state != 'open':
            raise NativeExecutionUnsupported('execution_pe_file_consumed')
        try:
            self.bounds(offset, size)
            if size > SPAN_LIMIT or self.read_bytes + size > READ_LIMIT:
                raise ValueError()
            self.read_bytes += size
            self.source.seek(offset)
            raw = self.source.read(size)
            if len(raw) != size: raise ValueError()
            return raw
        except Exception:
            self.state = 'failed'
            raise NativeExecutionUnsupported('execution_pe_file_read_unsupported') from None

    def close(self):
        if self.state == 'closed': return
        self.state = 'closed'
        try:
            self._verify()
        except Exception:
            raise NativeExecutionUnsupported('execution_pe_file_source_changed') from None
        finally:
            self.source.close()


class BoundedPEView:
    def __init__(self, source, *, _ordinary_envelope=False):
        self._source = source
        self._ordinary_envelope = _ordinary_envelope
        try:
            if (type(_ordinary_envelope) is not bool or _ordinary_envelope
                    and (source.size, source.digest) != _IMPORT_ENVELOPE_SOURCE):
                raise ValueError()
            self.pe = self.u32(60)
            if (self.span(0, 2) != b'MZ' or self.pe < 64 or self.span(self.pe, 4) != b'PE\0\0'
                    or self.u16(self.pe+4) != 0x8664):
                raise ValueError()
            count, optional_size = self.u16(self.pe+6), self.u16(self.pe+20)
            self.optional = self.pe+24
            if not 1 <= count <= 96 or optional_size < 112 or self.u16(self.optional) != 0x20b:
                raise ValueError()
            source.bounds(self.optional, optional_size)
            self.directory_count = self.u32(self.optional+108)
            if self.directory_count > 16 or optional_size < 112+8*self.directory_count:
                raise ValueError()
            self.headers = self.u32(self.optional+60)
            table = self.optional + optional_size
            if not table+count*40 <= self.headers <= source.size: raise ValueError()
            sections = []
            for index in range(count):
                virtual, rva, size, raw = struct.unpack('<IIII', self.span(table+index*40+8, 16))
                extent = max(virtual, size)
                if extent and (rva < self.headers or rva+extent > 0x100000000): raise ValueError()
                if size:
                    if raw < self.headers: raise ValueError()
                    source.bounds(raw, size)  # validate, never read the whole code section
                for old_virtual, old_rva, old_size, old_raw in sections:
                    old_extent = max(old_virtual, old_size)
                    if ((extent and old_extent and rva < old_rva+old_extent and old_rva < rva+extent)
                            or (size and old_size and raw < old_raw+old_size and old_raw < raw+size)):
                        raise ValueError()
                sections.append((virtual, rva, size, raw))
            self.sections = tuple(sections)
            ranges = []
            for index in (0, 1, 2, 13):
                rva, size = self.directory(index)
                if bool(rva) != bool(size): raise ValueError()
                if size:
                    self.locate(rva, size, headers=False)
                    if any(rva < hi and lo < rva+size for lo, hi in ranges): raise ValueError()
                    ranges.append((rva, rva+size))
            self.imports = PEImports(self._imports(1, 20, False), self._imports(13, 32, True))
        except NativeExecutionUnsupported:
            raise
        except Exception:
            raise NativeExecutionUnsupported('execution_pe_file_layout_unsupported') from None

    @property
    def dispatch_authorized(self): return False

    @property
    def read_bytes(self): return self._source.read_bytes

    def span(self, offset, size): return self._source.span(offset, size)
    def u16(self, offset): return struct.unpack('<H', self.span(offset, 2))[0]
    def u32(self, offset): return struct.unpack('<I', self.span(offset, 4))[0]

    def directory(self, index):
        if index >= self.directory_count: return 0, 0
        return struct.unpack('<II', self.span(self.optional+112+index*8, 8))

    def locate(self, rva, size, *, headers=True):
        if rva <= 0 or size <= 0 or rva+size > 0x100000000: raise ValueError()
        if headers and rva+size <= self.headers: return rva
        matches = [raw+rva-start for _, start, length, raw in self.sections
                   if start <= rva and rva+size <= start+length]
        if len(matches) != 1: raise ValueError()
        self._source.bounds(matches[0], size)
        return matches[0]

    def _name(self, rva):
        offset = self.locate(rva, 1)
        available = next((raw+size-offset for _, start, size, raw in self.sections
            if start <= rva < start+size), self.headers-offset)
        raw = self.span(offset, min(available, 256))
        end = raw.find(b'\0')
        if not 0 < end <= 255: raise ValueError()
        name = raw[:end].decode('ascii').lower()
        if not re.fullmatch(r'[a-z0-9_][a-z0-9_.-]*\.(dll|pyd)', name) or '..' in name:
            raise ValueError()
        return name

    def _imports(self, index, width, delayed):
        rva, size = self.directory(index)
        if rva == size == 0: return ()
        if not rva or not width <= size <= (IMPORT_LIMIT+1)*width or size % width:
            raise ValueError()
        origin = self.locate(rva, size)
        rows = []
        for position in range(0, size, width):
            descriptor = self.span(origin+position, width)
            if not any(descriptor):
                if (not self._ordinary_envelope or delayed) and any(self.span(origin+position, size-position)):
                    raise ValueError()
                return tuple(sorted(set(rows)))
            if len(rows) >= IMPORT_LIMIT or (delayed and struct.unpack_from('<I', descriptor)[0] != 1):
                raise ValueError()
            rows.append(self._name(struct.unpack_from('<I', descriptor, 4 if delayed else 12)[0]))
        raise ValueError()


@contextmanager
def open_bounded_pe(path, *, size_bytes, sha256):
    """One file lifetime; whole-file rehash completes before evidence returns."""
    source = _PEFile(path, size_bytes, sha256)
    try:
        yield BoundedPEView(source)
    finally:
        source.close()


@contextmanager
def open_import_envelope_pe(path, *, size_bytes, sha256):
    """One exact observed source, static facts only; legacy opener stays strict.

    PE import tables end at the null directory entry, independently of other
    .idata bytes: https://learn.microsoft.com/en-us/windows/win32/debug/pe-format
    Actual loading, source-build correspondence and provenance remain separate.
    """
    if (size_bytes, sha256) != _IMPORT_ENVELOPE_SOURCE or type(size_bytes) is not int:
        raise NativeExecutionUnsupported('execution_pe_import_envelope_source_unknown')
    source = _PEFile(path, size_bytes, sha256)
    try:
        yield BoundedPEView(source, _ordinary_envelope=True)
    finally:
        source.close()


@dataclass(frozen=True)
class PEFileEvidence:
    imports: PEImports
    resources: object
    forwarders: tuple[str, ...]
    metadata_read_bytes: int

    @property
    def dispatch_authorized(self): return False


def inspect_bounded_pe(path, *, size_bytes, sha256, role='dll'):
    with open_bounded_pe(path, size_bytes=size_bytes, sha256=sha256) as view:
        resources = classify_resource_view(view, role=role)
        forwarders = export_dependencies_view(view, directory_limit=EXPORT_DIRECTORY_LIMIT,
            export_limit=EXPORT_LIMIT, suffixes=('.dll', '.pyd'))
        result = PEFileEvidence(view.imports, resources, forwarders, view.read_bytes)
    return result

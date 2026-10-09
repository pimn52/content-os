"""Synthetic file bytes only; no executable/native mappings or loader calls."""
import hashlib
import os
import struct

import pytest

from app.providers import pe_bounded as subject
from app.providers.pe_imports import PE_LIMIT, parse_pe_imports
from app.providers.pe_resources import classify_pe_resources, private_export_dependencies
from app.providers.prepared import NativeExecutionUnsupported
from test_pe_imports import image
from test_pe_resources import manifest_image, resource_image


def fixed(tmp_path, raw):
    path = tmp_path / 'data.pe.bin'
    path.write_bytes(raw)
    return dict(path=path, size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def test_same_facts_and_old_limits_preserved(tmp_path):
    raw = manifest_image()
    result = subject.inspect_bounded_pe(**fixed(tmp_path, raw))
    assert result.imports == parse_pe_imports(raw)
    assert result.resources == classify_pe_resources(raw)
    assert result.forwarders == private_export_dependencies(raw)
    assert result.dispatch_authorized is False
    assert subject.SPAN_LIMIT == 1024**2 and PE_LIMIT == 64*1024**2


def test_large_file_is_stream_hashed_with_bounded_metadata(tmp_path):
    path = tmp_path/'large.data.bin'
    prefix = resource_image()
    with path.open('wb') as output:
        output.write(prefix)
        output.truncate(PE_LIMIT + 1024)
    with path.open('rb') as source: digest = hashlib.file_digest(source, 'sha256').hexdigest()
    result = subject.inspect_bounded_pe(path, size_bytes=path.stat().st_size, sha256=digest)
    assert result.metadata_read_bytes < 4096 and result.imports.ordinary == ('kernel32.dll',)
    with pytest.raises(NativeExecutionUnsupported):
        parse_pe_imports(path.read_bytes())


def test_pyd_imports_are_data_and_old_parser_stays_dll_only(tmp_path):
    raw = resource_image(ordinary=('libtorchaudio.pyd', 'python312.dll'), delayed=('next.pyd',))
    result = subject.inspect_bounded_pe(**fixed(tmp_path, raw))
    assert result.imports.dependencies == ('libtorchaudio.pyd', 'next.pyd', 'python312.dll')
    with pytest.raises(NativeExecutionUnsupported): parse_pe_imports(raw)


@pytest.mark.parametrize('name', ['../evil.pyd', 'folder/evil.pyd', 'bad..pyd', 'evil.exe', 'bad\\name.pyd'])
def test_new_names_do_not_allow_paths_or_other_extensions(tmp_path, name):
    with pytest.raises(NativeExecutionUnsupported):
        subject.inspect_bounded_pe(**fixed(tmp_path, resource_image(ordinary=(name,))))


@pytest.mark.parametrize('mutation', ['hash', 'size', 'bytes', 'delete', 'replaced'])
def test_source_identity_changes_reject(tmp_path, mutation):
    raw = resource_image()
    values = fixed(tmp_path, raw)
    if mutation == 'hash':
        values['sha256'] = '0'*64
        with pytest.raises(NativeExecutionUnsupported): subject.inspect_bounded_pe(**values)
        return
    if mutation == 'size':
        values['size_bytes'] += 1
        with pytest.raises(NativeExecutionUnsupported): subject.inspect_bounded_pe(**values)
        return
    try:
        with subject.open_bounded_pe(**values) as view:
            if mutation == 'bytes': values['path'].write_bytes(raw[:-1] + b'x')
            elif mutation == 'delete': values['path'].unlink()
            elif mutation == 'replaced':
                other = tmp_path/'replacement'
                other.write_bytes(raw)
                other.replace(values['path'])
    except NativeExecutionUnsupported as error:
        assert 'source_changed' in str(error)
    except PermissionError:
        # Windows can deny deletion/replacement while the same FD is open.
        # No mutation occurred; the reader did not waive its source checks.
        assert mutation in ('delete', 'replaced') and values['path'].read_bytes() == raw
    else:
        pytest.fail('changed source was accepted')


def test_span_and_total_limits_consume_read_lifetime(tmp_path, monkeypatch):
    values = fixed(tmp_path, resource_image())
    with subject.open_bounded_pe(**values) as view:
        monkeypatch.setattr(subject, 'SPAN_LIMIT', 1)
        with pytest.raises(NativeExecutionUnsupported): view.span(0, 2)
        with pytest.raises(NativeExecutionUnsupported, match='consumed'): view.span(0, 1)
    monkeypatch.setattr(subject, 'SPAN_LIMIT', 1024**2)
    with subject.open_bounded_pe(**values) as view:
        monkeypatch.setattr(subject, 'READ_LIMIT', view.read_bytes+1)
        view.span(0, 1)
        with pytest.raises(NativeExecutionUnsupported): view.span(0, 1)


def test_closed_view_has_no_read_access(tmp_path):
    with subject.open_bounded_pe(**fixed(tmp_path, resource_image())) as view:
        assert not view.dispatch_authorized
    with pytest.raises(NativeExecutionUnsupported, match='consumed'): view.span(0, 1)


def test_same_size_write_with_restored_time_still_fails_hash(tmp_path):
    raw = resource_image()
    values = fixed(tmp_path, raw)
    stamp = values['path'].stat()
    with pytest.raises(NativeExecutionUnsupported, match='source_changed'):
        with subject.open_bounded_pe(**values):
            values['path'].write_bytes(raw[:-1] + b'x')
            os.utime(values['path'], ns=(stamp.st_atime_ns, stamp.st_mtime_ns))


@pytest.mark.parametrize('update', [dict(size_bytes=True), dict(size_bytes=subject.FILE_LIMIT+1), dict(sha256='invalid')])
def test_invalid_descriptor_stops_before_read(tmp_path, update):
    values = fixed(tmp_path, resource_image()) | update
    with pytest.raises(NativeExecutionUnsupported, match='source_invalid'):
        subject.inspect_bounded_pe(**values)


@pytest.mark.parametrize('change', ['machine', 'magic', 'raw', 'overlap', 'truncated', 'delay_va', 'hidden_descriptor'])
def test_bad_layout_or_descriptor_rejects(tmp_path, change):
    raw = bytearray(resource_image(delayed=('module.pyd',)))
    if change == 'machine': struct.pack_into('<H', raw,132,0x14c)
    elif change == 'magic': struct.pack_into('<H', raw,152,0x10b)
    elif change == 'raw': struct.pack_into('<I', raw,412,len(raw))
    elif change == 'overlap': struct.pack_into('<II', raw,280,4096,40)  # resource aliases imports
    elif change == 'truncated': raw = raw[:400]
    elif change == 'delay_va': struct.pack_into('<I',raw,1024,0)
    elif change == 'hidden_descriptor': raw[1060] = 1
    with pytest.raises(NativeExecutionUnsupported): subject.inspect_bounded_pe(**fixed(tmp_path, raw))


def test_unknown_manifest_remains_unsupported(tmp_path):
    with pytest.raises(NativeExecutionUnsupported, match='resources_unsupported'):
        subject.inspect_bounded_pe(**fixed(tmp_path, manifest_image(manifest=b'<unknown/>')))


def export_image(forwarder='example.Symbol', count=1):
    raw = bytearray(resource_image())
    # table and forwarder both inside one bounded synthetic export directory
    struct.pack_into('<II',raw,264,4096+1536,512)
    struct.pack_into('<I',raw,2048+20,count)
    struct.pack_into('<I',raw,2048+28,4096+1600)
    struct.pack_into('<I',raw,2112,4096+1664)
    text = forwarder.encode()+b'\0'
    raw[2176:2176+len(text)] = text
    return bytes(raw)


def test_forwarder_dependency_and_pyd_suffix_are_explicit(tmp_path):
    result = subject.inspect_bounded_pe(**fixed(tmp_path, export_image('library.pyd.Symbol')))
    assert result.forwarders == ('library.pyd',)
    result = subject.inspect_bounded_pe(**fixed(tmp_path, export_image('library.#1')))
    assert result.forwarders == ('library.dll',)


@pytest.mark.parametrize('name', ['../library.Symbol', 'library.invalid/name', 'library..Symbol'])
def test_forwarder_paths_and_invalid_symbols_reject(tmp_path, name):
    with pytest.raises(NativeExecutionUnsupported):
        subject.inspect_bounded_pe(**fixed(tmp_path, export_image(name)))


def test_export_count_limit_is_bounded(tmp_path):
    with pytest.raises(NativeExecutionUnsupported, match='forwarders_unsupported'):
        subject.inspect_bounded_pe(**fixed(tmp_path, export_image(count=65537)))

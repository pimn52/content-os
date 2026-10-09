"""Explicit diagnostic envelope; legacy admission and delay tables unchanged."""
import hashlib
import struct
import pytest

from app.providers import pe_bounded as subject
from app.providers.runtime_primitives import NativeExecutionUnsupported
from test_pe_bounded import fixed
from test_pe_resources import resource_image


def envelope_image():
    raw=bytearray(resource_image())
    # Ordinary import descriptor + null followed by non-descriptor .idata data.
    struct.pack_into('<II',raw,272,4096,60)
    raw[552:572]=b'lookup/name payload!'
    return bytes(raw)


def admit_test_identity(monkeypatch,raw):
    monkeypatch.setattr(subject,'_IMPORT_ENVELOPE_SOURCE',(len(raw),hashlib.sha256(raw).hexdigest()))


def test_explicit_source_profile_reads_table_not_trailing_data(tmp_path,monkeypatch):
    raw=envelope_image();values=fixed(tmp_path,raw)
    with pytest.raises(NativeExecutionUnsupported,match='layout_unsupported'):
        with subject.open_bounded_pe(**values): pass
    admit_test_identity(monkeypatch,raw)
    with subject.open_import_envelope_pe(**values) as view:
        assert view.imports.ordinary==('kernel32.dll',)
        assert view.imports.delayed==() and view.dispatch_authorized is False
        assert view.read_bytes < subject.READ_LIMIT


def test_unknown_source_cannot_select_new_profile(tmp_path):
    with pytest.raises(NativeExecutionUnsupported,match='source_unknown'):
        with subject.open_import_envelope_pe(**fixed(tmp_path,envelope_image())):pass


@pytest.mark.parametrize('flag',[True, 'yes', 1])
def test_direct_view_cannot_bypass_exact_profile_identity(tmp_path,flag):
    values=fixed(tmp_path,envelope_image())
    source=subject._PEFile(values['path'],values['size_bytes'],values['sha256'])
    try:
        with pytest.raises(NativeExecutionUnsupported,match='layout_unsupported'):
            subject.BoundedPEView(source,_ordinary_envelope=flag)
    finally:
        source.close()


@pytest.mark.parametrize('problem',['terminator','name','delay_tail','size','section'])
def test_original_structural_bounds_still_reject(tmp_path,monkeypatch,problem):
    raw=bytearray(envelope_image())
    if problem=='terminator':raw[532]=1
    elif problem=='name':raw[2048:2060]=b'../evil.dll\0'
    elif problem=='delay_tail':
        raw=bytearray(resource_image(delayed=('module.pyd',)))
        raw[1060]=1
    elif problem=='size':struct.pack_into('<I',raw,276,19)
    elif problem=='section':struct.pack_into('<I',raw,272,0xfffffff0)
    raw=bytes(raw);admit_test_identity(monkeypatch,raw)
    with pytest.raises(NativeExecutionUnsupported):
        with subject.open_import_envelope_pe(**fixed(tmp_path,raw)):pass


def test_changed_source_closes_lifetime_without_evidence(tmp_path,monkeypatch):
    raw=envelope_image();admit_test_identity(monkeypatch,raw);values=fixed(tmp_path,raw)
    with pytest.raises(NativeExecutionUnsupported,match='source_changed'):
        with subject.open_import_envelope_pe(**values):
            values['path'].write_bytes(raw[:-1]+b'x')


def test_limits_and_closed_reads_remain(tmp_path,monkeypatch):
    raw=envelope_image();admit_test_identity(monkeypatch,raw)
    with subject.open_import_envelope_pe(**fixed(tmp_path,raw)) as view:
        monkeypatch.setattr(subject,'READ_LIMIT',view.read_bytes)
        with pytest.raises(NativeExecutionUnsupported,match='read_unsupported'):view.span(0,1)
    with pytest.raises(NativeExecutionUnsupported,match='consumed'):view.span(0,1)
    assert subject.IMPORT_LIMIT==512 and subject.SPAN_LIMIT==1024**2

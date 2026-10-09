"""PE resource data corruption tests; no native APIs or executable loads."""
import struct

import pytest

from app.providers.pe_data import inspect_resource_data
from app.providers.runtime_primitives import NativeExecutionUnsupported
from test_pe_resources import resource_image


def data_image(*, pe32=True, named=False):
    data = bytearray(resource_image([(16, 1, 1033, b'data', 0)], ordinary=()))
    struct.pack_into('<I', data, 152 + 56, 8192)
    struct.pack_into('<I', data, 392 + 36, 0x40000040)
    if pe32:
        directories = bytes(data[264:392])
        data[248:376] = directories
        data[376:392] = bytes(16)
        struct.pack_into('<H', data, 132, 0x14c)
        struct.pack_into('<H', data, 152, 0x10b)
        struct.pack_into('<I', data, 244, 16)
    if named:
        entry = 248 if pe32 else 264
        _, size = struct.unpack_from('<II', data, entry + 16)
        offset = (size + 1) & ~1
        encoded = 'MUI'.encode('utf-16-le')
        data[2800+offset:2800+offset+8] = struct.pack('<H', 3) + encoded
        struct.pack_into('<HH', data, 2800+12, 1, 0)
        struct.pack_into('<I', data, 2800+16, 0x80000000 | offset)
        struct.pack_into('<I', data, entry+20, offset+8)
    return bytes(data)


@pytest.mark.parametrize('pe32', [True, False])
@pytest.mark.parametrize('named', [True, False])
def test_data_container_has_no_executable_authority(pe32, named):
    evidence = inspect_resource_data(data_image(pe32=pe32, named=named))
    assert evidence.machine == ('0x14c' if pe32 else '0x8664')
    assert evidence.leaf_count == 1


@pytest.mark.parametrize('problem', ['entry', 'code', 'execute', 'code_section', 'machine',
    'import', 'delay', 'bound', 'iat', 'tls', 'clr', 'export', 'load_config', 'manifest',
    'cycle', 'overlap', 'bounds', 'name_bounds', 'hidden_named', 'leaf_directory',
    'half_directory', 'truncated', 'section_bounds', 'debug_bounds', 'certificate_overlap'])
def test_resource_data_never_hides_execution_or_ambiguous_structure(problem):
    data = bytearray(data_image(named=problem in ('name_bounds', 'hidden_named')))
    directories = dict(export=0, import_=1, tls=9, load_config=10, bound=11, iat=12, delay=13, clr=14)
    if problem == 'import': problem = 'import_'
    if problem in directories: struct.pack_into('<II', data, 248+8*directories[problem], 4096, 20)
    if problem == 'entry': struct.pack_into('<I', data, 168, 4096)
    if problem == 'code': struct.pack_into('<I', data, 156, 1)
    if problem in ('execute', 'code_section'):
        struct.pack_into('<I', data, 428, 0x40000040 | (0x20000000 if problem == 'execute' else 0x20))
    if problem == 'machine': struct.pack_into('<H', data, 132, 0x8664)
    if problem == 'manifest': struct.pack_into('<I', data, 2816, 24)
    if problem == 'cycle': struct.pack_into('<I', data, 2820, 0x80000000)
    if problem == 'overlap': struct.pack_into('<I', data, 2872, 6384)
    if problem == 'bounds': struct.pack_into('<I', data, 2820, 0x80000000 | 65536)
    if problem == 'name_bounds': struct.pack_into('<I', data, 2816, 0x800ffff0)
    if problem == 'hidden_named': struct.pack_into('<HH', data, 2812, 0, 1)
    if problem == 'leaf_directory': struct.pack_into('<I', data, 2868, 0x80000000 | 72)
    if problem == 'half_directory': struct.pack_into('<II', data, 264, 6384, 0)
    if problem == 'truncated': data = data[:700]
    if problem == 'section_bounds': struct.pack_into('<I', data, 404, 0xffffffff)
    if problem == 'debug_bounds': struct.pack_into('<II', data, 296, 4096, 27)
    if problem == 'certificate_overlap': struct.pack_into('<II', data, 280, 512, 8)
    with pytest.raises(NativeExecutionUnsupported, match='resource_data_unsupported'):
        inspect_resource_data(bytes(data))

from array import array
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import wave

import pytest

from app.providers import omnivoice_audio_pcm as pcm


@pytest.fixture
def upstream():
    path = Path(__file__).resolve().parents[3] / 'content-os-data/omnivoice-runtime/Lib/site-packages/omnivoice/utils/audio.py'
    if not path.is_file():
        pytest.skip('retained fixed source unavailable; no acquisition')
    return path.read_bytes()


def test_exact_source_derivation_locality_and_guards(upstream):
    result = pcm.derive_pcm_audio(upstream)
    changed = ast.parse(result.payload)
    assert not result.dispatch_authorized
    rebuilt = result.payload.replace(pcm.GUARD, b'')
    lines = rebuilt.splitlines(keepends=True)
    lines.insert(30, b'import soundfile as sf\n')
    assert b''.join(lines) == upstream
    functions = {n.name: n for n in changed.body if isinstance(n, ast.FunctionDef)}
    for name in pcm.SITES:
        assert isinstance(functions[name].body[1], ast.Raise)
        assert pcm.DECODE_STOP in ast.unparse(functions[name].body[1])
    record = json.loads(result.descriptor)
    assert record['derived_sha256'] == hashlib.sha256(result.payload).hexdigest()
    assert record['upstream_sha256'] == pcm.UPSTREAM_SHA256
    assert record['terms_review'] == 'not_verified'
    assert record['dispatch_authorized'] is False
    assert pcm.derive_pcm_audio(upstream) == result
    with pytest.raises(pcm.PCMContractError):
        pcm.derive_pcm_audio(result.payload)


@pytest.mark.parametrize('bad', [b'', b'changed', bytearray(b'bytes')])
def test_changed_source_rejected(bad):
    with pytest.raises(pcm.PCMContractError, match='source_derivation_invalid'):
        pcm.derive_pcm_audio(bad)


def test_pinned_span_change_rejected(upstream, monkeypatch):
    monkeypatch.setattr(pcm, 'SITES', pcm.SITES | {'load_audio': (69, 88, 79, '0'*64)})
    with pytest.raises(pcm.PCMContractError):
        pcm.derive_pcm_audio(upstream)


def test_implementation_identity_participates(upstream, monkeypatch):
    before = pcm.derive_pcm_audio(upstream)
    monkeypatch.setattr(pcm, 'implementation_identity', lambda: 'a'*64)
    after = pcm.derive_pcm_audio(upstream)
    assert before.payload == after.payload
    assert before.identity_sha256 != after.identity_sha256


def write(path, samples, **kwargs):
    return pcm.write_pcm16_wav(path, samples, 24000, max_frames=100, max_bytes=1000, **kwargs)


def test_pcm_samples_and_wave_header(tmp_path):
    path = (tmp_path/'out.wav').resolve()
    samples = array('f', [-2, -1, -.5, -3/65536, -1/65536, 0, 1/65536, 3/65536, .5, 1, 2])
    result = write(path, samples)
    raw = path.read_bytes()
    assert result.frames == 11 and result.size_bytes == 66
    assert result.sha256 == hashlib.sha256(raw).hexdigest()
    with wave.open(io.BytesIO(raw), 'rb') as media:
        assert (media.getnchannels(), media.getsampwidth(), media.getframerate(), media.getnframes()) == (1, 2, 24000, 11)
        assert struct.unpack('<11h', media.readframes(11)) == (-32768, -32768, -16384, -2, 0, 0, 0, 2, 16384, 32767, 32767)


@pytest.mark.parametrize('samples', [array('f'), array('f', [float('nan')]), array('f', [float('inf')]),
                                  array('d', [.1]), [.1], [[.1]]])
def test_invalid_samples_stop_before_create(tmp_path, samples):
    path=(tmp_path/'out.wav').resolve()
    with pytest.raises(pcm.PCMContractError, match='output_invalid'):
        write(path, samples)
    assert not path.exists()


@pytest.mark.parametrize('kwargs', [dict(sampling_rate=True), dict(sampling_rate=0), dict(sampling_rate=192001),
    dict(max_frames=True), dict(max_frames=0), dict(max_frames=pcm.MAX_FRAMES+1),
    dict(max_bytes=44), dict(max_bytes=pcm.MAX_BYTES+1), dict(max_bytes=47)])
def test_limit_validation(tmp_path, kwargs):
    params=dict(sampling_rate=24000,max_frames=2,max_bytes=48) | kwargs
    with pytest.raises(pcm.PCMContractError):
        pcm.write_pcm16_wav((tmp_path/'out.wav').resolve(),array('f',[.1,.2]),**params)
    assert not (tmp_path/'out.wav').exists()


def test_exact_limits_and_existing_file(tmp_path):
    path=(tmp_path/'out.wav').resolve()
    pcm.write_pcm16_wav(path,array('f',[.1,.2]),24000,max_frames=2,max_bytes=48)
    before=path.read_bytes()
    with pytest.raises(pcm.PCMContractError, match='conflict'):
        write(path,array('f',[.3]))
    assert path.read_bytes() == before


def test_snapshot_before_creation(tmp_path, monkeypatch):
    path=(tmp_path/'out.wav').resolve(); samples=array('f',[.5])
    opened=Path.open
    def changing_open(self,*args,**kwargs):
        if self == path and args == ('xb',):
            samples[0]=float('nan')
        return opened(self,*args,**kwargs)
    monkeypatch.setattr(Path,'open',changing_open)
    write(path,samples)
    assert struct.unpack('<h',path.read_bytes()[44:]) == (16384,)


def test_short_writes_and_nonprogress():
    class Short(io.BytesIO):
        def write(self,payload): return super().write(payload[:2])
    output=Short(); pcm._write_all(output,b'abcdef')
    assert output.getvalue()==b'abcdef'
    class Broken:
        def write(self,payload): return 0
    with pytest.raises(OSError): pcm._write_all(Broken(),b'data')


def test_partial_write_cleanup(tmp_path,monkeypatch):
    path=(tmp_path/'out.wav').resolve()
    def broken(stream,payload):
        stream.write(b'partial'); raise OSError()
    monkeypatch.setattr(pcm,'_write_all',broken)
    with pytest.raises(pcm.PCMContractError,match='write_failed'):write(path,array('f',[.5]))
    assert not path.exists()


def test_replaced_partial_preserved(tmp_path):
    path=(tmp_path/'out.wav').resolve(); displaced=tmp_path/'owned-partial'
    with path.open('xb') as handle:
        created=os.fstat(handle.fileno())
        identity=(created.st_dev,created.st_ino)
    path.rename(displaced);path.write_bytes(b'replacement')
    pcm._remove_owned_partial(path,identity)
    assert path.read_bytes()==b'replacement'


def test_racing_creation_preserved(tmp_path,monkeypatch):
    path=(tmp_path/'out.wav').resolve(); opened=Path.open
    def racing(self,*args,**kwargs):
        if self==path and args==('xb',):
            with opened(self,'xb') as handle:handle.write(b'concurrent')
        return opened(self,*args,**kwargs)
    monkeypatch.setattr(Path,'open',racing)
    with pytest.raises(pcm.PCMContractError,match='conflict'):write(path,array('f',[.5]))
    assert path.read_bytes()==b'concurrent'


def test_replacement_after_close_stops_and_preserves_it(tmp_path,monkeypatch):
    path=(tmp_path/'out.wav').resolve(); opened=Path.open
    class Replacing:
        def __init__(self,handle):self.handle=handle
        def __enter__(self):return self.handle
        def __exit__(self,*args):
            self.handle.close()
            path.rename(tmp_path/'completed-owned')
            with opened(path,'xb') as replacement:replacement.write(b'replacement')
    def opening(self,*args,**kwargs):
        handle=opened(self,*args,**kwargs)
        return Replacing(handle) if self==path and args==('xb',) else handle
    monkeypatch.setattr(Path,'open',opening)
    with pytest.raises(pcm.PCMContractError,match='write_failed'):write(path,array('f',[.5]))
    assert path.read_bytes()==b'replacement'

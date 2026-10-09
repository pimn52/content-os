"""Source-bound PCM-only helper and bounded WAV writer; no vendor imports."""
from array import array
import ast
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import struct
import sys

UPSTREAM_SHA256 = '6e2154cb8936138ee6c417c253dc9b222500ed322e6fbb2c0377a0b6850b8b48'
UPSTREAM_SIZE = 10285
TRANSFORM = 'omnivoice-audio-pcm-only-v1'
CONVERSION = 'pcm16-le-round-even-v1'
DECODE_STOP = 'omnivoice_pcm_file_decode_unsupported'
MAX_BYTES = 64 * 1024**2
MAX_FRAMES = (MAX_BYTES - 44) // 2
GUARD = b'    raise RuntimeError("omnivoice_pcm_file_decode_unsupported")\n'
SITES = {
    'load_waveform': (45, 66, 56, 'e019e554586c9c9d3d97f493e8cdc2157b5564f3fedd6a5ffa4a1e30af5c7176'),
    'load_audio': (69, 88, 79, '301246eaac622a52dde0c35b9ca04d35b70988a9d49d8e254dee764f2096adfa'),
    'load_audio_bytes': (91, 121, 101, 'b920361d5fb791052822808c2a2014d55b12c62ac3ef1309d2096ac0888658f2'),
}


class PCMContractError(ValueError):
    pass


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def implementation_identity():
    path = Path(__file__)
    if path != path.resolve(strict=True):
        raise PCMContractError('omnivoice_pcm_implementation_path_invalid')
    with path.open('rb') as source:
        raw = source.read(2 * 1024**2 + 1)
    if not 0 < len(raw) <= 2 * 1024**2:
        raise PCMContractError('omnivoice_pcm_implementation_size_invalid')
    return _sha(raw)


@dataclass(frozen=True)
class PCMAudioDerivation:
    payload: bytes
    descriptor: bytes

    @property
    def identity_sha256(self):
        return _sha(self.descriptor)

    @property
    def dispatch_authorized(self):
        return False


def derive_pcm_audio(upstream):
    """Exactly four byte edits, preserving all other source and notices."""
    try:
        if type(upstream) is not bytes or len(upstream) != UPSTREAM_SIZE or _sha(upstream) != UPSTREAM_SHA256:
            raise ValueError()
        tree = ast.parse(upstream)
        lines = upstream.splitlines(keepends=True)
        imports = [n for n in tree.body if isinstance(n, ast.Import)
                   and any(a.name == 'soundfile' for a in n.names)]
        if len(imports) != 1 or imports[0].lineno != 31 or lines[30] != b'import soundfile as sf\n':
            raise ValueError()
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        edits = [(offsets[30], offsets[31], b'')]
        originals = {}
        for name, (start, end, first, digest) in SITES.items():
            nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
            if len(nodes) != 1:
                raise ValueError()
            node = nodes[0]
            if ((node.lineno, node.end_lineno, node.body[1].lineno) != (start, end, first)
                    or ast.get_docstring(node) is None
                    or _sha(upstream[offsets[start-1]:offsets[end]]) != digest):
                raise ValueError()
            originals[name] = node
            edits.append((offsets[first-1], offsets[first-1], GUARD))
        derived = upstream
        for start, end, replacement in sorted(edits, reverse=True):
            derived = derived[:start] + replacement + derived[end:]
        parsed = ast.parse(derived)
        guarded_ids = set()
        for name, old in originals.items():
            node = next(n for n in parsed.body if isinstance(n, ast.FunctionDef) and n.name == name)
            guard = node.body[1]
            if (ast.dump(guard) != ast.dump(ast.parse(GUARD.strip()).body[0])
                    or ast.dump(node.args) != ast.dump(old.args)
                    or ast.get_docstring(node) != ast.get_docstring(old)):
                raise ValueError()
            guarded_ids.update(id(n) for n in ast.walk(node))
        for node in ast.walk(parsed):
            backend = (isinstance(node, ast.Name) and node.id in ('sf', 'librosa')
                       or isinstance(node, ast.Import) and any(a.name in ('soundfile', 'librosa') for a in node.names))
            if backend and id(node) not in guarded_ids:
                raise ValueError()
        implementation = implementation_identity()
        transform_bytes = json.dumps([TRANSFORM, SITES, GUARD.decode(), 'remove:import soundfile as sf'],
                                     sort_keys=True, separators=(',', ':')).encode()
        record = dict(descriptor_version=1, recipe=TRANSFORM, upstream_size=UPSTREAM_SIZE,
            upstream_sha256=UPSTREAM_SHA256, transform_sha256=_sha(transform_bytes),
            derived_sha256=_sha(derived), implementation_sha256=implementation,
            writer_sha256=implementation, conversion=CONVERSION,
            notices_source_sha256=UPSTREAM_SHA256, terms_references=[], terms_review='not_verified',
            dispatch_authorized=False)
        return PCMAudioDerivation(derived, (json.dumps(record, sort_keys=True, separators=(',', ':'))+'\n').encode())
    except (ValueError, TypeError, AttributeError, IndexError, StopIteration, SyntaxError, OSError):
        raise PCMContractError('omnivoice_pcm_source_derivation_invalid') from None


@dataclass(frozen=True)
class PCMWriteResult:
    frames: int
    sampling_rate: int
    size_bytes: int
    sha256: str


def _write_all(stream, payload):
    view = memoryview(payload)
    while view:
        count = stream.write(view)
        if type(count) is not int or not 0 < count <= len(view):
            raise OSError('pcm_short_write')
        view = view[count:]


def _remove_owned_partial(path, identity):
    if identity is None or not identity[1]:
        return
    try:
        current = path.lstat()
        if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity:
            path.unlink()
    except FileNotFoundError:
        pass


def write_pcm16_wav(path, samples, sampling_rate, *, max_frames, max_bytes):
    """Detach/validate samples before exclusive creation; never overwrites."""
    if (type(samples) is not array or samples.typecode != 'f' or samples.itemsize != 4
            or type(sampling_rate) is not int or not 1 <= sampling_rate <= 192000
            or type(max_frames) is not int or not 1 <= max_frames <= MAX_FRAMES
            or type(max_bytes) is not int or not 44 < max_bytes <= MAX_BYTES
            or not 0 < len(samples) <= max_frames or 44 + 2*len(samples) > max_bytes):
        raise PCMContractError('omnivoice_pcm_output_invalid')
    snapshot = array('f', samples)
    if not all(math.isfinite(value) for value in snapshot):
        raise PCMContractError('omnivoice_pcm_output_invalid')
    target = Path(path)
    if not target.is_absolute() or target.parent != target.parent.resolve(strict=True):
        raise PCMContractError('omnivoice_pcm_output_path_invalid')
    frames = len(snapshot)
    header = struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', 36+2*frames, b'WAVE', b'fmt ',
                         16, 1, 1, sampling_rate, sampling_rate*2, 2, 16, b'data', 2*frames)
    digest = hashlib.sha256(header)
    identity = None
    try:
        stream = target.open('xb')
    except FileExistsError:
        raise PCMContractError('omnivoice_pcm_output_conflict') from None
    except OSError:
        raise PCMContractError('omnivoice_pcm_output_write_failed') from None
    try:
        with stream as output:
            created = os.fstat(output.fileno())
            identity = (created.st_dev, created.st_ino)
            _write_all(output, header)
            for start in range(0, frames, 4096):
                encoded = array('h', (max(-32768, min(32767, round(max(-1., min(1., value))*32768)))
                                      for value in snapshot[start:start+4096]))
                if sys.byteorder != 'little':
                    encoded.byteswap()
                payload = encoded.tobytes()
                _write_all(output, payload)
                digest.update(payload)
            output.flush()
            current = target.lstat()
            if ((current.st_dev, current.st_ino) != identity or current.st_size != 44+2*frames
                    or not stat.S_ISREG(current.st_mode)):
                raise ValueError('output_changed')
        current = target.lstat()
        if (current.st_dev, current.st_ino) != identity or current.st_size != 44+2*frames:
            raise ValueError('output_changed')
        return PCMWriteResult(frames, sampling_rate, 44+2*frames, digest.hexdigest())
    except BaseException as error:
        try:
            _remove_owned_partial(target, identity)
        except OSError:
            raise PCMContractError('omnivoice_pcm_output_cleanup_failed') from None
        if not isinstance(error, Exception):
            raise
        raise PCMContractError('omnivoice_pcm_output_write_failed') from None

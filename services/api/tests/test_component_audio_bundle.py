"""The opt-in owned audio handle helper adds only the frozen stdlib core."""
import hashlib
from pathlib import Path

from app.providers import component_probe
from app.domain.execution_runtime import RuntimeInventoryFile
from app.providers.runtime_tree import _with_owned


def test_audio_helper_extends_native_helper_without_changing_existing_bundles():
    original_component = component_probe.owned_component_probe_files()
    original_native = component_probe.owned_native_load_probe_files()

    audio = component_probe.owned_soundfile_handle_probe_files()

    assert component_probe.owned_component_probe_files() == original_component
    assert component_probe.owned_native_load_probe_files() == original_native
    assert audio[:-1] == original_native
    core = audio[-1]
    assert core.destination == 'Lib/site-packages/_content_os_host/soundfile_handle.py'
    assert core.role == 'dependency'
    source = Path(component_probe.__file__).with_name('soundfile_handle.py')
    assert core.payload == source.read_bytes()
    assert len(core.payload) <= 1024 * 1024

    base = [
        RuntimeInventoryFile(name='python.exe', role='interpreter', size_bytes=1,
                             sha256=hashlib.sha256(b'i').hexdigest()),
        RuntimeInventoryFile(name='Scripts/worker.py', role='entrypoint', size_bytes=1,
                             sha256=hashlib.sha256(b'e').hexdigest()),
        RuntimeInventoryFile(name='python312.dll', role='dependency', size_bytes=1,
                             sha256=hashlib.sha256(b'd').hexdigest()),
    ]
    inventory = _with_owned({'Scripts'}, base, audio, 64 * 1024**2,
                            owned_recipe='native-load-probe-v5')
    entry = next(row for row in inventory.files if row.name == core.destination)
    assert (entry.size_bytes, entry.sha256) == (
        len(core.payload), hashlib.sha256(core.payload).hexdigest())

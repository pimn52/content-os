"""Generate tiny, silent synthetic UI fixtures; never creator-quality evidence."""
from pathlib import Path
import hashlib
import subprocess
import tempfile
import wave
import struct
import zlib

web = Path(__file__).resolve().parents[1]
ffmpeg = web.parent / "renderer/node_modules/@remotion/compositor-win32-x64-msvc/ffmpeg.exe"
target = web / "tests/media"
target.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory() as temp:
    audio = Path(temp) / "silence.wav"
    with wave.open(str(audio), "wb") as out:
        out.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        out.writeframes(bytes(48000))
    for name, color in (("before", (16, 36, 61)), ("after", (21, 59, 52))):
        path = target / f"recovery-{name}.mp4"
        still = Path(temp) / f"{name}.png"
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        still.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 108, 192, 8, 2, 0, 0, 0))
                          + chunk(b"IDAT", zlib.compress((b"\0" + bytes(color) * 108) * 192)) + chunk(b"IEND", b""))
        subprocess.run([str(ffmpeg), "-v", "error", "-y", "-loop", "1", "-framerate", "30",
                        "-i", str(still), "-i", str(audio), "-t", "1",
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(path)],
                       check=True)
        print(name, hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size)

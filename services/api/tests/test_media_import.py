import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from threading import Barrier, Event

import pytest

from app.db import AssetRepository, Database, apply_migrations
from app.domain.models import RationalFps
from app.media import FFProbeAdapter, MediaImporter, ProbeMetadata
from app.media.ffprobe import ProbeInvalid, ProbeMalformed, ProbeNoVideo


def _fake_probe(tmp_path: Path, document: dict) -> FFProbeAdapter:
    script = tmp_path / "fake ffprobe.py"
    script.write_text(
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(tmp_path / 'argv.json')!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"print(json.dumps({document!r}))\n",
        encoding="utf-8",
    )
    return FFProbeAdapter((sys.executable, str(script)))


def _document(duration="1.001"):
    return {"format": {"duration": duration, "format_name": "mov,mp4"}, "streams": [{"codec_type": "video", "width": 640, "height": 360, "r_frame_rate": "30000/1001"}, {"codec_type": "audio", "sample_rate": "48000"}]}


class _StaticProbe:
    def probe(self, path: Path) -> ProbeMetadata:
        return ProbeMetadata(
            duration_ms=1001,
            width=640,
            height=360,
            fps=RationalFps(numerator=30, denominator=1),
            has_audio=True,
            metadata={"format": {"duration": "1.001"}, "streams": [{"codec_type": "video", "duration": "1.001"}]},
        )


def test_probe_uses_argv_and_parses_exact_metadata(tmp_path: Path):
    adapter = _fake_probe(tmp_path, _document())
    result = adapter.probe(tmp_path / "clip with 中文.mp4")
    assert result.duration_ms == 1001
    assert (result.width, result.height, result.fps.numerator, result.fps.denominator, result.has_audio) == (640, 360, 30000, 1001, True)
    argv = json.loads((tmp_path / "argv.json").read_text(encoding="utf-8"))
    assert argv[-1].endswith("clip with 中文.mp4")


@pytest.mark.parametrize(
    ("avg_frame_rate", "r_frame_rate", "expected"),
    [
        ("30000/1001", "0/0", (30000, 1001)),
        ("not-a-rate", "25/1", (25, 1)),
    ],
)
def test_probe_uses_first_valid_fps_candidate(tmp_path: Path, avg_frame_rate: str, r_frame_rate: str, expected: tuple[int, int]):
    document = _document()
    video = document["streams"][0]
    video["avg_frame_rate"] = avg_frame_rate
    video["r_frame_rate"] = r_frame_rate
    result = _fake_probe(tmp_path, document).probe(tmp_path / "fps.mp4")
    assert (result.fps.numerator, result.fps.denominator) == expected


@pytest.mark.parametrize(
    ("format_info", "video_values", "expected_ms"),
    [
        ({"duration": "not-a-duration"}, {"duration": "1.0001"}, 1001),
        ({}, {"duration_ts": "1001", "time_base": "1/1000"}, 1001),
    ],
)
def test_probe_duration_falls_back_to_valid_stream_timing(
    tmp_path: Path, format_info: dict, video_values: dict, expected_ms: int,
):
    document = _document()
    document["format"] = format_info
    document["streams"][0].update(video_values)
    result = _fake_probe(tmp_path, document).probe(tmp_path / "duration.mp4")
    assert result.duration_ms == expected_ms


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_ffprobe_timeout_must_be_finite_and_positive(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        FFProbeAdapter(timeout_seconds=timeout)


@pytest.mark.parametrize("chunk_size", [0, -1, True, 64 * 1024 * 1024 + 1])
def test_import_chunk_size_must_be_a_bounded_positive_integer(tmp_path: Path, chunk_size: int) -> None:
    db = Database(tmp_path / "store.sqlite")
    try:
        with pytest.raises(ValueError, match="chunk_size"):
            MediaImporter(db, tmp_path / "data", _StaticProbe(), chunk_size=chunk_size)
    finally:
        db.close()


@pytest.mark.parametrize("document, error", [
    ({"format": {}, "streams": []}, ProbeNoVideo),
    ({"format": {"duration": "nan"}, "streams": [{"codec_type": "video", "width": 1, "height": 1, "r_frame_rate": "1/1"}]}, ProbeInvalid),
    ({"format": {"duration": "1"}, "streams": [{"codec_type": "video", "width": 0, "height": 1, "r_frame_rate": "1/1"}]}, ProbeInvalid),
])
def test_probe_errors_are_typed(tmp_path: Path, document, error):
    with pytest.raises(error):
        _fake_probe(tmp_path, document).probe(tmp_path / "x.mp4")


def test_import_path_and_stream_dedupe_and_cleanup(tmp_path: Path):
    source = tmp_path / "原始 footage with spaces.mp4"
    source.write_bytes(b"same bytes")
    db = Database(tmp_path / "store.sqlite")
    importer = MediaImporter(db, tmp_path / "data root", _fake_probe(tmp_path, _document()))
    first = importer.import_path(source, "rights-1")
    second = importer.import_stream(BytesIO(source.read_bytes()), "../../evil.mp4", "rights-2")
    assert first.id == second.id
    assert first.source_file == second.source_file
    assert Path(first.source_file).parent == (tmp_path / "data root" / "assets" / "originals")
    assert len(list((tmp_path / "data root" / "assets" / "originals").glob("*"))) == 1
    assert source.read_bytes() == b"same bytes"
    assert len(AssetRepository(db).list()) == 1
    db.close()


def test_concurrent_duplicate_import_retains_one_asset_and_one_original(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    data_root = tmp_path / "data"
    workers = 2
    barrier = Barrier(workers)
    Database(path).close()

    def import_contender(filename: str) -> str:
        db = Database(path)
        try:
            importer = MediaImporter(db, data_root, _StaticProbe())
            barrier.wait()
            return str(importer.import_stream(BytesIO(b"identical media bytes"), filename, "rights").id)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        asset_ids = list(pool.map(import_contender, ("one.mp4", "two.mov")))

    db = Database(path)
    try:
        assert len(set(asset_ids)) == 1
        assert len(AssetRepository(db).list()) == 1
        originals = list((data_root / "assets" / "originals").iterdir())
        assert len(originals) == 1
        assert originals[0].read_bytes() == b"identical media bytes"
    finally:
        db.close()


def test_failing_contender_cannot_remove_successful_contenders_final_file(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    data_root = tmp_path / "data"
    start = Barrier(2)
    failing_probe_started = Event()
    successful_probe_started = Event()
    failure_finished = Event()

    class FailingProbe:
        def probe(self, path: Path) -> ProbeMetadata:
            failing_probe_started.set()
            assert successful_probe_started.wait(timeout=5)
            raise ProbeMalformed("intentional concurrent failure")

    class SuccessfulProbe:
        def probe(self, path: Path) -> ProbeMetadata:
            assert failing_probe_started.wait(timeout=5)
            successful_probe_started.set()
            assert failure_finished.wait(timeout=5)
            return _StaticProbe().probe(path)

    Database(path).close()

    def fail_import() -> None:
        db = Database(path)
        try:
            start.wait()
            with pytest.raises(ProbeMalformed, match="intentional concurrent failure"):
                MediaImporter(db, data_root, FailingProbe()).import_stream(BytesIO(b"same bytes"), "failed.mp4", "rights")
        finally:
            failure_finished.set()
            db.close()

    def successful_import() -> str:
        db = Database(path)
        try:
            start.wait()
            return str(MediaImporter(db, data_root, SuccessfulProbe()).import_stream(BytesIO(b"same bytes"), "winner.mov", "rights").id)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        failure = pool.submit(fail_import)
        success = pool.submit(successful_import)
        failure.result()
        winner_id = success.result()

    db = Database(path)
    try:
        winner = AssetRepository(db).get_by_content_hash(hashlib.sha256(b"same bytes").hexdigest())
        assert winner is not None and str(winner.id) == winner_id
        assert Path(winner.source_file).is_file()
        assert Path(winner.source_file).read_bytes() == b"same bytes"
    finally:
        db.close()


def test_import_failure_removes_new_file(tmp_path: Path):
    class FailingProbe:
        def probe(self, path):
            raise ProbeMalformed("bad")

    db = Database(tmp_path / "store.sqlite")
    importer = MediaImporter(db, tmp_path / "data", FailingProbe())
    with pytest.raises(ProbeMalformed):
        importer.import_stream(BytesIO(b"bytes"), "clip.mp4", "rights")
    assert list((tmp_path / "data" / "assets" / "originals").iterdir()) == []
    db.close()


def test_import_cleanup_preserves_primary_error_when_lookup_is_broken(tmp_path: Path):
    class CloseDatabaseThenFail:
        def probe(self, path: Path) -> ProbeMetadata:
            db.close()
            raise ProbeMalformed("primary probe failure")

    db = Database(tmp_path / "store.sqlite")
    importer = MediaImporter(db, tmp_path / "data", CloseDatabaseThenFail())
    with pytest.raises(ProbeMalformed, match="primary probe failure"):
        importer.import_stream(BytesIO(b"bytes"), "clip.mp4", "rights")
    assert list((tmp_path / "data" / "assets" / "originals").iterdir()) == []


def test_migration_from_v1_backfills_asset_hash_and_unique_index(tmp_path: Path):
    path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP);"
        "INSERT INTO schema_migrations(version) VALUES (1);"
        "CREATE TABLE assets(id TEXT PRIMARY KEY, duration_ms INTEGER NOT NULL, payload TEXT NOT NULL);"
    )
    payload = json.dumps({"content_hash": "b" * 64})
    connection.execute("INSERT INTO assets(id, duration_ms, payload) VALUES (?, ?, ?)", ("asset-1", 1, payload))
    connection.commit()
    apply_migrations(connection)
    assert connection.execute("SELECT content_hash FROM assets WHERE id = 'asset-1'").fetchone()[0] == "b" * 64
    assert [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")] == [1, 2, 3, 4]
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO assets(id, duration_ms, content_hash, payload) VALUES ('asset-2', 1, ?, ?)", ("b" * 64, payload))
    with pytest.raises(sqlite3.IntegrityError, match="content_hash is required"):
        connection.execute("INSERT INTO assets(id, duration_ms, content_hash, payload) VALUES ('asset-3', 1, NULL, ?)", (payload,))
    connection.close()


@pytest.mark.parametrize(
    "payloads",
    [
        [json.dumps({"content_hash": "c" * 64}), json.dumps({"content_hash": "c" * 64})],
        [json.dumps({"other": "missing hash"})],
        ["{malformed json"],
    ],
)
def test_v1_hash_migration_failure_rolls_back_atomically(tmp_path: Path, payloads: list[str]):
    connection = sqlite3.connect(tmp_path / "legacy.sqlite")
    connection.executescript(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP);"
        "INSERT INTO schema_migrations(version) VALUES (1);"
        "CREATE TABLE assets(id TEXT PRIMARY KEY, duration_ms INTEGER NOT NULL, payload TEXT NOT NULL);"
    )
    for index, payload in enumerate(payloads):
        connection.execute("INSERT INTO assets(id, duration_ms, payload) VALUES (?, 1, ?)", (f"asset-{index}", payload))
    connection.commit()

    with pytest.raises(sqlite3.DatabaseError):
        apply_migrations(connection)
    assert [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")] == [1]
    assert "content_hash" not in [row[1] for row in connection.execute("PRAGMA table_info(assets)")]
    connection.close()


def test_real_ffmpeg_import(tmp_path: Path):
    ffmpeg = os.environ.get("CONTENT_OS_FFMPEG") or shutil.which("ffmpeg")
    ffprobe = os.environ.get("CONTENT_OS_FFPROBE") or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("real ffmpeg/ffprobe binaries are unavailable")
    source = tmp_path / "generated source.mp4"
    subprocess.run([ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25", "-f", "lavfi", "-i", "sine=frequency=1000", "-t", "0.8", "-c:v", "mpeg4", "-c:a", "aac", str(source)], check=True, capture_output=True)
    db = Database(tmp_path / "store.sqlite")
    importer = MediaImporter(db, tmp_path / "data", FFProbeAdapter(ffprobe))
    asset = importer.import_path(source, "fixture-rights")
    retained = Path(asset.source_file)
    assert source.is_file()
    assert retained.is_file()
    assert list((tmp_path / "data" / "assets" / "originals").iterdir()) == [retained]
    assert 790 <= asset.duration_ms <= 900
    assert (asset.width, asset.height, asset.fps.numerator, asset.fps.denominator, asset.has_audio) == (320, 240, 25, 1, True)
    streams = asset.metadata["streams"]
    video = next(stream for stream in streams if stream["codec_type"] == "video")
    audio = next(stream for stream in streams if stream["codec_type"] == "audio")
    assert _has_usable_timing(video)
    assert _has_usable_timing(audio)
    db.close()


def _has_usable_timing(stream: dict) -> bool:
    try:
        duration = Decimal(str(stream.get("duration")))
        if duration.is_finite() and duration > 0:
            return True
    except (InvalidOperation, ValueError):
        pass
    try:
        duration_ts = Decimal(str(stream.get("duration_ts")))
        numerator, denominator = str(stream.get("time_base")).split("/", 1)
        time_base = Decimal(numerator) / Decimal(denominator)
        return duration_ts.is_finite() and time_base.is_finite() and duration_ts > 0 and time_base > 0
    except (InvalidOperation, ValueError, ZeroDivisionError):
        return False

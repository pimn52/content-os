"""Real constructor contract, no media probe/model invocation or fake adapter."""
from app.db import Database
from app.evaluation_talking_execution import _output_importer
from app.media.ffprobe import FFProbeAdapter


def test_owned_output_importer_accepts_runtime_tool_path(tmp_path):
    with Database(tmp_path / "importer.sqlite") as db:
        importer = _output_importer(db, tmp_path, tmp_path / "tools")
        assert type(importer.probe) is FFProbeAdapter
        assert importer.probe.command == (str(tmp_path / "tools" / "ffprobe.exe"),)

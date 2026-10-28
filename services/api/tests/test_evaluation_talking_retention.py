from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.evaluation_talking import EvaluationTalkingService
from app.execution_admission import UseAdmissionError


def test_internal_output_retention_pins_scope_in_the_same_database():
    events = []

    class Connection:
        def execute(self, sql, values):
            events.append(("insert", sql, values))

    db = SimpleNamespace(connection=Connection())
    asset = SimpleNamespace(
        id=uuid4(), duration_ms=1000, content_hash="a" * 64,
        model_dump_json=lambda: '{"internal":true}',
    )

    class Prepared:
        _importer = SimpleNamespace(db=db)

        def _claim_retention(self, metadata):
            events.append(("claim", metadata))
            return asset

        def _place_retained(self, value):
            events.append(("place", value))
            return value

    service = object.__new__(EvaluationTalkingService)
    service.db = db
    service._pin_output = lambda operation, job, context, value: events.append(("pin", value))
    prepared = Prepared()
    result = service.retain_output(prepared, object(), object(), {}, {"source": "verified"})

    assert result is asset
    assert [event[0] for event in events] == ["claim", "pin", "insert", "place"]
    assert events[2][2] == (str(asset.id), 1000, "a" * 64, '{"internal":true}')


def test_internal_output_retention_rejects_a_different_database():
    service = object.__new__(EvaluationTalkingService)
    service.db = object()
    with pytest.raises(UseAdmissionError, match="evaluation_talking_output_importer_mismatch"):
        service.retain_output(SimpleNamespace(_importer=SimpleNamespace(db=object())), None, None, {}, {})

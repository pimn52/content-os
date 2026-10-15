"""Pure version/canonical contracts; no runtime or installation observation."""
import json
from uuid import uuid4
import pytest
from pydantic import TypeAdapter
from app.domain.evaluation_talking import TalkingAdmissionResultRecord, TalkingExactQualityObservation


def observation():
    fields=TalkingExactQualityObservation.model_fields
    value={k:'a'*64 for k in fields if k.endswith('sha256') or k=='output_scope_fingerprint'}
    return dict(value,observation_version=1,policy='evaluation-talking-calibration-v1',scope='exact_sample_only',observed_pass_duration_ms=1000)


def admission():
    result=dict(result_version=2,quality_observation=observation())
    from app.domain.evaluation_talking import EvaluationTalkingCalibrationAdmissionResult
    for key in EvaluationTalkingCalibrationAdmissionResult.model_fields:
        if key.endswith('_id'):result[key]=str(uuid4())
        elif key.endswith('sha256'):result[key]='b'*64
    return result


@pytest.mark.parametrize('version',[True,1.0,'1'])
def test_observation_version_is_an_exact_integer(version):
    value={**observation(),'observation_version':version}
    with pytest.raises(ValueError):TalkingExactQualityObservation.model_validate_json(json.dumps(value))


@pytest.mark.parametrize('version',[2.0,'2',True])
def test_calibration_result_version_is_an_exact_integer(version):
    value={**admission(),'result_version':version}
    with pytest.raises(ValueError):TypeAdapter(TalkingAdmissionResultRecord).validate_json(json.dumps(value))


def test_result_observation_roundtrip_and_no_client_approval():
    raw=admission();value=TypeAdapter(TalkingAdmissionResultRecord).validate_json(json.dumps(raw))
    assert value.model_dump(mode='json')==raw
    raw['quality_observation']['quality_status']='verified'
    with pytest.raises(ValueError):TypeAdapter(TalkingAdmissionResultRecord).validate_json(json.dumps(raw))

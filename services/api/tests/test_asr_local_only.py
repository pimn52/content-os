"""Strict local recipe using injected model factory; no installed ASR imports."""
from pathlib import Path
from types import SimpleNamespace
import pytest
from app.domain.asr_recipe import ASR_MODEL_FILES, ASR_DECODE, ASR_LOAD
from app.domain.asr_child import ASRChildResult
from app.providers.asr import FasterWhisperASRProvider, ASRConfigurationError, ASRInputError, ASRProviderResponseError

@pytest.fixture
def local(tmp_path):
    model=tmp_path/"model";model.mkdir()
    for name in ASR_MODEL_FILES:(model/name).write_bytes(b"synthetic")
    audio=tmp_path/"input.wav";audio.write_bytes(b"synthetic adapter input")
    calls=[]
    class Model:
        def transcribe(self,path,**kwargs):
            calls.append((path,kwargs))
            return iter([SimpleNamespace(text="observed",start=0.0,end=0.001,words=[])]),SimpleNamespace(language="zh")
    def factory(name,**kwargs):calls.append((name,kwargs));return Model()
    return model,audio,calls,factory

def test_local_only_pins_every_load_and_decode_setting_and_rejects_target_prompt(local):
    model,audio,calls,factory=local
    provider=FasterWhisperASRProvider(str(model),vad_filter=False,word_timestamps=True,local_only=True,model_factory=factory)
    with pytest.raises(ASRInputError):provider.transcribe(audio,prompt="target copy")
    assert calls==[]
    assert provider.transcribe(audio,language="zh").text=="observed"
    assert calls[0]==(str(model),ASR_LOAD)
    assert calls[1]==(str(audio),{**ASR_DECODE,"language":"zh"})
    assert calls[1][1]["initial_prompt"] is None and calls[1][1]["hotwords"] is None

@pytest.mark.parametrize("file",ASR_MODEL_FILES)
def test_missing_local_asset_never_constructs_or_downloads(local,file):
    model,_,calls,factory=local;(model/file).unlink()
    with pytest.raises(ASRConfigurationError):FasterWhisperASRProvider(str(model),vad_filter=False,word_timestamps=True,local_only=True,model_factory=factory)
    assert calls==[]

@pytest.mark.parametrize("name",["small","Systran/faster-whisper-small"])
def test_named_model_is_not_a_local_fallback(name):
    with pytest.raises(ASRConfigurationError):FasterWhisperASRProvider(name,vad_filter=False,word_timestamps=True,local_only=True)


@pytest.mark.parametrize("raw",['{}', '{"sampling_rate":16000,"feature_size":80}'])
def test_optional_preprocessor_uses_fixed_local_defaults(local, raw):
    model,audio,calls,factory=local
    (model/"preprocessor_config.json").write_text(raw)
    assert FasterWhisperASRProvider(str(model),vad_filter=False,word_timestamps=True,
        local_only=True,model_factory=factory).transcribe(audio).text == "observed"


@pytest.mark.parametrize("raw",['not json', '[]', '{"feature_size":128}', '{"sampling_rate":true}'])
def test_optional_preprocessor_cannot_change_recipe_or_trigger_fallback(local, raw):
    model,_,calls,factory=local
    (model/"preprocessor_config.json").write_text(raw)
    with pytest.raises(ASRConfigurationError):
        FasterWhisperASRProvider(str(model),vad_filter=False,word_timestamps=True,
            local_only=True,model_factory=factory)
    assert calls == []

@pytest.mark.parametrize("raw",[
    '{"text":"a","segments":[{"start_ms":true,"end_ms":1,"text":"a"}],"words":[]}',
    '{"text":"a","segments":[{"start_ms":0,"end_ms":NaN,"text":"a"}],"words":[]}',
    '{"text":"a","segments":[{"start_ms":0,"end_ms":100001,"text":"a"}],"words":[]}',
    '{"text":"target","segments":[],"words":[]}'
])
def test_malformed_or_unbound_transcript_never_becomes_qa(raw):
    with pytest.raises(ValueError):ASRChildResult.model_validate_json(raw).transcription(1000)

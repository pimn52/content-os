"""Isolated local ASR child. Only the owned reservation invokes this entry."""
import sys
from pathlib import Path
from app.domain.asr_child import ASRChildRequest, ASRChildResult
from app.domain.asr_recipe import ASR_PARAMETERS

def main():
    if len(sys.argv) != 2:
        return 2
    request_path = Path(sys.argv[1])
    request = ASRChildRequest.model_validate_json(request_path.read_bytes())
    duration = request.verify_audio()
    from app.providers.asr import FasterWhisperASRProvider
    provider = FasterWhisperASRProvider(request.model_root, device="cpu", compute_type="int8",
        vad_filter=False, word_timestamps=True, local_only=True)
    result = ASRChildResult.from_transcription(provider.transcribe(request.audio_path, language=request.language))
    result.transcription(duration)
    request.verify_audio()
    raw = result.model_dump_json().encode()
    if len(raw) > ASR_PARAMETERS["output_max_bytes"]:
        raise ValueError("maintained_asr_output_limit")
    request_path.with_name("result.json").write_bytes(raw)
    return 0

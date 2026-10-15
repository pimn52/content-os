"""Fixed local ASR recipe; no vendor imports or implicit decode defaults."""
import json
from pathlib import Path

# The installed FasterWhisper accepts a local vocabulary.txt and uses these
# FeatureExtractor defaults when the optional preprocessor file is absent.
ASR_FEATURES = dict(feature_size=80, sampling_rate=16000, hop_length=160,
    chunk_length=30, n_fft=400)

ASR_DECODE = dict(task="transcribe", log_progress=False, beam_size=5, best_of=5,
    patience=1.0, length_penalty=1.0, repetition_penalty=1.0, no_repeat_ngram_size=0,
    temperature=0.0, compression_ratio_threshold=2.4, log_prob_threshold=-1.0,
    no_speech_threshold=0.6, condition_on_previous_text=False,
    prompt_reset_on_temperature=0.5, initial_prompt=None, prefix=None,
    suppress_blank=True, suppress_tokens=[-1], without_timestamps=False,
    max_initial_timestamp=1.0, word_timestamps=True,
    prepend_punctuations="\"'“¿([{-", append_punctuations="\"'.。,，!！?？:：”)]}、",
    multilingual=False, vad_filter=False, vad_parameters=None, max_new_tokens=None,
    chunk_length=None, clip_timestamps="0", hallucination_silence_threshold=None,
    hotwords=None, language_detection_threshold=0.5, language_detection_segments=1)
ASR_LOAD = dict(device="cpu", device_index=0, compute_type="int8", cpu_threads=4,
    num_workers=1, download_root=None, local_files_only=True, files=None,
    revision=None, use_auth_token=None)
ASR_PARAMETERS = dict(device="cpu", compute_type="int8", local_files_only=True,
    features_json=json.dumps(ASR_FEATURES, sort_keys=True, separators=(",", ":")),
    decode_json=json.dumps(ASR_DECODE, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
    load_json=json.dumps(ASR_LOAD, sort_keys=True, separators=(",", ":")),
    input_max_bytes=16*1024**2, input_max_frames=8*1024**2, output_max_bytes=1024**2,
    qa_policy="voice-evaluation-qa-v1", max_silence_ms=2000,
    max_leading_silence_ms=500, max_substitution_ratio=0.05)
ASR_MODEL_FILES = ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")


def validate_local_features(model: Path) -> None:
    """Optional local configuration must preserve the exact fixed recipe."""
    config = model / "preprocessor_config.json"
    if not config.exists():
        return
    if (not config.is_file() or config.resolve().parent != model
        or config.stat().st_size > 64 * 1024):
        raise ValueError("maintained_asr_feature_config_invalid")
    try:
        raw = json.loads(config.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError("maintained_asr_feature_config_invalid") from exc
    if not isinstance(raw, dict):
        raise ValueError("maintained_asr_feature_config_invalid")
    for key, expected in ASR_FEATURES.items():
        actual = raw.get(key, expected)
        if type(actual) is not type(expected) or actual != expected:
            raise ValueError("maintained_asr_feature_recipe_changed")

"""Run a provenance-preserving visual assisted-test over local MP4s.

The script sends sampled JPEG frames rather than whole videos. The provider is
selected explicitly (or by ``CONTENT_OS_ASSISTED_TEST_PROVIDER``), and each
provider reads only its own configured environment variable. This is an
assisted-test artifact, not a runtime/provider health check or a rights,
identity, or production-usage decision.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5


FRACTIONS = (0.05, 0.20, 0.35, 0.50, 0.65, 0.80, 0.95)
_DEFAULT_ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1"
_DEFAULT_OPENAI_ENDPOINT = "https://api.openai.com/v1"
_ASSISTED_PROMPT_VERSION = "r1-assisted-vision-v2"


def _command(value: str | None, fallback: Path) -> str:
    return value.strip() if value and value.strip() else str(fallback)


def _probe_duration(ffprobe: str, source: Path) -> float:
    completed = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", str(source)],
        capture_output=True,
        text=True,
        check=True,
    )
    document = json.loads(completed.stdout)
    duration = float(document["format"]["duration"])
    if duration <= 0:
        raise ValueError(f"source duration must be positive: {source}")
    return duration


def _file_sha256(source: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sample_frames(ffmpeg: str, ffprobe: str, source: Path, output_dir: Path) -> list[tuple[float, Path]]:
    duration = _probe_duration(ffprobe, source)
    output_dir.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[float, Path]] = []
    for index, fraction in enumerate(FRACTIONS, start=1):
        destination = output_dir / f"frame{index:02d}.jpg"
        seconds = round(duration * fraction, 3)
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", str(seconds), "-i", str(source), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3", str(destination)],
            check=True,
        )
        frames.append((fraction, destination))
    return frames


def _image_content(fraction: float, path: Path) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
        },
        "cache_control": {"type": "ephemeral"},
    }


def _openai_image_content(path: Path) -> dict[str, Any]:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}


def _analysis_prompt(source_name: str) -> str:
    return f"""你是 Content OS 的 assisted-test 视觉分析器。以下是同一个本地 MP4《{source_name}》在时间轴上的七个抽样画面，样本位置写在图片元信息中。

请只返回一个 JSON 对象，不要 Markdown。字段必须是：
{{
  "summary": "一句准确的中文内容概述",
  "language": "可见字幕或口播可能使用的语言；不确定写 unknown",
  "segments": [{{"start_fraction": 0.05, "end_fraction": 0.20, "visual_description": "", "on_screen_text": [], "talking_candidate": false, "confidence": 0.0}}],
  "topic_candidates": [{{"topic": "", "angle": "", "evidence_samples": [0.05]}}],
  "uncertainties": ["..."]
}}

只根据画面和可读字幕，不要臆测人物姓名、所有权、授权、年龄、职业或个人身份；不要把“看起来像”写成事实。segments 至少覆盖两个有意义的连续画面区间，confidence 必须在 0 到 1。`start_fraction` 和 `end_fraction` 必须是 0 到 1 之间的小数比例，不能写百分数或视频秒数（例如写 0.30，不要写 30）。topic_candidates 最多 3 个，evidence_samples 只能使用提供的抽样位置。"""


def _anthropic_content(source_name: str, frames: list[tuple[float, Path]]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": _analysis_prompt(source_name)}]
    for fraction, path in frames:
        content.append({"type": "text", "text": f"接下来是抽样位置 {fraction:.2f} 的画面："})
        content.append(_image_content(fraction, path))
    return content


def _openai_content(source_name: str, frames: list[tuple[float, Path]]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": _analysis_prompt(source_name)}]
    for fraction, path in frames:
        content.append({"type": "text", "text": f"接下来是抽样位置 {fraction:.2f} 的画面："})
        content.append(_openai_image_content(path))
    return content


def _extract_text(document: dict[str, Any]) -> str:
    blocks = document.get("content")
    if not isinstance(blocks, list):
        raise ValueError("model response has no content blocks")
    text = "\n".join(block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text")
    if not text.strip():
        raise ValueError("model response has no text")
    return text.strip()


def _extract_openai_text(document: dict[str, Any]) -> str:
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("model response has no choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("model response has no message")
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        text = "\n".join(block.get("text", "") for block in content if isinstance(block, dict) and isinstance(block.get("text"), str))
        if text.strip():
            return text.strip()
    raise ValueError("model response has no text")


def _json_from_model(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("model response JSON must be an object")
    return value


def _validate_analysis(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise ValueError("model response summary is missing")
    segments = value.get("segments")
    if not isinstance(segments, list) or len(segments) < 2:
        raise ValueError("model response must contain at least two segments")
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("model response contains an invalid segment")
        start, end, confidence = segment.get("start_fraction"), segment.get("end_fraction"), segment.get("confidence")
        if not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in (start, end, confidence)):
            raise ValueError("model response segment fractions/confidence must be numeric")
        if not 0 <= start < end <= 1 or not 0 <= confidence <= 1:
            raise ValueError("model response segment values are outside 0..1")
    topics = value.get("topic_candidates", [])
    if not isinstance(topics, list) or len(topics) > 3:
        raise ValueError("model response topic_candidates must contain at most three items")
    return value


def _analysis_input_hash(items: list[dict[str, Any]], provider: str, model: str) -> str:
    material = {
        "prompt_version": _ASSISTED_PROMPT_VERSION,
        "provider": provider,
        "model": model,
        "sources": [
            {"content_hash": item["content_hash"], "filename": item["filename"]}
            for item in items
        ],
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _load_local_context(database: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Read local asset/profile payloads needed to bind a bundle.

    The assisted-test can still run without a local database and will retain
    its raw model artifact. A bundle is emitted only when every source can be
    matched by its immutable content hash, so a semantic result is never
    attached to an unknown asset by filename or position.
    """
    if not database.is_file():
        return {}, {}
    connection = sqlite3.connect(database)
    try:
        assets: dict[str, dict[str, Any]] = {}
        for (raw_payload,) in connection.execute("SELECT payload FROM assets"):
            payload = json.loads(raw_payload)
            if isinstance(payload, dict) and isinstance(payload.get("content_hash"), str):
                assets[payload["content_hash"]] = payload
        profile_row = connection.execute("SELECT payload FROM ip_profiles ORDER BY rowid LIMIT 1").fetchone()
        profile = {} if profile_row is None else json.loads(profile_row[0])
        if isinstance(profile, dict) and isinstance(profile.get("id"), str):
            revision_row = connection.execute(
                "SELECT MAX(version) FROM ip_profile_revisions WHERE profile_id = ?",
                (profile["id"],),
            ).fetchone()
            profile_metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
            profile["metadata"] = {**profile_metadata, "profile_version": revision_row[0] or 1}
        return assets, profile if isinstance(profile, dict) else {}
    finally:
        connection.close()


def _fraction_to_ms(fraction: float, duration_ms: int) -> int:
    return max(0, min(duration_ms, int(round(fraction * duration_ms))))


def _build_analysis_bundle(
    items: list[dict[str, Any]],
    *,
    provider: str,
    model: str,
    asset_by_hash: dict[str, dict[str, Any]],
    profile: dict[str, Any] | None = None,
    analyzed_at: datetime | None = None,
) -> dict[str, Any]:
    """Convert validated model segments into the existing replay contract.

    The conversion deliberately carries only fields the model actually
    returned. In particular, on-screen text is stored as available subtitles,
    never as a fabricated spoken transcript. Clip IDs are stable for the same
    source/model/segment, making API persistence idempotent.
    """
    input_hash = _analysis_input_hash(items, provider, model)
    timestamp = analyzed_at or datetime.now(timezone.utc)
    profile_value = profile or {}
    profile_id = profile_value.get("id") if isinstance(profile_value.get("id"), str) else None
    profile_metadata = profile_value.get("metadata")
    profile_version = profile_metadata.get("profile_version") if isinstance(profile_metadata, dict) else None
    results: list[dict[str, Any]] = []
    for item in items:
        asset = asset_by_hash.get(item["content_hash"])
        if asset is None:
            raise ValueError(f"local asset content hash is not imported: {item['content_hash']}")
        duration_ms = int(asset["duration_ms"])
        for segment_index, segment in enumerate(item["analysis"]["segments"]):
            start_ms = _fraction_to_ms(float(segment["start_fraction"]), duration_ms)
            end_ms = _fraction_to_ms(float(segment["end_fraction"]), duration_ms)
            if end_ms <= start_ms:
                end_ms = min(duration_ms, start_ms + 1)
            if end_ms <= start_ms:
                raise ValueError(f"model segment is too small for asset duration: {item['filename']}")
            keyframes: list[dict[str, Any]] = []
            for fraction, frame_path in item["frames"]:
                timestamp_ms = _fraction_to_ms(float(fraction), duration_ms)
                if start_ms <= timestamp_ms <= end_ms:
                    keyframes.append({
                        "timestamp_ms": timestamp_ms,
                        "reference": str(Path(frame_path).resolve()),
                        "description": segment.get("visual_description") or None,
                    })
            clip_id = uuid5(
                NAMESPACE_URL,
                f"content-os:{input_hash}:{asset['id']}:{segment_index}:{start_ms}:{end_ms}",
            )
            subtitles = [
                text for text in segment.get("on_screen_text", [])
                if isinstance(text, str) and text.strip()
            ]
            results.append({
                "schema_version": "0.1",
                "asset_id": asset["id"],
                "clip_id": str(clip_id),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "transcript": None,
                "available_subtitles": subtitles,
                "visual_description": segment.get("visual_description") or None,
                "people": [],
                "objects": [],
                "action": None,
                "confidence": segment.get("confidence"),
                "keyframes": keyframes,
            })
    bundle_id = uuid5(NAMESPACE_URL, f"content-os:analysis-bundle:{input_hash}")
    return {
        "schema_version": "0.1",
        "id": str(bundle_id),
        "input_hash": input_hash,
        "mode": "assisted_test",
        "source": "scripts.real_assisted_test",
        "model": model,
        "tool": f"{provider}:sampled-jpeg-vision",
        "analyzed_at": timestamp.astimezone(timezone.utc).isoformat(),
        "ip_profile_id": profile_id,
        "ip_profile_version": profile_version if isinstance(profile_version, int) and profile_version > 0 else None,
        "profile_snapshot": profile_value,
        "results": results,
    }


def _provider_config(provider: str, model: str | None, endpoint: str | None) -> tuple[str, str, str]:
    normalized = provider.strip().lower()
    if normalized == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        selected_model = model or os.environ.get("ANTHROPIC_MODEL", "claude-3-5-haiku-latest")
        selected_endpoint = endpoint or os.environ.get("ANTHROPIC_BASE_URL", _DEFAULT_ANTHROPIC_ENDPOINT)
    elif normalized in {"openai", "openai-compatible"}:
        key = os.environ.get("OPENAI_API_KEY", "").strip() or os.environ.get("CONTENT_OS_LLM_API_KEY", "").strip()
        selected_model = model or os.environ.get("CONTENT_OS_LLM_MODEL", "gpt-4o-mini")
        selected_endpoint = endpoint or os.environ.get("CONTENT_OS_LLM_BASE_URL", os.environ.get("OPENAI_BASE_URL", _DEFAULT_OPENAI_ENDPOINT))
        normalized = "openai-compatible"
    else:
        raise ValueError(f"unsupported assisted-test provider: {provider}")
    if not key:
        variable = "ANTHROPIC_API_KEY" if normalized == "anthropic" else "OPENAI_API_KEY or CONTENT_OS_LLM_API_KEY"
        raise ValueError(f"{variable} is not configured for provider {normalized}")
    return normalized, selected_model, selected_endpoint


def _request_json(endpoint: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint, data=json.dumps(body).encode("utf-8"), headers={"content-type": "application/json", **headers}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            document = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(500).decode("utf-8", errors="replace")
        raise RuntimeError(f"assisted-test provider HTTP {exc.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"assisted-test provider connection failed: {type(exc).__name__}") from None
    if not isinstance(document, dict):
        raise ValueError("provider response must be a JSON object")
    return document


def _persist_local_bundle(api_url: str, bundle: dict[str, Any]) -> dict[str, Any]:
    headers: dict[str, str] = {}
    access_token = os.environ.get("CONTENT_OS_ACCESS_TOKEN", "").strip()
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return _request_json(api_url.rstrip("/") + "/analysis-results", bundle, headers)


def _call_model(*, provider: str, api_key: str, model: str, endpoint: str, source_name: str, frames: list[tuple[float, Path]], temperature: float = 0.0) -> tuple[dict[str, Any], dict[str, Any]]:
    if provider == "anthropic":
        response = _request_json(
            endpoint.rstrip("/") + "/messages",
            {"model": model, "max_tokens": 1800, "temperature": temperature, "messages": [{"role": "user", "content": _anthropic_content(source_name, frames)}]},
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
        model_text = _extract_text(response)
    else:
        response = _request_json(
            endpoint.rstrip("/") + "/chat/completions",
            {"model": model, "temperature": temperature, "messages": [{"role": "user", "content": _openai_content(source_name, frames)}]},
            {"Authorization": f"Bearer {api_key}"},
        )
        model_text = _extract_openai_text(response)
    analysis = _validate_analysis(_json_from_model(model_text))
    return analysis, {"model": model, "provider_response": response, "model_text": model_text}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ffmpeg")
    parser.add_argument("--ffprobe")
    parser.add_argument("--provider", default=os.environ.get("CONTENT_OS_ASSISTED_TEST_PROVIDER", "anthropic"), choices=["anthropic", "openai-compatible"])
    parser.add_argument("--model")
    parser.add_argument("--endpoint")
    parser.add_argument("--temperature", type=float, default=0.0, help="Provider sampling temperature; some models require a fixed value such as 1.")
    parser.add_argument("--database", type=Path, default=Path("content-os-data") / "content-os.sqlite3", help="Local SQLite database used only to bind imported assets by content hash.")
    parser.add_argument("--persist", action="store_true", help="POST the generated AnalysisResultBundle to the local API after validation.")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000", help="Local API base URL used with --persist.")
    args = parser.parse_args()
    if not math.isfinite(args.temperature) or args.temperature < 0 or args.temperature > 2:
        parser.error("temperature must be finite and between 0 and 2")
    provider, model, endpoint = _provider_config(args.provider, args.model, args.endpoint)
    root = Path(__file__).resolve().parents[1]
    bundled = root / "apps" / "renderer" / "node_modules" / "@remotion" / "compositor-win32-x64-msvc"
    ffmpeg = _command(args.ffmpeg or os.environ.get("CONTENT_OS_FFMPEG"), bundled / "ffmpeg.exe")
    ffprobe = _command(args.ffprobe or os.environ.get("CONTENT_OS_FFPROBE"), bundled / "ffprobe.exe")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip() if provider == "anthropic" else (os.environ.get("OPENAI_API_KEY", "").strip() or os.environ.get("CONTENT_OS_LLM_API_KEY", "").strip())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "assisted_test",
        "provider": provider,
        "model": model,
        "sources": [],
        "notice": "Visual sampled-frame analysis only; no ownership or identity decision.",
    }
    analyzed_items: list[dict[str, Any]] = []
    for index, source in enumerate(args.sources, start=1):
        if not source.is_file() or source.suffix.lower() != ".mp4":
            raise SystemExit(f"source is not an MP4 file: {source}")
        item_dir = args.output_dir / f"video{index}"
        content_hash = _file_sha256(source)
        duration = _probe_duration(ffprobe, source)
        frames = _sample_frames(ffmpeg, ffprobe, source, item_dir)
        analysis, raw = _call_model(provider=provider, api_key=api_key, model=model, endpoint=endpoint, source_name=source.name, frames=frames, temperature=args.temperature)
        (item_dir / "model-response.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        (item_dir / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
        item = {
            "index": index,
            "filename": source.name,
            "source_path": str(source),
            "content_hash": content_hash,
            "duration_seconds": duration,
            "duration_ms": int(round(duration * 1000)),
            "sample_fractions": list(FRACTIONS),
            "analysis_path": str(item_dir / "analysis.json"),
            "frames": frames,
            "analysis": analysis,
        }
        analyzed_items.append(item)
        manifest["sources"].append({key: value for key, value in item.items() if key not in {"frames", "analysis"}})
        print(f"analyzed video{index}: {source.name}")
    asset_by_hash, profile = _load_local_context(args.database)
    if asset_by_hash and all(item["content_hash"] in asset_by_hash for item in analyzed_items):
        bundle = _build_analysis_bundle(analyzed_items, provider=provider, model=model, asset_by_hash=asset_by_hash, profile=profile, analyzed_at=datetime.now(timezone.utc))
        bundle_path = args.output_dir / "analysis-bundle.json"
        bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest["analysis_bundle_path"] = str(bundle_path)
        manifest["analysis_bundle_status"] = "validated_local_asset_bindings"
        if args.persist:
            persisted = _persist_local_bundle(args.api_url, bundle)
            manifest["analysis_bundle_status"] = "persisted_to_local_api"
            manifest["analysis_bundle_id"] = persisted.get("id")
    else:
        if args.persist:
            raise ValueError("--persist requires every source to match an imported local Asset by content hash")
        manifest["analysis_bundle_status"] = "not_emitted: local Asset bindings unavailable"
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, json.JSONDecodeError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"assisted-test failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

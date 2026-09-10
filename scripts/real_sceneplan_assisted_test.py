"""Generate traceable ScenePlan artifacts with a real text-model assisted test.

This command is intentionally separate from the runtime API. It reads only
bounded local project/IP/media context, uses a key supplied for this invocation,
validates the model result against the domain contract, and writes an ignored
artifact. It never changes project drafts or provider-call accounting unless a
future explicit import workflow is added.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from app.domain.models import Project, ScenePlan
from scripts.real_assisted_test import _extract_openai_text, _json_from_model, _request_json


PROMPT_VERSION = "r1-assisted-sceneplan-v1"
DEFAULT_ENDPOINT = "https://api.moonshot.cn/v1"
DEFAULT_MODEL = "kimi-k3"
_SCENE_FIELDS = {
    "scene_id",
    "order",
    "purpose",
    "voice_text",
    "duration_target_ms",
    "visual_intent",
    "preferred_sources",
    "fallback_sources",
    "caption_emphasis",
}


def _read_key(path: Path | None) -> str:
    if path is not None:
        value = path.read_text(encoding="utf-8").strip()
    else:
        value = os.environ.get("CONTENT_OS_ASSISTED_TEST_LLM_KEY", "").strip()
    if not value:
        raise ValueError("an assisted-test key must be supplied by --key-file or CONTENT_OS_ASSISTED_TEST_LLM_KEY")
    return value


def _bounded(value: object, maximum: int = 2_000) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:maximum] if value else None


def _load_local_context(database: Path, project_ids: list[UUID]) -> list[dict[str, Any]]:
    if not database.is_file():
        raise ValueError(f"database does not exist: {database}")
    connection = sqlite3.connect(database)
    try:
        projects_by_id: dict[UUID, Project] = {}
        for raw_payload, in connection.execute("SELECT payload FROM projects"):
            project = Project.model_validate(json.loads(raw_payload))
            projects_by_id[project.id] = project
        missing = [str(project_id) for project_id in project_ids if project_id not in projects_by_id]
        if missing:
            raise ValueError(f"project not found in local database: {', '.join(missing)}")

        profile_rows = connection.execute("SELECT payload FROM ip_profiles").fetchall()
        profiles = {UUID(json.loads(row[0])["id"]): json.loads(row[0]) for row in profile_rows}
        assets = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM assets")]
        clips = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM clips")]
        bundles = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM analysis_result_bundles")]
        clip_by_asset: dict[str, list[dict[str, Any]]] = {}
        for clip in clips:
            asset_id = clip.get("asset_id")
            if isinstance(asset_id, str):
                clip_by_asset.setdefault(asset_id, []).append(clip)

        material_context: list[dict[str, Any]] = []
        for asset in assets:
            if not isinstance(asset, dict) or not isinstance(asset.get("id"), str):
                continue
            asset_id = asset["id"]
            asset_clips = sorted(clip_by_asset.get(asset_id, []), key=lambda clip: (clip.get("start_ms", 0), clip.get("id", "")))
            material_context.append({
                "asset_id": asset_id,
                "source_file": Path(str(asset.get("source_file", ""))).name,
                "source_kind": asset.get("source_kind"),
                "duration_ms": asset.get("duration_ms"),
                "width": asset.get("width"),
                "height": asset.get("height"),
                "usage": (asset.get("metadata") or {}).get("r1_usage", "unknown"),
                "identity": (asset.get("metadata") or {}).get("r1_identity", "unknown"),
                "clips": [{
                    "clip_id": clip.get("id"),
                    "start_ms": clip.get("start_ms"),
                    "end_ms": clip.get("end_ms"),
                    "transcript": _bounded(clip.get("transcript"), 500),
                    "visual_description": _bounded(clip.get("visual_description"), 500),
                    "action": _bounded(clip.get("action"), 300),
                    "orientation": clip.get("orientation"),
                    "talking_candidate": clip.get("talking_candidate"),
                } for clip in asset_clips[:12]],
            })

        bundle_refs = [
            f"analysis_bundle:{bundle['id']}"
            for bundle in bundles
            if isinstance(bundle, dict) and isinstance(bundle.get("id"), str)
        ]
        result: list[dict[str, Any]] = []
        for project_id in project_ids:
            project = projects_by_id[project_id]
            profile = profiles.get(project.ip_profile_id, {})
            profile_metadata = profile.get("metadata") if isinstance(profile, dict) else {}
            refs = [
                f"project:{project.id}:topic",
                f"ip_profile:{project.ip_profile_id}:v{(profile_metadata or {}).get('profile_version', 1)}",
                *bundle_refs,
            ]
            result.append({
                "project": project.model_dump(mode="json"),
                "ip_profile": {
                    "id": profile.get("id"),
                    "creator_name": profile.get("creator_name"),
                    "audience": profile.get("audience"),
                    "expertise": profile.get("expertise"),
                    "knowledge": _bounded(profile.get("knowledge"), 1_000),
                    "opinions": _bounded(profile.get("opinions"), 1_000),
                    "style_notes": _bounded(profile.get("style_notes"), 1_000),
                    "boundaries": _bounded(profile.get("boundaries"), 1_000),
                },
                "evidence_refs": refs,
                "materials": material_context,
            })
        return result
    finally:
        connection.close()


def _prompt(context: dict[str, Any]) -> str:
    return (
        "你是 Content OS R1 的 assisted-test 文案与镜头规划器。只返回 JSON，不要 Markdown。\n"
        "任务：基于给定的项目主题、IP 资料和真实本地素材摘要，规划 2 到 5 个连续短视频场景。\n"
        "硬约束：\n"
        "1. 不把不存在的画面、人物、字幕、口播或事实写成已确认内容；没有真实转写时间轴时，不声称镜头与句子已经对齐。\n"
        "2. 优先复用 preferred_sources 中的 user_asset 或 historical_asset；不可满足时才使用 typography 作为 fallback。\n"
        "3. voice_text 是待审核的新文案，不是素材原声的转写；duration_target_ms 只是规划目标，不是已验证的音频时长。\n"
        "4. 每个场景都要有 hook/development/close 等明确 purpose、具体 visual_intent 和可审核的 caption_emphasis。\n"
        "5. 只输出下列 JSON 结构，场景字段不能增删：\n"
        '{"scenes":[{"scene_id":"scene_01","order":0,"purpose":"hook",'
        '"voice_text":"","duration_target_ms":6000,"visual_intent":{"subject":"",'
        '"action":"","framing":"","description":""},"preferred_sources":["user_asset"],'
        '"fallback_sources":["typography"],"caption_emphasis":[]}]}\n\n'
        "LOCAL CONTEXT:\n"
        + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    )


def _input_hash(context: dict[str, Any], model: str) -> str:
    material = {
        "prompt_version": PROMPT_VERSION,
        "model": model,
        "context": context,
    }
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _validate_plan(raw: dict[str, Any], project: Project, evidence_refs: list[str], input_hash: str) -> list[ScenePlan]:
    if set(raw) != {"scenes"} or not isinstance(raw["scenes"], list) or not 2 <= len(raw["scenes"]) <= 5:
        raise ValueError("model ScenePlan must contain 2 to 5 scenes and no extra top-level fields")
    plans: list[ScenePlan] = []
    for index, item in enumerate(raw["scenes"]):
        if not isinstance(item, dict) or set(item) != _SCENE_FIELDS:
            raise ValueError("model ScenePlan scene does not match the strict field set")
        if item.get("order") != index:
            raise ValueError("model ScenePlan orders must be contiguous and start at zero")
        scene_id = item.get("scene_id")
        if not isinstance(scene_id, str) or not scene_id.strip():
            raise ValueError("model ScenePlan scene_id must be non-empty")
        stable_id = uuid5(NAMESPACE_URL, f"content-os:assisted-sceneplan:{project.id}:{input_hash}:{scene_id}")
        plans.append(ScenePlan.model_validate({
            **item,
            "id": stable_id,
            "project_id": project.id,
            "evidence_refs": evidence_refs,
        }))
    if len({plan.scene_id for plan in plans}) != len(plans):
        raise ValueError("model ScenePlan scene IDs must be unique")
    if any(not plan.preferred_sources or plan.preferred_sources[0].value not in {"user_asset", "historical_asset"} for plan in plans):
        raise ValueError("real creator media must be the first preferred source in this assisted test")
    return plans


def _call_plan(key: str, endpoint: str, model: str, context: dict[str, Any], temperature: float) -> tuple[dict[str, Any], dict[str, Any]]:
    response = _request_json(
        endpoint.rstrip("/") + "/chat/completions",
        {
            "model": model,
            "temperature": temperature,
            "messages": [{"role": "user", "content": _prompt(context)}],
        },
        {"Authorization": f"Bearer {key}"},
    )
    text = _extract_openai_text(response)
    return _json_from_model(text), {"model": model, "provider_response": response, "model_text": text}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a real text-model ScenePlan assisted-test without mutating drafts.")
    parser.add_argument("project_ids", nargs="+", type=UUID)
    parser.add_argument("--database", type=Path, default=Path("content-os-data") / "content-os.sqlite3")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--endpoint", default=os.environ.get("CONTENT_OS_ASSISTED_TEST_ENDPOINT", DEFAULT_ENDPOINT))
    parser.add_argument("--model", default=os.environ.get("CONTENT_OS_ASSISTED_TEST_MODEL", DEFAULT_MODEL))
    parser.add_argument("--temperature", type=float, default=1.0)
    args = parser.parse_args(argv)
    if not math.isfinite(args.temperature) or not 0 <= args.temperature <= 2:
        parser.error("temperature must be finite and between 0 and 2")
    if not args.model.strip() or not args.endpoint.strip():
        parser.error("model and endpoint must be non-empty")
    key = _read_key(args.key_file)
    contexts = _load_local_context(args.database, args.project_ids)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    run_at = datetime.now(timezone.utc)
    manifest: dict[str, Any] = {
        "schema_version": "0.1",
        "mode": "assisted_test",
        "source": "scripts.real_sceneplan_assisted_test",
        "provider": "openai-compatible",
        "model": args.model,
        "prompt_version": PROMPT_VERSION,
        "created_at": run_at.isoformat(),
        "notice": "Real model planning artifact only; not a runtime readiness or U1 semantic acceptance result.",
        "projects": [],
    }
    for context in contexts:
        project = Project.model_validate(context["project"])
        input_hash = _input_hash(context, args.model)
        raw, provider_artifact = _call_plan(key, args.endpoint, args.model, context, args.temperature)
        plans = _validate_plan(raw, project, context["evidence_refs"], input_hash)
        project_dir = args.output_dir / str(project.id)
        project_dir.mkdir(parents=True, exist_ok=True)
        plan_path = project_dir / "scene-plan.json"
        response_path = project_dir / "model-response.json"
        plan_artifact = {
            "schema_version": "0.1",
            "mode": "assisted_test",
            "source": "scripts.real_sceneplan_assisted_test",
            "provider": "openai-compatible",
            "model": args.model,
            "prompt_version": PROMPT_VERSION,
            "input_hash": input_hash,
            "created_at": run_at.isoformat(),
            "project_id": str(project.id),
            "ip_profile_id": str(project.ip_profile_id),
            "evidence_refs": context["evidence_refs"],
            "scenes": [plan.model_dump(mode="json") for plan in plans],
            "notice": "voice_text is a model draft; source audio/transcript and semantic timing remain unverified.",
        }
        plan_path.write_text(json.dumps(plan_artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        response_path.write_text(json.dumps(provider_artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest["projects"].append({
            "project_id": str(project.id),
            "input_hash": input_hash,
            "scene_count": len(plans),
            "scene_plan_path": str(plan_path),
        })
        print(f"planned {project.id}: {len(plans)} scenes")
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, json.JSONDecodeError, OSError, RuntimeError) as exc:
        print(f"assisted scene-plan failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

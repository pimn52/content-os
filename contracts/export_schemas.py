"""Export public JSON Schema contracts and stable examples.

Run from the repository root: python contracts/export_schemas.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "services" / "api"
sys.path.insert(0, str(API_ROOT))

from app.domain.models import (  # noqa: E402
    AccountConnection, AnalysisClipResult, AnalysisKeyframe, AnalysisResultBundle, Asset, AssetUsageEvent, AudioAsset, BudgetPolicy, CandidateAsset, Clip, ContentFeedback, ContentOpportunity, CostEstimate, CostLineItem, CostReductionSuggestion, DraftRoute, HistoricalContent, ImageAsset, IPProfile, Job, ProviderCallRecord, PublicationRecord,
    Project, ProjectDraft, ProjectDraftRevision, ProjectFormat, RationalFps, ScenePlan, ShootTask, SourceKind, TalkingProfile, TranscriptSegment,
    CostCategory, MasterNarration, UsageCost, VideoCaption, VideoScene, VideoSpec, VideoVisual, VoiceProfile,
)

SCHEMAS = (IPProfile, AccountConnection, HistoricalContent, ContentOpportunity, Asset, ImageAsset, AudioAsset, Clip, TranscriptSegment, AnalysisKeyframe, AnalysisClipResult, AnalysisResultBundle, AssetUsageEvent, PublicationRecord, ContentFeedback, BudgetPolicy, ProviderCallRecord, VoiceProfile,
           TalkingProfile, Project, ScenePlan, CandidateAsset, ShootTask, DraftRoute, ProjectDraft, ProjectDraftRevision,
           VideoCaption, MasterNarration, VideoSpec, Job, UsageCost, CostLineItem, CostEstimate, CostReductionSuggestion)


def main() -> None:
    schemas_dir = ROOT / "contracts" / "schemas"
    examples_dir = ROOT / "contracts" / "examples"
    schemas_dir.mkdir(parents=True, exist_ok=True)
    examples_dir.mkdir(parents=True, exist_ok=True)
    for model in SCHEMAS:
        (schemas_dir / f"{model.__name__}.schema.json").write_text(
            json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8"
        )

    project_id = UUID("11111111-1111-1111-1111-111111111111")
    asset_id = UUID("22222222-2222-2222-2222-222222222222")
    clip_id = UUID("33333333-3333-3333-3333-333333333333")
    spec = VideoSpec(
        project_id=project_id, format=ProjectFormat.VERTICAL, width=1080, height=1920,
        fps=RationalFps(numerator=30, denominator=1),
        scenes=[VideoScene(scene_id="scene_01", start_frame=0, duration_frames=90,
                           visual=VideoVisual(source_kind=SourceKind.USER_ASSET, authorization_reference="user-owned-media",
                                              asset_id=asset_id, clip_id=clip_id, clip_start_ms=13_200, clip_end_ms=16_200, source_duration_ms=60_000),
                           caption="A continuous user clip", transition="cut")],
        estimated_cost=UsageCost(category=CostCategory.USER_ASSET, amount=Decimal("0"), currency="USD"),
    )
    (examples_dir / "video_spec.json").write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
    clip = Clip(id=clip_id, asset_id=asset_id, start_ms=13_200, end_ms=16_200, asset_duration_ms=60_000,
                transcript="A continuous source interval.", talking_candidate=True)
    (examples_dir / "clip.json").write_text(clip.model_dump_json(indent=2) + "\n", encoding="utf-8")
    scene = ScenePlan(id=UUID("44444444-4444-4444-4444-444444444444"), project_id=project_id, scene_id="scene_01", order=0, purpose="hook",
                      voice_text="Start with the creator’s point.", duration_target_ms=3_000,
                      visual_intent={"subject": "creator", "framing": "close"},
                      preferred_sources=[SourceKind.USER_ASSET], fallback_sources=[SourceKind.TYPOGRAPHY])
    (examples_dir / "scene_plan.json").write_text(scene.model_dump_json(indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

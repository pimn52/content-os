"""按独立 QA 证据返工声音，保留不受影响的合格生成段。"""
from __future__ import annotations

import json
import hashlib
import wave
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path
import re
from typing import Literal
from uuid import UUID, uuid4

from pypinyin import Style, pinyin
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.budget import BudgetLimitError, enforce_reservation
from app.db import (AssetRepository, AudioAssetRepository, BudgetPolicyRepository,
    ClipRepository, Database, JobRepository, ProjectDraftRepository, ProviderCallRepository,
    ProviderMachineCapabilityProfileRepository, ProviderMachineSettingRepository, VoiceProfileRepository)
from app.domain.models import Job, JobStatus, JobType, UsageCost, CostCategory, VoiceGenerationJobPayload, TranscriptSegment
from app.production_runs import ProductionRunConflict, ProductionRunNotReady, ProductionRunService, _snapshot_sha256
from app.repair_allowance import used_repairs
from app.voice_qa import (voice_human_review_status, VoiceQaError, generated_voice_qa_parts,
    generation_part_qa, comparison_tokens, _context_reading, _sha256_text, _transcript_sha256)
from app.providers.asr import TranscriptionResult, TranscriptionSegment


class VoiceRepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=500)
    confirmed_action: Literal["regenerate_take"]
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class VoiceRepairPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    run_id: UUID
    fingerprint: str
    action: Literal["regenerate_take", "scoped_fix_required", "replan", "stop"]
    failure_kind: Literal["technical", "copy_qa", "subjective", "unassessed", "invalid_request"]
    stop_reasons: tuple[str, ...]
    evidence: dict[str, JsonValue]
    remaining_run_repairs: int
    responsible_stage: Literal["voice", "unknown"] = "voice"
    assessment_state: Literal["known", "unknown"] = "unknown"
    scope: dict[str, JsonValue] = Field(default_factory=dict)
    preserves: dict[str, JsonValue] = Field(default_factory=dict)
    required_capability: tuple[str, ...] = ()
    max_tts_calls: Literal[1] = 1
    max_qa_asr_calls: Literal[1] = 1
    external_charge_ceiling: Literal["0"] = "0"
    currency: Literal["USD"] = "USD"
    local_compute_cost: None = None
    user_active_minutes: None = None
    execution_granularity: Literal["whole_take", "segment"] = "whole_take"
    review_scope: Literal["fresh_full_copy_qa_and_exact_u_voice"] = "fresh_full_copy_qa_and_exact_u_voice"
    worker_readiness: Literal["not_live_observed"] = "not_live_observed"


def execution_snapshot(db: Database, payload: VoiceGenerationJobPayload) -> dict:
    profile = VoiceProfileRepository(db).get(payload.voice_profile_id)
    capability = ProviderMachineCapabilityProfileRepository(db).get(payload.execution_capability_profile_id) if payload.execution_capability_profile_id else None
    references = []
    for clip_id in (() if profile is None else profile.reference_clip_ids):
        clip = ClipRepository(db).get(clip_id)
        asset = None if clip is None else AssetRepository(db).get(clip.asset_id)
        references.append({"clip": None if clip is None else clip.model_dump(mode="json"),
                           "asset": None if asset is None else asset.model_dump(mode="json")})
    settings = sorted((item.model_dump(mode="json") for item in ProviderMachineSettingRepository(db).list()
                       if capability is not None and item.provider == capability.provider), key=lambda item: item["id"])
    return {"job_payload": payload.model_dump(mode="json"), "profile": None if profile is None else profile.model_dump(mode="json"),
            "capability": None if capability is None else capability.model_dump(mode="json"),
            "references": references, "settings_sha256": _snapshot_sha256(settings)}


def _failed_generation_segments(audio, data_root):
    """Return exact failed generation parts without guessing across the master."""
    parts = generated_voice_qa_parts(audio, data_root)
    generation = audio.metadata.get("voice_generation", {})
    report = generation.get("qa", {})
    saved = report.get("generation_part_reports") if isinstance(report, dict) else None
    if not parts or not isinstance(saved, list) or len(saved) != len(parts):
        raise VoiceQaError("voice_repair_segment_diagnosis_required")
    if (report.get("source_sha256") != audio.content_hash
        or report.get("qa_state") != "failed" or report.get("playable") is not True
        or report.get("transcript_segment_count") != len(audio.transcript_segments)
        or report.get("target_text_sha256") != _sha256_text(generation["target_text"])
        or report.get("transcript_sha256") != _transcript_sha256(audio)
        or report.get("transcription_scope") != "generation_parts"
        or report.get("source_part_count") != len(parts)):
        raise VoiceQaError("voice_repair_segment_qa_binding_changed")
    transcriptions = []
    joined = []
    try:
        for item, (_, offset, _) in zip(saved, parts):
            result = TranscriptionResult(item["transcript_text"],
                tuple(TranscriptionSegment(segment["start_ms"], segment["end_ms"], segment["text"])
                    for segment in item["transcript_segments"]), audio.language)
            transcriptions.append(result)
            joined.extend(TranscriptSegment(start_ms=offset+s.start_ms, end_ms=offset+s.end_ms, text=s.text)
                for s in result.segments)
        joined_tokens = tuple(token for segment in joined for token in comparison_tokens(segment.text))
        master_tokens = tuple(token for segment in audio.transcript_segments
            for token in comparison_tokens(segment.text))
        if joined_tokens != master_tokens:
            raise ValueError()
        # 所有段由同一 QA 设置核验，旧证据不能靠更换阈值变成可复用段。
        settings = {(item["max_silence_ms"], item["max_leading_silence_ms"]) for item in saved}
        if len(settings) != 1:
            raise ValueError()
        silence, leading = next(iter(settings))
        rebuilt = generation_part_qa(audio, parts, transcriptions, provider=report["provider"], model=report["model"],
            max_silence_ms=silence, max_leading_silence_ms=leading)
        if not _generation_part_evidence_matches(rebuilt, saved):
            raise ValueError()
        failures = [item for item in saved if item["report"]["qa_state"] != "verified"]
        known = {"copy_missing_tokens", "copy_duplicate_tokens", "copy_substitution_tokens",
                 "leading_silence_or_unrecognized_audio", "long_silence",
                 "copy_substitutions_within_tolerance", "copy_phonetic_equivalence"}
        actionable = {"copy_missing_tokens", "copy_duplicate_tokens", "copy_substitution_tokens",
                      "leading_silence_or_unrecognized_audio", "long_silence"}
        if (not isinstance(report.get("checks"), list) or not set(report["checks"]) <= known
            or not failures or any(not set(item["report"]["checks"]) <= known for item in failures)):
            raise ValueError()
        if any(not set(item["report"]["checks"]) & actionable or item["report"]["playable"] is not True
               for item in failures):
            raise ValueError()
        failed_indexes = {item["index"] for item in failures}
        segments = []
        for failed in failures:
            segment = generation["generation_segments"][failed["index"]]
            part_copy = generation["target_text"][segment["start_char"]:segment["end_char"]]
            segments.append({
                "index": failed["index"],
                "start_char": segment["start_char"],
                "end_char": segment["end_char"],
                "source_sha256": segment["sha256"],
                "checks": failed["report"]["checks"],
                "missing_token_count": failed["report"]["missing_token_count"],
                "unresolved_substitution_token_count": failed["report"].get(
                    "unresolved_substitution_token_count",
                    failed["report"].get("substitution_token_count", 0),
                ),
                "pronunciation_corrections": _targeted_pronunciation_corrections(
                    part_copy, failed["transcript_text"],
                ),
            })
        return {
            "segments": segments,
            "failed_segment_indexes": sorted(failed_indexes),
            "reused_segment_sha256": [
                item["sha256"] for index, item in enumerate(generation["generation_segments"])
                if index not in failed_indexes
            ],
        }
    except (KeyError, TypeError, ValueError, IndexError):
        raise VoiceQaError("voice_repair_segment_diagnosis_required") from None


def _single_segment_repair(diagnosis: dict) -> dict:
    segments = diagnosis.get("segments") if isinstance(diagnosis, dict) else None
    if not isinstance(segments, list) or len(segments) != 1:
        raise VoiceQaError("voice_repair_segment_diagnosis_required")
    return {
        **segments[0],
        "reused_segment_sha256": diagnosis["reused_segment_sha256"],
    }


def _whole_take_pronunciation_repair(diagnosis: dict) -> dict | None:
    """Batch isolated pronunciation substitutions into one bounded whole-take repair."""
    segments = diagnosis.get("segments") if isinstance(diagnosis, dict) else None
    if not isinstance(segments, list) or len(segments) < 2:
        return None
    corrections = []
    for segment in segments:
        checks = set(segment.get("checks", ()))
        if not checks <= {
            "copy_missing_tokens", "copy_substitution_tokens",
            "copy_phonetic_equivalence",
        } or checks & {"copy_duplicate_tokens"}:
            raise VoiceQaError("voice_repair_segment_diagnosis_required")
        local = segment.get("pronunciation_corrections")
        expected = (
            segment.get("missing_token_count", 0)
            + segment.get("unresolved_substitution_token_count", 0)
        )
        if (not local or type(expected) is not int or expected <= 0
                or len(local) != expected):
            raise VoiceQaError("voice_repair_segment_diagnosis_required")
        for correction in local:
            corrections.append({
                **correction,
                "char_index": segment["start_char"] + correction["char_index"],
            })
    return {
        "failed_segment_indexes": diagnosis["failed_segment_indexes"],
        "pronunciation_corrections": corrections,
    }


def segment_repair_preserves_delivery(payload: VoiceGenerationJobPayload, segment: dict) -> bool:
    """Allow segment replacement only when no whole-copy cue intersects it."""
    if payload.delivery_text is not None:
        return False
    plan = payload.narration_performance_plan
    if plan is None:
        return True
    start, end = segment.get("start_char"), segment.get("end_char")
    if type(start) is not int or type(end) is not int or start >= end:
        return False
    return all(cue.end_char <= start or cue.start_char >= end for cue in plan.cues)


def _targeted_pronunciation_corrections(copy: str, observed: str) -> list[dict]:
    """Force target readings for substituted CJK characters in one failed part."""
    target_tokens = comparison_tokens(copy)
    observed_tokens = comparison_tokens(observed)
    raw = [(match.group(), match.start()) for match in re.finditer(
        r"[a-z0-9]+|[\u4e00-\u9fff]", copy, flags=re.IGNORECASE,
    )]
    if len(raw) != len(target_tokens):
        return []
    corrections = []
    for tag, target_start, target_end, observed_start, observed_end in SequenceMatcher(
        None, target_tokens, observed_tokens, autojunk=False,
    ).get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            return []
        observed_span = observed_tokens[observed_start:observed_end]
        fallback_observed = "".join(observed_span)
        for offset, target in enumerate(target_tokens[target_start:target_end]):
            value = observed_span[offset] if offset < len(observed_span) else fallback_observed
            if target == value:
                continue
            if not re.fullmatch(r"[\u4e00-\u9fff]", target):
                return []
            token_index = target_start + offset
            character, char_index = raw[token_index]
            reading = _contextual_character_reading(copy, char_index) or _context_reading(target)
            if reading is None:
                continue
            observed_reading = (
                _context_reading(value)
                if isinstance(value, str) and re.fullmatch(r"[\u4e00-\u9fff]", value)
                else None
            )
            if observed_reading == reading:
                # The token differs visually but already has the target sound;
                # forcing pinyin here would alter connected speech needlessly.
                continue
            corrections.append({
                "char_index": char_index,
                "character": character,
                "provider_spelling": reading.upper(),
                "target_token": target,
                "observed_token": value,
                "reason": "automatic independent ASR found a target-character substitution",
            })
    return corrections if corrections else []


def _contextual_character_reading(copy: str, char_index: int) -> str | None:
    """Read one target character inside its contiguous CJK phrase."""
    if type(char_index) is not int or not 0 <= char_index < len(copy):
        return None
    start = end = char_index
    while start > 0 and re.fullmatch(r"[\u4e00-\u9fff]", copy[start - 1]):
        start -= 1
    while end + 1 < len(copy) and re.fullmatch(r"[\u4e00-\u9fff]", copy[end + 1]):
        end += 1
    phrase = copy[start:end + 1]
    values = pinyin(
        phrase, style=Style.TONE3, heteronym=False,
        neutral_tone_with_five=True, strict=True,
    )
    position = char_index - start
    if position >= len(values) or len(values[position]) != 1:
        return None
    value = values[position][0]
    return value if isinstance(value, str) and re.fullmatch(r"[a-zv]+[1-5]", value) else None


def apply_segment_pronunciation(provider_text: str, copy: str, corrections: list[dict]) -> str:
    """Apply character-scoped pronunciation hints while preserving provider text."""
    result = provider_text
    for correction in sorted(corrections, key=lambda value: value.get("char_index", -1), reverse=True):
        character = correction.get("character")
        index = correction.get("char_index")
        spelling = correction.get("provider_spelling")
        if (not isinstance(character, str) or len(character) != 1
                or type(index) is not int or index < 0 or index >= len(copy)
                or copy[index] != character
                or not isinstance(spelling, str) or not re.fullmatch(r"[A-Z]+[1-5]", spelling)):
            raise VoiceQaError("voice_repair_pronunciation_correction_invalid")
        term_start = index - 2 if index >= 2 else None
        term = copy[term_start:index + 1] if term_start is not None else ""
        readings = (
            pinyin(term, style=Style.TONE3, heteronym=False,
                   neutral_tone_with_five=True, strict=True)
            if len(term) == 3 and all(re.fullmatch(r"[\u4e00-\u9fff]", char) for char in term)
            and copy.count(term) >= 2
            else []
        )
        if (len(readings) == 3
                and all(isinstance(value, list) and len(value) == 1
                    and re.fullmatch(r"[a-zv]+[1-5]", value[0]) for value in readings)):
            term_occurrence = copy[:term_start].count(term)
            term_positions = [
                match.start() for match in re.finditer(re.escape(term), result)
            ]
            if term_occurrence < len(term_positions):
                position = term_positions[term_occurrence]
                replacement = " ".join(value[0].upper() for value in readings)
                result = result[:position] + replacement + result[position + len(term):]
                continue
        occurrence = copy[:index].count(character)
        positions = [match.start() for match in re.finditer(re.escape(character), result)]
        if occurrence >= len(positions):
            if copy.count(character) > result.count(character):
                # An earlier reviewed homophone alias already replaced this
                # target character. Its sound is bound by the alias; do not
                # fail or force a second, conflicting pinyin control.
                continue
            raise VoiceQaError("voice_repair_pronunciation_correction_missing")
        position = positions[occurrence]
        result = result[:position] + spelling + result[position + 1:]
    result = re.sub(r"(?<=[1-5])(?=[A-Z])", " ", result)
    return result


def _generation_part_evidence_matches(rebuilt: list[dict], saved: list[dict]) -> bool:
    """Compare stable per-part evidence; ASR may refine only its segment seams."""
    if len(rebuilt) != len(saved):
        return False
    for current, previous in zip(rebuilt, saved):
        if not isinstance(current, dict) or not isinstance(previous, dict):
            return False
        current_report = current.get("report")
        previous_report = previous.get("report")
        if not isinstance(current_report, dict) or not isinstance(previous_report, dict):
            return False
        # `observed_segments` and its count preserve the raw ASR seam evidence.
        # Top-level transcript segments may later be refined at real word edges,
        # while transcript_sha256 binds that refined persisted timeline.
        observed_variable = {"observed_segments", "transcript_segment_count"}
        if ({key: value for key, value in current_report.items() if key not in observed_variable}
                != {key: value for key, value in previous_report.items() if key not in observed_variable}):
            return False
        if ({key: value for key, value in current.items() if key != "report"}
                != {key: value for key, value in previous.items() if key != "report"}):
            return False
        raw = previous_report.get("observed_segments")
        persisted = previous.get("transcript_segments")
        if isinstance(raw, list) and isinstance(persisted, list):
            raw_tokens = tuple(token for segment in raw
                for token in comparison_tokens(str(segment.get("text", ""))))
            persisted_tokens = tuple(token for segment in persisted
                for token in comparison_tokens(str(segment.get("text", ""))))
            if raw_tokens != persisted_tokens:
                return False
    return True


class VoiceRepairService:
    def __init__(self, db: Database, data_root: str | Path):
        self.db = db
        self.production = ProductionRunService(db, data_root)

    def _row(self, project: UUID, run: UUID):
        row = self.db.connection.execute("SELECT * FROM production_runs WHERE id = ? AND project_id = ?", (str(run), str(project))).fetchone()
        if row is None:
            raise LookupError("production run not found")
        return row

    def _qa_capability(self, audio, qa):
        """Select the observed local ASR identity, not a private admission tier."""
        generation = None if audio is None else audio.metadata.get("voice_generation")
        report = generation.get("qa") if isinstance(generation, dict) else None
        choices = [item for item in ProviderMachineCapabilityProfileRepository(self.db).list()
                   if item.capability == "asr" and item.mode == "local"]
        if isinstance(report, dict):
            provider, model = report.get("provider"), report.get("model")
            if isinstance(provider, str) and provider and isinstance(model, str) and model:
                choices = [item for item in choices
                           if item.provider == provider and item.model == model]
        receipt = None if qa is None else self.db.connection.execute(
            "SELECT payload FROM voice_execution_receipts WHERE job_id = ?", (str(qa.id),),
        ).fetchone()
        execution = None if receipt is None else json.loads(receipt["payload"])
        if isinstance(execution, dict) and not execution.get("ambiguous"):
            runtime, machine_id = execution.get("runtime"), execution.get("machine_id")
            if isinstance(runtime, str) and runtime and isinstance(machine_id, str) and machine_id:
                choices = [item for item in choices
                           if item.runtime == runtime and item.machine_id == machine_id]
        return choices[0] if len(choices) == 1 else None

    def _budgets(self, project):
        repo = BudgetPolicyRepository(self.db)
        return [None if (p := repo.get_by_project(scope)) is None else p.model_dump(mode="json") for scope in (None, project)]

    def require_execution(self, project: UUID, run: UUID, job: Job, expected: dict):
        row = self._row(project, run)
        if row["stage"] != "voice" or row["voice_job_id"] != str(job.id):
            raise ProductionRunNotReady(("voice_repair_job_no_longer_current",))
        payload = job.payload
        draft = ProjectDraftRepository(self.db).get(project)
        copy = None if draft is None else draft.script or "\n\n".join(s.voice_text for s in sorted(draft.scenes, key=lambda s: s.order))
        # Auto performance plans are frozen in the Voice Job snapshot. Draft
        # keeps an optional user-authored plan, so null there does not mean the
        # generated plan changed. Reject only an explicit conflicting plan.
        if (draft is None or draft.version != row["draft_version"] or draft.script_revision != row["script_revision"]
            or copy != payload.text
            or (draft.narration_performance_plan is not None
                and draft.narration_performance_plan != payload.narration_performance_plan)):
            raise ProductionRunNotReady(("voice_repair_copy_or_intent_changed",))
        current = execution_snapshot(self.db, payload)
        if current != expected:
            raise ProductionRunNotReady(("voice_repair_execution_snapshot_changed",))
        profile, capability = current["profile"], current["capability"]
        if (not profile or not profile["consent"]["confirmed"] or not capability
            or capability["mode"] != "local" or capability["capability"] != "voice"
            or not capability.get("runtime") or not capability.get("machine_id")
            or profile["provider"] != payload.expected_provider
            or capability["provider"] != payload.expected_provider or capability["model"] != payload.expected_model):
            raise ProductionRunNotReady(("voice_repair_local_admission_required",))
        for reference in current["references"]:
            if reference["clip"] is None or reference["asset"] is None or not reference["asset"]["authorization_reference"]:
                raise ProductionRunNotReady(("voice_repair_reference_unavailable",))
            self.production._require_source_bytes(reference["asset"]["source_file"], reference["asset"]["content_hash"])

    def plan(self, project: UUID, run: UUID) -> VoiceRepairPlan:
        row = self._row(project, run)
        job = None if row["voice_job_id"] is None else JobRepository(self.db).get(UUID(row["voice_job_id"]))
        reasons, kind, action = [], "unassessed", "stop"
        segment_repair = None
        whole_take_repair = None
        audio = None if row["voice_audio_id"] is None else AudioAssetRepository(self.db).get(UUID(row["voice_audio_id"]))
        qa = None if row["voice_qa_job_id"] is None else JobRepository(self.db).get(UUID(row["voice_qa_job_id"]))
        snapshot_row = None if job is None else self.db.connection.execute("SELECT payload FROM voice_dispatch_snapshots WHERE job_id = ?", (str(job.id),)).fetchone()
        expected = None if snapshot_row is None else json.loads(snapshot_row["payload"])
        receipt_row = None if job is None else self.db.connection.execute("SELECT payload FROM voice_execution_receipts WHERE job_id = ?", (str(job.id),)).fetchone()
        receipt = None if receipt_row is None else json.loads(receipt_row["payload"])
        if (expected is None or receipt is None or not expected.get("capability")
            or receipt.get("runtime") != expected["capability"]["runtime"]
            or receipt.get("machine_id") != expected["capability"]["machine_id"]
            or receipt.get("ambiguous") or not isinstance(receipt.get("parameters"), dict)):
            reasons.append("voice_repair_original_worker_execution_unavailable")
        remaining = max(0, row["repair_allowance"] - used_repairs(self.db, run))
        if row["stage"] != "voice" or row["render_job_id"] or row["talking_master_audio_id"] or json.loads(row["talking_source_bindings"]):
            reasons.append("voice_repair_requires_unconsumed_voice_stage")
        if remaining == 0:
            reasons.append("run_repair_allowance_exhausted")
        if self.db.connection.execute("SELECT id FROM voice_repairs WHERE run_id = ?", (str(run),)).fetchone():
            reasons.append("voice_take_repair_already_attempted")
        if job is None or not isinstance(job.payload, VoiceGenerationJobPayload) or job.project_id != project:
            reasons.append("voice_repair_generation_mismatch")
        elif job.status == JobStatus.FAILED:
            kind = "technical"
            if job.error_code == "voice_temporarily_unavailable":
                action = "regenerate_take"
            else:
                kind = "invalid_request" if job.error_code == "voice_invalid_request" else "unassessed"
                reasons.append("voice_repair_failure_not_verified_transient")
        elif job.status == JobStatus.COMPLETED and audio is not None and qa is not None:
            generation = audio.metadata.get("voice_generation", {})
            if not isinstance(generation, dict):
                generation = {}
            if (generation.get("job_id") != str(job.id) or generation.get("project_id") != str(project)
                or generation.get("target_text") != job.payload.text or generation.get("provider") != job.payload.expected_provider
                or generation.get("model") != job.payload.expected_model or audio.authorization_reference != job.payload.authorization_reference):
                reasons.append("voice_repair_output_provenance_mismatch")
            if voice_human_review_status(audio) == "approved":
                reasons.append("voice_repair_approved_master_not_replaceable")
            elif generation.get("human_review") is not None or voice_human_review_status(audio) == "rejected":
                kind, action = "subjective", "scoped_fix_required"
                reasons.append("voice_repair_quality_localization_required")
            elif (qa.status == JobStatus.FAILED and qa.error_code == "voice_qa_failed"
                  and qa.type == JobType.VERIFY_VOICE and qa.project_id == project
                  and qa.payload.narration_audio_id == audio.id and qa.payload.target_text == job.payload.text
                  and generation.get("job_id") == str(job.id) and generation.get("project_id") == str(project)
                  and generation.get("target_text") == job.payload.text and generation.get("qa_state") == "failed"):
                report = generation.get("qa", {})
                if not isinstance(report, dict):
                    report = {}
                checks = set(report.get("checks", [])) if isinstance(report, dict) else set()
                copy_checks = {"copy_missing_tokens", "copy_duplicate_tokens", "copy_substitution_tokens"}
                if generation.get("generation_segments"):
                    try:
                        diagnosis = _failed_generation_segments(audio, self.production.data_root)
                        whole_take_repair = _whole_take_pronunciation_repair(diagnosis)
                        if whole_take_repair is None:
                            segment_repair = _single_segment_repair(diagnosis)
                            if not segment_repair_preserves_delivery(job.payload, segment_repair):
                                raise VoiceQaError("voice_repair_segment_delivery_diagnosis_required")
                        kind, action = "copy_qa", "regenerate_take"
                    except VoiceQaError as exc:
                        reasons.append(str(exc))
                elif (report.get("qa_state") == "failed" and report.get("playable") is True
                    and report.get("transcript_segment_count", 0) == len(audio.transcript_segments) > 0
                    and audio.transcript_source == f"{report.get('provider')}:{report.get('model')}"
                    and all(0 <= s.start_ms < s.end_ms <= audio.duration_ms for s in audio.transcript_segments)
                    and checks & copy_checks and checks <= copy_checks | {"copy_substitutions_within_tolerance", "leading_silence_or_unrecognized_audio", "long_silence"}):
                    kind, action = "copy_qa", "regenerate_take"
                else:
                    reasons.append("voice_repair_qa_requires_diagnosis_not_trimming")
            else:
                reasons.append("voice_repair_complete_independent_qa_required")
            try:
                self.production._require_master_bytes(audio.id)
            except ProductionRunNotReady as exc:
                reasons.extend(exc.reasons)
            if generation.get("composition") is not None or any(key in audio.metadata for key in ("voice_composition", "master_narration", "voice_pace_candidate")):
                reasons.append("voice_repair_requires_single_original_take")
        else:
            reasons.append("voice_repair_terminal_dependency_required")
        if job is not None and expected is not None:
            try:
                self.require_execution(project, run, job, expected)
            except ProductionRunNotReady as exc:
                reasons.extend(exc.reasons)
        else:
            reasons.append("voice_repair_original_snapshot_unavailable")
        qa_cap = self._qa_capability(audio, qa)
        qa_execution_row = None if qa is None else self.db.connection.execute(
            "SELECT payload FROM voice_execution_receipts WHERE job_id = ?", (str(qa.id),),
        ).fetchone()
        qa_execution = None if qa_execution_row is None else json.loads(qa_execution_row["payload"])
        if qa_cap is None:
            reasons.append("voice_repair_local_qa_capability_missing_or_ambiguous")
        if kind == "unassessed" and not reasons:
            reasons.append("voice_repair_persisted_failure_required")
        calls = ProviderCallRepository(self.db)
        for scope in (None, project):
            policy = BudgetPolicyRepository(self.db).get_by_project(scope)
            if policy:
                records = calls.list_all() if scope is None else calls.list_for_project(project)
                try:
                    current = enforce_reservation(policy, records, UsageCost(category=CostCategory.VOICE, amount=Decimal("0"), currency="USD"), ignore_existing_unknown_cost=True)
                    if policy.max_calls is not None and current.calls + 2 > policy.max_calls:
                        reasons.append("voice_repair_tts_and_asr_budget_required")
                except BudgetLimitError as exc:
                    reasons.append(f"voice_repair_budget_{exc.code}")
        if reasons and action == "regenerate_take":
            action = "stop"
        evidence = {"job": None if job is None else job.model_dump(mode="json"),
                    "audio": None if audio is None else audio.model_dump(mode="json"),
                    "qa_job": None if qa is None else qa.model_dump(mode="json"),
                    "execution": expected, "worker_execution": receipt, "qa_capability": None if qa_cap is None else qa_cap.model_dump(mode="json"),
                    "qa_worker_execution": qa_execution,
                    "budgets": self._budgets(project), "draft_version": row["draft_version"], "script_revision": row["script_revision"],
                    "segment_repair": segment_repair, "whole_take_repair": whole_take_repair}
        known = kind == "copy_qa" or (kind == "technical" and job is not None and job.error_code == "voice_temporarily_unavailable")
        required = [] if known else ["exact_audio_quality_failure_localization" if kind == "subjective"
            else "voice_request_correction" if kind == "invalid_request" else "exact_audio_failure_diagnosis"]
        if "voice_repair_requires_unconsumed_voice_stage" in reasons:
            required.append("consumed_voice_dependency_repair")
        if "voice_repair_segment_delivery_diagnosis_required" in reasons:
            required.append("voice_generation_segment_delivery_binding")
        scope = {"scene_id": None, "start_ms": 0 if audio else None, "end_ms": None if audio is None else audio.duration_ms}
        if segment_repair is not None:
            part = audio.metadata["voice_generation"]["generation_segments"][segment_repair["index"]]
            start_sample = part.get("final_start_sample", part["start_sample"])
            end_sample = part.get("final_end_sample", part["end_sample"])
            scope.update(start_ms=start_sample * 1000 // audio.sample_rate,
                end_ms=(end_sample * 1000 + audio.sample_rate - 1) // audio.sample_rate,
                generation_segment_index=segment_repair["index"], start_char=part["start_char"], end_char=part["end_char"])
        elif whole_take_repair is not None:
            scope["failed_generation_segment_indexes"] = whole_take_repair["failed_segment_indexes"]
        draft = ProjectDraftRepository(self.db).get(project)
        bindings = json.loads(row["talking_source_bindings"])
        preserves = {"master_audio_id": None if audio is None else str(audio.id),
            "master_audio_sha256": None if audio is None else audio.content_hash,
            "voice_job_id": row["voice_job_id"], "voice_qa_job_id": row["voice_qa_job_id"], "render_job_id": row["render_job_id"],
            "talking_job_ids": [binding["talking_job_id"] for binding in bindings if binding.get("talking_job_id")],
            "talking_output_asset_ids": [binding["talking_output_asset_id"] for binding in bindings if binding.get("talking_output_asset_id")],
            "scene_plan_ids": [] if draft is None or draft.version != row["draft_version"] or draft.script_revision != row["script_revision"]
                else [str(scene.id) for scene in draft.scenes],
            "reused_segment_sha256": [] if segment_repair is None else segment_repair["reused_segment_sha256"],
            "reuse_admission": "unchanged_dependencies_only"}
        values = dict(project_id=project, run_id=run, action=action, failure_kind=kind,
                      responsible_stage="voice", assessment_state="known" if known else "unknown", scope=scope,
                      preserves=preserves, required_capability=tuple(required),
                      stop_reasons=tuple(dict.fromkeys(reasons)), evidence=evidence, remaining_run_repairs=remaining,
                      execution_granularity="segment" if segment_repair is not None else "whole_take")
        draft_plan = VoiceRepairPlan(fingerprint="0" * 64, **values)
        return draft_plan.model_copy(update={"fingerprint": _snapshot_sha256(draft_plan.model_dump(mode="json", exclude={"fingerprint"}))})

    def records(self, project: UUID, run: UUID) -> list[dict]:
        self._row(project, run)
        rows = self.db.connection.execute("SELECT * FROM voice_repairs WHERE project_id = ? AND run_id = ? ORDER BY rowid", (str(project), str(run))).fetchall()
        calls = ProviderCallRepository(self.db).list_for_project(project)
        result = []
        for row in rows:
            record = json.loads(row["payload"])
            current = JobRepository(self.db).get(UUID(row["replacement_job_id"]))
            outputs = [a.model_dump(mode="json") for a in AudioAssetRepository(self.db).list() if isinstance(a.metadata.get("voice_generation"), dict) and a.metadata["voice_generation"].get("job_id") == row["replacement_job_id"]]
            record.update(replacement_job_status=None if current is None else current.status.value,
                          successor_audio=outputs, successor_qa_job_id=row["qa_job_id"],
                          provider_calls=[c.model_dump(mode="json") for c in calls if c.idempotency_key.startswith(f"job:{row['replacement_job_id']}:tts:") or row["qa_job_id"] and c.idempotency_key.startswith(f"job:{row['qa_job_id']}:asr:")],
                          predecessor_provider_calls=[c.model_dump(mode="json") for c in calls if any(c.idempotency_key.startswith(f"job:{j}:") for j in record["old_job_ids"] if j)])
            result.append(record)
        return result

    def execute(self, project: UUID, run: UUID, request: VoiceRepairRequest) -> dict:
        with self.db.transaction(immediate=True):
            digest = _snapshot_sha256(request.model_dump(mode="json"))
            prior = self.db.connection.execute("SELECT * FROM voice_repairs WHERE project_id = ? AND idempotency_key = ?", (str(project), request.idempotency_key)).fetchone()
            if prior:
                if prior["run_id"] != str(run) or prior["request_fingerprint"] != digest:
                    raise ProductionRunConflict("voice_repair_idempotency_conflict")
                return json.loads(prior["payload"])
            plan = self.plan(project, run)
            if plan.fingerprint != request.expected_fingerprint:
                raise ProductionRunConflict("voice_repair_plan_stale")
            if plan.action != "regenerate_take" or plan.assessment_state != "known":
                raise ProductionRunNotReady(plan.stop_reasons)
            row = self._row(project, run)
            original = JobRepository(self.db).get(UUID(row["voice_job_id"]))
            now, repair_id = datetime.now(timezone.utc), uuid4()
            job = Job(project_id=project, type=JobType.GENERATE_VOICE, payload=original.payload,
                      idempotency_key=f"voice-repair:{repair_id}", created_at=now, updated_at=now)
            JobRepository(self.db).create(job)
            record = {"id": str(repair_id), "project_id": str(project), "run_id": str(run),
                      "idempotency_key": request.idempotency_key, "reason": request.reason, "plan": plan.model_dump(mode="json"),
                      "old_job_ids": [row["voice_job_id"], row["voice_qa_job_id"]], "old_audio_id": row["voice_audio_id"],
                      "replacement_job_id": str(job.id), "created_at": now.isoformat()}
            self.db.connection.execute("INSERT INTO voice_repairs(id,project_id,run_id,idempotency_key,request_fingerprint,replacement_job_id,payload) VALUES (?,?,?,?,?,?,?)",
                (str(repair_id), str(project), str(run), request.idempotency_key, digest, str(job.id), json.dumps(record)))
            self.db.connection.execute("INSERT INTO voice_dispatch_snapshots(job_id,payload) VALUES (?,?)", (str(job.id), json.dumps(plan.evidence["execution"])))
            self.db.connection.execute("UPDATE production_runs SET voice_job_id = ?, voice_audio_id = NULL, voice_qa_job_id = NULL, voice_qa_auto_stop_reasons = '[]' WHERE id = ? AND stage = 'voice'", (str(job.id), str(run)))
            return record

    def reconcile_automatic_qa_failure(self, project: UUID, run: UUID):
        """普通制作只自动修正一个已定位的坏段；其它失败保留诊断并停止。"""
        row = self._row(project, run)
        if not row["automatic"] or row["stage"] != "voice":
            return None
        if json.loads(row["voice_qa_auto_stop_reasons"]) or any(
                item.startswith("automatic_stop:") for item in json.loads(row["waiting_stop_reasons"])):
            return None
        qa = None if row["voice_qa_job_id"] is None else JobRepository(self.db).get(UUID(row["voice_qa_job_id"]))
        if qa is None or qa.status is not JobStatus.FAILED or qa.error_code != "voice_qa_failed":
            return None
        plan = self.plan(project, run)
        if plan.action == "regenerate_take" and plan.execution_granularity == "segment":
            return self.execute(project, run, VoiceRepairRequest(expected_fingerprint=plan.fingerprint,
                idempotency_key=f"automatic-voice-segment-repair:{qa.id}", confirmed_action="regenerate_take",
                reason="独立分段核验已定位一段缺陷；只重做该段，复用其余原始 PCM 并重新核验全稿"))
        if (plan.action == "regenerate_take" and plan.execution_granularity == "whole_take"
                and plan.evidence.get("whole_take_repair")):
            return self.execute(project, run, VoiceRepairRequest(expected_fingerprint=plan.fingerprint,
                idempotency_key=f"automatic-voice-pronunciation-repair:{qa.id}", confirmed_action="regenerate_take",
                reason="独立分段核验定位到多个隔离的字级读音替换；用目标字符级控制重做一次并重新核验全稿"))
        reasons = plan.stop_reasons or ("voice_repair_segment_diagnosis_required",)
        with self.db.transaction(immediate=True):
            current = self.db.connection.execute("SELECT * FROM production_runs WHERE id = ? AND project_id = ?",
                (str(run), str(project))).fetchone()
            if current is None or dict(current) != dict(row) or JobRepository(self.db).get(qa.id) != qa:
                return None
            self.db.connection.execute("UPDATE production_runs SET voice_qa_auto_stop_reasons=? WHERE id=? AND stage='voice'",
                (json.dumps(list(reasons)), str(run)))
        return None


def voice_segment_repair_context(db: Database, job: Job, data_root: Path | None):
    """执行前复核原稿、逐段事实和全部复用 PCM；全稿 payload 始终不变。"""
    row = db.connection.execute("SELECT payload FROM voice_repairs WHERE replacement_job_id=?", (str(job.id),)).fetchone()
    if row is None:
        return None
    record = json.loads(row["payload"])
    plan = record["plan"]
    if plan.get("execution_granularity") != "segment":
        return None
    try:
        if data_root is None:
            raise ValueError()
        audio = AudioAssetRepository(db).get(UUID(record["old_audio_id"]))
        if audio is None or audio.model_dump(mode="json") != plan["evidence"]["audio"]:
            raise ValueError()
        segment = _single_segment_repair(_failed_generation_segments(audio, Path(data_root)))
        if segment != plan["evidence"]["segment_repair"] or audio.metadata["voice_generation"]["target_text"] != job.payload.text:
            raise ValueError()
        return {"audio": audio, "segment": segment, "parts": generated_voice_qa_parts(audio, Path(data_root)),
                "repair_id": record["id"]}
    except (ValueError, TypeError, KeyError, OSError):
        raise ProductionRunNotReady(("voice_repair_segment_sources_changed",)) from None


def voice_whole_take_pronunciation_corrections(
    db: Database, job: Job, data_root: Path | None,
) -> list[dict]:
    """Revalidate a batched pronunciation repair against exact predecessor evidence."""
    row = db.connection.execute(
        "SELECT payload FROM voice_repairs WHERE replacement_job_id=?", (str(job.id),),
    ).fetchone()
    if row is None:
        return []
    record = json.loads(row["payload"])
    plan = record["plan"]
    expected = plan["evidence"].get("whole_take_repair")
    if plan.get("execution_granularity") != "whole_take" or not isinstance(expected, dict):
        return []
    try:
        if data_root is None:
            raise ValueError()
        audio = AudioAssetRepository(db).get(UUID(record["old_audio_id"]))
        if (audio is None or audio.model_dump(mode="json") != plan["evidence"]["audio"]
                or audio.metadata["voice_generation"]["target_text"] != job.payload.text):
            raise ValueError()
        current = _whole_take_pronunciation_repair(
            _failed_generation_segments(audio, Path(data_root)),
        )
        if current != expected:
            raise ValueError()
        return current["pronunciation_corrections"]
    except (ValueError, TypeError, KeyError, OSError, VoiceQaError):
        raise ProductionRunNotReady(("voice_repair_pronunciation_sources_changed",)) from None


def recompose_voice_segment_result(context, result, *, data_root: Path):
    """只拼接完整 PCM 段；不裁音、不变速、不扩写，并保留确切来源。"""
    if context is None:
        return result
    old = context["audio"]
    generation = old.metadata["voice_generation"]
    index = context["segment"]["index"]
    replacement = Path(result.audio_path).resolve()
    root = Path(data_root).resolve()
    output = replacement.with_name(replacement.stem + ".repaired-master.wav")
    try:
        if not replacement.is_relative_to(root) or not replacement.is_file() or output.exists():
            raise ValueError()
        with wave.open(str(context["parts"][0][0]), "rb") as reference:
            format_ = reference.getnchannels(), reference.getsampwidth(), reference.getframerate(), reference.getcomptype()
        if format_[1] != 2 or format_[3] != "NONE":
            raise ValueError()
        paths = [replacement if i == index else source for i, (source, _, _) in enumerate(context["parts"])]
        segments, cursor = [], 0
        with output.open("xb") as destination:
            with wave.open(destination, "wb") as merged:
                merged.setnchannels(format_[0]); merged.setsampwidth(format_[1]); merged.setframerate(format_[2])
                for i, (path, previous) in enumerate(zip(paths, generation["generation_segments"])):
                    with wave.open(str(path), "rb") as source:
                        if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != format_:
                            raise ValueError()
                        count = source.getnframes()
                        if count <= 0 or cursor + count > format_[2] * 600:
                            raise ValueError()
                        digest = hashlib.sha256(path.read_bytes()).hexdigest()
                        if i != index and digest != previous["sha256"]:
                            raise ValueError()
                        if i == index and digest == previous["sha256"]:
                            raise ValueError()
                        pcm = source.readframes(count)
                        if len(pcm) != count * format_[0] * format_[1]:
                            raise ValueError()
                        merged.writeframes(pcm)
                    segments.append({"start_char": previous["start_char"], "end_char": previous["end_char"],
                        "start_sample": cursor, "end_sample": cursor+count,
                        "source_file": path.relative_to(root).as_posix(), "sha256": digest})
                    cursor += count
        return replace(result, audio_path=output, generation_segments=tuple(segments))
    except (OSError, ValueError, wave.Error):
        # 失败文件留证；不注册、不让后续消费、不再次调用生成器。
        raise ProductionRunNotReady(("voice_repair_segment_recomposition_invalid",)) from None


def require_voice_repair_call(db: Database, job: Job, *, operation: str, known_local_cost: bool, provider: str, model: str, data_root: Path, worker_execution: dict | None):
    row = db.connection.execute("SELECT * FROM voice_repairs WHERE replacement_job_id = ? OR qa_job_id = ?", (str(job.id), str(job.id))).fetchone()
    if row is None:
        return
    record = json.loads(row["payload"])
    service = VoiceRepairService(db, data_root)
    current = service._row(UUID(row["project_id"]), UUID(row["run_id"]))
    original = JobRepository(db).get(UUID(row["replacement_job_id"]))
    service.require_execution(UUID(row["project_id"]), UUID(row["run_id"]), original, record["plan"]["evidence"]["execution"])
    if operation == "tts":
        voice_segment_repair_context(db, original, data_root)
        voice_whole_take_pronunciation_corrections(db, original, data_root)
    expected = record["plan"]["evidence"]["execution"]["capability"] if operation == "tts" else record["plan"]["evidence"]["qa_capability"]
    expected_worker = (record["plan"]["evidence"]["worker_execution"] if operation == "tts"
        else record["plan"]["evidence"].get("qa_worker_execution"))
    if (expected is None or worker_execution is None
        or worker_execution.get("runtime") != expected["runtime"]
        or worker_execution.get("machine_id") != expected["machine_id"]
        or expected_worker is not None and worker_execution != expected_worker):
        raise ProductionRunNotReady(("voice_repair_worker_execution_changed_or_unknown",))
    if operation not in {"tts", "asr"} or not known_local_cost or job.project_id != UUID(row["project_id"]) or provider != expected["provider"] or model != expected["model"]:
        raise ProductionRunNotReady(("voice_repair_external_or_changed_execution_not_authorized",))
    if operation == "asr":
        cap = ProviderMachineCapabilityProfileRepository(db).get(UUID(expected["id"]))
        identity = None if cap is None else cap.model_dump(mode="json")
        identity_fields = ("id", "capability", "mode", "provider", "model", "runtime", "machine_id")
        if (identity is None or any(identity.get(field) != expected.get(field) for field in identity_fields)
            or current["voice_qa_job_id"] != str(job.id)):
            raise ProductionRunNotReady(("voice_repair_qa_admission_changed",))
        audio = AudioAssetRepository(db).get(job.payload.narration_audio_id)
        if (audio is None or current["voice_audio_id"] != str(audio.id)
            or job.payload.target_text != original.payload.text
            or not isinstance(audio.metadata.get("voice_generation"), dict)
            or audio.metadata["voice_generation"].get("job_id") != str(original.id)):
            raise ProductionRunNotReady(("voice_repair_qa_candidate_changed",))
        service.production._require_master_bytes(audio.id)
    prefix = f"job:{job.id}:{operation}:"
    if any(c.idempotency_key.startswith(prefix) for c in ProviderCallRepository(db).list_for_project(job.project_id)):
        raise ProductionRunNotReady(("voice_repair_call_limit",))

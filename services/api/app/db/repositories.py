"""Explicit repositories for persisted domain models."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Generic, TypeVar
from uuid import UUID

from app.domain.models import AccountConnection, AnalysisResultBundle, Asset, AssetUsageEvent, AudioAsset, BudgetPolicy, Clip, ContentFeedback, ContentOpportunity, HistoricalContent, ImageAsset, IPProfile, Job, Project, ProjectDraft, ProjectDraftRevision, ProviderCallRecord, PublicationRecord, ShootTask, TalkingProfile, VoiceProfile

from .database import Database

ModelT = TypeVar("ModelT")


def _payload(model: ModelT) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))  # type: ignore[attr-defined]


def _model(row: sqlite3.Row, cls: type[ModelT]) -> ModelT:
    return cls.model_validate(json.loads(row["payload"]))  # type: ignore[attr-defined]


def _utc_timestamp(value: datetime) -> str:
    """Use a fixed-width UTC encoding that sorts lexicographically by time."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _job_payload(job: Job) -> str:
    """Serialize Job timestamps with the same fixed-width UTC encoding as columns."""
    data = job.model_dump(mode="json")
    data["created_at"] = _utc_timestamp(job.created_at)
    data["updated_at"] = _utc_timestamp(job.updated_at)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _Repository(Generic[ModelT]):
    table: str
    model: type[ModelT]

    def __init__(self, db: Database):
        self.db = db

    def get(self, item_id: UUID) -> ModelT | None:
        row = self.db.connection.execute(f"SELECT * FROM {self.table} WHERE id = ?", (str(item_id),)).fetchone()
        return None if row is None else _model(row, self.model)

    def list(self) -> list[ModelT]:
        rows = self.db.connection.execute(f"SELECT * FROM {self.table} ORDER BY rowid").fetchall()
        return [_model(row, self.model) for row in rows]

    def delete(self, item_id: UUID) -> bool:
        cursor = self.db.connection.execute(f"DELETE FROM {self.table} WHERE id = ?", (str(item_id),))
        return cursor.rowcount == 1


class IPProfileRepository(_Repository[IPProfile]):
    table, model = "ip_profiles", IPProfile

    def create(self, value: IPProfile) -> IPProfile:
        self.db.connection.execute("INSERT INTO ip_profiles(id, payload) VALUES (?, ?)", (str(value.id), _payload(value)))
        self.db.connection.execute(
            "INSERT INTO ip_profile_revisions(profile_id, version, created_at, payload) VALUES (?, 1, ?, ?)",
            (str(value.id), _utc_timestamp(datetime.now(timezone.utc)), _payload(value)),
        )
        return value

    def update(self, value: IPProfile) -> IPProfile:
        cursor = self.db.connection.execute("UPDATE ip_profiles SET payload = ? WHERE id = ?", (_payload(value), str(value.id)))
        if cursor.rowcount != 1:
            raise KeyError(value.id)
        current = self.db.connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM ip_profile_revisions WHERE profile_id = ?",
            (str(value.id),),
        ).fetchone()
        next_version = int(current["version"]) + 1
        self.db.connection.execute(
            "INSERT INTO ip_profile_revisions(profile_id, version, created_at, payload) VALUES (?, ?, ?, ?)",
            (str(value.id), next_version, _utc_timestamp(datetime.now(timezone.utc)), _payload(value)),
        )
        return value

    def revisions(self, profile_id: UUID) -> list[tuple[int, datetime, IPProfile]]:
        rows = self.db.connection.execute(
            "SELECT version, created_at, payload FROM ip_profile_revisions WHERE profile_id = ? ORDER BY version",
            (str(profile_id),),
        ).fetchall()
        return [(int(row["version"]), datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")), IPProfile.model_validate(json.loads(row["payload"]))) for row in rows]


class ProjectRepository(_Repository[Project]):
    table, model = "projects", Project

    def create(self, value: Project) -> Project:
        self.db.connection.execute("INSERT INTO projects(id, ip_profile_id, payload) VALUES (?, ?, ?)", (str(value.id), str(value.ip_profile_id), _payload(value)))
        return value

    def update(self, value: Project) -> Project:
        cursor = self.db.connection.execute("UPDATE projects SET ip_profile_id = ?, payload = ? WHERE id = ?", (str(value.ip_profile_id), _payload(value), str(value.id)))
        if cursor.rowcount != 1:
            raise KeyError(value.id)
        return value


class ProjectDraftRepository(_Repository[ProjectDraft]):
    table, model = "project_drafts", ProjectDraft

    def get(self, project_id: UUID) -> ProjectDraft | None:
        row = self.db.connection.execute(
            "SELECT * FROM project_drafts WHERE project_id = ?", (str(project_id),)
        ).fetchone()
        return None if row is None else _model(row, self.model)

    def save(self, value: ProjectDraft) -> ProjectDraft:
        if value.version <= 0:
            raise ValueError("persisted project drafts require a positive version")
        self.db.connection.execute(
            """INSERT INTO project_drafts(project_id, version, updated_at, payload)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(project_id) DO UPDATE SET
                 version=excluded.version, updated_at=excluded.updated_at, payload=excluded.payload""",
            (str(value.project_id), value.version, _utc_timestamp(value.updated_at), _payload(value)),
        )
        revision = ProjectDraftRevision(
            project_id=value.project_id,
            version=value.version,
            script_revision=value.script_revision,
            script=value.script,
            topic=value.topic,
            ip_profile_version=value.ip_profile_version,
            evidence_refs=value.evidence_refs,
            input_fingerprint=value.input_fingerprint,
            invalidation_reasons=value.invalidation_reasons,
            created_at=value.updated_at,
        )
        self.db.connection.execute(
            """INSERT INTO project_draft_revisions(project_id, version, script_revision, created_at, payload)
               VALUES (?, ?, ?, ?, ?)""",
            (
                str(value.project_id),
                value.version,
                value.script_revision,
                _utc_timestamp(value.updated_at),
                _payload(revision),
            ),
        )
        return value

    def revisions(self, project_id: UUID) -> list[ProjectDraftRevision]:
        rows = self.db.connection.execute(
            "SELECT payload FROM project_draft_revisions WHERE project_id = ? ORDER BY version",
            (str(project_id),),
        ).fetchall()
        return [ProjectDraftRevision.model_validate(json.loads(row["payload"])) for row in rows]


class ShootTaskRepository(_Repository[ShootTask]):
    table, model = "shoot_tasks", ShootTask

    def get_by_scene(self, project_id: UUID, scene_plan_id: UUID) -> ShootTask | None:
        row = self.db.connection.execute(
            "SELECT * FROM shoot_tasks WHERE project_id = ? AND scene_plan_id = ?",
            (str(project_id), str(scene_plan_id)),
        ).fetchone()
        return None if row is None else _model(row, self.model)

    def create(self, value: ShootTask) -> ShootTask:
        self.db.connection.execute(
            "INSERT INTO shoot_tasks(id, project_id, scene_plan_id, status, asset_id, created_at, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(value.id), str(value.project_id), str(value.scene_plan_id), value.status,
                None if value.asset_id is None else str(value.asset_id),
                _utc_timestamp(value.created_at), _utc_timestamp(value.updated_at), _payload(value),
            ),
        )
        return value

    def update(self, value: ShootTask) -> ShootTask:
        cursor = self.db.connection.execute(
            "UPDATE shoot_tasks SET status = ?, asset_id = ?, updated_at = ?, payload = ? WHERE id = ?",
            (
                value.status, None if value.asset_id is None else str(value.asset_id),
                _utc_timestamp(value.updated_at), _payload(value), str(value.id),
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError(value.id)
        return value

    def list_for_project(self, project_id: UUID) -> list[ShootTask]:
        rows = self.db.connection.execute(
            "SELECT * FROM shoot_tasks WHERE project_id = ? ORDER BY created_at, id", (str(project_id),)
        ).fetchall()
        return [_model(row, self.model) for row in rows]


class AnalysisResultRepository(_Repository[AnalysisResultBundle]):
    table, model = "analysis_result_bundles", AnalysisResultBundle

    def get_by_input_hash(self, input_hash: str) -> AnalysisResultBundle | None:
        row = self.db.connection.execute(
            "SELECT * FROM analysis_result_bundles WHERE input_hash = ?", (input_hash,)
        ).fetchone()
        return None if row is None else _model(row, self.model)

    def create(self, value: AnalysisResultBundle) -> AnalysisResultBundle:
        self.db.connection.execute(
            "INSERT INTO analysis_result_bundles(id, input_hash, mode, analyzed_at, payload) VALUES (?, ?, ?, ?, ?)",
            (str(value.id), value.input_hash, value.mode, _utc_timestamp(value.analyzed_at), _payload(value)),
        )
        return value


class ImageAssetRepository(_Repository[ImageAsset]):
    table, model = "image_assets", ImageAsset

    def create(self, value: ImageAsset) -> ImageAsset:
        self.db.connection.execute(
            "INSERT INTO image_assets(id, content_hash, payload) VALUES (?, ?, ?)",
            (str(value.id), value.content_hash, _payload(value)),
        )
        return value

    def get_by_content_hash(self, content_hash: str) -> ImageAsset | None:
        row = self.db.connection.execute(
            "SELECT * FROM image_assets WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return None if row is None else _model(row, self.model)


class AudioAssetRepository(_Repository[AudioAsset]):
    table, model = "audio_assets", AudioAsset

    def create(self, value: AudioAsset) -> AudioAsset:
        self.db.connection.execute(
            "INSERT INTO audio_assets(id, content_hash, payload) VALUES (?, ?, ?)",
            (str(value.id), value.content_hash, _payload(value)),
        )
        return value

    def update(self, value: AudioAsset) -> AudioAsset:
        existing = self.db.connection.execute("SELECT content_hash FROM audio_assets WHERE id = ?", (str(value.id),)).fetchone()
        if existing is None:
            raise KeyError(value.id)
        if existing["content_hash"] != value.content_hash:
            raise ValueError("audio asset content_hash is immutable")
        cursor = self.db.connection.execute(
            "UPDATE audio_assets SET content_hash = ?, payload = ? WHERE id = ?",
            (value.content_hash, _payload(value), str(value.id)),
        )
        if cursor.rowcount != 1:
            raise KeyError(value.id)
        return value

    def get_by_content_hash(self, content_hash: str) -> AudioAsset | None:
        row = self.db.connection.execute(
            "SELECT * FROM audio_assets WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return None if row is None else _model(row, self.model)


class AssetUsageRepository(_Repository[AssetUsageEvent]):
    table, model = "asset_usage_events", AssetUsageEvent

    def get_by_event_key(self, event_key: str) -> AssetUsageEvent | None:
        row = self.db.connection.execute(
            "SELECT * FROM asset_usage_events WHERE event_key = ?", (event_key,)
        ).fetchone()
        return None if row is None else _model(row, self.model)

    def create(self, value: AssetUsageEvent) -> AssetUsageEvent:
        self.db.connection.execute(
            "INSERT INTO asset_usage_events(id, project_id, event_key, payload) VALUES (?, ?, ?, ?)",
            (str(value.id), str(value.project_id), value.event_key, _payload(value)),
        )
        return value

    def list_for_project(self, project_id: UUID) -> list[AssetUsageEvent]:
        rows = self.db.connection.execute(
            "SELECT * FROM asset_usage_events WHERE project_id = ? ORDER BY rowid", (str(project_id),)
        ).fetchall()
        return [_model(row, self.model) for row in rows]


class PublicationRepository(_Repository[PublicationRecord]):
    table, model = "publication_records", PublicationRecord

    def get_by_key(self, project_id: UUID, output_version: str, platform: str) -> PublicationRecord | None:
        row = self.db.connection.execute(
            "SELECT * FROM publication_records WHERE project_id = ? AND output_version = ? AND platform = ?",
            (str(project_id), output_version, platform),
        ).fetchone()
        return None if row is None else _model(row, self.model)

    def create(self, value: PublicationRecord) -> PublicationRecord:
        self.db.connection.execute(
            "INSERT INTO publication_records(id, project_id, output_version, platform, payload) VALUES (?, ?, ?, ?, ?)",
            (str(value.id), str(value.project_id), value.output_version, value.platform, _payload(value)),
        )
        return value

    def list_for_project(self, project_id: UUID) -> list[PublicationRecord]:
        rows = self.db.connection.execute(
            "SELECT * FROM publication_records WHERE project_id = ? ORDER BY rowid", (str(project_id),)
        ).fetchall()
        return [_model(row, self.model) for row in rows]


class FeedbackRepository(_Repository[ContentFeedback]):
    table, model = "content_feedback", ContentFeedback

    def get_by_key(self, project_id: UUID, output_version: str) -> ContentFeedback | None:
        row = self.db.connection.execute(
            "SELECT * FROM content_feedback WHERE project_id = ? AND output_version = ?",
            (str(project_id), output_version),
        ).fetchone()
        return None if row is None else _model(row, self.model)

    def create(self, value: ContentFeedback) -> ContentFeedback:
        self.db.connection.execute(
            "INSERT INTO content_feedback(id, project_id, output_version, payload) VALUES (?, ?, ?, ?)",
            (str(value.id), str(value.project_id), value.output_version, _payload(value)),
        )
        return value

    def list_for_project(self, project_id: UUID) -> list[ContentFeedback]:
        rows = self.db.connection.execute(
            "SELECT * FROM content_feedback WHERE project_id = ? ORDER BY rowid", (str(project_id),)
        ).fetchall()
        return [_model(row, self.model) for row in rows]


class ContentOpportunityRepository(_Repository[ContentOpportunity]):
    table, model = "content_opportunities", ContentOpportunity

    def get_by_dedupe_key(self, dedupe_key: str) -> ContentOpportunity | None:
        row = self.db.connection.execute(
            "SELECT * FROM content_opportunities WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        return None if row is None else _model(row, self.model)

    def create(self, value: ContentOpportunity) -> ContentOpportunity:
        self.db.connection.execute(
            "INSERT INTO content_opportunities(id, dedupe_key, status, created_at, payload) VALUES (?, ?, ?, ?, ?)",
            (str(value.id), value.dedupe_key, value.status, _utc_timestamp(value.created_at), _payload(value)),
        )
        return value

    def update(self, value: ContentOpportunity) -> ContentOpportunity:
        cursor = self.db.connection.execute(
            "UPDATE content_opportunities SET status = ?, payload = ? WHERE id = ?",
            (value.status, _payload(value), str(value.id)),
        )
        if cursor.rowcount != 1:
            raise KeyError(value.id)
        return value

    def list_for_planning(self, limit: int = 20) -> list[ContentOpportunity]:
        rows = self.db.connection.execute(
            """SELECT * FROM content_opportunities
               WHERE status != 'dismissed'
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [_model(row, self.model) for row in rows]

    def list_all(self, status: str | None = None) -> list[ContentOpportunity]:
        if status is None:
            rows = self.db.connection.execute(
                "SELECT * FROM content_opportunities ORDER BY created_at DESC, id DESC"
            ).fetchall()
        else:
            rows = self.db.connection.execute(
                "SELECT * FROM content_opportunities WHERE status = ? ORDER BY created_at DESC, id DESC",
                (status,),
            ).fetchall()
        return [_model(row, self.model) for row in rows]


class AccountConnectionRepository(_Repository[AccountConnection]):
    table, model = "account_connections", AccountConnection

    def get_by_key(self, provider: str, account_external_id: str) -> AccountConnection | None:
        row = self.db.connection.execute(
            "SELECT * FROM account_connections WHERE provider = ? AND account_external_id = ?",
            (provider, account_external_id),
        ).fetchone()
        return None if row is None else _model(row, self.model)

    def create(self, value: AccountConnection) -> AccountConnection:
        self.db.connection.execute(
            "INSERT INTO account_connections(id, provider, account_external_id, payload) VALUES (?, ?, ?, ?)",
            (str(value.id), value.provider, value.account_external_id, _payload(value)),
        )
        return value


class HistoricalContentRepository(_Repository[HistoricalContent]):
    table, model = "historical_content", HistoricalContent

    def get_by_key(self, account_connection_id: UUID, external_id: str) -> HistoricalContent | None:
        row = self.db.connection.execute(
            "SELECT * FROM historical_content WHERE account_connection_id = ? AND external_id = ?",
            (str(account_connection_id), external_id),
        ).fetchone()
        return None if row is None else _model(row, self.model)

    def create(self, value: HistoricalContent) -> HistoricalContent:
        self.db.connection.execute(
            "INSERT INTO historical_content(id, account_connection_id, external_id, payload) VALUES (?, ?, ?, ?)",
            (str(value.id), str(value.account_connection_id), value.external_id, _payload(value)),
        )
        return value

    def list_for_account(self, account_connection_id: UUID) -> list[HistoricalContent]:
        rows = self.db.connection.execute(
            "SELECT * FROM historical_content WHERE account_connection_id = ? ORDER BY rowid",
            (str(account_connection_id),),
        ).fetchall()
        return [_model(row, self.model) for row in rows]


class VoiceProfileRepository(_Repository[VoiceProfile]):
    table, model = "voice_profiles", VoiceProfile

    def get_by_provider_profile(self, provider: str, provider_profile_id: str) -> VoiceProfile | None:
        rows = self.db.connection.execute(
            "SELECT * FROM voice_profiles WHERE json_extract(payload, '$.provider') = ? AND json_extract(payload, '$.provider_profile_id') = ?",
            (provider, provider_profile_id),
        ).fetchall()
        return None if not rows else _model(rows[0], self.model)

    def create(self, value: VoiceProfile) -> VoiceProfile:
        self.db.connection.execute(
            "INSERT INTO voice_profiles(id, payload) VALUES (?, ?)", (str(value.id), _payload(value))
        )
        return value


class TalkingProfileRepository(_Repository[TalkingProfile]):
    table, model = "talking_profiles", TalkingProfile

    def create(self, value: TalkingProfile) -> TalkingProfile:
        self.db.connection.execute(
            "INSERT INTO talking_profiles(id, payload) VALUES (?, ?)", (str(value.id), _payload(value))
        )
        return value

class BudgetPolicyRepository(_Repository[BudgetPolicy]):
    table, model = "budget_policies", BudgetPolicy

    def get_by_project(self, project_id: UUID | None) -> BudgetPolicy | None:
        scope_key = "global" if project_id is None else str(project_id)
        row = self.db.connection.execute(
            "SELECT * FROM budget_policies WHERE scope_key = ?", (scope_key,)
        ).fetchone()
        return None if row is None else _model(row, self.model)

    def save(self, value: BudgetPolicy) -> BudgetPolicy:
        scope_key = "global" if value.project_id is None else str(value.project_id)
        self.db.connection.execute(
            """INSERT INTO budget_policies(id, scope_key, payload) VALUES (?, ?, ?)
               ON CONFLICT(scope_key) DO UPDATE SET id=excluded.id, payload=excluded.payload""",
            (str(value.id), scope_key, _payload(value)),
        )
        return value


class ProviderCallRepository(_Repository[ProviderCallRecord]):
    table, model = "provider_call_records", ProviderCallRecord

    def get_by_idempotency_key(self, idempotency_key: str) -> ProviderCallRecord | None:
        row = self.db.connection.execute(
            "SELECT * FROM provider_call_records WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return None if row is None else _model(row, self.model)

    def create(self, value: ProviderCallRecord) -> ProviderCallRecord:
        self.db.connection.execute(
            """INSERT INTO provider_call_records(
                   id, project_id, idempotency_key, status, created_at, completed_at, payload
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                str(value.id), str(value.project_id), value.idempotency_key, value.status,
                _utc_timestamp(value.created_at),
                None if value.completed_at is None else _utc_timestamp(value.completed_at),
                _payload(value),
            ),
        )
        return value

    def update(self, value: ProviderCallRecord) -> ProviderCallRecord:
        cursor = self.db.connection.execute(
            """UPDATE provider_call_records
               SET status = ?, completed_at = ?, payload = ?
               WHERE id = ?""",
            (
                value.status,
                None if value.completed_at is None else _utc_timestamp(value.completed_at),
                _payload(value),
                str(value.id),
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError(value.id)
        return value

    def list_for_project(self, project_id: UUID) -> list[ProviderCallRecord]:
        rows = self.db.connection.execute(
            "SELECT * FROM provider_call_records WHERE project_id = ? ORDER BY created_at, id",
            (str(project_id),),
        ).fetchall()
        return [_model(row, self.model) for row in rows]

    def list_all(self) -> list[ProviderCallRecord]:
        return self.list()


class AssetRepository(_Repository[Asset]):
    table, model = "assets", Asset

    def __init__(self, db: Database):
        super().__init__(db)
        self.db.connection.execute("PRAGMA busy_timeout = 5000")

    def create(self, value: Asset) -> Asset:
        self.db.connection.execute("INSERT INTO assets(id, duration_ms, content_hash, payload) VALUES (?, ?, ?, ?)", (str(value.id), value.duration_ms, value.content_hash, _payload(value)))
        return value

    def update(self, value: Asset) -> Asset:
        existing = self.db.connection.execute("SELECT content_hash FROM assets WHERE id = ?", (str(value.id),)).fetchone()
        if existing is None:
            raise KeyError(value.id)
        if existing["content_hash"] != value.content_hash:
            raise ValueError("asset content_hash is immutable")
        clips = self.db.connection.execute("SELECT asset_duration_ms, end_ms FROM clips WHERE asset_id = ?", (str(value.id),)).fetchall()
        if any(int(row["asset_duration_ms"]) != value.duration_ms or int(row["end_ms"]) > value.duration_ms for row in clips):
            raise ValueError("asset duration update would invalidate existing clips")
        cursor = self.db.connection.execute("UPDATE assets SET duration_ms = ?, content_hash = ?, payload = ? WHERE id = ?", (value.duration_ms, value.content_hash, _payload(value), str(value.id)))
        if cursor.rowcount != 1:
            raise KeyError(value.id)
        return value

    def get_by_content_hash(self, content_hash: str) -> Asset | None:
        row = self.db.connection.execute("SELECT * FROM assets WHERE content_hash = ?", (content_hash,)).fetchone()
        return None if row is None else _model(row, self.model)


class ClipRepository(_Repository[Clip]):
    table, model = "clips", Clip

    def list_by_asset(self, asset_id: UUID) -> list[Clip]:
        rows = self.db.connection.execute(
            "SELECT * FROM clips WHERE asset_id = ? ORDER BY start_ms, end_ms, id", (str(asset_id),)
        ).fetchall()
        return [_model(row, Clip) for row in rows]

    def create(self, value: Clip) -> Clip:
        self._validate_asset(value)
        self.db.connection.execute("INSERT INTO clips(id, asset_id, start_ms, end_ms, asset_duration_ms, payload) VALUES (?, ?, ?, ?, ?, ?)", (str(value.id), str(value.asset_id), value.start_ms, value.end_ms, value.asset_duration_ms, _payload(value)))
        return value

    def update(self, value: Clip) -> Clip:
        self._validate_asset(value)
        cursor = self.db.connection.execute("UPDATE clips SET asset_id = ?, start_ms = ?, end_ms = ?, asset_duration_ms = ?, payload = ? WHERE id = ?", (str(value.asset_id), value.start_ms, value.end_ms, value.asset_duration_ms, _payload(value), str(value.id)))
        if cursor.rowcount != 1:
            raise KeyError(value.id)
        return value

    def upsert(self, value: Clip) -> Clip:
        self._validate_asset(value)
        self.db.connection.execute(
            """INSERT INTO clips(id, asset_id, start_ms, end_ms, asset_duration_ms, payload)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   asset_id = excluded.asset_id,
                   start_ms = excluded.start_ms,
                   end_ms = excluded.end_ms,
                   asset_duration_ms = excluded.asset_duration_ms,
                   payload = excluded.payload""",
            (
                str(value.id), str(value.asset_id), value.start_ms, value.end_ms,
                value.asset_duration_ms, _payload(value),
            ),
        )
        return value

    def _validate_asset(self, value: Clip) -> None:
        row = self.db.connection.execute("SELECT duration_ms FROM assets WHERE id = ?", (str(value.asset_id),)).fetchone()
        if row is None:
            raise sqlite3.IntegrityError("clip asset does not exist")
        duration = int(row["duration_ms"])
        if value.asset_duration_ms != duration:
            raise ValueError("clip asset_duration_ms does not match stored asset duration")
        if value.end_ms > duration:
            raise ValueError("clip interval exceeds stored asset duration")


class JobRepository(_Repository[Job]):
    table, model = "jobs", Job

    def __init__(self, db: Database):
        super().__init__(db)
        # Each SQLite connection needs its own busy timeout.  This lets a
        # second local process wait for an idempotent insert to commit instead
        # of spuriously failing with "database is locked".
        self.db.connection.execute("PRAGMA busy_timeout = 5000")

    def create(self, value: Job) -> Job:
        self.db.connection.execute(
            """INSERT INTO jobs(
                    id, project_id, idempotency_key, status, attempt,
                    created_at, updated_at, error_code, error_message, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING""",
            (
                str(value.id), None if value.project_id is None else str(value.project_id), value.idempotency_key,
                value.status.value, value.attempt, _utc_timestamp(value.created_at), _utc_timestamp(value.updated_at),
                value.error_code, value.error_message, _job_payload(value),
            ),
        )
        # Fetch after the insert attempt, rather than before it: the UNIQUE
        # constraint is the concurrency authority for separate connections.
        row = self.db.connection.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (value.idempotency_key,)).fetchone()
        if row is None:  # A conflicting primary key is not an idempotency hit.
            raise sqlite3.IntegrityError("job insert did not persist")
        return _model(row, Job)

    def list_for_asset(self, asset_id: UUID) -> list[Job]:
        rows = self.db.connection.execute(
            """SELECT j.* FROM jobs AS j
               INNER JOIN asset_job_targets AS target ON target.job_id = j.id
               WHERE target.asset_id = ? ORDER BY j.created_at, j.id""",
            (str(asset_id),),
        ).fetchall()
        return [_model(row, Job) for row in rows]

    def update(self, value: Job) -> Job:
        cursor = self.db.connection.execute(
            """UPDATE jobs
               SET project_id = ?, idempotency_key = ?, status = ?, attempt = ?,
                   created_at = ?, updated_at = ?, error_code = ?, error_message = ?,
                   lease_owner = CASE WHEN ? = 'running' THEN lease_owner ELSE NULL END,
                   lease_expires_at = CASE WHEN ? = 'running' THEN lease_expires_at ELSE NULL END,
                   payload = ?
               WHERE id = ?""",
            (
                None if value.project_id is None else str(value.project_id), value.idempotency_key,
                value.status.value, value.attempt, _utc_timestamp(value.created_at), _utc_timestamp(value.updated_at),
                value.error_code, value.error_message, value.status.value, value.status.value,
                _job_payload(value), str(value.id),
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError(value.id)
        return value

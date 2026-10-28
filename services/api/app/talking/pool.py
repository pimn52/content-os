"""创作者动作参考的合格连续区间；声音参考不参与此池。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from uuid import UUID

from app.db import (AssetRepository, ClipRepository, Database, ProjectRepository,
    ProviderMachineCapabilityProfileRepository, ProviderMachineSettingRepository, TalkingProfileRepository)
from app.domain.models import Asset, Clip, ProviderMachineCapabilityProfile, SourceKind, TalkingPerformanceBrief, TalkingProfile
from app.providers.local_generation import local_generation_ready, local_generation_licensed
from app.providers.talking import TalkingInputError
from app.source_usage import SourceUsageConflict, SourceUsageService
from .reference_selection import select_talking_reference

def _duration_validator(talking_provider):
    validate = getattr(talking_provider, "validate_reference_duration", None)
    return validate if callable(validate) else None


def _reference_covers(validate, narration_duration_ms: int, reference_duration_ms: int) -> bool:
    try:
        validate(narration_duration_ms, reference_duration_ms)
    except (TalkingInputError, ValueError):
        return False
    return True


def talking_reference_capacity(talking_provider, reference_duration_ms: int) -> dict[str, object]:
    """只问适配器的纯时长校验，不构造运行时或推测模型帧率。

    此处只计算出镜窗口容量，不限制完整旁白文案；校验须随时长单调收紧。
    返回的是单段可覆盖时长，不能把它当成多个场景各自拥有的额度。
    """
    if type(reference_duration_ms) is not int or reference_duration_ms < 1:
        raise ValueError("参考时长须为正整数毫秒")
    validate = _duration_validator(talking_provider)
    if validate is None:
        return {"alignment_state": "unknown", "max_narration_duration_ms": None}
    low, high = 0, reference_duration_ms
    while low < high:
        middle = (low + high + 1) // 2
        if _reference_covers(validate, middle, reference_duration_ms):
            low = middle
        else:
            high = middle - 1
    return {"alignment_state": "known", "max_narration_duration_ms": low}


def required_talking_reference_duration_ms(talking_provider, narration_duration_ms: int,
        reference_duration_ms: int) -> int | None:
    """在当前可用参考范围内求所需连续时长；未知或不足返回 None。"""
    if (type(narration_duration_ms) is not int or narration_duration_ms < 1
            or type(reference_duration_ms) is not int or reference_duration_ms < 1):
        raise ValueError("旁白和参考时长须为正整数毫秒")
    validate = _duration_validator(talking_provider)
    if (validate is None or narration_duration_ms > reference_duration_ms
            or not _reference_covers(validate, narration_duration_ms, reference_duration_ms)):
        return None
    low, high = narration_duration_ms, reference_duration_ms
    while low < high:
        middle = (low + high) // 2
        if _reference_covers(validate, narration_duration_ms, middle):
            high = middle
        else:
            low = middle + 1
    return low


def configured_talking_choices(db: Database, project_id: UUID):
    """规划和派发共用创作者已登记的来源，不以姓名猜身份。"""
    project = ProjectRepository(db).get(project_id)
    if project is None:
        return ()
    choices = {}
    clips, assets = ClipRepository(db), AssetRepository(db)
    profiles = TalkingProfileRepository(db)
    capabilities = ProviderMachineCapabilityProfileRepository(db)

    def add(profile, clip, capability, expected_hash=None):
        asset = assets.get(clip.asset_id) if clip is not None else None
        if (profile is None or not profile.consent.confirmed or clip is None or asset is None
            or profile.ip_profile_id not in (None,project.ip_profile_id)
            or clip.id not in profile.reference_clip_ids
            or asset.metadata.get("ip_profile_id") not in (None, str(project.ip_profile_id))
            or expected_hash is not None and expected_hash != asset.content_hash
            or capability is None or capability.capability != "talking" or capability.provider != profile.provider
            or not local_generation_ready(capability) or not local_generation_licensed(capability)):
            return
        choices[(profile.id, clip.id, capability.id)] = (profile, clip, capability)

    for row in db.connection.execute("SELECT project_id,talking_source_bindings FROM production_runs ORDER BY created_at DESC"):
        owner = ProjectRepository(db).get(UUID(row["project_id"]))
        if owner is None or owner.ip_profile_id != project.ip_profile_id:
            continue
        for value in json.loads(row["talking_source_bindings"]):
            try:
                add(profiles.get(UUID(value["talking_profile_id"])), clips.get(UUID(value["reference_clip_id"])),
                    capabilities.get(UUID(value["capability_profile_id"])), value.get("reference_asset_hash"))
            except (ValueError, KeyError, TypeError):
                continue  # Incomplete legacy hints are not dispatchable choices.
    for profile in profiles.list():
        for clip_id in profile.reference_clip_ids:
            clip = clips.get(clip_id)
            asset = assets.get(clip.asset_id) if clip is not None else None
            if asset is not None and asset.metadata.get("ip_profile_id") == str(project.ip_profile_id):
                for capability in capabilities.list():
                    if capability.capability == "talking":
                        add(profile, clip, capability)
    return tuple(sorted(choices.values(), key=lambda item: (
        -(item[1].end_ms - item[1].start_ms), -item[2].updated_at.timestamp(),
        str(item[0].id), str(item[1].id))))


@dataclass(frozen=True)
class TalkingPoolEntry:
    profile: TalkingProfile
    clip: Clip
    asset: Asset
    capability: ProviderMachineCapabilityProfile
    start_ms: int
    end_ms: int
    source_hash: str
    source_start_ms: int
    source_end_ms: int
    max_narration_duration_ms: int | None = None

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class TalkingAllocation:
    entry: TalkingPoolEntry
    start_ms: int
    end_ms: int
    narration_duration_ms: int
    needed_reference_ms: int


class TalkingPoolCapacityError(ValueError):
    def __init__(self, reason="talking_motion_capacity_insufficient", *, request_index=None):
        self.reason, self.request_index = reason, request_index
        super().__init__(reason)


def _subtract(start: int, end: int, intervals):
    """保持真实连续区间；绝不越过已占用或未获证实的缺口。"""
    cursor = start
    for left, right in sorted(intervals):
        if right <= cursor or left >= end:
            continue
        if cursor < left:
            yield cursor, min(left, end)
        cursor = max(cursor, right)
        if cursor >= end:
            return
    if cursor < end:
        yield cursor, end


def _union_duration(intervals):
    total, end = 0, -1
    for left, right in sorted(intervals):
        total += max(0, right - max(end, left))
        end = max(end, right)
    return total


@dataclass(frozen=True)
class TalkingPool:
    entries: tuple[TalkingPoolEntry, ...]
    occupancy_state: str = "known"

    @property
    def usable_total_ms(self) -> int:
        by_source = {}
        for entry in self.entries:
            by_source.setdefault(entry.source_hash, []).append((entry.source_start_ms, entry.source_end_ms))
        return sum(_union_duration(intervals) for intervals in by_source.values())

    @property
    def longest_continuous_ms(self) -> int:
        # 不把相邻 Clip 的单独评价冒充跨 Clip 的连续合格证明。
        return max((entry.duration_ms for entry in self.entries), default=0)

    def summary(self, *, talking_provider=None) -> dict[str, object]:
        """规划提示不是派发许可；未知适配器对齐能力保持未知。"""
        capacities, claimed = [], {}
        for entry in sorted(self.entries, key=lambda value: -value.duration_ms):
            for start, end in _subtract(entry.source_start_ms, entry.source_end_ms,
                    claimed.get(entry.source_hash, ())):
                matches = _provider_matches(talking_provider, entry.capability)
                capacity = talking_reference_capacity(talking_provider if matches else None, end-start)
                if capacity["max_narration_duration_ms"] is not None and entry.max_narration_duration_ms is not None:
                    capacity["max_narration_duration_ms"] = min(capacity["max_narration_duration_ms"], entry.max_narration_duration_ms)
                capacities.append(capacity)
            claimed.setdefault(entry.source_hash, []).append((entry.source_start_ms, entry.source_end_ms))
        known = bool(capacities) and all(value["alignment_state"] == "known" for value in capacities)
        longest = []
        for entry in self.entries:
            if not _provider_matches(talking_provider, entry.capability):
                continue
            capacity = talking_reference_capacity(talking_provider, entry.duration_ms)["max_narration_duration_ms"]
            if capacity is not None:
                longest.append(min(capacity, entry.max_narration_duration_ms) if entry.max_narration_duration_ms is not None else capacity)
        return {"state": "available" if self.entries else "unavailable",
            "occupancy_state": self.occupancy_state, "entry_count": len(self.entries),
            "usable_total_ms": self.usable_total_ms, "longest_continuous_ms": self.longest_continuous_ms,
            "alignment_state": "known" if known else "unknown",
            "max_narration_duration_ms": max(longest) if known and longest else None}


def _provider_matches(provider, capability):
    return (provider is not None and getattr(provider, "provider_name", None) == capability.provider
        and getattr(provider, "model", None) == capability.model)


def _qualified(db, data_root, asset, clip, brief):
    if select_talking_reference([clip], brief).selected_clip_id == clip.id:
        return True
    if data_root is None or not isinstance(asset.metadata.get("source_derivation"), dict):
        return False
    # 复用已入库整段复核的精确字节和来源，不能只凭 metadata 绕过未知评价。
    from app.media import FFProbeAdapter, MediaImporter, VerticalSourceDerivationService
    from app.media.vertical_derivation import VerticalDerivationError
    try:
        VerticalSourceDerivationService(db, Path(data_root),
            MediaImporter(db, Path(data_root), FFProbeAdapter())).verify_existing(asset, clip)
    except (VerticalDerivationError, ValueError, KeyError, TypeError, OSError):
        return False
    return select_talking_reference([clip], brief,
        adopted_full_interval_clip_ids=frozenset({clip.id})).selected_clip_id == clip.id


def build_talking_pool(db: Database, project_id: UUID, *, talking_provider=None,
        brief: TalkingPerformanceBrief | None = None, data_root=None, run_id=None, work_id=None,
        occupied=None) -> TalkingPool:
    """读取所有已关联创作者的动作参考；静音、不流利不影响动作参考资格。"""
    usage = SourceUsageService(db)
    try:
        held = usage.occupied(project_id, run_id=run_id, work_id=work_id) if occupied is None else list(occupied)
    except (SourceUsageConflict, KeyError, ValueError):
        return TalkingPool((), "unresolved")
    brief = brief or TalkingPerformanceBrief(minimum_reference_duration_ms=1)
    entries = []
    checked={}
    for profile, clip, capability in configured_talking_choices(db, project_id):
        if talking_provider is not None and not _provider_matches(talking_provider, capability):
            continue
        asset = AssetRepository(db).get(clip.asset_id)
        if (asset is None or clip.reference_source_timebase is not None
                or asset.source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK}
                or not _qualified(db, data_root, asset, clip, brief)):
            continue
        if data_root is not None:
            if asset.id not in checked:
                from app.media.local_paths import existing_local_media_file
                try:
                    with existing_local_media_file(asset.source_file,data_root).open('rb') as stream:
                        checked[asset.id]=hashlib.file_digest(stream,'sha256').hexdigest()==asset.content_hash
                except (ValueError,OSError):checked[asset.id]=False
            if not checked[asset.id]:continue
        try:
            canonical = usage.resolve(asset.id, clip.start_ms, clip.end_ms, str(clip.id))
        except SourceUsageConflict:
            continue  # 未知来源坐标不能冒充独立的可调度容量。
        if len(canonical) != 1 or canonical[0]["content_changed"]:
            continue
        source = canonical[0]
        if source["end_ms"] - source["start_ms"] != clip.end_ms - clip.start_ms:
            continue
        blocked = [(other["start_ms"], other["end_ms"]) for other in held
                   if other["source_hash"] == source["source_hash"]]
        limit = capability.verified_parameters.get("fresh_voice_talking_duration_ms")
        if limit is None:
            setting = ProviderMachineSettingRepository(db).get_by_scope_key(capability.scope_key)
            limit = None if setting is None else setting.values.get("max_talking_duration_ms")
        if type(limit) is not int or limit <= 0:
            limit = None
        for start, end in _subtract(source["start_ms"], source["end_ms"], blocked):
            offset = start-source["start_ms"]
            entries.append(TalkingPoolEntry(profile, clip, asset, capability,
                clip.start_ms+offset, clip.start_ms+offset+end-start,
                source["source_hash"], start, end, limit))
    return TalkingPool(tuple(entries))


def allocate_talking_windows(pool: TalkingPool, durations_ms, *, talking_provider=None) -> tuple[TalkingAllocation, ...]:
    """按请求顺序整体分配连续前向窗口，失败不产生部分结果。"""
    if pool.occupancy_state != "known":
        raise TalkingPoolCapacityError("talking_motion_occupancy_unresolved")
    durations = tuple(durations_ms)
    for duration in durations:
        if type(duration) is not int or duration <= 0:
            raise ValueError("出镜时长须为正整数毫秒")

    # 同一来源严格前进，先前窗口已在 cursor 之前；不同来源独立。
    # 按短窗口优先探索，但只有整批成功才确定，避免局部最优耗尽后续容量。
    failed, visits, furthest = set(), 0, 0
    max_states = 50000

    def search(index, cursors):
        nonlocal visits, furthest
        if index == len(durations):
            return ()
        state = (index, tuple(sorted(cursors.items())))
        if state in failed:
            return None
        visits += 1
        if visits > max_states:
            raise TalkingPoolCapacityError("talking_motion_allocation_search_limit", request_index=index)
        furthest = max(furthest, index)
        duration = durations[index]
        candidates = []
        for entry in pool.entries:
            if talking_provider is not None and not _provider_matches(talking_provider, entry.capability):
                continue
            if entry.max_narration_duration_ms is not None and duration > entry.max_narration_duration_ms:
                continue
            start = max(entry.source_start_ms, cursors.get(entry.source_hash, 0))
            available = entry.source_end_ms-start
            if available <= 0:
                continue
            needed = (duration if talking_provider is None else
                required_talking_reference_duration_ms(talking_provider, duration, available))
            if needed is None or needed > available:
                continue
            candidates.append(((available, str(entry.clip.id), str(entry.capability.id), start), entry, start, needed))
        for _, entry, start, needed in sorted(candidates, key=lambda item: item[0]):
            next_cursors = {**cursors, entry.source_hash: start+needed}
            tail = search(index+1, next_cursors)
            if tail is not None:
                local_start = entry.start_ms+start-entry.source_start_ms
                allocation = TalkingAllocation(entry, local_start, local_start+needed, duration, needed)
                return (allocation, *tail)
        failed.add(state)
        return None

    result = search(0, {})
    if result is None:
        raise TalkingPoolCapacityError(request_index=furthest)
    return result



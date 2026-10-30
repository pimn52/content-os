"""Read existing, receipted source failures for the same task and visible view.

No global material ban, provider call, database write, or occupancy policy.
"""
from __future__ import annotations

import json
import math
import re
from uuid import UUID

from app.budget import provider_call_input_digest
from app.db import AssetRepository, ClipRepository, ProviderCallRepository
from app.assembly.edit_plan import source_frame_interval_ms


class SemanticUseRejected(ValueError):
    reasons = ('source_semantic_use_rejected',)

    def __init__(self, hits=()):
        super().__init__(self.reasons[0])
        self.hits = tuple(hits)


def source_view_uses(db, asset_id, start_ms, end_ms, *, _seen=()):
    """Exact bytes always identify a view; only full-frame derivations inherit it."""
    asset = AssetRepository(db).get(UUID(str(asset_id)))
    if (asset is None or asset.id in _seen or type(start_ms) is not int or type(end_ms) is not int
            or not 0 <= start_ms < end_ms <= asset.duration_ms):
        raise ValueError('semantic_source_view_unresolved')
    uses = [dict(source_hash=asset.content_hash,start_ms=start_ms,end_ms=end_ms)]
    derived = asset.metadata.get('source_derivation')
    if not isinstance(derived,dict):
        return uses
    try:
        parent = AssetRepository(db).get(UUID(derived['source_asset_id']))
        crop, interval = derived['crop_window'], derived['source_interval']
        transform = derived.get('transform')
        if (parent is None or parent.content_hash != derived['source_content_hash']
                or transform is not None and (not isinstance(transform,dict)
                    or transform.get('filter') != 'fixed_crop_then_scale'
                    or transform.get('timing') != 'native_fps_first_frame_to_zero')
                or not isinstance(crop,dict) or not isinstance(interval,dict)
                or any(type(crop.get(key)) is not int for key in ('x','y','width','height'))
                or set(crop)-{'schema_version','x','y','width','height'}
                or {key:crop[key] for key in ('x','y','width','height')} != dict(x=0,y=0,width=parent.width,height=parent.height)
                or type(interval.get('start_ms')) is not int or type(interval.get('end_ms')) is not int
                or not 0 <= interval['start_ms'] < interval['end_ms'] <= parent.duration_ms
                or abs(interval['end_ms']-interval['start_ms']-asset.duration_ms) > math.ceil(1000*asset.fps.denominator/asset.fps.numerator)
                or interval['start_ms']+end_ms > interval['end_ms']):
            return uses
        uses.extend(source_view_uses(db,parent.id,interval['start_ms']+start_ms,interval['start_ms']+end_ms,
            _seen=(*_seen,asset.id)))
    except (KeyError,TypeError,ValueError):
        pass  # Missing or spatially different lineage cannot condemn the original full view.
    return uses


def _valid_use(use):
    return (isinstance(use,dict) and isinstance(use.get('source_hash'),str)
        and re.fullmatch(r'[0-9a-f]{64}',use['source_hash']) is not None
        and type(use.get('start_ms')) is int and type(use.get('end_ms')) is int
        and 0 <= use['start_ms'] < use['end_ms'])


def _source_receipt(db, machine, task_key, *, project, key, input_source, states):
    from app.presentation_diagnosis import classify_semantic_comparison
    from app.providers.vision import VideoIntervalBatch
    try:
        if (machine.get('stale') is not False or machine.get('state') not in states
                or machine.get('assessment_state') != 'known' or machine.get('responsible_stage') != 'material'):
            return None
        scope, document, binding = machine['scope'], machine['input'], machine['binding']
        if (scope.get('scene_key') != task_key or scope['scene_request']['scene_key'] != task_key
                or document['scope'] != scope or document['binding'] != binding):return None
        call = ProviderCallRepository(db).get(UUID(machine['call_id']))
        if (call is None or call.status != 'completed' or call.mode != 'runtime' or call.operation != 'vision'
                or str(call.project_id) != project or call.provider != machine['provider'] or call.model != machine['model']
                or call.input_source != input_source or call.idempotency_key != key):return None
        raw = machine['raw_result']
        if (raw.get('validation_issues') or call.result_payload is None or call.result_payload.get('result') != raw
                or call.input_digest != provider_call_input_digest(dict(project_id=project,operation='vision',
                    provider=call.provider,model=call.model,input_source=input_source,input=document))):return None
        verdict = classify_semantic_comparison(scope,VideoIntervalBatch.model_validate(raw['batch']))
        if verdict.get('assessment_state') != 'known' or verdict.get('responsible_stage') != 'material':return None
        source = scope['source']
        own = dict(source_hash=source['source_content_hash'],start_ms=source['start_ms'],end_ms=source['end_ms'])
        if not _valid_use(own) or binding['asset_content_hash'] != own['source_hash']:return None
        uses = [own]
        frozen = source.get('view_source_uses',[])
        if isinstance(frozen,list) and all(_valid_use(use) for use in frozen):
            uses.extend(use for use in frozen if use not in uses)
        return dict(call_id=str(call.id),scene_key=task_key,uses=uses)
    except (KeyError,TypeError,ValueError,AttributeError):
        return None


def negative_source_evidence(db, task_key):
    """Read only source-specific failures from the two existing comparison paths."""
    from app.production_runs import _snapshot_sha256
    evidence, seen = [], set()
    for row in db.connection.execute("""SELECT payload FROM presentation_observations
            WHERE json_extract(payload,'$.request.finding')='semantic_mismatch' ORDER BY rowid DESC"""):
        try:
            observed = json.loads(row[0]); request = observed['request']
            identity = (observed['run_id'],observed['render_job_id'],request['scene_id'])
            if identity in seen:continue
            seen.add(identity)
            machine = observed.get('machine_diagnosis',{})
            binding = machine['binding']
            if (binding['observation_id'] != observed['id'] or binding['render_job_id'] != observed['render_job_id']
                    or binding['observation_sha256'] != _snapshot_sha256({key:observed[key] for key in ('id','source','request','render_job_id')})):
                continue
            receipt = _source_receipt(db,machine,task_key,project=observed['project_id'],
                key=f"presentation-semantic:{observed['run_id']}:{observed['render_job_id']}:{observed['id']}",
                input_source=f"presentation-observation:{observed['id']}",states={'known'})
            if receipt:evidence.append(dict(observation_id=observed['id'],**receipt))
        except (KeyError,TypeError,ValueError,AttributeError):
            continue
    for row in db.connection.execute("SELECT payload,technical_qa FROM presentation_repairs WHERE json_extract(payload,'$.semantic_recheck.responsible_stage')='material'"):
        try:
            repair = json.loads(row['payload']);machine = repair['semantic_recheck'];binding = machine['binding']
            technical = json.loads(row['technical_qa'])
            if (binding['repair_id'] != repair['id'] or binding['render_job_id'] != repair['replacement_job_id']
                    or binding['repair_plan_fingerprint'] != repair['plan']['fingerprint']
                    or binding['repair_plan_sha256'] != _snapshot_sha256(repair['plan'])
                    or binding['technical_qa_sha256'] != _snapshot_sha256(technical)
                    or technical.get('state') != 'verified' or technical.get('decode') != 'pass'
                    or technical.get('sha256') != binding['render_sha256']):continue
            receipt = _source_receipt(db,machine,task_key,project=repair['project_id'],
                key=f"presentation-semantic:{repair['run_id']}:{repair['replacement_job_id']}:repair:{repair['id']}",
                input_source=f"presentation-repair:{repair['id']}",states={'failed'})
            if receipt:evidence.append(dict(repair_id=repair['id'],**receipt))
        except (KeyError,TypeError,ValueError,AttributeError):
            continue
    return tuple(evidence)


def rejected_window(db, task_key, asset_id, start_ms, end_ms, *, evidence=None):
    records = negative_source_evidence(db,task_key) if evidence is None else evidence
    if not records:return ()
    uses = source_view_uses(db,asset_id,start_ms,end_ms)
    return tuple(record for record in records if any(left['source_hash']==right['source_hash']
        and max(left['start_ms'],right['start_ms']) < min(left['end_ms'],right['end_ms'])
        for left in uses for right in record['uses']))


def candidate_rejections(db, scene, candidate, *, fps, duration_ms=None, evidence=None):
    from app.assembly.video_spec import milliseconds_to_frames
    from app.media.visual_facts import scene_visual_key
    clip = ClipRepository(db).get(candidate.clip_id) if candidate.clip_id else None
    if candidate.asset_id is None or clip is None:return ()
    start = candidate.playback_start_ms if candidate.playback_start_ms is not None else clip.start_ms
    end = candidate.playback_end_ms if candidate.playback_end_ms is not None else clip.end_ms
    first,last = source_frame_interval_ms(start,milliseconds_to_frames(duration_ms or scene.duration_target_ms,fps),fps,end)
    return rejected_window(db,scene_visual_key(scene),candidate.asset_id,first,last,evidence=evidence)


def require_spec_semantics(db, spec):
    if spec.edit_plan is None:return
    keys = {entry.scene_id:getattr(entry,'visual_task_key',None) for entry in spec.edit_plan.scenes}
    cached, hits = {}, []
    for scene in spec.scenes:
        key, visual = keys.get(scene.scene_id), scene.visual
        if not key or visual.asset_id is None or visual.clip_start_ms is None:continue
        if key not in cached:cached[key] = negative_source_evidence(db,key)
        records = cached[key]
        if not records:continue
        first,last = source_frame_interval_ms(visual.clip_start_ms,scene.duration_frames,spec.fps,visual.clip_end_ms)
        for hit in rejected_window(db,key,visual.asset_id,first,last,evidence=records):
            hits.append(dict(scene_id=scene.scene_id,**hit))
    if hits:raise SemanticUseRejected(hits)

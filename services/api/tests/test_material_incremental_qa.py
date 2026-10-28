"""Incremental material-use QA and production budget contracts (local fixtures)."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.db import AssetRepository, BudgetPolicyRepository, ProviderCallRepository
from app.domain.models import BudgetPolicy
from app.media.planning import MaterialResolutionError
from app.media.visual_facts import scene_visual_key
from test_material_analysis_batches import scope
from test_material_resolution import material_library


def policy(db, project, max_calls):
    BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, max_calls=max_calls,
        allow_unknown_cost=True, updated_at=datetime.now(timezone.utc)))


def test_new_clause_only_checks_new_use_and_preserves_existing_match(tmp_path):
    db, project, asset, _, base, provider, resolver = material_library(tmp_path, metered=True)
    try:
        scenes, windows = scope(resolver, asset, base, count=3, scene_count=2)
        provider.accept = lambda video: True
        job_id = uuid4()
        resolver.resolve(project, scenes[:1], job_id)
        old_key, new_key = map(scene_visual_key, scenes)
        before = deepcopy(AssetRepository(db).get(asset.id).metadata['visual_analysis'])
        # The selected old use stays valid when the measured script adds a clause.
        chosen = resolver.resolve(project, scenes, job_id, after_master=True)
        assert len(chosen) == 2
        assert len(provider.calls) == 2
        assert {request['scene_key'] for request in provider.calls[0][1]} == {old_key}
        assert {request['scene_key'] for request in provider.calls[1][1]} == {new_key}
        after = AssetRepository(db).get(asset.id).metadata['visual_analysis']
        for identity in windows:
            assert after[identity]['scene_matches'][old_key] == before[identity]['scene_matches'][old_key]
            assert after[identity]['scene_matches'][new_key]['supported'] is True
        assert len(ProviderCallRepository(db).list_for_project(project.id)) == 2
    finally:
        db.close()


@pytest.mark.parametrize('global_policy', [False, True])
def test_negative_batch_yields_to_supplement_with_tail_and_gaps_intact(tmp_path, global_policy):
    db, project, asset, _, base, provider, resolver = material_library(tmp_path, metered=True)
    try:
        scenes, _ = scope(resolver, asset, base, count=6)
        provider.accept = lambda video: False
        BudgetPolicyRepository(db).save(BudgetPolicy(project_id=None if global_policy else project.id,
            max_calls=6, allow_unknown_cost=True, updated_at=datetime.now(timezone.utc)))
        with pytest.raises(MaterialResolutionError) as caught:
            resolver.resolve(project, scenes, uuid4(), after_master=True, remaining_talking_calls=1,
                supplement_reserve_calls=3)
        assert 'material_acquisition_due' in caught.value.reasons
        assert 'material_scene_unresolved:part-0' in caught.value.reasons
        assert caught.value.candidate_rejections['part-0']
        records = ProviderCallRepository(db).list_for_project(project.id)
        assert len(provider.calls) == len(records) == 1
        assert records[0].status == 'completed'
        assert 6 - len(records) == 5  # three supplement calls, one talking call, one render
    finally:
        db.close()


def test_first_batch_can_succeed_without_unused_supplement_reserve(tmp_path):
    db, project, asset, _, base, provider, resolver = material_library(tmp_path, metered=True)
    try:
        scenes, _ = scope(resolver, asset, base)
        provider.accept = lambda video: True
        policy(db, project, 3)
        assert resolver.resolve(project, scenes, uuid4(), after_master=True,
            remaining_talking_calls=1, supplement_reserve_calls=3)
        assert len(provider.calls) == len(ProviderCallRepository(db).list_for_project(project.id)) == 1
    finally:
        db.close()


def test_candidates_with_different_known_answers_are_batched_by_missing_uses(tmp_path):
    db, project, asset, _, base, provider, resolver = material_library(tmp_path, metered=True)
    try:
        scenes, windows = scope(resolver, asset, base, count=3, scene_count=2)
        provider.accept = lambda video: False
        _, requests = resolver._windows(project, scenes)
        identities = list(windows)
        # Two existing negative facts belong to different uses and candidates.
        resolver._analyze(project, {identities[0]: windows[identities[0]]}, requests[:1], uuid4())
        resolver._analyze(project, {identities[1]: windows[identities[1]]}, requests[1:], uuid4())
        provider.calls.clear()
        with pytest.raises(MaterialResolutionError):
            resolver.resolve(project, scenes, uuid4(), after_master=True)
        asked = {video['clip_id']: {request['scene_key'] for request in batch_requests}
            for videos, batch_requests in provider.calls for video in videos}
        old_key, new_key = map(scene_visual_key, scenes)
        assert asked == {identities[0]: {new_key}, identities[1]: {old_key},
            identities[2]: {old_key, new_key}}
        assert len(provider.calls) == 3
    finally:
        db.close()


def test_malformed_increment_keeps_valid_geometry_and_old_use_without_implicit_retry(tmp_path):
    from app.db import ClipRepository
    from test_material_analysis_batches import malformed_candidate

    db, project, asset, _, base, provider, resolver = material_library(tmp_path, metered=True)
    try:
        scenes, windows = scope(resolver, asset, base, count=1, scene_count=2)
        identity = next(iter(windows))
        job_id, attempt = uuid4(), uuid4()
        provider.accept = lambda video: True
        resolver.resolve(project, scenes[:1], job_id, attempt_id=attempt)
        repository = AssetRepository(db)
        before = deepcopy(repository.get(asset.id).metadata['visual_analysis'][identity])
        clip_before = ClipRepository(db).get(windows[identity][1].id)
        old_key, new_key = map(scene_visual_key, scenes)
        assert before['analysis_valid'] is True
        assert before['scene_matches'][old_key]['supported'] is True

        malformed_candidate(provider, identity)
        with pytest.raises(MaterialResolutionError) as caught:
            resolver.resolve(project, scenes, job_id, attempt_id=attempt, after_master=True)
        assert 'visual_purpose_unknown' in caught.value.candidate_rejections[scenes[1].scene_id][identity]
        after = repository.get(asset.id).metadata['visual_analysis'][identity]
        assert {k: v for k, v in after.items() if k != 'scene_matches'} == {
            k: v for k, v in before.items() if k != 'scene_matches'}
        assert after['scene_matches'][old_key] == before['scene_matches'][old_key]
        assert after['scene_matches'][new_key]['analysis_valid'] is False
        assert after['scene_matches'][new_key]['analysis_invalid_fields']
        assert ClipRepository(db).get(clip_before.id) == clip_before
        assert {request['scene_key'] for request in provider.calls[-1][1]} == {new_key}
        assert len(provider.calls) == 2
        for same_attempt in (attempt, None):
            with pytest.raises(MaterialResolutionError):
                resolver.resolve(project, scenes, job_id, attempt_id=same_attempt, after_master=True)
        assert len(provider.calls) == 2
        assert len(ProviderCallRepository(db).list_for_project(project.id)) == 2
        assert repository.get(asset.id).metadata['visual_analysis'][identity] == after
    finally:
        db.close()


def test_invalid_use_cannot_reuse_stale_positive_match_in_routing_or_assembly(tmp_path):
    from app.assembly.video_spec import VideoSpecAssembler, InvalidCandidateSelection
    from app.db import ClipRepository
    from app.routing import AssetRouter
    from app.routing.asset_router import RoutingInputError
    from app.search import ClipTextSearchService

    db, project, asset, _, base, provider, resolver = material_library(tmp_path, metered=True)
    try:
        provider.accept = lambda video: True
        selected = resolver.resolve(project, [base], uuid4())[0]
        router = AssetRouter(ClipTextSearchService(db), AssetRepository(db))
        candidate = next(value for value in router.route(selected).candidates if value.recommended)
        current = AssetRepository(db).get(asset.id)
        metadata = deepcopy(current.metadata)
        for facts in metadata['visual_analysis'].values():
            match = facts.get('scene_matches', {}).get(scene_visual_key(selected))
            if match:
                assert match['supported'] and match['quality_adequate']
                match['analysis_valid'] = False
        AssetRepository(db).update(current.model_copy(update={'metadata': metadata}))
        with pytest.raises(RoutingInputError):
            router.explicit_footage(selected, [candidate])
        with pytest.raises(InvalidCandidateSelection):
            VideoSpecAssembler(AssetRepository(db), ClipRepository(db)).assemble(
                project, [selected], {selected.id: candidate})
        assert len(provider.calls) == 1
    finally:
        db.close()


def test_combination_conflict_checks_missing_alternative_for_earlier_scene(tmp_path, monkeypatch):
    from app.source_usage import SourceUsageService

    db, project, asset, _, base, provider, resolver = material_library(tmp_path, metered=True)
    try:
        scenes, windows = scope(resolver, asset, base, count=2, scene_count=2)
        shared, alternative = list(windows)
        _, requests = resolver._windows(project, scenes)
        provider.accept = lambda video: video['clip_id'] == shared
        resolver._analyze(project, {shared: windows[shared]}, requests, uuid4())
        resolver._analyze(project, {alternative: windows[alternative]}, requests[1:], uuid4())
        original_uses = SourceUsageService.candidate_uses
        def nonrepeatable(self, *args):
            return [{**use, 'repeatable': False, 'content_changed': True}
                for use in original_uses(self, *args)]
        monkeypatch.setattr(SourceUsageService, 'candidate_uses', nonrepeatable)
        with pytest.raises(MaterialResolutionError) as failure:
            resolver._select(project, scenes, windows, None)
        assert failure.value.reasons == (f'material_combination_unresolved:{scenes[1].scene_id}',)
        before_calls = len(provider.calls)
        provider.accept = lambda video: True
        chosen = resolver.resolve(project, scenes, uuid4(), after_master=True)
        assert str(chosen[0].preferred_clip_id) == alternative
        assert str(chosen[1].preferred_clip_id) == shared
        assert len(provider.calls) == before_calls + 1
        videos, requested = provider.calls[-1]
        assert [video['clip_id'] for video in videos] == [alternative]
        assert [request['scene_key'] for request in requested] == [scene_visual_key(scenes[0])]
    finally:
        db.close()

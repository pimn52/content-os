"""One named retained-media continuation. Execution and media grants stay exact."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from app.db import AudioAssetRepository, JobRepository, ProjectDraftRepository, ProjectRepository, TalkingRunRepository
from app.db.evaluation_composition import CompositionRepository
from app.domain.evaluation_composition import (POLICY, CompositionSelection, CompositionBatchReview,
    CompositionUseReview, CompositionAdmission)
from app.domain.models import (Job, JobType, JobStatus, RenderVideoJobPayload, execution_canonical_sha256 as digest)
from app.db.evaluation_master import EvaluationMasterRepository
from app.execution_admission import ProviderUseAdmissionService, UseAdmissionError, configured_admission_actor
from app.execution_scope import ExecutionScopeService, _make_scope, read_subject_scope, job_use_identity
from app.media.audio_access import file_digest


def is_composition(db, *, run_id=None, job_id=None, render_id=None):
    if CompositionRepository(db).owner(run_id=run_id,job_id=job_id,render_id=render_id):
        return True
    if run_id is not None:
        row = db.connection.execute("SELECT stage FROM production_runs WHERE id=?",(str(run_id),)).fetchone()
        return bool(row and row[0] == "evaluation_composition")
    if job_id is not None:
        row = db.connection.execute("SELECT 1 FROM production_runs WHERE stage='evaluation_composition' AND render_job_id=?",(str(job_id),)).fetchone()
        return bool(row)
    return False


class EvaluationCompositionService:
    def __init__(self, db, root):
        self.db, self.root = db, Path(root).resolve()
        self.records = CompositionRepository(db)
        self.scopes = ExecutionScopeService(db,self.root)
        self.masters = EvaluationMasterRepository(db)
        self.admissions = ProviderUseAdmissionService(db,self.root)

    def _token(self):
        # The local monolith's DB fence ignores only lease renewal timestamps.
        # A global SQLite data_version fence would reject our own heartbeat
        # during retained file observation. Job identity/status/attempt/owner,
        # resource ownership and every authority row remain covered.
        facts=[]
        tables=[r[0] for r in self.db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for table in tables:
            quoted='"'+table.replace('"','""')+'"'
            rows=[]
            for row in self.db.connection.execute(f"SELECT * FROM {quoted} ORDER BY rowid"):
                item=dict(row)
                if table == "jobs":
                    payload=json.loads(item["payload"])
                    if item["status"] == "running" and payload.get("status") == "running":
                        item.pop("updated_at",None);item.pop("lease_expires_at",None)
                        payload.pop("updated_at",None)
                    item["payload"]=payload
                elif table == "local_resource_leases":
                    item.pop("lease_expires_at",None)
                rows.append(item)
            facts.append([table,rows])
        return digest(facts)

    def _fence(self, token):
        if token != self._token():
            raise UseAdmissionError("composition_authority_changed_reload")

    def _file(self, reference, sha, maximum=1_000_000):
        raw = self.admissions._file(reference,maximum=maximum)
        if hashlib.sha256(raw).hexdigest() != sha:
            raise UseAdmissionError("composition_evidence_changed")
        return raw

    def _parent(self, project_id, selection):
        from app.assembly.video_spec import _require_generated_voice_qa
        from app.voice_boundary_alignment import voice_asset_project_id
        from app.voice_qa import comparison_tokens
        binding = None
        audio_id = selection.master_audio_id
        if selection.master_run_id is not None:
            # Read only the selected asset/style, not the old execution state.
            binding = self.masters.binding(selection.master_run_id)
            if binding is None or binding.project_id != project_id:
                raise UseAdmissionError("composition_master_missing")
            audio_id = binding.selection.audio_id
        audio = AudioAssetRepository(self.db).get(audio_id)
        draft = ProjectDraftRepository(self.db).get(project_id)
        project = ProjectRepository(self.db).get(project_id)
        if (audio is None or draft is None or project is None or not audio.authorization_reference.strip()
            or voice_asset_project_id(audio, JobRepository(self.db)) != project_id):
            raise UseAdmissionError("composition_master_project_mismatch")
        _require_generated_voice_qa(audio,self.db)
        copy = draft.script or " ".join(s.voice_text for s in sorted(draft.scenes,key=lambda s:s.order))
        if (not comparison_tokens(copy) or comparison_tokens(copy) != comparison_tokens(
            audio.metadata.get("voice_generation", {}).get("target_text")
            or " ".join(s.text for s in audio.transcript_segments))):
            raise UseAdmissionError("composition_master_copy_mismatch")
        from app.media.local_paths import existing_local_media_file
        if file_digest(existing_local_media_file(audio.source_file,self.root)) != audio.content_hash:
            raise UseAdmissionError("composition_audio_bytes_changed")
        return binding,audio,draft,project

    def _context(self, project_id, selection):
        from app.production_runs import _unique_scene_speech_span
        from app.assembly.evaluation_composition import assemble_retained_composition
        from app.db import AssetRepository, ClipRepository, VoiceProfileRepository, TalkingProfileRepository
        from app.media.local_paths import existing_local_media_file
        from app.source_use_constraints import SourceUseConstraintService, presentation
        token = self._token()
        binding,audio,draft,project = self._parent(project_id,selection)
        ordered = sorted(draft.scenes,key=lambda s:s.order)
        if not ordered or set(selection.talking_runs) - {s.id for s in ordered}:
            raise UseAdmissionError("composition_scene_selection_changed")
        intervals,inputs,missing,proofs,consents = [],{},[],[],{}
        from app.routing import AssetRouter
        from app.search import ClipTextSearchService
        router = AssetRouter(ClipTextSearchService(self.db),AssetRepository(self.db))
        dependencies = set()
        voice_origins_facts = {}
        # Current consent and model provenance are facts of the selected media;
        # the original generation Job's status/configuration is not admission.
        def voice_origins(value, seen):
            if value.id in seen:
                raise UseAdmissionError("composition_voice_origin_cycle")
            seen = seen | {value.id}
            generation = value.metadata.get("voice_generation",{})
            voice_origins_facts[str(value.id)] = dict(content_hash=value.content_hash,generation=generation)
            composition = generation.get("composition",{})
            takes = composition.get("source_take_audio_ids",[])
            if takes:
                hashes = composition.get("source_take_content_hashes",[])
                if len(takes) != len(hashes):
                    raise UseAdmissionError("composition_voice_origin_changed")
                for identity,sha in zip(takes,hashes):
                    parent = AudioAssetRepository(self.db).get(UUID(identity))
                    if parent is None or parent.content_hash != sha:
                        raise UseAdmissionError("composition_voice_origin_changed")
                    voice_origins(parent,seen)
            elif generation:
                provider = generation.get("provider")
                if not isinstance(provider,str) or not provider.strip():
                    raise UseAdmissionError("composition_voice_origin_unknown")
                dependencies.add("voice:"+provider)
                profile_id = generation.get("voice_profile_id")
                profile = VoiceProfileRepository(self.db).get(UUID(profile_id)) if profile_id else None
                if profile is None or not profile.consent.confirmed or profile.provider != provider:
                    raise UseAdmissionError("composition_voice_consent_required")
                consents["voice:"+str(profile.id)] = profile.consent.model_dump(mode="json")
        voice_origins(audio,set())
        values = list(self.scopes.scopes_for_hash(audio.content_hash))
        own_audio = self.scopes.get("audio",audio.id)
        if own_audio is not None: values.append(own_audio)
        for scene in ordered:
            interval = _unique_scene_speech_span(audio,ordered,scene.id)
            if interval is None:
                raise UseAdmissionError("composition_speech_interval_unaligned")
            intervals.append((scene,*interval))
            if scene.visual_requirement != "creator_speaking":
                from app.production_preflight import _choose_candidate
                from app.domain.models import SourceKind
                candidate, need = _choose_candidate(scene,router.route(scene).candidates,
                    ClipRepository(self.db),AssetRepository(self.db))
                if (need != "none" or candidate is None
                    or candidate.source_kind not in {SourceKind.USER_ASSET,SourceKind.HISTORICAL_ASSET,SourceKind.TYPOGRAPHY}):
                    missing.append(str(scene.id));continue
                asset = AssetRepository(self.db).get(candidate.asset_id) if candidate.asset_id else None
                clip = ClipRepository(self.db).get(candidate.clip_id) if candidate.clip_id else None
                if candidate.source_kind is SourceKind.TYPOGRAPHY:
                    if scene.visual_requirement == "action_evidence" or scene.graphic_plan is None or scene.graphic_plan.kind != "text_card":
                        missing.append(str(scene.id));continue
                else:
                    if (asset is None or clip is None or clip.asset_id != asset.id
                        or clip.asset_duration_ms != asset.duration_ms or clip.end_ms > asset.duration_ms
                        or not asset.authorization_reference.strip() or asset.metadata.get("r1_usage") != "production"
                        or asset.metadata.get("project_id") not in (None,str(project_id))
                        or asset.metadata.get("ip_profile_id") not in (None,str(project.ip_profile_id))):
                        raise UseAdmissionError("composition_source_not_authorized")
                    if clip.end_ms-clip.start_ms < interval[1]-interval[0]:
                        missing.append(str(scene.id));continue  # Never fill required evidence with a card.
                    if file_digest(existing_local_media_file(asset.source_file,self.root)) != asset.content_hash:
                        raise UseAdmissionError("composition_source_bytes_changed")
                    if "source_derivation" in asset.metadata:
                        from app.media import VerticalSourceDerivationService, MediaImporter, FFProbeAdapter
                        try:
                            VerticalSourceDerivationService(self.db,self.root,
                                MediaImporter(self.db,self.root,FFProbeAdapter())).verify_existing(asset,clip)
                        except (ValueError,RuntimeError,KeyError,OSError):
                            raise UseAdmissionError("composition_source_derivation_changed") from None
                    values.extend(self.scopes.scopes_for_hash(asset.content_hash))
                    dependencies.add("source:"+str(asset.id))
                inputs[scene.id] = dict(asset=asset,clip=clip,run=None,candidate=candidate)
                proofs.append(dict(scene_id=str(scene.id),start_ms=interval[0],end_ms=interval[1],
                    asset=None if asset is None else asset.model_dump(mode="json"),
                    clip=None if clip is None else clip.model_dump(mode="json")))
                continue
            run_id = selection.talking_runs.get(scene.id)
            if run_id is None:
                missing.append(str(scene.id));continue
            run = TalkingRunRepository(self.db).get(run_id)
            if run is None:
                raise UseAdmissionError("composition_talking_run_missing")
            candidates = router.reviewed_talking_candidates(scene,master_audio_id=audio.id,master_interval=interval,run_id=run.id)
            if not candidates:
                raise UseAdmissionError("composition_talking_visual_not_eligible")
            asset = AssetRepository(self.db).get(candidates[0].asset_id)
            clip = ClipRepository(self.db).get(candidates[0].clip_id)
            if file_digest(existing_local_media_file(asset.source_file,self.root)) != asset.content_hash:
                raise UseAdmissionError("composition_talking_bytes_changed")
            for child in run.child_evidence:
                dependencies.add("talking:"+child.provider)
                child_asset = AssetRepository(self.db).get(child.output_asset_id)
                origin = child_asset.metadata.get("talking_generation",{}) if child_asset else {}
                profile_id = origin.get("talking_profile_id")
                profile = TalkingProfileRepository(self.db).get(UUID(profile_id)) if profile_id else None
                if (profile is None or not profile.consent.confirmed or profile.provider != child.provider
                    or run.authorized_reference_clip_id not in profile.reference_clip_ids):
                    raise UseAdmissionError("composition_talking_consent_required")
                consents["talking:"+str(profile.id)] = profile.consent.model_dump(mode="json")
            offset = interval[0]-run.master_start_ms-asset.metadata.get("source_derivation",{}).get("source_interval",{}).get("start_ms",0)
            # An in-memory source window, never a new persisted Clip or media.
            window = clip.model_copy(update=dict(start_ms=offset,end_ms=offset+interval[1]-interval[0]))
            inputs[scene.id] = dict(asset=asset,clip=window,run=run,candidate=candidates[0])
            own = self.scopes.get("talking_run",run.id)
            if own is not None: values.append(own)
            values.extend(self.scopes.scopes_for_hash(asset.content_hash))
            proofs.append(dict(scene_id=str(scene.id),start_ms=interval[0],end_ms=interval[1],
                asset=asset.model_dump(mode="json"),clip=clip.model_dump(mode="json"),
                source_start_ms=window.start_ms,source_end_ms=window.end_ms,run_sha256=digest(run.model_dump(mode="json"))))
        bindings = {}
        for scope in values:
            if scope.project_id != project_id or scope.purpose != "internal_evaluation":
                raise UseAdmissionError("composition_origin_scope_invalid")
            for item in scope.bindings:
                if item.admission_id in bindings and bindings[item.admission_id] != item:
                    raise UseAdmissionError("composition_origin_changed")
                bindings[item.admission_id] = item
        scope = _make_scope(project_id,"internal_evaluation",tuple(bindings[k] for k in sorted(bindings,key=str)))
        style = selection.style_tokens if binding is None else binding.selection.style_tokens
        spec = None if missing else assemble_retained_composition(project,audio,intervals,inputs,style)
        constraints = SourceUseConstraintService(self.db,self.root)
        if spec is not None:
            for entry in spec.edit_plan.scenes:
                candidate = inputs[entry.scene_plan_id]["candidate"]
                treated = presentation(entry,width=spec.width,height=spec.height,style=spec.edit_plan.style_tokens)
                if constraints.evaluate(candidate,treated,constraints.records(project_id)):
                    raise UseAdmissionError("composition_source_use_excluded")
        proof = dict(draft_sha256=digest(draft.model_dump(mode="json")),audio_sha256=audio.content_hash,duration_ms=audio.duration_ms,
            inputs=proofs,scope=scope.model_dump(mode="json"),consents=consents,dependencies=sorted(dependencies),voice_origins=voice_origins_facts,
            source_use_constraints=constraints.context(project_id),tail=dict(kind=selection.tail,start_ms=intervals[-1][2],end_ms=audio.duration_ms),
            video_spec=None if spec is None else spec.model_dump(mode="json"))
        request = dict(policy=POLICY,project_id=str(project_id),selection=selection.model_dump(mode="json"),proof=proof)
        self._fence(token)
        return dict(selection=selection,proof=proof,request_sha256=digest(request),scope=scope,spec=spec,
            inputs=inputs,audio=audio,draft=draft,missing=missing,token=token)

    def retained_selection(self, project_id, style):
        from app.production_preflight import _approved_master
        from app.production_runs import _unique_scene_speech_span
        from app.db import AssetRepository
        from app.routing import AssetRouter
        from app.search import ClipTextSearchService
        draft = ProjectDraftRepository(self.db).get(project_id)
        if draft is None or not draft.scenes:
            raise UseAdmissionError("composition_scene_selection_changed")
        ordered = sorted(draft.scenes,key=lambda s:s.order)
        audio = _approved_master(self.db,project_id,draft.script or " ".join(s.voice_text for s in ordered))
        if audio is None:
            raise UseAdmissionError("composition_approved_master_required")
        choices = {}
        assets = AssetRepository(self.db)
        router = AssetRouter(ClipTextSearchService(self.db),assets)
        for scene in ordered:
            if scene.visual_requirement != "creator_speaking":
                continue
            interval = _unique_scene_speech_span(audio,ordered,scene.id)
            if interval is None:
                raise UseAdmissionError("composition_speech_interval_unaligned")
            candidates = router.reviewed_talking_candidates(scene,master_audio_id=audio.id,master_interval=interval)
            if candidates:
                choices[scene.id] = UUID(assets.get(candidates[0].asset_id).metadata["talking_run"]["run_id"])
        return CompositionSelection(master_audio_id=audio.id,style_tokens=style,talking_runs=choices)

    def current_use(self, project_id, context):
        for raw in self.records.list("admission",project_id):
            if raw["value"]["review"]["request_sha256"] == context["request_sha256"]:
                return self._receipt(UUID(raw["id"]),context,project_id)
        raise UseAdmissionError("composition_use_admission_required")

    def retained_batch(self, project_id, selection):
        row = self.db.connection.execute(
            "SELECT id FROM evaluation_composition_batches WHERE project_id=? AND master_audio_id=?",
            (str(project_id),str(selection.master_audio_id))).fetchone()
        if row is None:
            raise UseAdmissionError("composition_batch_missing")
        return self._batch(UUID(row[0]))

    def start_retained(self, project_id, *, style, fingerprint, idempotency_key):
        selection = self.retained_selection(project_id,style)
        c = self._context(project_id,selection)
        if fingerprint != c["request_sha256"]:
            raise UseAdmissionError("composition_preflight_changed")
        use = self.current_use(project_id,c)
        batch = self.retained_batch(project_id,selection)
        return self._create(project_id,c,use,batch,idempotency_key)

    def preflight(self, project_id, selection):
        result = dict(policy=POLICY,purpose="internal_evaluation",eligible=False,reasons=[],missing_scenes=[],dispatch_authorized=False)
        try:
            c = self._context(project_id,selection)
            result.update(fingerprint=c["request_sha256"],proof=c["proof"],missing_scenes=c["missing"],
                eligible=not c["missing"],reasons=["composition_visual_unresolved:"+x for x in c["missing"]])
            if c["spec"] is not None:
                self._check_render_budget(project_id,c)
        except (ValueError,KeyError,TypeError,OSError) as exc:
            result["eligible"] = False
            result["reasons"] = [str(exc) if isinstance(exc,ValueError) else "composition_evidence_invalid"]
        return result

    def _check_render_budget(self, project_id, c):
        from app.budget import ProviderCallLedger
        from app.db import ProviderCallRepository
        row = self.db.connection.execute("SELECT id FROM evaluation_composition_batches WHERE project_id=? AND (master_run_id=? OR master_audio_id=?)",
            (str(project_id),str(c["selection"].parent_id),str(c["selection"].parent_id))).fetchone()
        if row and ProviderCallRepository(self.db).get_by_idempotency_key(f"composition:{row[0]}:render") is not None:
            return  # A spent slot may only recover, never reserve again.
        ProviderCallLedger(self.db).check_reservation(project_id=project_id,estimated_cost=c["spec"].estimated_cost)

    def reconcile(self, project_id, identity):
        owner = self.records.owner(run_id=identity)
        if owner is None or owner["project_id"] != str(project_id):
            raise UseAdmissionError("composition_run_owner_missing")
        job = JobRepository(self.db).get(UUID(owner["job_id"]))
        c = self.require_current_job(job)
        self.require_result(job)
        with self.db.transaction(immediate=True):
            self._fence(c["token"])
            current = JobRepository(self.db).get(job.id)
            if current != job or current.status not in (JobStatus.FAILED,JobStatus.COMPLETED):
                raise UseAdmissionError("composition_terminal_reconciliation_required")
            JobRepository(self.db).update(job.model_copy(update=dict(status=JobStatus.COMPLETED,
                error_code=None,error_message=None,updated_at=datetime.now(timezone.utc))))

    def adopt_batch(self, project_id, payload, key):
        actor = configured_admission_actor(key)
        if actor is None:
            raise UseAdmissionError("execution_admission_operator_required")
        review = CompositionBatchReview.model_validate_json(self._file(payload.review_reference,payload.review_sha256))
        if review.project_id != project_id:
            raise UseAdmissionError("composition_batch_project_changed")
        self._file(review.authorization_reference,review.authorization_sha256)
        token = self._token()
        _,_,draft,_ = self._parent(project_id,CompositionSelection(master_run_id=review.master_run_id,master_audio_id=review.master_audio_id))
        if not set(review.scene_ids).issubset(s.id for s in draft.scenes):
            raise UseAdmissionError("composition_batch_scene_changed")
        with self.db.transaction(immediate=True):
            self._fence(token)
            row = self.db.connection.execute("SELECT id FROM evaluation_composition_batches WHERE project_id=? AND (master_run_id=? OR master_audio_id=?)",
                (str(project_id),str(review.parent_id),str(review.parent_id))).fetchone()
            if row:
                value = self._batch(UUID(row[0]))
                if value["review"] != review.model_dump(mode="json"):
                    raise UseAdmissionError("composition_batch_already_bound")
                return value
            if any((v["review"].get("master_run_id") or v["review"].get("master_audio_id")) == str(review.parent_id) for v in self.records.list("batch",project_id)):
                raise UseAdmissionError("composition_batch_index_missing")
            identity = uuid4()
            self.db.connection.execute("INSERT INTO evaluation_composition_batches(id,project_id,master_run_id,authorization_sha256,master_audio_id) VALUES (?,?,?,?,?)",
                (str(identity),str(project_id),None if review.master_run_id is None else str(review.master_run_id),
                 review.authorization_sha256,None if review.master_audio_id is None else str(review.master_audio_id)))
            return self.records.save("batch",identity,dict(project_id=str(project_id),review=review.model_dump(mode="json"),
                review_reference=payload.review_reference,review_sha256=payload.review_sha256,actor_id=actor))

    def _batch(self, identity):
        value = self.records.get("batch",identity)
        if value is None:
            raise UseAdmissionError("composition_batch_missing")
        review = CompositionBatchReview.model_validate_json(json.dumps(value["review"]))
        raw = self._file(value["review_reference"],value["review_sha256"])
        if CompositionBatchReview.model_validate_json(raw) != review:
            raise UseAdmissionError("composition_batch_changed")
        self._file(review.authorization_reference,review.authorization_sha256)
        row = self.db.connection.execute("SELECT * FROM evaluation_composition_batches WHERE id=?",(str(identity),)).fetchone()
        if row is None or (row["project_id"],row["master_run_id"],row["master_audio_id"],row["authorization_sha256"]) != (
            str(review.project_id),None if review.master_run_id is None else str(review.master_run_id),
            None if review.master_audio_id is None else str(review.master_audio_id),review.authorization_sha256):
            raise UseAdmissionError("composition_batch_changed")
        return value

    def _use_report(self, reference, sha):
        report = CompositionUseReview.model_validate_json(self._file(reference,sha))
        for item in (*report.license_evidence,*report.dependency_findings):
            self._file(item.terms_reference,item.terms_sha256)
        if not {"retained_media_composition","local_renderer_and_tools"}.issubset(v.dependency for v in report.dependency_findings):
            raise UseAdmissionError("composition_activity_evidence_required")
        return report

    def _match(self, project_id, report, c):
        if (report.project_id != project_id or report.selection != c["selection"] or report.request_sha256 != c["request_sha256"]
            or report.proof != c["proof"] or c["missing"]):
            raise UseAdmissionError("composition_exact_use_changed")
        if {v.admission_id for v in report.license_evidence} != {v.admission_id for v in c["scope"].bindings}:
            raise UseAdmissionError("composition_all_origin_license_required")
        required = set(c["proof"]["dependencies"])
        if not required.issubset(v.dependency for v in report.dependency_findings):
            raise UseAdmissionError("composition_media_license_required")
        for item in c["scope"].bindings:
            origin = self.admissions.get(project_id,item.admission_id)
            if origin is None or origin.revoked_at or origin.review_sha256 != item.review_sha256:
                raise UseAdmissionError("composition_origin_permission_revoked")

    def adopt(self, project_id, payload, key):
        actor = configured_admission_actor(key)
        if actor is None:
            raise UseAdmissionError("execution_admission_operator_required")
        report = self._use_report(payload.review_reference,payload.review_sha256)
        c = self._context(project_id,report.selection)
        self._match(project_id,report,c)
        with self.db.transaction(immediate=True):
            self._fence(c["token"])
            old = next((v for v in self.records.list("admission",project_id) if v["value"]["idempotency_key"] == payload.idempotency_key),None)
            if old:
                value = self._receipt(UUID(old["id"]),c,project_id)
                if value.actor_id != actor or value.review_reference != payload.review_reference or value.review_sha256 != payload.review_sha256:
                    raise UseAdmissionError("composition_admission_conflict")
                return value
            value = CompositionAdmission(project_id=project_id,idempotency_key=payload.idempotency_key,
                review_reference=payload.review_reference,review_sha256=payload.review_sha256,review=report,
                actor_id=actor,created_at=datetime.now(timezone.utc))
            self.records.save("admission",value.id,dict(project_id=str(project_id),value=value.model_dump(mode="json")))
            return value

    def _receipt(self, identity, c, project_id):
        raw = self.records.get("admission",identity)
        if raw is None:
            raise UseAdmissionError("composition_use_admission_required")
        value = CompositionAdmission.model_validate_json(json.dumps(raw["value"]))
        if value.id != identity or value.project_id != project_id or value.revoked_at:
            raise UseAdmissionError("composition_use_admission_revoked_or_changed")
        if self._use_report(value.review_reference,value.review_sha256) != value.review:
            raise UseAdmissionError("composition_use_evidence_changed")
        self._match(project_id,value.review,c)
        return value

    def revoke(self, project_id, identity, reason, key):
        actor = configured_admission_actor(key)
        if actor is None or not reason.strip():
            raise UseAdmissionError("execution_admission_operator_required")
        with self.db.transaction(immediate=True):
            raw = self.records.get("admission",identity)
            if raw is None or raw["project_id"] != str(project_id):
                raise UseAdmissionError("composition_use_admission_required")
            value = CompositionAdmission.model_validate_json(json.dumps(raw["value"]))
            if not value.revoked_at:
                value = value.model_copy(update=dict(revoked_at=datetime.now(timezone.utc),revoked_by=actor,revocation_reason=reason))
                self.records.save("admission",identity,dict(raw,value=value.model_dump(mode="json")),update=True)
            return value

    def _create(self, project_id, c, receipt, batch, idempotency_key):
        previous = self.db.connection.execute("SELECT operation_id FROM evaluation_composition_slots WHERE batch_id=? AND slot='render'",(batch["id"],)).fetchone()
        if previous:
            owner = self.records.owner(run_id=previous[0])
            if owner is None or owner["request_sha256"] != c["request_sha256"]:
                raise UseAdmissionError("composition_render_allowance_spent")
            return UUID(previous[0])
        with self.db.transaction(immediate=True):
            self._fence(c["token"])
            previous = self.db.connection.execute("SELECT operation_id FROM evaluation_composition_slots WHERE batch_id=? AND slot='render'",(batch["id"],)).fetchone()
            if previous:
                raise UseAdmissionError("composition_concurrent_submission_reload")
            if self.db.connection.execute("SELECT 1 FROM production_runs WHERE idempotency_key=?",(idempotency_key,)).fetchone():
                raise UseAdmissionError("composition_run_key_conflict")
            self._check_render_budget(project_id,c)
            run_id,job_id,render_id = uuid4(),uuid4(),uuid4()
            now = datetime.now(timezone.utc)
            job = Job(id=job_id,project_id=project_id,type=JobType.RENDER,idempotency_key=f"composition:{batch['id']}:render",
                created_at=now,updated_at=now,payload=RenderVideoJobPayload(project_id=project_id,render_id=render_id,video_spec=c["spec"]))
            JobRepository(self.db).create(job)
            self.db.connection.execute("""INSERT INTO production_runs(id,project_id,idempotency_key,preflight_fingerprint,
                draft_version,script_revision,style_tokens,repair_allowance,stage,render_job_id,created_at,talking_review_policy_version)
                VALUES (?,?,?,?,?,?,?,0,'evaluation_composition',?,?,?)""",(str(run_id),str(project_id),idempotency_key,c["request_sha256"],
                c["draft"].version,c["draft"].script_revision,c["spec"].edit_plan.style_tokens.model_dump_json(),str(job_id),now.isoformat(),
                min((v["run"].review_policy_version for v in c["inputs"].values() if v["run"] is not None),default=1)))
            self.db.connection.execute("INSERT INTO evaluation_composition_slots VALUES (?,'render',?)",(batch["id"],str(run_id)))
            self.db.connection.execute("INSERT INTO evaluation_composition_owners VALUES (?,?,?,?,?,?,?,?)",
                (str(run_id),str(project_id),str(job_id),str(render_id),batch["id"],c["request_sha256"],job_use_identity(job),c["scope"].fingerprint))
            self.records.save("binding",run_id,dict(project_id=str(project_id),selection=c["selection"].model_dump(mode="json"),
                admission_id=str(receipt.id),admission_sha256=digest(receipt.model_dump(mode="json")),batch_id=batch["id"],
                request_sha256=c["request_sha256"],proof=c["proof"]))
            for kind,identity,sha in (("run",run_id,c["request_sha256"]),("job",job_id,job_use_identity(job)),("render",render_id,c["request_sha256"])):
                self.db.connection.execute("INSERT INTO execution_use_scopes(subject_kind,subject_id,project_id,fingerprint,payload,subject_sha256) VALUES (?,?,?,?,?,?)",
                    (kind,str(identity),str(project_id),c["scope"].fingerprint,c["scope"].model_dump_json(),sha))
            # Retain generated media's internal-only restriction across aliases.
            # Reading an original Clip does not rewrite its own license scope.
            # These are use origins of this real render Run, not fabricated
            # generation parents or mutations of the original asset records.
            for sha in {c["audio"].content_hash,*(v["asset"].content_hash for v in c["inputs"].values() if v["run"] is not None)}:
                self.db.connection.execute("INSERT OR IGNORE INTO execution_use_content_origins(content_hash,subject_kind,subject_id) VALUES (?,'run',?)",
                    (sha,str(run_id)))
            from app.source_use_constraints import SourceUseConstraintService
            SourceUseConstraintService(self.db,self.root).snapshot_render(project_id,job_id)
        return run_id

    def require_job_identity(self, job):
        if job is None:
            raise UseAdmissionError("composition_job_missing")
        owner = self.records.owner(job_id=job.id)
        if owner is None:
            raise UseAdmissionError("composition_job_owner_missing")
        binding = self.records.get("binding",owner["run_id"])
        row = self.db.connection.execute("SELECT * FROM production_runs WHERE id=?",(owner["run_id"],)).fetchone()
        scope = read_subject_scope(self.db,"job",job.id)
        slot = self.db.connection.execute("SELECT operation_id FROM evaluation_composition_slots WHERE batch_id=? AND slot='render'",(owner["batch_id"],)).fetchone()
        if (binding is None or row is None or row["stage"] != "evaluation_composition" or row["render_job_id"] != str(job.id)
            or row["project_id"] != str(job.project_id) or owner["project_id"] != str(job.project_id)
            or job.type != JobType.RENDER or not isinstance(job.payload,RenderVideoJobPayload)
            or str(job.payload.render_id) != owner["render_id"] or job_use_identity(job) != owner["job_sha256"]
            or binding["request_sha256"] != owner["request_sha256"] or str(job.project_id) != binding["project_id"]
            or binding["proof"]["video_spec"] != job.payload.video_spec.model_dump(mode="json")
            or slot is None or slot[0] != owner["run_id"] or scope is None or scope.fingerprint != owner["scope_fingerprint"]):
            raise UseAdmissionError("composition_job_identity_changed")
        for kind,identity,sha in (("run",owner["run_id"],owner["request_sha256"]),("render",owner["render_id"],owner["request_sha256"]),("job",job.id,owner["job_sha256"])):
            saved = self.db.connection.execute("SELECT fingerprint,subject_sha256 FROM execution_use_scopes WHERE subject_kind=? AND subject_id=?",(kind,str(identity))).fetchone()
            if saved is None or tuple(saved) != (owner["scope_fingerprint"],sha) or read_subject_scope(self.db,kind,UUID(str(identity))) != scope:
                raise UseAdmissionError("composition_scope_changed")
        return binding,owner,scope

    def require_media_identity(self, job_id, asset):
        job = JobRepository(self.db).get(job_id)
        binding,owner,_ = self.require_job_identity(job)
        known = [v["asset"] for v in binding["proof"]["inputs"]]
        is_audio = str(asset.id) == str(job.payload.video_spec.master_narration.audio_asset_id)
        if is_audio:
            if asset.content_hash != binding["proof"]["audio_sha256"]:
                raise UseAdmissionError("composition_audio_identity_changed")
        elif asset.model_dump(mode="json") not in known:
            raise UseAdmissionError("composition_media_not_selected")
        return True

    def require_current_job(self, job):
        binding,owner,scope = self.require_job_identity(job)
        c = self._context(job.project_id,CompositionSelection.model_validate(binding["selection"]))
        receipt = self._receipt(UUID(binding["admission_id"]),c,job.project_id)
        batch = self._batch(UUID(owner["batch_id"]))
        if (c["request_sha256"] != owner["request_sha256"] or c["proof"] != binding["proof"]
            or scope != c["scope"] or digest(receipt.model_dump(mode="json")) != binding["admission_sha256"]
            or (batch["review"].get("master_run_id") or batch["review"].get("master_audio_id")) != str(c["selection"].parent_id)):
            raise UseAdmissionError("composition_current_proof_changed")
        from app.source_use_constraints import SourceUseConstraintService
        SourceUseConstraintService(self.db,self.root).require_render_snapshot(job)
        return c

    def get(self, project_id, identity):
        owner = self.records.owner(run_id=identity)
        if owner is None or owner["project_id"] != str(project_id):
            raise UseAdmissionError("composition_run_owner_missing")
        job = JobRepository(self.db).get(UUID(owner["job_id"]))
        result = dict(id=str(identity),project_id=str(project_id),policy=POLICY,purpose="internal_evaluation",
            render_job_id=owner["job_id"],render_id=owner["render_id"],automatic_retry=False,current_eligible=False,reasons=[],
            state="blocked",binding=self.records.get("binding",identity),technical_qa=None,final_u_product="required")
        try:
            self.require_current_job(job)
            result.update(current_eligible=True,state=job.status.value)
            saved = self.records.get("result",identity)
            if saved is not None:
                self.require_result(job)
                result.update(technical_qa=saved["evidence"],state="awaiting_u_product" if job.status == JobStatus.COMPLETED else "reconciliation_required")
        except (ValueError,KeyError,TypeError,OSError) as exc:
            result["reasons"] = [str(exc) if isinstance(exc,ValueError) else "composition_evidence_invalid"]
        return result

    def require_result(self, job):
        from app.db import ProviderCallRepository
        binding,owner,scope = self.require_job_identity(job)
        value = self.records.get("result",owner["run_id"])
        call = ProviderCallRepository(self.db).get_by_idempotency_key(f"composition:{owner['batch_id']}:render")
        if value is None or call is None or call.status != "completed" or call.result_payload != value["evidence"]:
            raise UseAdmissionError("composition_completed_result_required")
        path = self.output_path(owner)
        sha = value["evidence"]["sha256"]
        if file_digest(path) != sha:
            raise UseAdmissionError("composition_output_bytes_changed")
        origins = self.db.connection.execute("SELECT content_hash FROM execution_use_content_origins WHERE subject_kind='render' AND subject_id=?",(owner["render_id"],)).fetchall()
        if [r[0] for r in origins] != [sha]:
            raise UseAdmissionError("composition_output_origin_changed")
        for alias in self.scopes.scopes_for_hash(sha):
            if alias != scope:
                raise UseAdmissionError("composition_output_alias_changed")
        return path,value

    def output_path(self, owner):
        # Private result storage is never mounted as ordinary renders.
        path = (self.root / "evaluation-composition" / owner["project_id"] / owner["run_id"] / "verified.mp4").resolve()
        if not path.is_relative_to(self.root):
            raise UseAdmissionError("composition_output_outside_data_root")
        return path

    def media(self, project_id, identity, purpose):
        if purpose != "internal_evaluation":
            raise UseAdmissionError("evaluation_scope_not_commercial")
        owner = self.records.owner(run_id=identity)
        if owner is None or owner["project_id"] != str(project_id):
            raise UseAdmissionError("composition_run_owner_missing")
        job = JobRepository(self.db).get(UUID(owner["job_id"]))
        self.require_current_job(job)
        if job.status != JobStatus.COMPLETED:
            raise UseAdmissionError("composition_terminal_job_required")
        return self.require_result(job)[0]


def claim_talking_slot(db, operation):
    """Called within normal Talking create; one slot survives new grants/keys."""
    row = db.connection.execute("SELECT id FROM evaluation_composition_batches WHERE project_id=? AND master_run_id=?",
        (str(operation.project_id),str(operation.master_run_id))).fetchone()
    if row is None:
        return
    old = db.connection.execute("SELECT operation_id FROM evaluation_composition_slots WHERE batch_id=? AND slot=?",
        (row[0],"talking:"+str(operation.selection.scene_plan_id))).fetchone()
    if old and old[0] != str(operation.id):
        from app.evaluation_talking import EvaluationTalkingService
        from app.evaluation_talking_recovery import TalkingRecoveryService
        TalkingRecoveryService(EvaluationTalkingService(db,getattr(db,"data_root",Path(db.path).parent))).require_successor(operation)
        return
    service = EvaluationCompositionService(db,getattr(db,"data_root",Path(db.path).parent))
    batch = service._batch(UUID(row[0]))
    if str(operation.selection.scene_plan_id) not in batch["review"]["scene_ids"]:
        raise UseAdmissionError("composition_scene_not_in_call_allowance")
    slot = "talking:"+str(operation.selection.scene_plan_id)
    old = db.connection.execute("SELECT operation_id FROM evaluation_composition_slots WHERE batch_id=? AND slot=?",(row[0],slot)).fetchone()
    if old and old[0] != str(operation.id):
        raise UseAdmissionError("composition_talking_allowance_spent")
    if old is None:
        db.connection.execute("INSERT INTO evaluation_composition_slots VALUES (?,?,?)",(row[0],slot,str(operation.id)))


def require_talking_slot(db, operation):
    row = db.connection.execute("SELECT id FROM evaluation_composition_batches WHERE project_id=? AND master_run_id=?",
        (str(operation.project_id),str(operation.master_run_id))).fetchone()
    if row is None:
        return
    value = CompositionRepository(db).get("batch",row[0])
    if value is None:
        raise UseAdmissionError("composition_batch_missing")
    # Accepted older samples outside the missing-scene grant remain reusable.
    if str(operation.selection.scene_plan_id) not in value["review"]["scene_ids"]:
        return
    slot = db.connection.execute("SELECT operation_id FROM evaluation_composition_slots WHERE batch_id=? AND slot=?",
        (row[0],"talking:"+str(operation.selection.scene_plan_id))).fetchone()
    if slot is None or slot[0] != str(operation.id):
        from app.evaluation_talking import EvaluationTalkingService
        from app.evaluation_talking_recovery import TalkingRecoveryService
        TalkingRecoveryService(EvaluationTalkingService(db,getattr(db,"data_root",Path(db.path).parent))).require_successor(operation)

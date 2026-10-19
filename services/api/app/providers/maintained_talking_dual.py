"""Owned application3.12 / Conda engine3.10; no installer or model imports."""
from io import BytesIO
from zipfile import ZipFile, ZIP_STORED, ZipInfo
from pathlib import Path
from tempfile import TemporaryDirectory
from email.parser import BytesParser
import hashlib
import json
import os
import shutil
import subprocess
import time

from app.domain.models import ExecutionSpecificationV10, DualTalkingStartup, DualTalkingResourceCopy, ExecutionMachineObservation, execution_canonical_sha256
from app.domain.maintained_runtime import MaintainedRuntimeInventory, MaintainedDualTalkingOrigin
from app.domain.execution_runtime import RuntimeInventoryFile
from app.domain.talking_child import TalkingChildRequest
from app.providers.runtime_tree import _exact_path, _digest
from app.providers.prepared import ExecutionPreparationError
from .maintained_runtime import (_observe_inventory, _distribution_versions, _evidence_bytes, _distinct_json,
    observe_host, _system_root, stop_maintained_process_tree)
from .maintained_voice_bundle import bundle_bytes, BUNDLE_SLOT
from .maintained_talking_execution import (observe_talking_artifacts, require_reference_coverage,
    capture_admitted_talking_files, verify_admitted_talking_files)
from app.execution_admission import configured_admission_actor, UseAdmissionError
from app.runtime_inventory import RuntimeInventoryStore

ENGINE_SLOT = "entry/engine.pyz"
ENGINE_MAX_BYTES = 128*1024


def application_bundle():
    source = Path(__file__).with_name("asr_no_site_bootstrap.py").read_bytes()
    source = source.replace(b"from app.providers.maintained_asr_child import main", b"from app.providers.maintained_talking_dual import child_main as main")
    return bundle_bytes(source)


def engine_bundle():
    stream = BytesIO()
    with ZipFile(stream, "w", compression=ZIP_STORED) as z:
        for name, source in (("__main__.py", "talking_engine_bootstrap.py"), ("compatibility.py", "talking_attention_compatibility.py")):
            path = Path(__file__).with_name(source); _exact_path(path, directory=False)
            info = ZipInfo(name, date_time=(1980,1,1,0,0,0)); info.create_system=3; info.external_attr=0o100644<<16
            z.writestr(info,path.read_bytes())
    raw=stream.getvalue()
    if not 0<len(raw)<=ENGINE_MAX_BYTES: raise ExecutionPreparationError("maintained_engine_bundle_limit")
    return raw


def observe_application_inventory(environment, base, *, max_bytes):
    return _observe_inventory(environment,base,max_bytes=max_bytes,entry_name=BUNDLE_SLOT,entry_source=application_bundle())


def observe_engine_inventory(engine, *, max_bytes):
    engine=Path(engine); _exact_path(engine,directory=True)
    entries=[]; directories={"engine","entry"}; total=0
    for directory, folders, files in os.walk(engine,followlinks=False,onerror=lambda _e: (_ for _ in ()).throw(ExecutionPreparationError("maintained_engine_inventory_unavailable"))):
        current=Path(directory); _exact_path(current,directory=True)
        for name in folders:
            path=current/name; _exact_path(path,directory=True); directories.add("engine/"+path.relative_to(engine).as_posix())
        for name in sorted(files):
            path=current/name; logical="engine/"+path.relative_to(engine).as_posix()
            size,sha=_digest(path,max_bytes-total); total+=size
            entries.append(RuntimeInventoryFile(name=logical,role="interpreter" if logical=="engine/python.exe" else "dependency",size_bytes=size,sha256=sha))
            if len(entries)>=100_000 or len(directories)>20_000: raise ExecutionPreparationError("maintained_engine_inventory_limit")
    raw=engine_bundle(); total+=len(raw)
    if total>max_bytes: raise ExecutionPreparationError("maintained_engine_inventory_limit")
    entries.append(RuntimeInventoryFile(name=ENGINE_SLOT,role="entrypoint",size_bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()))
    return MaintainedRuntimeInventory(inventory_version=2,directories=tuple(sorted(directories)),files=tuple(entries))


def distributions(options):
    result={}
    for key,version in _distribution_versions(Path(options["environment"]),Path(options["base"])).items():
        role,name=key.split("/",1); result[(role,"cpython" if name=="cpython" else "pip",name)]=(version,"")
    engine=Path(options["engine"])
    for p in sorted((engine/"conda-meta").glob("*.json")):
        if p.stat().st_size>1024**2: raise ExecutionPreparationError("maintained_conda_metadata_limit")
        value=_distinct_json(p.read_bytes())
        name,version,build=(value.get(k) for k in ("name","version","build"))
        key=("engine","conda",name.casefold() if isinstance(name,str) else "")
        if not all(isinstance(v,str) and v and v==v.strip() for v in (name,version,build)) or key in result:
            raise ExecutionPreparationError("maintained_conda_metadata_invalid")
        result[key]=(version,build)
    if not result.get(("engine","conda","python"),("",))[0].startswith("3.10."):
        raise ExecutionPreparationError("maintained_engine_python_unsupported")
    for p in sorted((engine/"Lib/site-packages").glob("*.dist-info/METADATA")):
        if p.stat().st_size>1024**2: raise ExecutionPreparationError("maintained_distribution_metadata_limit")
        value=BytesParser().parsebytes(p.read_bytes()); name,version=value.get("Name"),value.get("Version")
        key=("engine","pip",name.casefold() if name else "")
        if (not name or not version or key in result or len(value.get_all("Name",[]))!=1 or len(value.get_all("Version",[]))!=1):
            raise ExecutionPreparationError("maintained_distribution_metadata_invalid")
        result[key]=(version,"")
    # Manager namespaces are distinct identities, including version schemes.
    # Keep both labels in complete-set origin equality and both metadata files
    # in the byte inventory; same-manager duplicates are rejected above.
    return result


def require_dual_origin(root, reference, application, engine, *, current=True):
    try:
        origin=MaintainedDualTalkingOrigin.model_validate_json(json.dumps(_distinct_json(_evidence_bytes(root,reference))))
        for component,inventory,slot,expected in (("application",application,BUNDLE_SLOT,application_bundle if current else None),
                                                ("engine",engine,ENGINE_SLOT,engine_bundle if current else None)):
            part=next(p for p in origin.components if p.role==component)
            hooks=sorted(r.name for r in inventory.files if r.name.casefold().endswith((".pth","._pth")) or r.name.rsplit("/",1)[-1].casefold() in ("sitecustomize.py","usercustomize.py"))
            if part.inventory_sha256!=inventory.descriptor.sha256 or sorted(part.startup_hooks)!=hooks:
                raise ValueError()
            entries=[r for r in inventory.files if r.role=="entrypoint"]
            interpreters=[r.name for r in inventory.files if r.role=="interpreter"]
            if interpreters != ["environment/Scripts/python.exe" if component=="application" else "engine/python.exe"]:
                raise ValueError()
            maximum=8*1024**2 if component=="application" else ENGINE_MAX_BYTES
            if len(entries)!=1 or entries[0].name!=slot or not 0<entries[0].size_bytes<=maximum: raise ValueError()
            if expected:
                raw=expected()
                if entries[0].sha256!=hashlib.sha256(raw).hexdigest() or entries[0].size_bytes!=len(raw): raise ValueError()
            for d in part.distributions: _evidence_bytes(root,d.source)
        return origin
    except Exception:
        raise ExecutionPreparationError("maintained_dual_origin_invalid") from None


def _startup(options, application, engine):
    roots={k:Path(options[k]) for k in ("environment","base","engine","vendor","hf","torch","tools")}
    for p in roots.values(): _exact_path(p,directory=True)
    # Runtime and resource installations must be disjoint: private writes never enter them.
    values=list(roots.values())
    if any(a.is_relative_to(b) or b.is_relative_to(a) for i,a in enumerate(values) for b in values[i+1:]):
        raise ExecutionPreparationError("maintained_dual_layout_invalid")
    for inventory in (application,engine):
        if any(r.name.casefold().endswith("._pth") for r in inventory.files):
            raise ExecutionPreparationError("maintained_dual_no_site_pth_unsupported")
    for path in (roots["environment"]/"Scripts/python._pth", roots["environment"]/"Scripts/python312._pth",
        roots["environment"]/"python._pth", roots["environment"]/"python312._pth",
        roots["engine"]/"python._pth", roots["engine"]/"python310._pth"):
        if path.exists():
            raise ExecutionPreparationError("maintained_dual_no_site_pth_unsupported")
    version=next((v.strip() for k,v in (line.split("=",1) for line in (roots["environment"]/"pyvenv.cfg").read_text().splitlines() if "=" in line) if k.strip()=="version"),"")
    if not version.startswith("3.12."): raise ExecutionPreparationError("maintained_application_python_unsupported")
    for p in (roots["environment"]/"Lib/site-packages",roots["base"]/"Lib",roots["base"]/"DLLs",
              roots["engine"]/"Lib",roots["engine"]/"DLLs",roots["engine"]/"Lib/site-packages",roots["engine"]/"Library/bin"):
        _exact_path(p,directory=True)
    return DualTalkingStartup(policy="latentsync-dual-no-site-v1",**{k+"_root":str(p) for k,p in roots.items()})


def observe_dual(options, *, inventories=None):
    if options.get("invocation_policy")!="maintained-latentsync-dual-runtime-v1":
        raise ExecutionPreparationError("maintained_dual_policy_required")
    root,staging=Path(options["data_root"]),Path(options["staging_parent"])
    engine=Path(options["engine"]); _exact_path(root,directory=True); _exact_path(staging,directory=True)
    if not staging.is_relative_to(root) or any(staging.is_relative_to(Path(options[k])) or Path(options[k]).is_relative_to(staging) for k in ("environment","base","engine","vendor","hf","torch","tools")):
        raise ExecutionPreparationError("maintained_dual_layout_invalid")
    if inventories is None:
        app=observe_application_inventory(Path(options["environment"]),Path(options["base"]),max_bytes=options["max_bytes"])
        eng=observe_engine_inventory(engine,max_bytes=options["max_bytes"]-app.descriptor.total_file_bytes)
    else:
        app,eng=inventories
        if (type(app) is not MaintainedRuntimeInventory or type(eng) is not MaintainedRuntimeInventory
            or app.descriptor.total_file_bytes + eng.descriptor.total_file_bytes > options["max_bytes"]):
            raise ExecutionPreparationError("maintained_talking_inventory_invalid")
    startup=_startup(options,app,eng)
    origin=require_dual_origin(root,options["origin_reference"],app,eng)
    if configured_admission_actor(options["operator_key"])!=origin.actor:
        raise ExecutionPreparationError("maintained_talking_operator_required")
    declared={(d.root,d.kind,d.name.casefold()):(d.version,d.build) for p in origin.components for d in p.distributions}
    if declared!=distributions(options): raise ExecutionPreparationError("maintained_distribution_origin_mismatch")
    host=observe_host(Path(options["environment"]),Path(options["base"]),Path(options["vendor"]),root,device="cuda:0",gpu_uuid=options["gpu_uuid"])
    # Extend, rather than reinterpret, the existing host/device layout digest.
    host=host.model_copy(update={"layout_sha256":execution_canonical_sha256(dict(observed=host.layout_sha256,startup=startup.model_dump(mode="json")))})
    artifacts=observe_talking_artifacts(options)
    return ExecutionSpecificationV10(schema_version=10,trust_policy="maintained-local-process-v1",capability="talking",mode="local",
        provider="latentsync",model="LatentSync-1.5",runtime=options["runtime_label"],machine_id=options["machine_id"],
        observation_recipe="latentsync-maintained-dual-runtime",observation_version=1,invocation_policy=options["invocation_policy"],
        timeout_seconds=options["timeout_seconds"],model_artifacts=artifacts,runtime_inventory=app.descriptor,
        engine_inventory=eng.descriptor,runtime_origin=options["origin_reference"],startup=startup,host_runtime=host,
        resource_copies=tuple(DualTalkingResourceCopy(
            source_name=r.name,destination=r.name.removeprefix("vendor/")) for r in artifacts if r.name.startswith("vendor/configs/")
            or r.name in {"vendor/checkpoints/whisper/tiny.pt","vendor/latentsync/utils/mask.png"}),
        machine=ExecutionMachineObservation(installation_id=options["installation_id"],host_sha256=host.host_sha256,
            device_sha256=execution_canonical_sha256(host.model_dump(mode="json"))),parameters=options["parameters"])


def _assert_admitted_v10_options(options, spec):
    reference = options["origin_reference"]
    if (type(spec) is not ExecutionSpecificationV10 or spec.capability != "talking"
        or spec.provider != "latentsync" or spec.model != "LatentSync-1.5" or spec.mode != "local"
        or spec.runtime != options["runtime_label"] or spec.machine_id != options["machine_id"]
        or spec.timeout_seconds != options["timeout_seconds"] or spec.invocation_policy != options["invocation_policy"]
        or spec.parameters != options["parameters"] or spec.runtime_origin != reference
        or spec.machine.installation_id != options["installation_id"]
        or spec.host_runtime.gpu_uuid != options["gpu_uuid"]):
        raise ExecutionPreparationError("maintained_talking_child_identity_changed")


def _require_admitted_v10(options, spec):
    _assert_admitted_v10_options(options, spec)
    root = Path(options["data_root"])
    store = RuntimeInventoryStore(root)
    application = store.read_v2(spec.runtime_inventory)
    engine = store.read_v2(spec.engine_inventory)
    origin = require_dual_origin(root, options["origin_reference"], application, engine)
    if configured_admission_actor(options["operator_key"]) != origin.actor:
        raise ExecutionPreparationError("maintained_talking_operator_required")
    startup = _startup(options, application, engine)
    if startup != spec.startup:
        raise ExecutionPreparationError("maintained_talking_child_identity_changed")
    declared = {(d.root, d.kind, d.name.casefold()): (d.version, d.build)
        for part in origin.components for d in part.distributions}
    if declared != distributions(options):
        raise ExecutionPreparationError("maintained_distribution_origin_mismatch")
    host = observe_host(Path(options["environment"]), Path(options["base"]), Path(options["vendor"]), root,
        device="cuda:0", gpu_uuid=options["gpu_uuid"])
    host = host.model_copy(update={"layout_sha256": execution_canonical_sha256(
        dict(observed=host.layout_sha256, startup=startup.model_dump(mode="json")))})
    if host != spec.host_runtime:
        raise ExecutionPreparationError("maintained_talking_child_identity_changed")
    stamps = capture_admitted_talking_files(options, spec, application, engine)
    return application, engine, origin, stamps


def _safe_copy(source,target,expected=None):
    _exact_path(source,directory=False)
    size,sha=_digest(source,4*1024**3)
    if expected and (size,sha)!=expected: raise ExecutionPreparationError("maintained_resource_changed")
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists(): raise ExecutionPreparationError("maintained_resource_destination_exists")
    shutil.copyfile(source,target); _exact_path(target,directory=False)
    if _digest(target,4*1024**3)!=(size,sha): raise ExecutionPreparationError("maintained_resource_changed")
    return dict(source=str(source),destination=str(target),size_bytes=size,sha256=sha)


class OwnedDualLatentSync:
    """Concrete internal adapter wrapper; never supplied as a caller callback."""
    def __init__(self,options,private,deadline):
        self.options,self.private,self.deadline=options,Path(private),deadline

    def remaining(self):
        value=self.deadline-time.monotonic()
        if value<=0: raise ExecutionPreparationError("maintained_talking_child_timeout")
        return value

    def run(self,argv,cwd,_timeout):
        executable=Path(argv[0])
        if executable not in {Path(self.options["tools"])/"ffmpeg.exe",Path(self.options["tools"])/"ffprobe.exe"}:
            raise ExecutionPreparationError("maintained_owned_command_invalid")
        return self.process(argv,cwd,_environment(self.options,self.private))

    def _retain_failed_command(self, stdout, stderr, *, returncode, reason):
        """Keep bounded private tails before temporary command logs disappear.

        No raw command/environment/exception text enters the metadata or public
        errors. These logs are private execution evidence, never media Assets.
        """
        folder = self.private / "command-failure"
        folder.mkdir(exist_ok=False)
        record = dict(returncode=returncode, reason=reason, private_only=True, logs={})
        for label, source in (("stdout", stdout), ("stderr", stderr)):
            size = source.stat().st_size
            with source.open("rb") as stream:
                stream.seek(max(0, size - 512*1024))
                raw = stream.read(512*1024)
            (folder / (label + ".log")).write_bytes(raw)
            record["logs"][label] = dict(original_bytes=size, retained_bytes=len(raw), truncated=size > len(raw))
        (folder / "metadata.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")

    def process(self,argv,cwd,env):
        with TemporaryDirectory(prefix="command-",dir=self.private) as logs:
            stdout,stderr=Path(logs)/"stdout",Path(logs)/"stderr"
            with stdout.open("wb") as out,stderr.open("wb") as err:
                child=None
                try:
                    child=subprocess.Popen(list(argv),cwd=cwd,env=env,shell=False,stdout=out,stderr=err,
                        **({"creationflags":subprocess.CREATE_NEW_PROCESS_GROUP} if os.name=="nt" else {"start_new_session":True}))
                    code=child.wait(timeout=self.remaining())
                except BaseException:
                    # End the process tree before closing/removing its log files.
                    if child is not None and child.poll() is None:stop_maintained_process_tree(child)
                    raise
            if stdout.stat().st_size+stderr.stat().st_size>1024**2:
                self._retain_failed_command(stdout,stderr,returncode=code,reason="output_limit")
                raise ExecutionPreparationError("maintained_owned_output_limit")
            if code != 0:
                self._retain_failed_command(stdout,stderr,returncode=code,reason="nonzero_exit")
            return subprocess.CompletedProcess(argv,code,stdout.read_text(errors="replace"),stderr.read_text(errors="replace"))

    def inference(self,video,audio,target):
        work=self.private/"work"; work.mkdir()
        mapping=[]
        for source,name in ((video,"reference.mp4"),(audio,"narration-model.wav")):
            mapping.append(_safe_copy(Path(source),work/name))
        vendor=Path(self.options["vendor"])
        identities={row["name"]:(row["size_bytes"],row["sha256"]) for row in self.options["model_artifacts"]}
        for row in self.options["resource_copies"]:
            source_name,name=row["source_name"],row["destination"]
            if not source_name.startswith("vendor/") or name!=source_name.removeprefix("vendor/") or source_name not in identities:
                raise ExecutionPreparationError("maintained_resource_identity_missing")
            mapping.append(_safe_copy(vendor/name,work/name,identities[source_name]))
        (self.private/"resource-map.json").write_text(json.dumps(mapping,sort_keys=True),encoding="utf-8")
        p=self.options["parameters"]
        record=dict(policy="latentsync-dual-no-site-v1",engine=str(self.options["engine"]),vendor=str(vendor),work=str(work),
            hf=str(self.options["hf"]),torch=str(self.options["torch"]),tools=str(self.options["tools"]),checkpoint=str(self.options["checkpoint"]),
            steps=p["inference_steps"],guidance=p["guidance_scale"],seed=p["seed"],attention=p["attention_policy"],allocator=p["allocator"])
        request=self.private/"engine-request.json"; request.write_text(json.dumps(record),encoding="utf-8")
        env=_environment(self.options,self.private,engine=True)
        result=self.process([str(Path(self.options["engine"])/"python.exe"),"-I","-S","-B",str(self.private/"engine.pyz"),str(request)],work,env)
        if result.returncode!=0: raise ExecutionPreparationError("maintained_talking_engine_failed")
        if json.loads(request.read_text())!=record: raise ExecutionPreparationError("maintained_talking_child_identity_changed")
        for row in mapping:
            if _digest(Path(row["destination"]),4*1024**3)!=(row["size_bytes"],row["sha256"]):
                raise ExecutionPreparationError("maintained_resource_changed")
        _safe_copy(work/"generated.mp4",Path(target))

    def synthesize(self,request):
        from .latentsync import LatentSyncProvider
        from .talking import TalkingReference
        owner=self
        # This exact internal subclass only reuses existing media transforms;
        # it is never a prepared provider or an accepted callback authority.
        class Adapter(LatentSyncProvider):
            def _run_inference(self,video,audio,target): owner.inference(video,audio,target)
        p=self.options["parameters"]
        staging=self.private/"adapter"; staging.mkdir()
        adapter=Adapter(Path(self.options["engine"])/"python.exe",staging,self.options["checkpoint"],
            runner_path=Path(self.options["vendor"])/"scripts/inference.py",unet_config_path=Path(self.options["vendor"])/"configs/unet/stage2.yaml",
            ffmpeg_command=Path(self.options["tools"])/"ffmpeg.exe",inference_steps=p["inference_steps"],guidance_scale=p["guidance_scale"],seed=p["seed"],
            timeout_seconds=self.remaining(),command_runner=self.run)
        narration=request.narration
        if request.slice_plan is not None:
            from app.talking.slices import extract_talking_audio_slice
            narration=extract_talking_audio_slice(narration,request.slice_plan,self.private/"slice.wav",
                ffmpeg_command=Path(self.options["tools"])/"ffmpeg.exe",timeout_seconds=self.remaining(),
                command_runner=lambda argv, **_kw: self.run(argv,self.private,self.remaining()))
            narration=narration.model_copy(update={"content_hash":_digest(Path(narration.source_file),4*1024**3)[1]})
        adapter.synthesize(request.profile,narration,TalkingReference(request.reference_clip_id,Path(request.reference_path),request.reference_start_ms,request.reference_end_ms),self.private/"output.mp4")


def _environment(options,private,*,engine=False):
    system=_system_root(); private=Path(private)
    search=[Path(options["tools"]),system/"System32",system]
    if engine: search=[Path(options["engine"]),Path(options["engine"])/"Library/bin",*search]
    env=dict(SystemRoot=str(system),PATH=os.pathsep.join(map(str,search)),TEMP=str(private),TMP=str(private),
        HF_HOME=str(options["hf"]),TORCH_HOME=str(options["torch"]),HF_HUB_OFFLINE="1",TRANSFORMERS_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1",PYTHONDONTWRITEBYTECODE="1",MPLCONFIGDIR=str(private/"mpl"),XDG_CACHE_HOME=str(private/"cache"))
    if engine: env["PYTORCH_CUDA_ALLOC_CONF"]=options["parameters"]["allocator"]
    return env


def child_main():
    import sys
    private=Path(sys.argv[1]).parent
    options=json.loads((private/"options.json").read_bytes())
    request=TalkingChildRequest.model_validate_json((private/"request.json").read_bytes())
    request.verify_inputs(options["data_root"]);require_reference_coverage(request)
    OwnedDualLatentSync(options,private,float(options["deadline"])).synthesize(request)
    return 0


class PreparedDualTalkingChild:
    def __init__(self,temporary,options,spec,request,application_inventory,engine_inventory,origin,file_stamps):
        self._temporary,self._options,self._spec_json,self._request_raw=temporary,options,spec.model_dump_json(),request.model_dump_json().encode()
        self._application_inventory,self._engine_inventory,self._origin=application_inventory,engine_inventory,origin
        self._file_stamps=file_stamps
        self._state="prepared"
        self._files={"application.pyz":application_bundle(),"engine.pyz":engine_bundle(),"request.json":self._request_raw,
            "startup.json":json.dumps(dict(policy="cpython312-no-site-v1",environment_root=str(options["environment"]),base_root=str(options["base"]))).encode()}
        for name,raw in self._files.items(): (Path(temporary.name)/name).write_bytes(raw)

    @property
    def specification(self):return ExecutionSpecificationV10.model_validate_json(self._spec_json)
    def request_snapshot(self):return TalkingChildRequest.model_validate_json(self._request_raw)
    def _verify_files(self):
        for name,raw in self._files.items():
            path=Path(self._temporary.name)/name;_exact_path(path,directory=False)
            if path.read_bytes()!=raw:raise ExecutionPreparationError("maintained_talking_child_identity_changed")
    def _verify_runtime_binding(self,spec):
        _assert_admitted_v10_options(self._options,spec)
        startup=_startup(self._options,self._application_inventory,self._engine_inventory)
        if startup!=spec.startup or configured_admission_actor(self._options["operator_key"])!=self._origin.actor:
            raise ExecutionPreparationError("maintained_talking_child_identity_changed")
        require_dual_origin(Path(self._options["data_root"]),self._options["origin_reference"],
            self._application_inventory,self._engine_inventory,current=True)
        verify_admitted_talking_files(self._file_stamps)
    def verify(self):
        if self._state!="prepared":raise ExecutionPreparationError("maintained_preparation_consumed")
        try:
            self._verify_files(); self.request_snapshot().verify_inputs(self._options["data_root"])
            self._verify_runtime_binding(self.specification)
            return self.specification
        except Exception:
            self._state="failed";raise ExecutionPreparationError("maintained_talking_child_identity_changed") from None
    def execution_use_snapshot(self):
        spec=self.verify()
        return dict(snapshot_version=2,identity=spec.identity.model_dump(mode="json"),execution_specification=spec.model_dump(mode="json"),execution_sha256=spec.execution_sha256)
    def execute(self):
        return self._execute()
    def execute_owned(self, lease):
        from app.jobs.lease_fence import OwnedJobLease
        if type(lease) is not OwnedJobLease:
            raise ExecutionPreparationError("evaluation_talking_owned_lease_required")
        lease.require()
        return self._execute(lease)
    def _execute(self, lease=None):
        spec=self.verify();self._state="consumed"; private=Path(self._temporary.name);child=None
        stdout_log,stderr_log=private/"application-stdout.log",private/"application-stderr.log"
        options={k:str(v) if isinstance(v,Path) else str(v) if k=="installation_id" else v for k,v in self._options.items() if k not in ("origin_reference","operator_key")}
        options["deadline"]=time.monotonic()+spec.timeout_seconds
        options["model_artifacts"]=[row.model_dump(mode="json") for row in spec.model_artifacts]
        options["resource_copies"]=[row.model_dump(mode="json") for row in spec.resource_copies]
        raw=json.dumps(options,sort_keys=True).encode();self._files["options.json"]=raw;(private/"options.json").write_bytes(raw)
        try:
            with stdout_log.open("wb") as stdout,stderr_log.open("wb") as stderr:
                if lease is not None: lease.require()
                child=subprocess.Popen([str(Path(self._options["environment"])/"Scripts/python.exe"),"-I","-S","-B",str(private/"application.pyz"),str(private/"request.json")],
                    cwd=private,env=_environment(self._options,private),shell=False,stdout=stdout,stderr=stderr,
                    **({"creationflags":subprocess.CREATE_NEW_PROCESS_GROUP} if os.name=="nt" else {"start_new_session":True}))
                if child.wait(timeout=max(.001,options["deadline"]-time.monotonic()))!=0:raise ExecutionPreparationError("maintained_talking_child_failed")
            self._verify_files();self.request_snapshot().verify_inputs(self._options["data_root"])
            self._verify_runtime_binding(spec)
            if time.monotonic()>options["deadline"]:raise ExecutionPreparationError("maintained_talking_child_timeout")
            output=private/"output.mp4";_exact_path(output,directory=False)
            if not 0<output.stat().st_size<=4*1024**3:raise ExecutionPreparationError("maintained_talking_output_limit")
            return output
        except BaseException as exc:
            self._state="failed"
            # Keep partial private evidence until explicit reconciliation.
            self._temporary._finalizer.detach()
            stopped=False
            try:
                if child is not None and child.poll() is None:
                    stop_maintained_process_tree(child);stopped=True
            finally:
                (private/"quarantine.json").write_text(json.dumps(dict(output_admitted=False,reconciliation_required=True,
                    process_tree_stop_verified=stopped,reason="runtime_or_cleanup_not_verified")),encoding="utf-8")
                logs={}
                for label,path in (("stdout",stdout_log),("stderr",stderr_log)):
                    if not path.is_file():continue
                    size=path.stat().st_size
                    with path.open("rb") as stream:
                        stream.seek(max(0,size-512*1024));raw=stream.read(512*1024)
                    if len(raw)!=size:path.write_bytes(raw)
                    logs[label]=dict(original_bytes=size,retained_bytes=len(raw),truncated=size>len(raw))
                (private/"application-failure.json").write_text(json.dumps(dict(private_only=True,logs=logs)),encoding="utf-8")
            if isinstance(exc,subprocess.TimeoutExpired):raise ExecutionPreparationError("maintained_talking_child_timeout") from None
            if isinstance(exc,(ExecutionPreparationError,KeyboardInterrupt,UseAdmissionError)):raise
            raise ExecutionPreparationError("maintained_talking_child_failed") from None
    def close(self):
        failed=self._state=="failed";self._state="closed"
        if not failed:self._temporary.cleanup()
    def __enter__(self):return self
    def __exit__(self,*_args):self.close()


def prepare_dual_talking_child(*,request,approved_spec,**options):
    temporary=None
    try:
        options=dict(options);spec=ExecutionSpecificationV10.model_validate_json(approved_spec.model_dump_json())
        application,engine,origin,file_stamps=_require_admitted_v10(options,spec)
        request=TalkingChildRequest.model_validate_json(request.model_dump_json());request.verify_inputs(options["data_root"]);require_reference_coverage(request)
        temporary=TemporaryDirectory(prefix="maintained-dual-talking-",dir=options["staging_parent"])
        prepared=PreparedDualTalkingChild(temporary,options,spec,request,application,engine,origin,file_stamps);prepared.verify();return prepared
    except Exception as exc:
        if temporary:temporary.cleanup()
        if isinstance(exc,ExecutionPreparationError):raise
        raise ExecutionPreparationError("maintained_talking_preparation_invalid") from None

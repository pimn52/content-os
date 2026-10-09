"""Owned retained-source catalog, never a native loader or dispatch plan."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from app.domain.execution_runtime import runtime_path
from .cpu_native_recipe import CPUFileFact, _owned as cpu_owned
from .native_manifest import CPYTHON_COMMON_CONTROLS
from .pe_resources import ResourceClassification, classify_resource_view, read_private_resource_leaves
from .pe_bounded import BoundedPEView, _IMPORT_ENVELOPE_SOURCE
from .private_native_recipe import _unique
from .runtime_primitives import NativeExecutionUnsupported, _exact_path
from .windows_os_policy import owned_os_policy_bytes, parse_os_policy

PROFILE_LIMIT = 64*1024


def classify_acquired_resources(view, *, slot, size_bytes, sha256):
    """Finite owned-member bytes/resources only; all contexts remain pending."""
    policy,_=parse_acquired_descriptor(owned_acquired_bytes())
    row=next((r for r in policy['members'] if r['slot']==slot),None)
    if (row is None or type(view) is not BoundedPEView or type(size_bytes) is not int
            or (size_bytes,sha256)!=(row['size_bytes'],row['sha256'])
            or (view._source.size,view._source.digest)!=(size_bytes,sha256)
            or view._source.state!='open'):
        raise NativeExecutionUnsupported('execution_acquired_view_identity_changed')
    expected=row['resources'];manifest=expected['manifest_sha256']
    common=hashlib.sha256(CPYTHON_COMMON_CONTROLS).hexdigest()
    if manifest==common:
        result=classify_resource_view(view,role='bootstrap_exe' if slot=='python.exe' else 'dll')
    else:
        leaves=read_private_resource_leaves(view)
        selected=[leaf for leaf in leaves or () if leaf.ids[0]==24]
        if manifest is None:
            if selected:raise NativeExecutionUnsupported('execution_acquired_resource_changed')
            result=ResourceClassification('no_resources' if leaves is None else 'data_resources',len(leaves or ()))
        else:
            form=cpu_owned()[0]['manifests'].get(manifest)
            if (form is None or len(selected)!=1 or selected[0].ids!=tuple(form['ids'])
                    or selected[0].codepage!=form['codepage']
                    or selected[0].data!=bytes.fromhex(form['hex'])):
                raise NativeExecutionUnsupported('execution_acquired_resource_changed')
            result=ResourceClassification('requires_private_root_context',len(leaves or ()),manifest)
    if (result.kind,result.leaf_count,result.manifest_sha256)!=(
            expected['kind'],expected['leaf_count'],manifest):
        raise NativeExecutionUnsupported('execution_acquired_resource_changed')
    return result


def owned_import_supplement_bytes():
    path=Path(__file__).with_name('acquired_import_supplement.json')
    _exact_path(path,directory=False)
    with path.open('rb') as source:raw=source.read(PROFILE_LIMIT+1)
    if len(raw)>PROFILE_LIMIT:raise NativeExecutionUnsupported('execution_acquired_supplement_bound')
    return raw


def parse_import_supplement(raw):
    try:
        if type(raw) is not bytes or not 0<len(raw)<=PROFILE_LIMIT or raw!=owned_import_supplement_bytes():
            raise ValueError()
        value=json.loads(raw,object_pairs_hook=_unique)
        if set(value)!={'recipe','version','base_descriptor_sha256','record','catalog_record','source',
                       'dependencies','evidence_level','dispatch_authorized'}:
            raise ValueError()
        policy,identity=parse_acquired_descriptor(owned_acquired_bytes())
        if (value['recipe']!='omnivoice-acquired-import-supplement-v1' or type(value['version']) is not int
                or value['version']!=1 or value['base_descriptor_sha256']!=identity
                or value['dispatch_authorized'] is not False
                or value['evidence_level']!='retained_source_bound_import_envelope_not_current_fd'):
            raise ValueError()
        source=value['source']
        if set(source)!={'slot','size_bytes','sha256'} or (source['size_bytes'],source['sha256'])!=_IMPORT_ENVELOPE_SOURCE:
            raise ValueError()
        row=next(r for r in policy['members'] if r['slot']==source['slot'])
        if row['dependencies'] is not None or (row['size_bytes'],row['sha256'])!=_IMPORT_ENVELOPE_SOURCE:
            raise ValueError()
        for record in (value['record'],value['catalog_record']):
            if set(record)!={'name','sha256'} or not re.fullmatch('[a-f0-9]{64}',record['sha256']):raise ValueError()
            runtime_path(record['name'])
        dependencies=value['dependencies']
        if type(dependencies) is not list or not 0<len(dependencies)<=512 or dependencies!=sorted(set(dependencies)):
            raise ValueError()
        for name in dependencies:
            if not re.fullmatch(r'[a-z0-9_][a-z0-9_.-]*\.(dll|pyd)',name) or '..' in name:raise ValueError()
        return value,hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        raise NativeExecutionUnsupported('execution_acquired_supplement_changed') from None


@dataclass(frozen=True)
class AcquiredImportSupplementDiagnostic:
    base: 'AcquiredSourceDiagnostic'
    supplement_sha256: str
    slot: str
    retained_dependencies: tuple[str,...]
    compared_fact: CPUFileFact | None

    @property
    def dispatch_authorized(self): return False


def describe_acquired_import_supplement(fact=None, *, supplement_sha256=None):
    value,identity=parse_import_supplement(owned_import_supplement_bytes())
    if supplement_sha256 is not None and supplement_sha256!=identity:
        raise NativeExecutionUnsupported('execution_acquired_supplement_identity_changed')
    if fact is not None:
        source=value['source']
        if (type(fact) is not CPUFileFact or fact.name!=source['slot']
                or type(fact.size_bytes) is not int or (fact.size_bytes,fact.sha256)!=_IMPORT_ENVELOPE_SOURCE
                or type(fact.imports) is not tuple or fact.imports!=tuple(value['dependencies'])
                or type(fact.resources) is not ResourceClassification
                or type(fact.resources.leaf_count) is not int
                or fact.resources!=ResourceClassification('no_resources',0)):
            raise NativeExecutionUnsupported('execution_acquired_supplement_fact_changed')
    base=describe_acquired_sources(descriptor_sha256=value['base_descriptor_sha256'])
    return AcquiredImportSupplementDiagnostic(base,identity,value['source']['slot'],
        tuple(value['dependencies']),fact)


def owned_acquired_bytes():
    path = Path(__file__).with_suffix('.json')
    _exact_path(path, directory=False)
    with path.open('rb') as source: raw = source.read(PROFILE_LIMIT+1)
    if len(raw)>PROFILE_LIMIT:
        raise NativeExecutionUnsupported('execution_acquired_profile_bound')
    return raw


def parse_acquired_descriptor(raw):
    """Only exact owned bytes; callers cannot change slots/roots/evidence gates."""
    try:
        if type(raw) is not bytes or not raw or len(raw)>PROFILE_LIMIT:
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=_unique)
        if raw != owned_acquired_bytes():
            raise ValueError()
        if value['recipe']!='omnivoice-acquired-native-static-v1' or type(value['version']) is not int or value['version']!=1:
            raise ValueError()
        containers={c['id']:c for c in value['containers']}
        if len(containers)!=len(value['containers']) or len(containers)>8:raise ValueError()
        for container in containers.values():
            runtime_path(container['name'])
            if (type(container['size_bytes']) is not int or container['size_bytes']<=0
                    or not re.fullmatch('[a-f0-9]{64}',container['sha256'])):raise ValueError()
            chain=set();current=container
            while current['parent'] is not None:
                if current['id'] in chain or current['parent'] not in containers:raise ValueError()
                chain.add(current['id']);current=containers[current['parent']]
        common=hashlib.sha256(CPYTHON_COMMON_CONTROLS).hexdigest()
        manifests=set(cpu_owned()[0]['manifests'])
        slots=set();basenames=set();edges=0
        for category,limit in (('members',32),('sources',16),('terms',16)):
            if not 0<len(value[category])<=limit:raise ValueError()
            for row in value[category]:
                slot=runtime_path(row['slot']);runtime_path(row['member'])
                if slot.casefold() in slots or row['container'] not in containers:raise ValueError()
                slots.add(slot.casefold())
                if (type(row['size_bytes']) is not int or not 0<row['size_bytes']<=
                        (256*1024**2 if category=='members' else 4*1024**2)
                        or not re.fullmatch('[a-f0-9]{64}',row['sha256'])):raise ValueError()
                if category!='members':continue
                resource=row['resources'];manifest=resource['manifest_sha256']
                if (type(resource['leaf_count']) is not int or not 0<=resource['leaf_count']<=256
                        or manifest is not None and manifest not in manifests|{common}
                        or manifest==common and (row['slot'] not in ('python.exe','python312.dll')
                            or resource['kind']!='requires_os_sxs_binding')
                        or manifest in manifests and resource['kind']!='requires_private_root_context'
                        or manifest is None and resource['kind'] not in ('data_resources','no_resources')):
                    raise ValueError()
                name=Path(slot).name.casefold()
                if name in basenames:raise ValueError()
                basenames.add(name)
                dependencies=row['dependencies']
                if dependencies is not None:
                    if dependencies!=sorted(set(dependencies)):raise ValueError()
                    edges+=len(dependencies)
                    for name in dependencies:
                        if not re.fullmatch(r'[a-z0-9_][a-z0-9_.-]*\.(dll|pyd)',name) or '..' in name:raise ValueError()
        if edges>512:raise ValueError()
        loader=value['numpy_loader'];directory=runtime_path(loader['directory'])
        rows={row['slot']:row for row in value['sources']}
        if (rows[loader['source_slot']]['sha256']!=loader['source_sha256']
                or loader['members']!=sorted(r['slot'] for r in value['members']
                    if str(Path(r['slot']).parent).replace('\\','/')==directory)):
            raise ValueError()
        return value,hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, KeyError, TypeError):
        raise NativeExecutionUnsupported('execution_acquired_descriptor_changed') from None


@dataclass(frozen=True)
class AcquiredSourceDiagnostic:
    descriptor_sha256: str
    os_policy_sha256: str
    verified_facts: tuple[CPUFileFact, ...]
    pending: tuple[tuple[str,str], ...]
    recorded_dependencies: tuple[tuple[str,tuple[str,...] | None], ...]
    recorded_edges: tuple[tuple[str,str,str], ...]

    @property
    def dispatch_authorized(self): return False


def describe_acquired_sources(facts=(), *, descriptor_sha256=None,
                             source_identities=(), search_directories=()):
    """Compare finite facts only; no IO of native/source bytes or graph traversal.

    Inputs are diagnostic observations, never receipts. Recorded dependencies
    remain separate from current observed facts, including explicit unknowns.
    """
    policy,identity=parse_acquired_descriptor(owned_acquired_bytes())
    if descriptor_sha256 is not None and descriptor_sha256!=identity:
        raise NativeExecutionUnsupported('execution_acquired_identity_changed')
    if type(facts) is not tuple or len(facts)>32:
        raise NativeExecutionUnsupported('execution_acquired_facts_invalid')
    rows={r['slot']:r for r in policy['members']};seen=set()
    for fact in facts:
        if type(fact) is not CPUFileFact or type(fact.name) is not str or fact.name not in rows or fact.name in seen:
            raise NativeExecutionUnsupported('execution_acquired_member_changed')
        row=rows[fact.name];seen.add(fact.name)
        if row['dependencies'] is None:
            raise NativeExecutionUnsupported('execution_acquired_imports_not_verified')
        expected=row['resources']
        if (type(fact.size_bytes) is not int or fact.size_bytes!=row['size_bytes'] or fact.sha256!=row['sha256']
                or type(fact.imports) is not tuple or fact.imports!=tuple(row['dependencies'])
                or type(fact.resources) is not ResourceClassification
                or type(fact.resources.leaf_count) is not int
                or (fact.resources.kind,fact.resources.leaf_count,fact.resources.manifest_sha256)!=
                (expected['kind'],expected['leaf_count'],expected['manifest_sha256'])):
            raise NativeExecutionUnsupported('execution_acquired_member_changed')
    pending={('unverified',name) for name in policy['pending']}
    pending|={('missing_member_fact',slot) for slot in rows.keys()-seen}
    pending|={('recorded_imports_unknown',slot) for slot,row in rows.items() if row['dependencies'] is None}
    selected={r['slot']:r for r in policy['sources']+policy['terms']};observed=set()
    if type(source_identities) is not tuple or len(source_identities)>32:
        raise NativeExecutionUnsupported('execution_acquired_source_changed')
    for item in source_identities:
        if type(item) is not tuple or len(item)!=3:
            raise NativeExecutionUnsupported('execution_acquired_source_changed')
        slot,size,digest=item
        if (type(slot) is not str or type(size) is not int or type(digest) is not str
                or slot not in selected or slot in observed
                or (size,digest)!=(selected[slot]['size_bytes'],selected[slot]['sha256'])):
            raise NativeExecutionUnsupported('execution_acquired_source_changed')
        observed.add(slot)
    pending|={('missing_source_fact',slot) for slot in selected.keys()-observed}
    expected_directory=(policy['numpy_loader']['directory'],)
    if type(search_directories) is not tuple or search_directories not in ((),expected_directory):
        raise NativeExecutionUnsupported('execution_acquired_search_root_changed')
    pending.add(('numpy_search_root', 'not_observed' if not search_directories else 'identity_only_lifetime_and_origin_unknown'))
    os_bytes=owned_os_policy_bytes();os_policy=parse_os_policy(os_bytes)
    by_name={Path(slot).name.casefold():slot for slot in rows}
    if set(by_name)&(os_policy.component_names|os_policy.contract_names):
        raise NativeExecutionUnsupported('execution_acquired_os_shadow')
    edges=[]
    for slot,row in sorted(rows.items()):
        for dependency in row['dependencies'] or ():
            if dependency in by_name:kind='private_recorded';target=by_name[dependency]
            elif dependency in os_policy.contract_names:kind='virtual_os_recorded';target=dependency
            elif dependency in os_policy.component_names:kind='physical_os_recorded';target=dependency
            else:
                kind='unknown_recorded';target=dependency
                pending.add(('recorded_edge_unknown',slot+':'+dependency))
            edges.append((slot,target,kind))
    return AcquiredSourceDiagnostic(identity,hashlib.sha256(os_bytes).hexdigest(),facts,tuple(sorted(pending)),tuple(
        (slot,None if row['dependencies'] is None else tuple(row['dependencies']))
        for slot,row in sorted(rows.items())),tuple(edges))

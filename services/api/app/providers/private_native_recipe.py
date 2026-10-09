"""Fixed observed private sources; representation never grants runtime authority."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from app.domain.execution_runtime import runtime_path
from .cpu_native_recipe import _owned as cpu_owned, CPUFileFact
from .native_manifest import CPYTHON_COMMON_CONTROLS
from .pe_bounded import BoundedPEView, open_bounded_pe, EXPORT_DIRECTORY_LIMIT, EXPORT_LIMIT
from .pe_resources import (read_private_resource_leaves, classify_resource_view,
    ResourceClassification, export_dependencies_view)
from .runtime_primitives import NativeExecutionUnsupported, _exact_path

PROFILE_LIMIT = 64 * 1024
SOURCE_LIMIT = 4 * 1024**2
AUDIO_SLOTS = ('Lib/site-packages/_cffi_backend.cp312-win_amd64.pyd',
    'Lib/site-packages/_soundfile_data/libsndfile_x64.dll')
COMPILER_SLOTS = ('vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll', 'vcomp140.dll')
MEMBER_SLOTS = ('python.exe', 'python312.dll', *COMPILER_SLOTS, *AUDIO_SLOTS)
SOURCE_SLOTS = ('Lib/site-packages/soundfile.py','Lib/site-packages/_soundfile.py',
    'Lib/site-packages/_soundfile_data/__init__.py')
TERM_SLOTS = ('LICENSE.txt','Lib/site-packages/soundfile-0.14.0.dist-info/LICENSE',
    'Lib/site-packages/cffi-2.1.1.dist-info/licenses/LICENSE','Lib/site-packages/_soundfile_data/COPYING')


def _digest(value): return type(value) is str and re.fullmatch('[a-f0-9]{64}', value)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value: raise ValueError()
        value[key] = item
    return value


def _owned():
    path = Path(__file__).with_suffix('.json')
    try:
        _exact_path(path, directory=False)
        with path.open('rb') as source: raw = source.read(PROFILE_LIMIT + 1)
        policy = json.loads(raw, object_pairs_hook=_unique)
        if (len(raw) > PROFILE_LIMIT or set(policy) != {'recipe','version','members','sources','terms'}
                or policy['recipe'] != 'omnivoice-private-native-observed-v1'
                or type(policy['version']) is not int or policy['version'] != 1): raise ValueError()
        known = set(cpu_owned()[0]['manifests']) | {hashlib.sha256(CPYTHON_COMMON_CONTROLS).hexdigest()}
        all_slots = set()
        for category in ('members','sources','terms'):
            rows = policy[category]
            if type(rows) is not list or not rows or len(rows) > 32: raise ValueError()
            for row in rows:
                required = {'slot','size_bytes','sha256'}
                if category != 'terms':
                    required |= {'source_kind','declared_version','terms_slots'}
                if category == 'members': required |= {'graph_role','manifest_sha256'}
                if type(row) is not dict or set(row) != required: raise ValueError()
                runtime_path(row['slot'])
                folded = row['slot'].casefold()
                if folded in all_slots: raise ValueError()
                all_slots.add(folded)
                if (type(row['size_bytes']) is not int or not 0 < row['size_bytes'] <=
                        (256*1024**2 if category == 'members' else SOURCE_LIMIT)
                        or not _digest(row['sha256'])): raise ValueError()
                if category == 'terms': continue
                if (row['source_kind'] not in {'bundled_cpython','bundled_vc','observed_system_vc',
                        'observed_audio_native','generated_ffi','python_audio'}
                        or type(row['declared_version']) is not str or not 0 < len(row['declared_version']) <= 64
                        or type(row['terms_slots']) is not list): raise ValueError()
                if category == 'members' and (row['slot'] not in MEMBER_SLOTS
                        or row['graph_role'] != ('bootstrap' if row['slot'] == 'python.exe' else 'dependency')
                        or row['manifest_sha256'] is not None and row['manifest_sha256'] not in known):
                    raise ValueError()
        if {r['slot'] for r in policy['members']} != set(MEMBER_SLOTS): raise ValueError()
        if {r['slot'] for r in policy['sources']} != set(SOURCE_SLOTS): raise ValueError()
        if {r['slot'] for r in policy['terms']} != set(TERM_SLOTS): raise ValueError()
        terms = {r['slot'] for r in policy['terms']}
        for row in (*policy['members'], *policy['sources']):
            if (len(set(row['terms_slots'])) != len(row['terms_slots'])
                    or not set(row['terms_slots']) <= terms): raise ValueError()
        return policy, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, KeyError, TypeError):
        raise NativeExecutionUnsupported('execution_private_owned_recipe_invalid') from None


def _member(policy, slot):
    return next((row for row in policy['members'] if row['slot'] == slot), None)


def classify_private_resources(view, *, slot, size_bytes, sha256):
    """Whole source bytes plus finite resources. Every binding remains pending."""
    policy, _ = _owned()
    expected = _member(policy, slot)
    if (expected is None or type(view) is not BoundedPEView
            or (expected['size_bytes'], expected['sha256']) != (size_bytes, sha256)
            or (view._source.size, view._source.digest) != (size_bytes, sha256)):
        raise NativeExecutionUnsupported('execution_private_member_identity_changed')
    manifest = expected['manifest_sha256']
    if manifest == hashlib.sha256(CPYTHON_COMMON_CONTROLS).hexdigest():
        return classify_resource_view(view, role='bootstrap_exe' if slot == 'python.exe' else 'dll')
    leaves = read_private_resource_leaves(view)
    manifests = [leaf for leaf in leaves or () if leaf.ids[0] == 24]
    if manifest is None:
        if manifests: raise NativeExecutionUnsupported('execution_private_manifest_changed')
        return ResourceClassification('no_resources' if leaves is None else 'data_resources', len(leaves or ()))
    form = cpu_owned()[0]['manifests'].get(manifest)
    if (form is None or len(manifests) != 1 or manifests[0].ids != tuple(form['ids'])
            or manifests[0].codepage != form['codepage']
            or manifests[0].data != bytes.fromhex(form['hex'])):
        raise NativeExecutionUnsupported('execution_private_manifest_changed')
    return ResourceClassification('requires_private_root_context', len(leaves), manifest)


def require_private_fact(policy, fact):
    if type(fact) is not CPUFileFact:
        raise NativeExecutionUnsupported('execution_private_member_identity_changed')
    expected = _member(policy, fact.name)
    if (expected is None or type(fact) is not CPUFileFact
            or (fact.size_bytes, fact.sha256) != (expected['size_bytes'], expected['sha256'])
            or type(fact.resources) is not ResourceClassification
            or fact.resources.manifest_sha256 != expected['manifest_sha256']):
        raise NativeExecutionUnsupported('execution_private_member_identity_changed')
    expected_kind = ('requires_os_sxs_binding' if expected['manifest_sha256'] ==
        hashlib.sha256(CPYTHON_COMMON_CONTROLS).hexdigest() else 'requires_private_root_context')
    if (expected['manifest_sha256'] is not None and fact.resources.kind != expected_kind
            or expected['manifest_sha256'] is None and fact.resources.kind not in ('no_resources','data_resources')):
        raise NativeExecutionUnsupported('execution_private_manifest_changed')


def private_pending(policy):
    pending = {('compiler_set','coherent_source_compatibility_terms_required'),
        ('backend_handle_source','build_correspondence_not_verified'),
        ('audio_loader','derived_source_and_session_binding_required')}
    for row in policy['members']:
        if not row['terms_slots']: pending.add(('terms_missing', row['slot']))
        if row['manifest_sha256'] is not None:
            pending.add(('independent_context_required', row['slot']))
    return pending


@dataclass(frozen=True)
class PrivateSourceFacts:
    policy_sha256: str
    facts: tuple[CPUFileFact, ...]
    verified_sources: tuple[str, ...]
    pending: tuple[tuple[str, str], ...]

    @property
    def dispatch_authorized(self): return False


def inspect_private_runtime(root, inventory):
    """Read selected portable inventory slots; missing facts remain named gaps."""
    _exact_path(root, directory=True)
    policy, identity = _owned()
    entries = {entry.name: entry for entry in inventory.files}
    if len(entries) != len(inventory.files):
        raise NativeExecutionUnsupported('execution_private_inventory_invalid')
    pending, facts, verified = private_pending(policy), [], []
    for category in ('members','sources','terms'):
        for row in policy[category]:
            slot = row['slot']; entry = entries.get(slot)
            if entry is None:
                pending.add(('missing_' + category, slot)); continue
            role = 'interpreter' if slot == 'python.exe' else 'dependency'
            if (entry.role != role or (entry.size_bytes,entry.sha256) != (row['size_bytes'],row['sha256'])):
                raise NativeExecutionUnsupported('execution_private_member_identity_changed')
            if category == 'members':
                with open_bounded_pe(root / slot, size_bytes=row['size_bytes'], sha256=row['sha256']) as view:
                    resources = classify_private_resources(view, slot=slot,
                        size_bytes=row['size_bytes'], sha256=row['sha256'])
                    forwarders = export_dependencies_view(view, directory_limit=EXPORT_DIRECTORY_LIMIT,
                        export_limit=EXPORT_LIMIT, suffixes=('.dll','.pyd'))
                    imports = tuple(sorted(set(view.imports.dependencies) | set(forwarders)))
                    facts.append(CPUFileFact(slot,row['size_bytes'],row['sha256'],imports,resources))
            else:
                path = root / slot
                _exact_path(path, directory=False)
                with path.open('rb') as source: data = source.read(SOURCE_LIMIT + 1)
                if (len(data) != row['size_bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']):
                    raise NativeExecutionUnsupported('execution_private_source_changed')
                verified.append(slot)
    if _owned()[1] != identity:
        raise NativeExecutionUnsupported('execution_private_owned_recipe_changed')
    return PrivateSourceFacts(identity,tuple(facts),tuple(sorted(verified)),tuple(sorted(pending)))

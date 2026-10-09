from dataclasses import replace
import pytest
from app.providers import acquired_native_recipe as acquired, cpu_native_recipe as cpu
from app.providers.pe_resources import ResourceClassification
from app.providers.runtime_primitives import NativeExecutionUnsupported
from test_cpu_native_recipe import facts


def combined():
    policy,_=acquired.parse_acquired_descriptor(acquired.owned_acquired_bytes())
    supplement,_=acquired.parse_import_supplement(acquired.owned_import_supplement_bytes())
    native=[]
    for row in policy['members']:
        resource=row['resources']
        imports=tuple(row['dependencies'] if row['dependencies'] is not None else supplement['dependencies'])
        native.append(cpu.CPUFileFact(row['slot'],row['size_bytes'],row['sha256'],imports,
            ResourceClassification(resource['kind'],resource['leaf_count'],resource['manifest_sha256'])))
    return tuple(f for f in facts(cpu._owned()[0]) if f.name not in cpu.BASE)+tuple(native)


def test_one_engine_all_catalog_roots_still_diagnostic():
    result=cpu.plan_cpu_acquired_facts(combined())
    assert not result.dispatch_authorized and not result.plan.dispatch_authorized
    assert len(result.plan.nodes)==43 and len(result.plan.roots)==43
    assert any(name=='recorded_imports_unknown' for name,_ in result.plan.pending)
    assert ('dynamic_closure','not_verified') in result.plan.pending
    assert ('unverified','normal_worker_integration') in result.plan.pending
    assert result.plan.private_policy_sha256 is None
    assert result.acquired_policy_sha256!=result.import_supplement_sha256


def test_cross_directory_and_virtual_edges():
    result=cpu.plan_cpu_acquired_facts(combined()).plan
    assert ('Lib/site-packages/_soundfile_data/libsndfile_x64.dll','vcruntime140.dll','vcruntime140.dll') in result.edges
    assert ('Lib/site-packages/numpy/fft/_pocketfft_umath.cp312-win_amd64.pyd',
        'msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll',
        'Lib/site-packages/numpy.libs/msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll') in result.edges
    assert any(name=='api-ms-win-crt-private-l1-1-0.dll' and target.startswith('OS:') for _,name,target in result.edges)


@pytest.mark.parametrize('problem',['hash','resources','duplicate','unknown','supplement','type'])
def test_changed_facts_do_not_cross_profile(problem):
    rows=list(combined());index=next(i for i,r in enumerate(rows) if r.name=='vcomp140.dll')
    if problem=='hash':rows[index]=replace(rows[index],sha256='a'*64)
    if problem=='resources':rows[index]=replace(rows[index],resources=ResourceClassification('data_resources',1))
    if problem=='duplicate':rows.append(rows[index])
    if problem=='unknown':rows[index]=replace(rows[index],name='elsewhere/vcomp140.dll')
    if problem=='supplement':
        index=next(i for i,r in enumerate(rows) if 'libscipy_openblas' in r.name);rows[index]=replace(rows[index],imports=())
    if problem=='type':rows[index]=replace(rows[index],name=[])
    with pytest.raises(NativeExecutionUnsupported):cpu.plan_cpu_acquired_facts(tuple(rows))


def test_missing_is_explicit_and_old_branches_not_upgraded():
    rows=combined();selected=tuple(row for row in rows if row.name!='vcomp140.dll')
    assert ('missing_private_member','vcomp140.dll') in cpu.plan_cpu_acquired_facts(selected).plan.pending
    for planner in (cpu.plan_cpu_facts,cpu.plan_cpu_private_facts):
        with pytest.raises(NativeExecutionUnsupported):planner(rows)

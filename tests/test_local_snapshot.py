"""Read partial real geometry through production Ruby using isolated entities."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from shapely.geometry import Polygon

from dwg_semantic.live_repair import build_live_repair_plan, _mesh, seal_live_repair_plan
from test_live_cad_geometry import cad
from test_live_repair import document, live_entity, snapshot

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / 'tests/native_safety'


def run_local(tmp_path, fixture, query, *, plan=None, active_path=None, mode=None, current_cad_model=None, inject_face_failure=False, reverse_created_faces=False):
    request = {'fixture': fixture, 'query': query, 'mode': mode or ('apply' if plan else 'inspect')}
    if plan is not None:
        request['plan'] = plan
    if active_path is not None:
        request['active_path'] = active_path
    if current_cad_model is not None:
        request['current_cad_model'] = current_cad_model
    request['inject_face_failure'] = inject_face_failure
    request['reverse_created_faces'] = reverse_created_faces
    path = tmp_path / ('apply-request.json' if plan else 'inspect-request.json')
    path.write_text(json.dumps(request), encoding='utf-8')
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
           'DWG_AUDIT_REQUEST_FILE': str(path), 'DWG_AUDIT_FIXTURE_ROOT': str(HERE),
           'DWG_AUDIT_BRIDGE': os.environ.get('DWG_TEST_BRIDGE', str(ROOT/'skills/dwg-layered-skp/scripts/sketchup/dwg_live_repair_bridge.rb'))}
    result = subprocess.run([sys.executable, str(HERE/'embedded_ruby.py'), str(HERE/'ruby_local_adapter.rb')],
                            cwd=HERE, env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + '\n' + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def material(identity, entity_id, outer, *, source_handle=None, height=3000, base=0, holes=()):
    row = live_entity(identity, entity_id=entity_id, source_handle=source_handle or identity, outer=outer, height=height)
    polygon = Polygon(outer, holes)
    row.update(base_z_mm=base, footprint={'outer':outer,'holes':list(holes)},
               mesh=_mesh(polygon, base, height), volume_mm3=polygon.area*height)
    row['provenance'].update(base_z_mm=base, outer=outer, holes=list(holes))
    return row


def case(tmp_path, *, remote_count=0, references=0):
    source = tmp_path/'isolated-source.dwg'
    source.write_bytes(b'Isolated local scope fixture, never opened by AutoCAD')
    doc = document()
    doc['intake'].update(source_path=str(source), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    for row in doc['entities'] + doc['resolved_instances']:
        row['source_file'] = str(source)
    local = material('wall-1','entity:101',[[0,0],[4000,0],[4000,200],[0,200]],source_handle='W1')
    state = snapshot([local])
    state['root']['source_manifest'] = copy.deepcopy(doc['intake'])
    for i in range(remote_count):
        x = 100_000+i*6000
        state['entities'].append(material(f'remote-{i}',f'entity:{1000+i}',[[x,0],[x+4000,0],[x+4000,200],[x,200]]))
    state['references'] = [
        {'entity_id':f'entity:{20000+i}','parent_entity_id':'entity:20','group_id':f'ref-{i}',
         'provenance':{'id':f'ref-{i}','category':'reference','mode':'reference_edges'},
         'segments':[[[i*100,1000,0],[i*100+50,1000,0]]]}
        for i in range(references)]
    picked = cad()
    picked['document']['path'] = str(source)
    query = {'schema':'dwg-skp-local-query-v1','bounds_xy_mm':[0,0,2500,200],
             'source_identifiers':['NEW20'],'target_entity_ids':[],
             'include_selected':False,'wall_layer':None}
    return doc,picked,state,query


def inspect(tmp_path, state, query):
    result = run_local(tmp_path,state,query)
    assert result['accepted'],result
    return result,result['inspection']


def plan_case(tmp_path, state=None):
    doc,picked,fixture,query = case(tmp_path)
    if state is not None:
        fixture = state
    result, local = inspect(tmp_path,fixture,query)
    plan = build_live_repair_plan(doc,picked,local,action='replace_from_cad',live_cad_geometry=True,auto_match_position=True)
    plan['model']['local_read'] = copy.deepcopy(local['local_read'])
    seal_live_repair_plan(plan)
    return fixture,query,plan,result


def test_far_geometry_and_reference_details_are_never_materialized(tmp_path):
    _,_,state,query = case(tmp_path,remote_count=80,references=160)
    result,local = inspect(tmp_path,state,query)
    assert [e['entity_id'] for e in local['entities']] == ['entity:101']
    assert not local.get('references') and not local.get('protected_entities')
    for identities in result['heavy_reads'].values():
        assert set(identities) <= {'entity:101'}, result['heavy_reads']
    assert local['read_stats']['geometry_groups'] == 1
    assert local['read_stats']['reference_groups'] == 160
    assert local['read_stats']['inventory_groups'] >= 241
    assert local['local_read']['bounds_xy_mm'][2] >= 4000
    assert result['operations'] == []


def test_global_source_matches_are_included_even_after_remote_move(tmp_path):
    _,_,state,query = case(tmp_path,remote_count=1)
    state['entities'][1]['provenance']['source_handles'] = ['NEW20']
    _,local = inspect(tmp_path,state,query)
    assert {row['entity_id'] for row in local['entities']} == {'entity:101','entity:1000'}
    assert local['local_read']['bounds_xy_mm'][2] >= 104000


def test_protected_overhead_geometry_is_included_at_any_height(tmp_path):
    _,_,state,query = case(tmp_path)
    slab = material('slab-1','entity:301',[[-100,-100],[4500,-100],[4500,500],[-100,500]],height=200,base=10000)
    slab['provenance'].update(category='slab',mode='boundary_mesh')
    state['protected_entities']=[slab]
    _,local = inspect(tmp_path,state,query)
    assert [row['entity_id'] for row in local['protected_entities']] == ['entity:301']
    assert local['protected_entities'][0]['triangles_mm']


def test_actual_partial_plan_commits_one_undo_and_never_saves_model(tmp_path):
    state,query,plan,_ = plan_case(tmp_path)
    result = run_local(tmp_path,state,query,plan=plan)
    assert result['accepted'],result
    assert result['operations'] == ['start','commit']
    assert result['receipt']['verified'] is True
    assert result['receipt']['model_saved'] is False
    assert result['receipt']['new_skp_created'] is False


@pytest.mark.parametrize('drift',['move_in','add_neighbor','delete_neighbor','same_bounds','provenance','definition_guid'])
def test_apply_rechecks_current_membership_and_geometry_before_undo(tmp_path,drift):
    doc,picked,state,query = case(tmp_path,remote_count=1)
    if drift == 'delete_neighbor':
        state['entities'].append(material('neighbor','entity:902',[[3500,200],[4500,200],[4500,400],[3500,400]]))
    _,local = inspect(tmp_path,state,query)
    plan = build_live_repair_plan(doc,picked,local,action='replace_from_cad',live_cad_geometry=True,auto_match_position=True)
    plan['model']['local_read']=copy.deepcopy(local['local_read']);seal_live_repair_plan(plan)
    changed = copy.deepcopy(state)
    if drift == 'move_in':
        changed['entities'][1]['transform']={'origin_mm':[-99000,0,0]}
    elif drift == 'add_neighbor':
        changed['entities'].append(material('new','entity:903',[[1000,200],[2000,200],[2000,400],[1000,400]]))
    elif drift == 'delete_neighbor':
        changed['entities'].pop()
    elif drift == 'same_bounds':
        # Same outer bbox and definition GUID; actual local mesh differs.
        row=changed['entities'][0]
        outer=[[0,0],[4000,0],[4000,200],[2000,200],[2000,150],[1000,150],[1000,200],[0,200]]
        row['mesh']=_mesh(Polygon(outer),0,3000)
    elif drift == 'provenance':
        changed['entities'][1]['provenance']['evidence']=['edited outside scope']
    else:
        changed['entities'][1]['definition_guid']='changed-outside-scope'
    result=run_local(tmp_path,changed,query,plan=plan)
    assert not result['accepted'],result
    assert result['operations']==[],result


def test_active_source_leaf_blocks_partial_read_but_category_context_is_supported(tmp_path):
    _,_,state,query=case(tmp_path)
    blocked=run_local(tmp_path,state,query,active_path=['entity:20','entity:101'])
    assert not blocked['accepted'] and blocked['operations']==[]
    supported=run_local(tmp_path,state,query,active_path=['entity:20'])
    assert supported['accepted'],supported


def test_inverse_scaled_leaf_uses_actual_world_bounds_for_scope(tmp_path):
    _,_,state,query=case(tmp_path)
    row=state['entities'][0]
    scale=10000
    origin=[123456.789,-98765.432,0]
    row['mesh']['vertices']=[[(v[i]-origin[i])*scale for i in range(3)]for v in row['mesh']['vertices']]
    row['transform']={'origin_mm':origin,'xaxis':[1/scale,0,0],'yaxis':[0,1/scale,0],'zaxis':[0,0,1/scale]}
    _,local=inspect(tmp_path,state,query)
    assert [e['entity_id']for e in local['entities']]==['entity:101']
    assert local['local_read']['bounds_xy_mm']==pytest.approx([0,0,4000,200],abs=1e-5)
    assert local['entities'][0]['volume_mm3']==pytest.approx(4000*200*3000,rel=1e-8)


def test_rotated_category_bounds_are_transformed_once(tmp_path):
    _,_,state,query=case(tmp_path)
    state['containers'][0]['world_transform']={'origin_mm':[8000,0,0],
        'xaxis':[0,1,0],'yaxis':[-1,0,0],'zaxis':[0,0,1]}
    query['bounds_xy_mm']=[7800,0,8000,2000]
    _,local=inspect(tmp_path,state,query)
    assert [e['entity_id']for e in local['entities']]==['entity:101']
    assert local['local_read']['bounds_xy_mm']==pytest.approx([7800,0,8000,4000],abs=1e-5)


def test_whole_wall_family_closes_scope_before_neighbor_read(tmp_path):
    _,_,state,query=case(tmp_path)
    far_part=material('far-lintel','entity:905',[[8000,0],[10000,0],[10000,200],[8000,200]],base=2000,height=1000)
    family_outline={'outer':[[0,0],[10000,0],[10000,200],[0,200]],'holes':[]}
    for row in [state['entities'][0],far_part]:
        row['provenance'].update(wall_system_family_id='whole-family',wall_system_match_footprint=family_outline)
    state['entities'].append(far_part)
    state['entities'].append(material('far-neighbor','entity:906',[[9500,200],[10100,200],[10100,400],[9500,400]]))
    query['wall_layer']='A-WALL'
    _,local=inspect(tmp_path,state,query)
    assert {e['entity_id']for e in local['entities']}=={'entity:101','entity:905','entity:906'}
    assert local['local_read']['bounds_xy_mm'][2]==pytest.approx(10000)


def test_local_snapshot_reduces_response_bytes_and_geometry_reads_against_full_inspector(tmp_path):
    _,_,state,query=case(tmp_path,remote_count=80,references=160)
    partial,local=inspect(tmp_path,state,query)
    full=run_local(tmp_path,state,query,mode='full')
    assert full['accepted'],full
    full_snapshot=full['inspection']
    local_bytes=len(json.dumps(local,separators=(',',':')).encode('utf-8'))
    full_bytes=len(json.dumps(full_snapshot,separators=(',',':')).encode('utf-8'))
    assert len(local['entities'])==1 and len(full_snapshot['entities'])==81
    assert len(full_snapshot['references'])==160
    assert local_bytes<full_bytes*.1,(local_bytes,full_bytes)
    assert len(set(full['heavy_reads']['leaf_summary']))==81
    assert set(partial['heavy_reads']['leaf_summary'])=={'entity:101'}
    (tmp_path/'partial-read-metrics.json').write_text(json.dumps({
        'fixture_solids':81,'fixture_references':160,'partial_geometry_groups':1,'full_geometry_groups':81,
        'partial_response_bytes':local_bytes,'full_response_bytes':full_bytes,
        'response_reduction_percent':100*(1-local_bytes/full_bytes)},indent=2),encoding='utf-8')


@pytest.mark.parametrize('hole',[False,True])
def test_actual_protected_slab_mesh_controls_fixed_height_and_preserves_holes(tmp_path,hole):
    from dwg_semantic.live_repair import LiveRepairError
    from dwg_semantic.modeling_standard import FIXED
    doc,picked,state,query=case(tmp_path)
    state['root'].update(modeling_standard=copy.deepcopy(FIXED),fixed_standard_audit={
        'standard':copy.deepcopy(FIXED),'all_pass':True,'issues':[],'fire_body_source_entity_ids':[]})
    holes=[[[1000,-10],[1500,-10],[1500,210],[1000,210]]]if hole else []
    slab=material('roof','entity:307',[[-100,-100],[4500,-100],[4500,500],[-100,500]],base=3200,height=200,holes=holes)
    slab['provenance'].update(category='slab',mode='boundary_mesh',slab_role='overhead_slab')
    state['protected_entities']=[slab]
    _,local=inspect(tmp_path,state,query)
    if hole:
        with pytest.raises(LiveRepairError,match='板底|板孔'):
            build_live_repair_plan(doc,picked,local,action='replace_from_cad',live_cad_geometry=True,auto_match_position=True)
    else:
        plan=build_live_repair_plan(doc,picked,local,action='replace_from_cad',live_cad_geometry=True,auto_match_position=True)
        assert {row['height_mm']for row in plan['create']}=={3200}


def test_source_freshness_still_blocks_partial_apply_before_undo(tmp_path):
    state,query,plan,_=plan_case(tmp_path)
    Path(plan['source']['path']).write_bytes(b'Source changed after the isolated plan')
    result=run_local(tmp_path,state,query,plan=plan)
    assert result['accepted'] is False and result['code']=='SOURCE_CHANGED',result
    assert result['operations']==[]


def test_resealed_geometry_outside_local_coverage_cannot_hide_missing_neighbors(tmp_path):
    state,query,plan,_=plan_case(tmp_path)
    changed=copy.deepcopy(plan)
    for row in changed['create']:
        for point in row['mesh']['vertices']:
            point[0]+=50000
        for polygon in [row['footprint'],row['provenance']]:
            for point in polygon['outer']:
                point[0]+=50000
    seal_live_repair_plan(changed)
    result=run_local(tmp_path,state,query,plan=changed)
    assert not result['accepted'] and result['operations']==[],result
    assert result['code']=='LOCAL_COVERAGE_INCOMPLETE',result


@pytest.mark.parametrize('bad',['schema','empty_bounds','missing_targets','invalid_selected'])
def test_malformed_local_query_is_rejected_without_any_geometry_read(tmp_path,bad):
    _,_,state,query=case(tmp_path)
    if bad=='schema':query['schema']='unknown'
    elif bad=='empty_bounds':query['bounds_xy_mm']=[0,0,0,200]
    elif bad=='missing_targets':del query['target_entity_ids']
    else:query['include_selected']='yes'
    result=run_local(tmp_path,state,query)
    assert not result['accepted'] and result['operations']==[],result
    assert result['heavy_reads']=={},result


def test_actual_partial_compound_wall_preserves_cavities_opening_and_single_transaction(tmp_path):
    from shapely.ops import unary_union
    from dwg_semantic.local_snapshot import make_local_query
    from test_live_wall_system import fixture as compound_fixture

    doc,picked,state,scope,wall_material,opening=compound_fixture()
    source=tmp_path/'isolated-compound-source.dwg'
    source.write_bytes(b'Isolated compound source; never opened by AutoCAD')
    doc['intake'].update(source_path=str(source),source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    picked['document']['path']=str(source)
    state['root']['source_manifest']=copy.deepcopy(doc['intake'])
    for row in state['entities']:
        row['mesh']=_mesh(Polygon(row['footprint']['outer'],row['footprint']['holes']),
                          row['base_z_mm'],row['height_mm'])
    query=make_local_query(doc,picked,action='replace_from_cad',live_cad_geometry=True,wall_system_scope=scope)
    _,local=inspect(tmp_path,state,query)
    assert len(local['entities'])==3
    plan=build_live_repair_plan(doc,picked,local,action='replace_from_cad',live_cad_geometry=True,wall_system_scope=scope)
    assert plan['model']['local_read']==local['local_read']
    assert len(plan['targets'])==3 and len(plan['create'])==2
    low=unary_union([Polygon(row['footprint']['outer'],row['footprint']['holes'])
                     for row in plan['create']if row['base_z_mm']==0])
    whole=unary_union([Polygon(row['footprint']['outer'],row['footprint']['holes'])for row in plan['create']])
    assert low.symmetric_difference(wall_material.difference(opening)).area<.001
    assert whole.symmetric_difference(wall_material).area<.001
    assert len(wall_material.interiors)==2
    assert low.intersection(opening).area==0
    assert plan['source']['preserved_opening_audit'][0]['clear_top_z_mm']==2000
    result=run_local(tmp_path,state,query,plan=plan)
    assert result['accepted'],result
    assert result['operations']==['start','commit']
    receipt=result['receipt']
    assert len(receipt['removed'])==3 and len(receipt['created'])==2
    assert receipt['verified'] is True and receipt['snapshot_mode']=='local_geometry'
    assert receipt['model_saved'] is False and receipt['new_skp_created'] is False

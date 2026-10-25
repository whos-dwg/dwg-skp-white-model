"""Run whole-model inspection and transaction guards in production Ruby."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import pytest
from test_model_sync import case,native,poly
from test_local_snapshot import run_local
from dwg_semantic.live_repair import _mesh,seal_live_repair_plan
from dwg_semantic.model_sync import build_model_sync_plan
from shapely.geometry import Polygon
from dwg_semantic.cad_model import normalized_model

def fixture(tmp_path):
    c=case(tmp_path)
    for row in c['live']['entities']:
        row['mesh']=_mesh(Polygon(row['footprint']['outer']),row['base_z_mm'],row['height_mm'])
    c['live']['references']=[{'entity_id':'entity:400','parent_entity_id':'entity:20','group_id':'reference',
        'provenance':{'id':'reference','category':'reference','mode':'reference_edges'},
        'segments':[[[0,1000,0],[1000,1000,0]]]}]
    result=run_local(tmp_path,c['live'],None,mode='global')
    assert result['accepted'],result
    c['inspection']=result['inspection']
    return c

def plan_for(c,changed=True):
    rows=[poly('W1',0,2500),poly('C1',20000,600,'A-COLUMN'),poly('W3',30000,2000)]if changed else None
    return build_model_sync_plan(c['doc'],native(c,rows),c['inspection'])

def observation(plan):
    o=deepcopy(plan['source']['sync_model'])
    o['observed_at_utc']=(datetime.now(timezone.utc)+timedelta(milliseconds=1)).isoformat()
    return o

def apply(tmp_path,c,p,**kwargs):
    return run_local(tmp_path,c['live'],None,plan=p,current_cad_model=observation(p),**kwargs)

def test_global_read_proves_reference_edge_geometry_not_only_leaf_mesh(tmp_path):
    c=fixture(tmp_path);p=plan_for(c)
    c['live']['references'][0]['segments'][0][1][0]+=1
    r=apply(tmp_path,c,p)
    assert not r['accepted'] and r['code']=='MODEL_CONTEXT_CHANGED'
    assert r['operations']==[]

def test_whole_mixed_plan_uses_one_verified_native_transaction(tmp_path):
    c=fixture(tmp_path);p=plan_for(c);r=apply(tmp_path,c,p)
    assert r['accepted'],r
    assert r['operations']==['start','commit']
    assert len(r['receipt']['removed'])==2 and len(r['receipt']['created'])==2
    assert r['receipt']['verified'] and r['receipt']['snapshot_mode']=='whole_model'

def test_whole_noop_has_no_native_operation_or_undo(tmp_path):
    c=fixture(tmp_path);p=plan_for(c,False);r=apply(tmp_path,c,p)
    assert r['accepted'],r
    assert r['operations']==[] and r['receipt']['status']=='unchanged'
    assert r['receipt']['undo_operation']is None

def test_whole_failure_aborts_transaction_without_committing(tmp_path):
    c=fixture(tmp_path);p=plan_for(c);r=apply(tmp_path,c,p,inject_face_failure=True)
    assert not r['accepted'],r
    assert r['operations']==['start','abort']

def test_whole_noop_cannot_create_unused_category(tmp_path):
    c=fixture(tmp_path);p=plan_for(c,False);p['create_containers']=[{'entity_id':'new-category:curtain','name':'curtain','parent_entity_id':'entity:10'}]
    p=seal_live_repair_plan(p);r=apply(tmp_path,c,p)
    assert not r['accepted'] and r['code']=='INVALID_PLAN_CONTRACT'
    assert r['operations']==[]

@pytest.mark.parametrize('kind',['stale','changed','selected','reused','missing'])
def test_whole_bridge_independently_rejects_bad_cad_observation(tmp_path,kind):
    c=fixture(tmp_path);p=plan_for(c);o=observation(p)
    if kind=='stale':o['observed_at_utc']=(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()
    elif kind=='changed':o['geometry_signature']='f'*64
    elif kind=='selected':o['selection_count']=1
    elif kind=='reused':o=deepcopy(p['source']['sync_model'])
    else:o=None
    r=run_local(tmp_path,c['live'],None,plan=p,current_cad_model=o)
    assert not r['accepted'] and r['code']==('CAD_MODEL_STALE'if kind in ('stale','reused')else'CAD_MODEL_CHANGED'),r
    assert r['operations']==[]

def test_whole_fixed_roof_and_new_category_commit_through_production_bridge(tmp_path):
    from dwg_semantic.modeling_standard import FIXED
    c=fixture(tmp_path)
    c['raw']['entities'].append(poly('R1',-2000,42000,'S-SLAB'))
    c['doc']=normalized_model(native(c),c['doc'])
    c['live']['root'].update(modeling_standard=deepcopy(FIXED),fixed_standard_audit={
        'standard':deepcopy(FIXED),'all_pass':True,'issues':[],'fire_body_source_entity_ids':[]})
    p=Polygon([[-2000,-1000],[40000,-1000],[40000,1000],[-2000,1000]])
    roof={'entity_id':'entity:500','group_id':'roof','parent_entity_id':'entity:20','category':'slab','tag':'S-SLAB',
        'provenance':{'id':'roof','category':'slab','mode':'boundary_mesh','slab_role':'overhead_slab',
            'source_entity_ids':[c['doc']['entities'][-1]['entity_id']]},'mesh':_mesh(p,3200,200)}
    c['live']['protected_entities']=[roof]
    read=run_local(tmp_path,c['live'],None,mode='global');assert read['accepted'],read
    plan=build_model_sync_plan(c['doc'],native(c,[*c['raw']['entities'],poly('CW',30000,1000,'A-CURTAIN')]),read['inspection'])
    r=apply(tmp_path,c,plan)
    assert r['accepted'],r
    assert r['operations']==['start','commit']
    assert any(row['category']=='curtain'for row in plan['create'])

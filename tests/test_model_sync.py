"""Whole-model delta safety using isolated synthetic CAD and SketchUp evidence."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import hashlib,json
from pathlib import Path
from unittest.mock import patch
import pytest
from shapely.geometry import Polygon

from dwg_semantic.cad_model import (validate_model_snapshot,validate_current_model,normalized_model,
    require_empty_selection,model_signature)
from dwg_semantic.model_sync import build_model_sync_plan
from dwg_semantic.live_repair import LiveRepairError,validate_plan_digest
from dwg_semantic.workflow_io import WorkflowError
from test_live_repair import snapshot,live_entity

def poly(handle,x,length=4000,layer='A-WALL'):
    points=[[x,0,0],[x+length,0,0],[x+length,200,0],[x,200,0]]
    return {'type':'LWPOLYLINE','handle':handle,'source_handle':handle,'owner_block':'*Model_Space',
        'layer':layer,'original_layer':layer,'effective_layer':layer,'closed':True,
        'normal':{'x':0,'y':0,'z':1},'vertices_world':points,'length':2*(length+200),'area':length*200}

def native(case,rows=None):
    raw=deepcopy(case['raw']);raw['entities']=deepcopy(rows if rows is not None else case['raw']['entities'])
    raw['database']={'filename':str(case['source']),'insunits':4}
    return {'schema':'dwg-cad-model-snapshot-v1','success':True,'observed_at_utc':datetime.now(timezone.utc).isoformat(),
        'document':{'path':str(case['source']),'name':'source.dwg','fingerprint_guid':'isolated-native-guid'},
        'scope':'complete_database_including_definitions_and_placed_instances','selection_source':'Editor.SelectImplied',
        'selection_count':0,'truncated':False,'complete':True,'entity_count':len(raw['entities']),'resolved_instance_count':0,
        'native_extract':raw,'reader':'synthetic_read_only_fixture_not_a_live_app'}

def case(tmp_path):
    source=tmp_path/'source.dwg';source.write_bytes(b'isolated synthetic source, not a native drawing')
    intake={'source_path':str(source),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        'units':'mm','scale_to_mm':1,'dependencies':[]}
    raw={'schema':'dwg-native-extract-v3','entities':[poly('W1',0),poly('W2',10000),poly('C1',20000,600,'A-COLUMN')],
        'resolved_instances':[],'blockers':[],'unsupported':[],'layers':[],'blocks':[]}
    c={'source':source,'raw':raw};doc=normalized_model(native(c),{'intake':intake});c['doc']=doc
    rows=[]
    for n,e in enumerate(doc['entities']):
        pts=[[p['x'],p['y']]for p in e['vertices_world']];category='column'if e['handle']=='C1'else'architectural_wall'
        row=live_entity(e['handle'],entity_id='entity:'+str(100+n),source_handle=e['handle'],category=category,outer=pts)
        row['volume_mm3']=Polygon(pts).area*3000;row['tag']=e['layer'];row['parent_entity_id']='entity:30'if category=='column'else'entity:20'
        row['provenance'].update(source_entity_ids=[e['entity_id']],tag=e['layer'],source_layer=e['layer'],source_layers=[e['layer']])
        rows.append(row)
    live=snapshot(rows);live['root']['source_manifest']=deepcopy(intake)
    live['containers'].append({'entity_id':'entity:30','parent_entity_id':'entity:10','name':'column'})
    live['global_read']={'schema':'dwg-skp-global-read-v1','entity_count':100,'inventory_sha256':'a'*64}
    live['read_stats']={'geometry_scope':'whole_model','geometry_groups':3,'reference_groups':0}
    c['live']=live
    return c

def test_whole_comparison_with_no_difference_is_noop(tmp_path):
    c=case(tmp_path);before=deepcopy(c)
    plan=build_model_sync_plan(c['doc'],native(c),c['live'])
    assert plan['status']=='up_to_date' and not plan['targets'] and not plan['create']
    assert plan['difference_summary']['unchanged_count']==3
    assert validate_plan_digest(plan)==plan['plan_sha256']
    assert c==before

def test_whole_mixed_delta_only_replaces_adds_and_removes_changed_components(tmp_path):
    c=case(tmp_path);rows=[poly('W1',0,2500),poly('C1',20000,600,'A-COLUMN'),poly('W3',30000,2000)]
    plan=build_model_sync_plan(c['doc'],native(c,rows),c['live'])
    assert {t['group_id']for t in plan['targets']}=={'W1','W2'}
    assert len(plan['create'])==2 and plan['difference_summary']['unchanged_entity_ids']==['entity:102']
    assert {tuple(Polygon(r['footprint']['outer']).bounds)for r in plan['create']}=={(0,0,2500,200),(30000,0,32000,200)}
    assert plan['write_policy']['single_undo_operation'] is True

def test_empty_complete_cad_really_can_remove_source_components(tmp_path):
    c=case(tmp_path);plan=build_model_sync_plan(c['doc'],native(c,[]),c['live'])
    assert len(plan['targets'])==3 and plan['create']==[]

def test_current_model_geometry_drift_is_compared_to_bound_source_recipe(tmp_path):
    c=case(tmp_path);r=c['live']['entities'][0]
    r['footprint']=deepcopy(r['footprint'])
    r['footprint']['outer'][1][0]=3500;r['footprint']['outer'][2][0]=3500;r['volume_mm3']=3500*200*3000
    plan=build_model_sync_plan(c['doc'],native(c),c['live'])
    assert [t['group_id']for t in plan['targets']]==['W1']
    assert Polygon(plan['create'][0]['footprint']['outer']).bounds[2]==4000

@pytest.mark.parametrize('field,value',[('complete',False),('truncated',True),('entity_count',0),('selection_count',1),('scope','selection_only')])
def test_incomplete_or_selected_whole_read_never_authorizes_deletion(tmp_path,field,value):
    c=case(tmp_path);read=native(c);read[field]=value
    with pytest.raises(WorkflowError):build_model_sync_plan(c['doc'],read,c['live'])

def test_whole_apply_requires_later_fresh_identical_complete_read(tmp_path):
    c=case(tmp_path);first=native(c);plan=build_model_sync_plan(c['doc'],first,c['live'])
    with pytest.raises(WorkflowError,match='重新'):validate_current_model(plan,None)
    with pytest.raises(WorkflowError,match='重用'):validate_current_model(plan,first)
    later=deepcopy(first);later['observed_at_utc']=(datetime.now(timezone.utc)+timedelta(milliseconds=1)).isoformat()
    validate_current_model(plan,later)
    later['native_extract']['entities'][0]['layer']='A-OTHER'
    with pytest.raises(WorkflowError,match='已改變'):validate_current_model(plan,later)

def test_whole_source_file_drift_keeps_immutable_freshness_guard(tmp_path):
    c=case(tmp_path);c['source'].write_bytes(b'changed source')
    with pytest.raises(WorkflowError,match='版本'):build_model_sync_plan(c['doc'],native(c),c['live'])

def test_missing_selection_does_not_mean_empty_selection(tmp_path):
    c=case(tmp_path)
    with pytest.raises(Exception):require_empty_selection(None,c['doc'])
    empty={'success':True,'status':'EMPTY_SELECTION','observed_at_utc':datetime.now(timezone.utc).isoformat(),
        'document':{'path':str(c['source'])},'selection_source':'PickfirstSelectionSet','selection_count':0,'returned_count':0,'objects':[],'truncated':False}
    require_empty_selection(empty,c['doc'])
    empty['truncated']=True
    with pytest.raises(WorkflowError):require_empty_selection(empty,c['doc'])

def test_new_geometry_collision_aborts_entire_difference_plan(tmp_path):
    c=case(tmp_path);read=native(c,[*c['raw']['entities'],poly('W3',0,2500)])
    with pytest.raises(LiveRepairError,match='碰撞'):build_model_sync_plan(c['doc'],read,c['live'])

def applied_snapshot(c,plan):
    s=deepcopy(c['live']);removed={r['entity_id']for r in plan['targets']}
    s['entities']=[r for r in s['entities']if r['entity_id']not in removed]
    first=max([int(r['entity_id'].split(':')[-1])for r in c['live']['entities']]+[1000])+100
    for n,row in enumerate(plan['create']):
        s['entities'].append({'entity_id':f'entity:{first+n}','parent_entity_id':row['parent_entity_id'],
            'group_id':row['group_id'],'name':row['name'],'category':row['category'],'tag':row['tag'],
            'material':row['material'],'footprint':deepcopy(row['footprint']),'base_z_mm':row['base_z_mm'],
            'height_mm':row['height_mm'],'volume_mm3':row['mesh']['expected_volume_mm3'],'manifold':True,
            'locked':False,'signature':'new-'+row['group_id'],'provenance':deepcopy(row['provenance'])})
    return s

def test_repeat_comparison_after_mixed_update_is_idempotent(tmp_path):
    c=case(tmp_path);read=native(c,[poly('W1',0,2500),poly('C1',20000,600,'A-COLUMN'),poly('W3',30000,2000)])
    first=build_model_sync_plan(c['doc'],read,c['live']);after=applied_snapshot(c,first)
    second=build_model_sync_plan(c['doc'],native(c,read['native_extract']['entities']),after)
    assert second['status']=='up_to_date' and not second['targets'] and not second['create']

def test_new_inner_loop_is_a_cavity_not_a_duplicate_wall(tmp_path):
    c=case(tmp_path);inner=poly('HOLE',500,100)
    inner['vertices_world']=[[500,50,0],[600,50,0],[600,150,0],[500,150,0]]
    read=native(c,[*c['raw']['entities'],inner]);plan=build_model_sync_plan(c['doc'],read,c['live'])
    assert [t['group_id']for t in plan['targets']]==['W1']
    assert len(plan['create'])==1 and len(plan['create'][0]['footprint']['holes'])==1
    assert plan['create'][0]['mesh']['expected_volume_mm3']==(4000*200-100*100)*3000
    assert build_model_sync_plan(c['doc'],native(c,read['native_extract']['entities']),applied_snapshot(c,plan))['status']=='up_to_date'

def test_approved_door_clearance_survives_whole_wall_length_change(tmp_path):
    c=case(tmp_path);host=c['live']['entities'][0];parts=[]
    for n,(x0,x1,z,h,role)in enumerate([(0,500,0,3000,'side'),(1600,4000,0,3000,'side'),(500,1600,2100,900,'lintel')]):
        row=deepcopy(host);row.update(entity_id=f'entity:{200+n}',group_id=f'wall/{role}/{n}',base_z_mm=z,height_mm=h)
        row['footprint']={'outer':[[x0,0],[x1,0],[x1,200],[x0,200]],'holes':[]};row['volume_mm3']=(x1-x0)*200*h
        row['provenance'].update(id=row['group_id'],outer=deepcopy(row['footprint']['outer']),base_z_mm=z,height_mm=h,
            opening_ids=['door-clearance'],opening_part_role=role)
        parts.append(row)
    c['live']['entities']=parts+c['live']['entities'][1:]
    read=native(c,[poly('W1',0,5000),*c['raw']['entities'][1:]])
    plan=build_model_sync_plan(c['doc'],read,c['live'])
    after=applied_snapshot(c,plan);family=[r for r in after['entities']if r['provenance']['source_handles']==['W1']]
    assert sum(r['volume_mm3']for r in family)==5000*200*3000-1100*200*2100
    assert any(r['base_z_mm']==2100 and r['height_mm']==900 for r in family)

def test_layer_change_updates_tag_without_guessing_nearest_target(tmp_path):
    c=case(tmp_path);rows=deepcopy(c['raw']['entities']);rows[0].update(layer='A-WALL-NEW',original_layer='A-WALL-NEW',effective_layer='A-WALL-NEW')
    plan=build_model_sync_plan(c['doc'],native(c,rows),c['live'])
    assert [r['group_id']for r in plan['targets']]==['W1']
    assert plan['create'][0]['tag']=='A-WALL-NEW'

def test_new_unbound_opening_does_not_get_ignored_or_fill_existing_wall(tmp_path):
    c=case(tmp_path)
    with pytest.raises(LiveRepairError,match='淨空'):
        build_model_sync_plan(c['doc'],native(c,[*c['raw']['entities'],poly('D1',500,1000,'A-DOOR')]),c['live'])

def test_known_opening_cutter_updates_width_and_preserves_confirmed_height(tmp_path):
    c=case(tmp_path);host=c['live']['entities'][0];parts=[]
    c['raw']['entities'].append(poly('D1',500,1100,'A-DOOR'))
    c['doc']=normalized_model(native(c),c['doc'])
    key=c['doc']['entities'][-1]['entity_id']
    for n,(x0,x1,z,h,role)in enumerate([(0,500,0,3000,'side'),(1600,4000,0,3000,'side'),(500,1600,2100,900,'lintel')]):
        row=deepcopy(host);row.update(entity_id=f'entity:{200+n}',group_id=f'opening/{role}/{n}',base_z_mm=z,height_mm=h)
        row['footprint']={'outer':[[x0,0],[x1,0],[x1,200],[x0,200]],'holes':[]};row['volume_mm3']=(x1-x0)*200*h
        row['provenance'].update(id=row['group_id'],outer=deepcopy(row['footprint']['outer']),base_z_mm=z,height_mm=h,
            opening_ids=['known-door'],opening_part_role=role,selection_opening_source_entity_ids=[key])
        parts.append(row)
    c['live']['entities']=parts+c['live']['entities'][1:]
    rows=[*c['raw']['entities'][:-1],poly('D1',600,900,'A-DOOR')]
    p=build_model_sync_plan(c['doc'],native(c,rows),c['live']);after=applied_snapshot(c,p)
    assert sum(r['volume_mm3']for r in after['entities']if r['category']=='architectural_wall')==4000*200*3000-900*200*2100+4000*200*3000
    assert build_model_sync_plan(c['doc'],native(c,rows),after)['status']=='up_to_date'

def test_changed_ambiguous_door_jamb_is_blocked_instead_of_using_old_opening(tmp_path):
    c=case(tmp_path);host=c['live']['entities'][0]
    jamb={'type':'LINE','handle':'J1','source_handle':'J1','owner_block':'*Model_Space','layer':'A-WALL',
        'original_layer':'A-WALL','effective_layer':'A-WALL','start_world':[500,0,0],'end_world':[500,200,0]}
    c['raw']['entities'].append(jamb);c['doc']=normalized_model(native(c),c['doc'])
    host['provenance'].update(opening_ids=['door'],source_entity_ids=[c['doc']['entities'][0]['entity_id'],c['doc']['entities'][-1]['entity_id']])
    rows=deepcopy(c['raw']['entities']);rows[-1]['start_world']=[600,0,0]
    with pytest.raises(LiveRepairError,match='側界'):build_model_sync_plan(c['doc'],native(c,rows),c['live'])

def test_fixed_whole_new_category_fits_actual_roof_and_preserves_source_bound_slab(tmp_path):
    from test_fixed_live_standard import roof
    from dwg_semantic.modeling_standard import FIXED
    c=case(tmp_path);c['raw']['entities'].append(poly('R1',-2000,40000,'S-SLAB'))
    c['doc']=normalized_model(native(c),c['doc'])
    c['live']['root'].update(modeling_standard=deepcopy(FIXED),fixed_standard_audit={'standard':deepcopy(FIXED),'all_pass':True,'issues':[],'fire_body_source_entity_ids':[]})
    slab=roof(cap=3200,x0=-2000,x1=40000);slab['provenance']['source_entity_ids']=[c['doc']['entities'][-1]['entity_id']]
    c['live']['protected_entities']=[slab]
    p=build_model_sync_plan(c['doc'],native(c,[*c['raw']['entities'],poly('CW',30000,1000,'A-CURTAIN')]),c['live'])
    curtain=[r for r in p['create']if r['category']=='curtain']
    assert len(curtain)==1 and curtain[0]['height_mm']==3200
    assert p['create_containers']==[{'entity_id':'new-category:curtain','name':'curtain','parent_entity_id':c['live']['root']['entity_id']}]
    assert all(t['entity_id']!=slab['entity_id']for t in p['targets'])
    changed=deepcopy(c['raw']['entities']);changed[-1].update(layer='S-SLAB-CHANGED',original_layer='S-SLAB-CHANGED',effective_layer='S-SLAB-CHANGED')
    with pytest.raises(LiveRepairError,match='受保護'):build_model_sync_plan(c['doc'],native(c,changed),c['live'])

def test_unsaved_new_component_can_be_deleted_on_later_whole_sync(tmp_path):
    c=case(tmp_path);rows=[*c['raw']['entities'],poly('NEW',30000,2000)]
    added=build_model_sync_plan(c['doc'],native(c,rows),c['live'])
    c['live']=applied_snapshot(c,added)
    deleted=build_model_sync_plan(c['doc'],native(c),c['live'])
    assert len(deleted['targets'])==1 and deleted['create']==[]
    assert deleted['targets'][0]['group_id']==added['create'][0]['group_id']
    assert deleted['difference_summary']['unchanged_count']==3

def test_successive_partial_family_changes_keep_unchanged_groups_and_latest_baseline(tmp_path):
    c=case(tmp_path);host=c['live']['entities'][0];parts=[]
    for n,(x0,x1,z,h,role)in enumerate([(0,500,0,3000,'side'),(1600,4000,0,3000,'side'),(500,1600,2100,900,'lintel')]):
        r=deepcopy(host);r.update(entity_id=f'entity:{200+n}',group_id=f'family/{n}',base_z_mm=z,height_mm=h)
        r['footprint']={'outer':[[x0,0],[x1,0],[x1,200],[x0,200]],'holes':[]};r['volume_mm3']=(x1-x0)*200*h
        r['provenance'].update(id=r['group_id'],outer=deepcopy(r['footprint']['outer']),base_z_mm=z,height_mm=h,opening_ids=['door'],opening_part_role=role)
        parts.append(r)
    c['live']['entities']=parts+c['live']['entities'][1:]
    for x,length in [(0,5000),(-500,5500),(0,6000)]:
        rows=[poly('W1',x,length),*c['raw']['entities'][1:]]
        p=build_model_sync_plan(c['doc'],native(c,rows),c['live'])
        assert p['difference_summary']['unchanged_count']>=3
        c['live']=applied_snapshot(c,p)
        assert build_model_sync_plan(c['doc'],native(c,rows),c['live'])['status']=='up_to_date'

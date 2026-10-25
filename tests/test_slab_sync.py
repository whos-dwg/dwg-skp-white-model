"""Isolated source-bound slab delta tests; no live applications."""
from copy import deepcopy
import pytest
from shapely.geometry import Polygon
from test_model_sync import case,native,poly,applied_snapshot
from dwg_semantic.cad_model import normalized_model
from dwg_semantic.slab_modeling import prepare_slabs
from dwg_semantic.slab_sync import _row,triangles,validate_slab_rows
from dwg_semantic.model_sync import build_model_sync_plan
from dwg_semantic.live_repair import LiveRepairError

def rectangle(h,x,y,w,d,layer='S-SLAB',z=0):
    e=poly(h,x,w,layer);e['vertices_world']=[[x,y,z],[x+w,y,z],[x+w,y+d,z],[x,y+d,z]]
    e.update(length=2*(w+d),area=w*d)
    return e

def slab_case(tmp_path,*,stepped=False,hole=True,roof=False):
    c=case(tmp_path);extra=[rectangle('F1',-1000,-1000,30000 if roof else 7000,5000)]
    if hole:extra.append(rectangle('H1',4000,1000,600,800,'S-OPENING'))
    if stepped:extra.append(rectangle('Z1',-1000,-1000,3000,5000,'S-DROP'))
    c['raw']['entities']+=extra;c['doc']=normalized_model(native(c),c['doc'])
    by={e['handle']:e for e in c['doc']['entities']}
    def fp(h):return {'outer':[[p['x'],p['y']]for p in by[h]['vertices_world']],'holes':[]}
    s={'id':'isolated-floor','role':'overhead'if roof else'floor','extent':fp('F1'),'source_entity_ids':[by['F1']['entity_id']],
        'annotation_source_ids':[],'default_top_z_mm':3400 if roof else 0,'default_thickness_mm':200,
        'zones':[],'openings':[],'tag_binding':{'kind':'source','source_layer':'S-SLAB','tag':'S-SLAB'},
        'evidence':'Isolated synthetic slab fixture, not a project approval'}
    if hole:s['openings']=[{'id':'shaft','source_entity_ids':[by['H1']['entity_id']],'footprint':fp('H1'),'applies_to_system_id':s['id']}]
    if stepped:s['zones']=[{'id':'drop','source_entity_ids':[by['Z1']['entity_id']],'footprint':fp('Z1'),
        'top_z_mm':-100,'thickness_mm':200,'method':'constant_thickness'}]
    config={'enabled':True,'confirmed_scope':True,'assembly_mode':'whole_uniform_thickness','floor_datum_m':0,'systems':[s]}
    built=prepare_slabs(c['doc'],config);assert not built['blockers'],built['blockers']
    group=built['groups'][0];r=_row(group,None,'entity:40')
    protected={**r,'entity_id':'entity:500','signature':'isolated-slab-signature','manifold':True,'locked':False,'nested_geometry':False,
        'triangles_mm':triangles(r),'volume_mm3':group['volume_mm3']}
    c['live']['protected_entities']=[protected];c['live']['containers'].append({'entity_id':'entity:40','name':'slab','parent_entity_id':c['live']['root']['entity_id']})
    c['slab_config']=config;c['slab_id']=group['id'];return c

def apply_slab(c,p):
    after=applied_snapshot(c,{**p,'targets':[t for t in p['targets']if t['entity_id']!='entity:500'],
        'create':[r for r in p['create']if r['category']!='slab']})
    removed={r['entity_id']for r in p['targets']}
    after['protected_entities']=[r for r in c['live']['protected_entities']if r['entity_id']not in removed]
    for n,r in enumerate(p['create']):
        if r['category']=='slab':after['protected_entities'].append({**deepcopy(r),'entity_id':f'entity:{700+n}','signature':'new-slab',
            'manifold':True,'locked':False,'nested_geometry':False,'triangles_mm':triangles(r),'volume_mm3':r['mesh']['expected_volume_mm3']})
    return after

@pytest.mark.parametrize('stepped',[False,True])
def test_unchanged_slab_keeps_actual_group_and_is_noop(tmp_path,stepped):
    c=slab_case(tmp_path,stepped=stepped);p=build_model_sync_plan(c['doc'],native(c),c['live'])
    assert p['status']=='up_to_date'and not p['targets']and not p['create']

def test_whole_slab_outer_and_hole_change_only_replaces_that_slab(tmp_path):
    c=slab_case(tmp_path,stepped=True);rows=deepcopy(c['raw']['entities'])
    rows[-3]=rectangle('F1',-1000,-1000,8000,5000)
    rows[-2]=rectangle('H1',4500,1200,800,1000,'S-OPENING')
    p=build_model_sync_plan(c['doc'],native(c,rows),c['live']);validate_slab_rows(p)
    assert len(p['targets'])==len(p['create'])==1 and p['targets'][0]['entity_id']=='entity:500'
    assert p['create'][0]['provenance']['assembly_mode']=='whole_uniform_thickness'
    after=apply_slab(c,p)
    assert build_model_sync_plan(c['doc'],native(c,rows),after)['status']=='up_to_date'

def test_delete_slab_source_removes_only_slab(tmp_path):
    c=slab_case(tmp_path);rows=[r for r in c['raw']['entities']if r['handle']not in ('F1','H1')]
    p=build_model_sync_plan(c['doc'],native(c,rows),c['live'])
    assert len(p['targets'])==1 and p['create']==[]

def test_new_same_layer_flat_slab_and_later_delete(tmp_path):
    c=slab_case(tmp_path,hole=False);rows=[*c['raw']['entities'],rectangle('NEW',30000,0,2000,2000)]
    p=build_model_sync_plan(c['doc'],native(c,rows),c['live']);validate_slab_rows(p)
    assert len(p['create'])==1 and p['targets']==[] and p['create'][0]['category']=='slab'
    after=apply_slab(c,p);c['live']=after
    q=build_model_sync_plan(c['doc'],native(c),after)
    assert len(q['targets'])==1 and q['create']==[]

def test_unproven_new_inner_ring_cannot_fill_or_guess_slab_opening(tmp_path):
    c=slab_case(tmp_path,hole=False)
    with pytest.raises(LiveRepairError,match='孔洞'):
        build_model_sync_plan(c['doc'],native(c,[*c['raw']['entities'],rectangle('NEW',2000,1000,500,500)]),c['live'])

def test_changed_recipe_mesh_cannot_pass_equal_volume(tmp_path):
    c=slab_case(tmp_path);rows=deepcopy(c['raw']['entities']);rows[-2]=rectangle('F1',-1000,-1000,8000,5000)
    p=build_model_sync_plan(c['doc'],native(c,rows),c['live'])
    for v in p['create'][0]['mesh']['vertices']:v[0]+=10
    with pytest.raises(LiveRepairError):validate_slab_rows(p)

def test_roof_elevation_updates_wall_and_column_to_new_actual_soffit(tmp_path):
    from dwg_semantic.modeling_standard import FIXED
    c=slab_case(tmp_path,roof=True,hole=False)
    c['live']['root'].update(modeling_standard=deepcopy(FIXED),fixed_standard_audit={'standard':deepcopy(FIXED),'all_pass':True,'issues':[],'fire_body_source_entity_ids':[]})
    for r in c['live']['entities']:
        r['height_mm']=3200;r['volume_mm3']=Polygon(r['footprint']['outer']).area*3200;r['provenance']['height_mm']=3200
    rows=deepcopy(c['raw']['entities']);rows[-1]=rectangle('F1',-1000,-1000,30000,5000,z=100)
    p=build_model_sync_plan(c['doc'],native(c,rows),c['live']);validate_slab_rows(p)
    assert len(p['targets'])==len(p['create'])==4
    assert {r['height_mm']for r in p['create']if r['category']!='slab'}=={3300}
    assert any(r['category']=='column'for r in p['create'])
    after=apply_slab(c,p)
    assert build_model_sync_plan(c['doc'],native(c,rows),after)['status']=='up_to_date'

def test_floor_positive_material_collision_is_rejected(tmp_path):
    c=slab_case(tmp_path,hole=False);rows=deepcopy(c['raw']['entities']);rows[-1]=rectangle('F1',-1000,-1000,7000,5000,z=1000)
    with pytest.raises(LiveRepairError,match='碰撞'):build_model_sync_plan(c['doc'],native(c,rows),c['live'])

def test_changed_drop_zone_preserves_two_hundred_mm_nominal_thickness(tmp_path):
    c=slab_case(tmp_path,stepped=True);rows=deepcopy(c['raw']['entities']);rows[-1]=rectangle('Z1',-1000,-1000,3500,5000,'S-DROP',z=-50)
    p=build_model_sync_plan(c['doc'],native(c,rows),c['live']);validate_slab_rows(p)
    g=p['create'][0]['provenance']
    zone=g['model_sync_slab_state']['config']['systems'][0]['zones'][0]
    assert zone['top_z_mm']==-150 and zone['thickness_mm']==200
    assert min(p[2]for t in triangles(p['create'][0])for p in t)==-350
    assert len(p['targets'])==len(p['create'])==1

@pytest.mark.parametrize('field',['locked','nested_geometry'])
def test_changed_locked_or_nested_slab_is_not_replaced(tmp_path,field):
    c=slab_case(tmp_path,hole=False);c['live']['protected_entities'][0][field]=True
    rows=deepcopy(c['raw']['entities']);rows[-1]=rectangle('F1',-1000,-1000,8000,5000)
    with pytest.raises(LiveRepairError,match='鎖定'):build_model_sync_plan(c['doc'],native(c,rows),c['live'])

@pytest.mark.parametrize('layer',['S-SLAB','S-NEW-ROOF'])
def test_new_slab_without_unique_flat_level_template_blocks(tmp_path,layer):
    c=slab_case(tmp_path,stepped=True)
    with pytest.raises(LiveRepairError,match='角色'):
        build_model_sync_plan(c['doc'],native(c,[*c['raw']['entities'],rectangle('NEW',30000,0,2000,2000,layer)]),c['live'])

def test_equal_volume_actual_slab_drift_is_repaired_from_source(tmp_path):
    c=slab_case(tmp_path,stepped=True)
    for tri in c['live']['protected_entities'][0]['triangles_mm']:
        for p in tri:p[0]+=10
    p=build_model_sync_plan(c['doc'],native(c),c['live']);validate_slab_rows(p)
    assert len(p['targets'])==len(p['create'])==1

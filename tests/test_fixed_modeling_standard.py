from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest
from shapely.geometry import Point

from dwg_semantic.modeling import write_model
from dwg_semantic.modeling_standard import (FIXED, apply_standard_decisions, enforce_standard_payload, seed_standard)
from dwg_semantic.pipeline import _draft_payload
from dwg_semantic.plan_geometry import SourceIndex, shape_of
from dwg_semantic.plan_recognition import DEFAULTS
from dwg_semantic.slab_recognition import recognize_slab_openings
from test_modeling import rectangle, decisions as wall_decisions
from test_slab_modeling import document, decisions as slab_decisions, system, footprint


def fixture():
    doc=document()
    wall=rectangle('wall',width=3000,depth=200)
    doc['entities'].append(wall)
    config=wall_decisions()
    for entity in doc['entities']:
        if entity is not wall:
            config['layer_mapping'][entity['layer']]={'category':'reference','reason':'Source slab boundary/annotation fixture'}
    config['layer_mapping']['Walls']['height_mm']=None
    config['slabs']=slab_decisions(system(role='overhead',default_top_z_mm=3400))
    seed_standard(config)
    assert apply_standard_decisions(config,{})==[]
    return doc,config


def test_new_standard_seeds_all_four_fixed_rules_and_blocks_case_mode_fallback():
    config=seed_standard({})
    assert config['modeling_standard']==FIXED
    config['slabs']['systems']=[system(assembly_mode='prisms')]
    assert any(row['code']=='fixed_standard_requires_whole_uniform_slab'
               for row in apply_standard_decisions(config,{}))
    config['modeling_standard']['stair_opening_scope']='treads_only'
    assert any(row['code']=='fixed_modeling_standard_changed'for row in apply_standard_decisions(config,{}))


def test_roof_determines_wall_height_without_a_case_height_default():
    doc,config=fixture()
    payload=_draft_payload(doc,{},config)
    payload=enforce_standard_payload(doc,payload,config,{})
    assert payload['blockers']==[],payload['blockers']
    walls=[row for row in payload['groups']if row['category']=='architectural_wall']
    assert walls and all(row['base_z_mm']+row['height_mm']==3200 for row in walls)
    slabs=[row for row in payload['groups']if row['category']=='slab']
    assert len(slabs)==1 and slabs[0]['assembly_mode']=='whole_uniform_thickness'
    assert payload['fixed_standard_audit']['all_pass']


def test_a_missing_roof_blocks_instead_of_retaining_an_arbitrary_wall_top():
    doc,config=fixture()
    config['slabs']['enabled']=False
    config['layer_mapping']['Walls']['height_mm']=3200
    payload=enforce_standard_payload(doc,_draft_payload(doc,{},config),config,{})
    assert any(row.get('code')=='fixed_standard_overhead_underside_required'for row in payload['blockers'])


def test_partial_roof_coverage_needs_source_projection_not_a_global_height_guess():
    doc,config=fixture()
    config['slabs']['systems'][0]['extent']=footprint(0,0,1000,8000)
    payload=enforce_standard_payload(doc,_draft_payload(doc,{},config),config,{})
    assert any(row.get('code')=='fixed_standard_underside_coverage_required'for row in payload['blockers'])


def test_full_stair_enclosure_includes_platform_not_the_smaller_step_locator():
    outer=footprint(0,0,5000,4000,holes=[[[200,200],[4800,200],[4800,3800],[200,3800]]])
    walls=[{'id':'wall-room','source_entity_ids':['wall-room'],'footprint':outer}]
    lines=[]
    for index in range(6):
        lines.append({'entity_id':f'tread-{index}','handle':f'T{index}','type':'LINE','layer':'A-STAIR',
            'owner_block':'*Model_Space','start':{'x':500,'y':500+index*150,'z':0},
            'end':{'x':2000,'y':500+index*150,'z':0}})
    outline={'entity_id':'wall-room','handle':'W','type':'LWPOLYLINE','layer':'A-WALL','closed':True,
        'owner_block':'*Model_Space','vertices':[{'x':x,'y':y,'z':0}for x,y in outer['outer']]}
    result=recognize_slab_openings(SourceIndex({'entities':lines+[outline]}),
        {row['entity_id']for row in lines+[outline]},DEFAULTS,wall_context=walls)
    assert result['stair_boundary_review_requests']==[]
    stair=result['staircase_openings'][0]
    assert stair['includes_landings'] and shape_of(stair).covers(Point(4000,3000))
    assert shape_of(stair).area==4600*3600
    assert 'bbox'not in stair['boundary_basis'].split('_not_')[0]
    config=seed_standard({'slabs':{'enabled':True,'systems':[system()]}})
    config['slabs']['stair_opening_bindings']={stair['id']:{'applies_to_system_ids':['floor'],
        'reason':'Fixture source confirms this whole stair enclosure applies to the floor slab'}}
    assert apply_standard_decisions(config,{'automatic_plan':{'slab_openings':result}})==[]
    assert shape_of(config['slabs']['systems'][0]['openings'][0]['footprint']).equals(shape_of(stair))


def test_explicit_stair_symbol_membership_allowance_never_moves_actual_wall_cut():
    outer=footprint(0,0,5000,4000,holes=[[[200,200],[4800,200],[4800,3800],[200,3800]]])
    walls=[{'id':'wall-room','source_entity_ids':['wall-room'],'footprint':outer,'source_plane_z_mm':0}]
    lines=[{'entity_id':f'tread-{n}','handle':f'T{n}','type':'LINE','layer':'A-STAIR',
            'owner_block':'*Model_Space','start':{'x':199.7,'y':500+n*150,'z':0},
            'end':{'x':2000,'y':500+n*150,'z':0}}for n in range(6)]
    lines.append({'entity_id':'symbol-side','handle':'S','type':'LINE','layer':'A-STAIR',
                  'owner_block':'*Model_Space','start':{'x':199.7,'y':500,'z':0},
                  'end':{'x':199.7,'y':1250,'z':0}})
    index=SourceIndex({'entities':lines})
    ids={row['entity_id']for row in lines}
    assert recognize_slab_openings(index,ids,DEFAULTS,wall_context=walls)['stair_boundary_review_requests']
    result=recognize_slab_openings(index,ids,{**DEFAULTS,'stair_mark_boundary_tolerance_mm':.5},wall_context=walls)
    assert not result['stair_boundary_review_requests']
    stair=result['staircase_openings'][0]
    assert shape_of(stair).bounds==(200.,200.,4800.,3800.)
    assert abs(stair['maximum_mark_vertex_outside_cell_mm']-.3)<1e-6
    # A missing physical room side must still block even with the allowance.
    open_walls=[{'id':'open-room','footprint':footprint(0,0,200,4000)}]
    assert recognize_slab_openings(index,ids,{**DEFAULTS,'stair_mark_boundary_tolerance_mm':.5},wall_context=open_walls)['stair_boundary_review_requests']


def test_unresolved_stair_enclosure_blocks_slabs_without_asking_platform_scope():
    config=seed_standard({'slabs':{'enabled':True,'systems':[system()]}})
    issues=apply_standard_decisions(config,{'automatic_plan':{'slab_openings':{
        'stair_boundary_review_requests':[{'source_entity_ids':['stair-marks']}]
    }}})
    assert issues[0]['code']=='whole_stair_enclosure_boundary_unresolved'
    assert issues[0]['required_scope']=='entire_enclosure_including_landings'


def test_full_slab_keeps_source_outer_and_exact_hole_and_refuses_tampering():
    doc,config=fixture()
    config['slabs']['systems'][0]['openings']=[{'id':'shaft','source_entity_ids':['opening'],
        'footprint':footprint(1000,2000,500,900),'applies_to_system_id':'floor'}]
    payload=enforce_standard_payload(doc,_draft_payload(doc,{},config),config,{})
    assert payload['blockers']==[],payload['blockers']
    slab=next(row for row in payload['groups']if row['category']=='slab')
    assert not shape_of(slab).covers(Point(1250,2400))
    tampered=copy.deepcopy(payload)
    next(row for row in tampered['groups']if row['category']=='slab')['outer'][0][0]+=50
    result=enforce_standard_payload(doc,tampered,config,{})
    assert any(row.get('code')=='fixed_standard_outer_or_opening_boundary_changed'for row in result['blockers'])


def test_fire_door_body_is_omitted_but_its_plan_and_opening_audit_are_kept():
    doc,config=fixture()
    body=rectangle('fire-body',layer='DOOR_FIRE',x=6000,width=900,depth=50)
    doc['entities'].append(body)
    payload=_draft_payload(doc,{},config)
    payload['groups'].append({'id':'unwanted-body','category':'curtain_accessory','source_entity_ids':['fire-body'],
        'outer':[[6000,0],[6900,0],[6900,50],[6000,50]],'holes':[],'base_z_mm':0,'height_mm':2100})
    payload['opening_audit']=[{'id':'fire-opening','host_ids':[],'clearance_footprint':footprint(6000,0,900,200)}]
    result=enforce_standard_payload(doc,payload,config,{})
    assert 'unwanted-body'not in {row['id']for row in result['groups']}
    assert result['opening_audit'][0]['id']=='fire-opening'
    assert result['fixed_standard_audit']['removed_fire_body_ids']==['unwanted-body']
    assert any(row['source_entity_ids']==['fire-body']for row in result['references'])


def test_native_boundary_rechecks_actual_top_even_with_a_self_declared_passing_audit(tmp_path):
    from dwg_semantic.fixed_standard_validation import validate_fixed_payload
    doc,config=fixture()
    payload=enforce_standard_payload(doc,_draft_payload(doc,{},config),config,{})
    assert validate_fixed_payload(payload)==[]
    wall=next(row for row in payload['groups']if row['category']=='architectural_wall')
    wall['height_mm']-=500
    assert any(row['code']=='fixed_standard_actual_top_not_at_local_underside'
               for row in validate_fixed_payload(payload))
    with pytest.raises(ValueError,match='Fixed modeling standard'):
        write_model(payload,tmp_path/'must-not-exist.skp')
    assert not (tmp_path/'must-not-exist.skp').exists()


@pytest.mark.skipif(os.environ.get('DWG_RUN_NATIVE_TESTS')!='1',reason='Opt-in actual SketchUp C API')
def test_fixed_standard_actual_skp_reopen_has_whole_slab_and_wall_at_underside(tmp_path):
    doc,config=fixture()
    payload=enforce_standard_payload(doc,_draft_payload(doc,{},config),config,{})
    assert payload['blockers']==[],payload['blockers']
    qa=write_model(payload,tmp_path/'fixed-standard.skp')
    assert qa['all_pass'],[row for row in qa['checks']if not row['pass']]

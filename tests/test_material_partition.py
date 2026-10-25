"""Explicit priority partition keeps real source coverage and physical union."""
import copy
import os

import pytest
from shapely.ops import unary_union

from dwg_semantic.material_partition import partition_materials
from dwg_semantic.modeling import prepare_model,write_model
from dwg_semantic.plan_geometry import shape_of
from test_automatic_plan_recognition import rect


def fixture():
    wall=rect('wall','A-WALL',0,0,3000,200)
    column=rect('column','A-COLUMN',1400,-100,1600,300)
    cfg={'layer_mapping':{'A-WALL':{'category':'architectural_wall','height_mm':3200,'footprint_confirmed':True},
                          'A-COLUMN':{'category':'column','height_mm':3200,'footprint_confirmed':True}},
         'material_partition':{'enabled':True,'priority_categories':['column','structural_wall','architectural_wall','curtain','curtain_accessory'],
                               'reason':'User confirms column/structure priority; keep physical union'}}
    def candidate(row,category):
        return {'id':'candidate-'+row['entity_id'],'category':category,'source_entity_ids':[row['entity_id']],
                'source_layer':row['layer'],'source_layers':[row['layer']],
                'footprint':{'outer':[[p['x'],p['y']]for p in row['vertices']],'holes':[]}}
    automatic={'wall_materials':[candidate(wall,'architectural_wall')],
               'columns':[candidate(column,'column')],'facade_systems':[],'door_assemblies':[]}
    return {'entities':[wall,column]}, {'automatic_plan':automatic},cfg


def test_partition_splits_wall_preserves_column_tag_and_accounts_for_all_source_edges():
    doc,classification,cfg=fixture()
    payload=prepare_model(doc,classification,cfg)
    assert not payload['blockers'],payload['blockers']
    assert len(payload['groups'])==3
    column=next(row for row in payload['groups']if row['category']=='column')
    assert column['tag']=='A-COLUMN'
    assert set(column['source_entity_ids'])=={'wall','column'}
    assert set(column['source_layers'])=={'A-WALL','A-COLUMN'}
    assert payload['final_source_accounting']['all_accounted']
    original=unary_union([shape_of(row)for row in classification['automatic_plan']['wall_materials']+
                          classification['automatic_plan']['columns']])
    actual=unary_union([shape_of(row)for row in payload['groups']])
    assert original.symmetric_difference(actual).area==0
    assert not payload['overlap_audit']
    assert payload['source_material_partition_audit']['union_symmetric_difference_mm2']==0


def test_no_partition_is_inferred_without_the_explicit_job_choice():
    doc,classification,cfg=fixture();cfg.pop('material_partition')
    assert any(b['code']=='overlapping_solid_components'for b in prepare_model(doc,classification,cfg)['blockers'])


def test_reference_provenance_does_not_turn_physical_when_its_network_is_partitioned():
    from test_automatic_plan_recognition import line
    doc,classification,cfg=fixture()
    doc['entities'].append(line('locator','Defpoints',(10000,0),(10000,1000)))
    candidate=classification['automatic_plan']['wall_materials'][0]
    candidate['source_entity_ids'].append('locator');candidate['source_layers'].append('Defpoints')
    cfg['object_overrides']={'locator':{'category':'reference','reason':'Reviewed non-material locator'},
        candidate['id']:{'category':'architectural_wall','height_mm':3200,'primary_source_layer':'A-WALL','footprint_confirmed':True}}
    payload=prepare_model(doc,classification,cfg)
    assert not payload['blockers'],payload['blockers']
    column=next(row for row in payload['groups']if row['category']=='column')
    assert 'locator'in column['reference_source_entity_ids']


def test_mixed_jambs_need_an_actual_source_primary_layer_and_keep_both_sources():
    doc={'entities':[rect('left','A-WALL',-2000,0,0,200),rect('right','A-STRUCT',1000,0,3000,400),
                     rect('door','OPENING',0,0,1000,200)]}
    cfg={'layer_mapping':{'A-WALL':{'category':'architectural_wall','height_mm':3200,'footprint_confirmed':True},
                          'A-STRUCT':{'category':'structural_wall','height_mm':3200,'footprint_confirmed':True},
                          'OPENING':{'category':'opening','height_mm':2000,'footprint_confirmed':True}}}
    assert any(b['code']=='opening_jambs_or_lintel_ambiguous'for b in prepare_model(doc,{},cfg)['blockers'])
    cfg['layer_mapping']['OPENING']['lintel_primary_source_layer']='A-STRUCT'
    payload=prepare_model(doc,{},cfg)
    assert not payload['blockers'],payload['blockers']
    lintel=next(row for row in payload['groups']if row['id'].startswith('lintel:'))
    assert lintel['tag']=='A-STRUCT' and lintel['category']=='structural_wall'
    assert lintel['source_layers']==['A-STRUCT','A-WALL']
    assert set(lintel['source_entity_ids'])=={'left','right'}
    cfg['layer_mapping']['OPENING']['lintel_primary_source_layer']='INVENTED'
    assert any(b['code']=='opening_jambs_or_lintel_ambiguous'for b in prepare_model(doc,{},cfg)['blockers'])


def test_invalid_or_cross_z_partition_fails_without_changing_materials():
    row={'id':'a','category':'column','source_id':'S','base_z_mm':0,'height_mm':3000,
         'footprint':{'outer':[[0,0],[100,0],[100,100],[0,100]],'holes':[]}}
    rows=[row,{**copy.deepcopy(row),'id':'b','height_mm':2000}]
    for cfg in ('bad',{'enabled':True,'priority_categories':['column'],'reason':'Z mismatch'}):
        result,audit,issues=partition_materials(rows,cfg)
        assert result==rows and audit is None and issues


@pytest.mark.skipif(os.environ.get('DWG_RUN_NATIVE_TESTS')!='1',reason='Native SketchUp opt-in')
def test_partition_actual_native_roundtrip_keeps_split_groups_and_source_tags(tmp_path):
    doc,classification,cfg=fixture()
    payload=prepare_model(doc,classification,cfg)
    qa=write_model(payload,tmp_path/'partition.skp')
    assert qa['all_pass'],qa
    assert len(qa['groups'])==3

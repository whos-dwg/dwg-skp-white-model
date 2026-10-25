"""World-space readback, including inverse-conditioned native leaves."""
import copy
import sys
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from dwg_semantic.live_repair import LiveRepairError, _group_from_entity
from test_live_repair import live_entity

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests/native_safety'))
from test_native_safety_contract import run_ruby
from test_fixed_live_ruby import case


def mesh_entity():
    row=live_entity('world-wall',outer=[[0,0],[4000,0],[4000,200],[0,200]])
    row['volume_mm3']=4000*200*row['height_mm']
    row['bottom_triangles_mm']=[[[0,0],[4000,0],[4000,200]],[[0,0],[4000,200],[0,200]]]
    return row


def test_zero_area_native_backtracking_seam_uses_actual_tessellation_without_repair():
    row=mesh_entity()
    row['footprint']['outer']=[[0,0],[4000,0],[4000,200],[0,200],[0,100],[650,100],[0,100]]
    assert not Polygon(row['footprint']['outer']).is_valid
    before=copy.deepcopy(row)
    group=_group_from_entity(row)
    assert Polygon(group['outer'],group['holes']).equals(Polygon([[0,0],[4000,0],[4000,200],[0,200]]))
    assert row==before


@pytest.mark.parametrize('mutation',['empty','incomplete','degenerate','disconnected','overlap','wrong_volume'])
def test_actual_bottom_mesh_and_world_volume_fail_closed(mutation):
    row=mesh_entity()
    if mutation=='empty':row['bottom_triangles_mm']=[]
    elif mutation=='incomplete':row['bottom_triangles_mm']=[[[0,0],[1,1]]]
    elif mutation=='degenerate':row['bottom_triangles_mm']=[[[0,0],[1,0],[2,0]]]
    elif mutation=='disconnected':row['bottom_triangles_mm'].append([[5000,0],[6000,0],[6000,200]])
    elif mutation=='overlap':row['bottom_triangles_mm'].append(row['bottom_triangles_mm'][0])
    else:row['volume_mm3']*=10**12
    with pytest.raises(LiveRepairError):_group_from_entity(row)


def test_invalid_loop_without_actual_mesh_is_not_silently_repaired():
    row=mesh_entity();row.pop('bottom_triangles_mm')
    row['footprint']['outer']=[[0,0],[4000,200],[0,200],[4000,0]]
    with pytest.raises(LiveRepairError):_group_from_entity(row)


def test_exactly_zero_area_native_mesh_seams_do_not_move_positive_material():
    row=mesh_entity()
    row['bottom_triangles_mm'].append([[4000,0],[4000,200],[4000,100]])
    group=_group_from_entity(row)
    assert group['area_mm2']==800000


def test_position_matching_and_collision_checks_use_same_actual_bottom_material():
    from test_live_cad_geometry import plan
    from test_live_repair import snapshot
    row=mesh_entity()
    row['footprint']['outer']=[[0,0],[4000,0],[4000,200],[0,200],[0,100],[650,100],[0,100]]
    other=copy.deepcopy(row)
    other.update(entity_id='entity:202',group_id='remote-wall')
    other['footprint']['outer']=[[x,y+1000]for x,y in row['footprint']['outer']]
    other['bottom_triangles_mm']=[[[x,y+1000]for x,y in t]for t in row['bottom_triangles_mm']]
    state=snapshot([row,other]);before=copy.deepcopy(state)
    result=plan(state=state)
    assert result['targets'][0]['entity_id']==row['entity_id']
    assert state==before


def test_production_ruby_reads_conditioned_geometry_in_world_space(tmp_path):
    live,plan=case(tmp_path)
    result=run_ruby({'snapshot':live,'plan':plan,'production_create':True,
                     'mode':'inspect_geometry','mutation':'conditioned_local_geometry'})
    assert result['accepted'],result
    leaf=result['inspection']['entities'][0]
    expected=plan['create'][0]
    assert leaf['volume_mm3']==pytest.approx(expected['mesh']['expected_volume_mm3'],rel=1e-9)
    assert leaf['bottom_triangles_mm']
    actual=_group_from_entity(leaf)
    assert actual['area_mm2']==pytest.approx(Polygon(expected['footprint']['outer']).area,rel=1e-9)
    assert actual['base_z_mm']==pytest.approx(expected['base_z_mm'],abs=1e-7)
    assert actual['height_mm']==pytest.approx(expected['height_mm'],abs=1e-7)
    assert result['operations']==[]

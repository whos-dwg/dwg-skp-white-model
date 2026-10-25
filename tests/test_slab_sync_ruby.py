"""Production Ruby floor writes and safety, using isolated native-shaped data."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import pytest
from test_slab_sync import slab_case,rectangle
from test_model_sync import native
from test_local_snapshot import run_local
from dwg_semantic.live_repair import _mesh,seal_live_repair_plan
from dwg_semantic.model_sync import build_model_sync_plan
from shapely.geometry import Polygon

def fixture(tmp_path,stepped=True):
    c=slab_case(tmp_path,stepped=stepped)
    for r in c['live']['entities']:r['mesh']=_mesh(Polygon(r['footprint']['outer']),r['base_z_mm'],r['height_mm'])
    s=run_local(tmp_path,c['live'],None,mode='global');assert s['accepted'],s
    c['inspection']=s['inspection'];return c

def observation(p):
    o=deepcopy(p['source']['sync_model']);o['observed_at_utc']=(datetime.now(timezone.utc)+timedelta(milliseconds=1)).isoformat();return o

@pytest.mark.parametrize('change',['resize','hole','delete'])
def test_whole_floor_changes_commit_one_native_transaction(tmp_path,change):
    c=fixture(tmp_path);rows=deepcopy(c['raw']['entities'])
    if change=='resize':rows[-3]=rectangle('F1',-1000,-1000,8000,5000)
    elif change=='hole':rows[-2]=rectangle('H1',4500,1000,700,700,'S-OPENING')
    else:rows=[e for e in rows if e['handle']not in ('F1','H1','Z1')]
    p=build_model_sync_plan(c['doc'],native(c,rows),c['inspection'])
    result=run_local(tmp_path,c['live'],None,plan=p,current_cad_model=observation(p),reverse_created_faces=True)
    assert result['accepted'],result
    assert result['operations']==['start','commit']and result['receipt']['verified']

def test_slab_mesh_failure_aborts_every_change(tmp_path):
    c=fixture(tmp_path);rows=deepcopy(c['raw']['entities']);rows[-3]=rectangle('F1',-1000,-1000,8000,5000)
    p=build_model_sync_plan(c['doc'],native(c,rows),c['inspection'])
    result=run_local(tmp_path,c['live'],None,plan=p,current_cad_model=observation(p),inject_face_failure=True)
    assert not result['accepted']and result['operations']==['start','abort'],result

def test_floor_scope_cannot_be_smuggled_into_local_action(tmp_path):
    c=fixture(tmp_path);rows=deepcopy(c['raw']['entities']);rows[-3]=rectangle('F1',-1000,-1000,8000,5000)
    p=build_model_sync_plan(c['doc'],native(c,rows),c['inspection']);p['action']='replace_from_cad';p=seal_live_repair_plan(p)
    result=run_local(tmp_path,c['live'],None,plan=p,current_cad_model=observation(p))
    assert not result['accepted']and result['operations']==[],result

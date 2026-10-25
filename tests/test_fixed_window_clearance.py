"""Fitting the full wall to the roof must not fill the window above its sill."""
from __future__ import annotations

import copy
import os
import pytest

from dwg_semantic.fixed_standard_validation import validate_fixed_payload
from dwg_semantic.modeling import write_model
from dwg_semantic.modeling_standard import enforce_standard_payload
from dwg_semantic.pipeline import _draft_payload
from test_fixed_modeling_standard import fixture
from test_modeling import rectangle


def window_payload():
    doc,config=fixture()
    doc['entities'].append(rectangle('window',layer='Windows',x=1000,width=900,depth=200))
    config['layer_mapping']['Windows']={'category':'opening','base_z_mm':900,'height_mm':1200,
                                       'footprint_confirmed':True}
    return enforce_standard_payload(doc,_draft_payload(doc,{},config),config,{})


def test_fixed_wall_roof_fit_keeps_real_window_sill_and_clear_height():
    payload=window_payload()
    assert not payload['blockers'],payload['blockers']
    assert validate_fixed_payload(payload)==[]
    parts={row.get('opening_part_role'):row for row in payload['groups']if row.get('opening_part_role')}
    assert parts['sill']['base_z_mm']==0 and parts['sill']['height_mm']==900
    assert parts['lintel']['base_z_mm']==2100 and parts['lintel']['height_mm']==1100
    assert payload['opening_audit'][0]['base_z_mm']==900
    assert payload['opening_audit'][0]['height_mm']==1200
    forged=copy.deepcopy(payload)
    next(row for row in forged['groups']if row.get('opening_part_role')=='sill')['height_mm']=500
    assert any(row['code']=='fixed_standard_actual_top_not_at_local_underside'
               for row in validate_fixed_payload(forged))


@pytest.mark.skipif(os.environ.get('DWG_RUN_NATIVE_TESTS')!='1',reason='Actual isolated SketchUp C API')
def test_actual_saved_fixed_model_preserves_window_sill_clearance_and_roof(tmp_path):
    payload=window_payload()
    assert not payload['blockers'],payload['blockers']
    qa=write_model(payload,tmp_path/'fixed-window.skp')
    assert qa['all_pass'],[row for row in qa['checks']if not row['pass']]

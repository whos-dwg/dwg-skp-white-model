"""Automatic routing uses actual selection evidence; no app is contacted."""
from datetime import datetime,timezone
from unittest.mock import patch
import json
import pytest
from dwg_semantic.sync import main
from test_model_sync import case,native

def setup(tmp_path):
    c=case(tmp_path);job=tmp_path/'job';job.mkdir()
    (job/'document.json').write_text(json.dumps(c['doc']),encoding='utf-8')
    empty={'success':True,'status':'EMPTY_SELECTION','observed_at_utc':datetime.now(timezone.utc).isoformat(),
        'document':{'path':str(c['source'])},'selection_source':'PickfirstSelectionSet','selection_count':0,
        'returned_count':0,'objects':[],'truncated':False}
    sel=tmp_path/'selection.json';sel.write_text(json.dumps(empty))
    cad=tmp_path/'cad.json';cad.write_text(json.dumps(native(c)))
    return c,job,sel,cad

def test_actual_empty_selection_auto_routes_to_complete_difference(tmp_path,capsys):
    c,job,sel,cad=setup(tmp_path)
    with patch('dwg_semantic.sketchup_live.inspect_active_white_model',return_value=c['live'])as inspect,patch('dwg_semantic.cad_model.capture_model',side_effect=AssertionError('provided native evidence')):
        assert main(['plan',str(job),'--selection-json',str(sel),'--cad-model-json',str(cad),'--output',str(tmp_path/'plan.json')])==0
    inspect.assert_called_once_with(whole_model=True,port=51236)
    result=json.loads(capsys.readouterr().out)
    assert result['snapshot_mode']=='whole_model'and result['status']=='up_to_date'
    assert all(not isinstance(v,list)for v in result['difference_summary'].values())

@pytest.mark.parametrize('bad',['missing','truncated','selected','wrong_drawing','stale'])
def test_explicit_model_mode_cannot_bypass_real_complete_empty_selection(tmp_path,capsys,bad):
    c,job,sel,cad=setup(tmp_path);s=json.loads(sel.read_text())
    if bad=='truncated':s['truncated']=True
    elif bad=='selected':s['selection_count']=1
    elif bad=='wrong_drawing':s['document']['path']='different.dwg'
    elif bad=='stale':s['observed_at_utc']='2000-01-01T00:00:00+00:00'
    sel.write_text(json.dumps(s))
    args=['plan',str(job),'--mode','model','--cad-model-json',str(cad),'--output',str(tmp_path/'plan.json')]
    if bad!='missing':args+=['--selection-json',str(sel)]
    with patch('dwg_semantic.sketchup_live.inspect_active_white_model',side_effect=AssertionError('reject before model read')):
        assert main(args)==2
    assert json.loads(capsys.readouterr().err)['status']=='error'
    assert not(tmp_path/'plan.json').exists()

def test_nonempty_selection_keeps_local_route(tmp_path,capsys):
    c,job,sel,cad=setup(tmp_path);s=json.loads(sel.read_text());s['selection_count']=1;sel.write_text(json.dumps(s))
    with patch('dwg_semantic.live_workflow.live_repair_plan_job',return_value={'status':'ready'})as local,patch('dwg_semantic.live_workflow.live_model_plan_job',side_effect=AssertionError('do not widen')):
        assert main(['plan',str(job),'--selection-json',str(sel),'--output',str(tmp_path/'plan.json')])==0
    assert local.call_args.kwargs['partial_snapshot']is True
    capsys.readouterr()

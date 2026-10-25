"""Partial-read client and workflow contracts; no live application connections."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from unittest.mock import patch

import pytest

from dwg_semantic.live_repair import build_live_repair_plan, validate_plan_digest
from dwg_semantic.local_snapshot import make_local_query, validate_local_snapshot
from dwg_semantic.modeling_standard import VERSION
from dwg_semantic.sketchup_live import (
    SketchUpLiveError, apply_live_repair_plan, inspect_active_white_model,
)
from test_sync_cli import (
    bind_case, opening_case, plan_args, read_json, run_cli, write_json,
)


def health(*, partial=True):
    result = {'protocol': 'dwg-live-repair', 'protocol_version': '2.0',
              'fixed_modeling_standard_version': VERSION,
              'supported_operations': ['inspect_white_model', 'apply_live_repair']}
    if partial:
        result.update(partial_snapshot_version='1.0')
        result['supported_operations'].append('inspect_white_model_local')
    return result


def query(case, *, action='replace_from_cad'):
    return make_local_query(
        read_json(case['job'] / 'document.json'), read_json(case['selection']),
        action=action, live_cad_geometry=action == 'replace_from_cad',
        auto_match_position=action == 'replace_from_cad' and not case['scope'],
        wall_system_scope=read_json(case['scope']) if case['scope'] else None,
    )


def local_state(case, request=None):
    state = copy.deepcopy(case['state'])
    request = request or query(case)
    bounds = [row['footprint']['outer'] for row in state['entities']]
    points = [point for ring in bounds for point in ring]
    q = request['bounds_xy_mm']
    if q:
        points += [[q[0], q[1]], [q[2], q[3]]]
    state['local_read'] = {
        'schema': 'dwg-skp-local-read-v1', 'query': request,
        'bounds_xy_mm': [min(p[0] for p in points)-.02, min(p[1] for p in points)-.02,
                         max(p[0] for p in points)+.02, max(p[1] for p in points)+.02],
        'inventory_sha256': 'a' * 64,
        'entity_ids': [row['entity_id'] for row in state['entities']],
        'protected_entity_ids': [row['entity_id'] for row in state.get('protected_entities', [])],
    }
    state['read_stats'] = {'inventory_groups': 5255, 'geometry_entities': len(state['entities'])}
    return state


@pytest.mark.parametrize('compound', [False, True])
def test_default_cli_requests_only_local_geometry_and_binds_server_scope(tmp_path, capsys, compound):
    case = bind_case(tmp_path, compound=compound)
    expected_query = query(case)
    state = local_state(case, expected_query)
    destination = tmp_path / 'local-plan.json'
    args = [v for v in plan_args(case, destination) if v != '--full-model']
    with patch('dwg_semantic.sketchup_live.inspect_active_white_model', return_value=state) as inspect, \
         patch('dwg_semantic.sketchup_live.apply_live_repair_plan', side_effect=AssertionError('plan is read-only')):
        assert run_cli(args) == 0
    assert inspect.call_args.kwargs == {'port': 51236, 'local_query': expected_query}
    result = json.loads(capsys.readouterr().out)
    assert result['snapshot_mode'] == 'local_geometry'
    assert result['read_stats'] == state['read_stats']
    planned = read_json(destination)
    assert planned['model']['local_read'] == state['local_read']
    assert validate_plan_digest(planned) == planned['plan_sha256']
    assert planned['source']['sync_selection']['selection_snapshot'] == read_json(case['selection'])
    assert not (tmp_path / 'current.skp').exists()


def test_default_cli_refuses_full_snapshot_instead_of_silent_fallback(tmp_path, capsys):
    case = bind_case(tmp_path)
    destination = tmp_path / 'must-not-exist.json'
    args = [v for v in plan_args(case, destination) if v != '--full-model']
    with patch('dwg_semantic.sketchup_live.inspect_active_white_model', return_value=case['state']) as inspect:
        assert run_cli(args) == 2
    assert 'local_query' in inspect.call_args.kwargs
    assert not destination.exists()
    assert '局部讀取' in json.loads(capsys.readouterr().err)['message']


def test_changed_source_fails_before_partial_bridge_request(tmp_path, capsys):
    case = bind_case(tmp_path)
    case['source'].write_bytes(b'changed after normalization')
    destination = tmp_path / 'must-not-exist.json'
    args = [v for v in plan_args(case, destination) if v != '--full-model']
    with patch('dwg_semantic.sketchup_live.inspect_active_white_model', side_effect=AssertionError('stale source')):
        assert run_cli(args) == 2
    assert not destination.exists()
    assert '來源已失效' in json.loads(capsys.readouterr().err)['message']


def test_client_requests_local_endpoint_and_validates_proof(tmp_path):
    case = bind_case(tmp_path)
    request = query(case)
    state = local_state(case, request)
    with patch('dwg_semantic.sketchup_live._call', side_effect=[health(), state]) as remote:
        assert inspect_active_white_model(local_query=request, port=51237) == state
    assert [call.args[0] for call in remote.call_args_list] == ['health', 'inspect_white_model_local']
    assert remote.call_args_list[1].args[1] == {'query': request}
    assert all(call.kwargs['port'] == 51237 for call in remote.call_args_list)


@pytest.mark.parametrize('missing', ['version', 'operation'])
def test_old_bridge_is_rejected_before_any_inspection(tmp_path, missing):
    case = bind_case(tmp_path)
    old = health()
    if missing == 'version':
        old.pop('partial_snapshot_version')
    else:
        old['supported_operations'].remove('inspect_white_model_local')
    with patch('dwg_semantic.sketchup_live._call', return_value=old) as remote:
        with pytest.raises(SketchUpLiveError, match='局部讀取'):
            inspect_active_white_model(local_query=query(case))
    assert remote.call_count == 1 and remote.call_args.args[0] == 'health'


@pytest.mark.parametrize('mutation', ['missing', 'wrong_query', 'missing_entity', 'missing_protected', 'bad_inventory'])
def test_incomplete_or_wrong_local_proof_is_rejected(tmp_path, mutation):
    case = bind_case(tmp_path)
    request = query(case)
    state = local_state(case, request)
    if mutation == 'missing':
        state.pop('local_read')
    elif mutation == 'wrong_query':
        state['local_read']['query'] = {**request, 'target_entity_ids': ['entity:unrequested']}
    elif mutation == 'missing_entity':
        state['local_read']['entity_ids'] = []
    elif mutation == 'missing_protected':
        state['protected_entities'] = [{'entity_id': 'entity:roof'}]
    else:
        state['local_read']['inventory_sha256'] = 'unverified'
    with patch('dwg_semantic.sketchup_live._call', side_effect=[health(), state]):
        with pytest.raises(ValueError, match='局部讀取'):
            inspect_active_white_model(local_query=request)


@pytest.mark.parametrize('compound', [False, True])
def test_out_of_scope_created_geometry_cannot_be_sealed_as_local_plan(tmp_path, compound):
    case = bind_case(tmp_path, compound=compound)
    state = local_state(case)
    state['local_read']['bounds_xy_mm'] = [0, 0, 1000, 200]
    with pytest.raises(ValueError, match='範圍'):
        build_live_repair_plan(
            read_json(case['job'] / 'document.json'), read_json(case['selection']), state,
            action='replace_from_cad', live_cad_geometry=True, auto_match_position=not compound,
            **({'wall_system_scope': read_json(case['scope'])} if compound else {}),
        )


def local_plan(case):
    return build_live_repair_plan(
        read_json(case['job'] / 'document.json'), read_json(case['selection']), local_state(case),
        action='replace_from_cad', live_cad_geometry=True, auto_match_position=True,
    )


def reread(case):
    current = read_json(case['selection'])
    current['observed_at_utc'] = datetime.now(timezone.utc).isoformat()
    return current


def test_local_apply_requires_capability_and_does_not_send_plan_to_old_bridge(tmp_path):
    case = bind_case(tmp_path)
    plan = local_plan(case)
    with patch('dwg_semantic.sketchup_live._call', return_value=health(partial=False)) as remote:
        with pytest.raises(SketchUpLiveError, match='局部讀取'):
            apply_live_repair_plan(plan, confirmation_sha256=plan['plan_sha256'], current_selection=reread(case))
    assert remote.call_count == 1 and remote.call_args.args[0] == 'health'


def test_local_apply_still_requires_current_selection_before_any_remote_call(tmp_path):
    case = bind_case(tmp_path)
    plan = local_plan(case)
    with patch('dwg_semantic.sketchup_live._call', side_effect=AssertionError('no selection reread')):
        with pytest.raises(ValueError, match='重新讀取'):
            apply_live_repair_plan(plan, confirmation_sha256=plan['plan_sha256'])


def test_tampered_local_proof_cannot_reach_capability_or_apply_requests(tmp_path):
    case = bind_case(tmp_path)
    plan = local_plan(case)
    plan['model']['local_read']['inventory_sha256'] = 'b' * 64
    with patch('dwg_semantic.sketchup_live._call', side_effect=AssertionError('tampered local proof')):
        with pytest.raises(ValueError, match='雜湊'):
            apply_live_repair_plan(plan, confirmation_sha256=plan['plan_sha256'], current_selection=reread(case))


def test_local_apply_sends_exact_plan_only_after_current_read_and_capability(tmp_path):
    case = bind_case(tmp_path)
    plan = local_plan(case)
    with patch('dwg_semantic.sketchup_live._call', side_effect=[health(), {'status': 'applied'}]) as remote:
        assert apply_live_repair_plan(plan, confirmation_sha256=plan['plan_sha256'], current_selection=reread(case))['status'] == 'applied'
    assert [call.args[0] for call in remote.call_args_list] == ['health', 'apply_live_repair']
    assert remote.call_args.args[1]['plan'] == plan


def test_normalized_selection_query_includes_all_global_descendant_identifiers(tmp_path):
    case = opening_case(tmp_path)
    doc = read_json(case['job'] / 'document.json')
    doc['resolved_instances'] = [{
        'type': 'LINE', 'entity_id': 'source:D1:CHILD', 'source_handle': 'CHILD', 'handle': 'CHILD',
        'source_file': str(case['source']), 'instance_path': 'D1',
    }]
    request = make_local_query(doc, read_json(case['selection']), action='cut_opening')
    assert {'D1', 'source::D1', 'source:D1:CHILD', 'CHILD'} <= set(request['source_identifiers'])


def test_compound_query_keeps_all_selected_roles_in_source_lookup(tmp_path):
    case = bind_case(tmp_path, compound=True)
    request = query(case)
    scope = read_json(case['scope'])
    assert set(scope['roles']) <= set(request['source_identifiers'])
    assert request['wall_layer'] == scope['wall_layer']
    assert request['bounds_xy_mm'] == [0, 0, 5000, 1400]


def test_local_query_remove_wrong_uses_selection_or_explicit_ids_without_cad():
    selected = make_local_query({}, None, action='remove_wrong')
    explicit = make_local_query({}, None, action='remove_wrong', target_entity_ids=['entity:101'])
    assert selected['bounds_xy_mm'] is None and selected['include_selected']
    assert not explicit['include_selected'] and explicit['target_entity_ids'] == ['entity:101']


def test_explicit_target_query_is_canonical_before_bridge_round_trip():
    request = make_local_query({}, None, action='remove_wrong',
                               target_entity_ids=['entity:202', 'entity:101', 'entity:202'])
    assert request['target_entity_ids'] == ['entity:101', 'entity:202']

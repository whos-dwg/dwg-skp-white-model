"""Conservative local read queries and coverage proofs for the shared planner.

The bridge recomputes actual bounds/membership and local meshes at apply time.
This module never caches geometry or replaces source/selection freshness.
"""
from __future__ import annotations

from copy import deepcopy
import math

QUERY_SCHEMA = 'dwg-skp-local-query-v1'
READ_SCHEMA = 'dwg-skp-local-read-v1'
MARGIN_MM = .02


def _bounds(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 4 or
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value) or
            value[2] <= value[0] or value[3] <= value[1]):
        raise ValueError('局部讀取缺少有效的世界座標 XY 範圍。')
    return list(value)


def make_local_query(document, selection, *, action, target_entity_ids=None,
                     live_cad_geometry=False, auto_match_position=False, wall_system_scope=None):
    """Reuse the existing CAD geometry readers; do not derive walls from bboxes."""
    from .selection_openings import _selection_errors, _source_entities, _selection_polygon
    regions = []
    identifiers = set()
    if action != 'remove_wrong':
        drawing, scale = _selection_errors(selection, document)
        for obj in selection['objects']:
            identifiers.add(str(obj['handle']))
            if not live_cad_geometry:
                for entity in _source_entities(document, drawing, str(obj['handle'])):
                    identifiers.update(str(entity[k]) for k in ('entity_id', 'source_handle', 'handle') if entity.get(k))
        if wall_system_scope is not None:
            from .live_wall_system import prepare_wall_system
            regions = [prepare_wall_system(document, selection, wall_system_scope)['material']]
        elif live_cad_geometry:
            from .live_cad_geometry import live_regions
            regions = [r['polygon'] for r in live_regions(document, selection)]
        elif action in {'add_missing', 'replace_from_cad'}:
            from .live_repair import _selection_regions
            rows = _selection_regions(document, selection)
            regions = [r['polygon'] for r in rows]
            for row in rows:
                identifiers.update(row['source_entity_ids'])
                identifiers.update(row['source_handles'])
        else:
            for obj in selection['objects']:
                polygon, reason = _selection_polygon(obj, _source_entities(document, drawing, str(obj['handle'])), scale)
                if polygon is None:
                    raise ValueError('局部開口讀取缺少可驗證範圍：' + reason)
                regions.append(polygon)
        if not regions:
            raise ValueError('局部讀取沒有可驗證的 CAD 範圍。')
        extents = [r.bounds for r in regions]
        bounds = _bounds([min(b[0] for b in extents), min(b[1] for b in extents),
                          max(b[2] for b in extents), max(b[3] for b in extents)])
    else:
        bounds = None
    return {
        'schema': QUERY_SCHEMA, 'bounds_xy_mm': bounds,
        'source_identifiers': sorted(identifiers), 'target_entity_ids': sorted(set(target_entity_ids or [])),
        'include_selected': wall_system_scope is None and not target_entity_ids and (
            action == 'remove_wrong' or (action == 'replace_from_cad' and not auto_match_position)),
        'wall_layer': wall_system_scope.get('wall_layer') if wall_system_scope else None,
    }


def validate_local_snapshot(snapshot, query=None):
    proof = snapshot.get('local_read')
    if not isinstance(proof, dict) or proof.get('schema') != READ_SCHEMA:
        raise ValueError('橋接器未提供完整局部讀取證明；不能靜默改用全模型快照。')
    actual_query = proof.get('query')
    if (not isinstance(actual_query, dict) or actual_query.get('schema') != QUERY_SCHEMA or
            not isinstance(actual_query.get('include_selected'), bool) or
            any(not isinstance(actual_query.get(k), list) or
                any(not isinstance(v, str) or not v for v in actual_query[k])
                for k in ('source_identifiers', 'target_entity_ids')) or
            actual_query.get('wall_layer') is not None and not isinstance(actual_query['wall_layer'], str)):
        raise ValueError('局部讀取缺少完整查詢契約。')
    if query is not None and actual_query != query:
        raise ValueError('局部讀取查詢與回傳證明不一致。')
    bounds = _bounds(proof.get('bounds_xy_mm'))
    if actual_query.get('bounds_xy_mm') is not None:
        requested = _bounds(actual_query['bounds_xy_mm'])
        if not (bounds[0] <= requested[0]+MARGIN_MM and bounds[1] <= requested[1]+MARGIN_MM and
                bounds[2] >= requested[2]-MARGIN_MM and bounds[3] >= requested[3]-MARGIN_MM):
            raise ValueError('局部讀取證明未涵蓋原始查詢範圍。')
    digest = proof.get('inventory_sha256')
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('局部讀取缺少模型清單簽章。')
    for key, rows in [('entity_ids', snapshot.get('entities', [])),
                      ('protected_entity_ids', snapshot.get('protected_entities', []))]:
        ids = [r['entity_id'] for r in rows]
        if not isinstance(proof.get(key), list) or len(set(ids)) != len(ids) or sorted(proof[key]) != sorted(ids):
            raise ValueError('局部讀取構件清單不完整。')
    return proof


def bind_local_read(plan, snapshot):
    """Bind the server scope to canonical bytes and forbid out-of-scope writes."""
    if 'local_read' not in snapshot:
        return plan
    proof = validate_local_snapshot(snapshot)
    bounds = proof['bounds_xy_mm']
    if any(t['entity_id'] not in proof['entity_ids'] for t in plan['targets']):
        raise ValueError('修正目標超出局部讀取範圍。')
    for row in plan['create']:
        for point in row['mesh']['vertices']:
            if not (bounds[0]-MARGIN_MM <= point[0] <= bounds[2]+MARGIN_MM and
                    bounds[1]-MARGIN_MM <= point[1] <= bounds[3]+MARGIN_MM):
                raise ValueError('新構件超出已驗證的局部讀取範圍；需擴大查詢並重新規劃。')
    plan['model']['local_read'] = deepcopy(proof)
    return plan

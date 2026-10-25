"""Read-only checks for fixed-standard plans at prepared and native boundaries."""
from __future__ import annotations


def is_source_bound_opening_sill(group, opening_records):
    """A wall below a real window stops at the window bottom, not the roof.

    Match actual geometry and the approved opening identity/height, never the
    group's name or a caller's self-declared short-wall role.
    """
    from .plan_geometry import shape_of
    if group.get('opening_part_role') not in (None,'sill') or not group.get('opening_ids'):
        return False
    top=group.get('base_z_mm',0)+group.get('height_mm',0)
    for opening in opening_records:
        if (opening.get('id') not in group['opening_ids'] or not opening.get('clearance_footprint')
                or not isinstance(opening.get('base_z_mm'),(int,float))
                or abs(top-opening['base_z_mm'])>.01):
            continue
        if shape_of(group).difference(shape_of(opening['clearance_footprint'])).area<=.001:
            return True
    return False


def validate_fixed_payload(payload):
    import math
    from shapely.errors import GEOSException
    from .modeling_standard import FIXED, CATEGORIES
    if 'modeling_standard' not in payload:
        return []  # Historical low-level recipes are not retroactively rewritten.
    from .plan_geometry import shape_of
    from .whole_slabs import expand_groups
    from shapely.ops import unary_union
    issues = []
    def block(code, **detail):
        issues.append({'code': code, **detail})
    if payload.get('modeling_standard') != FIXED:
        block('fixed_standard_version_or_settings_invalid')
    audit = payload.get('fixed_standard_audit') or {}
    if audit.get('standard') != FIXED or not audit.get('all_pass') or audit.get('issues'):
        block('fixed_standard_complete_passing_audit_required')
    groups = payload.get('groups', [])
    slabs = [g for g in groups if g.get('category') == 'slab']
    for slab in slabs:
        if slab.get('mode') != 'boundary_mesh' or slab.get('assembly_mode') != 'whole_uniform_thickness':
            block('fixed_standard_requires_whole_uniform_slab', component_id=slab.get('id'))
    fire_ids = set(audit.get('fire_body_source_entity_ids', []))
    for group in groups:
        ids = set(group.get('source_entity_ids', []))
        if ids and ids <= fire_ids and group.get('semantic_role') != 'door_lintel':
            block('fixed_standard_fire_body_present', component_id=group.get('id'))
    openings=[*payload.get('opening_audit',[]),*payload.get('selection_opening_audit',[])]
    verticals = [g for g in groups if g.get('category') in CATEGORIES
                 and not str(g.get('semantic_subrole', '')).lower().startswith(('railing', '欄杆', '栏杆'))
                 and not is_source_bound_opening_sill(g,openings)]
    if not verticals:
        return issues
    fit = payload.get('wall_under_roof_audit') or {}
    if not fit.get('enabled') or not fit.get('all_pass'):
        block('fixed_standard_complete_underside_fit_audit_required')
    roofs = [g for g in expand_groups(slabs) if g.get('slab_role') == 'overhead_slab'
             or g.get('host_slab_role') == 'overhead_slab']
    projections = fit.get('height_projection_regions', [])
    try:
        for group in verticals:
            unclaimed = shape_of(group)
            top = group['base_z_mm'] + group['height_mm']
            if not unclaimed.is_valid or unclaimed.area <= 0 or not math.isfinite(top) or group['height_mm'] <= 0:
                block('fixed_standard_invalid_vertical_geometry_or_height', component_id=group.get('id'))
                continue
            for cap in sorted({r['base_z_mm'] for r in roofs}):
                area = unclaimed.intersection(unary_union([shape_of(r) for r in roofs if r['base_z_mm'] == cap]))
                if area.area > .001 and abs(top - cap) > .01:
                    block('fixed_standard_actual_top_not_at_local_underside', component_id=group['id'], cap_z_mm=cap)
                unclaimed = unclaimed.difference(area)
            for region in projections:
                area = unclaimed.intersection(shape_of(region['footprint']))
                refs = region.get('roof_component_ids', [])
                valid = bool(refs) and all(any(r['id'] == key and abs(r['base_z_mm'] - region['cap_z_mm']) < .01
                                               for r in roofs) for key in refs)
                if area.area > .001 and (not valid or abs(top - region['cap_z_mm']) > .01):
                    block('fixed_standard_invalid_height_projection', component_id=group['id'])
                unclaimed = unclaimed.difference(area) if valid else unclaimed
            if unclaimed.area > .001:
                block('fixed_standard_underside_coverage_required', component_id=group['id'])
    except (KeyError, TypeError, ValueError, GEOSException) as error:
        block('fixed_standard_invalid_geometry_or_height_evidence', detail=str(error))
    return issues

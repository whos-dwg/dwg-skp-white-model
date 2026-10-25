"""Final, reverse-direction source coverage (not candidate classification)."""
from __future__ import annotations

from collections import Counter, defaultdict


ANNOTATIONS = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "DIMENSION", "MLEADER", "MULTILEADER", "POINT"}
CONFIRMED_REFERENCE_REASONS = {"user_confirmed_reference", "approved_opening_reference",
                               "source_annotation_reference", "reviewed_reference", "job_source_reference_decision"}


def _verified_slab_opening_modifiers(payload, decisions, entities):
    """A hole mark proves a subtraction, never material inside that hole."""
    from .modeling import validate_slab_payload
    from .plan_geometry import shape_of
    slabs=[row for row in payload.get('groups',[])if row.get('category')=='slab']
    if not slabs or validate_slab_payload(payload):
        return {},set()
    expected={(system['id'],opening['id']):opening
              for system in decisions.get('slabs',{}).get('systems',[])for opening in system.get('openings',[])}
    declared=defaultdict(list);regions=defaultdict(list)
    for opening in payload.get('slab_audit',{}).get('openings',[]):
        system=opening.get('system_id');identity=opening.get('id')
        chosen=expected.get((system,identity))
        if (chosen is None or opening.get('status')!='targeted_through_opening'
                or not shape_of(opening['footprint']).equals(shape_of(chosen['footprint']))
                or set(opening.get('source_entity_ids',[]))!=set(chosen.get('source_entity_ids',[]))
                or not any(row.get('slab_system_id')==system for row in slabs)):
            continue
        for key in opening.get('source_entity_ids',[]):
            declared[key].append(identity)
            regions[key].append(shape_of(opening['footprint']))
    pairs={(row['id'],trace['entity_id'])for row in slabs for trace in row.get('sources',[])
           if trace.get('roles')==['opening_boundary'] and trace['entity_id']in declared}
    # Do not let an arbitrary distant source ID masquerade as an opening
    # symbol. Long/shared support lines need their other geometry disposition;
    # an explicit 2D reference choice remains valid, but is not material proof.
    from .modeling import _linework
    from shapely.ops import unary_union
    modifiers={}
    for key,ids in declared.items():
        entity=entities.get(key,{})
        lines=_linework(entity)
        length=sum(line.length for line in lines)
        tolerance=.01
        if 'stair'in str(entity.get('layer','')).lower():
            tolerance=max(tolerance,decisions.get('recognition',{}).get('settings',{}).get('stair_mark_boundary_tolerance_mm',0))
        region=unary_union(regions[key]).buffer(tolerance)
        missing=sum(line.difference(region).length for line in lines)
        if length>0 and missing<=max(.001,length*1e-6):modifiers[key]=ids
    return modifiers,pairs


def _confirmed_rc_source_coverage(payload, decisions):
    """A verified subtraction accounts for removed source material, not a leak.

    Recompute from the actual RC and original snapshots and compare the
    reviewed recipe. Prepared validation separately binds those snapshots to
    the immutable approved base; source linework is still checked below.
    """
    removed = defaultdict(list)
    audit = payload.get('base_component_cut_audit') or {}
    if not audit.get('enabled'):
        return removed, []
    try:
        from .modeling import _json_hash, validate_building_cut_payload
        from .slab_contacts import resolve_building_slab_cuts
        from .whole_slabs import expand_groups
        if validate_building_cut_payload(payload):
            raise ValueError('Actual source subtraction audit is invalid')
        config = decisions.get('building_slab_cuts')
        if not isinstance(config, dict) or config.get('enabled') is not True:
            raise ValueError('Source removal needs the explicit current subtraction decision')
        slabs = expand_groups([row for row in payload.get('groups', []) if row.get('category') == 'slab'])
        rebuilt = resolve_building_slab_cuts(audit['original_groups'], slabs, config)
        if rebuilt['blockers']:
            raise ValueError('RC source removal cannot be recomputed')
        for field in ('operation', 'target_component_ids', 'target_categories', 'original_groups', 'cutters', 'cuts'):
            if _json_hash(rebuilt['audit'].get(field)) != _json_hash(audit.get(field)):
                raise ValueError('RC source removal differs from its current decision and actual cutters')
        actual = {row['id']: row for row in payload.get('groups', [])}
        if any(actual.get(row['id']) != row for row in rebuilt['groups']):
            raise ValueError('RC source removal differs from actual remaining components')
        originals = {row['id']: row for row in audit['original_groups']}
        for cut in audit['cuts']:
            if cut['removed_volume_mm3'] <= 0:
                continue
            original = originals[cut['original_component_id']]
            for identity in original['source_entity_ids']:
                removed[identity].append(original)
        return removed, []
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
        return defaultdict(list), [{'code': 'invalid_source_modifier_coverage', 'detail': str(error)}]


def final_source_accounting(document, payload, decisions, classification=None):
    from .modeling import entity_key, model_entities, _raw_layer, _linework
    entities = {entity_key(row): row for row in model_entities(document)}
    handles=Counter(str(row.get('source_handle')or row.get('handle')or'')for row in entities.values())
    modeled, referenced, excluded = defaultdict(list), {}, {}
    try:
        slab_modifiers,modifier_pairs=_verified_slab_opening_modifiers(payload,decisions,entities)
    except (KeyError,TypeError,ValueError,AttributeError,OverflowError):
        slab_modifiers,modifier_pairs={},set()
    for row in payload.get("groups", []):
        for identity in row.get("source_entity_ids", []):
            if (row['id'],identity)in modifier_pairs:
                continue
            scope=decisions.get('object_overrides',{}).get(identity,{})
            if (identity in row.get('reference_source_entity_ids',[]) and scope.get('category')=='reference'
                    and str(scope.get('reason')or'').strip()):
                continue
            modeled[identity].append(row["id"])
    for row in payload.get("references", []):
        for identity in row.get("source_entity_ids", [row.get("id")]):
            referenced[identity] = row
    for row in payload.get("excluded", []):
        for identity in row.get("source_entity_ids", [row.get("id")]):
            excluded[identity] = row
    air = defaultdict(list)
    for system in (classification or {}).get("automatic_plan", {}).get("facade_systems", []):
        for row in system.get("bay_air_regions", []):
            for identity in row["source_entity_ids"]:
                air[identity].append(row["id"])
    rows, missing = [], []
    opening_ids={row.get('id')for row in payload.get('opening_audit',[])}
    groups_by_id={row['id']:row for row in payload.get('groups',[])}
    automatic=(classification or{}).get('automatic_plan',{})
    candidate_rows=[*automatic.get('wall_materials',[]),*automatic.get('columns',[]),
                    *[row for system in automatic.get('facade_systems',[])for row in system.get('sections',[])]]
    candidates={row['id']:row for row in candidate_rows}
    declarations=decisions.get('source_dispositions',{})
    removed, invalid_declarations = _confirmed_rc_source_coverage(payload, decisions)
    if not isinstance(declarations,dict):
        invalid_declarations.append({'code':'source_dispositions_must_be_an_object'})
        declarations={}
    for key,value in declarations.items():
        if key not in entities or not isinstance(value,dict) or value.get('status')not in{'reference','exclude'} or not str(value.get('reason')or'').strip():
            invalid_declarations.append({'code':'invalid_or_unmatched_source_disposition','source_entity_id':key})
        elif value.get('scope') is not None and (value['scope']!='unmodeled_remainder'
                or value.get('status')!='reference' or not modeled[key]):
            invalid_declarations.append({'code':'invalid_source_remainder_reference','source_entity_id':key})
    for key in excluded:
        if key not in entities:
            invalid_declarations.append({'code':'excluded_source_not_in_document','source_entity_id':key})
    for identity, entity in sorted(entities.items()):
        layer = _raw_layer(entity, payload.get("source_tag_policy", decisions.get("source_tag_policy", "effective")))
        config = dict(decisions.get("layer_mapping", {}).get(layer) or {})
        overrides=decisions.get('object_overrides',{})
        handle=str(entity.get('source_handle')or entity.get('handle')or'')
        override=overrides.get(identity)
        if override is None and handles[handle]==1:
            override=overrides.get(handle)
        config.update(override or {})
        declaration = declarations.get(identity)
        status, reason, components = None, None, []
        geometry_validation=None
        if modeled[identity] or removed[identity]:
            components = modeled[identity]
            if components:
                status, reason = "modeled_or_material_boundary_covered", "actual_final_component_provenance"
            else:
                status, reason = "material_subtracted_by_confirmed_rc", "source_bound_current_rc_subtraction"
            from .plan_geometry import shape_of
            from shapely.geometry import LineString, Polygon
            from shapely.ops import unary_union
            try:
                override=config.get('footprint')
                if override is None:
                    approved=[]
                    for key in components:
                        candidate_id=groups_by_id[key].get('automatic_recognition_id')
                        choice=overrides.get(candidate_id,{})
                        if identity in candidates.get(candidate_id,{}).get('source_entity_ids',[]) and choice.get('footprint'):
                            approved.append(shape_of(choice['footprint']))
                    if approved:
                        adopted=unary_union(approved)
                        lines=[LineString(ring.coords)for polygon in getattr(adopted,'geoms',[adopted])
                               for ring in [polygon.exterior,*polygon.interiors]]
                    else:
                        lines=_linework(entity)
                if isinstance(override,dict):
                    source_shape=Polygon(override['outer'],override.get('holes',[]))
                    lines=[LineString(ring.coords)for ring in [source_shape.exterior,*source_shape.interiors]]
                elif override is not None:
                    lines=_linework(entity)
                total=sum(line.length for line in lines)
                if total>.00001:
                    final_groups=[groups_by_id[key]for key in components]
                    coverage=[shape_of(group)for group in final_groups]
                    coverage.extend(shape_of(group) for group in removed[identity])
                    lineage={key for group in final_groups for key in [group['id'],*group.get('host_lineage_ids',[])]}
                    for opening in payload.get('opening_audit',[]):
                        if lineage&set(opening.get('host_ids',[])):
                            coverage.append(shape_of(opening['clearance_footprint']))
                    material=unary_union(coverage)
                    tolerance=.01
                    unsupported=sum(line.difference(material.buffer(tolerance)).length for line in lines)
                    geometry_validation={'source_length_mm':total,'uncovered_length_mm':unsupported,
                                         'plan_boundary_tolerance_mm':tolerance}
                    if unsupported>max(.1,total*1e-6):
                        if isinstance(declaration,dict) and declaration.get('scope')=='unmodeled_remainder' and declaration.get('status')=='reference' and str(declaration.get('reason')or'').strip():
                            status,reason='material_provenance_with_reference_remainder',declaration['reason']
                            geometry_validation['unmodeled_remainder_disposition']='explicit_reviewed_reference_not_material_coverage'
                        else:
                            status,reason='pending','declared_source_geometry_not_covered_by_final_material'
            except (KeyError,TypeError,ValueError,AttributeError,OverflowError)as error:
                status,reason='pending','declared_source_geometry_cannot_be_verified'
                geometry_validation={'detail':str(error)}
        elif identity in slab_modifiers:
            status,reason='slab_opening_modifier_evidence','source_bound_actual_through_opening_not_material_inside_the_hole'
            geometry_validation={'kind':'validated_subtraction_evidence','opening_ids':sorted(set(slab_modifiers[identity]))}
        elif identity in excluded and config.get('category')=='exclude' and str(config.get('reason')or'').strip() and excluded[identity].get('reason')==config['reason']:
            status, reason = "excluded", excluded[identity].get("reason")
        elif isinstance(declaration, dict) and declaration.get("status") in {"reference", "exclude"} and str(declaration.get("reason", "")).strip():
            status, reason = declaration["status"], declaration["reason"]
        elif config.get("category") in {"reference", "exclude"}:
            status = "reference" if config["category"] == "reference" else "excluded"
            reason = config.get("reason") or ("job_source_reference_decision" if status == "reference" else None)
        elif identity in referenced and referenced[identity].get('reason')=='approved_opening_reference' and referenced[identity].get('id')in opening_ids:
            status,reason='opening_applied_reference','source_bound_opening_host_clearance_audit'
        elif str(entity.get("type", "")).upper() in ANNOTATIONS:
            status, reason = "source_annotation_retained", "non_material_annotation_retained_in_bound_source_document"
        elif not entity.get("type") and not any(entity.get(key) for key in (
                "vertices", "vertices_world", "curve_points", "curve_points_world", "segments",
                "start", "start_world", "end", "end_world", "boundary", "loops", "geometry")):
            status, reason = "non_geometric_source_record_retained", "source_record_has_no_cad_geometry"
        elif air[identity]:
            status, reason, components = "non_material_boundary_reference", "reviewed_facade_bay_air_not_extruded", air[identity]
        else:
            status, reason = "pending", referenced.get(identity, {}).get("reason", "no_final_model_source_disposition")
        if not reason:
            status, reason = "pending", "source_exclusion_requires_explicit_reason"
        if status == "pending":
            missing.append(identity)
        rows.append({"source_entity_id": identity, "source_layer": layer,
                     "original_layer": entity.get("original_layer", entity.get("layer")),
                     "effective_layer": entity.get("effective_layer", entity.get("layer")),
                     "instance_path": entity.get("instance_path"), "source_type": entity.get("type"),
                     "status": status, "reason": reason, "component_or_boundary_ids": sorted(components),
                     "geometry_validation":geometry_validation,
                     "authorized_rc_subtraction_original_ids":sorted(row['id'] for row in removed[identity])})
    return {"schema": "dwg-final-source-accounting-v1", "all_accounted": not missing and not invalid_declarations,
            "source_count": len(rows), "counts": dict(Counter(row["status"] for row in rows)),
            "pending_source_entity_ids": missing, "records": rows,"invalid_declarations":invalid_declarations}


def attach_final_source_accounting(document, payload, decisions, classification=None):
    audit = final_source_accounting(document, payload, decisions, classification)
    payload["final_source_accounting"] = audit
    blockers = []
    blockers.extend(audit['invalid_declarations'])
    if not audit["all_accounted"]:
        blockers.append({"code": "source_disposition_incomplete", "source_entity_ids": audit["pending_source_entity_ids"],
                         "detail": "Every placed source must be modeled, covered, explicitly referenced/excluded, or remain blocking pending."})
    return blockers

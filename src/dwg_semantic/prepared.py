"""Validate source-bound composite contours prepared by a measured CAD solver.

This is the interface between multi-object contour reconstruction and the same
review/approval/native writer used for simple per-entity footprints. It never
repairs geometry, changes tags, or grants approval.
"""
from __future__ import annotations

import copy
import math

from .modeling import SOLID_CATEGORIES, _json_hash, _solid_overlaps


def validate_prepared(payload: dict, document: dict, classification: dict, decisions: dict, *, base_payload=None) -> dict:
    from shapely.geometry import Polygon
    result = copy.deepcopy(payload)
    blockers = list(result.get("blockers", []))
    def block(code, **detail):
        blockers.append({"code": code, **detail})
    if result.get("schema") != "dwg-skp-model-v2" or result.get("units") != "mm":
        block("invalid_prepared_schema_or_units")
    for key, value in (("document_digest", document), ("classification_digest", classification), ("decisions_digest", decisions)):
        if result.get(key) != _json_hash(value):
            block("prepared_input_digest_mismatch", input=key)
    if result.get("source_manifest", {}).get("source_sha256") != document.get("intake", {}).get("source_sha256"):
        block("prepared_source_sha256_mismatch")
    placed = [entity for entity in document.get("entities", []) if entity.get("owner_block") == "*Model_Space"]
    placed += [entity for entity in document.get("resolved_instances", []) if entity.get("root_space") == "*Model_Space"]
    entities = {entity["entity_id"]: entity for entity in placed if entity.get("entity_id")}
    tag_mapping = result.get("tag_mapping", [])
    tags = {item.get("tag") for item in tag_mapping}
    if len(tags) != len(tag_mapping) or any(not isinstance(tag, str) or not tag for tag in tags):
        block("invalid_prepared_tag_mapping")
    tag_sources = {(item.get("tag"), item.get("source_layer")) for item in tag_mapping}
    expected_slabs = None
    if decisions.get("slabs", {}).get("enabled") or any(group.get("category") == "slab" for group in result.get("groups", [])):
        from .slab_modeling import prepare_slabs
        slab_config = copy.deepcopy(decisions.get("slabs", {}))
        slab_config["source_tag_policy"] = decisions.get("source_tag_policy", "effective")
        expected_slabs = prepare_slabs(document, slab_config)
        expected_groups = expected_slabs["groups"]
        actual_groups = [group for group in result.get("groups", []) if group.get("category") == "slab"]
        if _json_hash(expected_groups) != _json_hash(actual_groups):
            block("prepared_slab_geometry_differs_from_decisions")
        if _json_hash(result.get("slab_audit")) != _json_hash(expected_slabs["slab_audit"]):
            block("prepared_slab_audit_differs_from_decisions")
        if _json_hash(result.get("whole_slab_audit")) != _json_hash(expected_slabs.get("whole_slab_audit")):
            block("prepared_whole_slab_audit_differs_from_decisions")
        if _json_hash(result.get("annotation_binding_audit")) != _json_hash(expected_slabs.get("annotation_binding_audit")):
            block("prepared_annotation_binding_audit_differs_from_decisions")
        for issue in expected_slabs["blockers"]:
            if issue not in blockers:
                blockers.append(issue)
        for item in expected_slabs["tag_mapping"]:
            if item not in tag_mapping:
                block("prepared_derived_slab_tag_mapping_missing", tag=item.get("tag"))
    expected_building = {}
    if base_payload is not None and result.get("base_model_digest") != _json_hash(base_payload):
        block("prepared_immutable_base_digest_mismatch")
    base_matches = (base_payload is not None and result.get("base_model_digest") == _json_hash(base_payload)
                    and base_payload.get("source_manifest", {}).get("source_sha256") == document.get("intake", {}).get("source_sha256"))
    cut_config = decisions.get("building_slab_cuts")
    fit_config = decisions.get("wall_under_roof")
    has_cut = cut_config is not None or result.get("base_component_cut_audit") is not None
    has_fit = fit_config is not None or result.get("wall_under_roof_audit") is not None
    if has_cut or has_fit:
        if (has_cut and not isinstance(cut_config, dict)) or (has_fit and not isinstance(fit_config, dict)) or base_payload is None or expected_slabs is None:
            block("prepared_building_cuts_require_trusted_base_and_slab_decisions" if has_cut else "prepared_roof_fit_requires_trusted_base_and_slab_decisions")
        else:
            from .slab_workflow import resolve_building_revisions
            rebuilt_cuts = resolve_building_revisions(base_payload["groups"], expected_slabs["groups"], decisions, document=document,
                opening_records=[*base_payload.get('opening_audit',[]),*base_payload.get('selection_opening_audit',[])])
            if base_matches:
                expected_building = {group['id']: group for group in rebuilt_cuts['groups']}
            actual_building = [group for group in result.get("groups", []) if group.get("category") != "slab"]
            if _json_hash(actual_building) != _json_hash(rebuilt_cuts["groups"]):
                block("prepared_building_cut_geometry_differs_from_immutable_base")
            if _json_hash(result.get("base_component_cut_audit")) != _json_hash(rebuilt_cuts.get("base_component_cut_audit")):
                block("prepared_building_cut_audit_differs_from_recomputed_recipe")
            if _json_hash(result.get("wall_under_roof_audit")) != _json_hash(rebuilt_cuts.get("wall_under_roof_audit")):
                block("prepared_roof_fit_audit_differs_from_recomputed_recipe")
            for issue in rebuilt_cuts["blockers"]:
                if issue not in blockers:
                    blockers.append(issue)
            height_rules = decisions.get("component_height_rules", {})
            for key, rule in rebuilt_cuts["height_rules"].items():
                if height_rules.get(key) != rule:
                    block("prepared_building_cut_height_rule_differs_from_recipe", id=key)
            originals = {group["id"]: group for group in base_payload["groups"]}
            for key, rule in height_rules.items():
                if key in rebuilt_cuts["height_rules"]:
                    continue
                original = originals.get(key)
                if original is None or rule != {"base_z_mm": original["base_z_mm"], "height_mm": original["height_mm"]}:
                    block("prepared_building_cut_unrelated_height_rule", id=key)
    elif base_payload is not None:
        if base_matches:
            expected_building = {group['id']: group for group in base_payload['groups']}
        actual_building = [group for group in result.get("groups", []) if group.get("category") != "slab"]
        if _json_hash(actual_building) != _json_hash(base_payload["groups"]):
            block("prepared_building_geometry_differs_from_immutable_base")
    if _json_hash(result.get("slab_join_policy", {})) != _json_hash(decisions.get("slab_join_policy", {})):
        block("prepared_join_policy_differs_from_decisions")
    if _json_hash(result.get("presentation", {})) != _json_hash(decisions.get("presentation", {})):
        block("prepared_presentation_differs_from_decisions")
    ids = set()
    valid_groups = []
    approved_categories = decisions.get("prepared_categories_by_primary_layer", {})
    for group in result.get("groups", []):
        key = group.get("id")
        if not key or key in ids:
            block("duplicate_or_missing_prepared_group_id", id=key)
        ids.add(key)
        if group.get("category") not in SOLID_CATEGORIES or (group.get("mode") != "solid_extrusion"
                and not(group.get("mode") == "boundary_mesh" and group.get("category") == "slab")):
            block("invalid_prepared_solid_category_or_mode", id=key)
        is_slab = group.get("category") == "slab" and expected_slabs is not None
        # A slab attachment may start from an ordinary layer-mapped approved
        # model, not only a solver-prepared one. The caller verifies its saved
        # base snapshot; trust only an exact unchanged/recomputed group here.
        trusted_building = not is_slab and key in expected_building and _json_hash(group) == _json_hash(expected_building[key])
        if not is_slab and not trusted_building and group.get("category") not in approved_categories.get(group.get("source_layer"), []):
            block("prepared_category_outside_decision_scope", id=key)
        if (group.get("tag"), group.get("source_layer")) not in tag_sources:
            block("prepared_tag_not_bound_to_source_layer", id=key)
        declared = group.get("source_entity_ids", [])
        if not declared or any(source_id not in entities for source_id in declared):
            block("prepared_sources_unresolved", id=key)
        actual_layers = set()
        for source_id in declared:
            entity = entities.get(source_id, {})
            original = str(entity.get("original_layer", entity.get("layer", "")))
            layer = str(entity.get("effective_layer", entity.get("layer", ""))) if original in {"0", "Layer0"} and decisions.get("source_tag_policy", "effective") == "effective" else original
            if layer:
                actual_layers.add(layer)
        if not is_slab and group.get("source_layer") not in actual_layers:
            block("prepared_primary_layer_lacks_source_evidence", id=key, actual_layers=sorted(actual_layers))
        if not actual_layers.issubset(set(group.get("source_layers", []))):
            block("prepared_secondary_source_layers_missing", id=key, actual_layers=sorted(actual_layers))
        if not group.get("evidence"):
            block("prepared_geometry_requires_evidence", id=key)
        try:
            polygon = Polygon(group["outer"], group.get("holes", []))
            height, base = float(group["height_mm"]), float(group.get("base_z_mm", 0))
            if not polygon.is_valid or polygon.is_empty or polygon.area <= .1 or not math.isfinite(height) or not math.isfinite(base) or height <= 0:
                raise ValueError("invalid polygon or vertical dimensions")
            if not all(math.isfinite(float(v)) for ring in [group["outer"], *group.get("holes", [])] for point in ring for v in point):
                raise ValueError("nonfinite coordinates")
            default_height = decisions.get("default_height_mm")
            rules = decisions.get("component_height_rules", {}).get(key, {})
            if rules:
                expected_base, expected_height = rules.get("base_z_mm", 0), rules.get("height_mm")
            elif group.get("semantic_role") == "door_lintel":
                expected_base = decisions.get("opening_height_mm")
                expected_height = default_height - expected_base if default_height is not None and expected_base is not None else None
            else:
                expected_base, expected_height = 0, default_height
            if not is_slab and not trusted_building and (expected_height is None or abs(height-float(expected_height)) > .001 or abs(base-float(expected_base)) > .001):
                block("prepared_height_differs_from_approved_decisions", id=key)
            valid_groups.append(group)
        except (ValueError, KeyError, TypeError, OverflowError) as error:
            block("invalid_prepared_geometry", id=key, detail=str(error))
    if not result.get("groups"):
        block("no_prepared_solids")
    from .modeling import evaluate_slab_joins, validate_slab_payload, validate_building_cut_payload
    for issue in [*validate_slab_payload(result), *validate_building_cut_payload(result)]:
        if issue not in blockers:
            blockers.append(issue)
    join_result = evaluate_slab_joins(valid_groups, policy=result.get("slab_join_policy"))
    for invalid in join_result["invalid_policy"]:
        block("invalid_prepared_slab_join_policy", detail=invalid)
    for overlap in join_result["unresolved_overlaps"]:
        block("overlapping_prepared_solids", **overlap)
    if expected_slabs is not None:
        result["slab_join_audit"] = join_result
    for reference in result.get("references", []):
        if reference.get("id") in ids:
            block("duplicate_prepared_reference_id", id=reference.get("id"))
        ids.add(reference.get("id"))
        if (reference.get("tag"), reference.get("source_layer")) not in tag_sources:
            block("prepared_reference_tag_not_bound", id=reference.get("id"))
        if not reference.get("source_entity_ids") or any(source_id not in entities for source_id in reference["source_entity_ids"]):
            block("prepared_reference_sources_unresolved", id=reference.get("id"))
    result.setdefault("warnings", [])
    result.setdefault("excluded", [])
    from .modeling import _nested_accessory_decision_audit
    semantic_audit, _ = _nested_accessory_decision_audit(classification, decisions, list(entities.values()), result)
    if semantic_audit is not None:
        result["semantic_review_audit"] = semantic_audit
        for request in semantic_audit["requests"]:
            if request["status"] != "resolved":
                block("nested_accessory_decision_requires_confirmation", **request)
                warning = {"code": "nested_accessory_not_modeled_pending_confirmation",
                           "request_id": request["request_id"], "source_entity_ids": request["source_entity_ids"]}
                if warning not in result["warnings"]:
                    result["warnings"].append(warning)
    from .source_accounting import attach_final_source_accounting
    blockers.extend(attach_final_source_accounting(document, result, decisions, classification))
    result["blockers"] = blockers
    from .modeling_standard import active, enforce_standard_payload
    if active(decisions):
        result = enforce_standard_payload(document, result, decisions, classification)
        from .fixed_standard_validation import validate_fixed_payload
        result['blockers'].extend(validate_fixed_payload(result))
    result["prepared_validation"] = {"valid_solid_count": len(valid_groups), "blocker_count": len(blockers), "source_entity_count": len(entities)}
    return result

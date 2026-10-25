"""Source-bound single-shell RC assemblies, connected to the normal planner.

Prisms are retained only as audit/CSG cells. The exported leaf contains the
exterior boundary, not nested slab groups or coincident internal faces.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math

from shapely.geometry import GeometryCollection, Polygon, LineString
from shapely.ops import unary_union

from .mesh_union import boundaries, encoded, interval_rows, parts, shape, topology


MODES = {"prisms", "whole_union", "whole_uniform_thickness"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def merged_provenance(rows):
    """Keep complete per-source original/effective layer, placement and roles."""
    traces={}
    for row in rows:
        for source in row.get('sources',[]):
            identity=source['entity_id']
            previous=traces.get(identity)
            if previous is not None:
                if {key:value for key,value in previous.items()if key!='roles'}!={key:value for key,value in source.items()if key!='roles'}:
                    raise ValueError('Conflicting source provenance cannot be merged into one whole slab')
                previous['roles']=sorted(set(previous.get('roles',[]))|set(source.get('roles',[])))
            else:
                traces[identity]=copy.deepcopy(source)
    fields=('source_entity_ids','annotation_source_ids','source_layers','source_handles','unresolved_source_entity_ids')
    return {**{field:sorted({value for row in rows for value in row.get(field,[])})for field in fields},
            'sources':[traces[key]for key in sorted(traces)]}


def cells_of(group):
    return group.get("construction_cells", []) if group.get("mode") == "boundary_mesh" else [group]


def expand_groups(groups):
    return [copy.deepcopy(cell) for group in groups for cell in cells_of(group)]


def expanded_payload(payload):
    result = dict(payload)
    result["groups"] = expand_groups(payload.get("groups", []))
    return result


def prism_only_payload(payload):
    """Keep legacy saved-prism QA only for systems not exported as meshes."""
    whole_ids = {row["slab_system_id"] for row in payload.get("groups", []) if row.get("mode") == "boundary_mesh"}
    result = copy.deepcopy(payload)
    result["groups"] = [row for row in result.get("groups", []) if row.get("mode") != "boundary_mesh"]
    audit = result.get("slab_audit", {})
    for key in ("systems", "assemblies", "zones", "openings", "step_risers", "step_riser_slices",
                "edge_aligned_replacements", "geometry_precision", "step_riser_joins"):
        if isinstance(audit.get(key), list):
            audit[key] = [row for row in audit[key] if row.get("system_id", row.get("id")) not in whole_ids]
    result.pop("whole_slab_audit", None)
    return result


def _mode(config, system):
    mode = system.get("assembly_mode", config.get("assembly_mode", "prisms"))
    if mode not in MODES:
        raise ValueError("assembly_mode must be prisms, whole_union, or whole_uniform_thickness")
    return mode


def uniform_sections(gross, openings, thickness, side_changes=()):
    """Rebuild a reviewed local-side shell from raw source height regions."""
    regions=[(row,shape(row))for row in gross]
    domain=unary_union([p for _,p in regions])
    cutter=unary_union([shape(row)for row in openings])
    source={row['id']:row for row in gross}
    events=sorted({row['base_z_mm']+offset for row in gross for offset in(0,thickness)})
    sections=[]
    for low,high in zip(events,events[1:]):
        z=(low+high)/2
        raised=unary_union([p for row,p in regions if row['base_z_mm']>z-thickness])
        below=unary_union([p for row,p in regions if row['base_z_mm']>z])
        material=raised.buffer(thickness,join_style='mitre').intersection(domain).difference(below)
        for change in side_changes:
            lower,upper=[source[key]for key in change['source_component_ids']]
            shared=shape(lower).boundary.intersection(shape(upper).boundary)
            lines=[shared]if shared.geom_type=='LineString'else[p for p in getattr(shared,'geoms',[])if p.geom_type=='LineString']
            if not lines or upper['base_z_mm']<=lower['base_z_mm']:
                raise ValueError('Local side change must bind a genuine higher/lower source shared edge')
            # Only this source pair's shared edge is changed. Source holes,
            # outer domain and role-specific cutters remain untouched.
            band=unary_union([line.buffer(thickness,cap_style='flat',join_style='mitre')for line in lines])
            low_band=band.intersection(shape(lower));high_band=band.intersection(shape(upper))
            if z>lower['base_z_mm']+thickness:
                material=material.difference(low_band)
            if lower['base_z_mm']<z<upper['base_z_mm']+thickness:
                material=material.union(high_band)
        material=material.intersection(domain).difference(cutter)
        sections.append({'base_z_mm':low,'top_z_mm':high,'footprint':encoded(material)})
    return sections


def _uniform_cells(document, config, system, original, prism_prepare):
    gross_config = copy.deepcopy(config)
    gross_system = copy.deepcopy(system)
    gross_policy=copy.deepcopy(system.get('step_risers',config.get('step_risers',{})))
    gross_policy['enabled']=False
    # The intermediate heightfield needs no riser solids, but must retain its
    # explicitly authorized common source graph/precision normalization.
    gross_system.update(openings=[], step_risers=gross_policy, assembly_mode="prisms")
    gross_config.update(systems=[gross_system], step_risers=gross_policy, assembly_mode="prisms")
    gross = prism_prepare(document, gross_config)
    if gross["blockers"]:
        raise ValueError("Whole thickness needs resolved gross source sections: " + str(gross["blockers"]))
    source = gross["groups"]
    thicknesses = {round(row["height_mm"], 8) for row in source}
    if len(thicknesses) != 1:
        raise ValueError("Whole uniform mode needs one explicit nominal thickness; conflicting sections are not averaged")
    thickness = thicknesses.pop()
    regions = [(row, shape(row)) for row in source]
    domain = unary_union([polygon for _, polygon in regions])
    if abs(sum(p.area for _, p in regions) - domain.area) > .001:
        raise ValueError("Gross source regions overlap: underside heightfield must be unambiguous")
    openings = [row for row in original["slab_audit"]["openings"]
                if row["system_id"] == system["id"] and row["status"] == "targeted_through_opening"]
    cutter = unary_union([shape(row) for row in openings])
    template = source[0]
    provenance=merged_provenance(source+openings)
    base_recipe={'mode':'whole_uniform_thickness','gross_source_prisms':source,'nominal_thickness_mm':thickness}
    slices=uniform_sections(source,openings,thickness)
    from .whole_slab_qa import nominal_step_probes
    failed=[row for row in nominal_step_probes(base_recipe,slices)if not row['pass']]
    pairs=sorted({tuple(row['source_component_ids'])for row in failed})
    side_changes=[]
    for pair in pairs:
        evidence=[row for row in failed if tuple(row['source_component_ids'])==pair]
        side_changes.append({'source_component_ids':list(pair),'side':'higher_source_region',
            'reason':'default_lower_side_is_narrowed_by_actual_source_hole_or_domain; use_the_source_bound_higher_side',
            'minimum_default_available_width_mm':min(row['actual_transverse_material_mm']for row in evidence),
            'source_edges_mm':[row['source_edge_mm']for row in evidence],
            'hole_and_outer_boundaries_preserved':True})
    if side_changes:
        repaired=uniform_sections(source,openings,thickness,side_changes)
        remaining=[row for row in nominal_step_probes(base_recipe,repaired)if not row['pass']]
        if remaining:
            raise ValueError('Neither source-bound side can preserve nominal thickness without changing a hole or outer edge: '+str(remaining[:5]))
        slices=repaired
    original_sections=uniform_sections(source,openings,thickness)
    additions=[];removals=[]
    for old,new in zip(original_sections,slices):
        before,after=shape(old['footprint']),shape(new['footprint'])
        dz=new['top_z_mm']-new['base_z_mm']
        additions.append(after.difference(before).area*dz)
        removals.append(before.difference(after).area*dz)
    cells=[]
    for section in slices:
        low,high=section['base_z_mm'],section['top_z_mm']
        material=shape(section['footprint'])
        for polygon in parts(material):
            identity = "uniform-cell:" + digest([system["id"], low, high, encoded(polygon)])[:24]
            cell = {**copy.deepcopy(template), "id": identity, "name": identity,
                    **encoded(polygon), "base_z_mm": low, "top_z_mm": high,
                    "height_mm": high-low, "area_mm2": polygon.area, "volume_mm3": polygon.area*(high-low),
                    "slab_zone_id": system["id"] + ":uniform_material", "mode": "solid_extrusion",
                    "evidence": {"method": "source_underside_offset_shell_then_role_specific_through_cuts",
                                 "nominal_thickness_mm": thickness}}
            cell.update(copy.deepcopy(provenance))
            cells.append(cell)
    gross_audit = copy.deepcopy(gross["slab_audit"])
    # This is an input heightfield, not a disconnected final slab delivery.
    # Disabled-riser gap diagnostics remain as evidence but are not material
    # connections required of the intermediate reference surface.
    gross_audit["source_disabled_step_riser_audit"] = gross_audit.get("step_risers", [])
    gross_audit["step_risers"] = []
    return cells, {"mode": "whole_uniform_thickness", "nominal_thickness_mm": thickness,
                   "gross_source_prisms": source, "gross_source_audit": gross_audit,
                   "source_domain": encoded(domain), "source_openings": openings,
                   "construction_rule": "underside_heightfield_miter_shell_clipped_to_original_domain_then_whole_z_openings",
                   "local_side_changes":side_changes,
                   "material_change_ledger":{"baseline":"raw_source_underside_shell_minus_current_role_openings",
                       "added_volume_mm3":sum(additions),"removed_volume_mm3":sum(removals),
                       "net_volume_change_mm3":sum(additions)-sum(removals)},
                   "slices": slices, "nominal_thickness_status": "requires_saved_surface_normal_verification"}


def assemble_whole_slabs(document, config, result, prism_prepare):
    """Transform selected systems, preserving all original decision evidence."""
    choices = []
    for system in config.get("systems", []):
        try:
            mode = _mode(config, system)
        except (ValueError, TypeError) as error:
            result["blockers"].append({"code": "whole_slab_mode_invalid", "system_id": system.get("id"), "detail": str(error)})
            continue
        if mode != "prisms":
            choices.append((system, mode))
    if not choices:
        return result
    source_snapshot = {key: copy.deepcopy(result[key]) for key in ("groups", "tag_mapping", "slab_audit", "blockers", "warnings")}
    records = []
    for system, mode in choices:
        identity = system.get("id")
        originals = [row for row in source_snapshot["groups"] if row.get("slab_system_id") == identity]
        if not originals or result["blockers"]:
            continue
        try:
            cells = copy.deepcopy(originals)
            recipe = {"mode": mode, "source_prism_ids": sorted(row["id"] for row in originals)}
            if mode == "whole_uniform_thickness":
                cells, recipe = _uniform_cells(document, config, system, source_snapshot, prism_prepare)
            intervals, faces = boundaries(cells)
            if mode == "whole_uniform_thickness":
                from .whole_slab_qa import nominal_step_probes
                probes = nominal_step_probes(recipe, [{"base_z_mm":row["base_z_mm"],"top_z_mm":row["top_z_mm"],
                                                       "footprint":encoded(row["geometry"])} for row in intervals])
                recipe["planned_normal_probes"] = probes
                if any(not row["pass"] for row in probes):
                    raise ValueError("Source holes or return geometry cannot preserve nominal transverse thickness on the current side; review a local source-bound side change")
            closure = topology(faces)
            if not closure["all_closed"] or closure["shell_count"] != 1:
                raise ValueError("Whole slab must be one closed connected material shell; split disconnected source systems explicitly")
            projection = unary_union([shape(row) for row in cells])
            if projection.geom_type != "Polygon":
                raise ValueError("One connected whole slab requires a connected XY projection")
            tags = sorted({row["tag"] for row in originals})
            primary = system.get("whole_primary_tag") or (tags[0] if len(tags) == 1 else None)
            if primary not in tags:
                raise ValueError("Multiple original Tags require whole_primary_tag from actual source Tags")
            template = next(row for row in originals if row["tag"] == primary)
            low = min(row["base_z_mm"] for row in cells)
            high = max(row["base_z_mm"]+row["height_mm"] for row in cells)
            volume = sum(row["geometry"].area*(row["top_z_mm"]-row["base_z_mm"]) for row in intervals)
            key = "whole-slab:" + identity + ":" + digest([faces, primary])[:24]
            group = {**copy.deepcopy(template), "id": key, "name": key, "mode": "boundary_mesh",
                     "slab_zone_id": identity + ":whole_material", **encoded(projection),
                     "base_z_mm": low, "top_z_mm": high, "height_mm": high-low,
                     "area_mm2": projection.area, "volume_mm3": volume,
                     "faces": faces, "construction_cells": cells, "assembly_mode": mode,
                     "source_component_ids": sorted(row["id"] for row in originals),
                     **merged_provenance(cells),
                     "original_zone_tags": tags, "tag": primary,
                     "evidence": {"method": "true_exterior_boundary_union", "construction_recipe": recipe,
                                  "primary_tag_rule": "explicit_source_tag" if len(tags)>1 else "single_original_tag",
                                  "internal_partition_faces_removed": True, "child_group_count": 0}}
            record = {"system_id": identity, "component_id": key, "mode": mode,
                      "source_prisms": originals, "construction_cells_sha256": digest(cells),
                      "boundary_sha256": digest(faces), "source_tags": tags, "primary_tag": primary,
                      "recipe": recipe, "topology": closure, "expected_volume_mm3": volume,
                      "z_slices": [{key: value for key, value in row.items() if key != "geometry"}
                                   | {"footprint": encoded(row["geometry"])} for row in intervals]}
            result["groups"] = [row for row in result["groups"] if row.get("slab_system_id") != identity] + [group]
            records.append(record)
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            result["blockers"].append({"code": "whole_slab_assembly_invalid", "system_id": identity, "detail": str(error)})
    result["whole_slab_audit"] = {"schema": "dwg-whole-slab-audit-v1", "systems": records,
                                  "source_prism_payload": source_snapshot, "all_pass": not result["blockers"]}
    result["groups"].sort(key=lambda row: row["id"])
    # Reviews and native metadata use JSON value types, not tuple/int-key
    # implementation details that would change on a save/reopen roundtrip.
    return json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))


def validate_whole_payload(payload):
    """Independently bind faces, actual cells and source audit before writing."""
    mesh_groups = [row for row in payload.get("groups", []) if row.get("mode") == "boundary_mesh"]
    audit = payload.get("whole_slab_audit")
    if not mesh_groups:
        return [] if audit is None else [{"code": "whole_slab_audit_without_material"}]
    errors = []
    try:
        if not isinstance(audit, dict) or audit.get("schema") != "dwg-whole-slab-audit-v1":
            raise ValueError("Whole material requires its complete source-bound audit")
        records = {row["component_id"]: row for row in audit["systems"]}
        if len(records) != len(audit["systems"]) or set(records) != {row["id"] for row in mesh_groups}:
            raise ValueError("Whole source records must match every actual whole component")
        from .modeling import validate_slab_payload
        original = audit["source_prism_payload"]
        legacy_errors = validate_slab_payload(original)
        if legacy_errors:
            raise ValueError("Original source prism audit is invalid: " + str(legacy_errors))
        whole_systems = {row["slab_system_id"] for row in mesh_groups}
        remaining = [row for row in payload.get("groups", []) if row.get("category") == "slab" and row.get("mode") != "boundary_mesh"]
        expected_remaining = [row for row in original["groups"] if row.get("slab_system_id") not in whole_systems]
        if digest(sorted(remaining, key=lambda row:row["id"])) != digest(sorted(expected_remaining, key=lambda row:row["id"])):
            raise ValueError("Unselected prism systems changed during whole assembly")
        for group in mesh_groups:
            record = records[group["id"]]
            cells = group["construction_cells"]
            if group["category"] != "slab" or group["tag"] != record["primary_tag"]:
                raise ValueError("Whole assembly category or primary source Tag changed")
            if digest(cells) != record["construction_cells_sha256"] or digest(group["faces"]) != record["boundary_sha256"]:
                raise ValueError("Whole material changed after review")
            intervals, expected = boundaries(cells)
            if digest(expected) != digest(group["faces"]):
                raise ValueError("Boundary faces are not the exterior union of reviewed cells")
            closed = topology(group["faces"])
            if not closed["all_closed"] or closed["shell_count"] != 1:
                raise ValueError("Whole material is not one closed shell")
            volume = sum(row["geometry"].area*(row["top_z_mm"]-row["base_z_mm"]) for row in intervals)
            if abs(volume-group["volume_mm3"]) > max(.1, volume*1e-8):
                raise ValueError("Whole volume differs from independently unioned source intervals")
            if record["mode"] == "whole_union":
                source = interval_rows(record["source_prisms"])
                if digest([{k:v for k,v in row.items() if k != "geometry"} | {"footprint": encoded(row["geometry"])} for row in source]) != digest(record["z_slices"]):
                    raise ValueError("Whole source material intervals changed")
            else:
                recipe = record["recipe"]
                thickness = recipe["nominal_thickness_mm"]
                gross = recipe["gross_source_prisms"]
                if validate_slab_payload({"groups": gross, "slab_audit": recipe["gross_source_audit"]}):
                    raise ValueError("Whole thickness has invalid gross source sections")
                rebuilt=uniform_sections(gross,recipe['source_openings'],thickness,recipe.get('local_side_changes',[]))
                if len(rebuilt)!=len(record['z_slices']):
                    raise ValueError('Whole thickness lost a source Z interval')
                for section,expected_section in zip(record["z_slices"],rebuilt):
                    if (section['base_z_mm'],section['top_z_mm'])!=(expected_section['base_z_mm'],expected_section['top_z_mm']):
                        raise ValueError('Whole thickness source Z events changed')
                    expected_slice=shape(expected_section['footprint'])
                    if expected_slice.symmetric_difference(shape(section["footprint"])).area > .001:
                        raise ValueError("Whole thickness geometry differs from original source underside and openings")
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
        errors.append({"code": "invalid_whole_slab_material", "detail": str(error)})
    return errors

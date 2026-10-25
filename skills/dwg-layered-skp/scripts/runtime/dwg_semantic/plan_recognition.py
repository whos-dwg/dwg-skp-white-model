"""Automatic, reviewable material/opening recognition from placed CAD leaves.

All algorithms operate on source layers, topology and assembly geometry. Case
decisions may authorize a bounded repair, but never supply expected counts or
precomputed candidate shapes to the recognizer.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math

from shapely import set_precision
from shapely.geometry import GeometryCollection, LineString, Point, Polygon, box
from shapely.ops import unary_union

from .plan_geometry import (SourceIndex, active_plan_ids, collinear_gap_repairs, dangling_endpoints, dimensions,
                            footprint, hatch_profiles, layer_of, matches, parts, shape_of,
                            stable_id, topology_faces,node_linework)


DEFAULTS = {
    "precision_mm": .01, "minimum_material_area_mm2": 5000,
    "maximum_wall_inradius_mm": 375, "maximum_wall_hydraulic_width_mm": 700,
    "facade_terms": ["facade", "curtain", "帷幕", "幕牆", "幕墙"],
    "glazing_thickness_range_mm": [8, 45], "frame_thickness_range_mm": [45, 150],
    "facade_section_length_range_mm": [150, 2500], "collinear_gap_repair_mm": 0,
    "collinear_gap_minimum_source_length_mm": 0,
    "symbol_boundary_search_mm": 100, "door_host_search_mm": 450,
    "maximum_frame_recess_mm": 150,
    "door_assembly_connection_mm":150,
    "stair_mark_boundary_tolerance_mm":0,
}


def _roles(value, rules):
    semantics = rules.get("layer_semantics", {})
    result = set()
    for key, role in (("architectural_wall", "wall"), ("structural", "structural"),
                      ("opening", "opening"), ("curtain", "curtain")):
        if matches(value, semantics.get(key, [])):
            result.add(role)
    if matches(value, semantics.get("wall_finish", [])):
        result.discard("wall")
        result.add("finish")
    if matches(value,semantics.get("reference",["reference","ref","參考","参考"])):
        result.add("reference")
    # Windows and curtain systems overlap lexically; this alone is no conflict.
    if result == {"opening", "curtain"}:
        return {"opening", "curtain"}
    return result


def _candidate(index, shape, category, source_ids, *, prefix="MATERIAL", **extra):
    source_ids = sorted(set(source_ids))
    layers = sorted({index.layers[key] for key in source_ids})
    return {"id": stable_id(prefix, source_ids, shape), "category": category,
            "source_entity_ids": source_ids, "source_layers": layers,
            "footprint": footprint(shape), "area_mm2": shape.area,
            "status": "automatic_plan_candidate_requires_review", **extra}


def layer_conflicts(index, active, rules, settings):
    requests = []
    decisions = settings.get("system_decisions", {})
    for key in sorted(active):
        e = index.by_id[key]
        original = str(e.get("original_layer", e.get("layer", "0")))
        if original in {"0", "Layer0"} or not index.lines_by_id[key]:
            continue
        child = _roles(original, rules)
        if not child:
            continue
        for path, ancestor in reversed(index.ancestors(e)):
            parent_layer = layer_of(ancestor)
            parent = _roles(parent_layer, rules)
            if not parent or child & parent:
                continue
            decision = decisions.get(path)
            requests.append({"id": stable_id("LAYER-CONFLICT", [key, path]), "source_entity_ids": [key],
                             "instance_path": path, "child_original_layer": original,
                             "parent_layer": parent_layer, "child_role_candidates": sorted(child),
                             "parent_role_candidates": sorted(parent),
                             "status": "resolved_by_source_bound_system_decision" if decision else "user_confirmation_required",
                             "decision": decision, "reason": "explicit_parent_child_semantics_conflict"})
            break
    return requests


def _wall_materials(index, active, facade_ids, rules, settings):
    groups = defaultdict(list)
    planes = []
    solid_hatches=[]
    for key in sorted(active-facade_ids):
        entity=index.by_id[key]
        if entity.get('type')!='HATCH' or not entity.get('solid_fill') or index.planes[key]['nonplanar']:
            continue
        profiles,failures=hatch_profiles(entity,index.precision)
        if not failures:
            solid_hatches.extend((key,shape_of(row))for row in profiles)
    for key in sorted(active-facade_ids):
        e = index.by_id[key]
        layer = index.layers[key]
        role = _roles(layer, rules)
        if not index.lines_by_id[key] or not role & {"wall", "structural"} or "finish" in role:
            continue
        if index.planes[key]["nonplanar"]:
            planes.append({"source_entity_ids": [key], "status": "projection_confirmation_required", **index.planes[key]})
            continue
        category = "structural_wall" if "structural" in role else "architectural_wall"
        groups[(category, layer, round(index.planes[key]["plane_z_mm"], 2))].append(key)
    walls, columns, voids, unresolved = [], [], [], []
    for (category, layer, z), ids in sorted(groups.items()):
        if category == "structural_wall":
            faces = [f for f in index.polygons(ids) if f.is_valid and f.area >= settings["minimum_material_area_mm2"]]
            # Nested detail contours belong to one structural material profile.
            # Classify its exterior once instead of counting the inner rectangle
            # as a column and its surrounding strip as an extra wall.
            faces = parts(unary_union([Polygon(f.exterior) for f in faces]))
            gross=unary_union(faces)
            span=max(gross.bounds[2]-gross.bounds[0],gross.bounds[3]-gross.bounds[1])if not gross.is_empty else 1
            material,cavities,pending=topology_faces(index,ids,max_inradius=span*2,
                max_hydraulic_width=span*4,min_area=settings["minimum_material_area_mm2"])
            column_shapes = []
            for face in faces:
                short, long = dimensions(face)
                explicit_column = matches(layer, ["column", "col", "柱"])
                cfg = rules.get("column", {})
                min_width = 100 if explicit_column else cfg.get("minimum_width_mm", 250)
                fill = face.area/(short*long)
                similar = sum(abs(dimensions(other)[0]-short) <= cfg.get("similar_size_tolerance_mm",120)
                              and abs(dimensions(other)[1]-long) <= cfg.get("similar_size_tolerance_mm",120) for other in faces)
                if (min_width <= short <= cfg.get("maximum_width_mm", 1800)
                        and long <= cfg.get("maximum_width_mm", 1800)
                        and long/max(short, 1e-9) <= cfg.get("maximum_aspect_ratio", 2.2)
                        and (explicit_column or (fill >= .7 and similar >= cfg.get("minimum_repetition_count",2)))):
                    section_material=material.intersection(face)
                    fill_ids=set()
                    profile_ids={key for key in ids if index.lines_by_id[key]
                                 and all(face.buffer(index.precision).covers(line)for line in index.lines_by_id[key])}
                    contexts={(str(index.by_id[key].get('source_file','')),str(index.by_id[key].get('instance_path','')))for key in profile_ids}
                    for cavity_row in cavities:
                        cavity=shape_of(cavity_row)
                        if not face.buffer(index.precision).covers(cavity):
                            continue
                        for hatch_id,hatch_shape in solid_hatches:
                            entity=index.by_id[hatch_id]
                            if (str(entity.get('source_file','')),str(entity.get('instance_path','')))not in contexts:
                                continue
                            if abs(index.planes[hatch_id]['plane_z_mm']-z)>1e-6:
                                continue
                            # Only a complete matching material fill resolves a
                            # nested column contour as solid. A hollow column
                            # without this source evidence keeps its void.
                            if hatch_shape.symmetric_difference(cavity).area<=index.precision*max(cavity.length,1):
                                section_material=section_material.union(cavity)
                                fill_ids.add(hatch_id)
                    for section in parts(section_material):
                        refs = sorted(set(index.refs(section.boundary, set(ids)))|profile_ids|fill_ids)
                        columns.append(_candidate(index, section, "column", refs, prefix="COLUMN",
                                                  source_plane_z_mm=z, geometry_evidence="complete_structural_profile_including_curves_and_inner_voids",
                                                  source_solid_fill_entity_ids=sorted(fill_ids),
                                                  inner_profile_material_basis="matching_same_placement_solid_hatch"if fill_ids else "outer_minus_unfilled_inner_contours"))
                        column_shapes.append(section)
            column_union = unary_union(column_shapes)
            for part in parts(material.difference(column_union)):
                refs=index.refs(part.boundary,set(ids))
                walls.append(_candidate(index,part,category,refs,source_plane_z_mm=z,
                                        geometry_evidence="structural_outer_minus_inner_material_profile"))
            for row in cavities:
                cavity=shape_of(row)
                if cavity.intersection(column_union).area>=cavity.area*.99:
                    continue
                voids.append({**row,"id":stable_id("WALL-VOID",row["source_entity_ids"],cavity),
                              "source_layer":layer,"semantic_subrole":"structural_wall_void","source_plane_z_mm":z})
            unresolved.extend(pending)
        else:
            material, cavities, pending = topology_faces(index, ids,
                max_inradius=settings["maximum_wall_inradius_mm"],
                max_hydraulic_width=settings["maximum_wall_hydraulic_width_mm"],
                min_area=settings["minimum_material_area_mm2"])
            subtype = "pipe_shaft_wall" if matches(layer, ["parapet", "shaft", "管井"]) else "partition_wall"
            for part in parts(material):
                refs = index.refs(part.boundary, set(ids))
                walls.append(_candidate(index, part, category, refs, semantic_subrole=subtype,
                                        source_plane_z_mm=z, geometry_evidence="wall_network_outer_minus_inner"))
            for row in cavities:
                voids.append({**row, "id": stable_id("WALL-VOID", row["source_entity_ids"], shape_of(row)),
                              "source_layer": layer, "semantic_subrole": subtype, "source_plane_z_mm": z})
            unresolved.extend(pending)
    # Wall-detail contours nested inside a complete column are accounted for
    # as supporting source, not a second overlapping wall.
    column_union = unary_union([shape_of(row) for row in columns])
    covered = []
    kept = []
    for wall in walls:
        shape = shape_of(wall)
        if not shape.is_empty and shape.intersection(column_union).area >= shape.area*.98:
            covered.append({"source_entity_ids": wall["source_entity_ids"], "reason": "wall_detail_covered_by_complete_column"})
        else:
            kept.append(wall)
    return sorted(kept,key=lambda r:r["id"]),sorted(columns,key=lambda r:r["id"]),sorted(voids,key=lambda r:r["id"]),unresolved,planes,covered


def _facades(index, active, settings):
    groups = defaultdict(set)
    for key in active:
        system = index.named_system(index.by_id[key], settings["facade_terms"])
        if system:
            groups[system].add(key)
    systems, source_ids = [], set()
    for path, ids in sorted(groups.items()):
        system_decision = settings.get("system_decisions", {}).get(path, {})
        if system_decision.get("action") in {"reference", "exclude"}:
            systems.append({"id": stable_id("FACADE-SYSTEM", ids), "instance_path": path,
                            "source_entity_ids": sorted(ids), "sections": [], "unresolved_faces": [],
                            "hatch_profiles": [], "hatch_boundary_failures": [], "repairs": [],
                            "open_source_endpoints_mm": [], "nonplanar_source_entity_ids": [], "counts": {},
                            "status": "source_bound_system_"+system_decision["action"], "decision": system_decision})
            continue
        nonplanar = [key for key in ids if index.lines_by_id[key] and index.planes[key]["nonplanar"]]
        planar = ids-set(nonplanar)
        by_layer = defaultdict(set)
        for key in planar:
            if index.lines_by_id[key]:
                by_layer[(index.layers[key],round(index.planes[key]["plane_z_mm"],2))].add(key)
        sections, other_faces, repairs, profiles, failures = [], [], [], [], []
        all_lines = []
        repaired_network = []
        networks_by_plane=defaultdict(list)
        for (layer,plane_z), layer_ids in sorted(by_layer.items()):
            lines = [line for key in sorted(layer_ids) for line in index.lines_by_id[key]]
            all_lines += lines
            fixes = collinear_gap_repairs(lines, settings["collinear_gap_repair_mm"], index.precision,
                                         minimum_source_length=settings["collinear_gap_minimum_source_length_mm"])
            repaired_network += lines+fixes
            networks_by_plane[plane_z]+=lines+fixes
            for fix in fixes:
                repairs.append({"segment_mm": [list(p) for p in fix.coords], "length_mm": fix.length,
                                "source_entity_ids": index.refs(fix.buffer(index.precision).boundary, layer_ids),
                                "authority": "source_bound_collinear_gap_repair_setting"})
            faces = index.polygons(layer_ids, repairs=fixes)
            frame_min, frame_max = settings["frame_thickness_range_mm"]
            frame_modes = Counter(round(dimensions(f)[0]) for f in faces if f.is_valid and f.area >= 1
                                  and frame_min <= dimensions(f)[0] <= frame_max)
            primary_frame_width = max(frame_modes,key=lambda width:(frame_modes[width],-width)) if frame_modes else None
            for face in faces:
                if not face.is_valid or face.area < 1:
                    continue
                short, long = dimensions(face)
                gmin, gmax = settings["glazing_thickness_range_mm"]
                fmin, fmax = settings["frame_thickness_range_mm"]
                lmin, lmax = settings["facade_section_length_range_mm"]
                role = "opaque_return" if matches(layer, ["wall", "牆", "墙"]) else (
                    "glazing" if gmin <= short <= gmax and lmin <= long <= lmax else
                    "frame" if fmin <= short <= fmax and lmin <= long <= lmax
                    and (matches(layer,["frame","mullion","transom","metal","框料","豎框","横框"])
                         or(primary_frame_width is not None and abs(short-primary_frame_width) <= max(index.precision*2,primary_frame_width*.1))) else None)
                refs = index.refs(face.boundary, layer_ids)
                row = _candidate(index, face, "curtain" if role == "glazing" else "curtain_accessory", refs,
                                 prefix="FACADE", semantic_subrole=role, system_instance_path=path,
                                 source_plane_z_mm=plane_z,
                                 short_dimension_mm=short, long_dimension_mm=long)
                if role:
                    row["closure_basis"] = "shared_source_network_and_authorized_local_repair" if any(face.boundary.distance(LineString(r["segment_mm"])) < index.precision for r in repairs) else "complete_shared_source_network"
                    sections.append(row)
                else:
                    other_faces.append({**row, "reason": "enclosed_facade_air_or_unresolved_detail"})
        returns = unary_union([shape_of(s) for s in sections if s["semantic_subrole"] == "opaque_return"])
        for section in sections:
            if section["semantic_subrole"] != "frame":
                continue
            raw = shape_of(section)
            overlap = raw.intersection(returns).area
            if overlap > index.precision**2:
                trimmed = raw.difference(returns)
                if trimmed.geom_type == "Polygon" and not trimmed.is_empty:
                    section.update(raw_closed_footprint=section["footprint"], footprint=footprint(trimmed),
                                   area_mm2=trimmed.area, return_interface_trim_mm2=overlap)
        for key in sorted(ids):
            e = index.by_id[key]
            if e.get("type") == "HATCH":
                found, missing = hatch_profiles(e, index.precision)
                profiles.extend({**row, "source_entity_ids": [key]} for row in found)
                failures.extend({**row, "source_entity_ids": [key]} for row in missing)
        glass_union=unary_union([shape_of(s)for s in sections if s["semantic_subrole"]=="glazing"])
        frame_union=unary_union([shape_of(s)for s in sections if s["semantic_subrole"]=="frame"])
        hatch_union=unary_union([shape_of(p)for p in profiles])
        bay_air=[];still_unresolved=[]
        for row in other_faces:
            shape=shape_of(row);short,long=dimensions(shape)
            glass_shared=shape.boundary.intersection(glass_union.boundary).length
            frame_shared=shape.boundary.intersection(frame_union.boundary).length
            if long>=short*1.5 and glass_shared>=long*.65 and frame_shared>=short*.75 and shape.intersection(hatch_union).area<=index.precision**2:
                bay_air.append({**row,"semantic_subrole":"facade_bay_air",
                                "reason":"bounded_bay_between_glazing_and_transverse_frames_without_material_hatch",
                                "shared_glazing_boundary_mm":glass_shared,"shared_frame_boundary_mm":frame_shared,
                                "status":"non_material_source_bay_preserved"})
            else:
                still_unresolved.append(row)
        other_faces=still_unresolved
        for row in sections:
            source_ids.update(row["source_entity_ids"])
        from shapely.ops import polygonize
        perimeter_faces=[face for lines in networks_by_plane.values()for face in polygonize(node_linework(lines,index.precision))]
        network_bounds=unary_union(all_lines).bounds if all_lines else(0,0,0,0)
        network_area=(network_bounds[2]-network_bounds[0])*(network_bounds[3]-network_bounds[1])
        large_perimeters=[p for p in perimeter_faces if p.area>=network_area*.3
                          and (p.bounds[2]-p.bounds[0])>=.8*(network_bounds[2]-network_bounds[0])
                          and (p.bounds[3]-p.bounds[1])>=.8*(network_bounds[3]-network_bounds[1])]
        perimeter={"status":"closed_source_perimeter_found"if large_perimeters else"perimeter_closure_requires_review",
                   "closed_face_count":len(large_perimeters),
                   "source_planes_mm":sorted(networks_by_plane),
                   "candidate_outer":footprint(Polygon(max(large_perimeters,key=lambda p:p.area).exterior))if large_perimeters else None}
        systems.append({"id": stable_id("FACADE-SYSTEM", ids), "instance_path": path,
                        "source_entity_ids": sorted(ids), "sections": sorted(sections,key=lambda row:row["id"]),
                        "unresolved_faces": other_faces, "hatch_profiles": profiles,
                        "bay_air_regions":bay_air,
                        "hatch_boundary_failures": failures, "repairs": repairs,
                        "open_source_endpoints_mm": dangling_endpoints(all_lines, index.precision),
                        "perimeter_audit":perimeter,
                        "nonplanar_source_entity_ids": nonplanar,
                        "counts": dict(Counter(s["semantic_subrole"] for s in sections)),
                        "status": "automatic_facade_plan_review"})
    return systems, set().union(*(set(s["source_entity_ids"]) for s in systems)) if systems else set()


def _clear_void_interfaces(voids,walls,columns):
    """Retain original inner contours and the actual clear domain separately.

    A wall from another source family can enter a shaft contour. It remains
    material; the clear cavity follows the difference, preserving both sources
    instead of either erasing the crossing wall or claiming that it is air.
    """
    audit=[]
    for row in voids:
        source=shape_of(row)
        intruders=[]
        for material in walls+columns:
            if row["source_layer"]in material["source_layers"]:
                continue
            shape=shape_of(material)
            overlap=source.intersection(shape).area
            if overlap>.1:
                intruders.append((material,shape,overlap))
        if not intruders:
            continue
        clear=source.difference(unary_union([shape for _,shape,_ in intruders]))
        evidence=[{"component_id":material["id"],"source_entity_ids":material["source_entity_ids"],"overlap_area_mm2":area}
                  for material,_,area in intruders]
        row["source_inner_footprint"]=row["footprint"]
        row["source_inner_area_mm2"]=source.area
        row["material_intrusions"]=evidence
        if clear.geom_type=="Polygon"and not clear.is_empty:
            row.update(footprint=footprint(clear),area_mm2=clear.area,status="clear_domain_excludes_other_source_material")
        else:
            row["status"]="split_or_filled_cavity_requires_material_interface_review"
        audit.append({"void_id":row["id"],"source_inner_area_mm2":source.area,"clear_area_mm2":clear.area,
                      "intruders":evidence,"status":row["status"]})
    return voids,audit


def recognize_plan(document, rules, classification=None, *, decisions=None, slab_decisions=None):
    settings = {**DEFAULTS, **rules.get("plan_recognition", {})}
    if decisions:
        expected = decisions.get("source_sha256")
        actual = document.get("intake", {}).get("source_sha256")
        if not expected or expected != actual:
            raise ValueError("Recognition decisions must match the exact DWG source SHA256")
        supplied=decisions.get("settings",{})
        if not isinstance(supplied,dict)or set(supplied)-set(DEFAULTS):
            raise ValueError("Unknown or invalid recognition settings")
        settings.update(supplied)
        settings["system_decisions"] = decisions.get("system_decisions", {})
        if not isinstance(settings["system_decisions"],dict)or any(not isinstance(row,dict)or row.get("action")not in{"reference","exclude","facade"}or not str(row.get("reason","")).strip()for row in settings["system_decisions"].values()):
            raise ValueError("System choices require an exact instance path, action and reason")
    for key in ("minimum_material_area_mm2","maximum_wall_inradius_mm","maximum_wall_hydraulic_width_mm",
                "symbol_boundary_search_mm","door_host_search_mm","maximum_frame_recess_mm","door_assembly_connection_mm"):
        if isinstance(settings[key],bool)or not isinstance(settings[key],(int,float))or not math.isfinite(settings[key])or settings[key]<=0:
            raise ValueError(key+" must be positive and finite")
    for key in("collinear_gap_repair_mm","collinear_gap_minimum_source_length_mm","stair_mark_boundary_tolerance_mm"):
        if isinstance(settings[key],bool)or not isinstance(settings[key],(int,float))or not math.isfinite(settings[key])or settings[key]<0:
            raise ValueError(key+" must be nonnegative and finite")
    precision = float(settings["precision_mm"])
    if not math.isfinite(precision) or precision <= 0:
        raise ValueError("Recognition precision must be positive and finite")
    index = SourceIndex(document, precision=precision, curve_error=rules.get("geometry", {}).get("max_curve_error_mm", 1.0))
    active = active_plan_ids(index,classification)
    facades, facade_ids = _facades(index, active, settings)
    walls, columns, voids, unresolved, planes, covered = _wall_materials(index, active, facade_ids, rules, settings)
    voids,void_interfaces=_clear_void_interfaces(voids,walls,columns)
    conflicts = layer_conflicts(index, active, rules, settings)
    from .opening_recognition import canonical_door_assemblies, cut_wall_plan, recognize_doors
    source_doors = recognize_doors(index, active, walls, facade_ids, settings)
    doors = canonical_door_assemblies(source_doors,tolerance=max(.1,index.precision*2))
    cut_walls, door_cuts = cut_wall_plan(walls, doors)
    from .slab_recognition import recognize_slab_openings
    from .slabs import prepare_slab_review
    slab_review = prepare_slab_review(document, classification or {}, slab_decisions=slab_decisions,include_automatic_recognition=False)
    slab_openings = recognize_slab_openings(index, active, settings, slab_review,slab_decisions=slab_decisions,
                                           wall_context=walls, door_context=doors)
    for door in doors:
        if door["geometry_method"]!="opposite_frame_crosspieces"or not door.get("clearance_footprint"):
            continue
        shape=shape_of(door["clearance_footprint"])
        shafts=[row for row in slab_openings["elevator_shafts"]if shape.distance(shape_of(row).boundary)<=settings["maximum_frame_recess_mm"]]
        if shafts:
            shaft=min(shafts,key=lambda row:shape.distance(shape_of(row).boundary))
            door["semantic_subrole"]="elevator_portal"
            door["associated_shaft_id"]=shaft["id"]
    result = {"schema": "dwg-automatic-plan-recognition-v1", "source_sha256": document.get("intake", {}).get("source_sha256"),
              "stage": "automatic_source_bound_plan_review", "settings": settings,
              "wall_materials": walls, "columns": columns, "wall_voids": voids,
              "unresolved_wall_faces": unresolved, "nonplanar_wall_sources": planes,
              "column_covered_wall_details": covered, "facade_systems": facades,
              "wall_void_material_interfaces":void_interfaces,
              "layer_conflict_requests": conflicts,
              "source_geometry_errors": index.geometry_errors,
              "precision_audits":index.normalization_audits,
              "door_assemblies": doors, "wall_materials_at_door_level": cut_walls, "door_wall_cut_audit": door_cuts,
              "source_door_assemblies": source_doors,
              "slab_openings": slab_openings,
              "source_accounting": {"placed_leaf_objects": len(index.by_id), "active_plan_objects": len(active),
                                    "outside_plan_domain_objects": len(index.by_id)-len(active)},
              "counts": {"wall_materials": len(walls), "columns": len(columns), "wall_voids": len(voids),
                         "facade_sections": sum(len(s["sections"]) for s in facades),
                         "facade_bay_air_regions":sum(len(s.get("bay_air_regions",[]))for s in facades),
                         "door_assemblies": len(doors), "door_plan_resolved": sum(d["status"]=="plan_geometry_resolved"for d in doors),
                         "source_door_assemblies":len(source_doors),
                         "double_or_multi_leaf_doors": sum(d.get("leaf_count",0)>1 for d in doors),
                         **slab_openings["counts"],
                         "layer_conflicts": len(conflicts), "nonplanar_wall_sources": len(planes)}}
    from .recognition_qa import audit_recognition
    result["qa"]=audit_recognition(result,index,active)
    return result

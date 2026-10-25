from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from dwg_semantic.modeling import (_json_hash, _saved_building_cut_audit, _saved_slab_audit, _solid_overlaps,
                                  entity_key, evaluate_slab_joins, model_entities, prepare_model,
                                  validate_building_cut_payload, validate_slab_payload, write_model)


def rectangle(identity, layer="Walls", x=0, y=0, width=3000, depth=200, **extra):
    return {"entity_id": identity, "handle": identity, "source_file": "source-a.dwg", "type": "LWPOLYLINE", "layer": layer, "closed": True, "vertices": [[x, y], [x + width, y], [x + width, y + depth], [x, y + depth]], **extra}


def decisions(**overrides):
    result = {"layer_mapping": {"Walls": {"category": "architectural_wall", "height_mm": 3200, "footprint_confirmed": True}}, "object_overrides": {}}
    result.update(overrides)
    return result


def synthetic_slab_payload(*, column_top=3200):
    """Synthetic only: floor with a shaft, a 50mm lowered zone and an upper slab."""
    outer = [[0, 0], [6000, 0], [6000, 4000], [0, 4000]]
    shaft = [[2500, 1500], [3500, 1500], [3500, 2500], [2500, 2500]]
    drop = [[500, 500], [1500, 500], [1500, 1500], [500, 1500]]

    def slab(key, system, zone, ring, holes, base, top):
        role = system + "_slab"
        return {"id": key, "name": key, "category": "slab", "mode": "solid_extrusion", "slab_role": role,
                "semantic_role": role, "slab_system_id": system, "slab_zone_id": zone,
                "source_layer": "A-" + system, "source_layers": ["A-" + system], "tag": "A-" + system,
                "source_entity_ids": ["source:" + key], "source_handles": [key], "outer": ring, "holes": holes,
                "base_z_mm": base, "top_z_mm": top, "height_mm": top - base}

    groups = [slab("floor-main", "floor", "floor:base", outer, [shaft, drop], -200, 0),
              slab("floor-drop", "floor", "floor:drop", drop, [], -250, -50),
              slab("overhead-main", "overhead", "overhead:base", outer, [shaft], 3200, 3400),
              {"id": "column", "name": "column", "category": "column", "mode": "solid_extrusion",
               "source_layer": "COLUMNS", "source_layers": ["COLUMNS"], "tag": "COLUMNS", "source_entity_ids": ["source:column"],
               "source_handles": ["column"], "outer": [[2000, 0], [2400, 0], [2400, 400], [2000, 400]], "holes": [],
               "base_z_mm": 0, "height_mm": column_top}]
    assemblies = [{"id": system, "role": system + "_slab", "component_ids": ids,
                   "footprint": {"outer": outer, "holes": [shaft]}, "source_entity_ids": ["source:" + key for key in ids],
                   "expected_area_mm2": 23000000, "expected_volume_mm3": 4600000000}
                  for system, ids in (("floor", ["floor-main", "floor-drop"]), ("overhead", ["overhead-main"]))]
    zones = [{"id": group["slab_zone_id"], "system_id": group["slab_system_id"], "component_ids": [group["id"]],
              "base_z_mm": group["base_z_mm"], "top_z_mm": group["top_z_mm"], "thickness_mm": group["height_mm"],
              "footprint": {"outer": group["outer"], "holes": group["holes"]}, "source_entity_ids": group["source_entity_ids"]}
             for group in groups if group["category"] == "slab"]
    return {"schema": "dwg-skp-model-v2", "units": "mm", "groups": groups, "references": [], "blockers": [], "warnings": [],
            "coordinate_policy": "Synthetic fixture coordinates in mm", "source_manifest": {"kind": "synthetic fixture"},
            "tag_mapping": [{"source_id": "synthetic.dwg", "source_layer": name, "tag": name} for name in ("A-floor", "A-overhead", "COLUMNS")],
            "slab_audit": {"schema": "dwg-slab-audit-v1", "assemblies": assemblies, "zones": zones,
                           "openings": [{"id": system + ":shaft", "system_id": system, "footprint": {"outer": shaft, "holes": []},
                                         "source_entity_ids": ["source:" + system + "-shaft"]} for system in ("floor", "overhead")]}}


def synthetic_slab_join(payload):
    column = next(group for group in payload["groups"] if group["id"] == "column")
    return {"schema": "dwg-slab-join-policy-v1", "joins": [{"component_ids": ["column", "overhead-main"],
             "allowed_contact": {"footprint": {"outer": column["outer"], "holes": []}, "base_z_mm": 3200, "top_z_mm": 3300,
                                 "max_overlap_volume_mm3": 16000000},
             "source_entity_ids": ["source:column", "source:overhead-main"], "evidence": "Synthetic confirmed embedded slab/column junction"}]}


def synthetic_riser_plan(*, diagonal=False, overhead=False):
    """Three plate levels meet at a T, with a shaft cutting another step edge."""
    from dwg_semantic.slab_modeling import prepare_slabs
    from test_slab_modeling import decisions as slab_decisions, document, footprint, system, zone
    x0, xm, x1 = (4000, 5000, 6000) if diagonal else (5000, 5000, 5000)
    top = 4200 if overhead else 0
    rings = [[[0, 0], [x0, 0], [x1, 8000], [0, 8000]],
             [[x0, 0], [10000, 0], [10000, 4000], [xm, 4000]],
             [[xm, 4000], [10000, 4000], [10000, 8000], [x1, 8000]]]
    zones = [zone(identity, footprint={"outer": ring, "holes": []}, top_z_mm=z, source_entity_ids=[source])
             for identity, ring, z, source in zip(("left", "right-low", "right-lower"), rings,
                                                  (top, top - 600, top - 1100), ("zone", "zone2", "boundary"))]
    opening = {"id": "shaft-at-step", "footprint": footprint(3800 if diagonal else 4500, 1000, 1200, 600),
               "source_entity_ids": ["opening"], "applies_to_system_id": "floor"}
    config = slab_decisions(system(role="overhead" if overhead else "floor", default_top_z_mm=top,
                                  zones=zones, openings=[opening], step_risers={"enabled": True, "thickness_mm": 200}))
    config["source_tag_policy"] = "effective"
    doc = document()
    for entity in doc["entities"]:
        entity["owner_block"] = "*Model_Space"
    doc["intake"] = {"source_sha256": "synthetic-riser-source", "source_path": "synthetic-risers.dwg"}
    planned = prepare_slabs(doc, config)
    payload = {"schema": "dwg-skp-model-v2", "units": "mm", "groups": planned["groups"], "references": [],
               "blockers": planned["blockers"], "warnings": planned["warnings"], "tag_mapping": planned["tag_mapping"],
               "slab_audit": planned["slab_audit"], "coordinate_policy": "Synthetic three-level riser fixture in mm",
               "source_manifest": doc["intake"]}
    return doc, config, payload


def synthetic_building_cut_plan():
    from dwg_semantic.slab_contacts import resolve_building_slab_cuts
    doc, slab_config, slab_payload = synthetic_riser_plan(overhead=True)
    originals = []
    for identity, category, layer, x, y, width, depth in (
            ("glass", "curtain", "4win", 1000, 2000, 8000, 50),
            ("metal", "curtain_accessory", "Metal", 1000, 2500, 8000, 20),
            ("untouched-wall", "architectural_wall", "Walls", 1000, 6000, 1000, 200)):
        doc["entities"].append({"entity_id": identity, "handle": identity, "owner_block": "*Model_Space", "type": "LWPOLYLINE",
                                "layer": layer, "original_layer": layer})
        originals.append({"id": identity, "name": identity, "category": category, "mode": "solid_extrusion", "source_layer": layer,
                          "source_layers": [layer], "tag": layer, "source_entity_ids": [identity], "source_handles": [identity],
                          "outer": [[x, y], [x + width, y], [x + width, y + depth], [x, y + depth]], "holes": [],
                          "base_z_mm": 0, "height_mm": 3950, "evidence": "Synthetic source material footprint"})
    cut_config = {"enabled": True, "operation": "subtract_rc_material_only", "target_component_ids": ["glass", "metal"],
                  "target_categories": ["curtain", "curtain_accessory"]}
    cuts = resolve_building_slab_cuts(originals, slab_payload["groups"], cut_config)
    base = {"schema": "dwg-skp-model-v2", "units": "mm", "groups": originals, "references": [], "blockers": [],
            "source_manifest": doc["intake"], "tag_mapping": [{"tag": group["tag"], "source_layer": group["source_layer"]} for group in originals]}
    decisions = {"slabs": slab_config, "building_slab_cuts": cut_config, "source_tag_policy": "effective", "default_height_mm": 3950,
                 "prepared_categories_by_primary_layer": {group["source_layer"]: [group["category"]] for group in originals},
                 "component_height_rules": cuts["height_rules"]}
    payload = {**slab_payload, "groups": [*cuts["groups"], *slab_payload["groups"]], "base_component_cut_audit": cuts["audit"],
               "blockers": [*cuts["blockers"], *slab_payload["blockers"]], "base_model_digest": _json_hash(base),
               "document_digest": _json_hash(doc), "classification_digest": _json_hash({}), "decisions_digest": _json_hash(decisions),
               "tag_mapping": [*base["tag_mapping"], *slab_payload["tag_mapping"]]}
    return doc, decisions, base, payload


class ModelingTests(unittest.TestCase):
    def prepare(self, entities, config=None):
        return prepare_model({"entities": entities}, {}, config or decisions())

    def test_exact_source_tag_and_editable_component(self):
        payload = self.prepare([rectangle("w1", "A+WALL")], decisions(layer_mapping={"A+WALL": {"category": "architectural_wall", "height_mm": 3200, "footprint_confirmed": True}}))
        self.assertEqual([], payload["blockers"])
        group = payload["groups"][0]
        self.assertEqual("A+WALL", group["tag"])
        self.assertEqual(["A+WALL"], group["source_layers"])
        self.assertEqual(3200, group["height_mm"])

    def test_closed_room_requires_footprint_confirmation(self):
        payload = self.prepare([rectangle("room", width=5000, depth=4000)], decisions(layer_mapping={"Walls": {"category": "architectural_wall", "height_mm": 3200, "thickness_mm": 200}}))
        self.assertIn("closed_contour_may_be_room", {item["code"] for item in payload["blockers"]})
        self.assertEqual([], payload["groups"])

    def test_missing_height_and_empty_are_blocking(self):
        payload = self.prepare([rectangle("w")], decisions(layer_mapping={"Walls": {"category": "architectural_wall", "footprint_confirmed": True}}))
        self.assertIn("height_requires_confirmation", {item["code"] for item in payload["blockers"]})
        self.assertTrue(self.prepare([])["blockers"])

    def test_null_review_height_uses_documented_default(self):
        payload = self.prepare([rectangle("w")], decisions(default_height_mm=3500, layer_mapping={"Walls": {"category": "architectural_wall", "height_mm": None, "footprint_confirmed": True}}, object_overrides={"w": {"height_mm": None}}))
        self.assertEqual([], payload["blockers"])
        self.assertEqual(3500, payload["groups"][0]["height_mm"])
        payload = self.prepare([rectangle("w")], decisions(object_overrides={"w": {"height_mm": None}}))
        self.assertEqual(3200, payload["groups"][0]["height_mm"])
        payload = self.prepare([rectangle("w")], decisions(default_height_mm=3500, object_overrides={"w": {"height_mm": 0}}))
        self.assertTrue(payload["blockers"])

    def test_overlapping_wall_and_column_are_blocked(self):
        payload = self.prepare([rectangle("wall"), rectangle("column", x=1000, y=-100, width=400, depth=400)], decisions(object_overrides={"column": {"category": "column", "height_mm": 4000}}))
        self.assertIn("overlapping_solid_components", {item["code"] for item in payload["blockers"]})
        self.assertEqual(["wall", "column"], payload["overlap_audit"][0]["component_ids"])
        self.assertEqual(400 * 200 * 3200, payload["overlap_audit"][0]["overlap_volume_mm3"])

    def test_face_contact_and_stacked_solids_do_not_overlap(self):
        payload = self.prepare([rectangle("wall"), rectangle("touching", y=200), rectangle("above")], decisions(object_overrides={"above": {"base_z_mm": 3200, "height_mm": 1000}}))
        self.assertEqual([], payload["blockers"])
        self.assertEqual([], payload["overlap_audit"])

    def test_nonplanar_source_needs_explicit_projection(self):
        entity = rectangle("tilted", vertices_world=[[0, 0, 0], [3000, 0, 100], [3000, 200, 100], [0, 200, 0]])
        payload = self.prepare([entity])
        self.assertIn("nonhorizontal_source_requires_projection_confirmation", {item["code"] for item in payload["blockers"]})
        payload = self.prepare([entity], decisions(object_overrides={"tilted": {"project_to_xy": True}}))
        self.assertEqual([], payload["blockers"])
        self.assertTrue(payload["groups"][0]["source_nonplanar"])

    def test_source_plane_and_confirmed_floor_datum_are_traceable(self):
        entity = rectangle("w", vertices_world=[[0, 0, 204450], [3000, 0, 204450], [3000, 200, 204450], [0, 200, 204450]])
        payload = self.prepare([entity], decisions(floor_datum_m=204.45))
        self.assertEqual([], payload["blockers"])
        self.assertEqual(204450, payload["groups"][0]["source_plane_z_mm"])
        self.assertEqual(204.45, payload["floor_datum_m"])
        self.assertIn("model Z=0 corresponds to floor H=204.45 m", payload["coordinate_policy"])

    def test_same_layer_can_have_different_categories(self):
        payload = self.prepare([rectangle("wall"), rectangle("column", x=5000, width=400, depth=400)], decisions(object_overrides={"column": {"category": "column", "height_mm": 3600}}))
        self.assertEqual([], payload["blockers"])
        self.assertEqual({"architectural_wall", "column"}, {g["category"] for g in payload["groups"]})
        self.assertEqual({"Walls"}, {g["tag"] for g in payload["groups"]})

    def test_reserved_and_colliding_tags_are_traceable(self):
        payload = self.prepare([rectangle("w1"), rectangle("w2", x=5000, source_file="source-b.dwg"), rectangle("c", "Layer0", x=9000)], decisions(layer_mapping={"Walls": {"category": "architectural_wall", "height_mm": 3200, "footprint_confirmed": True}, "Layer0": {"category": "column", "height_mm": 3200}}))
        self.assertEqual([], payload["blockers"])
        tags = {g["tag"] for g in payload["groups"]}
        self.assertEqual(3, len(tags))
        self.assertNotIn("Layer0", tags)
        self.assertTrue(all(g["source_layer"] in {"Walls", "Layer0"} for g in payload["groups"]))

    def test_multilayer_requires_explicit_primary(self):
        entity = rectangle("multi", source_layers=["Walls", "Other"])
        payload = self.prepare([entity])
        self.assertIn("ambiguous_primary_source_layer", {item["code"] for item in payload["blockers"]})
        payload = self.prepare([entity], decisions(object_overrides={"multi": {"primary_source_layer": "Other"}}))
        self.assertEqual("Other", payload["groups"][0]["source_layer"])
        self.assertEqual(["Walls", "Other"], payload["groups"][0]["source_layers"])

    def test_layer_zero_inherits_effective_with_original_metadata(self):
        entity = rectangle("nested", original_layer="0", effective_layer="Walls", block_layer="Walls", instance_path="B1")
        payload = prepare_model({"resolved_instances": [entity]}, {}, decisions())
        self.assertEqual([], payload["blockers"])
        self.assertEqual("Walls", payload["groups"][0]["tag"])
        self.assertEqual("0", payload["groups"][0]["original_layer"])

    def test_nested_container_and_leaf_share_full_instance_path(self):
        source = "native-vendor.dwg"
        root = {"entity_id": "root", "type": "INSERT", "handle": "85", "layer": "Walls", "source_file": source}
        nested = {"entity_id": "nested", "type": "BLOCK_REFERENCE", "handle": "84", "instance_path": "85/84", "layer": "Walls", "source_file": source}
        leaf = rectangle("leaf", source_file=source, handle="80", instance_path="85/84")
        document = {"entities": [root], "resolved_instances": [nested, leaf]}
        self.assertEqual(["leaf"], [entity["entity_id"] for entity in model_entities(document)])
        payload = prepare_model(document, {}, decisions())
        self.assertEqual([], payload["blockers"])
        self.assertEqual(1, len(payload["groups"]))

    def test_empty_nested_container_is_still_reviewable(self):
        empty = {"entity_id": "empty", "type": "BLOCK_REFERENCE", "handle": "84", "instance_path": "85/84", "layer": "Walls", "source_file": "native-vendor.dwg"}
        document = {"resolved_instances": [empty]}
        self.assertEqual([empty], model_entities(document))
        payload = prepare_model(document, {}, decisions())
        self.assertIn("unresolved_footprint", {item["code"] for item in payload["blockers"]})

    def test_observed_boundaries_not_centerline_offset(self):
        entities = [{"entity_id": "a", "handle": "a", "type": "LINE", "layer": "Walls", "start": [0, 0], "end": [3000, 0]}, {"entity_id": "b", "handle": "b", "type": "LINE", "layer": "Walls", "start": [0, 200], "end": [3000, 200]}]
        payload = self.prepare(entities, decisions(layer_mapping={"Walls": {"category": "architectural_wall", "height_mm": 3200, "thickness_mm": 200}}))
        self.assertEqual([], payload["blockers"])
        self.assertAlmostEqual(600000, payload["groups"][0]["area_mm2"])
        payload = self.prepare(entities[:1], decisions(layer_mapping={"Walls": {"category": "architectural_wall", "height_mm": 3200, "thickness_mm": 200}}))
        self.assertIn("unpaired_boundary", {item["code"] for item in payload["blockers"]})

    def test_unsupported_reference_and_exclusion_are_explicit(self):
        entities = [{"entity_id": "p", "handle": "p", "layer": "Proxy", "type": "PROXY_ENTITY"}, rectangle("wall")]
        payload = self.prepare(entities)
        self.assertTrue(payload["blockers"])
        for category in ["reference", "exclude"]:
            payload = self.prepare(entities, decisions(object_overrides={"p": {"category": category,"reason":"Explicit fixture scope decision"}}))
            self.assertEqual([], payload["blockers"])
            self.assertEqual(1, len(payload["references"] if category == "reference" else payload["excluded"]))

    def test_exclusion_without_a_reason_cannot_be_invented_as_user_confirmation(self):
        payload=self.prepare([rectangle('w')],decisions(object_overrides={'w':{'category':'exclude'}}))
        self.assertIn('source_exclusion_requires_explicit_reason',{row['code']for row in payload['blockers']})
        self.assertFalse(payload['excluded'])

    def test_holes_are_preserved(self):
        entity = rectangle("ring", footprint={"outer": [[0, 0], [4000, 0], [4000, 4000], [0, 4000]], "holes": [[[200, 200], [200, 3800], [3800, 3800], [3800, 200]]]})
        payload = self.prepare([entity])
        self.assertEqual([], payload["blockers"])
        self.assertEqual(1, len(payload["groups"][0]["holes"]))
        self.assertAlmostEqual(3040000, payload["groups"][0]["area_mm2"])

    def test_analytic_bulges_are_not_flattened_to_chords(self):
        entity = {"entity_id": "round", "handle": "round", "source_file": "source-a.dwg", "type": "LWPOLYLINE", "layer": "Walls", "closed": True, "vertices": [{"x": 0, "y": 0, "bulge": 1}, {"x": 1000, "y": 0, "bulge": 1}]}
        payload = self.prepare([entity], decisions(object_overrides={"round": {"category": "column"}}))
        self.assertEqual([], payload["blockers"])
        self.assertGreater(len(payload["groups"][0]["outer"]), 10)
        self.assertGreater(payload["groups"][0]["area_mm2"], 780000)

    def test_raw_arc_as_reference_is_sampled(self):
        arc = {"entity_id": "arc", "handle": "arc", "type": "ARC", "layer": "Arc", "center": {"x": 0, "y": 0}, "radius": 1000, "start_angle_degrees": 0, "end_angle_degrees": 90}
        payload = self.prepare([rectangle("w"), arc], decisions(object_overrides={"arc": {"category": "reference"}}))
        self.assertEqual([], payload["blockers"])
        self.assertGreater(len(payload["references"][0]["segments"]), 5)

    def test_repeated_handle_overrides_require_stable_entity_ids(self):
        first = rectangle("one", handle="AA", x=0, instance_path="B1")
        second = rectangle("two", handle="AA", x=5000, instance_path="B2")
        payload = self.prepare([first, second], decisions(object_overrides={"AA": {"category": "column"}, "two": {"category": "column"}}))
        self.assertEqual(["architectural_wall", "column"], [g["category"] for g in payload["groups"]])

    def test_curtain_accessory_is_a_supported_closed_solid(self):
        payload = self.prepare([rectangle("metal", width=50, depth=50)], decisions(object_overrides={"metal": {"category": "curtain_accessory"}}))
        self.assertEqual([], payload["blockers"])
        self.assertEqual("curtain_accessory", payload["groups"][0]["category"])

    def test_opening_cuts_only_below_approved_height(self):
        config = decisions(object_overrides={"door": {"category": "opening", "height_mm": 2100, "host_ids": ["wall"]}})
        payload = self.prepare([rectangle("wall"), rectangle("door", x=1000, width=900)], config)
        self.assertEqual([], payload["blockers"])
        self.assertEqual(3, len(payload["groups"]))
        lintel = next(group for group in payload["groups"] if "lintel" in group["name"])
        self.assertEqual(2100, lintel["base_z_mm"])
        self.assertEqual(1100, lintel["height_mm"])
        self.assertEqual("Walls", lintel["tag"])

    def test_free_floating_opening_blocks(self):
        payload = self.prepare([rectangle("wall"), rectangle("door", x=10000, width=900)], decisions(object_overrides={"door": {"category": "opening", "height_mm": 2100}}))
        self.assertIn("opening_hosts_unresolved", {item["code"] for item in payload["blockers"]})

    def test_gap_opening_inherits_matching_jamb_hosts(self):
        payload = self.prepare([rectangle("left", width=1000), rectangle("right", x=1900, width=1100), rectangle("door", x=1000, width=900)], decisions(object_overrides={"door": {"category": "opening", "height_mm": 2100, "host_ids": ["left", "right"]}}))
        self.assertEqual([], payload["blockers"])
        lintel = next(group for group in payload["groups"] if group["id"] == "lintel:door")
        self.assertEqual(2100, lintel["base_z_mm"])
        self.assertEqual({"left", "right"}, set(lintel["source_handles"]))

    def test_stable_entity_identity_has_no_concatenation_collision(self):
        self.assertNotEqual(entity_key({"handle": "AB", "source_handle": "C"}), entity_key({"handle": "B", "source_handle": "CA"}))
        self.assertNotEqual(entity_key({"handle": "A", "instance_path": "B1"}), entity_key({"handle": "A", "instance_path": "B2"}))

    def test_payload_is_deterministic_and_inputs_unchanged(self):
        source = {"entities": [rectangle("w")]}
        config = decisions()
        before = copy.deepcopy((source, config))
        self.assertEqual(prepare_model(source, {}, config), prepare_model(source, {}, config))
        self.assertEqual(before, (source, config))

    def test_slab_payload_requires_audit_and_consistent_known_zones(self):
        payload = synthetic_slab_payload()
        self.assertEqual([], validate_slab_payload(payload))
        broken = copy.deepcopy(payload)
        del broken["slab_audit"]
        self.assertEqual("slab_audit_required", validate_slab_payload(broken)[0]["code"])
        with self.assertRaisesRegex(ValueError, "Complete slab audit"):
            write_model(broken, Path(__file__).parent / "should-never-be-written.skp")
        broken = copy.deepcopy(payload)
        broken["slab_audit"]["zones"][0]["top_z_mm"] = None
        self.assertTrue(validate_slab_payload(broken))
        broken = copy.deepcopy(payload)
        broken["slab_audit"]["zones"].pop()
        self.assertIn("slab_component_audit_coverage", {error["code"] for error in validate_slab_payload(broken)})

    def test_slab_parts_footprint_supports_disconnected_material(self):
        payload = synthetic_slab_payload()
        entry = payload["slab_audit"]["assemblies"][0]
        original = entry.pop("footprint")
        entry["footprint"] = {"parts": [original]}
        self.assertEqual([], validate_slab_payload(payload))

    def test_slab_thickness_never_inherits_wall_default_height(self):
        entity = rectangle("floor", width=5000, depth=4000)
        payload = self.prepare([entity], decisions(default_height_mm=3200, layer_mapping={"Walls": {"category": "slab", "slab_role": "floor_slab", "height_mm": None}}))
        self.assertEqual([], payload["groups"])
        self.assertIn("height_requires_confirmation", {error["code"] for error in payload["blockers"]})

    def test_slab_join_exception_is_pair_bounded_and_baseline_stays_strict(self):
        payload = synthetic_slab_payload(column_top=3300)
        groups = payload["groups"]
        self.assertEqual(1, len(_solid_overlaps(groups)))
        self.assertEqual(1, len(evaluate_slab_joins(groups)["unresolved_overlaps"]))
        policy = synthetic_slab_join(payload)
        result = evaluate_slab_joins(groups, policy=policy)
        self.assertEqual([], result["invalid_policy"])
        self.assertEqual([], result["unresolved_overlaps"])
        self.assertEqual(1, len(result["accepted_joins"]))
        self.assertEqual(1, len(_solid_overlaps(groups)))
        changed = copy.deepcopy(groups)
        changed[-1]["height_mm"] = 3400
        self.assertEqual(1, len(evaluate_slab_joins(changed, policy=policy)["unresolved_overlaps"]))

    def test_slab_join_rejects_foreign_evidence_wrong_pairs_and_unbounded_caps(self):
        payload = synthetic_slab_payload(column_top=3300)
        for field, value in (("source_entity_ids", ["not-a-source"]), ("component_ids", ["floor-main", "overhead-main"])):
            policy = synthetic_slab_join(payload)
            policy["joins"][0][field] = value
            result = evaluate_slab_joins(payload["groups"], policy=policy)
            self.assertTrue(result["invalid_policy"])
            self.assertTrue(result["unresolved_overlaps"])
        policy = synthetic_slab_join(payload)
        policy["joins"][0]["allowed_contact"]["max_overlap_volume_mm3"] = float("inf")
        self.assertTrue(evaluate_slab_joins(payload["groups"], policy=policy)["invalid_policy"])

    def test_actual_slab_qa_detects_filled_holes_and_wrong_drop_zone_height(self):
        from shapely.geometry import Polygon
        payload = synthetic_slab_payload()
        saved = [{"id": group["id"], "category": "slab", "footprint": Polygon(group["outer"], group["holes"]),
                  "base_z_mm": group["base_z_mm"], "top_z_mm": group["top_z_mm"],
                  "volume_mm3": Polygon(group["outer"], group["holes"]).area * group["height_mm"]}
                 for group in payload["groups"] if group["category"] == "slab"]
        self.assertTrue(_saved_slab_audit(payload, saved)["all_pass"])
        saved[0]["footprint"] = Polygon(payload["groups"][0]["outer"])
        saved[1]["top_z_mm"] = 0
        audit = _saved_slab_audit(payload, saved)
        self.assertFalse(audit["all_pass"])
        failed = {check["check"] for check in audit["checks"] if not check["pass"]}
        self.assertIn("saved slab opening remains void", failed)
        self.assertIn("saved slab zone bottom top and thickness", failed)

    def test_riser_schema_requires_complete_event_slices_and_original_hosts(self):
        _, _, payload = synthetic_riser_plan()
        self.assertEqual([], payload["blockers"])
        self.assertEqual([], validate_slab_payload(payload))
        risers = [group for group in payload["groups"] if group["slab_role"] == "slab_step_riser"]
        self.assertTrue(risers)
        horizontal_ids = {group["id"] for group in payload["groups"] if group["slab_role"] != "slab_step_riser"}
        self.assertTrue(all(set(entry["component_ids"]) <= horizontal_ids for kind in ("assemblies", "zones") for entry in payload["slab_audit"][kind]))
        broken = copy.deepcopy(payload)
        row = next(entry for entry in broken["slab_audit"]["step_risers"] if entry["status"] == "bridged_gap")
        row["coverage_slices"][0]["base_z_mm"] += 5
        self.assertTrue(validate_slab_payload(broken))
        broken = copy.deepcopy(payload)
        next(group for group in broken["groups"] if group["slab_role"] == "slab_step_riser")["riser_audit_ids"] = []
        self.assertTrue(validate_slab_payload(broken))
        broken = copy.deepcopy(payload)
        next(group for group in broken["groups"] if group["slab_role"] == "slab_step_riser")["source_entity_ids"] = ["boundary"]
        self.assertIn("slab_step_riser_source_provenance_incomplete", {error["code"] for error in validate_slab_payload(broken)})

    def test_actual_riser_qa_catches_slice_crack_width_and_intrusion_into_hole(self):
        from shapely.geometry import Polygon
        _, _, payload = synthetic_riser_plan(diagonal=True)
        self.assertEqual([], payload["blockers"])
        rows = [{"id": group["id"], "category": "slab", "footprint": Polygon(group["outer"], group["holes"]),
                 "base_z_mm": group["base_z_mm"], "top_z_mm": group["top_z_mm"], "volume_mm3": group["volume_mm3"]}
                for group in payload["groups"]]
        self.assertTrue(_saved_slab_audit(payload, rows)["all_pass"])
        riser_ids = {group["id"] for group in payload["groups"] if group["slab_role"] == "slab_step_riser"}
        broken = copy.deepcopy(rows)
        next(row for row in broken if row["id"] in riser_ids)["top_z_mm"] -= 10
        self.assertFalse(_saved_slab_audit(payload, broken)["all_pass"])
        broken = copy.deepcopy(payload)
        next(entry for entry in broken["slab_audit"]["step_risers"] if entry["status"] == "bridged_gap")["horizontal_thickness_mm"] = 20
        self.assertFalse(_saved_slab_audit(broken, rows)["all_pass"])
        broken = copy.deepcopy(rows)
        row = next(row for row in broken if row["id"] in riser_ids)
        row["footprint"] = row["footprint"].union(Polygon([[20000, 20000], [21000, 20000], [21000, 21000], [20000, 21000]]))
        audit = _saved_slab_audit(payload, broken)
        self.assertTrue(any(check["check"] == "saved step riser event union has no extra or missing material"
                            and not check["pass"] for check in audit["checks"]))
        broken = copy.deepcopy(rows)
        hole = payload["slab_audit"]["openings"][0]["footprint"]
        next(row for row in broken if row["id"] in riser_ids)["footprint"] = Polygon(hole["outer"], hole.get("holes", []))
        audit = _saved_slab_audit(payload, broken)
        self.assertFalse(audit["all_pass"])
        self.assertTrue(any(check["check"] == "saved slab opening remains void" and not check["pass"] for check in audit["checks"]))

    def test_small_step_audit_rechecks_existing_material_contact_without_riser_solids(self):
        from dwg_semantic.slab_modeling import prepare_slabs
        from test_slab_modeling import decisions as slab_decisions, document, system, zone
        from shapely.geometry import Polygon
        planned = prepare_slabs(document(), slab_decisions(system(zones=[zone(top_z_mm=-100)],
                               step_risers={"enabled": True, "thickness_mm": 200})))
        payload = {"groups": planned["groups"], "slab_audit": planned["slab_audit"]}
        self.assertEqual([], validate_slab_payload(payload))
        self.assertTrue(all(group["slab_role"] != "slab_step_riser" for group in planned["groups"]))
        self.assertEqual({"already_continuous"}, {row["status"] for row in planned["slab_audit"]["step_risers"]})
        rows = [{"id": group["id"], "category": "slab", "footprint": Polygon(group["outer"], group["holes"]),
                 "base_z_mm": group["base_z_mm"], "top_z_mm": group["top_z_mm"], "volume_mm3": group["volume_mm3"]}
                for group in planned["groups"]]
        self.assertTrue(_saved_slab_audit(payload, rows)["all_pass"])
        row = planned["slab_audit"]["step_risers"][0]
        upper_id = row["upper_horizontal_component_ids"][0]
        next(item for item in rows if item["id"] == upper_id)["base_z_mm"] += 300
        audit = _saved_slab_audit(payload, rows)
        self.assertTrue(any(check["check"] == "saved step already continuous has no positive vertical gap"
                            and not check["pass"] for check in audit["checks"]))

    def test_building_cut_audit_binds_real_cutters_source_tags_and_remaining_material(self):
        from shapely.geometry import Polygon
        _, _, base, payload = synthetic_building_cut_plan()
        self.assertEqual([], payload["blockers"])
        self.assertEqual([], validate_building_cut_payload(payload))
        self.assertEqual(base["groups"][-1], next(group for group in payload["groups"] if group["id"] == "untouched-wall"))
        saved = [{"id": group["id"], "category": group["category"], "tag": group["tag"],
                  "footprint": Polygon(group["outer"], group["holes"]), "base_z_mm": group["base_z_mm"],
                  "top_z_mm": group["base_z_mm"] + group["height_mm"],
                  "volume_mm3": Polygon(group["outer"], group["holes"]).area * group["height_mm"]} for group in payload["groups"]]
        self.assertTrue(_saved_building_cut_audit(payload, saved)["all_pass"])
        broken = copy.deepcopy(payload)
        broken["base_component_cut_audit"]["cutters"][0]["id"] = "invented-cutter"
        self.assertTrue(validate_building_cut_payload(broken))
        broken = copy.deepcopy(payload)
        next(group for group in broken["groups"] if group.get("original_component_id"))["tag"] = "Walls"
        self.assertTrue(validate_building_cut_payload(broken))
        modified = copy.deepcopy(saved)
        cut_ids = {group["id"] for group in payload["groups"] if group.get("original_component_id")}
        next(row for row in modified if row["id"] in cut_ids)["footprint"] = Polygon()
        self.assertFalse(_saved_building_cut_audit(payload, modified)["all_pass"])

    def test_thin_cut_event_is_not_silently_skipped(self):
        from dwg_semantic.slab_contacts import resolve_building_slab_cuts
        from test_slab_contacts import material, rc_material, cut_config
        from shapely.geometry import Polygon
        original, cutter = material(), rc_material(height=.005)
        cut = resolve_building_slab_cuts([original], [cutter], cut_config())
        payload = {"groups": [*cut["groups"], cutter], "base_component_cut_audit": cut["audit"]}
        self.assertEqual([], validate_building_cut_payload(payload))
        saved = [{"id": group["id"], "category": group["category"], "tag": group["tag"],
                  "footprint": Polygon(group["outer"], group["holes"]), "base_z_mm": group["base_z_mm"],
                  "top_z_mm": group["base_z_mm"] + group["height_mm"],
                  "volume_mm3": Polygon(group["outer"], group["holes"]).area * group["height_mm"]} for group in payload["groups"]]
        audit = _saved_building_cut_audit(payload, saved)
        self.assertTrue(audit["all_pass"])
        self.assertTrue(any(0 < section["top_z_mm"] - section["base_z_mm"] < .01 for section in audit["cuts"][0]["actual_z_sections"]))

    @unittest.skipUnless(os.environ.get("DWG_RUN_NATIVE_TESTS") == "1", "Opt in to installed SketchUp native integration")
    def test_native_actual_roof_and_riser_subtraction_retains_only_remaining_facade_material(self):
        _, _, _, payload = synthetic_building_cut_plan()
        self.assertEqual([], payload["blockers"])
        self.assertEqual([], _solid_overlaps(payload["groups"]))
        role_by_id = {group["id"]: group.get("slab_role") for group in payload["groups"]}
        for row in payload["base_component_cut_audit"]["cuts"]:
            self.assertTrue({"overhead_slab", "slab_step_riser"} <= {role_by_id[key] for key in row["rc_cutter_component_ids"]})
            self.assertGreater(row["removed_volume_mm3"], 0)
        test_root = Path(__file__).resolve().parents[1] / "work/modeling-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="synthetic-rc-facade-cut-", dir=test_root) as directory:
            qa = write_model(payload, Path(directory) / "rc-cut-material.skp")
            self.assertTrue(qa["all_pass"], json.dumps(qa, ensure_ascii=False))
            self.assertTrue(qa["base_component_cut_audit"]["all_pass"])
            self.assertEqual(2, len(qa["base_component_cut_audit"]["cuts"]))
            self.assertEqual([], qa["overlap_audit"])
            self.assertTrue(any(check["check"] == "source-bound RC subtraction audit preserved in saved model"
                                and check["pass"] for check in qa["checks"]))
            broken = copy.deepcopy(payload)
            part = next(group for group in broken["groups"] if group.get("original_component_id"))
            center = sum(point[0] for point in part["outer"]) / len(part["outer"])
            part["outer"] = [[center + (point[0] - center) * .9, point[1]] for point in part["outer"]]
            destination = Path(directory) / "refuse-overexcavated.skp"
            with self.assertRaisesRegex(RuntimeError, "RC subtraction"):
                write_model(broken, destination)
            self.assertFalse(destination.exists())

    @unittest.skipUnless(os.environ.get("DWG_RUN_NATIVE_TESTS") == "1", "Opt in to installed SketchUp native integration")
    def test_native_riser_event_slices_roundtrip_right_and_oblique_t_junctions(self):
        test_root = Path(__file__).resolve().parents[1] / "work/modeling-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="synthetic-riser-", dir=test_root) as directory:
            for diagonal, overhead in ((False, False), (True, False), (True, True)):
                with self.subTest(diagonal=diagonal, overhead=overhead):
                    _, _, payload = synthetic_riser_plan(diagonal=diagonal, overhead=overhead)
                    self.assertEqual([], payload["blockers"])
                    self.assertEqual([], validate_slab_payload(payload))
                    self.assertEqual([], _solid_overlaps(payload["groups"]))
                    qa = write_model(payload, Path(directory) / f"riser-{diagonal}-{overhead}.skp")
                    self.assertTrue(qa["all_pass"], json.dumps(qa, ensure_ascii=False))
                    self.assertTrue(qa["slab_audit"]["step_risers"])
                    self.assertTrue(any(entry["coverage_slice_count"] > 1 for entry in qa["slab_audit"]["step_risers"] if entry["status"] == "bridged_gap"))
                    self.assertEqual([], qa["overlap_audit"])
                    if overhead:
                        self.assertTrue(any(check["check"] == "overhead slab parent hidden in default overview" and check["pass"] for check in qa["checks"]))

    @unittest.skipUnless(os.environ.get("DWG_RUN_NATIVE_TESTS") == "1", "Opt in to installed SketchUp native integration")
    def test_native_riser_preserves_distinct_original_zone_tags_at_same_elevation(self):
        from dwg_semantic.slab_modeling import prepare_slabs
        from test_slab_modeling import decisions as slab_decisions, document, footprint, system, zone
        first = zone("a", footprint=footprint(0, 0, 5000, 4000), top_z_mm=-600,
                     tag_binding={"tag": "DROP", "source_layer": "DROP", "kind": "source"})
        second = zone("b", footprint=footprint(5000, 0, 5000, 4000), top_z_mm=-600, source_entity_ids=["zone2"],
                      tag_binding={"tag": "DROP2", "source_layer": "DROP2", "kind": "source"})
        planned = prepare_slabs(document(), slab_decisions(system(zones=[first, second],
                                tag_binding={"tag": "A-OUTLINE", "source_layer": "A-OUTLINE", "kind": "source"},
                                step_risers={"enabled": True, "thickness_mm": 200})))
        self.assertEqual([], planned["blockers"])
        payload = {"schema": "dwg-skp-model-v2", "units": "mm", "groups": planned["groups"], "references": [],
                   "blockers": planned["blockers"], "warnings": planned["warnings"], "tag_mapping": planned["tag_mapping"],
                   "slab_audit": planned["slab_audit"], "coordinate_policy": "Synthetic distinct original slab tags fixture",
                   "source_manifest": {"kind": "synthetic"}}
        self.assertEqual([], validate_slab_payload(payload))
        test_root = Path(__file__).resolve().parents[1] / "work/modeling-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="synthetic-riser-source-tags-", dir=test_root) as directory:
            qa = write_model(payload, Path(directory) / "source-zone-tags.skp")
            self.assertTrue(qa["all_pass"], json.dumps(qa, ensure_ascii=False))
            self.assertEqual({"A-OUTLINE", "DROP", "DROP2"}, {row["tag"] for row in qa["groups"]})
            self.assertEqual({group["id"]: group["tag"] for group in planned["groups"]}, {row["id"]: row["tag"] for row in qa["groups"]})

    @unittest.skipUnless(os.environ.get("DWG_RUN_NATIVE_TESTS") == "1", "Opt in to installed SketchUp native integration")
    def test_native_prepare_slabs_output_roundtrips_holes_zones_and_disconnected_parts(self):
        from dwg_semantic.slab_modeling import prepare_slabs
        from test_slab_modeling import decisions as slab_decisions, document, footprint, system, zone

        shaft = {"id": "shaft", "footprint": footprint(3000, 1500, 2000, 1000),
                 "source_entity_ids": ["opening"], "applies_to_system_id": "floor"}
        cut = {"id": "cut", "footprint": footprint(4000, -1000, 1000, 10000),
               "source_entity_ids": ["opening"], "applies_to_system_id": "floor"}
        configurations = [slab_decisions(system(zones=[zone()], openings=[shaft]),
                                        system("upper", role="overhead", default_top_z_mm=4150)),
                          slab_decisions(system(openings=[cut]))]
        test_root = Path(__file__).resolve().parents[1] / "work/modeling-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="synthetic-slab-planner-", dir=test_root) as directory:
            for index, config in enumerate(configurations):
                with self.subTest(configuration=index):
                    planned = prepare_slabs(document(), config)
                    self.assertEqual([], planned["blockers"])
                    payload = {"schema": "dwg-skp-model-v2", "units": "mm", "groups": planned["groups"],
                               "references": [], "blockers": planned["blockers"], "warnings": planned["warnings"],
                               "tag_mapping": planned["tag_mapping"], "slab_audit": planned["slab_audit"],
                               "coordinate_policy": "Synthetic planner-to-native fixture in mm",
                               "source_manifest": {"kind": "synthetic fixture"}}
                    self.assertEqual([], validate_slab_payload(payload))
                    qa = write_model(payload, Path(directory) / f"planner-{index}.skp")
                    self.assertTrue(qa["all_pass"], json.dumps(qa, ensure_ascii=False))
                    self.assertTrue(qa["slab_audit"]["all_pass"])
                    if index == 0:
                        self.assertEqual(2, len(qa["slab_audit"]["assemblies"]))
                        self.assertEqual(3, len(qa["slab_audit"]["zones"]))
                        self.assertEqual(1, len(qa["slab_audit"]["openings"]))
                    else:
                        self.assertEqual(2, len(planned["groups"]))
                        self.assertEqual(2, len(planned["slab_audit"]["assemblies"][0]["footprint"]["parts"]))
                        self.assertEqual(1, len(qa["slab_audit"]["zones"]))

    @unittest.skipUnless(os.environ.get("DWG_RUN_NATIVE_TESTS") == "1", "Opt in to installed SketchUp native integration")
    def test_native_slab_holes_drop_zone_and_hidden_overhead_parent(self):
        payload = synthetic_slab_payload()
        before = copy.deepcopy(payload)
        test_root = Path(__file__).resolve().parents[1] / "work/modeling-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="synthetic-slab-", dir=test_root) as directory:
            qa = write_model(payload, Path(directory) / "synthetic-slabs.skp")
            self.assertTrue(qa["all_pass"])
            self.assertTrue(qa["slab_audit"]["all_pass"])
            self.assertEqual(3, len(qa["slab_audit"]["zones"]))
            self.assertEqual(2, len(qa["slab_audit"]["openings"]))
            self.assertTrue(any(check["check"] == "overhead slab parent hidden in default overview" and check["pass"] for check in qa["checks"]))
            self.assertEqual({"overview", "plan", "slab_assembly_overview", "slab_assembly_plan"}, {item["view"] for item in qa["previews"]})
            self.assertEqual(payload, before)

    @unittest.skipUnless(os.environ.get("DWG_RUN_NATIVE_TESTS") == "1", "Opt in to installed SketchUp native integration")
    def test_native_slab_join_rechecked_and_oversized_join_refused(self):
        test_root = Path(__file__).resolve().parents[1] / "work/modeling-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="synthetic-slab-join-", dir=test_root) as directory:
            payload = synthetic_slab_payload(column_top=3300)
            payload["slab_join_policy"] = synthetic_slab_join(payload)
            qa = write_model(payload, Path(directory) / "bounded-join.skp")
            self.assertTrue(qa["all_pass"])
            self.assertEqual(1, len(qa["overlap_audit"]))
            self.assertEqual(1, len(qa["slab_join_audit"]["accepted_joins"]))
            payload["slab_join_policy"]["joins"][0]["allowed_contact"]["max_overlap_volume_mm3"] = 1
            destination = Path(directory) / "refused-join.skp"
            with self.assertRaisesRegex(RuntimeError, "overlaps"):
                write_model(payload, destination)
            self.assertFalse(destination.exists())
            actual = json.loads(destination.with_suffix(".skp.qa.json").read_text(encoding="utf-8"))
            self.assertFalse(actual["all_pass"])
            self.assertTrue(actual["slab_join_audit"]["unresolved_overlaps"])

    @unittest.skipUnless(os.environ.get("DWG_RUN_NATIVE_TESTS") == "1", "Opt in to installed SketchUp native integration")
    def test_native_write_reopen_with_opening_voids_tags_and_metadata(self):
        entities = [rectangle("wall"), rectangle("door", x=1000, width=900), rectangle("ring", "Column", x=6000, width=400, depth=400, footprint={"outer": [[6000, 0], [6400, 0], [6400, 400], [6000, 400]], "holes": [[[6100, 100], [6100, 300], [6300, 300], [6300, 100]]]})]
        config = decisions(layer_mapping={**decisions()["layer_mapping"], "Column": {"category": "column", "height_mm": 4000}}, object_overrides={"door": {"category": "opening", "height_mm": 2100, "host_ids": ["wall"]}})
        payload = self.prepare(entities, config)
        self.assertEqual([], payload["blockers"])
        before = copy.deepcopy(payload)
        test_root = Path(__file__).resolve().parents[1] / "work" / "modeling-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="dwg-native-", dir=test_root) as directory:
            destination = Path(directory) / "roundtrip.skp"
            qa = write_model(payload, destination)
            self.assertTrue(qa["all_pass"], json.dumps(qa, ensure_ascii=False))
            self.assertGreater(destination.stat().st_size, 1000)
            self.assertEqual([], qa["overlap_audit"])
            self.assertTrue(Path(qa["diagnostics_path"]).is_file())
            self.assertTrue(all(Path(item["path"]).is_file() for item in qa["previews"]))
            self.assertEqual({"overview", "plan"}, {item["view"] for item in qa["previews"]})
            self.assertEqual(payload, before)
            with self.assertRaises(FileExistsError):
                write_model(payload, destination)

    @unittest.skipUnless(os.environ.get("DWG_RUN_NATIVE_TESTS") == "1", "Opt in to installed SketchUp native integration")
    def test_native_failure_retains_diagnostics_and_no_delivery(self):
        payload = self.prepare([rectangle("a"), rectangle("b", x=1000)])
        self.assertTrue(payload["overlap_audit"])
        # Simulate a malformed hand-authored payload that suppresses its prep
        # blockers. Saved geometry QA must independently catch the overlap.
        payload["blockers"] = []
        test_root = Path(__file__).resolve().parents[1] / "work" / "modeling-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="dwg-native-failure-", dir=test_root) as directory:
            destination = Path(directory) / "failed.skp"
            with self.assertRaisesRegex(RuntimeError, "overlaps"):
                write_model(payload, destination)
            self.assertFalse(destination.exists())
            qa = json.loads(destination.with_suffix(".skp.qa.json").read_text(encoding="utf-8"))
            self.assertFalse(qa["all_pass"])
            self.assertTrue(qa["overlap_audit"])
            self.assertTrue(qa["previews"])


if __name__ == "__main__":
    unittest.main()

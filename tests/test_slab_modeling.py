from __future__ import annotations

import copy
import json
import math
import unittest

from shapely.geometry import Polygon
from shapely.ops import unary_union

from dwg_semantic.slab_modeling import prepare_slabs


def footprint(x=0, y=0, width=10000, depth=8000, holes=None):
    return {"outer": [[x, y], [x + width, y], [x + width, y + depth], [x, y + depth]], "holes": holes or []}


def document():
    return {"intake": {"source_path": "C:/fixtures/vendor-plan.dwg"}, "entities": [{"entity_id": "boundary", "handle": "B", "layer": "A-OUTLINE", "original_layer": "A-OUTLINE", "effective_layer": "WRONG_EFFECTIVE"}, {"entity_id": "level", "handle": "L", "layer": "3dim", "type": "TEXT"}, {"entity_id": "zone", "handle": "Z", "layer": "DROP"}, {"entity_id": "zone2", "handle": "Z2", "layer": "DROP2"}, {"entity_id": "opening", "handle": "O", "layer": "SHAFT"}, {"entity_id": "zone_note", "handle": "ZN", "layer": "LEVEL-NOTES", "type": "TEXT"}, {"entity_id": "layer0", "handle": "ZERO", "original_layer": "0", "effective_layer": "BLOCK-LAYER", "layer": "BLOCK-LAYER"}]}


def system(identity="floor", **changes):
    value = {"id": identity, "role": "floor", "extent": footprint(), "source_entity_ids": ["boundary"], "boundary_source_layers": ["A-OUTLINE"], "annotation_source_ids": ["level"], "default_top_z_mm": 0, "default_thickness_mm": 200, "zones": [], "openings": [], "evidence": "reviewed exterior boundary and RC level note"}
    value.update(changes)
    return value


def decisions(*systems, **changes):
    value = {"enabled": True, "confirmed_scope": True, "derived_tag": {"tag": "RC樓板_推導", "source_layer": "RC樓板_推導", "kind": "derived"}, "floor_datum_m": 204.45, "systems": list(systems or [system()])}
    value.update(changes)
    return value


def zone(identity="drop", **changes):
    value = {"id": identity, "footprint": footprint(1000, 1000, 3000, 2000), "top_z_mm": -60, "source_entity_ids": ["zone"], "annotation_source_ids": ["zone_note"], "method": "constant_thickness"}
    value.update(changes)
    return value


def geometry(data):
    if "parts" in data:
        return unary_union([geometry(part) for part in data["parts"]])
    return Polygon(data["outer"], data.get("holes", []))


def stepped_junction_config():
    return decisions(system(zones=[zone("left", footprint=footprint(0, 0, 5000, 8000), top_z_mm=-600), zone("lower_right", footprint=footprint(5000, 0, 5000, 4000), top_z_mm=-1000, source_entity_ids=["zone2"])], step_risers={"enabled": True, "thickness_mm": 200}))


class SlabModelingTests(unittest.TestCase):
    def prepare(self, config=None):
        return prepare_slabs(document(), config or decisions())

    def codes(self, result):
        return {item["code"] for item in result["blockers"]}

    def test_default_plate_volume_provenance_and_derived_tag(self):
        result = self.prepare()
        self.assertEqual([], result["blockers"])
        group = result["groups"][0]
        self.assertEqual(("slab", "floor_slab", "solid_extrusion"), (group["category"], group["slab_role"], group["mode"]))
        self.assertEqual((-200, 0, 200), (group["base_z_mm"], group["top_z_mm"], group["height_mm"]))
        self.assertEqual(80000000 * 200, group["volume_mm3"])
        self.assertEqual(["A-OUTLINE", "3dim"], sorted(group["source_layers"], reverse=True))
        self.assertEqual({"boundary", "level"}, set(group["source_entity_ids"]))
        self.assertEqual(["level"], group["annotation_source_ids"])
        self.assertEqual("derived", result["tag_mapping"][0]["kind"])
        self.assertEqual("RC樓板_推導", group["source_layer"])

    def test_drop_zone_preserves_default_thickness_and_total_area(self):
        result = self.prepare(decisions(system(zones=[zone()])))
        self.assertEqual([], result["blockers"])
        drop = next(item for item in result["groups"] if item["decision_zone_ids"])
        self.assertEqual((-260, -60, 200), (drop["base_z_mm"], drop["top_z_mm"], drop["height_mm"]))
        self.assertEqual(80000000, sum(group["area_mm2"] for group in result["groups"]))
        self.assertEqual(16000000000, result["slab_audit"]["assemblies"][0]["expected_volume_mm3"])
        self.assertEqual([], result["slab_audit"]["unresolved_overlaps"])
        self.assertTrue({"A-OUTLINE", "3dim", "DROP", "LEVEL-NOTES"}.issubset(drop["source_layers"]))
        base = next(item for item in result["groups"] if not item["decision_zone_ids"])
        self.assertIn("zone", base["source_entity_ids"])
        self.assertIn("zone_cut_boundary", next(source for source in base["sources"] if source["entity_id"] == "zone")["roles"])

    def test_uniform_shift_moves_both_faces(self):
        result = self.prepare(decisions(system(zones=[zone(method="uniform_shift", top_z_mm=None, shift_z_mm=-75)])))
        self.assertEqual([], result["blockers"])
        drop = next(item for item in result["groups"] if item["decision_zone_ids"])
        self.assertEqual((-275, -75, 200), (drop["base_z_mm"], drop["top_z_mm"], drop["height_mm"]))

    def test_uniform_shift_requires_shift_and_cannot_change_thickness(self):
        for changes in [{"shift_z_mm": None}, {"shift_z_mm": -60, "thickness_mm": 300}, {"shift_z_mm": -50, "top_z_mm": -60}]:
            result = self.prepare(decisions(system(zones=[zone(method="uniform_shift", **changes)])))
            self.assertIn("slab_zone_invalid", self.codes(result))

    def test_explicit_bottom_defines_section(self):
        result = self.prepare(decisions(system(zones=[zone(method="explicit_bottom", top_z_mm=-60, bottom_z_mm=-400)])))
        self.assertEqual([], result["blockers"])
        drop = next(item for item in result["groups"] if item["decision_zone_ids"])
        self.assertEqual((-400, -60, 340), (drop["base_z_mm"], drop["top_z_mm"], drop["height_mm"]))

    def test_explicit_bottom_and_thickness_take_priority_over_default_top(self):
        result = self.prepare(decisions(system(zones=[zone(method="explicit_bottom", top_z_mm=None, bottom_z_mm=-300, thickness_mm=250)])))
        self.assertEqual([], result["blockers"])
        drop = next(item for item in result["groups"] if item["decision_zone_ids"])
        self.assertEqual((-300, -50, 250), (drop["base_z_mm"], drop["top_z_mm"], drop["height_mm"]))

    def test_inconsistent_explicit_top_bottom_thickness_blocks(self):
        result = self.prepare(decisions(system(zones=[zone(method="explicit_bottom", bottom_z_mm=-300, thickness_mm=200)])))
        self.assertIn("slab_zone_invalid", self.codes(result))

    def test_missing_sizes_keep_full_2d_extent_and_zone(self):
        result = self.prepare(decisions(system(default_top_z_mm=None, default_thickness_mm=None, zones=[zone(top_z_mm=None)])))
        self.assertEqual([], result["groups"])
        self.assertIn("slab_default_dimensions_required", self.codes(result))
        self.assertIn("slab_zone_dimensions_required", self.codes(result))
        audit = result["slab_audit"]["systems"][0]
        self.assertEqual(80000000, geometry(audit["plan_audit"]["extent"]).area)
        self.assertEqual(6000000, geometry(audit["plan_audit"]["zones"][0]["footprint"]).area)
        self.assertIsNone(audit["plan_audit"]["zones"][0]["top_z_mm"])
        self.assertEqual(200,audit["plan_audit"]["zones"][0]["thickness_mm"])
        missing=next(b for b in result['blockers']if b['code']=='slab_default_dimensions_required')
        self.assertEqual(['top_z_mm'],missing['parameters'])
        self.assertIsNone(audit["expected_volume_mm3"])

    def test_unspecified_or_null_thickness_defaults200_with_provenance_and_without_extra_approval(self):
        for omitted in [True,False]:
            value=system(default_thickness_mm=None)
            if omitted:value.pop('default_thickness_mm')
            config=decisions(value);before=copy.deepcopy(config)
            result=self.prepare(config)
            self.assertEqual([],result['blockers'])
            self.assertEqual([],result['warnings'])
            group=result['groups'][0]
            self.assertEqual((-200,0,200),(group['base_z_mm'],group['top_z_mm'],group['height_mm']))
            evidence=group['evidence']['thickness_provenance']
            self.assertFalse(evidence['source_measurement'])
            self.assertEqual('slab.nominal_thickness_200.v1',evidence['rule_id'])
            self.assertEqual(200,result['slab_audit']['default_assumptions'][0]['value_mm'])
            self.assertEqual(before,config)
            self.assertEqual(result,self.prepare(config))

    def test_explicit_source_or_zone_thickness_precedes_authorized_default(self):
        result=self.prepare(decisions(system(default_thickness_mm=None,zones=[zone(thickness_mm=275)])))
        self.assertEqual([],result['blockers'])
        drop=next(g for g in result['groups']if g['decision_zone_ids'])
        base=next(g for g in result['groups']if not g['decision_zone_ids'])
        self.assertEqual(275,drop['height_mm'])
        self.assertNotIn('thickness_provenance',drop['evidence'])
        self.assertEqual(200,base['height_mm'])
        explicit=self.prepare(decisions(system(default_thickness_mm=180)))
        self.assertEqual(180,explicit['groups'][0]['height_mm'])
        self.assertNotIn('default_assumptions',explicit['slab_audit'])
        self.assertNotIn('thickness_provenance',explicit['groups'][0]['evidence'])

    def test_merged_same_section_buckets_do_not_label_explicit_parts_as_defaulted(self):
        first=zone('implicit')
        second=zone('explicit',footprint=footprint(6000,1000,3000,2000),thickness_mm=200,source_entity_ids=['zone2'])
        result=self.prepare(decisions(system(default_thickness_mm=None,zones=[first,second])))
        self.assertEqual([],result['blockers'])
        from shapely.geometry import Point
        implicit=next(g for g in result['groups']if geometry(g).covers(Point(2000,2000)))
        explicit=next(g for g in result['groups']if geometry(g).covers(Point(7000,2000)))
        self.assertEqual(['implicit'],implicit['evidence']['thickness_provenance']['default_applied_to_decision_zone_ids'])
        self.assertNotIn('thickness_provenance',explicit['evidence'])

    def test_explicit_required_special_case_keeps_thickness_blocker_and_2d_region(self):
        result=self.prepare(decisions(system(default_thickness_mm=None,thickness_policy='explicit_required')))
        self.assertFalse(result['groups'])
        self.assertIn('slab_default_dimensions_required',self.codes(result))
        row=result['slab_audit']['systems'][0]['plan_audit']['base_region']
        self.assertEqual(['thickness_mm'],row['missing_parameters'])
        self.assertEqual(80000000,geometry(row['footprint']).area)
        resolved=self.prepare(decisions(system(default_thickness_mm=250,thickness_policy='explicit_required')))
        self.assertEqual([],resolved['blockers'])
        self.assertEqual(250,resolved['groups'][0]['height_mm'])

    def test_invalid_thickness_policy_blocks_without_falling_back_to200(self):
        result=self.prepare(decisions(system(default_thickness_mm=None,thickness_policy='guess_250')))
        self.assertIn('slab_default_dimensions_invalid',self.codes(result))
        self.assertFalse(result['groups'])

    def test_explicit_top_and_bottom_measure_thickness_instead_of_default200(self):
        result=self.prepare(decisions(system(default_thickness_mm=None,zones=[zone(method='explicit',top_z_mm=-50,bottom_z_mm=-425)])))
        self.assertEqual([],result['blockers'])
        drop=next(g for g in result['groups']if g['decision_zone_ids'])
        self.assertEqual(375,drop['height_mm'])
        self.assertNotIn('thickness_provenance',drop['evidence'])

    def test_uniform_shift_uses_authorized200_only_when_no_explicit_system_thickness(self):
        result=self.prepare(decisions(system(default_thickness_mm=None,zones=[zone(method='uniform_shift',top_z_mm=None,shift_z_mm=-75)])))
        self.assertEqual([],result['blockers'])
        drop=next(g for g in result['groups']if g['decision_zone_ids'])
        self.assertEqual((-275,-75,200),(drop['base_z_mm'],drop['top_z_mm'],drop['height_mm']))
        self.assertIn('thickness_provenance',drop['evidence'])

    def test_explicit_zones_cover_entire_extent_without_default_sizes(self):
        result = self.prepare(decisions(system(default_top_z_mm=None, default_thickness_mm=None, zones=[zone(footprint=footprint(), top_z_mm=-100, thickness_mm=180)])))
        self.assertEqual([], result["blockers"])
        self.assertEqual(180, result["groups"][0]["height_mm"])

    def test_invalid_zero_negative_and_nonfinite_thickness(self):
        for value in [0, -1, math.nan, math.inf, True]:
            result = self.prepare(decisions(system(default_thickness_mm=value)))
            self.assertIn("slab_default_dimensions_invalid", self.codes(result), value)
            self.assertEqual([], result["groups"])

    def test_zone_outside_extent_blocks_and_keeps_original_review_region(self):
        result = self.prepare(decisions(system(zones=[zone(footprint=footprint(-1000, 1000, 3000, 2000))])))
        self.assertIn("slab_zone_outside_extent", self.codes(result))
        row = result["slab_audit"]["systems"][0]["plan_audit"]["zones"][0]
        self.assertEqual(6000000, geometry(row["footprint"]).area)
        self.assertEqual(2000000, row["outside_extent_area_mm2"])
        self.assertEqual(0, result["slab_audit"]["systems"][0]["extra_area_mm2"])

    def test_conflicting_zone_overlap_is_unresolved_without_duplicate_solids(self):
        first = zone("a")
        second = zone("b", footprint=footprint(2000, 1000, 3000, 2000), top_z_mm=-100, source_entity_ids=["zone2"])
        result = self.prepare(decisions(system(zones=[first, second])))
        self.assertIn("slab_zone_sections_conflict", self.codes(result))
        self.assertEqual([], result["slab_audit"]["unresolved_overlaps"])
        self.assertEqual(4000000, result["slab_audit"]["systems"][0]["missing_area_mm2"])

    def test_same_section_zone_overlap_merges_and_keeps_both_sources(self):
        result = self.prepare(decisions(system(zones=[zone("a"), zone("b", footprint=footprint(2000, 1000, 3000, 2000), source_entity_ids=["zone2"])])))
        self.assertEqual([], result["blockers"])
        merged = next(item for item in result["groups"] if item["decision_zone_ids"])
        self.assertEqual(["a", "b"], merged["decision_zone_ids"])
        self.assertTrue({"zone", "zone2"}.issubset(merged["source_entity_ids"]))
        self.assertEqual(8000000, merged["area_mm2"])
        self.assertEqual(80000000, sum(item["area_mm2"] for item in result["groups"]))

    def test_nonorthogonal_polygon_and_source_hole_are_preserved(self):
        shape = {"outer": [[0, 0], [10000, 0], [8000, 8000], [1000, 7000]], "holes": [[[2000, 2000], [3000, 2000], [3000, 3000], [2000, 3000]]]}
        result = self.prepare(decisions(system(extent=shape)))
        self.assertEqual([], result["blockers"])
        self.assertEqual(1, len(result["groups"][0]["holes"]))
        self.assertAlmostEqual(Polygon(shape["outer"], shape["holes"]).area, result["groups"][0]["area_mm2"])

    def test_opening_targets_floor_only_and_cuts_across_drop_boundary(self):
        opening = {"id": "shaft", "footprint": footprint(3000, 1500, 2000, 1000), "source_entity_ids": ["opening"], "applies_to_system_id": "floor"}
        result = self.prepare(decisions(system(zones=[zone()], openings=[opening]), system("upper", role="overhead", default_top_z_mm=4150)))
        self.assertEqual([], result["blockers"])
        floor = [group for group in result["groups"] if group["slab_system_id"] == "floor"]
        upper = [group for group in result["groups"] if group["slab_system_id"] == "upper"]
        self.assertEqual(78000000, sum(group["area_mm2"] for group in floor))
        self.assertEqual(80000000, sum(group["area_mm2"] for group in upper))
        cut = geometry(opening["footprint"])
        self.assertTrue(all(Polygon(group["outer"], group["holes"]).intersection(cut).area == 0 for group in floor))
        self.assertTrue(all("opening" in group["source_entity_ids"] and "SHAFT" in group["source_layers"] for group in floor))

    def test_opening_through_edge_can_split_system_into_multiple_parts(self):
        opening = {"id": "cut", "footprint": footprint(4000, -1000, 1000, 10000), "source_entity_ids": ["opening"], "applies_to_system_id": "floor"}
        result = self.prepare(decisions(system(openings=[opening])))
        self.assertEqual([], result["blockers"])
        self.assertEqual(2, len(result["groups"]))
        self.assertEqual(2, len(result["slab_audit"]["assemblies"][0]["footprint"]["parts"]))
        self.assertEqual(1, len(result["slab_audit"]["zones"]))
        self.assertEqual(2, len(result["slab_audit"]["zones"][0]["component_ids"]))

    def test_opening_requires_existing_target_and_cannot_miss_extent(self):
        for target, shape in [(None, footprint(1000, 1000, 1000, 1000)), ("missing", footprint(1000, 1000, 1000, 1000)), ("floor", footprint(20000, 20000, 1000, 1000))]:
            result = self.prepare(decisions(system(openings=[{"id": "void", "footprint": shape, "source_entity_ids": ["opening"], "applies_to_system_id": target}])))
            self.assertTrue(result["blockers"])
            self.assertEqual(1, len(result["slab_audit"]["openings"]))

    def test_two_systems_cannot_have_positive_volume_overlap(self):
        result = self.prepare(decisions(system(), system("upper", role="overhead", default_top_z_mm=100)))
        self.assertIn("slab_positive_volume_overlap", self.codes(result))
        self.assertEqual(80000000 * 100, result["slab_audit"]["unresolved_overlaps"][0]["volume_mm3"])

    def test_system_face_contact_and_vertical_face_contact_are_valid(self):
        result = self.prepare(decisions(system(), system("upper", role="overhead", default_top_z_mm=200), system("side", extent=footprint(10000, 0, 2000, 8000))))
        self.assertEqual([], result["blockers"])
        self.assertEqual({"horizontal_face", "vertical_face"}, {join["contact"] for join in result["slab_audit"]["accepted_joins"]})

    def test_missing_source_and_scope_confirmation_block(self):
        result = self.prepare(decisions(system(source_entity_ids=["missing"]), confirmed_scope=False))
        self.assertIn("slab_scope_requires_confirmation", self.codes(result))
        self.assertIn("slab_source_entities_missing", self.codes(result))
        self.assertNotIn("missing", result["groups"][0]["source_entity_ids"])

    def test_layer_zero_policy_and_explicit_original_nonzero_are_preserved(self):
        for policy, expected in [("effective", "BLOCK-LAYER"), ("original", "0")]:
            result = self.prepare(decisions(system(source_entity_ids=["boundary", "layer0"]), source_tag_policy=policy))
            self.assertEqual([], result["blockers"])
            self.assertIn(expected, result["groups"][0]["source_layers"])
            self.assertIn("A-OUTLINE", result["groups"][0]["source_layers"])
            self.assertNotIn("WRONG_EFFECTIVE", result["groups"][0]["source_layers"])

    def test_deterministic_input_unchanged_and_unique_audit_membership(self):
        config = decisions(system(zones=[zone("b", footprint=footprint(6000, 1000, 2000, 1000)), zone("a")]))
        source = document()
        before = copy.deepcopy((source, config))
        first = prepare_slabs(source, config)
        config["systems"][0]["zones"].reverse()
        second = prepare_slabs(source, config)
        self.assertEqual(first["groups"], second["groups"])
        self.assertEqual(before[0], source)
        config["systems"][0]["zones"].reverse()
        self.assertEqual(before[1], config)
        for group in first["groups"]:
            self.assertEqual(1, sum(group["id"] in item["component_ids"] for item in first["slab_audit"]["assemblies"]))
            self.assertEqual(1, sum(group["id"] in item["component_ids"] for item in first["slab_audit"]["zones"]))
        json.dumps(first, ensure_ascii=False, allow_nan=False)

    def test_invalid_polygons_records_and_xy_contract_do_not_guess(self):
        shapes = [{"outer": [[0, 0], [10, 10], [0, 10], [10, 0]], "holes": []}, {"outer": [[0, 0, 0], [10, 0, 0], [10, 10, 0]], "holes": []}]
        for shape in shapes:
            result = self.prepare(decisions(system(extent=shape)))
            self.assertIn("slab_extent_invalid", self.codes(result))
            self.assertEqual([], result["groups"])
        result = self.prepare(decisions(system(zones="bad", openings=[None])))
        self.assertTrue({"slab_records_must_be_list", "slab_record_must_be_object"}.issubset(self.codes(result)))

    def test_disabled_slab_scope_has_no_effect_and_wrapper_is_supported(self):
        result = prepare_slabs(document(), {"enabled": False})
        self.assertEqual([], result["groups"])
        self.assertEqual([], result["blockers"])
        self.assertEqual(self.prepare(), prepare_slabs(document(), {"slabs": decisions()}))

    def test_malformed_source_id_list_is_a_blocker_not_an_exception(self):
        result = self.prepare(decisions(system(zones=[zone(source_entity_ids="zone")])))
        self.assertIn("slab_source_id_list_invalid", self.codes(result))

    def test_system_tags_override_global_and_identical_mappings_deduplicate(self):
        upper_tag = {"tag": "上方RC樓板_推導", "source_layer": "上方RC樓板_推導", "kind": "derived"}
        result = self.prepare(decisions(system(), system("upper", role="overhead", default_top_z_mm=4150, derived_tag=upper_tag)))
        self.assertEqual([], result["blockers"])
        self.assertEqual(2, len(result["tag_mapping"]))
        self.assertEqual({"RC樓板_推導", "上方RC樓板_推導"}, {group["tag"] for group in result["groups"]})
        result = self.prepare(decisions(system(), system("upper", role="overhead", default_top_z_mm=4150)))
        self.assertEqual(1, len(result["tag_mapping"]))
        self.assertEqual({"RC樓板_推導"}, {assembly["tag"] for assembly in result["slab_audit"]["assemblies"]})

    def test_all_systems_can_define_tags_without_global_and_conflicts_block(self):
        local = {"tag": "Local", "source_layer": "Local", "kind": "derived"}
        result = self.prepare(decisions(system(derived_tag=local), derived_tag=None))
        self.assertEqual([], result["blockers"])
        for other in [{"tag": "Local", "source_layer": "Other", "kind": "derived"}, {"tag": "Other", "source_layer": "Local", "kind": "derived"}]:
            result = self.prepare(decisions(system(derived_tag=local), system("upper", role="overhead", default_top_z_mm=4150, derived_tag=other)))
            self.assertIn("slab_derived_tag_invalid", self.codes(result))

    def test_reserved_derived_tag_names_are_rejected(self):
        for name in ["Layer0", "Untagged", "0"]:
            result = self.prepare(decisions(system(), derived_tag={"tag": name, "source_layer": name, "kind": "derived"}))
            self.assertIn("slab_derived_tag_invalid", self.codes(result))
            self.assertEqual([], result["groups"])

    def test_invalid_slab_config_cannot_silently_disable_scope(self):
        for config in [None, [], {"slabs": None}]:
            result = prepare_slabs(document(), config)
            self.assertIn("slab_decisions_must_be_object", self.codes(result))
        result = self.prepare(decisions(system(), enabled="true"))
        self.assertIn("slab_enabled_must_be_boolean", self.codes(result))

    def test_source_tag_binding_reuses_exact_original_mapping_shape(self):
        binding = {"tag": "A-OUTLINE", "source_layer": "A-OUTLINE", "kind": "source"}
        result = self.prepare(decisions(system(zones=[zone()]), tag_binding=binding))
        self.assertEqual([], result["blockers"])
        self.assertEqual([{"source_id": "C:/fixtures/vendor-plan.dwg", "source_layer": "A-OUTLINE", "tag": "A-OUTLINE", "reason": "original_name"}], result["tag_mapping"])
        self.assertTrue(all(group["tag"] == "A-OUTLINE" and group["source_layer"] == "A-OUTLINE" and group["tag_kind"] == "source" for group in result["groups"]))
        self.assertTrue(all(item["tag_kind"] == "source" for item in result["slab_audit"]["assemblies"] + result["slab_audit"]["zones"]))
        result = self.prepare(decisions(system(), system("upper", role="overhead", default_top_z_mm=4150), tag_binding=binding, derived_tag=None))
        self.assertEqual([], result["blockers"])
        self.assertEqual(1, len(result["tag_mapping"]))

    def test_system_source_binding_can_override_derived_global(self):
        binding = {"tag": "A-OUTLINE", "source_layer": "A-OUTLINE", "kind": "source"}
        result = self.prepare(decisions(system(tag_binding=binding), system("upper", role="overhead", default_top_z_mm=4150)))
        self.assertEqual([], result["blockers"])
        self.assertEqual(2, len(result["tag_mapping"]))
        self.assertEqual({"source", "derived"}, {group["tag_kind"] for group in result["groups"]})

    def test_source_binding_needs_system_source_evidence_not_other_objects(self):
        for name in ["6jd(底板)", "DROP", "3dim", "WRONG_EFFECTIVE", "Layer0", "Untagged", "0"]:
            result = self.prepare(decisions(system(), tag_binding={"tag": name, "source_layer": name, "kind": "source"}))
            self.assertIn("slab_tag_binding_invalid", self.codes(result), name)
            self.assertEqual([], result["groups"])

    def test_source_binding_cannot_rename_tag_or_conflict_with_derived(self):
        binding = {"tag": "Renamed", "source_layer": "A-OUTLINE", "kind": "source"}
        result = self.prepare(decisions(system(), tag_binding=binding))
        self.assertIn("slab_tag_binding_invalid", self.codes(result))
        binding = {"tag": "A-OUTLINE", "source_layer": "A-OUTLINE", "kind": "source"}
        conflict = {"tag": "A-OUTLINE", "source_layer": "A-OUTLINE", "kind": "derived"}
        result = self.prepare(decisions(system(), system("upper", role="overhead", default_top_z_mm=4150, tag_binding=conflict), tag_binding=binding))
        self.assertIn("slab_tag_binding_invalid", self.codes(result))

    def test_source_binding_layer_zero_effective_policy_and_document_identity(self):
        binding = {"tag": "BLOCK-LAYER", "source_layer": "BLOCK-LAYER", "kind": "source"}
        result = self.prepare(decisions(system(source_entity_ids=["layer0"], tag_binding=binding)))
        self.assertEqual([], result["blockers"])
        result = self.prepare(decisions(system(source_entity_ids=["layer0"], tag_binding=binding), source_tag_policy="original"))
        self.assertIn("slab_tag_binding_invalid", self.codes(result))
        source = document()
        source.pop("intake")
        result = prepare_slabs(source, decisions(system(tag_binding={"tag": "A-OUTLINE", "source_layer": "A-OUTLINE", "kind": "source"})))
        self.assertIn("slab_tag_binding_invalid", self.codes(result))

    def test_step_risers_are_opt_in_and_horizontal_groups_do_not_change(self):
        plain = decisions(system(zones=[zone(top_z_mm=-600)]))
        baseline = self.prepare(plain)
        disabled = copy.deepcopy(plain)
        disabled["step_risers"] = {"enabled": False}
        self.assertEqual(baseline["groups"], self.prepare(disabled)["groups"])
        enabled = copy.deepcopy(plain)
        enabled["step_risers"] = {"enabled": True, "thickness_mm": 200}
        result = self.prepare(enabled)
        self.assertEqual([], result["blockers"])
        horizontal = [group for group in result["groups"] if group["slab_role"] != "slab_step_riser"]
        self.assertEqual(baseline["groups"], horizontal)
        self.assertNotIn("step_risers", baseline["slab_audit"])

    def test_large_step_fills_only_empty_z_gap_with_approved_width(self):
        result = self.prepare(decisions(system(zones=[zone(top_z_mm=-600)], step_risers={"enabled": True, "thickness_mm": 200})))
        self.assertEqual([], result["blockers"])
        riser = next(group for group in result["groups"] if group["slab_role"] == "slab_step_riser")
        self.assertEqual((-600, -200, 400), (riser["base_z_mm"], riser["top_z_mm"], riser["height_mm"]))
        self.assertEqual(200, riser["horizontal_thickness_mm"])
        self.assertEqual("floor_slab", riser["host_slab_role"])
        self.assertTrue({"boundary", "level", "zone", "zone_note"}.issubset(riser["source_entity_ids"]))
        row = result["slab_audit"]["step_risers"][0]
        self.assertEqual("bridged_gap", row["status"])
        self.assertGreater(row["lower_contact_area_mm2"], 0)
        self.assertGreater(row["upper_contact_area_mm2"], 0)
        self.assertEqual([], result["slab_audit"]["unresolved_overlaps"])
        self.assertAlmostEqual(riser["volume_mm3"], row["expected_volume_mm3"])

    def test_small_step_or_exact_thickness_contact_creates_no_duplicate_material(self):
        for drop in [-60, -200]:
            result = self.prepare(decisions(system(zones=[zone(top_z_mm=drop)], step_risers={"enabled": True, "thickness_mm": 200})))
            self.assertEqual([], result["blockers"])
            self.assertFalse(any(group["slab_role"] == "slab_step_riser" for group in result["groups"]))
            self.assertEqual({"already_continuous"}, {row["status"] for row in result["slab_audit"]["step_risers"]})

    def test_step_riser_requires_explicit_valid_width(self):
        for value in [None, 0, -200, math.nan, True]:
            result = self.prepare(decisions(system(zones=[zone(top_z_mm=-600)], step_risers={"enabled": True, "thickness_mm": value})))
            self.assertIn("slab_step_riser_thickness_required", self.codes(result))
            self.assertFalse(any(group["slab_role"] == "slab_step_riser" for group in result["groups"]))

    def test_nonorthogonal_step_uses_real_shared_boundary_and_clips_outer_extent(self):
        shape = footprint(0, 0, 6000, 6000)
        triangle = {"outer": [[0, 0], [6000, 0], [0, 6000]], "holes": []}
        result = self.prepare(decisions(system(extent=shape, zones=[zone(footprint=triangle, top_z_mm=-600)], step_risers={"enabled": True, "thickness_mm": 200})))
        self.assertEqual([], result["blockers"])
        row = result["slab_audit"]["step_risers"][0]
        self.assertAlmostEqual(math.sqrt(6000 ** 2 * 2), row["shared_boundary_length_mm"])
        self.assertEqual(0, geometry(row["requested_footprint"]).difference(geometry(shape)).area)
        self.assertEqual(0, row["lower_uncovered_boundary_length_mm"])
        self.assertEqual(0, row["upper_uncovered_boundary_length_mm"])

    def test_holes_interrupt_step_network_and_stay_open(self):
        opening = {"id": "shaft", "footprint": footprint(4500, 3000, 1000, 2000), "source_entity_ids": ["opening"], "applies_to_system_id": "floor"}
        result = self.prepare(decisions(system(zones=[zone(footprint=footprint(0, 0, 5000, 8000), top_z_mm=-600)], openings=[opening], step_risers={"enabled": True, "thickness_mm": 200})))
        self.assertEqual([], result["blockers"])
        risers = [group for group in result["groups"] if group["slab_role"] == "slab_step_riser"]
        self.assertEqual(2, len(risers))
        self.assertTrue(all(geometry(group).intersection(geometry(opening["footprint"])).area == 0 for group in risers))
        self.assertAlmostEqual(6000, result["slab_audit"]["step_risers"][0]["shared_boundary_length_mm"])

    def test_disconnected_domains_never_get_a_bridge_across_their_gap(self):
        opening = {"id": "cut", "footprint": footprint(4900, -1000, 200, 10000), "source_entity_ids": ["opening"], "applies_to_system_id": "floor"}
        result = self.prepare(decisions(system(zones=[zone(footprint=footprint(0, 0, 4900, 8000), top_z_mm=-600)], openings=[opening], step_risers={"enabled": True, "thickness_mm": 200})))
        self.assertEqual([], result["blockers"])
        self.assertEqual([], result["slab_audit"]["step_risers"])
        self.assertFalse(any(group["slab_role"] == "slab_step_riser" for group in result["groups"]))

    def test_three_level_junction_is_sliced_without_any_volume_overlap(self):
        result = self.prepare(stepped_junction_config())
        self.assertEqual([], result["blockers"])
        self.assertEqual([], result["slab_audit"]["unresolved_overlaps"])
        rows = result["slab_audit"]["step_risers"]
        self.assertEqual({200, 400, 800}, {row["gap_height_mm"] for row in rows})
        self.assertTrue(all(row["status"] == "bridged_gap" for row in rows))
        risers = [group for group in result["groups"] if group["slab_role"] == "slab_step_riser"]
        self.assertEqual(3, len(risers))
        for row in rows:
            slices = row["coverage_slices"]
            self.assertEqual(row["base_z_mm"], slices[0]["base_z_mm"])
            self.assertEqual(row["top_z_mm"], slices[-1]["top_z_mm"])
            self.assertTrue(all(a["top_z_mm"] == b["base_z_mm"] for a, b in zip(slices, slices[1:])))
            for interval in slices:
                self.assertEqual(0, interval["uncovered_area_mm2"])
        for riser in risers:
            inverse = sorted(row["id"] for row in rows if riser["id"] in row["component_ids"])
            self.assertEqual(inverse, riser["riser_audit_ids"])
        self.assertTrue(any(len(group["riser_audit_ids"]) > 1 for group in risers))

    def test_riser_uses_original_system_tag_and_system_override(self):
        binding = {"tag": "A-OUTLINE", "source_layer": "A-OUTLINE", "kind": "source"}
        result = self.prepare(decisions(system(zones=[zone(top_z_mm=-600)], tag_binding=binding, step_risers={"enabled": True, "thickness_mm": 240}), step_risers={"enabled": False}))
        self.assertEqual([], result["blockers"])
        riser = next(group for group in result["groups"] if group["slab_role"] == "slab_step_riser")
        self.assertEqual(("A-OUTLINE", "A-OUTLINE", "source", 240), (riser["tag"], riser["source_layer"], riser["tag_kind"], riser["horizontal_thickness_mm"]))
        first = self.prepare(stepped_junction_config())
        second = self.prepare(stepped_junction_config())
        self.assertEqual(first, second)

    def test_zone_source_tags_preserve_distinct_leaves_at_the_same_elevation(self):
        base_binding = {"tag": "A-OUTLINE", "source_layer": "A-OUTLINE", "kind": "source"}
        first_binding = {"tag": "DROP", "source_layer": "DROP", "kind": "source"}
        second_binding = {"tag": "DROP2", "source_layer": "DROP2", "kind": "source"}
        first = zone("a", footprint=footprint(0, 0, 5000, 4000), top_z_mm=-600, tag_binding=first_binding)
        second = zone("b", footprint=footprint(5000, 0, 5000, 4000), top_z_mm=-600, source_entity_ids=["zone2"], tag_binding=second_binding)
        result = self.prepare(decisions(system(zones=[first, second], tag_binding=base_binding, step_risers={"enabled": True, "thickness_mm": 200})))
        self.assertEqual([], result["blockers"])
        horizontal = [group for group in result["groups"] if group["slab_role"] != "slab_step_riser"]
        self.assertEqual({"A-OUTLINE", "DROP", "DROP2"}, {group["tag"] for group in horizontal})
        self.assertEqual(3, len(horizontal))
        self.assertEqual(3, len(result["tag_mapping"]))
        self.assertEqual(1, len(result["slab_audit"]["assemblies"]))
        self.assertEqual({group["id"]: group["tag"] for group in horizontal}, result["slab_audit"]["assemblies"][0]["component_tags"])
        riser = next(group for group in result["groups"] if group["slab_role"] == "slab_step_riser")
        self.assertEqual("A-OUTLINE", riser["tag"])
        scores = riser["primary_tag_evidence"]["candidates"]
        self.assertEqual({"A-OUTLINE", "DROP", "DROP2"}, {score["tag"] for score in scores})
        self.assertGreater(scores[0]["area_mm2"], scores[1]["area_mm2"])
        self.assertTrue({"boundary", "zone", "zone2"}.issubset(riser["source_entity_ids"]))
        self.assertEqual([], result["slab_audit"]["unresolved_overlaps"])

    def test_zone_source_binding_requires_that_zones_real_layer_evidence(self):
        binding = {"tag": "A-OUTLINE", "source_layer": "A-OUTLINE", "kind": "source"}
        result = self.prepare(decisions(system(zones=[zone(tag_binding=binding)])))
        self.assertIn("slab_zone_invalid", self.codes(result))
        self.assertFalse(any(group["decision_zone_ids"] for group in result["groups"]))
        self.assertGreater(result["slab_audit"]["systems"][0]["missing_area_mm2"], 0)

    def test_overlapping_same_section_zones_with_different_tags_require_resolution(self):
        one = zone("a", tag_binding={"tag": "DROP", "source_layer": "DROP", "kind": "source"})
        two = zone("b", source_entity_ids=["zone2"], tag_binding={"tag": "DROP2", "source_layer": "DROP2", "kind": "source"})
        result = self.prepare(decisions(system(zones=[one, two])))
        self.assertIn("slab_zone_sections_conflict", self.codes(result))


if __name__ == "__main__":
    unittest.main()

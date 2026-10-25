from __future__ import annotations

import copy
import json
import math
import unittest

from shapely.geometry import Polygon
from shapely.ops import unary_union

from dwg_semantic.slab_contacts import resolve_building_slab_cuts


def footprint(x=0, y=0, width=1000, depth=100, holes=None):
    return {"outer": [[x, y], [x + width, y], [x + width, y + depth], [x, y + depth]], "holes": holes or []}


def material(identity="CURTAIN-001", category="curtain", base=0, height=3950, shape=None, **changes):
    """Small original-source prism fixture, also reusable by native tests."""
    value = {"id": identity, "name": identity, "category": category, "mode": "solid_extrusion", "tag": "4win+original", "source_layer": "4win+original", "source_layers": ["4win+original", "2col"], "source_entity_ids": ["dwg:curtain-A", "dwg:curtain-B"], "source_handles": ["A", "B"], "sources": [{"entity_id": "dwg:curtain-A", "role": "source_outline"}], "base_z_mm": base, "height_mm": height, "evidence": {"measurement": "actual source profile", "reviewed": True}, **(shape or footprint())}
    value.update(changes)
    return value


def rc_material(identity="RC-001", role="overhead_slab", base=3000, height=200, shape=None, **changes):
    value = material(identity, "slab", base, height, shape or footprint(400, -100, 400, 300), slab_role=role, semantic_role=role, tag="6jd(顶板)", source_layer="6jd(顶板)", source_layers=["6jd(顶板)"], source_entity_ids=[f"dwg:{identity}"], source_handles=[identity], sources=[{"entity_id": f"dwg:{identity}", "role": "slab_boundary"}])
    value.update(changes)
    return value


def cut_config(*identities, **changes):
    value = {"enabled": True, "operation": "subtract_rc_material_only", "target_component_ids": list(identities or ["CURTAIN-001"]), "target_categories": ["curtain", "curtain_accessory"]}
    value.update(changes)
    return value


def building_slab_cut_fixture():
    """Return fresh (base_groups, slab_groups, config) for compose/native tests.

    One 3950-high curtain crosses a horizontal slab and a lower RC step-riser;
    a nearby structural wall is deliberately outside the cut scope.
    """
    base = [material(), material("WALL-KEEP", "architectural_wall", shape=footprint(1500, 0, 300, 100), height=3950)]
    slabs = [rc_material(), rc_material("RISER-001", "slab_step_riser", base=2500, height=500, shape=footprint(500, -100, 200, 300), host_slab_role="overhead_slab")]
    return base, slabs, cut_config()


def geometry(value):
    if "parts" in value:
        return unary_union([geometry(part) for part in value["parts"]])
    return Polygon(value["outer"], value.get("holes", []))


def volume(groups):
    return sum(geometry(group).area * group["height_mm"] for group in groups)


class SlabContactsTests(unittest.TestCase):
    def resolve(self, base=None, slabs=None, config=None):
        return resolve_building_slab_cuts(base if base is not None else [material()], slabs if slabs is not None else [rc_material()], config if config is not None else cut_config())

    def codes(self, result):
        return {item["code"] for item in result["blockers"]}

    def assert_exact_material(self, originals, slabs, result):
        """Independent per-Z set difference; tests added/over-cut material too."""
        self.assertEqual([], result["blockers"])
        for original in originals:
            targets = [group for group in result["groups"] if group.get("original_component_id", group["id"]) == original["id"]]
            obase, otop = original["base_z_mm"], original["base_z_mm"] + original["height_mm"]
            events = sorted({obase, otop, *(max(obase, min(otop, group["base_z_mm"])) for group in slabs + targets), *(max(obase, min(otop, group["base_z_mm"] + group["height_mm"])) for group in slabs + targets)})
            for bottom, top in zip(events, events[1:]):
                if top <= bottom:
                    continue
                z = (bottom + top) / 2
                cutters = [geometry(group) for group in slabs if group["base_z_mm"] < z < group["base_z_mm"] + group["height_mm"]]
                expected = geometry(original).difference(unary_union(cutters))
                actual = unary_union([geometry(group) for group in targets if group["base_z_mm"] < z < group["base_z_mm"] + group["height_mm"]])
                self.assertLess(actual.symmetric_difference(expected).area, 1e-7)
            for group in targets:
                self.assertGreater(group["height_mm"], 0)
                self.assertGreater(geometry(group).area, 0)
                self.assertGreaterEqual(group["base_z_mm"], obase)
                self.assertLessEqual(group["base_z_mm"] + group["height_mm"], otop)
                self.assertLess(geometry(group).difference(geometry(original)).area, 1e-7)
            for number, left in enumerate(targets):
                for right in targets[number + 1:]:
                    dz = min(left["base_z_mm"] + left["height_mm"], right["base_z_mm"] + right["height_mm"]) - max(left["base_z_mm"], right["base_z_mm"])
                    self.assertLessEqual(max(0, dz) * geometry(left).intersection(geometry(right)).area, 1e-7)

    def test_localized_rc_material_and_riser_are_exactly_subtracted(self):
        base, slabs, config = building_slab_cut_fixture()
        result = self.resolve(base, slabs, config)
        self.assert_exact_material([base[0]], slabs, result)
        self.assertEqual(7, len(result["groups"]))  # Six edited prisms, one original wall.
        cut = result["audit"]["cuts"][0]
        self.assertEqual(18000000, cut["removed_volume_mm3"])
        self.assertEqual(395000000 - 18000000, cut["remaining_volume_mm3"])
        self.assertEqual([0, 2500, 3000, 3200], [row["base_z_mm"] for row in cut["z_slices"]])
        self.assertEqual([2500, 3000, 3200, 3950], [row["top_z_mm"] for row in cut["z_slices"]])
        self.assertEqual([], cut["z_slices"][0]["active_cutter_ids"])
        self.assertEqual([], cut["z_slices"][-1]["active_cutter_ids"])
        self.assertEqual(base[1], result["groups"][-1])

    def test_primary_tag_and_source_trace_remain_original(self):
        original = material()
        result = self.resolve([original])
        for group in result["groups"]:
            for key in ("tag", "source_layer", "source_layers", "source_entity_ids", "source_handles", "sources", "evidence", "category"):
                self.assertEqual(original[key], group[key])
            self.assertEqual((0, 3950, 3950), (group["nominal_base_z_mm"], group["nominal_top_z_mm"], group["nominal_height_mm"]))
            self.assertEqual(["dwg:RC-001"], group["modifier_source_entity_ids"])
            self.assertNotIn("dwg:RC-001", group["source_entity_ids"])
            self.assertEqual("rc_material_subtraction", group["geometry_modifier_provenance"]["typed_sources"][0]["role"])
            self.assertEqual({"base_z_mm": group["base_z_mm"], "height_mm": group["height_mm"]}, result["height_rules"][group["id"]])

    def test_inputs_and_nested_source_metadata_are_never_mutated(self):
        base, slabs, config = building_slab_cut_fixture()
        snapshots = copy.deepcopy((base, slabs, config))
        result = self.resolve(base, slabs, config)
        result["groups"][0]["evidence"]["changed"] = True
        result["audit"]["original_groups"][0]["source_entity_ids"].append("MUTATION")
        self.assertEqual(snapshots, (base, slabs, config))

    def test_disabled_returns_field_exact_base_without_evaluating_scope(self):
        base = [material(), {"id": "reference", "mode": "reference", "arbitrary": [1, {"a": 2}]}]
        result = self.resolve(base, [{"bad": "not geometry"}], {"enabled": False, "operation": "anything"})
        self.assertEqual(base, result["groups"])
        self.assertEqual([], result["blockers"])
        self.assertFalse(result["audit"]["enabled"])
        self.assertEqual([], result["audit"]["cuts"])
        self.assertEqual({}, result["height_rules"])

    def test_boundary_or_top_face_contact_is_untouched(self):
        original = material()
        for slab in [rc_material(base=3950), rc_material(shape=footprint(1000, 0, 500, 100))]:
            with self.subTest(slab=slab):
                result = self.resolve([original], [slab])
                self.assertEqual([original], result["groups"])
                self.assertEqual({}, result["height_rules"])
                cut = result["audit"]["cuts"][0]
                self.assertEqual("untouched_no_positive_intersection", cut["status"])
                self.assertEqual(1, len(cut["z_slices"]))
                self.assertEqual([], cut["z_slices"][0]["active_cutter_ids"])
                self.assertEqual([], result["audit"]["cutters"])

    def test_non_target_reference_and_wall_keep_all_fields(self):
        base = [material(), material("WALL", "architectural_wall"), {"id": "REF", "mode": "reference", "source_layer": "metal", "points": [[0, 0], [10, 0]]}]
        result = self.resolve(base)
        self.assertEqual(base[-2:], result["groups"][-2:])
        self.assertEqual(["WALL", "REF"], result["audit"]["untouched_component_ids"])

    def test_cutters_unused_by_targets_are_not_audited_as_used(self):
        slabs = [rc_material(), rc_material("FAR", shape=footprint(10000, 10000))]
        result = self.resolve(slabs=slabs)
        self.assertEqual(["RC-001"], [group["id"] for group in result["audit"]["cutters"]])
        self.assertEqual(["RC-001"], result["audit"]["cuts"][0]["rc_cutter_component_ids"])

    def test_slanted_partial_intersection_preserves_oblique_edges(self):
        original = material(shape={"outer": [[0, 0], [1000, 0], [1200, 200], [200, 200]], "holes": []})
        slab = rc_material(base=1000, height=900, shape={"outer": [[250, -100], [900, -100], [1000, 300], [350, 300]], "holes": []})
        result = self.resolve([original], [slab])
        self.assert_exact_material([original], [slab], result)
        self.assertGreater(len(result["groups"]), 3)
        self.assertGreater(result["audit"]["removed_volume_mm3"], 0)

    def test_rc_hole_is_kept_as_original_curtain_material(self):
        original = material(shape=footprint(0, 0, 1000, 1000))
        slab = rc_material(base=0, height=3950, shape=footprint(-100, -100, 1200, 1200, holes=[footprint(300, 300, 400, 400)["outer"]]))
        result = self.resolve([original], [slab])
        self.assert_exact_material([original], [slab], result)
        self.assertEqual(1, len(result["groups"]))
        self.assertEqual(160000, geometry(result["groups"][0]).area)

    def test_original_footprint_hole_is_never_filled(self):
        original = material(shape=footprint(0, 0, 1000, 1000, holes=[footprint(400, 400, 200, 200)["outer"]]))
        slab = rc_material(base=1000, height=500, shape=footprint(100, 100, 800, 800))
        result = self.resolve([original], [slab])
        self.assert_exact_material([original], [slab], result)
        hole = Polygon(footprint(400, 400, 200, 200)["outer"])
        self.assertTrue(all(geometry(group).intersection(hole).area == 0 for group in result["groups"]))

    def test_identical_neighbor_slices_merge_despite_cutter_identity_change(self):
        slabs = [rc_material("LOW", base=1000, height=500), rc_material("HIGH", base=1500, height=500)]
        result = self.resolve(slabs=slabs)
        self.assert_exact_material([material()], slabs, result)
        self.assertEqual(4, len(result["groups"]))
        mid = [group for group in result["groups"] if group["base_z_mm"] == 1000]
        self.assertEqual(2, len(mid))
        self.assertTrue(all(group["height_mm"] == 1000 for group in mid))
        self.assertTrue(all(group["geometry_modifier_provenance"]["slice_active_cutter_ids"] == ["HIGH", "LOW"] for group in mid))
        self.assertEqual(4, len(result["audit"]["cuts"][0]["z_slices"]))

    def test_full_cross_section_cut_leaves_separate_z_parts(self):
        slab = rc_material(base=1000, height=1000, shape=footprint(-100, -100, 1200, 300))
        result = self.resolve(slabs=[slab])
        self.assert_exact_material([material()], [slab], result)
        self.assertEqual([(0, 1000), (2000, 1950)], [(group["base_z_mm"], group["height_mm"]) for group in result["groups"]])
        self.assertEqual({"parts": []}, result["audit"]["cuts"][0]["z_slices"][1]["remaining_footprint"])

    def test_full_removal_is_explicit_without_zero_volume_prism(self):
        slab = rc_material(base=-200, height=4500, shape=footprint(-100, -100, 1200, 300))
        result = self.resolve(slabs=[slab])
        self.assert_exact_material([material()], [slab], result)
        self.assertEqual([], result["groups"])
        self.assertEqual({}, result["height_rules"])
        self.assertEqual("fully_removed_by_confirmed_rc_material", result["audit"]["cuts"][0]["status"])
        self.assertEqual(395000000, result["audit"]["removed_volume_mm3"])

    def test_overlapping_cutters_are_unioned_without_double_subtraction(self):
        slabs = [rc_material("A", base=0, height=3950, shape=footprint(300, 0, 400, 100)), rc_material("B", base=0, height=3950, shape=footprint(500, 0, 400, 100))]
        result = self.resolve(slabs=slabs)
        self.assert_exact_material([material()], slabs, result)
        self.assertEqual(60000 * 3950, result["audit"]["removed_volume_mm3"])

    def test_original_nonzero_base_and_cutter_outside_z_are_clipped(self):
        original = material(base=600, height=3950)
        slabs = [rc_material("BOTTOM", base=-100, height=1000), rc_material("TOP", base=4300, height=900)]
        result = self.resolve([original], slabs)
        self.assert_exact_material([original], slabs, result)
        self.assertEqual(600, result["audit"]["cuts"][0]["z_slices"][0]["base_z_mm"])
        self.assertEqual(4550, result["audit"]["cuts"][0]["z_slices"][-1]["top_z_mm"])

    def test_curtain_accessory_is_valid_target(self):
        original = material(category="curtain_accessory")
        result = self.resolve([original])
        self.assert_exact_material([original], [rc_material()], result)
        self.assertTrue(all(group["category"] == "curtain_accessory" for group in result["groups"]))

    def test_result_is_deterministic_json_and_cutter_order_independent(self):
        base, slabs, config = building_slab_cut_fixture()
        result = self.resolve(base, slabs, config)
        self.assertEqual(result, self.resolve(base, list(reversed(slabs)), config))
        self.assertEqual(result, json.loads(json.dumps(result, allow_nan=False)))
        self.assertEqual([f"CURTAIN-001-rc-cut-{number:03}" for number in range(1, 7)], result["audit"]["cuts"][0]["result_component_ids"])

    def test_previous_modifier_provenance_is_preserved(self):
        original = material(geometry_modifier_provenance={"schema": "old-repair", "source": ["EXACT"]})
        result = self.resolve([original])
        self.assertEqual(original["geometry_modifier_provenance"], result["groups"][0]["geometry_modifier_provenance"]["prior_geometry_modifier_provenance"])

    def test_wall_column_or_slab_cannot_be_selected_even_with_scope_override(self):
        for category in ["architectural_wall", "structural_wall", "column", "slab"]:
            with self.subTest(category=category):
                base = [material(category=category)]
                for config in [cut_config(), cut_config(target_categories=[category])]:
                    result = self.resolve(base, config=config)
                    self.assertTrue(result["blockers"])
                    self.assertEqual(base, result["groups"])
                    self.assertEqual({}, result["height_rules"])

    def test_invalid_recipe_never_partially_changes_base(self):
        base = [material()]
        cases = [None, {}, {"enabled": "true"}, cut_config(operation="trim_to_nominal_height"), cut_config(target_component_ids=[]), cut_config("UNKNOWN"), cut_config(target_categories=[])]
        for config in cases:
            with self.subTest(config=config):
                result = resolve_building_slab_cuts(base, [rc_material()], config)
                self.assertTrue(result["blockers"])
                self.assertEqual(base, result["groups"])
                self.assertEqual({}, result["height_rules"])
                self.assertFalse(result["audit"]["all_pass"])

    def test_invalid_original_geometry_and_missing_sources_are_blocked(self):
        changes = [{"height_mm": None}, {"height_mm": 0}, {"base_z_mm": math.nan}, {"top_z_mm": 4000}, {"source_entity_ids": []}, {"outer": [[0, 0], [1, 1], [0, 1], [1, 0]]}, {"outer": [[0, 0, 0], [10, 0, 0], [10, 10, 0]]}, {"mode": "reference"}]
        for change in changes:
            with self.subTest(change=change):
                original = material(**change)
                result = self.resolve([original])
                self.assertIn("building_slab_cut_target_invalid", self.codes(result))
                self.assertEqual([original], result["groups"])

    def test_invalid_or_non_rc_cutters_are_blocked(self):
        for slabs in [[], [material("NOT-RC")], [rc_material(slab_role="ceiling")], [rc_material(height_mm=0)], [rc_material(source_entity_ids=[])], [rc_material(), rc_material()]]:
            with self.subTest(slabs=slabs):
                result = self.resolve(slabs=slabs)
                self.assertTrue(result["blockers"])
                self.assertEqual([material()], result["groups"])

    def test_duplicate_original_or_generated_id_collision_rolls_back(self):
        for base in [[material(), material()], [material(), material("CURTAIN-001-rc-cut-001")]]:
            with self.subTest(base=base):
                result = self.resolve(base)
                self.assertTrue(result["blockers"])
                self.assertEqual(base, result["groups"])
                self.assertEqual({}, result["height_rules"])


if __name__ == "__main__":
    unittest.main()

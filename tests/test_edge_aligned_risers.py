from __future__ import annotations

import copy
import json
import unittest

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from dwg_semantic.slab_modeling import common_edge_precision_overlay, prepare_slabs

PROFILE = "edge_aligned_full_depth"


def box(x=0, y=0, width=1000, depth=1000):
    return {"outer": [[x, y], [x + width, y], [x + width, y + depth], [x, y + depth]], "holes": []}


def shape(value):
    if "parts" in value:
        return unary_union([shape(part) for part in value["parts"]])
    return Polygon(value["outer"], value.get("holes", []))


def zone(identity, footprint, top, **changes):
    value = {"id": identity, "footprint": footprint, "top_z_mm": top, "source_entity_ids": [identity], "method": "constant_thickness"}
    value.update(changes)
    return value


def edge_aligned_fixture(zones=None, extent=None, openings=None, width=200, default_top=0):
    """Deterministic (document, decisions) fixture reusable by native tests."""
    zones = zones if zones is not None else [zone("low", box(0, 0, 500, 1000), -600)]
    openings = openings or []
    identities = {"boundary", *(item for section in zones + openings for item in section.get("source_entity_ids", []))}
    document = {"intake": {"source_path": "C:/fixtures/edge-aligned.dwg"}, "entities": [{"entity_id": identity, "layer": "LOW" if identity == "low" else "RC", "original_layer": "LOW" if identity == "low" else "RC", "handle": identity} for identity in sorted(identities)]}
    system = {"id": "roof", "role": "overhead", "extent": extent or box(), "source_entity_ids": ["boundary"], "default_top_z_mm": default_top, "default_thickness_mm": 200, "zones": zones, "openings": openings, "step_risers": {"enabled": True, "profile": PROFILE, "thickness_mm": width}}
    config = {"enabled": True, "confirmed_scope": True, "derived_tag": {"kind": "derived", "tag": "RC-derived", "source_layer": "RC-derived"}, "systems": [system]}
    return document, config


class EdgeAlignedRiserTests(unittest.TestCase):
    def test_precision_material_band_can_touch_an_isolated_zero_area_point(self):
        from dwg_semantic.modeling import validate_slab_payload
        lower = {"outer": [[0, 0], [1000, 0], [1000, 500], [0, 500]],
                 "holes": [[[100, 100], [900, 100], [900, 400], [600, 400],
                            [500, 200], [400, 400], [100, 400]]]}
        document, config = edge_aligned_fixture(
            [zone("low", lower, -600)], extent=box(0, -500, 1000, 1000))
        config["systems"][0]["step_risers"]["geometry_precision_mm"] = .05
        result = prepare_slabs(document, config)
        self.assertEqual([], result["blockers"])
        self.assertEqual([], validate_slab_payload(result))
        changed = copy.deepcopy(result)
        band = changed["slab_audit"]["geometry_precision"][0]["edge_overlay"]["input_requested_bands"][0]["footprint"]
        polygon = band.get("parts", [band])[0]
        polygon["outer"][0][0] += .1
        self.assertIn("invalid_slab_geometry_precision", {issue["code"] for issue in validate_slab_payload(changed)})

    def prepare(self, zones=None, **kwargs):
        document, config = edge_aligned_fixture(zones, **kwargs)
        result = prepare_slabs(document, config)
        self.assertEqual([], result["blockers"])
        return result

    def risers(self, result):
        return [group for group in result["groups"] if group["slab_role"] == "slab_step_riser"]

    def assert_physical_invariants(self, result):
        self.assertEqual([], result["blockers"])
        self.assertEqual([], result["slab_audit"]["unresolved_overlaps"])
        groups = result["groups"]
        snapshot = result["slab_audit"]["edge_aligned_replacements"][0]
        originals = {group["id"]: group for group in snapshot["original_groups"]}
        actuals = {group["id"]: group for group in groups}
        rows = result["slab_audit"]["step_risers"]
        projection = unary_union([shape(group) for group in groups])
        original_projection = unary_union([shape(group) for group in originals.values()])
        self.assertLess(projection.symmetric_difference(original_projection).area, 1e-6)
        self.assertEqual(0, snapshot["envelope_difference_area_mm2"])
        self.assertEqual([], result["slab_audit"]["step_riser_slices"])
        for number, first in enumerate(groups):
            for second in groups[number + 1:]:
                dz = min(first["top_z_mm"], second["top_z_mm"]) - max(first["base_z_mm"], second["base_z_mm"])
                self.assertLessEqual(max(0, dz) * shape(first).intersection(shape(second)).area, 1e-5)
        for row in rows:
            lower = originals[row["original_lower_horizontal_component_ids"][0]]
            upper = originals[row["original_upper_horizontal_component_ids"][0]]
            shared = unary_union([LineString(points) for points in row["shared_boundary"]["lines"]])
            requested = shared.buffer(row["horizontal_thickness_mm"], cap_style="flat", join_style="mitre").intersection(shape(lower))
            self.assertLess(shape(row["requested_footprint"]).symmetric_difference(requested).area, 1e-7)
            self.assertLess(requested.intersection(shape(upper)).area, 1e-7)
            self.assertEqual((lower["base_z_mm"], upper["top_z_mm"]), (row["base_z_mm"], row["top_z_mm"]))
            panels = [actuals[identity] for identity in row["component_ids"]]
            self.assertLess(requested.difference(unary_union([shape(panel) for panel in panels])).area, 1e-7)
            self.assertTrue(all(panel["base_z_mm"] <= row["base_z_mm"] and panel["top_z_mm"] >= row["top_z_mm"] for panel in panels))
            for side in ("lower", "upper"):
                self.assertTrue(set(row[side + "_replacement_component_ids"]).issubset(actuals))
        events = sorted({value for group in [*groups, *originals.values()] for value in [group["base_z_mm"], group["top_z_mm"]]})
        for lo, hi in zip(events, events[1:]):
            z = (lo + hi) / 2
            original_at_z = [shape(group) for group in originals.values() if group["base_z_mm"] < z < group["top_z_mm"]]
            required = [shape(row["requested_footprint"]) for row in rows if row["base_z_mm"] < z < row["top_z_mm"]]
            expected = unary_union(original_at_z + required)
            actual = unary_union([shape(group) for group in groups if group["base_z_mm"] < z < group["top_z_mm"]])
            self.assertLess(actual.symmetric_difference(expected).area, 1e-6)
        for panel in self.risers(result):
            self.assertEqual(PROFILE, panel["profile"])
            linked = [row for row in rows if row["id"] in panel["riser_audit_ids"]]
            self.assertTrue(linked)
            self.assertIn(panel["base_z_mm"], [row["base_z_mm"] for row in linked])
            self.assertIn(panel["top_z_mm"], [row["top_z_mm"] for row in linked])
            for row in linked:
                self.assertIn(panel["id"], row["component_ids"])

    def test_straight_edge_full_depth_and_exact_original_offset(self):
        result = self.prepare()
        self.assert_physical_invariants(result)
        panels = self.risers(result)
        self.assertEqual(1, len(panels))
        panel = panels[0]
        self.assertEqual((300, 0, 500, 1000), shape(panel).bounds)
        self.assertEqual((-800, 0, 800), (panel["base_z_mm"], panel["top_z_mm"], panel["height_mm"]))
        self.assertEqual(160000000, panel["volume_mm3"])
        lower = next(group for group in result["groups"] if group["slab_role"] == "overhead_slab" and group["top_z_mm"] == -600)
        self.assertEqual((0, 0, 300, 1000), shape(lower).bounds)

    def test_small_step_still_has_full_depth_panel(self):
        result = self.prepare([zone("low", box(0, 0, 500, 1000), -50)])
        self.assert_physical_invariants(result)
        self.assertEqual((-250, 0, 250), tuple(self.risers(result)[0][field] for field in ["base_z_mm", "top_z_mm", "height_mm"]))
        self.assertTrue(all(row["status"] == PROFILE for row in result["slab_audit"]["step_risers"]))

    def test_l_corner_is_one_clean_prism_without_layered_slices(self):
        low = {"outer": [[0, 0], [1000, 0], [1000, 400], [400, 400], [400, 1000], [0, 1000]], "holes": []}
        result = self.prepare([zone("low", low, -600)])
        self.assert_physical_invariants(result)
        self.assertEqual(1, len(self.risers(result)))
        self.assertGreaterEqual(len(self.risers(result)[0]["outer"]), 6)

    def test_t_junction_uses_whole_tallest_spans_without_positive_overlap(self):
        result = self.prepare([zone("low", box(0, 0, 500, 1000), -600), zone("deep", box(500, 0, 500, 500), -1000)])
        self.assert_physical_invariants(result)
        self.assertEqual(3, len(result["slab_audit"]["step_risers"]))
        self.assertEqual(3, len(self.risers(result)))
        deepest = [panel for panel in self.risers(result) if panel["base_z_mm"] == -1200]
        self.assertEqual([-600, 0], sorted(panel["top_z_mm"] for panel in deepest))

    def test_four_levels_and_nearby_edges_resolve_tallest_first(self):
        result = self.prepare([zone("low", box(0, 0, 450, 1000), -600), zone("narrow", box(450, 0, 100, 1000), -1000), zone("medium", box(550, 0, 450, 500), -300)])
        self.assert_physical_invariants(result)
        snapshot = result["slab_audit"]["edge_aligned_replacements"][0]
        narrow = next(group for group in snapshot["original_groups"] if group["top_z_mm"] == -1000)
        replacement = next(row for row in snapshot["replacements"] if row["original_component_id"] == narrow["id"])
        self.assertEqual([], replacement["result_horizontal_component_ids"])
        self.assertTrue(replacement["riser_component_ids"])

    def test_narrow_lower_region_fully_absorbed_has_actual_replacement_refs(self):
        result = self.prepare([zone("low", box(450, 0, 100, 1000), -600)])
        self.assert_physical_invariants(result)
        self.assertEqual(1, len(self.risers(result)))
        self.assertEqual(1, len(result["slab_audit"]["zones"]))
        for row in result["slab_audit"]["step_risers"]:
            self.assertEqual([], row["lower_horizontal_component_ids"])
            self.assertEqual([self.risers(result)[0]["id"]], row["lower_replacement_component_ids"])

    def test_hole_on_shared_boundary_is_not_closed(self):
        opening = {"id": "shaft", "footprint": box(400, 400, 200, 200), "source_entity_ids": ["shaft"], "applies_to_system_id": "roof"}
        result = self.prepare(openings=[opening])
        self.assert_physical_invariants(result)
        self.assertEqual(2, len(self.risers(result)))
        self.assertTrue(all(shape(group).intersection(shape(opening["footprint"])).area == 0 for group in result["groups"]))

    def test_internal_hole_in_lower_band_remains_empty(self):
        opening = {"id": "shaft", "footprint": box(350, 400, 100, 200), "source_entity_ids": ["shaft"], "applies_to_system_id": "roof"}
        result = self.prepare(openings=[opening])
        self.assert_physical_invariants(result)
        self.assertEqual(1, len(self.risers(result)))
        self.assertEqual(1, len(self.risers(result)[0]["holes"]))

    def test_oblique_shared_line_stays_on_lower_side(self):
        low = {"outer": [[0, 0], [300, 0], [700, 1000], [0, 1000]], "holes": []}
        result = self.prepare([zone("low", low, -500)], width=150)
        self.assert_physical_invariants(result)
        band = shape(self.risers(result)[0])
        self.assertLess(band.difference(shape(low)).area, 1e-7)
        self.assertAlmostEqual(150, band.area / LineString([[300, 0], [700, 1000]]).length, delta=12)

    def test_disconnected_plan_domains_do_not_get_a_connector(self):
        opening = {"id": "gap", "footprint": box(450, -1, 100, 1002), "source_entity_ids": ["gap"], "applies_to_system_id": "roof"}
        result = self.prepare(openings=[opening])
        self.assert_physical_invariants(result)
        self.assertEqual([], self.risers(result))
        self.assertEqual([], result["slab_audit"]["step_risers"])

    def test_outer_edges_are_not_closed(self):
        result = self.prepare()
        panel = shape(self.risers(result)[0])
        self.assertEqual(200000, panel.area)
        self.assertEqual(0, panel.intersection(Polygon(box(0, 0, 100, 1000)["outer"])).area)

    def test_same_level_different_tags_are_not_joined_by_a_panel(self):
        doc, config = edge_aligned_fixture([zone("low", box(0, 0, 500, 1000), 0, tag_binding={"kind": "source", "tag": "LOW", "source_layer": "LOW"})])
        result = prepare_slabs(doc, config)
        self.assert_physical_invariants(result)
        self.assertEqual([], self.risers(result))
        self.assertEqual({"LOW", "RC-derived"}, {group["tag"] for group in result["groups"]})

    def test_original_lower_tag_is_on_actual_panel_and_sources_include_both_hosts(self):
        doc, config = edge_aligned_fixture([zone("low", box(0, 0, 500, 1000), -600, tag_binding={"kind": "source", "tag": "LOW", "source_layer": "LOW"})])
        result = prepare_slabs(doc, config)
        self.assert_physical_invariants(result)
        panel = self.risers(result)[0]
        self.assertEqual(("LOW", "LOW", "source"), (panel["tag"], panel["source_layer"], panel["tag_kind"]))
        self.assertEqual({"boundary", "low"}, set(panel["source_entity_ids"]))
        self.assertEqual({"LOW", "RC"}, set(panel["source_layers"]))

    def test_continuous_coplanar_bands_with_distinct_original_tags_stay_separate(self):
        doc, config = edge_aligned_fixture([zone("low", box(0, 0, 500, 1000), -600, tag_binding={"kind": "source", "tag": "LOW", "source_layer": "LOW"}), zone("other", box(500, 0, 500, 500), -600)])
        result = prepare_slabs(doc, config)
        self.assert_physical_invariants(result)
        panels = self.risers(result)
        self.assertEqual(2, len(panels))
        self.assertEqual({"LOW", "RC-derived"}, {group["tag"] for group in panels})
        self.assertTrue(all((group["base_z_mm"], group["top_z_mm"]) == (-800, 0) for group in panels))

    def test_large_translated_source_coordinates_do_not_move_original_edge(self):
        offset_x, offset_y = 11431.798, 29356.451
        result = self.prepare([zone("low", box(offset_x, offset_y, 500, 1000), -600)], extent=box(offset_x, offset_y))
        self.assert_physical_invariants(result)
        xmin, ymin, xmax, ymax = shape(self.risers(result)[0]).bounds
        self.assertAlmostEqual(offset_x + 300, xmin)
        self.assertAlmostEqual(offset_x + 500, xmax)
        self.assertAlmostEqual(offset_y, ymin)
        self.assertAlmostEqual(offset_y + 1000, ymax)

    def test_redundant_collinear_source_vertices_create_one_face_per_straight_side(self):
        low = {"outer": [[0, 0], [250, 0], [500, 0], [500, 200], [500, 400], [500, 600], [500, 800], [500, 1000], [250, 1000], [0, 1000], [0, 500]], "holes": []}
        result = self.prepare([zone("low", low, -600)])
        self.assert_physical_invariants(result)
        panel = self.risers(result)[0]
        row = result["slab_audit"]["step_risers"][0]
        requested = shape(row["requested_footprint"])
        actual = shape(panel)
        self.assertEqual(0, actual.symmetric_difference(requested).area)
        self.assertEqual(0, actual.hausdorff_distance(requested))
        self.assertEqual(4, len(panel["outer"]))
        self.assertEqual([], panel["holes"])
        self.assertEqual({(300, 0), (500, 0), (500, 1000), (300, 1000)}, {tuple(point) for point in panel["outer"]})
        # One explicit perimeter edge per side means the extrusion produces
        # exactly four full rectangular side faces, not collinear sub-faces.
        points = panel["outer"]
        edges = list(zip(points, points[1:] + points[:1]))
        self.assertEqual([200, 200, 1000, 1000], sorted(LineString(edge).length for edge in edges))

    def test_assemblies_zones_and_original_snapshots_reconcile(self):
        result = self.prepare()
        self.assert_physical_invariants(result)
        audit = result["slab_audit"]
        snapshot = audit["edge_aligned_replacements"][0]
        self.assertEqual(1000000, snapshot["original_assembly"]["expected_area_mm2"])
        self.assertEqual(800000, audit["assemblies"][0]["expected_area_mm2"])
        horizontal = [group for group in result["groups"] if group["slab_role"] == "overhead_slab"]
        self.assertEqual({group["id"] for group in horizontal}, set(audit["assemblies"][0]["component_ids"]))
        self.assertEqual(sum(group["volume_mm3"] for group in horizontal), audit["assemblies"][0]["expected_volume_mm3"])
        for zone_row in audit["zones"]:
            actual = [group for group in horizontal if group["id"] in zone_row["component_ids"]]
            self.assertEqual(sum(group["area_mm2"] for group in actual), zone_row["expected_area_mm2"])
        for before in snapshot["original_groups"]:
            self.assertNotIn("original_horizontal_component_id", before)
            self.assertEqual(200, before["height_mm"])

    def test_original_host_can_split_into_multiple_horizontal_parts(self):
        # The lower plate wraps around an island through 200mm-wide necks;
        # replacing those necks by the reviewed band splits that one host.
        result = self.prepare([zone("high", box(450, 200, 100, 600), 0)], default_top=-600)
        self.assert_physical_invariants(result)
        snapshot = result["slab_audit"]["edge_aligned_replacements"][0]
        lower = next(group for group in snapshot["original_groups"] if group["top_z_mm"] == -600)
        replacement = next(row for row in snapshot["replacements"] if row["original_component_id"] == lower["id"])
        self.assertEqual(2, len(replacement["result_horizontal_component_ids"]))
        self.assertEqual(set(replacement["result_horizontal_component_ids"]), set(result["slab_audit"]["step_risers"][0]["lower_horizontal_component_ids"]))

    def test_deterministic_no_input_mutation_and_no_legacy_changes(self):
        doc, config = edge_aligned_fixture()
        before = copy.deepcopy((doc, config))
        first = prepare_slabs(doc, config)
        self.assertEqual(first, prepare_slabs(doc, config))
        self.assertEqual(first, json.loads(json.dumps(first, allow_nan=False)))
        self.assertEqual(before, (doc, config))
        config["systems"][0]["step_risers"].pop("profile")
        legacy = prepare_slabs(doc, config)
        self.assertEqual([], legacy["blockers"])
        self.assertNotIn("edge_aligned_replacements", legacy["slab_audit"])
        self.assertEqual((-600, -200), tuple(self.risers(legacy)[0][field] for field in ["base_z_mm", "top_z_mm"]))

    def test_disabled_profile_leaves_original_horizontal_groups_unchanged(self):
        doc, config = edge_aligned_fixture()
        config["systems"][0]["step_risers"]["enabled"] = False
        disabled = prepare_slabs(doc, config)
        config["systems"][0].pop("step_risers")
        plain = prepare_slabs(doc, config)
        self.assertEqual(plain["groups"], disabled["groups"])
        self.assertNotIn("edge_aligned_replacements", disabled["slab_audit"])

    def test_unknown_profile_is_a_blocker(self):
        doc, config = edge_aligned_fixture()
        config["systems"][0]["step_risers"]["profile"] = "auto_guess"
        result = prepare_slabs(doc, config)
        self.assertIn("slab_step_riser_profile_invalid", {item["code"] for item in result["blockers"]})

    def test_explicit_precision_has_raw_snap_source_noding_and_final_audits(self):
        doc, config = edge_aligned_fixture([zone("low", box(0, 0, 500.0045, 1000), -600)])
        config["systems"][0]["step_risers"]["geometry_precision_mm"] = .05
        before = copy.deepcopy((doc, config))
        result = prepare_slabs(doc, config)
        self.assert_physical_invariants(result)
        record = result["slab_audit"]["geometry_precision"][0]
        self.assertEqual(.05, record["max_boundary_change_mm"])
        self.assertAlmostEqual(.0045, record["max_vertex_displacement_mm"])
        self.assertEqual("common_noded_planar_faces", record["source_overlay"]["algorithm"])
        self.assertEqual([], record["source_overlay"]["input_requested_bands"])
        self.assertEqual("common_noded_planar_faces", record["edge_overlay"]["algorithm"])
        self.assertEqual(record["source_noded_original_groups"], record["edge_overlay"]["input_original_groups"])
        self.assertEqual(0, sum(row["boundary_change_outside_limit_area_mm2"] for row in record["final_components"]))
        self.assertEqual(before, (doc, config))

    def test_disabled_precision_prepares_same_common_source_height_field(self):
        doc, config = edge_aligned_fixture([zone("low", box(0, 0, 500.0045, 1000), -600)])
        config["systems"][0]["step_risers"].update(enabled=False, geometry_precision_mm=.05)
        result = prepare_slabs(doc, config)
        self.assertEqual([], result["blockers"])
        self.assertEqual([], self.risers(result))
        record = result["slab_audit"]["geometry_precision"][0]
        self.assertEqual(record["source_noded_original_groups"], result["groups"])
        self.assertEqual(record["source_noded_original_groups"], record["canonical_original_groups"])
        self.assertEqual([], record["source_overlay"]["input_requested_bands"])
        self.assertNotIn("edge_overlay", record)

    def test_precision_removes_unrepresentable_sliver_with_explicit_topology_record(self):
        low = box(0, 0, 500, 1000)
        low["holes"] = [[[100, 400], [100.00904, 600], [100.00904, 400]]]
        doc, config = edge_aligned_fixture([zone("low", low, -600)])
        config["systems"][0]["step_risers"]["geometry_precision_mm"] = .05
        result = prepare_slabs(doc, config)
        self.assert_physical_invariants(result)
        record = result["slab_audit"]["geometry_precision"][0]
        self.assertTrue(any(row["status"] == "collapsed" for row in record["components"]))
        self.assertTrue(any(row["removed_raw_holes"] for row in record["components"]))
        self.assertTrue(any(row["removed_raw_holes"] for row in record["final_components"]))
        self.assertTrue(all(shape(group).minimum_clearance > .0254 for group in result["groups"]))

    def test_common_graph_assigns_nearly_collinear_sources_to_disjoint_exact_faces(self):
        # Two separately rounded source edges have slightly different node
        # sequences. Their common graph must own each resulting face once.
        originals = [{"id": "left", **{"outer": [[0, 0], [500, 0], [500.5, 1000], [0, 1000]], "holes": []}}, {"id": "right", **{"outer": [[500, 0], [1000, 0], [1000, 1000], [500.5, 1000], [500.25, 499.98]], "holes": []}}]
        overlay = common_edge_precision_overlay(originals, [], .05)
        regions = [shape(row["footprint"]) for row in overlay["normalized_host_regions"]]
        self.assertEqual(0, regions[0].intersection(regions[1]).area)
        self.assertEqual(1000000, unary_union(regions).area)
        self.assertEqual(overlay["atomic_faces"], common_edge_precision_overlay(list(reversed(originals)), [], .05)["atomic_faces"])
        self.assertTrue(all(row["requested_connection_ids"] == [] for row in overlay["atomic_faces"]))

    def test_precision_keeps_true_holes_and_external_boundary(self):
        opening = {"id": "shaft", "footprint": box(350, 400, 100, 200), "source_entity_ids": ["shaft"], "applies_to_system_id": "roof"}
        doc, config = edge_aligned_fixture(openings=[opening])
        config["systems"][0]["step_risers"]["geometry_precision_mm"] = .05
        result = prepare_slabs(doc, config)
        self.assert_physical_invariants(result)
        record = result["slab_audit"]["geometry_precision"][0]
        self.assertEqual(0, record["projected_symmetric_difference_area_mm2"])
        self.assertEqual([], record["removed_projected_holes"])
        self.assertEqual(1, len(shape(record["canonical_projected_footprint"]).interiors))

    def test_invalid_or_large_precision_is_blocking(self):
        for grid in [0, -1, True, float("nan"), .2]:
            with self.subTest(grid=grid):
                doc, config = edge_aligned_fixture()
                config["systems"][0]["step_risers"]["geometry_precision_mm"] = grid
                result = prepare_slabs(doc, config)
                self.assertIn("slab_geometry_precision_invalid", {row["code"] for row in result["blockers"]})


if __name__ == "__main__":
    unittest.main()

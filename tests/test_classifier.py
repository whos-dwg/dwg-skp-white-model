from __future__ import annotations

import copy
import unittest
from pathlib import Path

from dwg_semantic.classifier import classify_document, load_rules


ROOT = Path(__file__).resolve().parents[1]


def box(x0: float, y0: float, x1: float, y1: float) -> dict:
    return {"min": {"x": x0, "y": y0}, "max": {"x": x1, "y": y1}}


def line(handle: str, layer: str, start: tuple[float, float], end: tuple[float, float]) -> dict:
    return {
        "type": "LINE",
        "handle": handle,
        "owner_block": "*Model_Space",
        "layer": layer,
        "start": {"x": start[0], "y": start[1]},
        "end": {"x": end[0], "y": end[1]},
        "local_extents": box(min(start[0], end[0]), min(start[1], end[1]), max(start[0], end[0]), max(start[1], end[1])),
    }


def rectangle(handle: str, layer: str, x: float, y: float, width: float, height: float) -> dict:
    return {
        "type": "LWPOLYLINE",
        "handle": handle,
        "owner_block": "*Model_Space",
        "layer": layer,
        "closed": True,
        "area": width * height,
        "vertices": [
            {"x": x, "y": y},
            {"x": x + width, "y": y},
            {"x": x + width, "y": y + height},
            {"x": x, "y": y + height},
        ],
        "local_extents": box(x, y, x + width, y + height),
    }


def open_polyline(handle: str, layer: str, points: list[tuple[float, float]]) -> dict:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return {
        "type": "LWPOLYLINE",
        "handle": handle,
        "owner_block": "*Model_Space",
        "layer": layer,
        "closed": False,
        "vertices": [{"x": x, "y": y} for x, y in points],
        "local_extents": box(min(xs), min(ys), max(xs), max(ys)),
    }


def fixture(handle: str, x: float, y: float) -> dict:
    return {
        "type": "INSERT",
        "handle": handle,
        "owner_block": "*Model_Space",
        "layer": "LA-sanitary",
        "block_name": "TOILET-FIXTURE",
        "position": {"x": x, "y": y},
        "local_extents": box(x - 200, y - 200, x + 200, y + 200),
    }


def curtain_fixture() -> tuple[dict, list[dict]]:
    entity = {
        "type": "INSERT",
        "handle": "EXT",
        "owner_block": "*Model_Space",
        "layer": "A-window",
        "block_name": "EXTERIOR-ASSEMBLY",
        "position": {"x": 0, "y": 0},
        "local_extents": box(0, 0, 20000, 16000),
    }
    resolved = []
    for index, name in enumerate(("CW_GLASS", "標準豎框", "中空玻璃", "MULLION"), 1):
        resolved.append({
            "type": "BLOCK_REFERENCE",
            "root_space": "*Model_Space",
            "instance_path": f"EXT/B{index}",
            "source_handle": f"B{index}",
            "block_name": name,
            "layer": "0",
            "position_world": {"x": index * 1000, "y": 0},
        })
    for index, (start, end) in enumerate((
        ((0, 0), (20000, 0)),
        ((20000, 0), (20000, 16000)),
        ((20000, 16000), (0, 16000)),
        ((0, 16000), (0, 0)),
    ), 1):
        resolved.append({
            "type": "LINE",
            "root_space": "*Model_Space",
            "instance_path": f"EXT/G{index}",
            "source_handle": f"G{index}",
            "layer": "A-window",
            "start_world": {"x": start[0], "y": start[1]},
            "end_world": {"x": end[0], "y": end[1]},
        })
    return entity, resolved


def structural_container_fixture() -> tuple[dict, list[dict]]:
    container = {
        "type": "INSERT",
        "handle": "STRUCT-ROOT",
        "owner_block": "*Model_Space",
        "layer": "A-struct",
        "block_name": "STRUCTURAL-PLAN",
        "position": {"x": 0, "y": 0},
        "local_extents": box(0, 0, 20000, 16000),
    }
    resolved = [
        {
            "type": "LWPOLYLINE",
            "root_space": "*Model_Space",
            "instance_path": "STRUCT-ROOT",
            "source_handle": "COL-OUTER",
            "source_block": "STRUCTURAL-PLAN",
            "layer": "0",
            "closed": True,
            "world_extents": box(9000, 7000, 9600, 7600),
            "vertices_world": [
                {"x": 9000, "y": 7000},
                {"x": 9600, "y": 7000},
                {"x": 9600, "y": 7600},
                {"x": 9000, "y": 7600},
            ],
        },
        {
            "type": "LWPOLYLINE",
            "root_space": "*Model_Space",
            "instance_path": "STRUCT-ROOT",
            "source_handle": "WALL-1",
            "source_block": "STRUCTURAL-PLAN",
            "layer": "0",
            "closed": False,
            "world_extents": box(2000, 2000, 8000, 2400),
            "vertices_world": [
                {"x": 2000, "y": 2000},
                {"x": 8000, "y": 2000},
                {"x": 8000, "y": 2400},
                {"x": 2000, "y": 2400},
                {"x": 2000, "y": 2000},
            ],
        },
        {
            "type": "LINE",
            "root_space": "*Model_Space",
            "instance_path": "STRUCT-ROOT",
            "source_handle": "WALL-2",
            "source_block": "STRUCTURAL-PLAN",
            "layer": "0",
            "world_extents": box(2000, 2400, 8000, 2400),
            "start_world": {"x": 2000, "y": 2400},
            "end_world": {"x": 8000, "y": 2400},
        },
        {
            "type": "LINE",
            "root_space": "*Model_Space",
            "instance_path": "STRUCT-ROOT",
            "source_handle": "WALL-3",
            "source_block": "STRUCTURAL-PLAN",
            "layer": "0",
            "world_extents": box(2000, 2000, 2000, 2400),
            "start_world": {"x": 2000, "y": 2000},
            "end_world": {"x": 2000, "y": 2400},
        },
        {
            "type": "LINE",
            "root_space": "*Model_Space",
            "instance_path": "STRUCT-ROOT/GRID-BLOCK",
            "source_handle": "GRID-LINE",
            "source_block": "GRID-BLOCK",
            "layer": "0",
            "world_extents": box(0, 8000, 20000, 8000),
            "start_world": {"x": 0, "y": 8000},
            "end_world": {"x": 20000, "y": 8000},
        },
    ]
    return container, resolved


def explicit_nested_semantic_fixture() -> tuple[dict, list[dict]]:
    container = {
        "type": "INSERT",
        "handle": "NESTED-ROOT",
        "owner_block": "*Model_Space",
        "layer": "0",
        "block_name": "MIXED-PLAN-ASSEMBLY",
        "position": {"x": 0, "y": 0},
        "local_extents": box(2000, 9000, 15000, 15000),
    }
    resolved = [
        {
            "type": "LINE",
            "root_space": "*Model_Space",
            "instance_path": "NESTED-ROOT/SUBPLAN",
            "source_handle": "INNER-WALL",
            "source_block": "SUBPLAN",
            "layer": "A-WALL",
            "world_extents": box(3000, 11000, 9000, 11000),
            "start_world": {"x": 3000, "y": 11000},
            "end_world": {"x": 9000, "y": 11000},
        },
        {
            "type": "LINE",
            "root_space": "*Model_Space",
            "instance_path": "NESTED-ROOT/SUBPLAN",
            "source_handle": "INNER-STRUCT-WALL",
            "source_block": "SUBPLAN",
            "layer": "2col",
            "world_extents": box(10000, 10000, 10000, 14500),
            "start_world": {"x": 10000, "y": 10000},
            "end_world": {"x": 10000, "y": 14500},
        },
        {
            "type": "LWPOLYLINE",
            "root_space": "*Model_Space",
            "instance_path": "NESTED-ROOT/SUBPLAN",
            "source_handle": "INNER-COLUMN",
            "source_block": "SUBPLAN",
            "layer": "0-COL",
            "closed": True,
            "world_extents": box(12000, 12000, 12600, 12600),
            "vertices_world": [
                {"x": 12000, "y": 12000},
                {"x": 12600, "y": 12000},
                {"x": 12600, "y": 12600},
                {"x": 12000, "y": 12600},
            ],
        },
        {
            "type": "BLOCK_REFERENCE",
            "root_space": "*Model_Space",
            "instance_path": "NESTED-ROOT/SUBPLAN/WIN-1",
            "source_handle": "WIN-1",
            "source_block": "SUBPLAN",
            "block_name": "WINDOW-1200",
            "layer": "4WIN",
            "world_extents": box(4800, 10700, 6000, 11300),
            "position_world": {"x": 5400, "y": 11000},
        },
    ]
    return container, resolved


def synthetic_document() -> dict:
    curtain, resolved = curtain_fixture()
    entities = [
        curtain,
        fixture("WC1", 5000, 5000),
        fixture("WC2", 5200, 5000),
        line("WF-L", "LA-wall-finish", (4000, 3800), (4000, 6200)),
        line("WF-R", "LA-wall-finish", (6200, 3800), (6200, 6200)),
        line("WF-B", "LA-wall-finish", (4000, 3800), (6200, 3800)),
        line("WF-T", "LA-wall-finish", (4000, 6200), (6200, 6200)),
        line("AW-1", "LA-wall", (2000, 8000), (9000, 8000)),
        line("SW-1", "A-struct", (10000, 3000), (10000, 9000)),
        rectangle("SC-1", "A-struct", 10800, 5000, 800, 800),
        rectangle("PC-1", "LA-wall-finish", 1000, 14500, 800, 1000),
        rectangle("PC-2", "LA-wall-finish", 9000, 14500, 800, 1000),
        rectangle("CAB-1", "LA-built-in", 12000, 14500, 800, 1000),
    ]
    return {"entities": entities, "resolved_instances": resolved}


class SemanticClassifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = load_rules(ROOT / "config" / "default_rules.json")

    def classify(self) -> dict:
        return classify_document(synthetic_document(), copy.deepcopy(self.rules), "synthetic.json")

    def test_toilet_walls_are_recovered_and_enclosure_passes(self) -> None:
        result = self.classify()
        self.assertGreaterEqual(result["counts"]["sanitary_wall_segments_promoted"], 4)
        self.assertEqual(result["counts"]["sanitary_zones"], 1)
        self.assertEqual(result["audits"]["sanitary_enclosures"][0]["status"], "pass")
        promoted_handles = {
            segment["source_handle"]
            for segment in result["inferences"]["sanitary_wall_segments_promoted"]
        }
        self.assertTrue({"WF-L", "WF-R", "WF-B", "WF-T"}.issubset(promoted_handles))

    def test_dominant_curtain_container_propagates_full_closed_envelope(self) -> None:
        result = self.classify()
        self.assertEqual(result["counts"]["curtain_envelopes"], 1)
        envelope = result["components"]["curtain_envelopes"][0]
        self.assertEqual(envelope["geometry_mode"], "dominant_container_closed_envelope")
        self.assertEqual(len(envelope["segments"]), 4)
        self.assertEqual(envelope["segments"][0]["start"], envelope["segments"][-1]["end"])

    def test_parallel_oversized_win_polylines_become_curtain_system(self) -> None:
        entities = [
            open_polyline(
                f"WIN-OUTER-{index}",
                "4win",
                [
                    (-offset, 16000 + offset),
                    (-offset, -offset),
                    (20000 + offset, -offset),
                    (20000 + offset, 16000 + offset),
                ],
            )
            for index, offset in enumerate((0, 15, 30), 1)
        ]
        result = classify_document({"entities": entities, "resolved_instances": []}, copy.deepcopy(self.rules), "large-win.json")
        self.assertEqual(result["counts"]["curtain_envelopes"], 1)
        self.assertEqual(result["counts"]["oversized_opening_objects_auto_classified_as_curtain"], 3)
        self.assertEqual(result["counts"]["special_dimension_review_requests"], 0)
        self.assertEqual(result["counts"]["opening_source_objects"], 0)
        system = result["components"]["curtain_envelopes"][0]
        self.assertEqual(system["geometry_mode"], "parallel_oversized_window_layer_polylines")
        self.assertEqual(system["source_handles"], ["WIN-OUTER-1", "WIN-OUTER-2", "WIN-OUTER-3"])

    def test_unresolved_oversized_win_object_requests_user_confirmation(self) -> None:
        entity = open_polyline(
            "WIN-SPECIAL",
            "4win",
            [(0, 16000), (0, 0), (20000, 0), (20000, 16000)],
        )
        result = classify_document({"entities": [entity], "resolved_instances": []}, copy.deepcopy(self.rules), "special-win.json")
        self.assertEqual(result["counts"]["curtain_envelopes"], 0)
        self.assertEqual(result["counts"]["opening_source_objects"], 0)
        self.assertEqual(result["counts"]["special_dimension_review_requests"], 1)
        request = result["audits"]["semantic_decision_requests"][0]
        self.assertEqual(request["source_handle"], "WIN-SPECIAL")
        self.assertEqual(request["status"], "user_confirmation_required")
        self.assertEqual(result["validation"]["status"], "review_required")
        self.assertIn(
            "special_dimension_user_confirmation",
            {warning["rule"] for warning in result["validation"]["warnings"]},
        )

    def test_curtain_section_elevation_and_mullion_layers_require_spatial_attachment(self) -> None:
        curtain_entities = [
            open_polyline(
                f"WIN-OUTER-{index}",
                "4win",
                [(-offset, 16000 + offset), (-offset, -offset), (20000 + offset, -offset), (20000 + offset, 16000 + offset)],
            )
            for index, offset in enumerate((0, 15, 30), 1)
        ]
        attached_line = line("ACC-LINE", "4金属构件看面", (1000, 200), (5000, 200))
        detached_line = line("ACC-FAR", "4金属构件看面", (5000, 8000), (10000, 8000))
        partially_attached = open_polyline(
            "ACC-PARTIAL",
            "4金属构件看面",
            [(0, 200), (1000, 200), (1000, 8000), (10000, 8000)],
        )
        attached_insert = {
            "type": "INSERT",
            "handle": "ACC-INSERT",
            "owner_block": "*Model_Space",
            "layer": "4金属构件断面",
            "block_name": "MULLION-SECTION",
            "local_extents": box(0, 2000, 220, 2250),
        }
        resolved = [{
            "type": "LWPOLYLINE",
            "root_space": "*Model_Space",
            "instance_path": "ACC-INSERT",
            "source_handle": "ACC-CHILD",
            "source_block": "MULLION-SECTION",
            "layer": "0",
            "closed": True,
            "world_extents": box(0, 2000, 220, 2250),
            "vertices_world": [
                {"x": 0, "y": 2000},
                {"x": 220, "y": 2000},
                {"x": 220, "y": 2250},
                {"x": 0, "y": 2250},
            ],
        }]
        result = classify_document(
            {
                "entities": curtain_entities + [attached_line, detached_line, partially_attached, attached_insert],
                "resolved_instances": resolved,
            },
            copy.deepcopy(self.rules),
            "curtain-accessories.json",
        )
        accessories = result["components"]["curtain_accessories"]
        handles = {item["source_handle"] for item in accessories}
        self.assertEqual(handles, {"ACC-LINE", "ACC-INSERT"})
        insert = next(item for item in accessories if item["source_handle"] == "ACC-INSERT")
        self.assertEqual(insert["geometry_mode"], "resolved_insert_geometry")
        self.assertEqual(insert["resolved_linear_descendant_count"], 1)
        self.assertEqual(result["counts"]["curtain_accessory_objects"], 2)

    def test_fire_shutter_and_fire_door_layers_are_explicitly_ignored(self) -> None:
        fire_line = line("FIRE-SHUTTER", "TEST-FIRE-DOOR", (0, 0), (18000, 0))
        result = classify_document(
            {"entities": [fire_line], "resolved_instances": []},
            load_rules(ROOT / "config/default_rules.json", ROOT / "tests/fixtures/synthetic-profile.json"),
            "fire-shutter.json",
        )
        self.assertEqual(result["counts"]["ignored_fire_protection_objects"], 1)
        self.assertEqual(result["counts"]["opening_source_objects"], 0)
        self.assertEqual(result["counts"]["special_dimension_review_requests"], 0)
        self.assertEqual(result["audits"]["ignored_fire_protection_objects"][0]["reason"], "profile_scope_exclusion")

    def test_generic_fire_door_is_retained_for_opening_review(self) -> None:
        result = classify_document({"entities": [line("FIRE", "A-FIRE DOOR", (0, 0), (900, 0))]}, self.rules)
        self.assertEqual(result["counts"]["ignored_fire_protection_objects"], 0)
        self.assertEqual(result["counts"]["opening_source_objects"], 1)
        self.assertEqual(result["components"]["openings"][0]["source_layer"], "A-FIRE DOOR")

    def test_generic_color_layer_is_not_structural_and_numbered_column_is(self) -> None:
        result = classify_document({"entities": [
            line("COLOR", "A-COLOR", (0, 0), (10000, 0)),
            rectangle("COL", "2col", 0, 0, 600, 600),
        ]}, self.rules)
        self.assertFalse(result["components"]["structural_walls"])
        self.assertEqual(result["components"]["columns"][0]["source_handles"], ["COL"])
        self.assertEqual(result["audits"]["source_coverage"]["unclassified"][0]["source_handle"], "COLOR")

    def test_profile_lists_add_and_override_without_mutating_defaults(self) -> None:
        rules = load_rules(ROOT / "config/default_rules.json", {"layer_semantics": {
            "structural_add": ["glob:consultant-*-rc"], "opening": ["PORTAL"]}})
        self.assertIn("column", rules["layer_semantics"]["structural"])
        self.assertIn("glob:consultant-*-rc", rules["layer_semantics"]["structural"])
        self.assertEqual(rules["layer_semantics"]["opening"], ["PORTAL"])
        self.assertNotIn("glob:consultant-*-rc", self.rules["layer_semantics"]["structural"])
        generic = classify_document({"entities": [line("W", "CUSTOM-SOLID", (0, 0), (1000, 0))]}, self.rules)
        case = classify_document({"entities": [line("W", "CUSTOM-SOLID", (0, 0), (1000, 0))]},
                                 load_rules(ROOT / "config/default_rules.json", ROOT / "tests/fixtures/synthetic-profile.json"))
        self.assertFalse(generic["components"]["structural_walls"])
        self.assertEqual(case["components"]["structural_walls"][0]["source_handle"], "W")

    def test_empty_unknown_and_unsupported_sources_cannot_pass(self) -> None:
        self.assertEqual(classify_document({}, self.rules)["validation"]["status"], "fail")
        unknown = classify_document({"entities": [line("U", "XYZ", (0, 0), (1000, 0)), {
            "type": "ACAD_PROXY_ENTITY", "handle": "PROXY", "layer": "XYZ", "owner_block": "*Model_Space"}]}, self.rules)
        self.assertEqual(unknown["validation"]["status"], "fail")
        coverage = unknown["audits"]["source_coverage"]
        self.assertEqual(coverage["unclassified_entities"], 2)
        self.assertEqual(coverage["unsupported_entity_types"], ["ACAD_PROXY_ENTITY"])
        self.assertEqual({item["source_handle"] for item in coverage["records"]}, {"U", "PROXY"})

    def test_plus_layer_names_are_preserved_exactly_after_wall_merge(self) -> None:
        result = classify_document({"entities": [
            line("A", "WALL+A", (0, 0), (1000, 0)),
            line("B", "WALL+B", (1000, 0), (2000, 0)),
        ]}, self.rules)
        self.assertEqual(len(result["components"]["architectural_walls"]), 1)
        self.assertEqual(result["components"]["architectural_walls"][0]["source_layers"], ["WALL+A", "WALL+B"])
        self.assertEqual(result["components"]["architectural_walls"][0]["source_handles"], ["A", "B"])

    def test_columns_are_independent_and_furniture_is_excluded(self) -> None:
        result = self.classify()
        handles = {
            handle
            for column in result["components"]["columns"]
            for handle in column["source_handles"]
        }
        self.assertTrue({"SC-1", "PC-1", "PC-2"}.issubset(handles))
        self.assertNotIn("CAB-1", handles)
        wall_handles = {
            handle
            for wall in result["components"]["architectural_walls"]
            for handle in str(wall.get("source_handle") or "").split("+")
        }
        self.assertTrue(handles.isdisjoint(wall_handles))

    def test_structural_and_general_walls_are_disjoint(self) -> None:
        result = self.classify()
        structural = {item["source_handle"] for item in result["components"]["structural_walls"]}
        general = {
            handle
            for item in result["components"]["architectural_walls"]
            for handle in str(item.get("source_handle") or "").split("+")
        }
        self.assertIn("SW-1", structural)
        self.assertIn("AW-1", general)
        self.assertTrue(structural.isdisjoint(general))
        self.assertEqual(result["validation"]["status"], "review_required")
        self.assertFalse(result["validation"]["failures"])

    def test_original_chinese_masonry_layer_is_an_architectural_wall(self) -> None:
        document = synthetic_document()
        document["entities"].append(line("MASONRY-1", "qz砌筑", (3000, 10000), (7000, 10000)))
        result = classify_document(document, copy.deepcopy(self.rules), "masonry.json")
        handles = {
            handle
            for wall in result["components"]["architectural_walls"]
            for handle in str(wall.get("source_handle") or "").split("+")
        }
        self.assertIn("MASONRY-1", handles)

    def test_structural_container_inherits_direct_geometry_but_not_nested_grid(self) -> None:
        document = synthetic_document()
        container, resolved = structural_container_fixture()
        document["entities"].append(container)
        document["resolved_instances"].extend(resolved)
        result = classify_document(document, copy.deepcopy(self.rules), "container.json")
        column_handles = {
            handle
            for column in result["components"]["columns"]
            for handle in column["source_handles"]
        }
        structural_handles = {item["source_handle"] for item in result["components"]["structural_walls"]}
        self.assertIn("STRUCT-ROOT/COL-OUTER", column_handles)
        self.assertIn("STRUCT-ROOT/WALL-1", structural_handles)
        self.assertNotIn("STRUCT-ROOT/GRID-LINE", structural_handles)
        self.assertEqual(result["generalization_guards"]["nested_structural_container_descendants_inherited"], 0)

    def test_explicit_wall_and_column_layers_inside_nested_blocks_are_materialized(self) -> None:
        document = synthetic_document()
        container, resolved = explicit_nested_semantic_fixture()
        document["entities"].append(container)
        document["resolved_instances"].extend(resolved)
        result = classify_document(document, copy.deepcopy(self.rules), "nested-semantics.json")
        wall_handles = {
            handle
            for wall in result["components"]["architectural_walls"]
            for handle in str(wall.get("source_handle") or "").split("+")
        }
        structural_handles = {item["source_handle"] for item in result["components"]["structural_walls"]}
        column_handles = {
            handle
            for column in result["components"]["columns"]
            for handle in column["source_handles"]
        }
        self.assertIn("NESTED-ROOT/SUBPLAN/INNER-WALL", wall_handles)
        self.assertIn("NESTED-ROOT/SUBPLAN/INNER-STRUCT-WALL", structural_handles)
        self.assertIn("NESTED-ROOT/SUBPLAN/INNER-COLUMN", column_handles)
        self.assertEqual(result["generalization_guards"]["explicit_nested_layer_entities_materialized"], 3)

    def test_win_layer_inside_nested_block_is_an_opening_not_a_wall(self) -> None:
        document = synthetic_document()
        container, resolved = explicit_nested_semantic_fixture()
        document["entities"].append(container)
        document["resolved_instances"].extend(resolved)
        result = classify_document(document, copy.deepcopy(self.rules), "nested-opening.json")
        opening = next(
            item
            for item in result["components"]["openings"]
            if item["source_handle"] == "NESTED-ROOT/SUBPLAN/WIN-1"
        )
        self.assertEqual(opening["source_layer"], "4WIN")
        self.assertEqual(opening["geometry_mode"], "block_reference_extent")
        self.assertEqual(opening["wall_adjacency_status"], "adjacent")
        wall_handles = {
            handle
            for wall in result["components"]["architectural_walls"]
            for handle in str(wall.get("source_handle") or "").split("+")
        }
        self.assertNotIn("NESTED-ROOT/SUBPLAN/WIN-1", wall_handles)

    def test_fallback_plan_domain_and_transformed_fixture_position(self) -> None:
        entities = [
            line("OUT-B", "qz砌筑", (0, 0), (10000, 0)),
            line("OUT-R", "qz砌筑", (10000, 0), (10000, 10000)),
            line("OUT-T", "qz砌筑", (10000, 10000), (0, 10000)),
            line("OUT-L", "qz砌筑", (0, 10000), (0, 0)),
            line("ROOM-L", "qz砌筑", (4000, 4000), (4000, 6000)),
            line("ROOM-R", "qz砌筑", (6000, 4000), (6000, 6000)),
            line("ROOM-B", "qz砌筑", (4000, 4000), (6000, 4000)),
            line("ROOM-T", "qz砌筑", (4000, 6000), (6000, 6000)),
            rectangle("COL-LAYER", "0-COL", 9000, 9000, 300, 300),
            {
                "type": "INSERT",
                "handle": "FIX-TRANSFORMED",
                "owner_block": "*Model_Space",
                "layer": "5fix",
                "block_name": "ANONYMOUS-FIXTURE",
                "position": {"x": -500000, "y": 700000},
                "local_extents": box(4800, 4800, 5200, 5200),
            },
        ]
        result = classify_document({"entities": entities, "resolved_instances": []}, copy.deepcopy(self.rules), "fallback.json")
        self.assertEqual(result["audits"]["plan_domains"][0]["source"], "architectural_wall_network_fallback")
        self.assertEqual(result["counts"]["sanitary_zones"], 1)
        self.assertEqual(result["audits"]["sanitary_enclosures"][0]["status"], "pass")
        self.assertIn(
            "COL-LAYER",
            {
                handle
                for column in result["components"]["columns"]
                for handle in column["source_handles"]
            },
        )


if __name__ == "__main__":
    unittest.main()

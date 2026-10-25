from __future__ import annotations

import math
import unittest
from pathlib import Path

from dwg_semantic.classifier import classify_document, load_rules
from dwg_semantic.geometry import Segment, entity_segments, merge_collinear


ROOT = Path(__file__).resolve().parents[1]


class GeometryTests(unittest.TestCase):
    def test_arc_tessellation_bounds_radial_chord_error(self) -> None:
        segments = entity_segments({"type": "ARC", "handle": "A", "layer": "WALL+CURVE",
                                    "center": {"x": 0, "y": 0}, "radius": 1000,
                                    "start_angle": 0, "end_angle": math.pi / 2}, max_curve_error_mm=0.5)
        self.assertGreater(len(segments), 1)
        for segment in segments:
            self.assertLessEqual(1000 - math.hypot(*segment.midpoint), 0.50000001)
            self.assertEqual(segment.as_dict()["source_layers"], ["WALL+CURVE"])
        self.assertAlmostEqual(segments[0].p1[0], 1000)
        self.assertAlmostEqual(segments[-1].p2[1], 1000)

    def test_degree_arc_wrapping_and_world_coordinates(self) -> None:
        segments = entity_segments({"type": "ARC", "source_handle": "A", "layer": "WALL",
                                    "center_world": {"x": 100, "y": 100}, "radius": 10,
                                    "start_angle_degrees": 350, "end_angle_degrees": 10}, max_curve_error_mm=0.01)
        self.assertAlmostEqual(segments[0].p1[0], 100 + 10 * math.cos(math.radians(350)))
        self.assertTrue(all(segment.p1[0] >= 109.84 for segment in segments))
        with self.assertRaises(ValueError):
            entity_segments({"type": "ARC", "center": {"x": 0, "y": 0}, "radius": 10,
                             "start_angle": 0, "end_angle": 1}, max_curve_error_mm=0)

    def test_bulge_sign_and_closed_final_edge(self) -> None:
        base = {"type": "LWPOLYLINE", "handle": "P", "layer": "WALL", "closed": True,
                "vertices": [{"x": -100, "y": 0, "bulge": 1}, {"x": 100, "y": 0, "bulge": -1}]}
        segments = entity_segments(base, max_curve_error_mm=0.25)
        self.assertEqual(segments[0].p1, (-100, 0))
        self.assertEqual(segments[-1].p2, (-100, 0))
        self.assertLess(min(segment.midpoint[1] for segment in segments), -99)
        self.assertGreater(len(segments), 4)
        base["closed"] = False
        base["vertices"][0]["bulge"] = -1
        negative = entity_segments(base, max_curve_error_mm=0.25)
        self.assertGreater(max(segment.midpoint[1] for segment in negative), 99)

    def test_affine_safe_samples_take_precedence_over_analytic_arc(self) -> None:
        segments = entity_segments({"type": "ARC", "source_handle": "R", "layer": "WALL",
                                    "center_world": {"x": 0, "y": 0}, "radius": 500,
                                    "start_angle": 0, "end_angle": math.pi,
                                    "curve_points_world": [{"x": 2, "y": 0}, {"x": 0, "y": 1}, {"x": -2, "y": 0}],
                                    "curve_error_mm": 0.5})
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].p1, (2, 0))
        self.assertEqual(segments[0].p2, (0, 1))

    def test_curve_chords_survive_minimum_length_filter(self) -> None:
        rules = load_rules(ROOT / "config/default_rules.json")
        arc = {"type": "ARC", "handle": "CURVE", "owner_block": "*Model_Space", "layer": "A-WALL",
               "center": {"x": 0, "y": 0}, "radius": 100,
               "start_angle": 0, "end_angle": math.pi / 2}
        before = entity_segments(arc)
        self.assertTrue(all(segment.length < rules["geometry"]["minimum_segment_length_mm"] for segment in before))
        result = classify_document({"entities": [arc]}, rules)
        self.assertEqual(len(result["components"]["architectural_walls"]), len(before))

    def test_exact_provenance_survives_repeated_merges(self) -> None:
        rules = load_rules(ROOT / "config/default_rules.json")["geometry"]
        original = [Segment((0, 0), (1000, 0), "A+1", "WALL+A"),
                    Segment((1000, 0), (2000, 0), "B", "WALL+B")]
        first = merge_collinear(original, rules)
        second = merge_collinear(first, rules)
        self.assertEqual(second[0].as_dict()["source_layers"], ["WALL+A", "WALL+B"])
        self.assertEqual(second[0].as_dict()["source_handles"], ["A+1", "B"])

    def test_repeated_block_handles_keep_per_instance_identity(self) -> None:
        segments = []
        for index in (1, 2):
            segments += entity_segments({"type": "LINE", "source_handle": "CHILD", "source_handles": ["CHILD"],
                                         "instance_path": f"ROOT{index}", "layer": "WALL",
                                         "start_world": {"x": 0, "y": 0}, "end_world": {"x": 1000, "y": 0}})
        rules = load_rules(ROOT / "config/default_rules.json")["geometry"]
        merged = merge_collinear(segments, rules)
        self.assertEqual(merged[0].source_handles, ["ROOT1/CHILD", "ROOT2/CHILD"])


if __name__ == "__main__":
    unittest.main()

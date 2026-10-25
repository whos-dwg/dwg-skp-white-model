from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dwg_semantic.intake import extract_document, _unit


def line(handle="A1", layer="A-WALL"):
    return {"type": "LINE", "handle": handle, "layer": layer, "owner_block": "*Model_Space",
            "start": {"x": 1., "y": 2., "z": 0.}, "end": {"x": 3., "y": 4., "z": 0.}}


class IntakeTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parents[1] / "work" / "test-intake-temp"
        test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=test_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def ingest(self, data, unit=None, name="input.json"):
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return extract_document(path, output_dir=self.root / (path.stem + "-output"), unit=unit)

    def test_legacy_json_requires_explicit_unit(self):
        doc = self.ingest({"entities": [line()]})
        self.assertTrue(any("units_unconfirmed" in issue for issue in doc["intake"]["blockers"]))
        accepted = self.ingest({"entities": [line()]}, unit="mm", name="confirmed.json")
        self.assertEqual(accepted["intake"]["blockers"], [])

    def test_unit_conversion_scales_coordinates_extents_area_but_not_angles_or_bulges(self):
        entity = line()
        entity.update({"area": 5., "radius": 3., "start_angle": 1.2, "bulges": [1., 0.], "normal": {"x": 0., "y": 0., "z": 1.},
                       "world_extents": {"min": {"x": 1., "y": 2.}, "max": {"x": 3., "y": 4.}}})
        result = self.ingest({"entities": [entity], "database": {"insertion_units": "Meters"}})
        actual = result["entities"][0]
        self.assertEqual(actual["start"]["x"], 1000.)
        self.assertEqual(actual["world_extents"]["max"]["y"], 4000.)
        self.assertEqual(actual["radius"], 3000.)
        self.assertEqual(actual["area"], 5000000.)
        self.assertEqual(actual["start_angle"], 1.2)
        self.assertEqual(actual["bulges"], [1., 0.])
        self.assertEqual(actual["normal"]["z"], 1.)

    def test_nested_layer_zero_inherits_nearest_effective_insertion_layer(self):
        root = {"type": "INSERT", "handle": "ROOT", "layer": "A-WALL", "owner_block": "*Model_Space"}
        nested = {"type": "BLOCK_REFERENCE", "source_handle": "INNER", "instance_path": "ROOT/INNER", "layer": "0", "root_space": "*Model_Space"}
        leaf = {**line("LEAF", "0"), "source_handle": "LEAF", "instance_path": "ROOT/INNER", "root_space": "*Model_Space"}
        explicit = {**line("EXPLICIT", "A-COLUMN"), "source_handle": "EXPLICIT", "instance_path": "ROOT/INNER", "root_space": "*Model_Space"}
        doc = self.ingest({"entities": [root], "resolved_instances": [leaf, explicit, nested]}, unit="mm")
        leaf, explicit = doc["resolved_instances"][:2]
        self.assertEqual(leaf["original_layer"], "0")
        self.assertEqual(leaf["effective_layer"], "A-WALL")
        self.assertEqual(leaf["block_layer"], "A-WALL")
        self.assertEqual(explicit["effective_layer"], "A-COLUMN")

    def test_numeric_point_arrays_are_scaled_and_canonicalized(self):
        entity = {"type": "LWPOLYLINE", "handle": "PL", "owner_block": "*Model_Space", "layer": "A-WALL",
                  "vertices": [[1, 2], [3, 4]], "curve_points_world": [[1, 2, 0], [3, 4, 0]], "bulges": [0.5, 0.]}
        doc = self.ingest({"entities": [entity], "units": "cm"})
        actual = doc["entities"][0]
        self.assertEqual(doc["intake"]["blockers"], [])
        self.assertEqual(actual["vertices"][0], {"x": 10., "y": 20.})
        self.assertEqual(actual["curve_points_world"][1], {"x": 30., "y": 40., "z": 0.})
        self.assertEqual(actual["bulges"], [0.5, 0.])

    def test_source_identity_does_not_conflate_two_placed_instances(self):
        a = {**line("LEAF"), "source_handle": "LEAF", "root_space": "*Model_Space", "instance_path": "I1"}
        b = {**a, "instance_path": "I2"}
        result = self.ingest({"entities": [], "resolved_instances": [a, b]}, unit="mm")
        a, b = result["resolved_instances"]
        self.assertEqual(a["source_id"], b["source_id"])
        self.assertNotEqual(a["entity_id"], b["entity_id"])

    def test_attached_attribute_instance_values_and_nested_positions_survive_normalization(self):
        attribute = {"type": "ATTRIB", "handle": "AT1", "source_handle": "AT1", "tag": "SLAB_THK",
                     "text": "THK=180", "is_instance_value": True, "annotation_role": "attached_attribute_instance",
                     "root_space": "*Model_Space", "instance_path": "ROOT/INNER", "layer": "ANNO-SLAB",
                     "original_layer": "0", "effective_layer": "ANNO-SLAB", "position_world": {"x": 10., "y": 20.}}
        definition = {"type": "ATTDEF", "handle": "DEF1", "owner_block": "SLAB-TAG", "layer": "0",
                      "tag": "SLAB_THK", "text": "DEFAULT-999", "default_text": "DEFAULT-999", "is_instance_value": False}
        metadata = {"schema": "dwg-annotation-instances-v1", "counts": {"placed_attached_attribute_values_read": 2}}
        second = {**attribute, "instance_path": "ROOT2/INNER"}
        result = self.ingest({"entities": [line(), definition], "resolved_instances": [attribute, second],
                              "annotation_extraction": metadata, "units": "cm"})
        first, second = result["resolved_instances"]
        self.assertEqual(first["text"], "THK=180")
        self.assertEqual(first["tag"], "SLAB_THK")
        self.assertEqual(first["position_world"], {"x": 100., "y": 200.})
        self.assertEqual(first["original_layer"], "0")
        self.assertEqual(first["effective_layer"], "ANNO-SLAB")
        self.assertNotEqual(first["entity_id"], second["entity_id"])
        self.assertTrue(first["is_instance_value"])
        self.assertFalse(result["entities"][1]["is_instance_value"])
        self.assertEqual(result["annotation_extraction"], metadata)
        self.assertEqual(result["intake"]["unsupported"], [])

    def test_mleader_content_and_linked_vertices_scale_without_changing_indices_or_text(self):
        leader = {"type": "MLEADER", "handle": "ML1", "owner_block": "*Model_Space", "layer": "ANNO-SLAB",
                  "text": "SLAB THK=180 FL+204.450", "position_world": {"x": 3., "y": 4.},
                  "leader_lines": [{"leader_index": 2, "leader_line_index": 7,
                                    "annotation_owner_handle": "ML1", "content_owner_handle": "ML1",
                                    "vertices_world": [{"x": 1., "y": 2.}, {"x": 2., "y": 3.}],
                                    "start_world": {"x": 1., "y": 2.}, "end_world": {"x": 2., "y": 3.}}]}
        result = self.ingest({"entities": [line(), leader], "units": "m"})
        actual = result["entities"][1]
        self.assertEqual(result["intake"]["unsupported"], [])
        self.assertEqual(actual["text"], leader["text"])
        self.assertEqual(actual["position_world"], {"x": 3000., "y": 4000.})
        linked = actual["leader_lines"][0]
        self.assertEqual(linked["leader_index"], 2)
        self.assertEqual(linked["leader_line_index"], 7)
        self.assertEqual(linked["annotation_owner_handle"], "ML1")
        self.assertEqual(linked["vertices_world"][0], {"x": 1000., "y": 2000.})
        self.assertEqual(linked["end_world"], {"x": 2000., "y": 3000.})

    def test_literal_plus_in_handle_and_layer_is_preserved(self):
        doc = self.ingest({"entities": [line("A+B", "WALL+FINISH")]}, unit="mm")
        entity = doc["entities"][0]
        self.assertEqual(entity["source_handles"], ["A+B"])
        self.assertEqual(entity["source_layers"], ["WALL+FINISH"])

    def test_hatch_boundary_evidence_scales_but_does_not_become_material(self):
        hatch={"type":"HATCH","handle":"H1","owner_block":"*Model_Space","layer":"Changes","unsupported":True,
               "boundary_role":"source_reference_evidence_only","boundary_loops":[{"supported":True,"closed":True,
               "loop_type":"External, Polyline","vertices_world":[{"x":1.,"y":2.},{"x":3.,"y":2.},{"x":3.,"y":4.}],"bulges":[0.,0.,0.]}]}
        result=self.ingest({"entities":[line(),hatch],"units":"m"})
        actual=result["entities"][1]
        self.assertEqual(actual["boundary_loops"][0]["vertices_world"][0],{"x":1000.,"y":2000.})
        self.assertEqual(actual["boundary_loops"][0]["bulges"],[0.,0.,0.])
        self.assertTrue(actual["unsupported"])
        self.assertEqual(actual["boundary_role"],"source_reference_evidence_only")

    def test_empty_or_paper_only_extraction_is_blocked(self):
        doc = self.ingest({"entities": []}, unit="mm")
        self.assertIn("empty_model_space: no placed objects extracted", doc["intake"]["blockers"])
        entity = {**line(), "owner_block": "*Paper_Space"}
        doc = self.ingest({"entities": [entity]}, unit="mm", name="paper.json")
        self.assertEqual(doc["intake"]["stats"]["supported_geometry"], 0)

    def test_missing_xref_blocks_even_when_other_geometry_exists(self):
        doc = self.ingest({"entities": [line()], "blocks": [{"name": "building", "is_xref": True, "path": "missing.dwg", "xref_status": "Unresolved"}]}, unit="mm")
        self.assertEqual(doc["intake"]["dependencies"][0]["status"], "missing")
        self.assertTrue(any("xref_missing" in issue for issue in doc["intake"]["blockers"]))

    def test_dependency_hash_and_unloaded_status_are_preserved(self):
        dependency = self.root / "part.dwg"
        dependency.write_bytes(b"test dependency")
        doc = self.ingest({"entities": [line()], "blocks": [{"is_xref": True, "path": dependency.name, "xref_status": "Unloaded"}]}, unit="mm")
        self.assertEqual(doc["intake"]["dependencies"][0]["status"], "unloaded")
        self.assertEqual(len(doc["intake"]["dependencies"][0]["sha256"]), 64)

    def test_unsupported_objects_are_not_silently_discarded(self):
        proxy = {"type": "ACAD_PROXY_ENTITY", "handle": "B1", "layer": "A-WALL", "owner_block": "*Model_Space"}
        doc = self.ingest({"entities": [line(), proxy]}, unit="mm")
        self.assertEqual(doc["intake"]["unsupported"][0]["handle"], "B1")
        self.assertEqual(doc["intake"]["stats"]["unsupported_objects"], 1)
        self.assertEqual(doc["entities"][1]["type"], "ACAD_PROXY_ENTITY")

    def test_engine_failure_is_explicit_with_no_stale_output(self):
        source = self.root / "source.dwg"
        source.write_bytes(b"AC1032not a valid dwg")
        output = self.root / "engine-output"
        output.mkdir()
        (output / "normalized.json").write_text('{"entities":[{"old":true}]}', encoding="utf-8")
        with patch("dwg_semantic.intake._autocad_extract", side_effect=RuntimeError("engine unavailable")):
            doc = extract_document(source, output_dir=output, unit="mm")
        self.assertEqual(doc["entities"], [])
        self.assertTrue(any("engine unavailable" in issue for issue in doc["intake"]["blockers"]))
        self.assertEqual(source.read_bytes(), b"AC1032not a valid dwg")

    def test_corrupt_json_is_a_blocked_report(self):
        path = self.root / "corrupt.json"
        path.write_text("{bad", encoding="utf-8")
        doc = extract_document(path, output_dir=self.root / "corrupt-out", unit="mm")
        self.assertTrue(any("extraction_failed" in issue for issue in doc["intake"]["blockers"]))

    def test_incomplete_line_geometry_is_blocked(self):
        entity = line()
        del entity["end"]
        doc = self.ingest({"entities": [entity]}, unit="mm")
        self.assertTrue(any("geometry_payload_missing" in issue for issue in doc["intake"]["blockers"]))

    def test_nonfinite_geometry_saved_as_blocked_valid_json(self):
        entity = line()
        entity["start"]["x"] = float("nan")
        doc = self.ingest({"entities": [entity]}, unit="mm")
        self.assertIn("non_finite_geometry", doc["intake"]["blockers"])
        self.assertIsNone(doc["entities"][0]["start"]["x"])
        json.loads((self.root / "input-output" / "normalized.json").read_text(encoding="utf-8"))

    def test_normalized_mm_is_not_double_scaled(self):
        doc = self.ingest({"entities": [line()], "intake": {"units": "mm"}}, unit="m")
        self.assertEqual(doc["entities"][0]["start"]["x"], 1.)
        self.assertTrue(any("already_normalized" in issue for issue in doc["intake"]["blockers"]))

    def test_input_is_never_output_target(self):
        path = self.root / "normalized.json"
        path.write_text('{"entities": []}', encoding="utf-8")
        with self.assertRaises(ValueError):
            extract_document(path, output_dir=path.parent, unit="mm")
        self.assertEqual(path.read_text(encoding="utf-8"), '{"entities": []}')

    def test_autodesk_unit_names(self):
        self.assertEqual(_unit("Millimeters"), "mm")
        self.assertEqual(_unit(6), "m")
        self.assertEqual(_unit("USSurveyFeet"), "us-ft")


if __name__ == "__main__":
    unittest.main()

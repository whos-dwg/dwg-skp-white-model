import tempfile
import unittest
from pathlib import Path

from dwg_semantic.modeling import apply_selection_opening_patches
from dwg_semantic.selection_openings import SelectionOpeningError, propose_selection_openings


def wall(identity="wall-1"):
    return {
        "id": identity,
        "name": identity,
        "category": "architectural_wall",
        "source_layer": "A-WALL",
        "source_layers": ["A-WALL"],
        "source_handles": ["W1"],
        "source_entity_ids": ["source::W1"],
        "source_id": "source.dwg",
        "tag": "A-WALL",
        "mode": "solid_extrusion",
        "outer": [[0, 0], [4000, 0], [4000, 200], [0, 200]],
        "holes": [],
        "base_z_mm": 0,
        "height_mm": 3000,
        "area_mm2": 800000,
        "evidence": ["fixture"],
    }


class SelectionOpeningsTests(unittest.TestCase):
    def setUp(self):
        workspace_temp = Path("work/test-selection-openings").resolve()
        workspace_temp.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=workspace_temp)
        self.source = Path(self.directory.name) / "source.dwg"
        self.source.write_bytes(b"fixture")
        self.document = {
            "intake": {
                "source_path": str(self.source),
                "source_sha256": "fixture-source-sha",
                "units": "mm",
                "scale_to_mm": 1,
            },
            "entities": [
                {"type": "INSERT", "handle": "D1", "source_handle": "D1", "entity_id": "source::D1",
                 "source_file": str(self.source), "owner_block": "*Model_Space", "layer": "A-DOOR",
                 "effective_layer": "A-DOOR", "instance_path": ""},
                {"type": "INSERT", "handle": "D2", "source_handle": "D2", "entity_id": "source::D2",
                 "source_file": str(self.source), "owner_block": "*Model_Space", "layer": "A-DOOR",
                 "effective_layer": "A-DOOR", "instance_path": ""},
            ],
            "resolved_instances": [],
        }
        self.plan = {"schema": "dwg-skp-model-v2", "units": "mm", "groups": [wall()],
                     "references": [], "blockers": [], "warnings": [], "excluded": [],
                     "tag_mapping": [], "opening_audit": []}

    def tearDown(self):
        self.directory.cleanup()

    def selection(self, *, two=False):
        objects = [{
            "selection_index": 0,
            "handle": "D1",
            "object_type": "AcDbBlockReference",
            "properties": {"Layer": "A-DOOR"},
            "bounding_box": {"coordinate_system": "WCS", "minimum": [1000, -100, 0],
                             "maximum": [1900, 300, 0]},
        }]
        if two:
            objects.append({
                "selection_index": 1,
                "handle": "D2",
                "object_type": "AcDbBlockReference",
                "properties": {"Layer": "A-DOOR"},
                "bounding_box": {"coordinate_system": "WCS", "minimum": [2500, -100, 0],
                                 "maximum": [3400, 300, 0]},
            })
        return {
            "success": True,
            "schema_version": "1.0",
            "status": "SELECTED",
            "observed_at_utc": "2026-10-20T00:00:00Z",
            "document": {"name": self.source.name, "path": str(self.source)},
            "selection_source": "PickfirstSelectionSet",
            "selection_scope": "top-level selected entities; nested block contents and subentities are not expanded",
            "selection_count": len(objects),
            "returned_count": len(objects),
            "truncated": False,
            "objects": objects,
        }

    def test_proposes_host_clipped_opening_from_selected_block_extent(self):
        proposal = propose_selection_openings(self.document, self.plan, self.selection(),
                                              height_mm=2100, opening_type="door")
        self.assertEqual("ready_for_review", proposal["status"])
        self.assertFalse(proposal["blockers"])
        patch = proposal["patches"][0]
        self.assertEqual(["wall-1"], patch["host_ids"])
        self.assertEqual(180000, self._area(patch["footprint"]))
        self.assertEqual("selection_wcs_bbox_intersect_reviewed_host_material", patch["footprint_basis"])

    def test_apply_patch_rebuilds_only_host_parts_and_keeps_clearance(self):
        proposal = propose_selection_openings(self.document, self.plan, self.selection(), height_mm=2100)
        decisions = {"source_sha256": "fixture-source-sha", "opening_patches": proposal["patches"]}
        result = apply_selection_opening_patches(self.plan, decisions)
        self.assertFalse(result["blockers"])
        self.assertEqual(1, len(result["selection_opening_audit"]))
        self.assertEqual(3, len(result["groups"]))
        roles = {group["id"].split("/")[-2] for group in result["groups"]}
        self.assertEqual({"side", "lintel"}, roles)
        self.assertTrue(all("wall-1" in group["host_lineage_ids"] for group in result["groups"]))

    def test_multiple_openings_reuse_host_lineage_in_one_revision(self):
        proposal = propose_selection_openings(self.document, self.plan, self.selection(two=True), height_mm=2100)
        decisions = {"source_sha256": "fixture-source-sha", "opening_patches": proposal["patches"]}
        result = apply_selection_opening_patches(self.plan, decisions)
        self.assertFalse(result["blockers"])
        self.assertEqual(2, len(result["selection_opening_audit"]))
        self.assertEqual({patch["id"] for patch in proposal["patches"]},
                         {item["id"] for item in result["selection_opening_audit"]})

    def test_rejects_truncated_selection(self):
        selection = self.selection()
        selection["truncated"] = True
        with self.assertRaisesRegex(SelectionOpeningError, "截斷"):
            propose_selection_openings(self.document, self.plan, selection, height_mm=2100)

    def test_rejects_selection_from_another_drawing(self):
        selection = self.selection()
        selection["document"]["path"] = str(Path(self.directory.name) / "other.dwg")
        with self.assertRaisesRegex(SelectionOpeningError, "不是這個白模"):
            propose_selection_openings(self.document, self.plan, selection, height_mm=2100)

    def test_same_root_handle_in_another_source_is_not_attached(self):
        self.document["resolved_instances"].append({
            "type": "LINE", "source_handle": "LEAF", "entity_id": "xref:D1:LEAF",
            "source_file": str(Path(self.directory.name) / "xref.dwg"),
            "instance_path": "D1/NESTED", "layer": "XREF-DOOR",
        })
        proposal = propose_selection_openings(self.document, self.plan, self.selection(), height_mm=2100)
        self.assertNotIn("xref:D1:LEAF", proposal["patches"][0]["source_entity_ids"])

    def test_rejects_unknown_opening_type_from_api(self):
        with self.assertRaisesRegex(SelectionOpeningError, "開口類型"):
            propose_selection_openings(self.document, self.plan, self.selection(),
                                       height_mm=2100, opening_type="void-ish")

    @staticmethod
    def _area(footprint):
        from shapely.geometry import Polygon
        return Polygon(footprint["outer"], footprint.get("holes", [])).area


if __name__ == "__main__":
    unittest.main()

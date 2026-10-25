import copy
import unittest

from dwg_semantic.live_repair import (
    LiveRepairError,
    build_live_repair_plan,
    validate_plan_digest,
)


SOURCE = "D:/Models/source.dwg"


def provenance(identity="wall-1", *, source_handle="W1", category="architectural_wall",
               outer=None, height=3000):
    outer = outer or [[0, 0], [4000, 0], [4000, 200], [0, 200]]
    return {
        "id": identity,
        "name": identity,
        "category": category,
        "tag": "A-WALL",
        "source_layer": "A-WALL",
        "source_layers": ["A-WALL"],
        "source_handles": [source_handle],
        "source_entity_ids": [f"source::{source_handle}"],
        "outer": outer,
        "holes": [],
        "base_z_mm": 0,
        "height_mm": height,
        "mode": "solid_extrusion",
        "evidence": ["fixture"],
    }


def live_entity(identity="wall-1", *, entity_id="entity:101", source_handle="W1",
                category="architectural_wall", outer=None, height=3000, selected=False):
    data = provenance(identity, source_handle=source_handle, category=category,
                      outer=outer, height=height)
    return {
        "entity_id": entity_id,
        "parent_entity_id": "entity:20",
        "group_id": identity,
        "name": identity,
        "category": category,
        "tag": data["tag"],
        "material": "DWG white model",
        "hidden": False,
        "locked": False,
        "manifold": True,
        "volume_mm3": 4000 * 200 * height,
        "base_z_mm": 0,
        "height_mm": height,
        "footprint": {"outer": data["outer"], "holes": []},
        "signature": "sig-" + identity,
        "provenance": data,
        "selected": selected,
    }


def snapshot(entities=None, selection=None):
    return {
        "schema": "dwg-skp-live-snapshot-v1",
        "status": "ready",
        "model": {"guid": "model-guid", "path": "D:/Models/current.skp", "name": "current"},
        "root": {
            "entity_id": "entity:10",
            "payload_sha256": "payload-sha",
            "source_manifest": {"source_path": SOURCE, "source_sha256": "source-sha",
                                "units": "mm", "scale_to_mm": 1},
            "world_transform": {
                "origin_mm": [0.0, 0.0, 0.0],
                "xaxis": [1.0, 0.0, 0.0],
                "yaxis": [0.0, 1.0, 0.0],
                "zaxis": [0.0, 0.0, 1.0],
            },
        },
        "containers": [{"entity_id": "entity:20", "parent_entity_id": "entity:10",
                        "name": "architectural_wall"}],
        "entities": [live_entity()] if entities is None else entities,
        "selection_entity_ids": selection or [],
    }


def document(*, exact_door=False):
    entities = [{
        "type": "INSERT", "handle": "D1", "source_handle": "D1",
        "entity_id": "source::D1", "source_file": SOURCE,
        "owner_block": "*Model_Space", "layer": "A-DOOR",
        "effective_layer": "A-DOOR", "instance_path": "",
    }]
    resolved = []
    if exact_door:
        resolved.append({
            "type": "LWPOLYLINE", "handle": "LEAF", "source_handle": "LEAF",
            "entity_id": "source:D1:LEAF", "source_file": SOURCE,
            "instance_path": "D1", "layer": "A-WALL", "effective_layer": "A-WALL",
            "closed": True,
            "vertices_world": [[500, 0], [1600, 0], [1600, 200], [500, 200]],
        })
    return {
        "intake": {"source_path": SOURCE, "source_sha256": "source-sha",
                   "units": "mm", "scale_to_mm": 1},
        "entities": entities,
        "resolved_instances": resolved,
    }


def selection():
    return {
        "success": True,
        "status": "SELECTED",
        "observed_at_utc": "2026-10-20T00:00:00Z",
        "document": {"path": SOURCE, "name": "source.dwg"},
        "selection_source": "PickfirstSelectionSet",
        "selection_count": 1,
        "returned_count": 1,
        "truncated": False,
        "objects": [{
            "selection_index": 0,
            "handle": "D1",
            "object_type": "AcDbBlockReference",
            "properties": {"Layer": "A-DOOR"},
            "bounding_box": {"coordinate_system": "WCS", "minimum": [500, -100, 0],
                             "maximum": [1600, 300, 0]},
        }],
    }


class LiveRepairTests(unittest.TestCase):
    def test_opening_plan_replaces_active_wall_without_creating_skp(self):
        plan = build_live_repair_plan(
            document(), selection(), snapshot(), action="cut_opening",
            height_mm=2100, base_z_mm=0, opening_type="door",
        )
        self.assertEqual("ready_to_apply", plan["status"])
        self.assertEqual(["entity:101"], [row["entity_id"] for row in plan["targets"]])
        self.assertEqual(3, len(plan["create"]))
        self.assertFalse(plan["write_policy"]["create_new_skp"])
        self.assertFalse(plan["write_policy"]["save_model"])
        self.assertTrue(plan["write_policy"]["single_undo_operation"])
        self.assertEqual(plan["plan_sha256"], validate_plan_digest(plan))

    def test_add_missing_uses_exact_cad_contour_and_rejects_bbox_only(self):
        empty = snapshot(entities=[])
        with self.assertRaisesRegex(LiveRepairError, "封閉輪廓"):
            build_live_repair_plan(
                document(), selection(), empty, action="add_missing",
                category="architectural_wall", height_mm=3000,
            )
        plan = build_live_repair_plan(
            document(exact_door=True), selection(), empty, action="add_missing",
            category="architectural_wall", height_mm=3000,
        )
        self.assertFalse(plan["targets"])
        self.assertEqual(1, len(plan["create"]))
        self.assertAlmostEqual(220000 * 3000, plan["create"][0]["mesh"]["expected_volume_mm3"])

    def test_replace_wrong_contour_uses_current_sketchup_selection(self):
        wrong = live_entity(source_handle="OLD")
        live = snapshot([wrong], selection=["entity:101"])
        plan = build_live_repair_plan(
            document(exact_door=True), selection(), live,
            action="replace_from_cad", height_mm=3200,
        )
        self.assertEqual("entity:101", plan["targets"][0]["entity_id"])
        replacement = plan["create"][0]
        self.assertEqual("wall-1", replacement["group_id"])
        self.assertEqual(3200, replacement["height_mm"])
        self.assertEqual([[500.0, 0.0], [1600.0, 0.0], [1600.0, 200.0], [500.0, 200.0]],
                         replacement["footprint"]["outer"])

    def test_remove_wrong_requires_selected_source_bound_leaf(self):
        with self.assertRaisesRegex(LiveRepairError, "先在 SketchUp 選取"):
            build_live_repair_plan(document(), None, snapshot(), action="remove_wrong")
        plan = build_live_repair_plan(
            document(), None, snapshot(selection=["entity:101"]), action="remove_wrong",
        )
        self.assertEqual(1, len(plan["targets"]))
        self.assertFalse(plan["create"])

    def test_changed_plan_digest_is_rejected(self):
        plan = build_live_repair_plan(
            document(), selection(), snapshot(), action="cut_opening", height_mm=2100,
        )
        changed = copy.deepcopy(plan)
        changed["create"][0]["height_mm"] = 999
        with self.assertRaisesRegex(LiveRepairError, "雜湊不符"):
            validate_plan_digest(changed)

    def test_moved_root_is_rejected_instead_of_guessing_alignment(self):
        live = snapshot()
        live["root"]["world_transform"]["origin_mm"] = [100.0, 0.0, 0.0]
        with self.assertRaisesRegex(LiveRepairError, "根群組已被移動"):
            build_live_repair_plan(
                document(), selection(), live, action="cut_opening", height_mm=2100,
            )


if __name__ == "__main__":
    unittest.main()

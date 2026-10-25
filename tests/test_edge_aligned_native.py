from __future__ import annotations

import copy
import ctypes as C
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dwg_semantic.modeling import (_saved_edge_aligned_slab_audit, _saved_slab_audit, _solid_overlaps,
                                  validate_slab_payload, write_model)
from dwg_semantic.slab_modeling import prepare_slabs
from test_edge_aligned_risers import box, edge_aligned_fixture, shape, zone


def payload(zones=None, *, visible=True, precision=None, **options):
    document, config = edge_aligned_fixture(zones, **options)
    if precision is not None:
        config["systems"][0]["step_risers"]["geometry_precision_mm"] = precision
    result = prepare_slabs(document, config)
    return {"schema": "dwg-skp-model-v2", "units": "mm", **result, "references": [],
            "source_manifest": {"kind": "synthetic edge-aligned native fixture"},
            "coordinate_policy": "Synthetic original source coordinates in mm", "presentation": {"overhead_visible": visible}}


def saved_rows(plan):
    return [{"id": group["id"], "category": group["category"], "tag": group["tag"], "footprint": shape(group),
             "base_z_mm": group["base_z_mm"], "top_z_mm": group["top_z_mm"], "volume_mm3": group["volume_mm3"]}
            for group in plan["groups"]]


def native_panel_vertices(path, panel_ids):
    """Read actual group face vertices, independently of the prepared payload."""
    from dwg_semantic.skp_native import Native, P3, utf
    result = {}
    with Native() as api:
        model = api.create("SUModelCreateFromFile", utf(path))
        try:
            def walk(entities):
                for group in api.refs("SUEntitiesGetNumGroups", "SUEntitiesGetGroups", entities):
                    children = api.get("SUGroupGetEntities", group)
                    try:
                        identity = api.read_metadata(api.api.SUGroupToEntity(group), "id")
                    except RuntimeError:
                        identity = None
                    if identity in panel_ids:
                        vertices = []
                        faces = api.refs("SUEntitiesGetNumFaces", "SUEntitiesGetFaces", children)
                        for face in faces:
                            loop = api.get("SUFaceGetOuterLoop", face)
                            for vertex in api.refs("SULoopGetNumVertices", "SULoopGetVertices", loop):
                                point = P3()
                                api.call("SUVertexGetPosition", vertex, C.byref(point))
                                vertices.append([point.x * 25.4, point.y * 25.4, point.z * 25.4])
                        result[identity] = {"vertices": vertices, "face_count": len(faces), "edge_count": len(api.edges(children))}
                    walk(children)
            walk(api.get("SUModelGetEntities", model))
        finally:
            api.call("SUModelRelease", C.byref(model))
    return result


class EdgeAlignedNativeTests(unittest.TestCase):
    def test_precision_requires_reproducible_raw_geometry_and_preserved_identity(self):
        plan = payload([zone("low", box(0, 0, 500.0045, 1000), -600)], precision=.05)
        self.assertEqual([], plan["blockers"])
        self.assertEqual([], validate_slab_payload(plan))
        record = plan["slab_audit"]["geometry_precision"][0]
        self.assertGreater(record["max_vertex_displacement_mm"], 0)
        for tamper in ("grid", "raw", "movement", "tag", "remove_evidence"):
            with self.subTest(tamper=tamper):
                broken = copy.deepcopy(plan)
                evidence = broken["slab_audit"]["geometry_precision"][0]
                if tamper == "grid":
                    evidence["grid_size_mm"] = .1
                elif tamper == "raw":
                    evidence["raw_original_groups"][0]["outer"][0][0] += 5
                elif tamper == "movement":
                    evidence["components"][0]["max_vertex_displacement_mm"] = 0
                elif tamper == "tag":
                    evidence["normalized_original_groups"][0]["tag"] = "Invented"
                else:
                    del broken["slab_audit"]["geometry_precision"]
                self.assertTrue(any(row["code"] == "invalid_slab_geometry_precision" for row in validate_slab_payload(broken)))

    def test_common_noded_face_owners_and_bands_cannot_be_self_asserted(self):
        oblique = {"outer": [[0, 0], [300.0045, 0], [700.0045, 1000], [0, 1000]], "holes": []}
        plan = payload([zone("low", oblique, -600)], precision=.05)
        self.assertEqual([], plan["blockers"])
        self.assertEqual([], validate_slab_payload(plan))
        for tamper in ("owner", "attribution", "rule", "raw_edge", "canonical_band"):
            with self.subTest(tamper=tamper):
                broken = copy.deepcopy(plan)
                overlay = broken["slab_audit"]["geometry_precision"][0]["edge_overlay"]
                if tamper == "owner":
                    overlay["atomic_faces"][0]["original_component_id"] = "unrelated-source"
                elif tamper == "attribution":
                    overlay["atomic_faces"][0]["requested_connection_ids"] = ["unapproved-edge"]
                elif tamper == "rule":
                    overlay["source_owner_rule"] = "trust_prepared_claim"
                elif tamper == "raw_edge":
                    broken["slab_audit"]["step_risers"][0]["raw_shared_boundary"]["lines"][0][0][0] += 1
                else:
                    overlay["normalized_requested_bands"][0]["footprint"]["outer"][0][0] += 1
                self.assertTrue(any(row["code"] == "invalid_slab_geometry_precision" for row in validate_slab_payload(broken)))

    def test_source_boundary_and_lower_side_tampering_are_blocking(self):
        good = payload()
        self.assertEqual([], validate_slab_payload(good))
        wrong_side = copy.deepcopy(good)
        panel = next(group for group in wrong_side["groups"] if group["slab_role"] == "slab_step_riser")
        panel["outer"] = [[point[0] + 200, point[1]] for point in panel["outer"]]
        failures = validate_slab_payload(wrong_side)
        self.assertTrue(any(error["code"] == "invalid_edge_aligned_slab_audit" for error in failures))
        self.assertTrue(any("LOWER side" in error["detail"]["check"] for error in failures if error["code"] == "invalid_edge_aligned_slab_audit"))
        forged_edge = copy.deepcopy(good)
        row = forged_edge["slab_audit"]["step_risers"][0]
        row["shared_boundary"]["lines"] = [[[point[0] + 10, point[1]] for point in line] for line in row["shared_boundary"]["lines"]]
        self.assertTrue(validate_slab_payload(forged_edge))

    def test_gap_only_or_intermediate_sections_cannot_claim_full_depth_profile(self):
        plan = payload()
        broken = copy.deepcopy(plan)
        panel = next(group for group in broken["groups"] if group["slab_role"] == "slab_step_riser")
        panel.update(base_z_mm=-600, top_z_mm=-200, height_mm=400)
        self.assertTrue(validate_slab_payload(broken))
        rows = saved_rows(plan)
        next(row for row in rows if row["id"] == panel["id"])["top_z_mm"] = -200
        audit = _saved_edge_aligned_slab_audit(plan, rows)
        self.assertFalse(audit["all_pass"])
        self.assertTrue(any(check["check"] == "edge saved panel is one complete original section without intermediate slicing"
                            and not check["pass"] for check in audit["checks"]))

    def test_untrimmed_lower_plate_and_missing_original_snapshot_are_rejected(self):
        plan = payload()
        snapshot = plan["slab_audit"]["edge_aligned_replacements"][0]
        lower = min(snapshot["original_groups"], key=lambda group: group["top_z_mm"])
        mapping = next(row for row in snapshot["replacements"] if row["original_component_id"] == lower["id"])
        part = next(group for group in plan["groups"] if group["id"] == mapping["result_horizontal_component_ids"][0])
        part.update(outer=copy.deepcopy(lower["outer"]), holes=copy.deepcopy(lower["holes"]))
        self.assertTrue(_solid_overlaps(plan["groups"]))
        self.assertTrue(validate_slab_payload(plan))
        plan = payload()
        plan["slab_audit"]["edge_aligned_replacements"][0]["original_groups"].pop()
        self.assertTrue(validate_slab_payload(plan))

    def test_holes_and_narrow_absorbed_hosts_preserve_complete_material(self):
        cases = [payload([zone("low", box(450, 0, 100, 1000), -600)]),
                 payload(openings=[{"id": "shaft", "footprint": box(400, 400, 200, 200),
                                    "source_entity_ids": ["shaft"], "applies_to_system_id": "roof"}])]
        for plan in cases:
            self.assertEqual([], validate_slab_payload(plan))
            self.assertTrue(_saved_slab_audit(plan, saved_rows(plan))["all_pass"])

    @unittest.skipUnless(os.environ.get("DWG_RUN_NATIVE_TESTS") == "1", "Opt in to installed SketchUp native integration")
    def test_native_subresolution_material_is_preserved_and_unconditioned_loss_still_rejected(self):
        # An internal triangular sliver becomes a separate high plate and its
        # riser. The unconditioned native kernel collapses it. Keep that negative
        # control, then prove representation-only conditioning preserves it.
        low = box(0, 0, 500, 1000)
        low["holes"] = [[[100, 400], [100.00904, 600], [100.00904, 400]]]
        raw = payload([zone("low", low, -600)])
        clean = payload([zone("low", low, -600)], precision=.05)
        self.assertEqual([], raw["blockers"])
        self.assertEqual([], validate_slab_payload(raw))
        self.assertEqual([], clean["blockers"])
        self.assertEqual([], validate_slab_payload(clean))
        evidence = clean["slab_audit"]["geometry_precision"][0]
        self.assertTrue(any(row["raw_hole_count"] != row["normalized_hole_count"] for row in evidence["components"]))
        self.assertTrue(any(row["status"] == "collapsed" for row in evidence["components"]))
        test_root = Path(__file__).resolve().parents[1] / "work/modeling-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="synthetic-edge-precision-", dir=test_root) as directory:
            failed = Path(directory) / "raw-sliver-must-not-publish.skp"
            from dwg_semantic.skp_native import Native
            def unconditioned(api,group,entities,faces,**kwargs):
                api.geometry(entities,faces)
                return {'scale':1,'transform':[1.,0.,0.,0.,0.,1.,0.,0.,0.,0.,1.,0.,0.,0.,0.,1.]}
            with patch.object(Native,'solid_geometry',unconditioned):
                with self.assertRaisesRegex(RuntimeError, "Reopened SKP failed verification"):
                    write_model(raw, failed)
            self.assertFalse(failed.exists())
            raw_before=copy.deepcopy(raw)
            raw_qa=write_model(raw,Path(directory)/'raw-sliver-preserved-without-source-movement.skp')
            self.assertTrue(raw_qa['all_pass'])
            self.assertTrue(raw_qa['native_geometry_conditioning'])
            self.assertEqual(raw_before,raw)
            actual = Path(directory) / "explicit-bounded-grid.skp"
            qa = write_model(clean, actual)
            self.assertTrue(qa["all_pass"], json.dumps(qa, ensure_ascii=False))
            self.assertTrue(qa["slab_audit"]["geometry_precision"]["all_pass"])
            self.assertEqual([], qa["overlap_audit"])
            self.assertTrue(all(row["pass"] for row in qa["checks"] if row["check"] in {
                "closed solid topology", "signed solid volume", "saved footprint and holes match approval"}))
            oblique = {"outer": [[0, 0], [300.0045, 0], [700.0045, 1000], [0, 1000]], "holes": []}
            oblique_plan = payload([zone("low", oblique, -600)], precision=.05)
            qa = write_model(oblique_plan, Path(directory) / "common-graph-oblique.skp")
            self.assertTrue(qa["all_pass"])
            self.assertEqual([], qa["overlap_audit"])

    @unittest.skipUnless(os.environ.get("DWG_RUN_NATIVE_TESTS") == "1", "Opt in to installed SketchUp native integration")
    def test_native_complete_sections_and_original_edge_survive_actual_reopen(self):
        l_shape = {"outer": [[0, 0], [1000, 0], [1000, 400], [400, 400], [400, 1000], [0, 1000]], "holes": []}
        oblique = {"outer": [[0, 0], [300, 0], [700, 1000], [0, 1000]], "holes": []}
        redundant = {"outer": [[0, 0], [500, 0], [500, 200], [500, 400], [500, 600], [500, 800], [500, 1000], [0, 1000]], "holes": []}
        plans = [payload(), payload([zone("low", box(0, 0, 500, 1000), -50)]),
                 payload([zone("low", l_shape, -600)]),
                 payload([zone("low", box(0, 0, 500, 1000), -600), zone("deep", box(500, 0, 500, 500), -1000)]),
                 payload([zone("low", oblique, -500)]),
                 payload([zone("low", box(450, 0, 100, 1000), -600)]),
                 payload(openings=[{"id": "shaft", "footprint": box(350, 400, 100, 200),
                                    "source_entity_ids": ["shaft"], "applies_to_system_id": "roof"}]),
                 payload([zone("low", redundant, -600)])]
        test_root = Path(__file__).resolve().parents[1] / "work/modeling-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="synthetic-edge-full-panels-", dir=test_root) as directory:
            for index, plan in enumerate(plans):
                with self.subTest(case=index):
                    self.assertEqual([], plan["blockers"])
                    self.assertEqual([], validate_slab_payload(plan))
                    destination = Path(directory) / f"full-panel-{index}.skp"
                    qa = write_model(plan, destination)
                    self.assertTrue(qa["all_pass"], json.dumps(qa, ensure_ascii=False))
                    self.assertTrue(qa["slab_audit"]["edge_aligned_replacements"])
                    self.assertEqual([], qa["overlap_audit"])
                    panels = {group["id"]: group for group in plan["groups"] if group["slab_role"] == "slab_step_riser"}
                    actual_vertices = native_panel_vertices(destination, set(panels))
                    self.assertEqual(set(panels), set(actual_vertices))
                    for identity, geometry in actual_vertices.items():
                        points = geometry["vertices"]
                        self.assertEqual({round(panels[identity]["base_z_mm"], 6), round(panels[identity]["top_z_mm"], 6)},
                                         {round(point[2], 6) for point in points})
                        if index in (0, 1, 7):
                            self.assertEqual((6, 12), (geometry["face_count"], geometry["edge_count"]))
                    self.assertTrue(any(check["check"] == "overhead slab parent visibility matches presentation"
                                        and check["pass"] for check in qa["checks"]))

    @unittest.skipUnless(os.environ.get("DWG_RUN_NATIVE_TESTS") == "1", "Opt in to installed SketchUp native integration")
    def test_visibility_defaults_and_source_snapshot_metadata_remain_compatible(self):
        plan = payload()
        plan["wall_under_roof_audit"] = {"schema": "synthetic-metadata-only", "fixture": "roundtrip source-bound record"}
        test_root = Path(__file__).resolve().parents[1] / "work/modeling-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="synthetic-edge-presentation-", dir=test_root) as directory:
            for visible in (False, True):
                plan["presentation"] = {"overhead_visible": visible}
                qa = write_model(plan, Path(directory) / f"roof-visible-{visible}.skp")
                self.assertTrue(qa["all_pass"])
                self.assertTrue(any(check["check"] == "saved model wall_under_roof_audit metadata preserved" and check["pass"]
                                    for check in qa["checks"]))
            del plan["presentation"]
            qa = write_model(plan, Path(directory) / "legacy-visibility-default.skp")
            self.assertTrue(any(check["check"] == "overhead slab parent hidden in default overview" and check["pass"] for check in qa["checks"]))
            plan["presentation"] = {"overhead_visible": "true"}
            with self.assertRaisesRegex(ValueError, "must be a boolean"):
                write_model(plan, Path(directory) / "invalid-visibility.skp")

    @unittest.skipUnless(os.environ.get("DWG_RUN_NATIVE_TESTS") == "1", "Opt in to installed SketchUp native integration")
    def test_actual_wall_underside_trims_extends_and_projects_only_outside_rc(self):
        from dwg_semantic.wall_under_roof import resolve_walls_under_roof
        opening = {"id": "shaft", "footprint": box(150, 130, 100, 50), "source_entity_ids": ["shaft"], "applies_to_system_id": "roof"}
        plan = payload([zone("low", box(0, 0, 500, 1000), 3800)], default_top=4200, openings=[opening])
        lower = next(group for group in plan["groups"] if group["slab_role"] == "overhead_slab" and group["top_z_mm"] == 3800)
        original = {"id": "wall", "name": "wall", "category": "architectural_wall", "mode": "solid_extrusion",
                    "source_layer": "Walls", "source_layers": ["Walls"], "tag": "Walls", "source_entity_ids": ["wall-source"],
                    "source_handles": ["W"], "base_z_mm": 0, "height_mm": 3950, "evidence": "Synthetic original wall", **box(100, 100, 800, 100)}
        recipe = {"enabled": True, "operation": "fit_to_lowest_overhead_underside", "target_categories": ["architectural_wall"],
                  "target_component_ids": ["wall"], "uncovered_policy": "height_projection_regions", "require_projected_coverage": True,
                  "height_projection_regions": [{"id": "source-low-profile", "footprint": box(0, 0, 500, 1000), "cap_z_mm": 3600,
                                                  "roof_component_ids": [lower["id"]], "source_entity_ids": lower["source_entity_ids"]}]}
        fit = resolve_walls_under_roof([original], plan["groups"], recipe)
        self.assertEqual([], fit["blockers"])
        plan["groups"] = [*fit["groups"], *plan["groups"]]
        plan["wall_under_roof_audit"] = fit["audit"]
        plan["tag_mapping"].append({"source_layer": "Walls", "tag": "Walls"})
        self.assertEqual([], _solid_overlaps(plan["groups"]))
        test_root = Path(__file__).resolve().parents[1] / "work/modeling-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="synthetic-wall-panel-underside-", dir=test_root) as directory:
            qa = write_model(plan, Path(directory) / "walls-under-actual-panels.skp")
            self.assertTrue(qa["all_pass"], json.dumps(qa, ensure_ascii=False))
            self.assertTrue(qa["wall_under_roof_audit"]["all_pass"])
            self.assertEqual([3600, 4000], qa["wall_under_roof_audit"]["changes"][0]["actual_top_z_mm"])
            self.assertGreater(fit["audit"]["trimmed_volume_mm3"], 0)
            self.assertGreater(fit["audit"]["extended_volume_mm3"], 0)
            broken = copy.deepcopy(plan)
            broken["wall_under_roof_audit"]["height_projection_regions"][0]["cap_z_mm"] = 3500
            destination = Path(directory) / "refuse-forged-source-cap.skp"
            with self.assertRaisesRegex(RuntimeError, "wall"):
                write_model(broken, destination)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()

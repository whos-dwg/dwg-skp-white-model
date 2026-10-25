from __future__ import annotations

import copy
import os
from pathlib import Path
import tempfile
import unittest

from shapely.geometry import Polygon

from dwg_semantic.mesh_union import boundaries, shape, topology
from dwg_semantic.modeling import validate_slab_payload, write_model
from dwg_semantic.slab_modeling import prepare_slabs
from test_slab_modeling import decisions, document, footprint, system, zone


def plan(mode="whole_union", *, stepped=False, hole=False):
    config = decisions(system(assembly_mode=mode,
        zones=[zone(footprint=footprint(0,0,5000,8000),top_z_mm=-600)] if stepped else [],
        openings=[{"id":"shaft", "source_entity_ids":["opening"], "footprint":footprint(6500,2500,500,1000),
                   "applies_to_system_id":"floor"}] if hole else [],
        step_risers={"enabled":True,"profile":"edge_aligned_full_depth","thickness_mm":200} if stepped else {"enabled":False}))
    result=prepare_slabs(document(),config)
    return {"schema":"dwg-skp-model-v2","units":"mm",**result,"references":[],
            "coordinate_policy":"Synthetic source-bound whole slab fixture in mm",
            "source_manifest":{"kind":"synthetic whole-slab validation"},
            "presentation":{"overhead_visible":True}}


class WholeSlabTests(unittest.TestCase):
    def test_flat_hole_is_one_real_boundary_leaf_not_a_parent_of_prisms(self):
        result=plan(hole=True)
        self.assertEqual([],result["blockers"])
        self.assertEqual(1,len(result["groups"]))
        whole=result["groups"][0]
        self.assertEqual("boundary_mesh",whole["mode"])
        self.assertEqual(1,topology(whole["faces"])["shell_count"])
        self.assertFalse(shape(whole).covers(Polygon(footprint(6500,2500,500,1000)["outer"])))
        self.assertEqual([],validate_slab_payload(result))

    def test_stepped_union_removes_internal_partitions_and_preserves_volume(self):
        result=plan(stepped=True,hole=True)
        self.assertEqual([],result["blockers"])
        self.assertEqual(1,len(result["groups"]))
        whole=result["groups"][0]
        self.assertTrue(topology(whole["faces"])["all_closed"])
        self.assertAlmostEqual(sum(g["volume_mm3"]for g in whole["construction_cells"]),whole["volume_mm3"])
        self.assertEqual([],validate_slab_payload(result))

    def test_uniform_shell_retains_height_difference_and_source_hole(self):
        result=plan("whole_uniform_thickness",stepped=True,hole=True)
        self.assertEqual([],result["blockers"])
        self.assertEqual(1,len(result["groups"]))
        self.assertEqual([],validate_slab_payload(result))
        self.assertEqual(200,result["groups"][0]["evidence"]["construction_recipe"]["nominal_thickness_mm"])
        self.assertEqual((-800,0),(result["groups"][0]["base_z_mm"],result["groups"][0]["top_z_mm"]))
        whole=result['groups'][0]
        self.assertTrue({'B','L','Z','O','ZN'}<=set(whole['source_handles']))
        self.assertEqual(set(whole['source_entity_ids']),{row['entity_id']for row in whole['sources']})
        self.assertIn('zone_note',whole['annotation_source_ids'])

    def test_tampered_boundary_and_source_cells_are_rejected(self):
        result=plan(stepped=True)
        for case in ("face","cell","volume","audit"):
            broken=copy.deepcopy(result)
            if case=="face":
                p=broken["groups"][0]["faces"][0]["outer"][0]
                broken["groups"][0]["faces"][0]["outer"][0]=[p[0]+10,p[1],p[2]]
            elif case=="cell":broken["groups"][0]["construction_cells"][0]["height_mm"]+=10
            elif case=="volume":broken["groups"][0]["volume_mm3"]+=10000
            else:broken.pop("whole_slab_audit")
            self.assertTrue(validate_slab_payload(broken),case)

    def test_thin_hole_edge_moves_only_source_bound_riser_to_the_other_side(self):
        config=decisions(system(assembly_mode='whole_uniform_thickness',
            zones=[zone(footprint=footprint(0,0,5000,8000),top_z_mm=-600)],
            openings=[{'id':'near-step-shaft','source_entity_ids':['opening'],
                       'footprint':footprint(4500,2500,400,1000),'applies_to_system_id':'floor'}],
            step_risers={'enabled':True,'profile':'edge_aligned_full_depth','thickness_mm':200}))
        result=prepare_slabs(document(),config)
        self.assertEqual([],result['blockers'])
        self.assertEqual([],validate_slab_payload(result))
        whole=result['groups'][0]
        recipe=whole['evidence']['construction_recipe']
        self.assertTrue(recipe['local_side_changes'])
        self.assertAlmostEqual(100,recipe['local_side_changes'][0]['minimum_default_available_width_mm'],places=2)
        self.assertTrue(recipe['material_change_ledger']['added_volume_mm3']>0)
        hole=Polygon(footprint(4500,2500,400,1000)['outer'])
        self.assertTrue(all(shape(cell).intersection(hole).area<.01 for cell in whole['construction_cells']))
        self.assertEqual((0.,0.,10000.,8000.),shape(whole).bounds)

    def test_uniform_oblique_return_and_small_height_step_do_not_require_a_core_gap(self):
        config=decisions(system(assembly_mode='whole_uniform_thickness',
            zones=[zone(footprint={'outer':[[0,0],[4500,0],[5500,4000],[4500,8000],[0,8000]],'holes':[]},top_z_mm=-100)],
            step_risers={'enabled':True,'profile':'edge_aligned_full_depth','thickness_mm':200}))
        result=prepare_slabs(document(),config)
        self.assertEqual([],result['blockers'])
        self.assertEqual([],validate_slab_payload(result))
        self.assertEqual(1,topology(result['groups'][0]['faces'])['shell_count'])

    def test_actual_mesh_missing_and_extra_material_are_not_hidden_by_equal_volume(self):
        from dwg_semantic.whole_slab_qa import verify_saved_whole
        result=plan(stepped=True,hole=True)
        group=result['groups'][0]
        # Shift the actual material, retaining identical cells/volume metadata.
        triangles=[]
        for face in group['faces']:
            ring=face['outer']
            for i in range(1,len(ring)-1):
                triangles.append([[p[0]+100,p[1],p[2]]for p in(ring[0],ring[i],ring[i+1])])
        audit=verify_saved_whole(group,triangles)
        self.assertFalse(audit['all_pass'])

    def test_disconnected_material_cannot_be_packaged_as_one_whole_slab(self):
        cells=[{"id":str(x),**footprint(x,0,100,100),"base_z_mm":0,"height_mm":200}for x in(0,1000)]
        _,faces=boundaries(cells)
        self.assertEqual(2,topology(faces)["shell_count"])

    @unittest.skipUnless(os.environ.get("DWG_RUN_NATIVE_TESTS")=="1","Opt in to installed SketchUp native integration")
    def test_actual_saved_whole_union_and_uniform_material_roundtrip(self):
        for mode in("whole_union","whole_uniform_thickness"):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory()as directory:
                result=plan(mode,stepped=True,hole=True)
                qa=write_model(result,Path(directory)/"whole.skp")
                self.assertTrue(qa["all_pass"])
                self.assertEqual(1,len(qa["groups"]))
                self.assertTrue(qa["whole_slab_audit"]["all_pass"])

    @unittest.skipUnless(os.environ.get('DWG_RUN_NATIVE_TESTS')=='1','Installed SketchUp native integration')
    def test_actual_native_local_side_change_preserves_nearby_opening(self):
        config=decisions(system(assembly_mode='whole_uniform_thickness',
            zones=[zone(footprint=footprint(0,0,5000,8000),top_z_mm=-600)],
            openings=[{'id':'shaft','source_entity_ids':['opening'],'footprint':footprint(4500,2500,400,1000),
                       'applies_to_system_id':'floor'}],
            step_risers={'enabled':True,'profile':'edge_aligned_full_depth','thickness_mm':200}))
        result=prepare_slabs(document(),config)
        payload={'schema':'dwg-skp-model-v2','units':'mm',**result,'references':[],
                 'coordinate_policy':'isolated local-side-change fixture','source_manifest':{'kind':'synthetic'}}
        with tempfile.TemporaryDirectory()as directory:
            qa=write_model(payload,Path(directory)/'near-hole.skp')
            self.assertTrue(qa['all_pass'])


if __name__=="__main__":
    unittest.main()

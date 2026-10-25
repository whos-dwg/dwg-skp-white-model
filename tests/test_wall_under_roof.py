from __future__ import annotations

import copy
import json
import math
import unittest

from shapely.geometry import Polygon, Point
from shapely.ops import unary_union

from dwg_semantic.wall_under_roof import resolve_walls_under_roof, OPERATION


def rectangle(x=0,y=0,width=1000,depth=100,holes=None):
    return {"outer":[[x,y],[x+width,y],[x+width,y+depth],[x,y+depth]],"holes":holes or []}


def wall(identity="WALL",category="architectural_wall",base=0,top=3950,shape=None,**extra):
    return {"id":identity,"name":identity,"category":category,"mode":"solid_extrusion",
            "base_z_mm":base,"height_mm":top-base,"tag":"ORIGINAL-WALL","source_layer":"ORIGINAL-WALL",
            "source_layers":["ORIGINAL-WALL","ORIGINAL-BLOCK"],"source_entity_ids":[f"dwg:{identity}"],
            "source_handles":[identity],"evidence":{"source":"literal-source-profile"},
            **(shape or rectangle()),**extra}


def roof(identity="ROOF",bottom=3500,shape=None,role="overhead_slab",**extra):
    return wall(identity,"slab",bottom,bottom+200,shape or rectangle(),slab_role=role,semantic_role=role,
                tag="RAW-ROOF",source_layer="RAW-ROOF",**extra)


def config(*ids,**extra):
    value={"enabled":True,"operation":OPERATION,"target_categories":["architectural_wall","structural_wall"]}
    if ids:value["target_component_ids"]=list(ids)
    value.update(extra)
    return value


def shape(value):
    return unary_union([shape(p) for p in value["parts"]]) if "parts" in value else Polygon(value["outer"],value.get("holes",[]))


def cap_at(groups,x,y=50):
    selected=[g for g in groups if shape(g).covers(Point(x,y))]
    if len(selected)!=1:raise AssertionError(f"Expected one piece at{x},{y}, got{len(selected)}")
    g=selected[0]
    return g["base_z_mm"]+g["height_mm"]


def projection(identity="P",roof_id="ROOF",cap=3500,footprint=None,source=None):
    return {"id":identity,"roof_component_ids":[roof_id],"source_entity_ids":[source or f"dwg:{roof_id}"],
            "cap_z_mm":cap,"footprint":footprint or rectangle()}


def wall_under_roof_fixture():
    """Reusable pure fixture for native/compose integration without source data."""
    return ([wall(),wall("DOOR-LINTEL",base=2000,semantic_role="door_lintel")],
            [roof("LOW",3500,rectangle(width=500)),roof("HIGH",4000,rectangle(x=500,width=500)),
             roof("STEP",3700,rectangle(x=450,width=100),role="slab_step_riser",host_slab_role="overhead_slab")],config())


class WallUnderRoofTests(unittest.TestCase):
    def resolve(self,base=None,rc=None,recipe=None):
        return resolve_walls_under_roof(base if base is not None else [wall()],rc if rc is not None else [roof()],recipe if recipe is not None else config())

    def successful(self,result,originals):
        self.assertEqual([],result["blockers"])
        self.assertTrue(result["audit"]["all_pass"])
        for original in originals:
            parts=[g for g in result["groups"] if g.get("wall_under_roof_original_component_id",g["id"])==original["id"]]
            self.assertLess(unary_union([shape(g) for g in parts]).symmetric_difference(shape(original)).area,1e-6)
            for g in parts:
                self.assertGreater(g["height_mm"],0)
                self.assertEqual(original["base_z_mm"],g["base_z_mm"])
                for field in ["category","tag","source_layer","source_layers","source_entity_ids","source_handles","evidence"]:
                    self.assertEqual(original.get(field),g.get(field))
        self.assertEqual(result,json.loads(json.dumps(result,allow_nan=False)))

    def test_crossing_distinct_roof_heights_splits_XY_without_lowering_whole_wall(self):
        base=[wall()];rc=[roof("LOW",3500,rectangle(width=400)),roof("HIGH",4000,rectangle(x=400,width=600))]
        result=self.resolve(base,rc)
        self.successful(result,base)
        self.assertEqual(2,len(result["groups"]))
        self.assertEqual(3500,cap_at(result["groups"],200))
        self.assertEqual(4000,cap_at(result["groups"],700))
        self.assertEqual(40000*450,result["audit"]["trimmed_volume_mm3"])
        self.assertEqual(60000*50,result["audit"]["extended_volume_mm3"])

    def test_overhead_riser_underside_caps_only_its_true_footprint(self):
        base,rc,recipe=wall_under_roof_fixture()
        result=self.resolve(base,rc,recipe)
        self.successful(result,base)
        parts=[g for g in result["groups"] if g["wall_under_roof_original_component_id"]=="WALL"]
        self.assertEqual([3500,3700,4000],[cap_at(parts,x) for x in [200,525,800]])
        lintels=[g for g in result["groups"] if g["wall_under_roof_original_component_id"]=="DOOR-LINTEL"]
        self.assertTrue(all(g["base_z_mm"]==2000 for g in lintels))

    def test_cap_can_extend_original3950_to4000(self):
        result=self.resolve(rc=[roof(bottom=4000)])
        self.successful(result,[wall()])
        self.assertEqual(4000,cap_at(result["groups"],200))
        self.assertEqual(5000000,result["audit"]["extended_volume_mm3"])

    def test_equal_actual_underside_keeps_original_dictionary_exact(self):
        original=wall()
        result=self.resolve([original],[roof(bottom=3950)])
        self.assertEqual([original],result["groups"])
        self.assertEqual({},result["height_rules"])
        self.assertEqual("already_at_actual_or_projected_underside",result["audit"]["changes"][0]["status"])

    def test_uncovered_XY_retains_original_top(self):
        result=self.resolve(rc=[roof(shape=rectangle(width=400))])
        self.successful(result,[wall()])
        self.assertEqual([3500,3950],[cap_at(result["groups"],x) for x in [200,700]])
        result=self.resolve(rc=[roof(shape=rectangle(x=5000))])
        self.assertEqual([wall()],result["groups"])

    def test_target_holes_and_roof_holes_preserved_without_invented_material(self):
        hole=[[400,20],[600,20],[600,80],[400,80]]
        original=wall(shape=rectangle(holes=[hole]))
        result=self.resolve([original])
        self.successful(result,[original])
        self.assertEqual(1,len(result["groups"][0]["holes"]))
        original=wall()
        result=self.resolve([original],[roof(shape=rectangle(holes=[hole]))])
        self.assertEqual(3950,cap_at(result["groups"],500,50))
        self.assertEqual(3500,cap_at(result["groups"],200,50))

    def test_lintel_base_and_door_clearance_2000_are_not_changed(self):
        original=wall(base=2000,semantic_role="door_lintel")
        result=self.resolve([original],[roof(bottom=3500)])
        self.successful(result,[original])
        self.assertEqual((2000,1500),(result["groups"][0]["base_z_mm"],result["groups"][0]["height_mm"]))

    def test_cap_at_or_below_original_base_blocks_all_targets_transactionally(self):
        base=[wall("NORMAL"),wall("LINTEL",base=2000)]
        for z in (1800,2000):
            with self.subTest(cap=z):
                result=self.resolve(base,[roof(bottom=z)])
                self.assertIn("wall_under_roof_cap_not_above_base",{b["code"] for b in result["blockers"]})
                self.assertEqual(base,result["groups"])
                self.assertEqual({},result["height_rules"])

    def test_floor_and_floor_hosted_riser_do_not_cap_walls(self):
        rc=[roof(bottom=4000),roof("FLOOR",-200,role="floor_slab"),roof("FLOOR-STEP",100,role="slab_step_riser",host_slab_role="floor_slab")]
        result=self.resolve(rc=rc)
        self.assertEqual(4000,cap_at(result["groups"],100))
        self.assertEqual(["ROOF"],[r["id"] for r in result["audit"]["roofs"]])

    def test_only_explicitly_selected_categories_change(self):
        base=[wall(),wall("COLUMN","column"),wall("CURTAIN","curtain"),wall("ACCESSORY","curtain_accessory")]
        result=self.resolve(base)
        self.successful(result,base)
        self.assertEqual(base[1:],result["groups"][1:])
        result=self.resolve(base,recipe=config(target_categories=["architectural_wall","column","curtain","curtain_accessory"]))
        self.successful(result,base)
        self.assertTrue(all(cap_at([g],500)==3500 for g in result["groups"]))

    def test_curtain_door_lintel_changes_only_when_curtain_explicitly_selected(self):
        original=wall("CURTAIN-LINTEL","curtain",2000,semantic_role="door_lintel")
        result=self.resolve([original],recipe=config(target_categories=["curtain"]))
        self.successful(result,[original])
        self.assertEqual(2000,result["groups"][0]["base_z_mm"])
        result=self.resolve([original],recipe=config("CURTAIN-LINTEL"))
        self.assertTrue(result["blockers"])
        self.assertEqual([original],result["groups"])

    def test_generic_lintel_category_is_supported_explicitly(self):
        original=wall(category="lintel",base=2000)
        result=self.resolve([original],recipe=config(target_categories=["lintel"]))
        self.successful(result,[original])

    def test_inputs_never_mutated_and_full_snapshots_modifier_sources_preserved(self):
        base=[wall(geometry_modifier_provenance={"prior":"original repair"})];rc=[roof()];recipe=config()
        before=copy.deepcopy((base,rc,recipe))
        result=self.resolve(base,rc,recipe)
        self.assertEqual(before,(base,rc,recipe))
        self.assertEqual(base,result["audit"]["original_groups"])
        self.assertEqual(rc,result["audit"]["roofs"])
        part=result["groups"][0]
        self.assertNotIn("original_component_id",part)
        self.assertEqual("WALL",part["wall_under_roof_original_component_id"])
        self.assertEqual(["dwg:ROOF"],part["modifier_source_entity_ids"])
        self.assertEqual(base[0]["geometry_modifier_provenance"],part["geometry_modifier_provenance"]["prior_geometry_modifier_provenance"])

    def test_result_deterministic_under_reversed_roof_order(self):
        base,rc,recipe=wall_under_roof_fixture()
        self.assertEqual(self.resolve(base,rc,recipe),self.resolve(base,list(reversed(rc)),recipe))

    def test_explicit_component_subset_keeps_other_selected_category_exact(self):
        base=[wall("A"),wall("B")]
        result=self.resolve(base,recipe=config("A"))
        self.assertEqual(base[1],result["groups"][-1])

    def test_disabled_is_exact_noop_and_invalid_scope_rolls_back(self):
        self.assertEqual([wall()],self.resolve(recipe={"enabled":False})["groups"])
        for recipe in [{},None,{"enabled":"true"},config(operation="trim_only"),config(target_categories=[]),config(target_categories=["slab"]),config(target_categories=["equipment"]),config("MISSING"),config(target_component_ids=[])]:
            with self.subTest(recipe=recipe):
                result=resolve_walls_under_roof([wall()],[roof()],recipe)
                self.assertTrue(result["blockers"])
                self.assertEqual([wall()],result["groups"])

    def test_invalid_material_roof_or_identity_does_not_partially_apply(self):
        for base,rc in [([wall(height_mm=0)],[roof()]),([wall(source_entity_ids=[])],[roof()]),([wall(base_z_mm=math.nan)],[roof()]),
                        ([wall(outer=[[0,0],[1,1],[0,1],[1,0]])],[roof()]),([wall()],[roof(height_mm=0)]),([wall()],[roof(role="ceiling")]),
                        ([wall()],[roof(role="slab_step_riser")]),([wall()],[roof(),roof()]),([wall(),wall()],[roof()]),
                        ([wall(),wall("WALL-under-roof-001")],[roof()]),([wall()],[])]:
            with self.subTest(base=base,rc=rc):
                result=self.resolve(base,rc)
                self.assertTrue(result["blockers"])
                self.assertEqual(base,result["groups"])

    def projected(self,*regions,require=True):
        return config(uncovered_policy="height_projection_regions",height_projection_regions=list(regions),require_projected_coverage=require)

    def test_projection_extends_height_only_not_RC_material(self):
        base=[wall(shape=rectangle(x=1100,width=400))];rc=[roof()];before=copy.deepcopy(rc)
        result=self.resolve(base,rc,self.projected(projection(footprint=rectangle(x=1000,width=1000))))
        self.successful(result,base)
        self.assertEqual(3500,cap_at(result["groups"],1200))
        self.assertEqual(before,rc)
        self.assertEqual("height_projected",result["audit"]["changes"][0]["cap_regions"][0]["coverage"])
        self.assertEqual(["P"],result["groups"][0]["geometry_modifier_provenance"]["height_projection_region_ids"])

    def test_projection_splits_uncovered_piece_across_two_height_regions(self):
        base=[wall(shape=rectangle(x=1000))]
        rc=[roof("LOW",3500),roof("HIGH",4000)]
        recipe=self.projected(projection("LEFT","LOW",3500,rectangle(x=1000,width=400)),projection("RIGHT","HIGH",4000,rectangle(x=1400,width=600)))
        result=self.resolve(base,rc,recipe)
        self.successful(result,base)
        self.assertEqual([3500,4000],[cap_at(result["groups"],x) for x in (1200,1700)])

    def test_projection_never_overrides_actual_RC_even_when_lower(self):
        base=[wall()];rc=[roof("ACTUAL",4000),roof("REMOTE",3500,rectangle(x=2000))]
        recipe=self.projected(projection("P","REMOTE",3500))
        result=self.resolve(base,rc,recipe)
        self.assertEqual(4000,cap_at(result["groups"],500))
        self.assertEqual(["ACTUAL"],[r["id"] for r in result["audit"]["roofs"]])

    def test_conflicting_projection_caps_only_block_real_uncovered_overlap(self):
        rc=[roof("LOW",3500,rectangle(x=4000)),roof("HIGH",4000,rectangle(x=5000))]
        recipe=self.projected(projection("P1","LOW",3500),projection("P2","HIGH",4000))
        result=self.resolve(rc=rc,recipe=recipe)
        self.assertIn("wall_under_roof_projection_conflict",{b["code"] for b in result["blockers"]})
        self.assertEqual([wall()],result["groups"])
        # Same overlap behind actual RC is irrelevant: projections never act there.
        result=self.resolve(rc=rc+[roof("ACTUAL",3800)],recipe=recipe)
        self.assertEqual([],result["blockers"])
        self.assertEqual(3800,cap_at(result["groups"],500))

    def test_same_height_projection_overlap_is_safe_and_deterministic(self):
        rc=[roof("REMOTE",3500,rectangle(x=4000))]
        p1=projection("P1","REMOTE",3500);p2=projection("P2","REMOTE",3500,rectangle(x=500,width=500))
        result=self.resolve(rc=rc,recipe=self.projected(p1,p2))
        self.successful(result,[wall()])
        self.assertEqual(result,self.resolve(rc=rc,recipe=self.projected(p2,p1)))

    def test_invalid_projection_references_caps_sources_and_shapes_are_blocked(self):
        rc=[roof("REMOTE",3500,rectangle(x=4000)),roof("RISER",3000,rectangle(x=4000),role="slab_step_riser",host_slab_role="overhead_slab")]
        valid=projection(roof_id="REMOTE")
        variants=[{**valid,"roof_component_ids":["UNKNOWN"]},{**valid,"roof_component_ids":["RISER"],"cap_z_mm":3000,"source_entity_ids":["dwg:RISER"]},
                  {**valid,"cap_z_mm":3400},{**valid,"source_entity_ids":["forged-source"]},{**valid,"source_entity_ids":[]},
                  {**valid,"id":""},{**valid,"footprint":{"outer":[[0,0],[1,1],[0,1],[1,0]]}},{**valid,"cap_z_mm":math.nan}]
        for p in variants:
            with self.subTest(projection=p):
                result=self.resolve(rc=rc,recipe=self.projected(p))
                self.assertIn("wall_under_roof_projection_invalid",{b["code"] for b in result["blockers"]})
                self.assertEqual([wall()],result["groups"])
        self.assertTrue(self.resolve(rc=rc,recipe=self.projected(valid,valid))["blockers"])

    def test_required_projected_coverage_blocks_holes_left_unresolved(self):
        rc=[roof(shape=rectangle(x=4000))]
        result=self.resolve(rc=rc,recipe=self.projected(projection(footprint=rectangle(width=400))))
        self.assertIn("wall_under_roof_projected_coverage_incomplete",{b["code"] for b in result["blockers"]})
        self.assertEqual([wall()],result["groups"])
        result=self.resolve(rc=rc,recipe=self.projected(projection(footprint=rectangle(width=400)),require=False))
        self.assertEqual([3500,3950],[cap_at(result["groups"],x) for x in (200,700)])

    def test_projection_opt_in_and_multi_reference_source_binding_are_required(self):
        rc=[roof("A",3500,rectangle(x=4000)),roof("B",3500,rectangle(x=5000))]
        p=projection("P","A",3500);p["roof_component_ids"].append("B")
        result=self.resolve(rc=rc,recipe=self.projected(p))
        self.assertTrue(result["blockers"])
        p["source_entity_ids"].append("dwg:B")
        result=self.resolve(rc=rc,recipe=self.projected(p))
        self.assertEqual([],result["blockers"])
        result=self.resolve(rc=rc,recipe=config(height_projection_regions=[p]))
        self.assertIn("wall_under_roof_projection_policy_required",{b["code"] for b in result["blockers"]})

    def test_malformed_policy_and_unhashable_id_or_role_are_blocked(self):
        for base,rc,recipe in [([wall()],[roof()],config(uncovered_policy=[])),
                               ([wall()],[roof()],config(require_projected_coverage="true")),
                               ([wall(id=["invalid"])],[roof()],config("WALL")),
                               ([wall()],[{**roof(),"slab_role":[],"semantic_role":[]}],config())]:
            with self.subTest(base=base,rc=rc,recipe=recipe):
                result=self.resolve(base,rc,recipe)
                self.assertTrue(result["blockers"])
                self.assertEqual(base,result["groups"])


if __name__=="__main__":
    unittest.main()

"""Contract regressions discovered by an independent full-skill review.

This suite is intentionally isolated from production tests until integrated by
the owner. Every check uses normalized synthetic JSON, without CAD/SKP or an
approval record. Failures identify program gaps, not absent project dimensions.
"""
from __future__ import annotations

import copy
from pathlib import Path
import unittest

from shapely.geometry import Point

from dwg_semantic.classifier import load_rules
from dwg_semantic.modeling import prepare_model
from dwg_semantic.plan_geometry import shape_of
from dwg_semantic.plan_recognition import recognize_plan
from dwg_semantic.slabs import prepare_slab_review
from dwg_semantic.prepared import validate_prepared

ROOT = Path(__file__).resolve().parents[1]


def point(x,y,z=0):
    return {"x":x,"y":y,"z":z}


def poly(key,layer,coords,closed=True,**extra):
    return {"entity_id":key,"handle":key,"source_handle":key,"source_file":"synthetic.dwg",
            "type":"LWPOLYLINE","owner_block":"*Model_Space","layer":layer,"closed":closed,
            "vertices":[point(*p)for p in coords],**extra}


def rect(key,layer,x0,y0,x1,y1,**extra):
    return poly(key,layer,[(x0,y0),(x1,y0),(x1,y1),(x0,y1)],**extra)


def line(key,layer,a,b):
    return {"entity_id":key,"handle":key,"source_handle":key,"source_file":"synthetic.dwg",
            "type":"LINE","owner_block":"*Model_Space","layer":layer,"start":point(*a),"end":point(*b)}


def note(key,value,x=500,y=500):
    return {"entity_id":key,"handle":key,"type":"TEXT","owner_block":"*Model_Space",
            "layer":"Notes","text":value,"position":point(x,y)}


class SemanticContractGaps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules=load_rules(ROOT/"config/default_rules.json")

    def auto(self,document,**kwargs):
        return recognize_plan(document,self.rules,{},**kwargs)

    def bent_sources(self):
        return [line("south","A-WALL",(0,0),(1000,0)),line("east","A-WALL",(1000,0),(1000,1000)),
                line("north","A-WALL",(1000,1000),(0,1000)),line("west","A-WALL",(0,1000),(0,0)),
                poly("mark","SURFACE",[(0,0),(350,650),(1000,1000)],closed=False)]

    def test_open_bent_source_no_opening_note_beats_default(self):
        result=self.auto({"entities":[*self.bent_sources(),note("n","NO OPENING")]})["slab_openings"]
        self.assertEqual([],result["open_bent_marks"])
        self.assertEqual([],result["canonical_symbol_cut_regions"])
        self.assertTrue(result["explicit_non_opening_regions"])

    def test_open_bent_source_drop_note_beats_default(self):
        result=self.auto({"entities":[*self.bent_sources(),note("n","降板200mm")]})["slab_openings"]
        self.assertEqual([],result["open_bent_marks"])
        self.assertEqual([],result["canonical_symbol_cut_regions"])
        self.assertTrue(result["explicit_non_opening_regions"])

    def test_shaft_source_no_opening_note_does_not_become_default_hole(self):
        result=self.auto({"entities":[rect("shaft","A-lift",0,0,2850,2750),note("n","NO OPENING")]})["slab_openings"]
        self.assertEqual([],result["elevator_shafts"])
        self.assertTrue(result["explicit_non_opening_regions"])

    def test_exact_source_footprint_override_wins_over_automatic_topology(self):
        doc={"entities":[rect("wall","A-WALL",0,0,2000,200)]}
        decisions={"layer_mapping":{"A-WALL":{"category":"architectural_wall","height_mm":3000,"footprint_confirmed":True}},
                   "object_overrides":{"wall":{"footprint":{"outer":[[0,0],[1000,0],[1000,200],[0,200]],"holes":[]},
                                                  "reason":"Exact source contour corrected by user"}}}
        payload=prepare_model(doc,{"automatic_plan":self.auto(doc)},decisions)
        self.assertEqual([],payload["blockers"])
        self.assertEqual(200000,sum(shape_of(row).area for row in payload["groups"]))
        self.assertFalse(any(shape_of(row).covers(Point(1500,100))for row in payload["groups"]))

    def test_hollow_column_material_keeps_inner_source_void(self):
        doc={"entities":[rect("outer","A-COLUMN",0,0,600,600),rect("inner","A-COLUMN",200,200,400,400)]}
        result=self.auto(doc)
        self.assertEqual(1,len(result["columns"]))
        shape=shape_of(result["columns"][0])
        self.assertEqual(320000,shape.area)
        self.assertEqual(1,len(shape.interiors))
        self.assertFalse(shape.covers(Point(300,300)))
        self.assertEqual({"outer","inner"},set(result["columns"][0]["source_entity_ids"]))

    def test_conflicting_source_dimensions_cannot_silently_use_sorted_last(self):
        doc={"entities":[rect("outer","A-WALL",0,0,4000,3000),rect("inner","A-WALL",200,200,3800,2800)]}
        decisions={"layer_mapping":{"A-WALL":{"category":"architectural_wall","height_mm":3000,"footprint_confirmed":True}},
                   "object_overrides":{"inner":{"height_mm":3000,"reason":"Source-specific confirmed height"},
                                       "outer":{"height_mm":4000,"reason":"Conflicting source-specific confirmed height"}}}
        payload=prepare_model(doc,{"automatic_plan":self.auto(doc)},decisions)
        self.assertTrue(payload["blockers"],"A composite candidate must resolve conflicting exact-source dimensions, not let ID ordering decide")

    def test_unclosed_facade_review_item_must_reach_model_blockers(self):
        parent={"type":"INSERT","handle":"F","entity_id":"assembly:F","source_file":"synthetic.dwg",
                "owner_block":"*Model_Space","layer":"0","block_name":"facade"}
        frames=[rect("f1","Defpoints",0,0,80,300),rect("f2","Defpoints",10000,0,10080,300)]
        for row in frames:
            row.update(root_space="*Model_Space",instance_path="F",original_layer=row["layer"])
            row["vertices_world"]=row.pop("vertices")
        doc={"entities":[parent],"resolved_instances":frames}
        recognition=self.auto(doc)
        self.assertEqual("perimeter_closure_requires_review",recognition["facade_systems"][0]["perimeter_audit"]["status"])
        decisions={"layer_mapping":{"Defpoints":{"category":"curtain_accessory","height_mm":3000,"footprint_confirmed":True}}}
        payload=prepare_model(doc,{"automatic_plan":recognition},decisions)
        self.assertTrue(payload["blockers"],"Unresolved facade closure may not pass to approval just because local frames are individually closed")

    def test_native_legacy_polyline_bounded_cross_is_not_silently_missed(self):
        frame=rect("legacy-frame","Vendor-Slab",0,0,1000,1000,type="POLYLINE",polyline_kind="legacy_2d")
        crosses=[line("x1","Symbols",(0,0),(1000,1000)),line("x2","Symbols",(1000,0),(0,1000))]
        result=prepare_slab_review({"entities":[frame,*crosses]},{})
        self.assertEqual(1,len(result["opening_proposals"]))
        self.assertEqual({"legacy-frame","x1","x2"},set(result["opening_proposals"][0]["source_entity_ids"]))

    def test_prepared_solver_must_account_for_omitted_source_wall(self):
        doc={"intake":{"source_sha256":"same-source"},"entities":[rect("w1","Walls",0,0,1000,200),
               rect("w2","Walls",2000,0,3000,200)]}
        decisions={"default_height_mm":3200,"layer_mapping":{"Walls":{"category":"architectural_wall","height_mm":3200,
                   "footprint_confirmed":True}},"prepared_categories_by_primary_layer":{"Walls":["architectural_wall"]}}
        original=prepare_model(doc,{},decisions)
        self.assertEqual([],original["blockers"])
        self.assertEqual(2,len(original["groups"]))
        omitted=copy.deepcopy(original)
        omitted["groups"]=[row for row in omitted["groups"]if row["id"]!="w2"]
        result=validate_prepared(omitted,doc,{},decisions)
        self.assertTrue(result["blockers"],"Valid existing IDs in the remaining group do not account for a missing source wall")

    def test_retained_whole_boundary_cannot_be_reopened_by_nested_default_mark(self):
        rows=[rect("whole","Vendor-Slab",0,0,2000,2000),line("whole-x1","Symbols",(0,0),(2000,2000)),
              line("whole-x2","Symbols",(2000,0),(0,2000)),rect("detail","Vendor-Slab",500,500,1500,1500),
              line("detail-x1","Symbols",(500,500),(1500,1500)),line("detail-x2","Symbols",(1500,500),(500,1500))]
        doc={"entities":rows}
        decisions={"region_overrides":{"whole":{"action":"retain","reason":"The entire bounded slab region is confirmed solid"}}}
        result=self.auto(doc,slab_decisions=decisions)["slab_openings"]
        self.assertEqual([],result["canonical_symbol_cut_regions"],"An inner unconfirmed default mark cannot override a confirmed retained whole region")


if __name__=="__main__":
    unittest.main()



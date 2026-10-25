"""Second independent review: decision conflicts and source-plane invariants.

Output additions proposed here follow existing review naming: decision_issues
is a list of {code, source_entity_ids, ...}, never a substitute for a candidate.
"""
from __future__ import annotations

import copy
from pathlib import Path
import unittest

from dwg_semantic.classifier import load_rules
from dwg_semantic.plan_recognition import recognize_plan
from dwg_semantic.slabs import prepare_slab_review

ROOT=Path(__file__).resolve().parents[1]


def point(x,y,z=0):
    return {"x":x,"y":y,"z":z}


def poly(key,layer,coords,closed=True,**extra):
    return {"entity_id":key,"handle":key,"source_handle":key,"source_file":"synthetic.dwg",
            "type":"LWPOLYLINE","owner_block":"*Model_Space","layer":layer,"closed":closed,
            "vertices":[point(*p)for p in coords],**extra}


def rect(key,layer,x0,y0,x1,y1,z=0,**extra):
    return poly(key,layer,[(x0,y0,z),(x1,y0,z),(x1,y1,z),(x0,y1,z)],**extra)


def line(key,layer,a,b):
    return {"entity_id":key,"handle":key,"source_handle":key,"source_file":"synthetic.dwg",
            "type":"LINE","owner_block":"*Model_Space","layer":layer,"start":point(*a),"end":point(*b)}


def crossed_frame(key="frame",z=0):
    return [rect(key,"Vendor-Slab",0,0,1000,1000,z=z),
            line(key+"-x1","Symbols",(0,0,z),(1000,1000,z)),
            line(key+"-x2","Symbols",(1000,0,z),(0,1000,z))]


class SecondPassSemantics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules=load_rules(ROOT/"config/default_rules.json")

    def auto(self,document,**kwargs):
        return recognize_plan(document,self.rules,{},**kwargs)

    def bent_sources(self):
        return [line("south","A-WALL",(0,0),(1000,0)),line("east","A-WALL",(1000,0),(1000,1000)),
                line("north","A-WALL",(1000,1000),(0,1000)),line("west","A-WALL",(0,1000),(0,0)),
                poly("mark","SURFACE",[(0,0),(350,650),(1000,1000)],closed=False)]

    def test_numerical_source_plane_noise_does_not_split_nested_cut_regions(self):
        from dwg_semantic.slab_recognition import canonical_openings
        rows=[{'id':'outer','source_entity_ids':['outer'], 'kind':'bounded_slab_mark',
               'source_plane_z_mm':-1.5371013516166294e-9,
               'footprint':{'outer':[[0,0],[2000,0],[2000,2000],[0,2000]],'holes':[]}},
              {'id':'inner','source_entity_ids':['inner'], 'kind':'bounded_slab_mark',
               'source_plane_z_mm':3.78349782516479e-10,
               'footprint':{'outer':[[500,500],[1000,500],[1000,1000],[500,1000]],'holes':[]}}]
        merged=canonical_openings(rows,.01)
        self.assertEqual(1,len(merged))
        self.assertEqual({'outer','inner'},set(merged[0]['source_entity_ids']))
        rows[1]['source_plane_z_mm']=500
        self.assertEqual(2,len(canonical_openings(rows,.01)))

    def test_matching_column_infill_hatch_is_material_not_a_false_hollow(self):
        from dwg_semantic.plan_geometry import shape_of
        rows=[rect('outer','A-COLUMN',0,0,700,1000),rect('inner','A-COLUMN',25,25,675,975)]
        hatch={'entity_id':'infill','handle':'infill','source_handle':'infill','source_file':'synthetic.dwg',
               'type':'HATCH','owner_block':'*Model_Space','layer':'Concrete-hatch','solid_fill':True,
               'pattern_name':'SOLID','boundary_loops':[{'supported':True,'closed':True,'loop_type':'External',
               'vertices':[point(25,25),point(675,25),point(675,975),point(25,975)]}]}
        result=self.auto({'entities':rows+[hatch]})
        self.assertEqual(1,len(result['columns']))
        column=result['columns'][0]
        self.assertEqual(700000,shape_of(column).area)
        self.assertEqual([],column['footprint']['holes'])
        self.assertEqual({'outer','inner','infill'},set(column['source_entity_ids']))
        self.assertEqual(['infill'],column['source_solid_fill_entity_ids'])
        self.assertFalse(result['wall_voids'])
        # A fill from a different placed instance cannot silently fill it.
        hatch['instance_path']='other-placement'
        hollow=self.auto({'entities':rows+[hatch]})['columns'][0]
        self.assertEqual(82500,shape_of(hollow).area)
        self.assertEqual(1,len(hollow['footprint']['holes']))

    def test_equal_priority_retained_and_opening_regions_are_an_explicit_conflict(self):
        rows=[rect("whole","Vendor-Slab",0,0,2000,2000),line("whole-x1","Symbols",(0,0),(2000,2000)),
              line("whole-x2","Symbols",(2000,0),(0,2000)),rect("detail","Vendor-Slab",500,500,1500,1500),
              line("detail-x1","Symbols",(500,500),(1500,1500)),line("detail-x2","Symbols",(1500,500),(500,1500))]
        decisions={"region_overrides":{"whole":{"action":"retain","reason":"Whole bounded region retained"},
                    "detail":{"action":"opening","reason":"Overlapping inner region explicitly opened"}}}
        slab=self.auto({"entities":rows},slab_decisions=decisions)["slab_openings"]
        self.assertEqual([],slab["canonical_symbol_cut_regions"],"A same-authority contradiction must not choose an opening silently")
        issues=slab.get("decision_issues",[])
        self.assertTrue(any("conflict"in row.get("code","")for row in issues),issues)
        self.assertTrue({"whole","detail"}<=set().union(*(set(row.get("source_entity_ids",[]))for row in issues)))

    def test_nonplanar_lift_boundary_is_not_flattened_to_a_candidate(self):
        shaft=poly("shaft","A-lift",[(0,0,0),(2850,0,0),(2850,2750,100),(0,2750,100)],
                   type="POLYLINE",polyline_kind="legacy_3d")
        slab=self.auto({"entities":[shaft]})["slab_openings"]
        self.assertEqual([],slab["elevator_shafts"])

    def test_nonplanar_open_bent_symbol_is_not_flattened_to_a_candidate(self):
        rows=self.bent_sources()
        rows[-1]["vertices"][1]["z"]=100
        slab=self.auto({"entities":rows})["slab_openings"]
        self.assertEqual([],slab["open_bent_marks"])
        self.assertEqual([],slab["canonical_symbol_cut_regions"])

    def test_open_bent_boundary_sources_must_share_one_plane(self):
        rows=self.bent_sources()
        for key in ("east","north"):
            row=next(row for row in rows if row["entity_id"]==key)
            row["start"]["z"]=500;row["end"]["z"]=500
        slab=self.auto({"entities":rows})["slab_openings"]
        self.assertEqual([],slab["open_bent_marks"])
        self.assertEqual([],slab["canonical_symbol_cut_regions"])

    def test_nonplanar_closed_slab_frame_is_not_a_valid_bounded_mark(self):
        rows=crossed_frame()
        rows[0]["type"]="POLYLINE";rows[0]["polyline_kind"]="legacy_3d"
        rows[0]["vertices"][2]["z"]=100
        review=prepare_slab_review({"entities":rows},{})
        self.assertEqual([],review["opening_proposals"])
        self.assertEqual([],review["automatic_opening_recognition"]["canonical_symbol_cut_regions"])

    def test_closed_mark_diagonals_cannot_be_borrowed_from_another_z_plane(self):
        rows=crossed_frame()
        rows[-1]["start"]["z"]=500;rows[-1]["end"]["z"]=500
        review=prepare_slab_review({"entities":rows},{})
        self.assertEqual([],review["opening_proposals"])

    def test_gross_slab_extent_does_not_close_from_z_separated_sources(self):
        rows=self.bent_sources()[:-1]
        for row in rows:
            row["layer"]="Exterior"
        for key in ("east","north"):
            row=next(row for row in rows if row["entity_id"]==key)
            row["start"]["z"]=500;row["end"]["z"]=500
        review=prepare_slab_review({"entities":rows},{},boundary_layers=["Exterior"])
        self.assertEqual([],review["boundaries"])

    def test_equal_xy_lift_sources_on_distinct_planes_are_not_deduplicated(self):
        rows=[rect("lower-shaft","A-lift",0,0,2850,2750,z=0),
              rect("upper-shaft","A-lift",0,0,2850,2750,z=3000)]
        slab=self.auto({"entities":rows})["slab_openings"]
        self.assertEqual(2,len(slab["elevator_shafts"]))
        self.assertEqual({0,3000},{row.get("source_plane_z_mm")for row in slab["elevator_shafts"]})
        self.assertTrue(all(len(row["boundary_source_entity_ids"])==1 for row in slab["elevator_shafts"]))

    def test_valid_planar_sources_remain_recognized_without_projection(self):
        rows=self.bent_sources()
        before=copy.deepcopy(rows)
        slab=self.auto({"entities":rows})["slab_openings"]
        self.assertEqual(1,len(slab["open_bent_marks"]))
        self.assertEqual(before,rows)
        review=prepare_slab_review({"entities":crossed_frame()},{})
        self.assertEqual(1,len(review["opening_proposals"]))


if __name__=="__main__":
    unittest.main()



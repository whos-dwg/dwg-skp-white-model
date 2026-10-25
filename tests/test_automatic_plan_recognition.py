from __future__ import annotations

import copy
import math
from pathlib import Path
import unittest

from shapely.affinity import rotate
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from dwg_semantic.classifier import classify_document, load_rules
from dwg_semantic.modeling import prepare_model
from dwg_semantic.plan_geometry import SourceIndex, shape_of
from dwg_semantic.plan_recognition import recognize_plan
from dwg_semantic.slab_recognition import recognize_slab_openings


ROOT=Path(__file__).resolve().parents[1]


def point(x,y,z=0):
    return {"x":x,"y":y,"z":z}


def poly(identity,layer,coords,closed=True,**extra):
    return {"entity_id":identity,"handle":identity,"source_handle":identity,"source_file":"synthetic.dwg",
            "type":"LWPOLYLINE","owner_block":"*Model_Space","layer":layer,"closed":closed,
            "vertices":[point(*p)for p in coords],**extra}


def rect(identity,layer,x0,y0,x1,y1,**extra):
    return poly(identity,layer,[(x0,y0),(x1,y0),(x1,y1),(x0,y1)],**extra)


def line(identity,layer,a,b,**extra):
    return {"entity_id":identity,"handle":identity,"source_handle":identity,"source_file":"synthetic.dwg",
            "type":"LINE","owner_block":"*Model_Space","layer":layer,"start":point(*a),"end":point(*b),**extra}


def placed(row,path):
    row=copy.deepcopy(row)
    row["root_space"]="*Model_Space";row["instance_path"]=path
    row["original_layer"]=row["layer"]
    for field in("vertices","curve_points","start","end","center"):
        if field in row:
            row[field+"_world"]=row.pop(field)
    return row


def container(path,name,layer):
    return {"type":"INSERT","handle":path,"entity_id":"assembly:"+path,"source_file":"synthetic.dwg",
            "owner_block":"*Model_Space","layer":layer,"original_layer":layer,"block_name":name}


def swing_arc(identity,center,start,end,path):
    row={"entity_id":identity,"handle":identity,"source_handle":identity,"source_file":"synthetic.dwg",
         "owner_block":"*Model_Space","type":"ARC","layer":"DOOR","center":point(*center),
         "start":point(*start),"end":point(*end),"radius":math.dist(center,start),
         "curve_points":[point(*start),point(*end)]}
    return placed(row,path)


class AutomaticPlanRecognitionTests(unittest.TestCase):
    def setUp(self):
        self.rules=load_rules(ROOT/"config/default_rules.json")

    def recognize(self,document,**kwargs):
        return recognize_plan(document,self.rules,{},**kwargs)

    def test_nested_child_layer_is_kept_and_parent_conflict_is_explicit(self):
        wall=placed(rect("child","A-WALL",0,0,2000,200),"ROOT")
        document={"entities":[container("ROOT","mixed assembly","CURTAIN")],"resolved_instances":[wall]}
        result=self.recognize(document)
        self.assertEqual(1,len(result["wall_materials"])+sum(len(s["sections"])for s in result["facade_systems"]))
        self.assertEqual(["child"],result["layer_conflict_requests"][0]["source_entity_ids"])
        self.assertEqual("user_confirmation_required",result["layer_conflict_requests"][0]["status"])
        decisions={"layer_mapping":{"A-WALL":{"category":"architectural_wall","height_mm":3000,"footprint_confirmed":True}}}
        payload=prepare_model(document,{"automatic_plan":result},decisions)
        self.assertTrue(any(b["code"]=="parent_child_layer_semantics_require_confirmation"for b in payload["blockers"]))

    def test_outer_wall_minus_multiple_inner_loops_survives_into_solid_planner(self):
        rows=[rect("outer","A-WALL",0,0,8000,6000),rect("h1","A-WALL",500,500,1800,4500),
              rect("h2","A-WALL",3000,3600,3800,4700),rect("h3","A-WALL",5000,4000,5800,4900),
              rect("h4","A-WALL",4200,1200,7000,2400),
              {"type":"CIRCLE","entity_id":"round-hole","handle":"round-hole","source_file":"synthetic.dwg","owner_block":"*Model_Space","layer":"A-WALL","center":point(3000,1500),"radius":350}]
        document={"entities":rows}
        result=self.recognize(document)
        self.assertEqual(1,len(result["wall_materials"]))
        material=shape_of(result["wall_materials"][0])
        self.assertEqual(5,len(material.interiors))
        self.assertFalse(material.covers(Point(3000,1500)))
        self.assertTrue(material.covers(Point(4000,3000)))
        decisions={"layer_mapping":{"A-WALL":{"category":"architectural_wall","height_mm":3200,"footprint_confirmed":True}}}
        payload=prepare_model(document,{"automatic_plan":result},decisions)
        self.assertEqual(1,len(payload["groups"]))
        self.assertEqual(5,len(payload["groups"][0]["holes"]))
        self.assertEqual([],payload["blockers"])

    def test_nonplanar_legacy_polyline_requires_projection_and_is_not_flattened(self):
        source=poly("3d","A-WALL",[(0,0,0),(2000,0,0),(2000,200,50),(0,200,50)],type="POLYLINE",polyline_kind="legacy_3d")
        result=self.recognize({"entities":[source]})
        self.assertEqual([],result["wall_materials"])
        self.assertEqual([0,50],result["nonplanar_wall_sources"][0]["z_range_mm"])
        self.assertEqual("projection_confirmation_required",result["nonplanar_wall_sources"][0]["status"])

    def test_separate_source_z_planes_cannot_close_each_others_missing_edges(self):
        rows=[line("bottom","A-WALL",(0,0,0),(2000,0,0)),line("right","A-WALL",(2000,0,0),(2000,200,0)),
              line("top","A-WALL",(2000,200,500),(0,200,500)),line("left","A-WALL",(0,200,500),(0,0,500))]
        result=self.recognize({"entities":rows})
        self.assertEqual([],result["wall_materials"])

    def test_reference_parent_cannot_silently_discard_a_wall_child(self):
        child=placed(rect("wall-child","A-WALL",0,0,2000,200),"R")
        result=self.recognize({"entities":[container("R","reference detail","REF")],"resolved_instances":[child]})
        self.assertEqual(1,len(result["wall_materials"]))
        self.assertEqual("user_confirmation_required",result["layer_conflict_requests"][0]["status"])

    def test_structural_wall_cavity_is_not_gross_filled(self):
        result=self.recognize({"entities":[rect("outer","A-STRUCT",0,0,6000,4000),
                                           rect("inner","A-STRUCT",250,250,5750,3750)]})
        self.assertEqual([],result["columns"])
        self.assertEqual(1,len(result["wall_materials"]))
        self.assertFalse(shape_of(result["wall_materials"][0]).covers(Point(3000,2000)))
        self.assertEqual(1,len(result["wall_voids"]))

    def test_nested_wall_island_keeps_alternating_material_and_void(self):
        result=self.recognize({"entities":[rect("outer","A-WALL",0,0,4000,3000),
                                           rect("inner","A-WALL",200,200,3800,2800),
                                           rect("island","A-WALL",1700,1300,2300,1700)]})
        material=unary_union([shape_of(row)for row in result["wall_materials"]])
        self.assertTrue(material.covers(Point(100,100)))
        self.assertFalse(material.covers(Point(1000,1000)))
        self.assertTrue(material.covers(Point(2000,1500)))

    def test_facade_z_separated_segments_do_not_form_a_false_closure(self):
        rows=[line("bottom","Defpoints",(0,0,0),(80,0,0)),line("right","Defpoints",(80,0,0),(80,300,0)),
              line("top","Defpoints",(80,300,500),(0,300,500)),line("left","Defpoints",(0,300,500),(0,0,500))]
        result=self.recognize({"entities":[container("F","facade","0")],"resolved_instances":[placed(r,"F")for r in rows]})
        self.assertEqual([],result["facade_systems"][0]["sections"])
        self.assertEqual("perimeter_closure_requires_review",result["facade_systems"][0]["perimeter_audit"]["status"])

    def test_foreign_wall_material_stays_material_inside_source_cavity(self):
        result=self.recognize({"entities":[rect("outer","WALL-PARAPET",0,0,2000,1600),
                                           rect("inner","WALL-PARAPET",200,200,1800,1400),
                                           rect("crossing","A-STRUCT",700,0,900,500)]})
        cavity=result["wall_voids"][0]
        self.assertIn("source_inner_footprint",cavity)
        self.assertTrue(shape_of(cavity["source_inner_footprint"]).covers(Point(800,300)))
        self.assertFalse(shape_of(cavity).covers(Point(800,300)))
        self.assertTrue(result["qa"]["all_geometric_invariants_pass"])

    def test_automatic_model_transfer_keeps_source_plane_and_layer_provenance(self):
        document={"entities":[rect("elevated","A-WALL",0,0,2000,200, elevation=125)]}
        for p in document["entities"][0]["vertices"]:
            p["z"]=125
        result=self.recognize(document)
        decisions={"layer_mapping":{"A-WALL":{"category":"architectural_wall","height_mm":3000,"footprint_confirmed":True}}}
        payload=prepare_model(document,{"automatic_plan":result},decisions)
        self.assertEqual(125,payload["groups"][0]["source_plane_z_mm"])
        self.assertEqual([125,125],payload["groups"][0]["source_z_range_mm"])
        self.assertEqual("A-WALL",payload["groups"][0]["source_provenance"][0]["original_layer"])

    def test_shared_long_line_closes_frame_and_bounded_authorized_gap_is_audited(self):
        rows=[line("left","Defpoints",(0,0),(0,300)),line("cap","Defpoints",(0,300),(80,300)),
              line("right","Defpoints",(80,300),(80,0)),line("shared","Defpoints",(-1000,0),(78.8,0))]
        document={"intake":{"source_sha256":"synthetic-source"},"entities":[container("F","facade assembly","0")],
                  "resolved_instances":[placed(r,"F")for r in rows]}
        before=self.recognize(document)
        self.assertFalse(before["facade_systems"][0]["sections"])
        self.assertEqual("perimeter_closure_requires_review",before["facade_systems"][0]["perimeter_audit"]["status"])
        options={"source_sha256":"synthetic-source","settings":{"collinear_gap_repair_mm":2,"collinear_gap_minimum_source_length_mm":500}}
        after=self.recognize(document,decisions=options)
        system=after["facade_systems"][0]
        self.assertEqual(1,system["counts"]["frame"])
        self.assertEqual(1,len(system["repairs"]))
        self.assertAlmostEqual(1.2,system["repairs"][0]["length_mm"],places=5)
        self.assertIn("shared",system["sections"][0]["source_entity_ids"])
        options["source_sha256"]="another-file"
        with self.assertRaises(ValueError):
            self.recognize(document,decisions=options)

    def test_common_precision_preserves_diagonal_t_junctions_and_input_order(self):
        from shapely.ops import polygonize
        from dwg_semantic.plan_geometry import node_linework
        length=math.hypot(1000,753.554);u=(1000/length,753.554/length);n=(-u[1],u[0])
        a=(182000.004+321.234*u[0],-150000.006+321.234*u[1])
        b=(a[0]+80*u[0],a[1]+80*u[1])
        c=(a[0]+300*n[0],a[1]+300*n[1]);d=(b[0]+300*n[0],b[1]+300*n[1])
        rows=[LineString([a,c]),LineString([c,d]),LineString([d,b]),
              LineString([(182000.004,-150000.006),(183000.004,-149246.452)])]
        first=list(polygonize(node_linework(rows,.01)))
        second=list(polygonize(node_linework(list(reversed(rows)),.01)))
        self.assertEqual(1,len(first))
        self.assertLess(abs(first[0].area-24000),10)
        self.assertTrue(first[0].equals(second[0]))

    def test_lift_opening_uses_entire_enclosure_and_deduplicates_boundary(self):
        rows=[rect("outer1","A-lift",0,0,2850,2750),rect("outer2","A-lift",0,0,2850,2750),
              rect("car","A-lift",225,450,2625,2150),line("x1","A-lift",(225,450),(2625,2150)),
              line("x2","A-lift",(225,2150),(2625,450))]
        result=self.recognize({"entities":rows})["slab_openings"]
        self.assertEqual(1,len(result["elevator_shafts"]))
        shaft=result["elevator_shafts"][0]
        self.assertEqual(2,shaft["duplicate_boundary_count"])
        self.assertAlmostEqual(2850*2750,shaft["area_mm2"])
        self.assertEqual({"x1","x2"},set(shaft["car_mark_source_entity_ids"]))

    def test_open_bent_symbol_finds_four_real_boundaries_with_offset_record(self):
        rows=[poly("symbol","SURFACE",[(0,50),(350,650),(1000,1000)],closed=False),
              line("south1","A-WALL",(0,0),(400,0)),line("south2","A-WALL",(400,0),(1000,0)),
              line("east","A-WALL",(1000,0),(1000,1000)),line("north","A-WALL",(1000,1000),(0,1000)),
              line("west","A-WALL",(0,1000),(0,0))]
        result=self.recognize({"entities":rows})["slab_openings"]
        self.assertEqual(1,len(result["open_bent_marks"]))
        mark=result["open_bent_marks"][0]
        self.assertAlmostEqual(1000000,mark["area_mm2"])
        self.assertTrue(any(abs(side["alignment_mm"]+50)<.01 for side in mark["side_support_audit"]))
        self.assertIn("south1",mark["source_entity_ids"])
        self.assertIn("south2",mark["source_entity_ids"])
        self.assertTrue(all(side["coverage_ratio"]>.999 for side in mark["side_support_audit"]))

    def test_furniture_edges_do_not_turn_a_bent_path_into_a_slab_opening(self):
        rows=[poly("symbol","SURFACE",[(0,0),(300,300),(1000,1000)],closed=False),rect("desk","FURN",0,0,1000,1000)]
        result=self.recognize({"entities":rows})["slab_openings"]
        self.assertEqual([],result["open_bent_marks"])

    def test_explicit_retained_bent_mark_and_lowered_shaft_override_opening_default(self):
        rows=[poly("symbol","SURFACE",[(0,0),(350,650),(1000,1000)],closed=False),
              rect("boundary","A-WALL",0,0,1000,1000),rect("shaft","A-lift",3000,0,5000,2500)]
        result=self.recognize({"entities":rows},slab_decisions={"region_overrides":{
            "symbol":{"action":"retain","reason":"User retained this exact slab region"},
            "shaft":{"action":"level_change","reason":"This exact source region is a recessed slab"}}})["slab_openings"]
        self.assertEqual([],result["open_bent_marks"])
        self.assertEqual([],result["elevator_shafts"])
        self.assertEqual([],result["canonical_symbol_cut_regions"])
        self.assertEqual({"retain","level_change"},{r["explicit_decision"]["action"]for r in result["explicit_non_opening_regions"]})

    def test_a_shared_wall_inner_boundary_does_not_suppress_its_slab_opening_mark(self):
        from dwg_semantic.slabs import prepare_slab_review
        document={"entities":[rect("outer","WALL-PARAPET",0,0,1800,1400),
                               rect("inner","WALL-PARAPET",200,200,1600,1200),
                               poly("symbol","AA-PD",[(200,200),(1100,400),(1600,1200)],closed=False)]}
        classification=classify_document(document,self.rules)
        review=prepare_slab_review(document,classification)
        marks=[r for r in review["region_candidates"]if r["boundary_source_entity_ids"]==["inner"]]
        self.assertEqual(1,len(marks))
        self.assertEqual("opening",marks[0]["default_decision"]["action"])

    def test_double_door_and_lintel_use_all_leaves_and_actual_wall_faces(self):
        walls=[rect("wall-left","A-WALL",-2000,-100,-50,200),rect("wall-right","A-WALL",1450,-100,3500,200)]
        leaves=[placed(line("leaf-left","DOOR",(0,0),(0,700)),"D"),placed(line("leaf-right","DOOR",(1400,0),(1400,700)),"D"),
                swing_arc("arc-left",(0,0),(700,0),(0,700),"D"),swing_arc("arc-right",(1400,0),(700,0),(1400,700),"D")]
        result=self.recognize({"entities":[*walls,container("D","double door","DOOR")],"resolved_instances":leaves})
        door=result["door_assemblies"][0]
        self.assertEqual(2,door["leaf_count"])
        self.assertAlmostEqual(1400,door["leaf_span_mm"])
        self.assertEqual("plan_geometry_resolved",door["status"])
        self.assertEqual((-50,-100,1450,200),shape_of(door["lintel_footprint"]).bounds)
        self.assertAlmostEqual(300,door["host_audit"]["wall_thickness_mm"])

    def test_sliding_and_folding_doors_are_recognized_without_swing_arcs(self):
        for name,kind in(("sliding door","sliding_door"),("folding door","folding_door")):
            with self.subTest(name=name):
                walls=[rect("wall-left","A-WALL",-2000,-100,-20,200),rect("wall-right","A-WALL",1520,-100,3500,200)]
                frames=[placed(line("j1","DOOR",(0,-100),(0,200)),"D"),placed(line("j2","DOOR",(1500,-100),(1500,200)),"D"),
                        placed(poly("folded-leaves","DOOR",[(0,0),(350,150),(750,0),(1100,150),(1500,0)],closed=False),"D")]
                result=self.recognize({"entities":[*walls,container("D",name,"DOOR")],"resolved_instances":frames})
                door=result["door_assemblies"][0]
                self.assertEqual(kind,door["opening_type"])
                self.assertEqual("opposite_frame_crosspieces",door["geometry_method"])
                self.assertEqual("plan_geometry_resolved",door["status"])
                self.assertAlmostEqual(1540,door["host_audit"]["span_mm"])

    def test_native_lintel_planner_measures_wall_depth_independently_of_door_net_depth(self):
        document={"entities":[rect("left","A-WALL",-2000,0,0,200),rect("right","A-WALL",1000,0,3000,200),
                               rect("door","OPENING",0,60,1000,140)]}
        decisions={"layer_mapping":{"A-WALL":{"category":"architectural_wall","height_mm":3000,"footprint_confirmed":True},
                                     "OPENING":{"category":"opening","height_mm":2100,"base_z_mm":0,"footprint_confirmed":True}}}
        payload=prepare_model(document,{},decisions)
        self.assertFalse(payload["blockers"],payload["blockers"])
        lintels=[g for g in payload["groups"]if g["id"].startswith("lintel:")]
        self.assertEqual(1,len(lintels))
        self.assertEqual((0,0,1000,200),shape_of(lintels[0]).bounds)
        self.assertEqual(2100,lintels[0]["base_z_mm"])

    def test_two_jambs_can_belong_to_one_connected_host_wall(self):
        host=poly("host","A-WALL",[(-1000,0),(0,0),(0,200),(1000,200),(1000,0),(2000,0),(2000,1000),(-1000,1000)])
        document={"entities":[host,rect("door","OPENING",0,60,1000,140)]}
        decisions={"layer_mapping":{"A-WALL":{"category":"architectural_wall","height_mm":3000,"footprint_confirmed":True},
                                     "OPENING":{"category":"opening","height_mm":2100,"base_z_mm":0,"footprint_confirmed":True}}}
        payload=prepare_model(document,{},decisions)
        self.assertFalse(payload["blockers"],payload["blockers"])
        lintels=[g for g in payload["groups"]if g["id"].startswith("lintel:")]
        self.assertEqual(1,len(lintels))
        self.assertEqual((0,0,1000,200),shape_of(lintels[0]).bounds)

    def test_thicker_junction_supplies_the_same_full_wall_band(self):
        from dwg_semantic.opening_recognition import host_footprint
        walls=[{"id":"left","category":"architectural_wall","source_entity_ids":["l"],"footprint":{"outer":[[-1000,-100],[0,-100],[0,100],[-1000,100]],"holes":[]}},
               {"id":"right","category":"structural_wall","source_entity_ids":["r"],"footprint":{"outer":[[1000,-100],[2000,-100],[2000,400],[1000,400]],"holes":[]}}]
        result=host_footprint([[0,0],[1000,0]],walls)
        self.assertEqual("opposite_jamb_faces_resolved",result["status"])
        self.assertEqual((0,-100,1000,100),shape_of(result["footprint"]).bounds)

    def test_staggered_sliding_frame_uses_wall_gap_instead_of_panel_outer_length(self):
        walls=[rect("wall-left","A-WALL",-2000,-100,-20,200),rect("wall-right","A-WALL",1420,-100,3500,200)]
        frames=[placed(line("j1","WINDOW",(-40,-30),(-40,0)),"D"),
                placed(line("j2","WINDOW",(1460,0),(1460,30)),"D"),
                placed(line("p1","WINDOW",(-40,0),(800,0)),"D"),placed(line("p2","WINDOW",(650,30),(1460,30)),"D")]
        result=self.recognize({"entities":[*walls,container("D","sliding door","WINDOW")],"resolved_instances":frames})
        door=result["door_assemblies"][0]
        self.assertEqual("plan_geometry_resolved",door["status"])
        self.assertEqual((-20,-100,1420,200),shape_of(door["clearance_footprint"]).bounds)

    def test_source_measured_recessed_frame_is_rechecked_with_the_same_bearing_rule(self):
        walls=[rect('left','A-WALL',-2000,-100,-20,200),rect('right','A-WALL',1420,-100,3500,200)]
        frames=[placed(line('j1','WINDOW',(-40,-210),(-40,-180)),'D'),
                placed(line('j2','WINDOW',(1460,-180),(1460,-150)),'D'),
                placed(line('p1','WINDOW',(-40,-180),(800,-180)),'D'),
                placed(line('p2','WINDOW',(650,-150),(1460,-150)),'D')]
        document={'entities':[*walls,container('D','sliding door','WINDOW')],'resolved_instances':frames}
        result=self.recognize(document)
        door=result['door_assemblies'][0]
        self.assertEqual('plan_geometry_resolved',door['status'])
        decisions={'layer_mapping':{'A-WALL':{'category':'architectural_wall','height_mm':3200,'footprint_confirmed':True},
                                    'WINDOW':{'category':'opening','height_mm':2000,'footprint_confirmed':True}}}
        payload=prepare_model(document,{'automatic_plan':result},decisions)
        self.assertEqual([],payload['blockers'])
        lintels=[row for row in payload['groups']if row['id'].startswith('lintel:')]
        self.assertEqual(1,len(lintels))
        self.assertEqual((-20,-100,1420,200),shape_of(lintels[0]).bounds)
        self.assertEqual(2000,lintels[0]['base_z_mm'])

    def test_disjoint_external_hatch_loops_are_not_misread_as_holes(self):
        from dwg_semantic.plan_geometry import hatch_profiles
        loops=[{"supported":True,"loop_type":"External","vertices_world":[point(x,y)for x,y in coords]}
               for coords in([(-100,0),(0,0),(0,100),(-100,100)],[(100,0),(200,0),(200,100),(100,100)])]
        profiles,errors=hatch_profiles({"boundary_loops":loops})
        self.assertEqual([],errors)
        self.assertEqual(2,len(profiles))
        self.assertTrue(all(not p["footprint"]["holes"]for p in profiles))

    def test_native_hatch_inner_loops_remain_void_and_island_is_material(self):
        from dwg_semantic.plan_geometry import hatch_profiles
        loops=[{"supported":True,"loop_type":"External","vertices_world":[point(x,y)for x,y in coords]}
               for coords in([(-1000,-1000),(1000,-1000),(1000,1000),(-1000,1000)],
                              [(-800,-800),(800,-800),(800,800),(-800,800)],
                              [(-100,-100),(100,-100),(100,100),(-100,100)])]
        profiles,errors=hatch_profiles({"boundary_loops":loops})
        self.assertEqual([],errors)
        self.assertEqual(2,len(profiles))
        material=unary_union([shape_of(row)for row in profiles])
        self.assertFalse(material.covers(Point(500,500)))
        self.assertTrue(material.covers(Point(0,0)))

    def test_complete_double_assembly_reaches_model_planner_without_half_leaf_fallback(self):
        walls=[rect("wall-left","A-WALL",-2000,-100,-50,200),rect("wall-right","A-WALL",1450,-100,3500,200)]
        leaves=[placed(line("leaf-left","DOOR",(0,0),(0,700)),"D"),placed(line("leaf-right","DOOR",(1400,0),(1400,700)),"D"),
                swing_arc("arc-left",(0,0),(700,0),(0,700),"D"),swing_arc("arc-right",(1400,0),(700,0),(1400,700),"D")]
        document={"entities":[*walls,container("D","double door","DOOR")],"resolved_instances":leaves}
        result=self.recognize(document)
        decisions={"layer_mapping":{"A-WALL":{"category":"architectural_wall","height_mm":3000,"footprint_confirmed":True},
                                     "DOOR":{"category":"opening","height_mm":2100,"footprint_confirmed":True}}}
        payload=prepare_model(document,{"automatic_plan":result},decisions)
        self.assertEqual([],payload["blockers"])
        self.assertEqual(1,len(payload["automatic_recognition_transfer"]["opening_candidate_ids"]))
        lintels=[row for row in payload["groups"]if row["id"].startswith("lintel:")]
        self.assertEqual(1,len(lintels))
        self.assertEqual((-50,-100,1450,200),shape_of(lintels[0]).bounds)

    def test_coincident_source_versions_form_one_physical_door_without_losing_sources(self):
        from dwg_semantic.opening_recognition import canonical_door_assemblies
        fp={"outer":[[0,0],[1000,0],[1000,200],[0,200]],"holes":[]}
        rows=[{"id":"old","instance_path":"A","source_container_entity_id":"assembly:A","source_entity_ids":["a"],"clearance_footprint":fp,"leaf_count":0,"leaf_span_mm":900},
              {"id":"new","instance_path":"B","source_container_entity_id":"assembly:B","source_entity_ids":["b"],"clearance_footprint":fp,"leaf_count":0,"leaf_span_mm":1000}]
        merged=canonical_door_assemblies(rows)
        self.assertEqual(1,len(merged))
        self.assertEqual({"a","b"},set(merged[0]["source_entity_ids"]))
        self.assertEqual(["A","B"],merged[0]["source_instance_paths"])

    def test_regression_checker_detects_refilled_hole_and_half_door(self):
        from dwg_semantic.recognition_regression import check_recognition
        fp={"outer":[[0,0],[1000,0],[1000,200],[0,200]],"holes":[]}
        result={"source_sha256":"S","wall_materials_at_door_level":[{"footprint":fp}],"door_assemblies":[
            {"id":"half","source_entity_ids":["d"],"source_container_entity_id":"door","clearance_footprint":{"outer":[[0,0],[500,0],[500,200],[0,200]],"holes":[]},"leaf_count":1}]}
        reference={"source_sha256":"S","checks":[
            {"id":"full door","kind":"door_covers_reviewed_span","collection":"doors","source_container_entity_id":"door","axis_world_mm":[[0,100],[1000,100]],"minimum_leaf_count":2},
            {"id":"no wall in door","kind":"door_cuts_clear","collection":"doors","source_container_entity_id":"door"}]}
        report=check_recognition(result,reference)
        self.assertFalse(report["all_pass"])
        self.assertEqual(0,report["passed"])


if __name__=="__main__":
    unittest.main()

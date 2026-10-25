"""Associate slab marks with source-supported boundaries, then deduplicate cuts."""
from __future__ import annotations

from collections import defaultdict
import math
import re

from shapely import set_precision
from shapely.affinity import affine_transform
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import polygonize, unary_union

from .plan_geometry import footprint, layer_of, matches, parts, shape_of, stable_id, xy


def _same_source_plane(first, second):
    # CAD insertion matrices leave nanometre-scale numerical Z noise. This is
    # not a genuine second plane and must not disable nesting/priority rules.
    if first is None or second is None:
        return first is None and second is None
    return abs(first-second) <= 1e-6


def _closed_polygon(index, key):
    if index.planes[key]["nonplanar"]:
        return None
    e=index.by_id[key]
    lines=index.lines_by_id[key]
    if not lines or not (e.get("closed") or math.dist(lines[0].coords[0],lines[-1].coords[-1])<=index.precision):
        return None
    faces=list(polygonize(unary_union([set_precision(line,index.precision)for line in lines])))
    if len(faces)!=1:
        return None
    return faces[0]


def lift_shafts(index, active):
    candidates=[]
    for key in sorted(active):
        e=index.by_id[key]
        layer=index.layers[key]
        if not matches(layer,["lift","elevator","電梯","电梯","井道"]):
            continue
        if matches(layer,["door","門","门"]):
            continue
        shape=_closed_polygon(index,key)
        if shape is None or shape.area<100000:
            continue
        candidates.append((key,shape,index.planes[key]["plane_z_mm"]))
    unique={}
    for key,shape,z in candidates:
        signature=(round(z,6),shape.normalize().wkb_hex)
        if signature not in unique:
            unique[signature]=[shape,[],z]
        unique[signature][1].append(key)
    output=[]
    for shape,ids,z in unique.values():
        enclosing=[other for other,_,other_z in unique.values()if abs(z-other_z)<1e-6 and other.area>shape.area+index.precision**2 and other.covers(shape)]
        if enclosing:
            continue
        marks=[]
        inside=[]
        for i in index.tree.query(shape):
            i=int(i)
            key=index.line_ids[i]
            if key in ids or key not in active or not matches(index.layers[key],["lift","elevator","電梯","电梯"]):
                continue
            if index.planes[key]["nonplanar"] or abs(index.planes[key]["plane_z_mm"]-z)>1e-6:
                continue
            line=index.lines[i]
            if shape.buffer(index.precision).covers(line):
                inside.append((key,line))
        for number,(key,line)in enumerate(inside):
            a,b=line.coords[0],line.coords[-1]
            if abs(a[0]-b[0])<index.precision or abs(a[1]-b[1])<index.precision:
                continue
            for other_id,other in inside[number+1:]:
                crossing=line.intersection(other)
                if crossing.geom_type=="Point" and shape.contains(crossing) and line.boundary.distance(crossing)>index.precision and other.boundary.distance(crossing)>index.precision:
                    marks.extend([key,other_id])
        output.append({"id":stable_id("LIFT-SHAFT",ids,shape),"kind":"elevator_shaft",
                       "footprint":footprint(shape),"source_entity_ids":sorted(set(ids+marks)),
                       "boundary_source_entity_ids":sorted(ids),"car_mark_source_entity_ids":sorted(set(marks)),
                       "area_mm2":shape.area,"duplicate_boundary_count":len(ids),
                       "source_plane_z_mm":z,
                       "boundary_basis":"entire_source_shaft_enclosure_not_internal_car_or_cross",
                       "status":"opening_candidate_requires_slab_owner","applies_to_system_id":None})
    return output


def _interval_length(intervals,tolerance):
    merged=[]
    for lo,hi in sorted(intervals):
        if merged and lo<=merged[-1][1]+tolerance:
            merged[-1]=(merged[-1][0],max(merged[-1][1],hi))
        else:
            merged.append((lo,hi))
    return sum(b-a for a,b in merged)


def infer_bent_boundary(index, key, active, *, search=100):
    """Use the symbol only to query four real source-supported sides.

    The endpoint rectangle is never accepted on its own. Every resulting edge
    must have positive, nearly complete collinear support in placed CAD leaves.
    """
    e=index.by_id[key]
    if index.planes[key]["nonplanar"]:
        return None
    z=index.planes[key]["plane_z_mm"]
    points=e.get("vertices_world")or e.get("vertices")or[]
    if e.get("closed") or len(points)!=3:
        return None
    a,knee,b=[xy(p)for p in points]
    diagonal=math.dist(a,b)
    if diagonal<200:
        return None
    nearby=index.tree.query(LineString([a,b]).envelope.buffer(search))
    allowed=[]
    for i in nearby:
        i=int(i);identity=index.line_ids[i]
        if identity==key or identity not in active:
            continue
        if index.planes[identity]["nonplanar"] or abs(index.planes[identity]["plane_z_mm"]-z)>1e-6:
            continue
        layer=index.layers[identity]
        if matches(layer,["furn","equipment","ceiling","light","家具","設備","设备","天花"]):
            continue
        allowed.append(i)
    angles={0.0}
    for i in allowed:
        line=index.lines[i]
        if line.length<min(200,diagonal/4):
            continue
        p,q=line.coords[0],line.coords[-1]
        angle=math.atan2(q[1]-p[1],q[0]-p[0])%(math.pi/2)
        angles.add(round(angle,6))
    solutions=[]
    for angle in sorted(angles):
        u=(math.cos(angle),math.sin(angle));n=(-u[1],u[0])
        matrix=[u[0],u[1],n[0],n[1],-a[0]*u[0]-a[1]*u[1],-a[0]*n[0]-a[1]*n[1]]
        local=lambda p:((p[0]-a[0])*u[0]+(p[1]-a[1])*u[1],(p[0]-a[0])*n[0]+(p[1]-a[1])*n[1])
        lb,lk=local(b),local(knee)
        x0,x1=sorted((0,lb[0]));y0,y1=sorted((0,lb[1]))
        if min(x1-x0,y1-y0)<200 or not x0<lk[0]<x1 or not y0<lk[1]<y1:
            continue
        first=(knee[0]-a[0],knee[1]-a[1]);second=(b[0]-knee[0],b[1]-knee[1])
        sine=abs(first[0]*second[1]-first[1]*second[0])/(math.hypot(*first)*math.hypot(*second))
        if sine<math.sin(math.radians(15)):
            continue
        local_lines=[(index.line_ids[i],affine_transform(index.lines[i],matrix))for i in allowed]
        side_audit=[];limits=[]
        for axis,fixed,lo,hi in ((0,y0,x0,x1),(0,y1,x0,x1),(1,x0,y0,y1),(1,x1,y0,y1)):
            bins=defaultdict(list)
            for identity,line in local_lines:
                p,q=line.coords[0],line.coords[-1]
                normal=1-axis
                if abs(p[normal]-q[normal])>index.precision*2:
                    continue
                station=(p[normal]+q[normal])/2
                if abs(station-fixed)>search:
                    continue
                start,end=sorted((p[axis],q[axis]))
                l,r=max(lo,start),min(hi,end)
                if r-l>index.precision:
                    bins[round(station/index.precision)].append((l,r,identity,station))
            supported=[]
            for rows in bins.values():
                coverage=_interval_length([(r[0],r[1])for r in rows],index.precision)
                if coverage>=hi-lo-max(index.precision*4,(hi-lo)*.001):
                    station=sum(r[3]*(r[1]-r[0])for r in rows)/sum(r[1]-r[0]for r in rows)
                    supported.append((abs(station-fixed),station,coverage,sorted({r[2]for r in rows})))
            if not supported:
                break
            error,station,coverage,refs=min(supported,key=lambda row:(row[0],row[1]))
            side_audit.append({"axis":axis,"symbol_station_mm":fixed,"source_station_mm":station,
                               "alignment_mm":station-fixed,"coverage_ratio":min(1,coverage/(hi-lo)),
                               "source_entity_ids":refs})
            limits.append(station)
        if len(side_audit)!=4:
            continue
        bottom,top,left,right=limits
        if not left<right or not bottom<top:
            continue
        local_shape=box(left,bottom,right,top)
        shape=affine_transform(local_shape,[u[0],n[0],u[1],n[1],a[0],a[1]])
        refs=sorted({key,*[identity for side in side_audit for identity in side["source_entity_ids"]]})
        solutions.append((sum(abs(row["alignment_mm"])for row in side_audit),shape,refs,side_audit))
    if not solutions:
        return None
    error,shape,refs,audit=min(solutions,key=lambda row:row[0])
    return {"id":stable_id("BENT-OPENING",refs,shape),"kind":"bent_slab_mark",
            "footprint":footprint(shape),"source_entity_ids":refs,"symbol_source_entity_ids":[key],
            "area_mm2":shape.area,"side_support_audit":audit,
            "source_plane_z_mm":z,
            "boundary_basis":"four_source_supported_sides_queried_by_open_bent_mark",
            "status":"opening_candidate_requires_slab_owner","applies_to_system_id":None}


def canonical_openings(rows,precision):
    """Overlapping/nested marks identify one cut, with all provenance retained."""
    output=[]
    pending=list(rows)
    while pending:
        cluster=[pending.pop(0)]
        shape=shape_of(cluster[0])
        changed=True
        while changed:
            changed=False
            for row in list(pending):
                if not _same_source_plane(row.get("source_plane_z_mm"), cluster[0].get("source_plane_z_mm")):
                    continue
                other=shape_of(row)
                if shape.intersection(other).area>precision**2:
                    shape=shape.union(other);cluster.append(row);pending.remove(row);changed=True
        for part in parts(shape):
            members=[r for r in cluster if part.intersection(shape_of(r)).area>precision**2]
            refs=sorted({key for row in members for key in row["source_entity_ids"]})
            output.append({"id":stable_id("SLAB-CUT",refs,part),"footprint":footprint(part),
                           "area_mm2":part.area,"source_entity_ids":refs,"source_mark_ids":[r["id"]for r in members],
                           "kinds":sorted({r["kind"]for r in members}),"applies_to_system_id":None,
                           "source_plane_z_mm":members[0].get("source_plane_z_mm"),
                           "status":"plan_opening_candidate_owner_and_elevation_independent"})
    return output


def recognize_slab_openings(index, active, settings, legacy_review=None,*,slab_decisions=None, wall_context=None, door_context=None):
    config=(slab_decisions or{}).get("slabs",slab_decisions or{})
    overrides=config.get("region_overrides",{})
    explicit_non_openings=[]
    decision_issues=[]
    def apply_explicit(rows):
        from .slabs import _region_decision
        accepted=[]
        for row in rows:
            exact_ids=row.get("symbol_source_entity_ids",row.get("boundary_source_entity_ids",[]))
            polygon=shape_of(row)
            associated=[]
            for note in (legacy_review or{}).get("annotations",[]):
                position=note.get("position_mm")
                heads=[line.get("start_world")for line in note.get("leader_lines",[])]
                if (position and polygon.covers(Point(position)))or any(isinstance(p,dict)and "x"in p and "y"in p
                    and polygon.covers(Point(p["x"],p["y"]))for p in heads):
                    associated.append(note)
            decision=_region_decision(row["id"],[{"source_entity_ids":exact_ids}],associated,overrides)
            row["effective_decision"]=decision
            if decision["basis"]!="authorized_bounded_mark_default":
                row["explicit_decision"]=decision
            if decision["action"]!="opening":
                explicit_non_openings.append(row)
                continue
            accepted.append(row)
        return accepted
    shafts=lift_shafts(index,active)
    closed=[]
    for row in (legacy_review or {}).get("region_candidates",[]):
        if row.get("default_decision",{}).get("action")!="opening":
            if row.get("default_decision",{}).get("basis")in{"explicit_source_entity_override","explicit_no_opening_note","explicit_lowered_slab_note","invalid_or_conflicting_explicit_override","explicit_annotation_conflict"}:
                explicit_non_openings.append({"id":row["id"],"kind":"bounded_slab_mark",
                    "source_entity_ids":row["source_entity_ids"],"footprint":{"outer":row["outer"],"holes":[]},
                    "source_plane_z_mm":row.get("source_plane_z_mm"),
                    "explicit_decision":row["default_decision"]})
            continue
        shape=Polygon(row["outer"])
        closed.append({"id":row["id"],"kind":"bounded_slab_mark","footprint":footprint(shape),
                       "source_entity_ids":row["source_entity_ids"],"area_mm2":shape.area,
                       "effective_decision":row["default_decision"],
                       "source_plane_z_mm":row.get("source_plane_z_mm"),
                       "symbol_source_entity_ids":sorted({key for symbol in row.get("symbols",[])for key in symbol["source_entity_ids"]}),
                       "status":"opening_candidate_requires_slab_owner","applies_to_system_id":None})
    bent=[];unresolved=[]
    for key in sorted(active):
        e=index.by_id[key]
        if e.get("type")not in{"LWPOLYLINE","POLYLINE"} or e.get("closed"):
            continue
        if not matches(index.layers[key],settings.get("slab_mark_layer_terms",["surface","slab","opening","樓板","楼板","開口","开口"])):
            continue
        points=e.get("vertices_world")or e.get("vertices")or[]
        if len(points)!=3:
            continue
        found=infer_bent_boundary(index,key,active,search=settings["symbol_boundary_search_mm"])
        if found:
            if any(_same_source_plane(row.get("source_plane_z_mm"),found.get("source_plane_z_mm")) and shape_of(row).covers(shape_of(found))for row in closed):
                unresolved.append({"source_entity_ids":[key],"status":"covered_by_existing_bounded_symbol"})
            else:
                bent.append(found)
        else:
            unresolved.append({"source_entity_ids":[key],"status":"symbol_boundary_review_required",
                               "reason":"Open three-point path has no complete source-supported opening boundary"})
    shafts=apply_explicit(shafts)
    bent=apply_explicit(bent)
    def priority(row):
        decision=row.get("effective_decision",row.get("explicit_decision",{}))
        return (3 if decision.get("basis")=="explicit_source_entity_override" else
                2 if decision.get("basis")not in{None,"authorized_bounded_mark_default"}else 1)
    def respect_protected_regions(rows):
        kept=[]
        for row in rows:
            original=shape_of(row)
            same_plane=[region for region in explicit_non_openings if _same_source_plane(region.get("source_plane_z_mm"),row.get("source_plane_z_mm"))]
            conflicts=[region for region in same_plane if priority(region)==priority(row)
                       and original.intersection(shape_of(region)).area>index.precision**2]
            if conflicts:
                decision_issues.append({"code":"equal_priority_slab_region_decision_conflict","opening_mark_id":row["id"],
                    "source_entity_ids":sorted(set(row["source_entity_ids"])|{key for region in conflicts for key in region["source_entity_ids"]}),
                    "protected_region_ids":[region["id"]for region in conflicts]})
                continue
            protections=[region for region in same_plane if priority(region)>priority(row)
                         and original.intersection(shape_of(region)).area>index.precision**2]
            if not protections:
                kept.append(row)
                continue
            remaining=original.difference(unary_union([shape_of(region)for region in protections]))
            proof={"source_entity_ids":row["source_entity_ids"],"status":"default_cut_suppressed_by_higher_priority_retained_or_lowered_region",
                   "protected_region_ids":[region["id"]for region in protections],"suppressed_area_mm2":original.area-remaining.area}
            unresolved.append(proof)
            for part in parts(remaining):
                kept.append({**row,"id":stable_id("SLAB-MARK-REMAINDER",row["source_entity_ids"],part),
                             "footprint":footprint(part),"area_mm2":part.area,"priority_clip_audit":proof})
        return kept
    shafts=respect_protected_regions(shafts)
    closed=respect_protected_regions(closed)
    bent=respect_protected_regions(bent)
    canonical=canonical_openings(closed+bent,index.precision)
    stair_ids={key for key in active if matches(index.layers[key],["stair","樓梯","楼梯"])and index.lines_by_id[key]}
    stair_planes=defaultdict(set)
    stair_regions=[]
    for key in sorted(stair_ids):
        plane=index.planes[key]
        if plane['nonplanar']:
            stair_regions.append({'id':stable_id('STAIR-REVIEW',[key]),'source_entity_ids':[key],
                'source_plane_z_mm':None,'source_z_range_mm':plane['z_range_mm'],
                'locator_bounds_mm':list(unary_union(index.lines_by_id[key]).bounds),
                'status':'nonplanar_stair_source_requires_projection_evidence',
                'required_scope':'entire_enclosure_including_landings','automatically_subtracted':False})
        else:
            stair_planes[plane['plane_z_mm']].add(key)
    for z,plane_ids in sorted(stair_planes.items()):
        stair_lines=[line for key in plane_ids for line in index.lines_by_id[key]]
        # This buffer clusters source marks only; its envelope is a locator,
        # never a slab cut boundary. One native polyline may contain a complete
        # flight: counting placed objects cannot establish stair completeness.
        zones=parts(unary_union([line.buffer(100)for line in stair_lines]))
        for zone in zones:
            ids=sorted({key for key in plane_ids if any(line.intersects(zone)for line in index.lines_by_id[key])})
            stair_regions.append({"id":stable_id("STAIR-REVIEW",ids),"source_entity_ids":ids,
                                  'source_plane_z_mm':z,
                                  "locator_bounds_mm":list(unary_union([line for key in ids for line in index.lines_by_id[key]]).bounds),
                                  "status":"exact_stair_enclosure_boundary_and_slab_owner_require_review",
                                  'required_scope':'entire_enclosure_including_landings',
                                  "automatically_subtracted":False})
    stair_openings=[]
    if stair_regions and wall_context:
        # Closing an actual, measured door throat is a semantic boundary, not
        # an invented rectangle around the stair symbol. Enclosed free cells
        # include landings but exclude adjacent lobbies separated by walls.
        def context_plane(row):
            ids=[key for key in row.get('source_entity_ids',[])if key in index.planes and index.lines_by_id[key]]
            if ids:
                if any(index.planes[key]['nonplanar']for key in ids):
                    return None
                values=[index.planes[key]['plane_z_mm']for key in ids]
                return values[0]if max(values)-min(values)<=1e-6 else None
            value=row.get('source_plane_z_mm')
            return value if isinstance(value,(int,float))and not isinstance(value,bool)and math.isfinite(value)else None
        contexts={}
        def plane_cells(z):
            if z not in contexts:
                wall_rows=[row for row in wall_context if context_plane(row)is not None and abs(context_plane(row)-z)<=1e-6]
                door_rows=[row for row in (door_context or[])if row.get('status')=='plan_geometry_resolved'
                           and row.get('clearance_footprint')and context_plane(row)is not None
                           and abs(context_plane(row)-z)<=1e-6]
                material=unary_union([shape_of(row)for row in wall_rows]+
                                     [shape_of(row['clearance_footprint'])for row in door_rows])
                cells=list(polygonize(unary_union([row.boundary for row in parts(material)])))
                free=[row for row in cells if row.area>settings['minimum_material_area_mm2']
                      and row.intersection(material).area<=max(.001,index.precision*row.length)]
                contexts[z]=(free,wall_rows,door_rows)
            return contexts[z]
        unresolved_stairs=[]
        enclosures={}
        for region in stair_regions:
            z=region.get('source_plane_z_mm')
            if z is None:
                unresolved_stairs.append(region)
                continue
            free_cells,wall_rows,door_rows=plane_cells(z)
            lines=[line for key in region['source_entity_ids']for line in index.lines_by_id[key]]
            total=sum(line.length for line in lines)
            # A source-bound, explicitly reviewed symbol-membership tolerance
            # may accommodate drafting overhang into an actual wall. It NEVER
            # buffers the opening: the selected, unchanged free cell is cut.
            mark_tolerance=max(index.precision,settings.get('stair_mark_boundary_tolerance_mm',0))
            choices=[cell for cell in free_cells if sum(line.intersection(cell.buffer(mark_tolerance)).length for line in lines)
                     >=total*.99]
            if len(choices)!=1:
                unresolved_stairs.append({**region,'required_scope':'entire_enclosure_including_landings',
                    'reason':'source_wall_and_measured_door_throats_do_not_prove_one_complete_stair_enclosure'})
                continue
            cell=choices[0]
            same_plane={key for key in active if not index.planes[key]['nonplanar']
                        and abs(index.planes[key]['plane_z_mm']-z)<=1e-6}
            boundary_sources=set(index.refs(cell.boundary,same_plane))
            for wall in wall_rows:
                if shape_of(wall).boundary.intersection(cell.boundary).length>index.precision:
                    boundary_sources.update(wall['source_entity_ids'])
            threshold_ids=[]
            for door in door_rows:
                if door.get('clearance_footprint') and shape_of(door['clearance_footprint']).boundary.intersection(cell.boundary).length>index.precision:
                    boundary_sources.update(door['source_entity_ids']);threshold_ids.append(door['id'])
            signature=(z,cell.normalize().wkb_hex)
            if signature not in enclosures:
                enclosures[signature]={'shape':cell,'marks':set(),'boundary':set(),'thresholds':set(),
                                      'mark_tolerance':mark_tolerance,'mark_overhang':0.}
            enclosure=enclosures[signature]
            enclosure['marks'].update(region['source_entity_ids'])
            enclosure['boundary'].update(boundary_sources)
            enclosure['thresholds'].update(threshold_ids)
            enclosure['mark_overhang']=max(enclosure['mark_overhang'],
                max((Point(p).distance(cell)for line in lines for p in line.coords),default=0))
        for (z,_),enclosure in sorted(enclosures.items()):
            cell=enclosure['shape']
            refs=sorted(enclosure['boundary']|enclosure['marks'])
            stair_openings.append({'id':stable_id('STAIR-ENCLOSURE',refs,cell),'kind':'whole_stair_enclosure',
                'footprint':footprint(cell),'area_mm2':cell.area,'source_entity_ids':refs,
                'source_plane_z_mm':z,
                'stair_mark_source_entity_ids':sorted(enclosure['marks']),
                'boundary_source_entity_ids':sorted(enclosure['boundary']),
                'door_threshold_candidate_ids':sorted(enclosure['thresholds']),
                'mark_membership_tolerance_mm':enclosure['mark_tolerance'],
                'maximum_mark_vertex_outside_cell_mm':enclosure['mark_overhang'],
                'includes_landings':True,'boundary_basis':'actual_enclosed_wall_inner_faces_and_measured_door_throats_not_symbol_bbox',
                'status':'opening_candidate_requires_slab_owner','applies_to_system_id':None})
        stair_regions=unresolved_stairs
    return {"elevator_shafts":shafts,"bounded_marks":closed,"open_bent_marks":bent,
            "canonical_symbol_cut_regions":canonical,"unresolved_or_covered_marks":unresolved,
            "explicit_non_opening_regions":explicit_non_openings,
            "decision_issues":decision_issues,
            "staircase_openings":stair_openings,
            "stair_boundary_review_requests":stair_regions,
            "counts":{"elevator_shafts":len(shafts),"bounded_symbol_marks":len(closed),"open_bent_marks":len(bent),
                      "canonical_symbol_cut_regions":len(canonical),"staircase_openings":len(stair_openings),"stair_boundary_review_requests":len(stair_regions)},
            "floor_owner_status":"bind_each_opening_to_the_actual_slab_system_before_solid_subtraction"}

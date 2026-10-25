"""Whole door assemblies and host-face lintel footprints.

Swing arcs locate every leaf; sliding/folding symbols use frame pairs. Neither
leaf bounding boxes nor swing sweeps are used as wall material footprints.
"""
from __future__ import annotations

from collections import defaultdict
import math

from shapely.affinity import affine_transform
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from .plan_geometry import dimensions, footprint, layer_of, matches, parts, shape_of, stable_id, xy


DOOR_TERMS = ["door", "門", "门", "sliding", "folding"]


def _local(shape, axis):
    a, b = axis
    length = math.dist(a, b)
    u = ((b[0]-a[0])/length, (b[1]-a[1])/length)
    n = (-u[1], u[0])
    return affine_transform(shape, [u[0], u[1], n[0], n[1], -a[0]*u[0]-a[1]*u[1], -a[0]*n[0]-a[1]*n[1]])


def _world(shape, axis):
    a, b = axis
    length = math.dist(a, b)
    u = ((b[0]-a[0])/length, (b[1]-a[1])/length)
    n = (-u[1], u[0])
    return affine_transform(shape, [u[0], n[0], u[1], n[1], a[0], a[1]])


def _axis_clusters(axes, tolerance):
    clusters = []
    for row in sorted(axes, key=lambda r: (-r["length_mm"], r["source_entity_ids"])):
        axis = row["axis"]
        placed = False
        for cluster in clusters:
            local = _local(LineString(axis), cluster[0]["axis"])
            if abs(local.coords[-1][1]-local.coords[0][1]) <= max(tolerance,local.length*.01) and max(abs(p[1]) for p in local.coords) <= tolerance:
                cluster.append(row)
                placed = True
                break
        if not placed:
            clusters.append([row])
    output = []
    for cluster in clusters:
        base = cluster[0]["axis"]
        projected = [_local(LineString(row["axis"]), base) for row in cluster]
        intervals = sorted((min(p[0]for p in line.coords),max(p[0]for p in line.coords))for line in projected)
        chains = []
        for lo, hi in intervals:
            if chains and lo <= chains[-1][1]+tolerance:
                chains[-1] = (chains[-1][0], max(hi,chains[-1][1]))
            else:
                chains.append((lo,hi))
        for lo, hi in chains:
            line = _world(LineString([(lo,0),(hi,0)]),base)
            members = [r for r,p in zip(cluster,projected) if p.bounds[0] >= lo-tolerance and p.bounds[2] <= hi+tolerance]
            output.append({"axis": [list(line.coords[0]),list(line.coords[-1])], "length_mm": line.length,
                           "leaf_matches": members, "source_entity_ids": sorted({k for r in members for k in r["source_entity_ids"]}),
                           "hinge_count": len({tuple(round(v,2)for v in r["hinge"])for r in members})})
    return output


def swing_axes(index, ids, tolerance=2.0):
    arcs = [(key,index.by_id[key]) for key in ids if index.by_id[key].get("type")=="ARC"]
    lines = [(key,line) for key in ids if index.by_id[key].get("type")in{"LINE","LWPOLYLINE","POLYLINE"} for line in index.lines_by_id[key]]
    leaves = []
    seen = set()
    for arc_id, arc in arcs:
        center = arc.get("center_world") or arc.get("center")
        start, end = arc.get("start_world"),arc.get("end_world")
        samples = arc.get("curve_points_world") or arc.get("curve_points")
        if not start and samples:
            start,end = samples[0],samples[-1]
        if not center or not start or not end:
            continue
        hinge, ends = xy(center), [xy(start),xy(end)]
        radius = math.dist(hinge,ends[0])
        if radius < 100:
            continue
        edge_tolerance=max(tolerance,min(100,radius*.05))
        for leaf_id,line in lines:
            if abs(line.length-radius)>max(tolerance,radius*.04):
                continue
            for fixed,free in ((line.coords[0],line.coords[-1]),(line.coords[-1],line.coords[0])):
                if math.dist(fixed,hinge)>edge_tolerance:
                    continue
                distance,which = min((math.dist(free,p),i)for i,p in enumerate(ends))
                if distance>edge_tolerance:
                    continue
                target = ends[1-which]
                key = (arc_id,leaf_id,tuple(hinge),tuple(target))
                if key in seen:
                    continue
                seen.add(key)
                leaves.append({"axis":[list(hinge),list(target)],"hinge":list(hinge),
                               "source_entity_ids":[arc_id,leaf_id],"length_mm":math.dist(hinge,target),
                               "free_endpoint_match_error_mm":distance,
                               "hinge_to_leaf_edge_offset_mm":math.dist(fixed,hinge),
                               "leaf_edge_matching_limit_mm":edge_tolerance})
    return _axis_clusters(leaves,tolerance)


def frame_axes(index, ids, walls, maximum_depth=600, tolerance=2.0):
    """Opposite frame crosspieces, each supported by nearby wall material."""
    wall_union = unary_union([shape_of(w) for w in walls])
    candidates = [(key,line)for key in ids for line in index.lines_by_id[key] if 5<=line.length<=maximum_depth]
    output = []
    for i,(first_id,first)in enumerate(candidates):
        a,b = first.coords[0],first.coords[-1]
        u = ((b[0]-a[0])/first.length,(b[1]-a[1])/first.length)
        for second_id,second in candidates[i+1:]:
            if first_id==second_id and first.equals(second):
                continue
            c,d = second.coords[0],second.coords[-1]
            if abs(u[0]*(d[1]-c[1])-u[1]*(d[0]-c[0]))>second.length*.01:
                continue
            if abs(first.length-second.length)>max(tolerance,first.length*.1):
                continue
            axis = [list(first.interpolate(.5,normalized=True).coords[0]),list(second.interpolate(.5,normalized=True).coords[0])]
            length = math.dist(*axis)
            if length<max(300,first.length*1.5):
                continue
            stagger=(axis[1][0]-axis[0][0])*u[0]+(axis[1][1]-axis[0][1])*u[1]
            if abs(stagger)>max(tolerance,max(first.length,second.length)*1.05):
                continue
            # Sliding leaves can have staggered crosspieces. Project their
            # stations onto the common direction normal to the frame pieces.
            normal=(-u[1],u[0]);station=sum((axis[1][j]-axis[0][j])*normal[j]for j in(0,1))
            midpoint=[(axis[0][j]+axis[1][j])/2 for j in(0,1)]
            axis=[[midpoint[j]-normal[j]*station/2 for j in(0,1)],
                  [midpoint[j]+normal[j]*station/2 for j in(0,1)]]
            length=math.dist(*axis)
            contacts = [Point(p).distance(wall_union)for p in axis]
            if max(contacts)>150:
                continue
            output.append({"axis":axis,"length_mm":length,"leaf_matches":[],"hinge_count":0,
                           "source_entity_ids":sorted({first_id,second_id}),
                           "frame_depth_mm":(first.length+second.length)/2,
                           "frame_normal_stagger_mm":abs(stagger),
                           "frame_wall_distances_mm":contacts})
    # Several frame edges can occupy one jamb. Preserve their source evidence,
    # then let host-face matching choose the physical wall opening.
    unique = {}
    for row in output:
        line = LineString(row["axis"])
        unique[line.normalize().wkb_hex] = row
    return list(unique.values())


def host_footprint(axis, walls, *, frame_lines=(), search=450, recess=150, tolerance=.1, allow_frame_bearing=False):
    """Find facing jamb endcaps and both parallel wall faces in local axes."""
    length = math.dist(*axis)
    if length<=0:
        return {"status":"zero_length_axis"}
    left,right = [],[]
    bearing=recess if allow_frame_bearing else tolerance
    normal_recess=recess if allow_frame_bearing else tolerance
    local_hosts = []
    for wall in walls:
        shape = _local(shape_of(wall),axis)
        if shape.distance(LineString([(0,0),(length,0)]))>search:
            continue
        local_hosts.append((wall,shape))
        for ring in [shape.exterior,*shape.interiors]:
            for a,b in zip(ring.coords,list(ring.coords)[1:]):
                if abs(a[0]-b[0])>tolerance or abs(a[1]-b[1])<40:
                    continue
                t = (a[0]+b[0])/2
                lo,hi = sorted((a[1],b[1]))
                if (min(abs(lo),abs(hi))>normal_recess and not lo<=0<=hi) or hi-lo>search*2:
                    continue
                row = {"station_mm":t,"normal_interval_mm":[lo,hi],"host":wall}
                if -search<=t<=bearing:
                    left.append(row)
                if length-bearing<=t<=length+search:
                    right.append(row)
    def consolidated(rows):
        groups=defaultdict(list)
        for row in rows:
            groups[(row["host"]["id"],round(row["station_mm"]/tolerance))].append(row)
        output=[]
        for group in groups.values():
            intervals=[]
            for row in sorted(group,key=lambda r:r["normal_interval_mm"]):
                lo,hi=row["normal_interval_mm"]
                if intervals and lo<=intervals[-1][1]+tolerance:
                    intervals[-1][1]=max(intervals[-1][1],hi)
                else:
                    intervals.append([lo,hi])
            output += [{**group[0],"normal_interval_mm":interval}for interval in intervals]
        return output
    left,right=consolidated(left),consolidated(right)
    pairs = []
    for a in left:
        for b in right:
            lo1,hi1=a["normal_interval_mm"];lo2,hi2=b["normal_interval_mm"]
            mismatch = max(abs(lo1-lo2),abs(hi1-hi2))
            common=max(0,min(hi1,hi2)-max(lo1,lo2))
            if common<min(hi1-lo1,hi2-lo2)*.98 or common<40:
                continue
            normal_distance=max(0,max(lo1,lo2),-min(hi1,hi2))
            score=abs(a["station_mm"])+abs(b["station_mm"]-length)+normal_distance*2
            pairs.append((score,a,b))
    if pairs:
        pairs.sort(key=lambda row:(row[0],row[1]["host"]["id"],row[2]["host"]["id"]))
        score,a,b=pairs[0]
        def pair_shape(pair):
            _,left_cap,right_cap=pair
            lo=max(left_cap["normal_interval_mm"][0],right_cap["normal_interval_mm"][0])
            hi=min(left_cap["normal_interval_mm"][1],right_cap["normal_interval_mm"][1])
            return box(left_cap["station_mm"],lo,right_cap["station_mm"],hi)
        chosen=pair_shape(pairs[0])
        competing=[p for p in pairs[1:]if abs(p[0]-score)<=tolerance
                   and chosen.symmetric_difference(pair_shape(p)).area>max(tolerance**2,chosen.area*1e-6)]
        if competing:
            return {"status":"ambiguous_opposite_jamb_hosts","candidate_host_pairs":[[p[1]["host"]["id"],p[2]["host"]["id"]]for p in pairs],
                    "candidate_footprints":[{"host_ids":[p[1]["host"]["id"],p[2]["host"]["id"]],
                                              "score_mm":p[0],"footprint":footprint(_world(pair_shape(p),axis)),
                                              "categories":[p[1]["host"]["category"],p[2]["host"]["category"]]}for p in pairs]}
        lo=max(a["normal_interval_mm"][0],b["normal_interval_mm"][0])
        hi=min(a["normal_interval_mm"][1],b["normal_interval_mm"][1])
        target=_world(box(a["station_mm"],lo,b["station_mm"],hi),axis)
        existing_overlap=target.intersection(unary_union([shape_of(wall)for wall in walls])).area/target.area
        equivalent=[p for p in pairs if abs(p[0]-score)<=tolerance
                    and chosen.symmetric_difference(pair_shape(p)).area<=max(tolerance**2,chosen.area*1e-6)]
        host_ids=sorted({cap["host"]["id"]for p in equivalent for cap in(p[1],p[2])})
        host_sources=sorted({key for p in equivalent for cap in(p[1],p[2])for key in cap["host"]["source_entity_ids"]})
        return {"status":"opposite_jamb_faces_resolved","footprint":footprint(target),
                "host_ids":host_ids,
                "host_source_entity_ids":host_sources,
                "jamb_stations_mm":[a["station_mm"],b["station_mm"]],"wall_normal_interval_mm":[lo,hi],
                "wall_thickness_mm":hi-lo,"span_mm":b["station_mm"]-a["station_mm"],
                "existing_material_overlap_ratio":existing_overlap,
                "frame_to_wall_band_distance_mm":max(0,lo,-hi),
                "jamb_wall_normal_intervals_mm":[a["normal_interval_mm"],b["normal_interval_mm"]],
                "normal_band_rule":"shared_full_wall_band_including_thicker_junction_support",
                "basis":"door_frame_location_with_two_facing_wall_endcaps_and_wall_side_faces"}
    # A continuous source wall can cross a door symbol. Use its measured side
    # faces and actual frame crosspieces to propose a full-depth cut.
    frame_stations=[]
    for line in frame_lines:
        p=_local(line,axis)
        a,b=p.coords[0],p.coords[-1]
        if abs(a[0]-b[0])<=tolerance and 30<=abs(a[1]-b[1])<=search*2:
            t=(a[0]+b[0])/2
            if -recess<=t<=tolerance or length-tolerance<=t<=length+recess:
                frame_stations.append(t)
    start=min([0,*[t for t in frame_stations if t<=0]])
    end=max([length,*[t for t in frame_stations if t>=length]])
    candidates=[]
    for wall,shape in local_hosts:
        probe=LineString([(length/2,-search),(length/2,search)])
        cross=shape.intersection(probe)
        for band in parts(cross,"LineString"):
            lo,hi=band.bounds[1],band.bounds[3]
            if hi-lo>=40 and lo-tolerance<=0<=hi+tolerance:
                target=box(start,lo,end,hi)
                overlap=shape.intersection(target).area/target.area
                if overlap>=.5:
                    candidates.append((overlap,wall,target,lo,hi))
    if candidates:
        candidates.sort(key=lambda row:(-row[0],row[1]["id"]))
        _,wall,target,lo,hi=candidates[0]
        target=_world(target,axis)
        return {"status":"continuous_host_requires_full_opening_cut","footprint":footprint(target),
                "host_ids":[wall["id"]],"host_source_entity_ids":wall["source_entity_ids"],
                "jamb_stations_mm":[start,end],"wall_normal_interval_mm":[lo,hi],
                "wall_thickness_mm":hi-lo,"span_mm":end-start,
                "existing_material_overlap_ratio":candidates[0][0],
                "basis":"complete_door_axis_frame_edges_and_measured_continuous_wall_side_faces"}
    return {"status":"host_faces_unresolved","nearby_host_ids":[row[0]["id"]for row in local_hosts],
            "left_endcap_candidates":[{k:v for k,v in row.items()if k!="host"}|{"host_id":row["host"]["id"]}for row in left],
            "right_endcap_candidates":[{k:v for k,v in row.items()if k!="host"}|{"host_id":row["host"]["id"]}for row in right],
            "reason":"No consistent opposite jamb pair or measured continuous host cross-section"}


def _source_assemblies(index,active,facade_ids,settings):
    selected_paths=set()
    for path,entity in sorted(index.containers.items(),key=lambda row:(row[0].count("/"),row[0])):
        text=" ".join(str(entity.get(k,""))for k in ("block_name","dynamic_block_name","original_layer","layer"))
        if not matches(text,DOOR_TERMS+["window","win","窗"]):
            continue
        ids={key for key in active-facade_ids if (p:=str(index.by_id[key].get("instance_path")or""))==path or p.startswith(path+"/")}
        if not ids or any(path.startswith(parent+"/")for parent in selected_paths):
            continue
        lines=[line for key in ids for line in index.lines_by_id[key]]
        if not lines:
            continue
        bounds=unary_union(lines).bounds
        span=max(bounds[2]-bounds[0],bounds[3]-bounds[1])
        if not 40<=span<=12000:
            continue
        zones=parts(unary_union([line.buffer(settings.get("door_assembly_connection_mm",150)/2)for line in lines]))
        groups=[]
        for zone in sorted(zones,key=lambda p:p.bounds):
            group={key for key in ids if any(line.intersects(zone)for line in index.lines_by_id[key])}
            if group:
                groups.append((zone,group))
        for key in ids:
            if index.lines_by_id[key]:
                continue
            e=index.by_id[key]
            position=e.get("position_world")or e.get("position")
            owner=min(groups,key=lambda row:row[0].distance(Point(xy(position))))if position else(groups[0]if groups else None)
            if owner:
                owner[1].add(key)
        selected_paths.add(path)
        for _,group in groups:
            bounds=unary_union([line for key in group for line in index.lines_by_id[key]]).bounds
            if max(bounds[2]-bounds[0],bounds[3]-bounds[1])>=40:
                yield path,entity,group,bounds


def recognize_doors(index, active, walls, facade_ids, settings):
    assemblies=[]
    for path,entity,ids,bounds in _source_assemblies(index,active,facade_ids,settings):
        text=" ".join(str(entity.get(k,""))for k in ("block_name","dynamic_block_name","original_layer","layer"))
        opening_type=("sliding_door"if matches(text,["slide","sliding","滑門","推拉"])else
                      "folding_door"if matches(text,["fold","folding","折門","摺門"])else
                      "door"if matches(text,DOOR_TERMS)else"door_or_window")
        axes=swing_axes(index,ids,tolerance=max(2,index.precision*2))
        mode="all_swing_leaves_and_arcs"
        if not axes:
            axes=frame_axes(index,ids,walls,tolerance=max(.1,index.precision*2))
            mode="opposite_frame_crosspieces"
        frame_lines=[line for key in ids for line in index.lines_by_id[key]
                     if not any(key in leaf["source_entity_ids"]for axis in axes for leaf in axis.get("leaf_matches",[]))]
        evaluated=[]
        for candidate in axes:
            host=host_footprint(candidate["axis"],walls,frame_lines=frame_lines,
                                search=settings["door_host_search_mm"],recess=settings["maximum_frame_recess_mm"],
                                tolerance=max(.1,index.precision*2),allow_frame_bearing=mode=="opposite_frame_crosspieces")
            resolved="footprint"in host
            evaluated.append((resolved,candidate["hinge_count"],candidate["length_mm"],candidate,host))
        evaluated.sort(key=lambda r:(-r[1],-int(r[0]),r[4].get("existing_material_overlap_ratio",1),
                                     -int(r[4].get("status")=="opposite_jamb_faces_resolved"),-r[2]))
        row={"id":stable_id("DOOR-ASSEMBLY",ids),"instance_path":path,"source_entity_ids":sorted(ids),
             "source_block":entity.get("dynamic_block_name")or entity.get("block_name"),"opening_type":opening_type,
             "geometry_method":mode,"source_container_entity_id":entity.get("entity_id"),
             "source_bbox_mm":list(bounds),
             "source_layer":layer_of(entity),"height_status":"source_or_user_dimension_required"}
        if evaluated:
            _,_,_,candidate,host=evaluated[0]
            row.update(axis_world_mm=candidate["axis"],leaf_span_mm=candidate["length_mm"],
                       leaf_count=candidate["hinge_count"],leaf_matches=candidate["leaf_matches"],host_audit=host,
                       status="plan_geometry_resolved"if"footprint"in host else"host_review_required")
            if "footprint"in host:
                row["clearance_footprint"]=host["footprint"]
                row["lintel_footprint"]=host["footprint"]
                row["lintel_footprint_basis"]=host["basis"]
        else:
            row.update(status="door_geometry_review_required",reason="No complete swing assembly or supported opposite frame pair")
        assemblies.append(row)
    return assemblies


def canonical_door_assemblies(rows,tolerance=.1):
    """Coincident source versions of one measured wall opening share one cut."""
    output=[]
    pending=list(rows)
    while pending:
        first=pending.pop(0)
        if not first.get("clearance_footprint"):
            output.append(first);continue
        shape=shape_of(first["clearance_footprint"])
        matches_rows=[first]
        for other in list(pending):
            if not other.get("clearance_footprint"):
                continue
            second=shape_of(other["clearance_footprint"])
            if shape.hausdorff_distance(second)<=tolerance and abs(shape.area-second.area)<=tolerance*max(shape.length,second.length):
                matches_rows.append(other);pending.remove(other)
        chosen=max(matches_rows,key=lambda r:(r.get("leaf_count",0),r.get("leaf_span_mm",0),r["id"]))
        if len(matches_rows)>1:
            chosen=dict(chosen)
            source_ids=sorted({key for row in matches_rows for key in row["source_entity_ids"]})
            chosen.update(id=stable_id("DOOR-ASSEMBLY",source_ids,shape),source_entity_ids=source_ids,
                          source_instance_paths=sorted({row["instance_path"]for row in matches_rows}),
                          source_container_entity_ids=sorted({row["source_container_entity_id"]for row in matches_rows if row.get("source_container_entity_id")}),
                          coincident_source_assembly_ids=[row["id"]for row in matches_rows],
                          duplicate_source_version_count=len(matches_rows))
        output.append(chosen)
    return output


def cut_wall_plan(walls, doors):
    """Plan-only door cuts; upper wall material remains a separate Z decision."""
    output=[]
    cuts=[]
    for wall in walls:
        shape=shape_of(wall)
        modifiers=[d for d in doors if d.get("clearance_footprint")
                   and shape.intersection(Polygon(d["clearance_footprint"]["outer"])).area>.1]
        cut=unary_union([Polygon(d["clearance_footprint"]["outer"])for d in modifiers])
        remaining=shape.difference(cut)
        for number,part in enumerate(parts(remaining)):
            output.append({**wall,"id":wall["id"]+f"/door-cut/{number}","footprint":footprint(part),
                           "area_mm2":part.area,"door_opening_ids":[d["id"]for d in modifiers],
                           "uncut_wall_material_id":wall["id"]})
        if modifiers:
            cuts.append({"wall_id":wall["id"],"door_ids":[d["id"]for d in modifiers],
                         "removed_plan_area_mm2":shape.area-remaining.area,
                         "remaining_opening_overlap_mm2":remaining.intersection(cut).area})
    return output,cuts

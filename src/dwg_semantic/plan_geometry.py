"""Source-preserving planar topology shared by the semantic recognizers.

Nothing in this module approves a footprint or changes a DWG.  In particular,
Z-separated linework is never pooled into one polygonization network.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
import re

from shapely import set_precision,snap
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPoint, Point, Polygon, box
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree
from shapely.affinity import translate

from .geometry import entity_segments


def stable_id(prefix, sources, geometry=None):
    value = [sorted(set(sources)), geometry.normalize().wkb_hex if geometry is not None else None]
    return prefix + ":" + hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:20]


def xy(point):
    point = point.get("point", point) if isinstance(point, dict) else point
    return (float(point["x"]), float(point["y"])) if isinstance(point, dict) else (float(point[0]), float(point[1]))


def layer_of(entity):
    original = str(entity.get("original_layer", entity.get("layer", "0")))
    return str(entity.get("effective_layer", entity.get("layer", "0"))) if original in {"0", "Layer0"} else original


def matches(value, terms):
    value = (value or "").casefold()
    return any((term.casefold() in value if re.search(r"[^\x00-\x7f]", term)
                else re.search(r"(?<![a-z])" + re.escape(term.casefold()) + r"(?![a-z])", value))
               for term in terms)


def parts(shape, kind="Polygon"):
    if shape is None or shape.is_empty:
        return []
    if shape.geom_type == kind:
        return [shape]
    return [part for child in getattr(shape, "geoms", []) for part in parts(child, kind)]


def footprint(polygon):
    return {"outer": [list(p) for p in list(polygon.exterior.coords)[:-1]],
            "holes": [[list(p) for p in list(ring.coords)[:-1]] for ring in polygon.interiors]}


def shape_of(record):
    value = record.get("footprint", record)
    return Polygon(value["outer"], value.get("holes", []))


def dimensions(polygon):
    center=polygon.centroid
    local=translate(polygon,xoff=-center.x,yoff=-center.y)
    ring = list(local.minimum_rotated_rectangle.exterior.coords)
    lengths = [math.dist(a, b) for a, b in zip(ring, ring[1:])]
    return min(lengths), max(lengths)


def source_plane(entity):
    fields = ("curve_points_world", "vertices_world", "curve_points", "vertices")
    points = next((entity[key] for key in fields if entity.get(key)), None)
    if not points:
        points = [entity[key] for key in ("start_world", "end_world", "start", "end", "center_world", "center") if entity.get(key)]
    if not points and entity.get("boundary_loops"):
        points = [p for loop in entity["boundary_loops"] for p in loop.get("vertices_world", loop.get("vertices", []))]
        points += [p for loop in entity["boundary_loops"] for curve in loop.get("curves", [])
                   for p in curve.get("curve_points_world", curve.get("curve_points", []))]
    if not points and entity.get("segments"):
        points = [p for segment in entity["segments"] for key in ("start_world", "end_world", "start", "end")
                  if (p := segment.get(key)) is not None]
    z = []
    for point in points or []:
        point = point.get("point", point) if isinstance(point, dict) else point
        z.append(float(point.get("z", entity.get("elevation", 0))) if isinstance(point, dict)
                 else float(point[2]) if len(point) > 2 else float(entity.get("elevation", 0)))
    low, high = (min(z), max(z)) if z else (0.0, 0.0)
    normal = entity.get("normal_world") or entity.get("normal")
    tilted = bool(normal and (abs(xy(normal)[0]) > 1e-8 or abs(xy(normal)[1]) > 1e-8))
    return {"z_range_mm": [low, high], "plane_z_mm": low if high-low <= .01 and not tilted else None,
            "nonplanar": high-low > .01 or tilted, "polyline_kind": entity.get("polyline_kind")}


def entity_lines(entity, error=1.0):
    return [LineString([s.p1, s.p2]) for s in entity_segments(entity, max_curve_error_mm=error) if s.length > 1e-9]


def node_linework(lines,precision):
    """Snap the whole network to common endpoint nodes before grid rounding.

    Rounding each diagonal segment independently can move a T endpoint off the
    line it met. Shared endpoint targets are inserted into all incident lines
    first, preserving that topology while keeping the displacement measured.
    """
    if not lines:
        return GeometryCollection()
    targets=sorted({(round(p[0]/precision)*precision,round(p[1]/precision)*precision)
                    for line in lines for p in(line.coords[0],line.coords[-1])})
    network=MultiLineString([list(line.coords)for line in lines])
    common=snap(network,MultiPoint(targets),precision*1.01)
    return unary_union(set_precision(unary_union(common),precision))


def hatch_profiles(entity, precision=.01):
    """Use true native loops; External loops may be disjoint, not holes."""
    loops = []
    failures = []
    for index, loop in enumerate(entity.get("boundary_loops", [])):
        if not loop.get("supported"):
            failures.append({"loop_index": index, "reason": "unsupported_native_hatch_curve"})
            continue
        if loop.get("vertices_world") or loop.get("vertices"):
            edges = entity_lines({"type": "LWPOLYLINE", "closed": True,
                                  "vertices": loop.get("vertices_world") or loop["vertices"],
                                  "bulges": loop.get("bulges", [])})
        else:
            edges = []
            for curve in loop.get("curves", []):
                points = [xy(p) for p in curve.get("curve_points_world", curve.get("curve_points", []))]
                edges += [LineString([a, b]) for a, b in zip(points, points[1:]) if a != b]
        faces = list(polygonize(node_linework(edges,precision))) if edges else []
        if not faces:
            failures.append({"loop_index": index, "reason": "native_loop_does_not_close_at_precision"})
        for face in faces:
            loops.append({"loop_index": index, "loop_type": loop.get("loop_type"), "shape": face})
    # Native External flags are not an instruction to merge disjoint loops,
    # nor to fill an enclosed inner loop. Preserve the even/odd loop topology.
    output=[]
    for row in loops:
        shape=row["shape"]
        enclosing=[other for other in loops if other is not row and other["shape"].area>shape.area+precision**2
                   and other["shape"].covers(shape)]
        if len(enclosing)%2:
            continue
        children=[other for other in loops if other["shape"].area<shape.area-precision**2 and shape.covers(other["shape"])
                  and not any(middle is not row and middle is not other and middle["shape"].area<shape.area-precision**2
                              and middle["shape"].area>other["shape"].area+precision**2
                              and shape.covers(middle["shape"])and middle["shape"].covers(other["shape"])
                              for middle in loops)]
        material=shape.difference(unary_union([child["shape"]for child in children]))
        for profile in parts(material):
            output.append({"loop_index":row["loop_index"],"loop_type":row["loop_type"],
                           "inner_loop_indices":[child["loop_index"]for child in children],"footprint":footprint(profile)})
    return output, failures


class SourceIndex:
    def __init__(self, document, *, precision=.01, curve_error=1.0):
        from .modeling import entity_key, model_entities
        self.precision = precision
        self.entities = model_entities(document)
        self.by_id = {entity_key(e): e for e in self.entities}
        self.layers = {key: layer_of(e) for key, e in self.by_id.items()}
        self.planes = {key: source_plane(e) for key, e in self.by_id.items()}
        self.geometry_errors = []
        self.normalization_audits = []
        self.lines_by_id = {}
        for key,e in self.by_id.items():
            try:
                self.lines_by_id[key] = entity_lines(e, curve_error)
            except (KeyError,TypeError,ValueError,OverflowError) as error:
                self.lines_by_id[key] = []
                self.geometry_errors.append({"source_entity_ids":[key],"reason":str(error)})
        self.lines = [line for key in self.by_id for line in self.lines_by_id[key]]
        self.line_ids = [key for key in self.by_id for _ in self.lines_by_id[key]]
        self.tree = STRtree(self.lines)
        self.containers = {}
        for e in [*document.get("entities", []), *document.get("resolved_instances", [])]:
            if e.get("type") not in {"INSERT", "BLOCK_REFERENCE"}:
                continue
            if e.get("owner_block") != "*Model_Space" and e.get("root_space") != "*Model_Space":
                continue
            path = str(e.get("instance_path") or e.get("handle") or "")
            self.containers[path] = e

    def ancestors(self, entity):
        path = str(entity.get("instance_path") or "")
        return [(part, self.containers[part]) for count in range(1, len(path.split("/"))+1)
                if (part := "/".join(path.split("/")[:count])) in self.containers]

    def named_system(self, entity, terms):
        for path, ancestor in self.ancestors(entity):
            text = " ".join(str(ancestor.get(key, "")) for key in ("block_name", "dynamic_block_name", "original_layer", "layer"))
            if matches(text, terms):
                return path
        if matches(str(entity.get("source_block", "")), terms):
            return str(entity.get("instance_path") or entity.get("source_block"))
        return None

    def refs(self, boundary, allowed=None, tolerance=None):
        tolerance = self.precision*1.5 if tolerance is None else tolerance
        band = boundary.buffer(tolerance, cap_style=2, join_style=2)
        result = set()
        for i in self.tree.query(band):
            key = self.line_ids[int(i)]
            if allowed is not None and key not in allowed:
                continue
            if self.lines[int(i)].intersection(band).length > max(self.precision, 1e-6):
                result.add(key)
        return sorted(result)

    def polygons(self, ids, *, precision=None, repairs=()):
        precision = self.precision if precision is None else precision
        lines = [line for key in sorted(ids) for line in self.lines_by_id[key]]+list(repairs)
        if not lines:
            return []
        network=node_linework(lines,precision)
        raw=unary_union(lines)
        displacement=raw.hausdorff_distance(network)if not network.is_empty else 0.0
        self.normalization_audits.append({"source_entity_ids":sorted(ids),"line_count":len(lines),
                                          "method":"shared_endpoint_nodes_then_whole_network_precision",
                                          "precision_mm":precision,"measured_hausdorff_mm":displacement})
        return list(polygonize(network))


def active_plan_ids(index,classification):
    domains=(classification or {}).get("audits",{}).get("plan_domains",[])
    domain=unary_union([box(row["bbox"]["min"]["x"],row["bbox"]["min"]["y"],
                            row["bbox"]["max"]["x"],row["bbox"]["max"]["y"])for row in domains])
    if domain.is_empty:
        return set(index.by_id)
    active=set()
    for key,entity in index.by_id.items():
        ext=entity.get("world_extents")or entity.get("local_extents")
        if any(line.intersects(domain)for line in index.lines_by_id[key])or(ext and box(ext["min"]["x"],ext["min"]["y"],ext["max"]["x"],ext["max"]["y"]).intersects(domain)):
            active.add(key)
    return active


def topology_faces(index, ids, *, max_inradius=375, max_hydraulic_width=700, min_area=5000):
    """Preserve holes before any width-based face selection.

    Fully enclosed cells in a connected wall network are voids. A cell touching
    the outer perimeter is a material candidate only when its shape has wall
    width evidence. Closed room loops remain unresolved rather than filled.
    """
    faces = [p for p in index.polygons(ids) if p.is_valid and p.area >= min_area]
    gross = unary_union([Polygon(face.exterior) for face in faces])
    gross_parts = parts(gross)
    def inradius(face):
        lo, hi = 0.0, max(face.bounds[2]-face.bounds[0], face.bounds[3]-face.bounds[1])
        for _ in range(18):
            mid = (lo+hi)/2
            if face.buffer(-mid).is_empty:
                hi = mid
            else:
                lo = mid
        return (lo+hi)/2
    radii = {id(face): inradius(face) for face in faces}
    exposed_radii = {}
    for gross_part in gross_parts:
        exterior_cells = [f for f in faces if gross_part.covers(f.representative_point())
                          and f.exterior.intersection(gross_part.exterior.buffer(index.precision*1.5)).length > index.precision*2]
        exposed_radii[id(gross_part)] = max((radii[id(f)] for f in exterior_cells), default=0)
    material, voids, unresolved = [], [], []
    for face in faces:
        exterior_contact = face.exterior.intersection(gross.boundary.buffer(index.precision*1.5)).length
        enclosed = exterior_contact <= index.precision*2
        nesting_depth = sum(Polygon(ring).covers(face.representative_point()) for other in faces
                            if other is not face for ring in other.interiors)
        explicitly_inner = nesting_depth%2==1
        explicit_island = nesting_depth>0 and nesting_depth%2==0
        width = 2*face.area/face.length
        owner = next((g for g in gross_parts if g.covers(face.representative_point())), None)
        material_radius = exposed_radii.get(id(owner), 0)
        evidence = {"source_entity_ids": index.refs(face.boundary, set(ids)), "footprint": footprint(face),
                    "area_mm2": face.area, "hydraulic_width_mm": width,
                    "exterior_contact_mm": exterior_contact, "inradius_mm": radii[id(face)],
                    "exterior_material_inradius_mm": material_radius,"closed_loop_nesting_depth":nesting_depth}
        is_void = explicitly_inner or (not explicit_island and enclosed and radii[id(face)] > material_radius+index.precision*2)
        if is_void:
            voids.append({**evidence, "reason": "inner_cell_enclosed_by_wall_boundary", "slab_opening_status": "independent_semantic_decision_required"})
        elif explicit_island or face.interiors or (face.buffer(-max_inradius).is_empty and width <= max_hydraulic_width):
            material.append({**evidence, "reason": "outer_minus_inner_wall_topology" if face.interiors else "bounded_wall_width_evidence"})
        else:
            unresolved.append({**evidence, "reason": "closed_face_has_no_wall_width_evidence"})
    # A polygonizer returns adjacent cells separately. Subtract the enclosing
    # void cells before union, so union can never refill an internal shaft.
    void_shape = unary_union([shape_of(v) for v in voids])
    material_shape = unary_union([shape_of(m) for m in material]).difference(void_shape)
    return material_shape, voids, unresolved


def collinear_gap_repairs(lines, maximum_gap, precision, *, minimum_source_length=0):
    """Propose only forward collinear endpoint-to-source connections."""
    if maximum_gap <= 0:
        return []
    tree = STRtree(lines)
    dangling = [Point(point) for point in dangling_endpoints(lines, precision)]
    endpoint_tree = STRtree(dangling)
    output = {}
    for line in lines:
        if line.length < minimum_source_length:
            continue
        coords = list(line.coords)
        for a, b in ((coords[-2], coords[-1]), (coords[1], coords[0])):
            if not len(endpoint_tree.query(Point(b).buffer(precision*1.5))):
                continue
            length = math.dist(a, b)
            if length <= precision:
                continue
            u = ((b[0]-a[0])/length, (b[1]-a[1])/length)
            ray = LineString([b, (b[0]+u[0]*maximum_gap, b[1]+u[1]*maximum_gap)])
            targets = []
            for number in tree.query(ray.buffer(precision)):
                other = lines[int(number)]
                if other is line:
                    continue
                hit = ray.intersection(other)
                for point in parts(hit, "Point"):
                    d = Point(b).distance(point)
                    if precision*2 < d <= maximum_gap+precision:
                        targets.append((d, point))
                # Native transforms can put the target endpoint a few microns
                # off the supporting line. Keep that measured precision error
                # distinct from the authorized millimetre-scale forward gap.
                for candidate in (other.coords[0],other.coords[-1]):
                    along=(candidate[0]-b[0])*u[0]+(candidate[1]-b[1])*u[1]
                    offset=abs((candidate[0]-b[0])*(-u[1])+(candidate[1]-b[1])*u[0])
                    if precision*2<along<=maximum_gap and offset<=precision:
                        targets.append((math.dist(b,candidate),Point(candidate)))
            if targets:
                distance, point = min(targets, key=lambda row: row[0])
                repair = LineString([b, (point.x, point.y)])
                output[repair.normalize().wkb_hex] = repair
    return list(output.values())


def dangling_endpoints(lines, precision):
    graph = node_linework(lines,precision)
    degrees = defaultdict(int)
    for line in parts(graph, "LineString"):
        for point in (line.coords[0], line.coords[-1]):
            degrees[tuple(point)] += 1
    return [list(point) for point, degree in sorted(degrees.items()) if degree == 1]

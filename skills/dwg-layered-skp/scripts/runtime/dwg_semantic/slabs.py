"""Source-bound RC slab review without requiring a drawn slab outline.

This step proposes an envelope from building perimeter linework and inventories
annotations/opening or level-change regions. It does not approve elevations,
assign the nearest text to a slab, treat room faces as holes, or write a model.
"""
from __future__ import annotations

from collections import defaultdict
from html import escape
import json
import math
from pathlib import Path
import re

from .geometry import bbox_center, entity_segments
from .modeling import _json_hash, model_entities

_RELEVANT = re.compile(r'樓板|楼板|板底|板頂|板顶|板厚|降[板版]|降\s*\d|留洞|開洞|开洞|洞口|結構|结构|面[層层]|標高|标高|(?<![A-Za-z])(?:RC|SSL|FFL|TOS|BOS|TOC|SOFFIT)(?![A-Za-z])|[±+−-]\s*\d+\.\d+|\bH\s*[=±+−-]', re.I)
_LEVEL = re.compile(r'(?P<prefix>(?<![A-Za-z])(?:H|SSL|FFL|TOS|BOS|TOC)(?![A-Za-z])|板底(?:標高|标高)?|板[頂顶](?:標高|标高)?|[樓楼][層层](?:標高|标高)|(?:標高|标高))\s*[:：=]?\s*(?P<number>[±+−-]?\s*\d+(?:\.\d+)?)\s*(?P<unit>mm|cm|m)?', re.I)
_SIGNED = re.compile(r'(?<![\w.])(?P<number>[±+−-]\s*\d+\.\d+)\s*(?P<unit>mm|cm|m)?', re.I)
_EXCLUDED_LEVEL = re.compile(r'欄杆|栏杆|扶手|玻璃欄|玻璃栏|RAILING|HANDRAIL|天花|吊頂|吊顶|幕[牆墙]高度|[門门]高|窗高', re.I)
_REGION_LAYER = re.compile(r'[頂顶底]板|[樓楼]板|降[板版]|slab|slab.?opening|rc.?opening|floor.?opening', re.I)
_STRUCTURAL_MARKER = re.compile(r'^\s*[（(]?\s*[結结]\s*[）)]?\s*$')
_THICKNESS = re.compile(r'(?:板厚|\bTHK\b|\bTHICK(?:NESS)?\b)\s*[:：=]?\s*(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>mm|cm|m)?',re.I)
_OTHER_COMPONENT = re.compile(r'\$LIGUAN|立管|\bPIPE\b|DUCT|FURN|家具|設備|设备|^P_[A-Z]+_',re.I)
_AFF_BODY = r'A\.?\s*F\.?\s*F\b\.?'
_AFF_TOKEN = r'(?<![A-Za-z])'+_AFF_BODY
_AFF_HEIGHT = re.compile(r'(?P<number>[±+−-]?\s*\d+(?:\.\d+)?)\s*(?P<unit>mm|cm|m)?\s*'+_AFF_BODY,re.I)
_AFF_HEIGHT_PREFIX = re.compile(_AFF_TOKEN+r'\s*[:：=]?\s*(?P<number>[±+−-]?\s*\d+(?:\.\d+)?)\s*(?P<unit>mm|cm|m)?',re.I)
_OPENING_NOTE = re.compile(r'留[孔洞]|[開开][孔洞]|洞口|\b(?:SLAB\s+)?OPENING\b',re.I)
_NEGATED_OPENING = re.compile(r'(?:不|無|无|非|勿|取消|封[閉闭]?)\s*(?:(?:RC\s*)?(?:[頂顶底]板|[樓楼]板)\s*)?(?:留[孔洞]|[開开][孔洞]|洞口|[孔洞])|\bNO\s+OPENING\b',re.I)
_DROP_NOTE = re.compile(r'降[板版]|降\s*\d|\b(?:DROP(?:PED)?|DEPRESSED|LOWERED)\s+(?:SLAB|FLOOR)\b',re.I)
_NON_SLAB_REGION = re.compile(r'FURN|家具|EQUIP|設備|设备|\bROOM\b|房[間间]|包[廂厢]|\bCOLUMN\b|柱|\bDOOR\b|\bWINDOW\b|\bCURTAIN\b|幕[牆墙]',re.I)
_OPENING_DEFAULT_RULE = 'slab.bounded_opening_mark_default.v1'


def _owner_candidates(text, layer):
    """Names and height notation do not establish the modeled floor owner."""
    records=[]
    patterns=[('current_floor',r'本[層层]|當[層层]|当[層层]|本樓[層层]|本楼[層层]', ['floor_slab']),
              ('upper_floor_or_overhead',r'上[層层]|上方[樓楼]板|\bOVERHEAD\s+SLAB\b', ['overhead_slab']),
              ('named_top_plate',r'[頂顶]板', ['overhead_slab','roof_slab']),
              ('named_bottom_plate',r'底板', ['floor_slab','foundation_slab']),
              ('ceiling_surface',r'天花|吊[頂顶]|\bCEILING\b', ['finished_ceiling','structural_soffit'])]
    for owner,pattern,roles in patterns:
        for source,value in (('annotation_text',text),('source_layer_name',layer)):
            match=re.search(pattern,value,re.I)
            if match:
                records.append({'owner':owner,'modeled_role_candidates':roles,'evidence':{'kind':source,'matched_text':match.group()},
                                'confidence':'medium' if source=='annotation_text' else 'low','accepted':False,
                                'limitation':'A named surface or layer does not establish the modeled floor, datum, or RC construction.'})
    return records


def _cross_symbols(polygon, entity, linework, tolerance):
    """Recognize an X in a four-corner frame, never its construction meaning."""
    from shapely.geometry import LineString,Point
    corners=list(polygon.simplify(0,preserve_topology=True).exterior.coords)[:-1]
    if len(corners)!=4:
        return []
    diagonals=[]
    relevant=set()
    for x,y in corners:
        key=(math.floor(x/tolerance),math.floor(y/tolerance))
        for dx in (-1,0,1):
            for dy in (-1,0,1):
                relevant.update(linework['endpoints'].get((key[0]+dx,key[1]+dy),()))
    for index in sorted(relevant):
        identity,a,b=linework['segments'][index]
        if identity==entity['entity_id']:
            continue
        ends=[]
        for point in (a,b):
            nearest=min(range(4),key=lambda index:math.dist(point,corners[index]))
            if math.dist(point,corners[nearest])>tolerance:
                break
            ends.append(nearest)
        if len(ends)!=2 or ends[0]==ends[1] or (ends[0]-ends[1])%4!=2:
            continue
        diagonal=LineString([a,b])
        if polygon.buffer(tolerance).covers(diagonal):
            diagonals.append((identity,a,b,ends,diagonal))
    for index,first in enumerate(diagonals):
        for second in diagonals[index+1:]:
            if set(first[3]+second[3])!=set(range(4)):
                continue
            crossing=first[4].intersection(second[4])
            if crossing.geom_type=='Point' and polygon.contains(Point(crossing)):
                return [{'kind':'crossed_frame','source_entity_ids':sorted({entity['entity_id'],first[0],second[0]}),
                         'diagonal_segments_mm':[[list(first[1]),list(first[2])],[list(second[1]),list(second[2])]],
                         'endpoint_tolerance_mm':tolerance,
                         'endpoint_deviations_mm':[min(math.dist(point,corner)for corner in corners)for row in (first,second)for point in (row[1],row[2])],
                         'confidence':'high','evidence_role':'geometric_symbol_only',
                         'does_not_establish':['opening','level_change','modeled_floor_owner'],
                         'accepted_for_slab':False}]
    return []


def _bent_symbols(polygon, entity, linework, tolerance):
    """A two-leg diagonal mark inside a bounded frame, not an ordinary L edge.

    Both ends must reach opposite frame corners, with the knee strictly inside
    and a real bend. Independent LINE legs and open polyline legs are supported.
    Endpoint matching never alters the actual frame used for an opening.
    """
    from shapely.geometry import LineString, Point
    corners=list(polygon.simplify(0,preserve_topology=True).exterior.coords)[:-1]
    if len(corners)!=4:return []
    def near(point):
        key=(math.floor(point[0]/tolerance),math.floor(point[1]/tolerance))
        return sorted({i for dx in (-1,0,1)for dy in (-1,0,1)
                       for i in linework['endpoints'].get((key[0]+dx,key[1]+dy),())})
    allowed=linework['open_path_ids']
    for start,corner in enumerate(corners):
        opposite=corners[(start+2)%4]
        for first_index in near(corner):
            first_id,a,b=linework['segments'][first_index]
            if first_id==entity['entity_id']or first_id not in allowed:continue
            if math.dist(a,corner)<=tolerance:begin,knee=a,b
            elif math.dist(b,corner)<=tolerance:begin,knee=b,a
            else:continue
            if not polygon.contains(Point(knee))or polygon.boundary.distance(Point(knee))<=tolerance:continue
            for second_index in near(knee):
                if second_index==first_index:continue
                second_id,c,d=linework['segments'][second_index]
                if second_id==entity['entity_id']or second_id not in allowed:continue
                if math.dist(c,knee)<=tolerance:joint,end=c,d
                elif math.dist(d,knee)<=tolerance:joint,end=d,c
                else:continue
                if math.dist(end,opposite)>tolerance:continue
                u=(knee[0]-begin[0],knee[1]-begin[1]);v=(end[0]-joint[0],end[1]-joint[1])
                lengths=math.hypot(*u)*math.hypot(*v)
                if not lengths:continue
                sine=abs(u[0]*v[1]-u[1]*v[0])/lengths
                if sine<math.sin(math.radians(15)):continue
                legs=[LineString([begin,knee]),LineString([joint,end])]
                if not all(polygon.buffer(tolerance).covers(leg)for leg in legs):continue
                return [{'kind':'bent_diagonal','source_entity_ids':sorted({entity['entity_id'],first_id,second_id}),
                         'mark_segments_mm':[[list(begin),list(knee)],[list(joint),list(end)]],
                         'endpoint_tolerance_mm':tolerance,
                         'endpoint_deviations_mm':[math.dist(begin,corner),math.dist(end,opposite)],
                         'knee_join_deviation_mm':math.dist(knee,joint),'bend_sine':sine,
                         'confidence':'high','evidence_role':'geometric_symbol_only',
                         'does_not_establish':['modeled_floor_owner','elevation','source_thickness'],
                         'accepted_for_slab':False}]
    return []


def _bounded_region(entity, segments, precision):
    """Drop consecutive duplicate/near-duplicate curve points only.

    This is a bounded review repair, not polygon healing: no make_valid, hull,
    buffer closure, corner rounding, or symbol-tolerance coordinate snapping.
    Surviving source points remain byte-for-byte numeric values.
    """
    from shapely.geometry import LineString, Polygon
    if not segments:return None,None
    raw=[segments[0].p1]+[segment.p2 for segment in segments]
    if not entity.get('closed')and math.dist(raw[0],raw[-1])>precision:return None,None
    clean=[];indices=[];removed=[]
    for index,point in enumerate(raw):
        if clean and math.dist(point,clean[-1])<=precision:
            removed.append({'raw_index':index,'point_mm':list(point),'retained_raw_index':indices[-1],
                            'retained_point_mm':list(clean[-1]),'distance_mm':math.dist(point,clean[-1]),
                            'reason':'consecutive_duplicate_or_near_duplicate'})
        else:clean.append(point);indices.append(index)
    if len(clean)>1 and math.dist(clean[-1],clean[0])<=precision:
        point=clean.pop();index=indices.pop()
        removed.append({'raw_index':index,'point_mm':list(point),'retained_raw_index':indices[0],
                        'retained_point_mm':list(clean[0]),'distance_mm':math.dist(point,clean[0]),'reason':'closing_marker'})
    if len(clean)<3:return None,None
    polygon=Polygon(clean)
    if polygon.is_empty or not polygon.is_valid or polygon.area<=.1:return None,None
    raw_line=LineString(raw+[raw[0]]if raw[-1]!=raw[0]else raw)
    change=raw_line.hausdorff_distance(polygon.boundary)
    if change>precision+1e-8:return None,None
    audit={'method':'consecutive_near_duplicate_points_only','precision_mm':precision,
           'raw_curve_points_mm':[list(point)for point in raw],'cleaned_points_mm':[list(point)for point in clean],
           'removed_count':len(removed),'removed_points':removed,
           'max_vertex_displacement_mm':max((row['distance_mm']for row in removed),default=0),
           'boundary_hausdorff_mm':change,'surviving_points_moved':False,
           'reason':'Remove zero-length and precision-bounded consecutive repeats, including noisy duplicate closure tails; no other boundary repair.'}
    return polygon,audit


def _outer_boundary_sources(polygon, entities, precision):
    """Bind each gross component only to linework supporting its own exterior.

    A crossing or an endpoint touching the perimeter has no positive projected
    collinear support. Comparison uses the existing review grid, not symbol
    matching tolerance; source coordinates are never changed here.
    """
    from shapely import set_precision
    from shapely.geometry import LineString
    vertices=list(polygon.exterior.coords)
    edges=list(zip(vertices,vertices[1:]))
    result=[]
    for entity in entities:
        supported=False
        for segment in entity_segments(entity):
            a,b=segment.p1,segment.p2;length=math.dist(a,b)
            if length<=1e-12:continue
            line=LineString([a,b])
            if set_precision(line,precision).intersection(polygon.boundary).length>1e-8:
                supported=True;break
            direction=((b[0]-a[0])/length,(b[1]-a[1])/length)
            for c,d in edges:
                u=sum((c[i]-a[i])*direction[i]for i in (0,1))
                v=sum((d[i]-a[i])*direction[i]for i in (0,1))
                lo=max(0,min(u,v));hi=min(length,max(u,v))
                if hi-lo<=1e-8 or abs(v-u)<=1e-12:continue
                ends=[[c[i]+(d[i]-c[i])*(t-u)/(v-u)for i in (0,1)]for t in (lo,hi)]
                from shapely.geometry import Point
                if all(line.distance(Point(point))<=precision for point in ends):
                    supported=True;break
            if supported:break
        if supported:result.append(entity)
    return result


def _region_decision(frame_id, symbols, associated, overrides, *, excluded_context=False, gross_boundary=False):
    identities={frame_id}|{identity for symbol in symbols for identity in symbol['source_entity_ids']}
    supplied=[(key,overrides[key])for key in sorted(identities & overrides.keys())]
    # An explicit frame decision describes the complete boundary, including any
    # mark contained in it. Keep shadowed mark overrides in the review evidence.
    chosen=[(frame_id,overrides[frame_id])]if frame_id in overrides else supplied
    issues=[]
    for identity,value in chosen:
        if not isinstance(value,dict)or not isinstance(value.get('action'),str)or value['action']not in {'opening','level_change','retain','exclude'}or not isinstance(value.get('reason'),str)or not value['reason'].strip():
            issues.append({'code':'slab_region_override_invalid','source_entity_id':identity,
                           'detail':'Exact source-ID override requires opening/level_change/retain/exclude and a nonempty reason.'})
    actions={value['action']for _,value in chosen if isinstance(value,dict)and isinstance(value.get('action'),str)}
    if len(actions)>1:issues.append({'code':'slab_region_overrides_conflict','source_entity_ids':[key for key,_ in chosen]})
    base={'rule_id':_OPENING_DEFAULT_RULE,'source_entity_ids':sorted(identities),'accepted_for_modeling':False,
          'override_candidates':[{'source_entity_id':key,'decision':value}for key,value in supplied]}
    if issues:return {**base,'action':'unresolved','basis':'invalid_or_conflicting_explicit_override','issues':issues}
    if chosen:
        identity,value=chosen[0]
        return {**base,'action':value['action'],'basis':'explicit_source_entity_override',
                'override_source_entity_id':identity,'reason':value['reason'],'issues':[]}
    if gross_boundary:return {**base,'action':'retain','basis':'gross_slab_extent_is_not_an_opening_mark','issues':[]}
    if excluded_context:return {**base,'action':'unresolved','basis':'non_slab_component_context','issues':[]}
    roles={role for a in associated if not a['excluded_from_slab_parameters']for role in a['roles']}
    opening='opening_note'in roles;drop='level_change_note'in roles;negated='opening_note_negated'in roles
    if (opening and drop)or(opening and negated):
        return {**base,'action':'unresolved','basis':'explicit_annotation_conflict','issues':[{'code':'slab_opening_note_conflict'}]}
    if negated:return {**base,'action':'retain','basis':'explicit_no_opening_note','issues':[]}
    if drop:return {**base,'action':'level_change','basis':'explicit_lowered_slab_note','issues':[]}
    if opening:return {**base,'action':'opening','basis':'explicit_opening_note','issues':[]}
    if symbols:
        return {**base,'action':'opening','basis':'authorized_bounded_mark_default',
                'reason':'Bounded X or bent-diagonal opening mark defaults to an opening; explicit source notes and exact-ID exceptions take priority. A numeric H alone does not cancel this default.',
                'issues':[]}
    if 'level_change_candidate'in roles:
        return {**base,'action':'unresolved','basis':'numeric_H_without_opening_or_drop_note','issues':[]}
    return {**base,'action':'unresolved','basis':'bounded_outline_without_recognized_opening_mark','issues':[]}


def _thickness_proposal(associated, config):
    from .slab_modeling import DEFAULT_SLAB_THICKNESS_MM, DEFAULT_SLAB_THICKNESS_RULE
    values=[{'annotation_id':a['id'],'source_entity_id':a['entity_id'],**item}
            for a in associated if not a['excluded_from_slab_parameters']for item in a['thickness_candidates']]
    positive={v['value_mm']for v in values if v['value_mm']is not None and v['value_mm']>0}
    if values:
        status=('explicit_source_thickness_requires_unit'if any(v['value_mm']is None for v in values)else
                'explicit_source_thickness_conflict'if len(positive)!=1 or any(v['value_mm']<=0 for v in values)else'candidate_explicit_source_thickness')
        return {'value_mm':next(iter(positive))if status=='candidate_explicit_source_thickness'else None,
                'basis':'source_annotation_candidate','status':status,'source_candidates':values,
                'automatic_model_parameter_binding':False,'accepted_for_modeling':False}
    explicit=config.get('default_thickness_mm')
    if explicit is not None:
        valid=isinstance(explicit,(int,float))and not isinstance(explicit,bool)and math.isfinite(explicit)and explicit>0
        return {'value_mm':explicit if valid else None,'basis':'explicit_review_default_thickness',
                'status':'decision_value'if valid else'invalid_explicit_thickness','accepted_for_modeling':False}
    if config.get('thickness_policy')=='explicit_required':
        return {'value_mm':None,'basis':'special_case_explicit_thickness_required','status':'missing_explicit_thickness','accepted_for_modeling':False}
    return {'value_mm':DEFAULT_SLAB_THICKNESS_MM,'basis':'authorized_white_model_default',
            'rule_id':DEFAULT_SLAB_THICKNESS_RULE,'status':'available_default_not_source_measurement','accepted_for_modeling':False}


def plain_text(text):
    value = str(text).replace('\\P','\n').replace('%%p','±').replace('%%P','±').replace('−','-').replace('－','-').replace('＋','+')
    value = re.sub(r'\\[A-Za-z][^;\n]*;', '', value)
    return value.replace('{','').replace('}','').strip()


def _position(entity):
    point = entity.get('position_world') or entity.get('text_position_world') or entity.get('position')
    if point and 'x' in point and 'y' in point:
        return [float(point['x']),float(point['y'])]
    center=bbox_center(entity.get('world_extents') or entity.get('local_extents'))
    return list(center) if center else None


def _raw_text(entity):
    # Attribute definitions are templates, not values in a placed block.
    if entity.get('type')=='ATTDEF' or entity.get('definition_default'):
        return ''
    return entity.get('text') or entity.get('contents') or entity.get('dimension_text') or ''


def _text_distance(first, second):
    boxes=[entity.get('world_extents') or entity.get('local_extents') for entity in (first,second)]
    if all(boxes):
        a,b=boxes
        offsets=[max(a['min'][axis]-b['max'][axis],b['min'][axis]-a['max'][axis],0) for axis in ('x','y')]
        return math.hypot(*offsets)
    a,b=_position(first),_position(second)
    return math.dist(a,b) if a and b else math.inf


def _annotations(entities, elevation_unit, marker_distance):
    records=[]
    markers=[entity for entity in entities if _STRUCTURAL_MARKER.match(plain_text(_raw_text(entity)))]
    for entity in entities:
        raw=_raw_text(entity)
        text=plain_text(raw)
        tag=str(entity.get('tag',''))
        attribute_level=entity.get('type')=='ATTRIB' and re.search(r'ELEV|LEVEL|SSL|FFL|TOS|BOS|TOC|標高|标高',tag,re.I) and re.fullmatch(r'[±+−-]?\s*\d+(?:\.\d+)?\s*(?:mm|cm|m)?',text,re.I)
        attribute_aff=entity.get('type')=='ATTRIB' and re.fullmatch(_AFF_TOKEN,tag,re.I) and re.fullmatch(r'[±+−-]?\s*\d+(?:\.\d+)?\s*(?:mm|cm|m)?',text,re.I)
        semantic_text=(text+' '+tag) if attribute_aff else text
        thickness_text=(tag+' '+text) if entity.get('type')=='ATTRIB' and re.search(r'THK|THICK|板厚',tag,re.I) else text
        thickness_matches=list(_THICKNESS.finditer(thickness_text))
        other_height=bool(re.search(r'\d',text) and (_EXCLUDED_LEVEL.search(text) or re.search(r'ceiling|天花|吊[頂顶]',entity.get('effective_layer') or entity.get('layer',''),re.I)))
        datum_reference=bool(re.fullmatch(r'H',text,re.I))
        if not text or not (_RELEVANT.search(text) or attribute_level or attribute_aff or other_height or datum_reference or thickness_matches or _OPENING_NOTE.search(text) or _NEGATED_OPENING.search(text) or _DROP_NOTE.search(text)):
            continue
        position=_position(entity)
        layer=entity.get('effective_layer') or entity.get('layer','')
        reasons=[]
        if _EXCLUDED_LEVEL.search(text) or re.search(r'ceiling|天花|吊[頂顶]',layer,re.I):
            reasons.append('railing_curtain_door_or_finished_ceiling_annotation')
        context=[layer,entity.get('source_block',''),entity.get('block_name','')]
        slab_intent=bool(re.search(r'樓板|楼板|板底|板[頂顶]|板厚|\bSLAB\b',text,re.I))
        if any(_OTHER_COMPONENT.search(value or '') for value in context) and (attribute_level or thickness_matches or _LEVEL.search(text)) and not slab_intent:
            reasons.append('non_slab_component_context')
        thicknesses=[]
        for match in thickness_matches:
            unit=match['unit'].lower() if match['unit'] else None
            value=float(match['number'])
            thicknesses.append({'raw_token':match.group(),'numeric_value':value,'unit':unit,'value_mm':value*{'m':1000,'cm':10,'mm':1}[unit] if unit else None,'unit_basis':'explicit_text' if unit else 'unresolved_separate_from_elevation_unit','accepted_for_slab':False})
        levels=[]
        parse_text=(next((prefix for prefix in ('SSL','FFL','TOS','BOS','TOC') if prefix in tag.upper()),'標高')+' '+text) if attribute_level else text
        if attribute_aff:
            parse_text=semantic_text
        aff_matches=list(_AFF_HEIGHT.finditer(parse_text))
        aff_matches += [match for match in _AFF_HEIGHT_PREFIX.finditer(parse_text)
                        if not any(match.start()<prior.end() and prior.start()<match.end() for prior in aff_matches)]
        heights=[]
        for token_index,match in enumerate(aff_matches):
            token=re.sub(r'\s+','',match['number'])
            value=float(token.replace('±',''))
            # AFF is a height referenced to finished floor, not a structural
            # elevation. Its unstated unit does not inherit H/SSL job units.
            unit=match['unit'].lower() if match['unit'] else None
            clause=next((clause for clause in re.split(r'[;；\n]',semantic_text)if match.group()in clause),semantic_text)
            structural_bottom=bool(re.search(r'(?:(?<![A-Za-z])RC(?![A-Za-z])|結構|结构).*(?:板底|\bSOFFIT\b|\bBOS\b)|(?:板底|\bSOFFIT\b|\bBOS\b).*(?:(?<![A-Za-z])RC(?![A-Za-z])|結構|结构)',clause,re.I))
            target='structural_bottom'if structural_bottom else'finished_ceiling'if re.search(r'天花|吊[頂顶]|\bCEILING\b',clause,re.I)else'unresolved'
            heights.append({'raw_token':match.group(),'numeric_value':value,'unit':unit,
                            'token_index':token_index,'token_span':[match.start(),match.end()],
                            'unit_basis':'explicit_text' if unit else 'unresolved_separate_from_elevation_unit',
                            'value_mm':value*{'m':1000,'cm':10,'mm':1}[unit] if unit else None,
                            'height_kind':'above_finished_floor','reference_surface':'finished_floor',
                            'target_surface':target,'applies_to':'unresolved','accepted_for_slab':False,
                            'target_surface_basis':'explicit_source_RC_soffit'if structural_bottom else'literal_surface_or_unresolved',
                            'confidence':'high','evidence':'literal_AFF_reference_with_separate_surface_and_owner_semantics'})
        matches=list(_LEVEL.finditer(parse_text))
        matches+= [m for m in _SIGNED.finditer(parse_text) if not any(m.start()<p.end() and p.start()<m.end() for p in matches)]
        for match in matches:
            if any(match.start()<height.end() and height.start()<match.end() for height in aff_matches):
                continue
            token=re.sub(r'\s+','',match['number'])
            value=float(token.replace('±',''))
            unit=(match['unit'] or elevation_unit)
            unit=unit.lower() if unit else None
            prefix=match.groupdict().get('prefix','') or ''
            relative=token.startswith(('+','-','±'))
            kind='structural_bottom' if re.search(r'板底|BOS|SOFFIT',prefix,re.I) else 'structural_top' if re.search(r'板[頂顶]|SSL|TOS|TOC',prefix,re.I) else 'finished_level' if re.search(r'FFL',prefix,re.I) else 'unspecified_relative' if relative else 'unspecified_absolute'
            levels.append({'raw_token':match.group(),'numeric_value':value,'unit':unit,'unit_basis':'explicit_text' if match['unit'] else 'job_option' if unit else 'unresolved','value_mm':value*{'m':1000,'cm':10,'mm':1}[unit] if unit else None,'relative_to_datum':relative,'level_kind':kind,'applies_to':'unresolved','accepted_for_slab':False,
                           'semantic_role':'relative_datum_or_component_height' if re.fullmatch('H',prefix,re.I) else kind,
                           'datum_status':'unresolved_requires_source_or_job_confirmation','confidence':'medium',
                           'evidence':'literal_prefix_and_numeric_token_do_not_prove_modeled_floor_or_structural_surface'})
        if datum_reference:
            levels.append({'raw_token':text,'numeric_value':0,'unit':elevation_unit,'unit_basis':'job_option' if elevation_unit else 'unresolved','value_mm':0 if elevation_unit else None,'relative_to_datum':True,'level_kind':'datum_reference','applies_to':'unresolved','accepted_for_slab':False})
        if heights and not levels:
            reasons.append('aff_is_height_above_finished_floor_not_structural_elevation')
        for level in levels:
            # An independent FFL/SSL/BOS token is not invalidated merely by an
            # AFF or ceiling-height token elsewhere in the same annotation.
            explicit_level=level['level_kind']in{'finished_level','structural_top','structural_bottom'}
            level['eligible_for_slab_elevation']=explicit_level or not reasons
            level['exclusion_reasons']=[]if explicit_level else list(reasons)
        elevation_excluded=not any(level['eligible_for_slab_elevation']for level in levels)if levels else bool(reasons)
        parameter_excluded=bool(reasons)and not any(level['eligible_for_slab_elevation']for level in levels)
        related=[]
        if position:
            for marker in markers:
                other=_position(marker)
                if other:
                    distance=_text_distance(entity,marker)
                    if distance<=marker_distance:
                        related.append({'entity_id':marker['entity_id'],'text':_raw_text(marker),'distance_mm':round(distance,3),'distance_basis':'text_bounds_when_available','association':'proximity_candidate_only'})
        roles=[]
        if levels:roles.append('elevation_candidate')
        if heights:
            roles.append('above_finished_floor_height_candidate')
            if re.search(r'天花|吊[頂顶]|板底|[淨净][高空]|\b(?:CEILING|CLEAR(?:ANCE)?\s+HEIGHT)\b',semantic_text+' '+layer,re.I):
                roles.append('clearance_height_candidate')
        if thicknesses:roles.append('thickness_candidate')
        if _NEGATED_OPENING.search(text):roles.append('opening_note_negated')
        if _OPENING_NOTE.search(text):
            if _OPENING_NOTE.search(_NEGATED_OPENING.sub('',text)):roles.append('opening_note')
        if _DROP_NOTE.search(text):roles.append('level_change_note')
        if not reasons and any(level.get('semantic_role')=='relative_datum_or_component_height' and level['numeric_value']<0 for level in levels):
            roles.append('level_change_candidate')
        if re.search(r'面[層层]',text):roles.append('finish_build_up_note')
        records.append({'id':f'SA{len(records)+1:03d}','entity_id':entity['entity_id'],'source_handle':entity.get('source_handle',entity.get('handle')),'source_layer':layer,'source_block':entity.get('source_block') or entity.get('block_name'),'instance_path':entity.get('instance_path',''),'raw_text':raw,'text':text,'attribute_tag':tag or None,'position_mm':position,'roles':roles,'levels':levels,'height_candidates':heights,'thickness_candidates':thicknesses,'structural_marker_candidates':sorted(related,key=lambda item:item['distance_mm']),'owner_candidates':_owner_candidates(text,layer),'owner_status':'unresolved_requires_user_confirmation','excluded_from_slab_elevation':elevation_excluded,'excluded_from_slab_parameters':parameter_excluded,'exclusion_reasons':reasons,'leader_lines':entity.get('leader_lines',[]),'status':'candidate_height_requires_owner_surface_and_unit_review' if heights else 'reference_only' if reasons else 'candidate_requires_scope_and_datum_review'})
    return records


def prepare_slab_review(document, classification, *, elevation_unit=None, boundary_layers=(), precision_mm=.1, structural_marker_distance_mm=700, slab_decisions=None, include_automatic_recognition=True):
    from shapely import set_precision
    from shapely.geometry import LineString,Polygon
    from shapely.ops import polygonize,unary_union
    if elevation_unit not in {None,'mm','cm','m'}:
        raise ValueError('Unsupported annotation elevation unit')
    if not math.isfinite(precision_mm) or precision_mm<=0:
        raise ValueError('Boundary precision must be positive and finite')
    if not math.isfinite(structural_marker_distance_mm) or structural_marker_distance_mm<0:
        raise ValueError('Marker distance must be nonnegative and finite')
    config={}if slab_decisions is None else slab_decisions.get('slabs',slab_decisions)if isinstance(slab_decisions,dict)else None
    if not isinstance(config,dict):raise ValueError('Slab review decisions must be an object')
    if config.get('thickness_policy','default_200')not in {'default_200','explicit_required'}:
        raise ValueError('thickness_policy must be default_200 or explicit_required')
    overrides=config.get('region_overrides',{})
    if not isinstance(overrides,dict)or any(not isinstance(key,str)or not key for key in overrides):
        raise ValueError('region_overrides must map exact source entity IDs to decisions')
    symbol_tolerance=config.get('symbol_endpoint_tolerance_mm',precision_mm)
    if isinstance(symbol_tolerance,bool)or not isinstance(symbol_tolerance,(int,float))or not math.isfinite(symbol_tolerance)or symbol_tolerance<=0:
        raise ValueError('symbol_endpoint_tolerance_mm must be positive and finite')
    # Placed containers carry attached values even when geometry is materialized.
    containers=[e for e in document.get('entities',[]) if e.get('owner_block')=='*Model_Space' and e.get('attributes')]
    containers += [e for e in document.get('resolved_instances',[]) if e.get('root_space')=='*Model_Space' and e.get('attributes')]
    from .modeling import entity_key
    entities={entity_key(entity):dict(entity,entity_id=entity_key(entity)) for entity in model_entities(document)}
    for parent in containers:
        for attribute in parent.get('attributes',[]):
            entity_id=attribute.get('entity_id') or f"{entity_key(parent)}:attribute:{attribute.get('handle',attribute.get('tag',''))}"
            # The adapter also emits ATTRIB primitives; do not double-count.
            attribute_path=attribute.get('instance_path') or attribute.get('parent_instance_path') or parent.get('instance_path') or parent.get('handle')
            found=next((item for item in entities.values() if item.get('type')=='ATTRIB' and item.get('handle')==attribute.get('handle') and item.get('instance_path')==attribute_path),None)
            if not found:
                entities[entity_id]={**attribute,'entity_id':entity_id,'source_block':attribute.get('source_block') or parent.get('block_name')}
    entities=list(entities.values())
    perimeter_sources=[]
    if boundary_layers:
        perimeter_sources=[entity for entity in entities if (entity.get('effective_layer') or entity.get('layer')) in boundary_layers]
        method='explicit_boundary_layer_network'
    else:
        source_ids=set()
        source_handles=set()
        for envelope in classification.get('components',{}).get('curtain_envelopes',[]):
            source_ids.update(envelope.get('source_entity_ids',[]))
            source_handles.update(envelope.get('source_handles',[]))
        for entity in entities:
            qualified='/'.join(filter(None,[entity.get('instance_path'),str(entity.get('source_handle') or entity.get('handle'))]))
            if entity['entity_id'] in source_ids or qualified in source_handles or (not entity.get('instance_path') and str(entity.get('handle')) in source_handles):
                perimeter_sources.append(entity)
        method='classified_building_perimeter_network'
    from .plan_geometry import source_plane
    planes={entity['entity_id']:source_plane(entity)for entity in entities}
    planar_sources=defaultdict(list)
    for entity in perimeter_sources:
        plane=planes[entity['entity_id']]
        if not plane['nonplanar']:
            planar_sources[round(plane['plane_z_mm'],6)].append(entity)
    boundaries=[]
    for source_z, plane_sources in sorted(planar_sources.items()):
        lines=[LineString([segment.p1,segment.p2]) for entity in plane_sources for segment in entity_segments(entity) if segment.length>0]
        faces=list(polygonize(unary_union([set_precision(line,precision_mm) for line in lines]))) if lines else []
        if not faces:
            continue
        # Union room partitions first: a polygonizer face is not a separate
        # slab, and an interior room/service ring is not an inferred hole.
        gross=unary_union([Polygon(face.exterior)for face in faces])
        outers=[gross]if gross.geom_type=='Polygon'else list(gross.geoms)
        outers=sorted(outers,key=lambda p:(-p.area,p.bounds,p.wkb_hex))
        for number,component in enumerate(outers,1):
            outer=Polygon(component.exterior)
            sources=_outer_boundary_sources(outer,plane_sources,precision_mm)
            enclosed=[face for face in faces if outer.covers(face.representative_point())]
            rings={tuple(tuple(point)for point in ring.coords[:-1])for face in enclosed for ring in face.interiors}
            boundaries.append({'id':f'SB{len(boundaries)+1:03d}','role':'gross_slab_extent_candidate','method':method,
                'source_plane_z_mm':source_z,
                'outer':[list(point)for point in outer.exterior.coords[:-1]],'area_m2':outer.area/1e6,
                'source_entity_ids':sorted(entity['entity_id']for entity in sources),
                'source_layers':sorted({entity.get('effective_layer')or entity.get('layer')for entity in sources}),
                'precision_grid_mm':precision_mm,'source_binding':'positive collinear support on this component exterior only',
                'unclassified_interior_rings':[[list(point)for point in ring]for ring in sorted(rings)],
                'other_polygonized_faces':len(enclosed)-1,'status':'unconfirmed_building_enclosure_face',
                'boundary_face_choice':'slab edge vs facade face vs structural axis must be reviewed; disconnected exteriors preserved, interior room faces merged; not a bounding box or convex hull'})
    annotations=_annotations(entities,elevation_unit,structural_marker_distance_mm)
    from .annotation_bindings import resolve_annotation_bindings
    annotation_binding_audit,resolved_height_regions=resolve_annotation_bindings(annotations,entities,config)
    region_networks=defaultdict(lambda:{'segments':[],'endpoints':defaultdict(list),'open_path_ids':set()})
    for item in entities:
        plane=planes[item['entity_id']]
        if plane['nonplanar']:continue
        network=region_networks[round(plane['plane_z_mm'],6)]
        segments=[s for s in entity_segments(item)if s.length>0]
        network['segments'].extend((item['entity_id'],s.p1,s.p2)for s in segments)
        closed=bool(item.get('closed'))or(bool(segments)and math.dist(segments[0].p1,segments[-1].p2)<=precision_mm)
        if item.get('type')in {'LINE','LWPOLYLINE','POLYLINE'}and not closed:network['open_path_ids'].add(item['entity_id'])
    for network in region_networks.values():
        for index,(_,a,b) in enumerate(network['segments']):
            for x,y in (a,b):
                network['endpoints'][(math.floor(x/symbol_tolerance),math.floor(y/symbol_tolerance))].append(index)
    classified_components=set()
    for category,components in classification.get('components',{}).items():
        # Composite source IDs include both sides of a wall and its hole edges.
        # Boundary participation does not mean the enclosed region is occupied
        # by that material, so it cannot suppress an independent slab mark.
        if 'slab'in category.lower()or category in {'material_footprints','door_assemblies'}or not isinstance(components,list):continue
        for component in components:
            if isinstance(component,dict):classified_components.update(component.get('source_entity_ids',[]))
    regions=[]
    for entity in entities:
        layer=entity.get('effective_layer') or entity.get('layer','')
        if entity.get('type')not in {'LWPOLYLINE','POLYLINE'}:
            continue
        plane=planes[entity['entity_id']]
        if plane['nonplanar']:continue
        region_linework=region_networks[round(plane['plane_z_mm'],6)]
        segments=entity_segments(entity)
        polygon,cleanup=_bounded_region(entity,segments,precision_mm)
        if polygon is None:continue
        symbols=_cross_symbols(polygon,entity,region_linework,symbol_tolerance)
        symbols+=_bent_symbols(polygon,entity,region_linework,symbol_tolerance)
        if not _REGION_LAYER.search(layer) and not symbols:
            continue
        nearby=[]
        associated=[]
        from shapely.geometry import Point
        for annotation in annotations:
            position=annotation['position_mm']
            relevant=bool(annotation['levels'] or annotation['height_candidates'] or annotation['thickness_candidates']or set(annotation['roles']) & {'opening_note','opening_note_negated','level_change_note'})
            if not relevant:
                continue
            distance=polygon.distance(Point(position)) if position else math.inf
            starts=[line.get('start_world') for line in annotation['leader_lines']]
            leader_inside=any(isinstance(point,dict) and 'x' in point and 'y' in point and polygon.covers(Point(float(point['x']),float(point['y']))) for point in starts)
            if distance<=structural_marker_distance_mm or leader_inside:
                relation='leader_line_start_inside_candidate' if leader_inside else 'text_inside_candidate' if distance==0 else 'nearby_only'
                nearby.append({'annotation_id':annotation['id'],'source_entity_id':annotation['entity_id'],
                               'distance_mm':round(distance,3) if math.isfinite(distance) else None,'relationship':relation,
                               'accepted':False,'evidence_strength':'medium' if leader_inside or distance==0 else 'low'})
                if leader_inside or distance==0:
                    associated.append(annotation)
        semantics=[]
        for annotation in associated:
            if annotation['excluded_from_slab_parameters']:continue
            roles=annotation['roles']
            candidates=[]
            if 'opening_note' in roles:
                candidates.append(('opening','medium','literal_opening_note_with_candidate_spatial_association'))
            if 'level_change_note' in roles:
                candidates.append(('level_change','medium','literal_lowered_slab_note_with_candidate_spatial_association'))
            elif 'level_change_candidate' in roles:
                candidates.append(('level_change','low','negative_H_is_a_relative_offset_candidate_with_unresolved_owner_and_datum'))
            for role,confidence,evidence in candidates:
                semantics.append({'role':role,'confidence':confidence,'evidence':evidence,
                                  'annotation_id':annotation['id'],'source_entity_id':annotation['entity_id'],
                                  'owner_candidates':annotation['owner_candidates'],'accepted_for_slab':False})
        contexts=[layer,entity.get('source_block',''),entity.get('block_name','')]
        excluded=entity['entity_id']in classified_components or any(_OTHER_COMPONENT.search(value or '')or _NON_SLAB_REGION.search(value or '')for value in contexts)
        gross=any(polygon.symmetric_difference(Polygon(boundary['outer'])).area<=max(precision_mm**2,.000001)for boundary in boundaries)
        decision=_region_decision(entity['entity_id'],symbols,associated,overrides,excluded_context=excluded,gross_boundary=gross)
        if decision['basis']=='authorized_bounded_mark_default':
            semantics=[s for s in semantics if s['evidence']!='negative_H_is_a_relative_offset_candidate_with_unresolved_owner_and_datum']
            semantics.append({'role':'opening','confidence':'policy_default','evidence':decision['basis'],
                              'rule_id':decision['rule_id'],'source_entity_ids':decision['source_entity_ids'],'accepted_for_slab':False})
        interpretation=('opening_and_level_change_conflict'if decision['action']=='unresolved'and decision['issues']else
                        'default_opening_candidate_requires_scope_review'if decision['basis']=='authorized_bounded_mark_default'else
                        'opening_candidate_requires_scope_review'if decision['action']=='opening'else
                        'level_change_candidate_requires_datum_review'if decision['action']=='level_change'else
                        'retained_by_explicit_or_boundary_evidence'if decision['action']in {'retain','exclude'}else
                        'non_slab_component_reference'if excluded else'opening_or_level_change_unresolved')
        regions.append({'id':f'SR{len(regions)+1:03d}','source_entity_ids':decision['source_entity_ids'],'boundary_source_entity_ids':[entity['entity_id']],
                        'source_plane_z_mm':plane['plane_z_mm'],
                        'source_layer':layer,'outer':[list(p) for p in polygon.exterior.coords[:-1]],'area_m2':polygon.area/1e6,'nearby_annotation_candidates':nearby,
                        'symbols':symbols,'semantic_candidates':semantics,'owner_candidates':_owner_candidates('',layer),
                        'owner_status':'unresolved_requires_user_confirmation','interpretation':interpretation,
                        'default_decision':decision,'thickness_proposal':_thickness_proposal(associated,config),
                        'geometry_cleanup':cleanup,
                        'symbol_policy':'Recognized bounded X/bent marks default to openings under the authorized white-model rule; exact-ID exceptions and explicit no-opening/drop notes take priority. Boundary and owner remain independently traceable.',
                        'automatically_subtracted':False,'accepted_for_slab':False,'status':'default_opening_proposed'if decision['action']=='opening'else'not_subtracted_or_lowered'})
    required=['Confirm modeled slab belongs to this floor or the overhead/next-floor slab; local floor H annotations do not prove overhead elevations.',
              'Review slab extent/edge convention and owner. Bounded X/bent marks default to openings; adjust only explicit exceptions, without changing their actual boundary.',
              'Bind H offsets to their actual local datum and slab owner; do not transfer them between floors.',
              'AFF measures height above finished floor; confirm its target surface and independent unit before using it as clearance. AFF alone does not prove an RC soffit.',
              'Resolve structural top/bottom and special thickness conflicts; ordinary unspecified slab thickness uses the authorized200mm default.','Confirm derived slab tag/source attribution where no original slab perimeter entity exists.']
    if not boundaries:
        required.insert(0,'Building perimeter network has no closed face; interpret exterior wall/facade/slab-edge evidence and provide boundary layers or a measured source-bound perimeter. Do not substitute a bounding rectangle or convex hull.')
    review={'schema':'dwg-slab-review-v1','stage':'candidate_review_only','native_slab_generation':False,'source_sha256':document.get('intake',{}).get('source_sha256'),'document_digest':_json_hash(document),'classification_digest':_json_hash(classification),'annotation_extraction':document.get('annotation_extraction',{'status':'legacy_coverage_unknown','note':'Old normalized data does not establish complete ATTRIB/MLEADER coverage; re-extract when relevant.'}),'units':'mm','annotation_elevation_unit':elevation_unit,'structural_marker_distance_mm':structural_marker_distance_mm,'boundaries':boundaries,'annotations':annotations,'region_candidates':regions,'required_decisions':required,'counts':{'boundary_candidates':len(boundaries),'annotation_candidates':len(annotations),'elevation_annotations':sum(bool(a['levels']) and not a['excluded_from_slab_elevation'] for a in annotations),'aff_height_annotations':sum(bool(a['height_candidates']) for a in annotations),'crossed_frame_candidates':sum(bool(region['symbols']) for region in regions),'excluded_other_height_annotations':sum('railing_curtain_door_or_finished_ceiling_annotation' in a['exclusion_reasons'] for a in annotations),'excluded_non_slab_parameter_annotations':sum('non_slab_component_context' in a['exclusion_reasons'] for a in annotations),'region_candidates':len(regions)},'policy':'No slab solid or dimensions are authorized by this review; candidate assignments remain distinct from source facts. Confidence labels describe evidence strength, not probabilities or approvals.'}

    review['annotation_binding_audit']=annotation_binding_audit
    review['resolved_height_regions']=resolved_height_regions
    review['source_geometry_issues']=[{'code':'nonplanar_source_not_projected','source_entity_id':key,**plane}
                                      for key,plane in planes.items()if plane['nonplanar']]
    from .slab_modeling import DEFAULT_SLAB_THICKNESS_MM, DEFAULT_SLAB_THICKNESS_RULE
    review['default_policy']={'opening_rule_id':_OPENING_DEFAULT_RULE,
        'opening_marks':['bounded_crossed_frame','bounded_two_leg_bent_diagonal'],
        'ordinary_closed_polyline_is_not_an_opening_mark':True,
        'thickness_rule_id':DEFAULT_SLAB_THICKNESS_RULE,'slab_thickness_mm':DEFAULT_SLAB_THICKNESS_MM,
        'basis':'authorized_white_model_defaults_not_source_measurements',
        'precedence':['exact_source_entity_override','gross_or_non_slab_context','explicit_no_opening_or_drop_or_opening_note','bounded_opening_mark_default','unresolved_numeric_H_or_plain_outline'],
        'symbol_endpoint_tolerance_mm':symbol_tolerance,'source_geometry_precision_mm':precision_mm,
        'geometry_not_changed_by_symbol_tolerance':True}
    review['thickness_proposal']=_thickness_proposal([],config)
    review['decision_issues']=[{'region_id':r['id'],**issue}for r in regions for issue in r['default_decision']['issues']]
    review['decision_issues'].extend(annotation_binding_audit['issues'])
    used_ids={identity for r in regions for identity in r['source_entity_ids']}
    review['unmatched_region_override_ids']=sorted(set(overrides)-used_ids)
    review['opening_proposals']=[{'region_id':r['id'],'boundary_source_entity_ids':r['boundary_source_entity_ids'],
        'source_entity_ids':r['source_entity_ids'],'source_layer':r['source_layer'],
        'footprint':{'outer':r['outer'],'holes':[]},'basis':r['default_decision']['basis'],
        'rule_id':_OPENING_DEFAULT_RULE,'owner_status':r['owner_status'],
        'applies_to_system_id':None,'status':'proposed_from_rule_or_explicit_evidence_not_yet_bound_to_slab_system'}
        for r in regions if r['default_decision']['action']=='opening']
    review['counts']['default_opening_proposals']=sum(r['default_decision']['basis']=='authorized_bounded_mark_default'for r in regions)
    review['counts']['opening_proposals']=len(review['opening_proposals'])
    review['counts']['bent_mark_candidates']=sum(any(s['kind']=='bent_diagonal'for s in r['symbols'])for r in regions)
    review['policy']='Bounded recognized marks carry an authorized opening default and unspecified ordinary thickness defaults200mm. Explicit source-ID exceptions and source dimensions take priority. Owner/elevation and real region boundaries remain source-bound; this review writes no model and never authorizes a new SKP.'
    if include_automatic_recognition:
        from .plan_geometry import SourceIndex, active_plan_ids
        from .plan_recognition import DEFAULTS
        from .slab_recognition import recognize_slab_openings
        index=SourceIndex(document,precision=precision_mm)
        settings={**DEFAULTS,'symbol_boundary_search_mm':config.get('symbol_boundary_search_mm',100)}
        context=classification.get('automatic_plan',{})
        review['automatic_opening_recognition']=recognize_slab_openings(index,active_plan_ids(index,classification),settings,review,slab_decisions=config,
            wall_context=context.get('wall_materials'),door_context=context.get('door_assemblies'))
        issues=review['automatic_opening_recognition']['decision_issues']
        review['decision_issues'].extend(issues)
        conflicted={key for issue in issues for key in issue.get('source_entity_ids',[])}
        review['opening_proposals']=[row for row in review['opening_proposals']if not conflicted&set(row['source_entity_ids'])]
        review['counts']['opening_proposals']=len(review['opening_proposals'])
    return review



def write_slab_review(review, output):
    output=Path(output)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError('Slab review output already contains files; choose a new version')
    output.mkdir(parents=True,exist_ok=True)
    (output/'slab-review.json').write_text(json.dumps(review,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    shapes=review['boundaries']+review['region_candidates']
    points=[p for shape in shapes for p in shape['outer']]
    if not points:points=[a['position_mm'] for a in review['annotations'] if a['position_mm']]
    if not points:points=[[0,0],[1000,1000]]
    x0,y0=min(p[0] for p in points),min(p[1] for p in points)
    x1,y1=max(p[0] for p in points),max(p[1] for p in points)
    scale=min(1020/max(x1-x0,1),1040/max(y1-y0,1))
    def xy(p):return 70+(p[0]-x0)*scale,1140-(p[1]-y0)*scale
    svg=['<svg xmlns="http://www.w3.org/2000/svg" width="1160" height="1230" viewBox="0 0 1160 1230">','<rect width="100%" height="100%" fill="#f4f6f7"/>','<g font-family="Microsoft JhengHei,Segoe UI,sans-serif"><text x="55" y="45" font-size="23" fill="#24333c">RC 樓板｜範圍、標高與區域候選</text><text x="55" y="73" font-size="13" fill="#6f7e87">SB：平面推導範圍　SR：待判讀區域　SA：原圖註記　／ 尚未生成樓板</text>']
    for shape in shapes:
        path=' '.join(f'{x:.2f},{y:.2f}' for x,y in map(xy,shape['outer']))
        is_boundary=shape['id'].startswith('SB')
        svg.append(f'<polygon points="{path}" fill="{"#d8e8ee" if is_boundary else "#e6c39d"}" fill-opacity="{.5 if is_boundary else .28}" stroke="{"#2d7893" if is_boundary else "#af7d45"}" stroke-width="{2 if is_boundary else 1}" stroke-dasharray="{0 if is_boundary else 4}"><title>{escape(shape["id"])} {escape(shape.get("source_layer",""))}</title></polygon>')
        for symbol in shape.get('symbols',[]):
            for first,second in symbol.get('diagonal_segments_mm',symbol.get('mark_segments_mm',[])):
                a,b=xy(first),xy(second)
                svg.append(f'<line x1="{a[0]:.2f}" y1="{a[1]:.2f}" x2="{b[0]:.2f}" y2="{b[1]:.2f}" stroke="#af7d45" stroke-width="1"><title>有界原圖符號；依開口預設及明確例外判讀</title></line>')
    for annotation in review['annotations']:
        point=annotation['position_mm']
        if not point or not(x0<=point[0]<=x1 and y0<=point[1]<=y1):continue
        x,y=xy(point)
        color='#78549a' if annotation.get('height_candidates') else '#929da4' if annotation['excluded_from_slab_elevation'] else '#b45239' if 'opening_note' in annotation['roles'] else '#23745e'
        text=annotation['text'].splitlines()[0][:36]
        svg.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3.2" fill="{color}"/><text x="{x+5:.2f}" y="{y-4:.2f}" fill="{color}" font-size="10">{escape(annotation["id"])} {escape(text)}<title>{escape(annotation["text"])}</title></text>')
    svg.append('<text x="55" y="1200" font-size="12" fill="#61737d">有界 X／折線符號預設開口，明確例外優先；一般框不當開口，尚未生成或扣除實體。</text></g></svg>')
    (output/'slab-review.svg').write_text('\n'.join(svg),encoding='utf-8')
    rows=[]
    for annotation in review['annotations']:
        level='；'.join(f"{level['raw_token']} / {level['level_kind']}" for level in annotation['levels'])
        if annotation['thickness_candidates']:
            level+='；板厚候選：'+'；'.join(item['raw_token'] for item in annotation['thickness_candidates'])
        if annotation.get('height_candidates'):
            level+='；AFF 高度：'+'；'.join(item['raw_token']+' / '+('單位 '+item['unit'] if item['unit'] else '單位待定') for item in annotation['height_candidates'])
        decision='AFF 目標面、淨高用途及獨立單位待確認；不可直接當 RC 板底標高' if annotation.get('height_candidates') else '其他構件參數，不作樓板標高' if annotation['excluded_from_slab_elevation'] else '待關聯樓層、結構面與區域'
        owners=annotation.get('owner_candidates',[])
        if owners:
            decision+='；作用對象線索：'+'、'.join(sorted({item['owner'] for item in owners}))+'（未核定）'
        rows.append('<tr>'+''.join(f'<td>{escape(str(value))}</td>' for value in [annotation['id'],annotation['source_layer'],annotation['text'],level,decision])+'</tr>')
    region_rows=[]
    for region in review['region_candidates']:
        candidates='、'.join(sorted({item['role'] for item in region.get('semantic_candidates',[])})) or '用途證據不足'
        symbol='、'.join(sorted({item['kind']for item in region.get('symbols',[])}))or'無已辨識開口標示（一般框）'
        decision=region.get('default_decision',{})
        treatment=decision.get('action','unresolved')+' / '+decision.get('basis','unresolved')+'；未生成實體，作用板另依來源對應'
        region_rows.append('<tr>'+''.join(f'<td>{escape(str(value))}</td>' for value in [region['id'],region['source_layer'],symbol,candidates,treatment])+'</tr>')
    region_table='<h2>區域符號與用途證據</h2><p>已辨識有界 X／折線符號依核定規則預設開口，明確降板、無孔說明與來源 ID 覆寫優先；純 H 數值不取消開口預設。一般閉合框、房間或家具不因此當洞。板厚未明採200 mm；來源厚度、特殊狀況與標高仍分開追溯。</p><table><tr><th>ID</th><th>來源圖層</th><th>符號證據</th><th>用途候選</th><th>預設／例外處理</th></tr>'+''.join(region_rows)+'</table>'
    page='<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>RC樓板判讀候選</title><style>body{font:15px "Microsoft JhengHei",sans-serif;max-width:1250px;margin:35px auto;padding:0 20px;color:#273c45;line-height:1.8}img{max-width:100%}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid #d8e1e5;padding:10px;text-align:left;vertical-align:top}td:nth-child(3){white-space:pre-wrap}code{overflow-wrap:anywhere}.notice{background:#edf3f6;padding:20px}</style><h1>RC 樓板判讀候選</h1><p class="notice">從建築平面包絡推導樓板範圍，跨圖層讀取標高、結構面、降板及留洞資訊。有界開口符號預設開口，板厚未明採200 mm；明確來源尺寸及例外優先。本頁尚未指定作用板及標高，也沒有改動已交付 SKP。藍色範圍不能直接視為結構板邊；區域尚未進行實體扣洞或降板。</p><img src="slab-review.svg" alt="樓板範圍與原圖註記候選"><h2>原圖註記與語意</h2><p>完整原文、位置、來源 ID、單位依據及附近「結」標記見 <a href="slab-review.json">判讀資料</a>。附近文字僅是關聯候選，不代表已證實適用區域。</p><table><tr><th>ID</th><th>來源圖層</th><th>原文</th><th>標高語意</th><th>目前處理</th></tr>'+''.join(rows)+'</table><h2>建模前仍須解決</h2><ol><li>這是本層底板，還是上方頂板？標高要對應正確樓層。</li><li>樓板外緣、留洞與降板區的實際邊界及適用標高。</li><li>板頂、板底、厚度與面層的關係；降板需區分整板下移、變厚或僅面層高差。</li><li>無原始樓板外框時，新推導構件的圖層名稱及來源歸屬。</li></ol></html>'
    page=page.replace('<h2>建模前仍須解決</h2>',region_table+'<h2>建模前仍須解決</h2>')
    (output/'樓板判讀.html').write_text(page,encoding='utf-8')
    return {'status':'candidate_review_only','output':str(output.resolve()),**review['counts']}

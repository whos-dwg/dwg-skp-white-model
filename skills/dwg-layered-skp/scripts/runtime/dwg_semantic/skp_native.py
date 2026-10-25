"""Native SketchUp C API adapter (optional installed SketchUp SDK runtime).

Set SKETCHUP_API_DLL to a DLL file, or pass one to Native. Discovery deliberately
does not download executable code or assume a particular SketchUp release.
"""
from __future__ import annotations

import ctypes as C
import json
import math
import os
import threading
from pathlib import Path

class Ref(C.Structure):
    _fields_=[('ptr',C.c_void_p)]

class P3(C.Structure):
    _fields_=[('x',C.c_double),('y',C.c_double),('z',C.c_double)]

class Color(C.Structure):
    _fields_=[('r',C.c_ubyte),('g',C.c_ubyte),('b',C.c_ubyte),('a',C.c_ubyte)]

class Box(C.Structure):
    _fields_=[('min_point',P3),('max_point',P3)]

class Transform(C.Structure):
    _fields_=[('values',C.c_double*16)]

RP=C.POINTER(Ref); SZ=C.c_size_t; SP=C.POINTER(SZ); STR=C.c_char_p
def utf(s): return str(s).encode('utf-8')

def discover_library(path=None):
    configured = path or os.environ.get('SKETCHUP_API_DLL')
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if candidate.is_dir():
            candidate = candidate / 'SketchUpAPI.dll'
        if not candidate.is_file():
            raise FileNotFoundError(f'SketchUp native library not found: {candidate}')
        return candidate
    if os.name == 'nt':
        locations = [Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'SketchUp']
        candidates = [candidate for root in locations for candidate in root.glob('SketchUp */SketchUpAPI.dll')]
        if candidates:
            return sorted(candidates, reverse=True)[0]
    raise FileNotFoundError('Install the SketchUp C API runtime or set SKETCHUP_API_DLL to its library file.')


class Native:
    def __init__(self, path=None):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError('SketchUp C API must run on the main thread.')
        self.library_path=discover_library(path)
        self.dll_dir=os.add_dll_directory(str(self.library_path.parent)) if os.name=='nt' else None
        self.api=C.CDLL(str(self.library_path))
        self.bind('SUInitialize',[],None);self.bind('SUTerminate',[],None)
        for name in ['SUModelCreate','SUModelRelease','SULayerCreate','SUGroupCreate','SUGeometryInputCreate','SUGeometryInputRelease','SULoopInputCreate','SUStringCreate','SUStringRelease','SUTypedValueCreate','SUTypedValueRelease','SUMaterialCreate','SUCameraCreate','SUCameraRelease','SUSceneCreate']:
            self.bind(name,[RP])
        for name in ['SUModelGetEntities','SUGroupGetEntities','SUModelGetRenderingOptions','SUModelGetOptionsManager','SUModelGetCamera','SULayerGetMaterial','SUSceneGetRenderingOptions','SUFaceGetOuterLoop','SUDrawingElementGetMaterial','SUSceneGetCamera']:
            self.bind(name,[Ref,RP])
        for name in ['SUModelAddLayers','SUModelAddMaterials','SUModelAddScenes']:
            self.bind(name,[Ref,SZ,RP])
        for name in ['SUEntitiesAddGroup','SUDrawingElementSetLayer','SUDrawingElementSetMaterial','SUEntityAddAttributeDictionary','SUSceneSetCamera','SUModelSetActiveScene']:
            self.bind(name,[Ref,Ref])
        for name in ['SUGroupToDrawingElement','SUGroupToEntity','SUFaceToDrawingElement','SUEdgeToDrawingElement']:
            self.bind(name,[Ref],Ref)
        for name in ['SULayerSetName','SUGroupSetName','SUMaterialSetName','SUModelSetName','SUModelSetDescription','SUSceneSetName','SUTypedValueSetString']:
            self.bind(name,[Ref,STR])
        for name in ['SULayerGetName','SUGroupGetName','SUSceneGetName','SUMaterialGetName']:
            self.bind(name,[Ref,RP])
        for name in ['SULayerSetVisibility','SUMaterialSetUseOpacity','SUCameraSetPerspective','SUSceneSetUseCamera','SUSceneSetUseRenderingOptions','SUSceneSetUseHiddenLayers','SUSceneSetUseHiddenObjects','SUTypedValueSetBool']:
            self.bind(name,[Ref,C.c_bool])
        for name in ['SUMaterialSetOpacity','SUCameraSetOrthographicFrustumHeight','SUTypedValueSetDouble']:
            self.bind(name,[Ref,C.c_double])
        for name in ['SUMaterialSetColor','SUTypedValueSetColor']:
            self.bind(name,[Ref,C.POINTER(Color)])
        self.bind('SUAttributeDictionaryCreate',[RP,STR])
        self.bind('SUAttributeDictionarySetValue',[Ref,STR,Ref])
        self.bind('SUEntityGetAttributeDictionary',[Ref,STR,RP])
        self.bind('SUAttributeDictionaryGetValue',[Ref,STR,RP])
        self.bind('SUTypedValueGetString',[Ref,RP])
        self.bind('SUModelSaveToFile',[Ref,STR])
        self.bind('SUModelCreateFromFile',[RP,STR])
        self.bind('SULoopInputAddVertexIndex',[Ref,SZ])
        self.bind('SUGeometryInputSetVertices',[Ref,SZ,C.POINTER(P3)])
        self.bind('SUGeometryInputAddFace',[Ref,RP,SP])
        self.bind('SUGeometryInputFaceAddInnerLoop',[Ref,SZ,RP])
        self.bind('SUGeometryInputAddEdge',[Ref,SZ,SZ,SP])
        self.bind('SUEntitiesFill',[Ref,Ref,C.c_bool])
        self.bind('SUGroupSetTransform',[Ref,C.POINTER(Transform)])
        self.bind('SUGroupGetTransform',[Ref,C.POINTER(Transform)])
        self.bind('SUStringGetUTF8Length',[Ref,SP])
        self.bind('SUStringGetUTF8',[Ref,SZ,C.c_void_p,SP])
        self.bind('SUTypedValueSetInt32',[Ref,C.c_int32])
        self.bind('SURenderingOptionsSetValue',[Ref,STR,Ref])
        self.bind('SUOptionsManagerGetOptionsProviderByName',[Ref,STR,RP])
        self.bind('SUOptionsProviderSetValue',[Ref,STR,Ref])
        self.bind('SUCameraSetOrientation',[Ref,C.POINTER(P3),C.POINTER(P3),C.POINTER(P3)])
        self.bind('SUModelSetCamera',[Ref,RP])
        self.bind('SUSceneSetFlags',[Ref,C.c_uint])
        self.bind('SUSceneActivate',[Ref])
        self.bind('SUSceneAddLayer',[Ref,Ref])
        for name in ['SUEntitiesGetNumFaces','SUEntitiesGetNumGroups','SUModelGetNumLayers','SUModelGetNumScenes','SUEdgeGetNumFaces','SULoopGetNumVertices','SUFaceGetNumInnerLoops','SUModelGetNumMaterials','SUSceneGetNumLayers','SUSceneGetNumHiddenEntities']:
            self.bind(name,[Ref,SP])
        self.bind('SUEntitiesGetNumEdges',[Ref,C.c_bool,SP])
        for name in ['SUEntitiesGetFaces','SUEntitiesGetGroups','SUModelGetLayers','SUModelGetScenes','SULoopGetVertices','SUFaceGetInnerLoops','SUModelGetMaterials','SUSceneGetLayers','SUSceneGetHiddenEntities']:
            self.bind(name,[Ref,SZ,RP,SP])
        self.bind('SUEntitiesGetEdges',[Ref,C.c_bool,SZ,RP,SP])
        self.bind('SUEntitiesErase',[Ref,SZ,RP])
        self.bind('SUEntitiesClear',[Ref])
        self.bind('SUVertexGetPosition',[Ref,C.POINTER(P3)])
        self.bind('SUFaceGetNormal',[Ref,C.POINTER(P3)])
        self.bind('SUDrawingElementGetLayer',[Ref,RP])
        self.bind('SUMaterialGetColor',[Ref,C.POINTER(Color)])
        self.bind('SUMaterialGetOpacity',[Ref,C.POINTER(C.c_double)])
        self.bind('SUFaceGetFrontMaterial',[Ref,RP])
        self.bind('SUFaceGetBackMaterial',[Ref,RP])
        self.bind('SUSceneGetFlags',[Ref,C.POINTER(C.c_uint)])
        self.bind('SUSceneGetDrawingElementHidden',[Ref,Ref,C.POINTER(C.c_bool)])
        self.bind('SUSceneSetDrawingElementHidden',[Ref,Ref,C.c_bool])
        self.bind('SUCameraGetOrientation',[Ref,C.POINTER(P3),C.POINTER(P3),C.POINTER(P3)])
        self.bind('SUCameraGetPerspective',[Ref,C.POINTER(C.c_bool)])
        self.bind('SUCameraGetOrthographicFrustumHeight',[Ref,C.POINTER(C.c_double)])
        self.bind('SUDrawingElementGetBoundingBox',[Ref,C.POINTER(Box)])
        self.bind('SULayerGetVisibility',[Ref,C.POINTER(C.c_bool)])
        self.bind('SUDrawingElementSetHidden',[Ref,C.c_bool])
        self.bind('SUDrawingElementGetHidden',[Ref,C.POINTER(C.c_bool)])
        self.bind('SUMeshHelperCreate',[RP,Ref])
        self.bind('SUMeshHelperRelease',[RP])
        self.bind('SUMeshHelperGetNumVertices',[Ref,SP])
        self.bind('SUMeshHelperGetNumTriangles',[Ref,SP])
        self.bind('SUMeshHelperGetVertices',[Ref,SZ,C.POINTER(P3),SP])
        self.bind('SUMeshHelperGetVertexIndices',[Ref,SZ,SP,SP])
        self.api.SUInitialize()
        self._closed=False

    def close(self):
        if not self._closed:
            self.api.SUTerminate()
            if self.dll_dir:
                self.dll_dir.close()
            self._closed=True

    def __enter__(self): return self

    def __exit__(self,*args): self.close()

    def bind(self,name,args,result=C.c_int):
        f=getattr(self.api,name); f.argtypes=args;f.restype=result

    def call(self,name,*args):
        result=getattr(self.api,name)(*args)
        if result!=0:raise RuntimeError(f'{name}: SUResult={result}')

    def create(self,name,*args):
        r=Ref();self.call(name,C.byref(r),*args);return r

    def get(self,name,obj):
        r=Ref();self.call(name,obj,C.byref(r));return r

    def count(self,name,obj):
        n=SZ();self.call(name,obj,C.byref(n));return n.value

    def refs(self,count_name,get_name,obj):
        n=self.count(count_name,obj)
        if not n:return []
        array=(Ref*n)();actual=SZ();self.call(get_name,obj,n,array,C.byref(actual));return list(array)[:actual.value]

    def edges(self,entities):
        n=SZ();self.call('SUEntitiesGetNumEdges',entities,False,C.byref(n))
        if not n.value:return []
        array=(Ref*n.value)();actual=SZ();self.call('SUEntitiesGetEdges',entities,False,n.value,array,C.byref(actual));return list(array)[:actual.value]

    def string(self,name,obj):
        r=self.create('SUStringCreate');self.call(name,obj,C.byref(r))
        n=SZ();self.call('SUStringGetUTF8Length',r,C.byref(n));buf=C.create_string_buffer(n.value+1);actual=SZ()
        self.call('SUStringGetUTF8',r,n.value+1,buf,C.byref(actual));self.call('SUStringRelease',C.byref(r))
        return buf.value.decode('utf8')

    def value(self,value):
        r=self.create('SUTypedValueCreate')
        if isinstance(value,bool):self.call('SUTypedValueSetBool',r,value)
        elif isinstance(value,int):self.call('SUTypedValueSetInt32',r,value)
        elif isinstance(value,float):self.call('SUTypedValueSetDouble',r,value)
        elif isinstance(value,tuple) and len(value) in (3,4):
            color=Color(*(value if len(value)==4 else value+(255,)));self.call('SUTypedValueSetColor',r,C.byref(color))
        else:self.call('SUTypedValueSetString',r,utf(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)))
        return r

    def set_value(self,name,obj,key,value):
        v=self.value(value)
        try:self.call(name,obj,utf(key),v)
        except RuntimeError as e:raise RuntimeError(f'{e} key={key}') from e
        finally:self.call('SUTypedValueRelease',C.byref(v))

    def metadata(self,entity,data):
        dictionary=Ref()
        exists=self.api.SUEntityGetAttributeDictionary(entity,b'DWG_LAYERED_SKP',C.byref(dictionary))==0
        if not exists:
            dictionary=self.create('SUAttributeDictionaryCreate',b'DWG_LAYERED_SKP')
        for key,value in data.items():self.set_value('SUAttributeDictionarySetValue',dictionary,key,value)
        if not exists:
            self.call('SUEntityAddAttributeDictionary',entity,dictionary)

    def read_metadata(self,entity,key):
        dictionary=self.get_dictionary(entity)
        value=self.create('SUTypedValueCreate')
        try:
            self.call('SUAttributeDictionaryGetValue',dictionary,utf(key),C.byref(value))
            return self.string('SUTypedValueGetString',value)
        finally:
            self.call('SUTypedValueRelease',C.byref(value))

    def get_dictionary(self,entity):
        dictionary=Ref()
        self.call('SUEntityGetAttributeDictionary',entity,b'DWG_LAYERED_SKP',C.byref(dictionary))
        return dictionary

    def layer(self,model,name,color=(200,200,200),visible=True):
        for layer in self.refs('SUModelGetNumLayers','SUModelGetLayers',model):
            if self.string('SULayerGetName',layer)==name:
                return layer
        r=self.create('SULayerCreate');self.call('SULayerSetName',r,utf(name))
        self.call('SUModelAddLayers',model,1,C.byref(r));self.call('SULayerSetVisibility',r,visible)
        material=self.get('SULayerGetMaterial',r);col=Color(*color,255);self.call('SUMaterialSetColor',material,C.byref(col))
        return r

    def material(self,model,name,color,opacity=1.0):
        for existing in self.refs('SUModelGetNumMaterials','SUModelGetMaterials',model):
            if self.string('SUMaterialGetName',existing)==name:
                col=Color(*color,255)
                self.call('SUMaterialSetColor',existing,C.byref(col));self.call('SUMaterialSetOpacity',existing,opacity)
                self.call('SUMaterialSetUseOpacity',existing,opacity<1)
                return existing
        r=self.create('SUMaterialCreate');self.call('SUMaterialSetName',r,utf(name));col=Color(*color,255)
        self.call('SUMaterialSetColor',r,C.byref(col));self.call('SUMaterialSetOpacity',r,opacity)
        self.call('SUMaterialSetUseOpacity',r,opacity<1);self.call('SUModelAddMaterials',model,1,C.byref(r));return r

    def scene_snapshot(self,model):
        result=[]
        for scene in self.refs('SUModelGetNumScenes','SUModelGetScenes',model):
            flags=C.c_uint();self.call('SUSceneGetFlags',scene,C.byref(flags))
            camera=Ref();status=self.api.SUSceneGetCamera(scene,C.byref(camera))
            orientation=None
            if status==0 and camera.ptr:
                eye,target,up=P3(),P3(),P3()
                self.call('SUCameraGetOrientation',camera,C.byref(eye),C.byref(target),C.byref(up))
                perspective=C.c_bool();self.call('SUCameraGetPerspective',camera,C.byref(perspective))
                height=C.c_double()
                height_status=self.api.SUCameraGetOrthographicFrustumHeight(camera,C.byref(height))
                orientation={'eye':[round(eye.x,8),round(eye.y,8),round(eye.z,8)],
                             'target':[round(target.x,8),round(target.y,8),round(target.z,8)],
                             'up':[round(up.x,8),round(up.y,8),round(up.z,8)],
                             'perspective':perspective.value,'orthographic_height':round(height.value,8)if height_status==0 else None}
            result.append({'name':self.string('SUSceneGetName',scene),'flags':flags.value,'camera':orientation,
                           'hidden_layers':sorted(self.string('SULayerGetName',layer)for layer in self.refs('SUSceneGetNumLayers','SUSceneGetLayers',scene)),
                           'hidden_entity_count':self.count('SUSceneGetNumHiddenEntities',scene)})
        return result

    def group(self,parent,name,layer=None,material=None,data=None):
        r=self.create('SUGroupCreate');self.call('SUGroupSetName',r,utf(name));self.call('SUEntitiesAddGroup',parent,r)
        draw=self.api.SUGroupToDrawingElement(r)
        if layer:self.call('SUDrawingElementSetLayer',draw,layer)
        if material:self.call('SUDrawingElementSetMaterial',draw,material)
        if data:self.metadata(self.api.SUGroupToEntity(r),data)
        return r,self.get('SUGroupGetEntities',r)

    def geometry(self,entities,faces=None,edges=None,*,construction_scale=1.,origin_mm=(0.,0.,0.)):
        """Faces are (outer3d, inner3dloops) in millimeters; welded per group."""
        faces=faces or [];edges=edges or [];vertices=[];lookup={}
        def index(p):
            key=tuple(round(v,6) for v in p)
            if key not in lookup:lookup[key]=len(vertices);vertices.append(key)
            return lookup[key]
        fs=[([index(p) for p in outer],[[index(p) for p in hole] for hole in holes]) for outer,holes in faces]
        es=[(index(a),index(b)) for a,b in edges]
        es=[(a,b) for a,b in es if a!=b]
        # A reference shorter than the existing serialization grid may have
        # vertices but no edge. Do not submit an empty geometry input to the
        # native engine; its source/provenance group is retained by the caller.
        # Faces are not filtered here: invalid solid loops must still fail.
        if not fs and not es:return
        geom=self.create('SUGeometryInputCreate')
        points=(P3*len(vertices))(*(P3(*((v-origin_mm[i])*construction_scale/25.4 for i,v in enumerate(p))) for p in vertices))
        self.call('SUGeometryInputSetVertices',geom,len(vertices),points)
        def loop(indices):
            ref=self.create('SULoopInputCreate')
            for i in indices:self.call('SULoopInputAddVertexIndex',ref,i)
            return ref
        for outer,holes in fs:
            r=loop(outer);added=SZ();self.call('SUGeometryInputAddFace',geom,C.byref(r),C.byref(added))
            for h in holes:
                r=loop(h);self.call('SUGeometryInputFaceAddInnerLoop',geom,added.value,C.byref(r))
        for a,b in es:
            if a!=b:self.call('SUGeometryInputAddEdge',geom,a,b,None)
        self.call('SUEntitiesFill',entities,geom,True);self.call('SUGeometryInputRelease',C.byref(geom))

    def solid_geometry(self,group,entities,faces,*,minimum_clearance_mm=None):
        """Condition sub-native-scale edges without moving a world-space point.

        Scaling entities back down would make the kernel merge short edges
        again. Retain the inverse *instance* transform instead. This changes
        representation only; the approved six-decimal world lattice is shared
        with geometry(), and reopened QA must apply the saved transform.
        """
        faces=list(faces)
        rings=[ring for outer,holes in faces for ring in [outer,*holes]]
        lengths=[math.dist(tuple(round(v,6)for v in a),tuple(round(v,6)for v in b))
                 for ring in rings for a,b in zip(ring,ring[1:]+ring[:1])]
        minimum=min((v for v in lengths if v>0),default=1.)
        if minimum_clearance_mm is not None and math.isfinite(minimum_clearance_mm) and minimum_clearance_mm>0:
            minimum=min(minimum,minimum_clearance_mm)
        scale=10.**min(6,max(0,math.ceil(math.log10(1./minimum)))) if minimum<.0254 else 1.
        origin=tuple(round(v,6)for v in rings[0][0]) if scale!=1 else (0.,0.,0.)
        self.geometry(entities,faces,construction_scale=scale,origin_mm=origin)
        matrix=Transform((C.c_double*16)(1/scale,0,0,0,0,1/scale,0,0,0,0,1/scale,0,
                                       origin[0]/25.4,origin[1]/25.4,origin[2]/25.4,1))
        self.call('SUGroupSetTransform',group,C.byref(matrix))
        return {'scale':scale,'origin_mm':list(origin),'minimum_feature_mm':minimum,
                'transform':list(matrix.values)}

    def group_transform(self,group):
        result=Transform();self.call('SUGroupGetTransform',group,C.byref(result))
        return result

    @staticmethod
    def transformed_point(point_mm,transform):
        if transform is None:return list(point_mm)
        m=transform.values;x,y,z=point_mm
        return [m[i]*x+m[4+i]*y+m[8+i]*z+25.4*m[12+i]for i in range(3)]

    def camera(self,eye,target,up=(0,0,1),height=65000):
        r=self.create('SUCameraCreate');p=P3(*(v/25.4 for v in eye));t=P3(*(v/25.4 for v in target));u=P3(*up)
        self.call('SUCameraSetOrientation',r,C.byref(p),C.byref(t),C.byref(u));self.call('SUCameraSetPerspective',r,False)
        self.call('SUCameraSetOrthographicFrustumHeight',r,height/25.4);return r

    def triangle_mesh(self,face,transform=None):
        mesh=self.create('SUMeshHelperCreate',face)
        nv=self.count('SUMeshHelperGetNumVertices',mesh);nt=self.count('SUMeshHelperGetNumTriangles',mesh)
        vertices=(P3*nv)();actual=SZ();self.call('SUMeshHelperGetVertices',mesh,nv,vertices,C.byref(actual))
        idx=(SZ*(nt*3))();self.call('SUMeshHelperGetVertexIndices',mesh,nt*3,idx,C.byref(actual))
        points=[[v.x*25.4,v.y*25.4,v.z*25.4] for v in vertices]
        tris=[[points[idx[k+j]] for j in range(3)] for k in range(0,nt*3,3)]
        # SketchUp's tessellation may mix triangle winding within one face.
        # Match the authoritative face normal for downstream rendering/volume QA.
        normal=P3();self.call('SUFaceGetNormal',face,C.byref(normal))
        n=[normal.x,normal.y,normal.z]
        for tri in tris:
            a=[tri[1][i]-tri[0][i] for i in range(3)]
            b=[tri[2][i]-tri[0][i] for i in range(3)]
            cross=[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]
            if sum(cross[i]*n[i] for i in range(3))<0:tri[1],tri[2]=tri[2],tri[1]
        self.call('SUMeshHelperRelease',C.byref(mesh))
        return [[self.transformed_point(point,transform)for point in tri]for tri in tris]if transform is not None else tris

    def preview_polygons(self,face):
        normal=P3();self.call('SUFaceGetNormal',face,C.byref(normal))
        n=[normal.x,normal.y,normal.z]
        if self.count('SUFaceGetNumInnerLoops',face):
            return [{'points':tri,'normal':n} for tri in self.triangle_mesh(face)]
        loop=self.get('SUFaceGetOuterLoop',face);points=[]
        for vertex in self.refs('SULoopGetNumVertices','SULoopGetVertices',loop):
            p=P3();self.call('SUVertexGetPosition',vertex,C.byref(p))
            points.append([p.x*25.4,p.y*25.4,p.z*25.4])
        return [{'points':points,'normal':n}]

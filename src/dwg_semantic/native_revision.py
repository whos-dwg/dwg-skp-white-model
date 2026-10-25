"""Preserve native scenes and original Tags while revising a bound model copy.

The base file is only read. Its root/container/unchanged leaf identities survive
in memory; removed leaf scene visibility follows explicit modifier lineage.
"""
from __future__ import annotations

import ctypes as C
import hashlib
import json
from pathlib import Path

from .skp_native import Ref, utf


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def group_index(api, model):
    entities=api.get('SUModelGetEntities',model)
    roots=api.refs('SUEntitiesGetNumGroups','SUEntitiesGetGroups',entities)
    if len(roots)!=1:
        raise ValueError('Native revision requires exactly one source-bound model root')
    root=roots[0]
    categories={};leaves={};parents={}
    for category in api.refs('SUEntitiesGetNumGroups','SUEntitiesGetGroups',api.get('SUGroupGetEntities',root)):
        name=api.string('SUGroupGetName',category)
        if name in categories:
            raise ValueError('Duplicate category containers in native base')
        categories[name]=category
        children=api.get('SUGroupGetEntities',category)
        for group in api.refs('SUEntitiesGetNumGroups','SUEntitiesGetGroups',children):
            identity=api.read_metadata(api.api.SUGroupToEntity(group),'id')
            if not identity or identity in leaves:
                raise ValueError('Duplicate or missing source leaf ID in native base')
            leaves[identity]=group;parents[identity]=name
    return root,categories,leaves,parents


def visible_state(api, model):
    root,categories,leaves,_=group_index(api,model)
    objects={'root':root,**{'category:'+key:value for key,value in categories.items()},
             **{'leaf:'+key:value for key,value in leaves.items()}}
    scenes=api.refs('SUModelGetNumScenes','SUModelGetScenes',model)
    result=[]
    for scene in scenes:
        state={}
        for key,group in objects.items():
            hidden=C.c_bool()
            api.call('SUSceneGetDrawingElementHidden',scene,api.api.SUGroupToDrawingElement(group),C.byref(hidden))
            state[key]=hidden.value
        result.append(state)
    return result


class NativeRevision:
    def __init__(self,api,payload):
        self.api,self.payload=api,payload
        self.binding=payload.get('native_base')
        self.used=set();self.new_groups={};self.original_scenes=[];self.old_visibility=[]
        self.old_tags=[];self.old_leaves={};self.categories={};self.old_parents={}

    def create_model(self):
        api=self.api
        if not self.binding:
            return api.create('SUModelCreate')
        path=Path(self.binding['path']).resolve()
        if not path.is_file() or file_sha(path)!=self.binding.get('sha256'):
            raise ValueError('Native base SKP changed after review')
        model=api.create('SUModelCreateFromFile',utf(path))
        try:
            self.root,self.categories,self.old_leaves,self.old_parents=group_index(api,model)
            entity=api.api.SUGroupToEntity(self.root)
            if api.read_metadata(entity,'payload_sha256')!=self.binding.get('payload_sha256'):
                raise ValueError('Native base payload identity differs from the immutable reviewed base')
            source=json.loads(api.read_metadata(entity,'source_manifest'))
            if source.get('source_sha256')!=self.payload.get('source_manifest',{}).get('source_sha256'):
                raise ValueError('Native base and revised plan are from different source files')
            self.original_scenes=api.scene_snapshot(model)
            self.old_visibility=visible_state(api,model)
            self.old_tags=[api.string('SULayerGetName',layer)for layer in api.refs('SUModelGetNumLayers','SUModelGetLayers',model)]
            return model
        except Exception:
            api.call('SUModelRelease',C.byref(model))
            raise

    def root_entities(self, model, metadata):
        api=self.api
        if self.binding:
            api.metadata(api.api.SUGroupToEntity(self.root),metadata)
            return self.root,api.get('SUGroupGetEntities',self.root)
        self.root,entities=api.group(api.get('SUModelGetEntities',model),'DWG model',data=metadata)
        return self.root,entities

    def category_entities(self, root_entities, name):
        if name not in self.categories:
            group,entities=self.api.group(root_entities,name)
            self.categories[name]=group
            return group,entities
        group=self.categories[name]
        return group,self.api.get('SUGroupGetEntities',group)

    def leaf(self,parent,name,category,layer=None,material=None,data=None):
        api=self.api;identity=data['id']
        old=self.old_leaves.get(identity)
        if old is not None and self.old_parents[identity]==category:
            group=old;entities=api.get('SUGroupGetEntities',group)
            api.call('SUEntitiesClear',entities)
            api.call('SUGroupSetName',group,utf(name))
            drawing=api.api.SUGroupToDrawingElement(group)
            if layer:api.call('SUDrawingElementSetLayer',drawing,layer)
            if material:api.call('SUDrawingElementSetMaterial',drawing,material)
            api.metadata(api.api.SUGroupToEntity(group),data)
            self.used.add(identity)
        else:
            group,entities=api.group(parent,name,layer,material,data)
        self.new_groups[identity]=(group,data['provenance'])
        return group,entities

    def finish(self,model):
        api=self.api
        if not self.binding:
            return
        scenes=api.refs('SUModelGetNumScenes','SUModelGetScenes',model)
        for identity,group in self.old_leaves.items():
            if identity not in self.used:
                for scene in scenes:
                    api.call('SUSceneSetDrawingElementHidden',scene,api.api.SUGroupToDrawingElement(group),False)
                parent=api.get('SUGroupGetEntities',self.categories[self.old_parents[identity]])
                values=(Ref*1)(api.api.SUGroupToEntity(group))
                api.call('SUEntitiesErase',parent,1,values)
        for scene,state in zip(scenes,self.old_visibility):
            for identity,(group,provenance)in self.new_groups.items():
                ancestors={identity,*provenance.get('host_lineage_ids',[])}
                for key in ('original_component_id','wall_under_roof_original_component_id'):
                    if provenance.get(key):ancestors.add(provenance[key])
                inherited=[state['leaf:'+key]for key in ancestors if 'leaf:'+key in state]
                if inherited and len(set(inherited))>1:
                    raise ValueError('Revised component combines conflicting inherited scene visibility; explicitly split it')
                api.call('SUSceneSetDrawingElementHidden',scene,api.api.SUGroupToDrawingElement(group),
                         inherited[0]if inherited else False)
        self.expected_visibility=visible_state(api,model)

    def verify(self,model):
        if not self.binding:
            return None
        actual=self.api.scene_snapshot(model)[:len(self.original_scenes)]
        trim=lambda rows:[{key:value for key,value in row.items()if key!='hidden_entity_count'}for row in rows]
        actual_visibility=visible_state(self.api,model)[:len(self.old_visibility)]
        tags={self.api.string('SULayerGetName',layer)for layer in self.api.refs('SUModelGetNumLayers','SUModelGetLayers',model)}
        return {'base_path':self.binding['path'],'base_sha256':self.binding['sha256'],
                'source_file_unchanged':file_sha(self.binding['path'])==self.binding['sha256'],
                'scene_camera_flags_and_layers_preserved':trim(actual)==trim(self.original_scenes),
                'scene_component_visibility_preserved_by_lineage':actual_visibility==self.expected_visibility,
                'all_original_tags_preserved':set(self.old_tags)<=tags,
                'original_scenes':self.original_scenes,'reopened_scenes':actual,
                'reopened_scene_visibility':actual_visibility}

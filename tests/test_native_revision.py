import copy
import ctypes as C
import os
from pathlib import Path
import tempfile
import unittest

from dwg_semantic.modeling import _json_hash, prepare_model, write_model
from dwg_semantic.native_revision import file_sha
from dwg_semantic.skp_native import Native, utf
from test_modeling import rectangle, decisions


@unittest.skipUnless(os.environ.get('DWG_RUN_NATIVE_TESTS')=='1','Installed SketchUp native integration')
class NativeRevisionTests(unittest.TestCase):
    def test_existing_scene_camera_hidden_group_and_extra_original_tag_survive_real_reopen(self):
        document={'intake':{'source_sha256':'synthetic-native-revision'},'entities':[rectangle('W1')]}
        payload=prepare_model(document,{},decisions())
        self.assertEqual([],payload['blockers'])
        with tempfile.TemporaryDirectory()as directory:
            root=Path(directory);base=root/'base.skp'
            write_model(payload,base)
            with Native()as api:
                model=api.create('SUModelCreateFromFile',utf(base))
                try:
                    scene=api.create('SUSceneCreate');api.call('SUSceneSetName',scene,b'Original custom scene')
                    api.call('SUModelAddScenes',model,1,C.byref(scene))
                    camera=api.camera([10000,23000,5100],[300,200,1200],height=17000)
                    api.call('SUSceneSetCamera',scene,camera)
                    api.call('SUCameraRelease',C.byref(camera));api.call('SUSceneSetUseCamera',scene,True)
                    groups=api.refs('SUEntitiesGetNumGroups','SUEntitiesGetGroups',api.get('SUModelGetEntities',model))
                    category=api.refs('SUEntitiesGetNumGroups','SUEntitiesGetGroups',api.get('SUGroupGetEntities',groups[0]))[0]
                    leaf=api.refs('SUEntitiesGetNumGroups','SUEntitiesGetGroups',api.get('SUGroupGetEntities',category))[0]
                    api.call('SUSceneSetDrawingElementHidden',scene,api.api.SUGroupToDrawingElement(leaf),True)
                    api.call('SUSceneSetUseHiddenObjects',scene,True)
                    layer=api.layer(model,'Original extra source tag',visible=False)
                    api.call('SUSceneAddLayer',scene,layer);api.call('SUSceneSetUseHiddenLayers',scene,True)
                    api.call('SUModelSaveToFile',model,utf(base))
                finally:api.call('SUModelRelease',C.byref(model))
            before=file_sha(base)
            revised=copy.deepcopy(payload)
            old=revised['groups'][0]['id']
            revised['groups'][0].update(id='revised-W1',name='revised-W1',host_lineage_ids=[old],height_mm=3500)
            revised['native_base']={'path':str(base),'sha256':before,'payload_sha256':_json_hash(payload)}
            qa=write_model(revised,root/'revised.skp')
            self.assertTrue(qa['all_pass'])
            state=qa['native_base_preservation']
            self.assertTrue(state['scene_camera_flags_and_layers_preserved'],state)
            self.assertTrue(state['scene_component_visibility_preserved_by_lineage'],state)
            self.assertTrue(state['all_original_tags_preserved'],state)
            self.assertTrue(state['reopened_scene_visibility'][-1]['leaf:revised-W1'])
            self.assertEqual(before,file_sha(base))

    def test_changed_native_base_is_rejected_without_overwriting_either_file(self):
        payload=prepare_model({'intake':{'source_sha256':'fixture'},'entities':[rectangle('W1')]},{},decisions())
        with tempfile.TemporaryDirectory()as directory:
            root=Path(directory);base=root/'base.skp'
            write_model(payload,base)
            modified=copy.deepcopy(payload)
            modified['native_base']={'path':str(base),'sha256':'stale','payload_sha256':_json_hash(payload)}
            before=file_sha(base)
            with self.assertRaisesRegex(ValueError,'changed'):
                write_model(modified,root/'must-not-exist.skp')
            self.assertEqual(before,file_sha(base))
            self.assertFalse((root/'must-not-exist.skp').exists())

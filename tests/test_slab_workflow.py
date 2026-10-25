import copy
from pathlib import Path
import tempfile
import unittest

from dwg_semantic.modeling import _json_hash
from dwg_semantic.pipeline import WorkflowError,_read,_write,review_job,approve_job,digest_file,build_job
from dwg_semantic.slab_workflow import attach_slab_job


class SlabRevisionWorkflowTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[1]/'work/slab-workflow-tests'
        root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root)
        self.root=Path(self.temp.name)
        self.assertEqual(self.root.resolve().parent,root.resolve())
        self.addCleanup(self.temp.cleanup)
        source=self.root/'fixture.dwg'
        source.write_bytes(b'slab workflow source identity fixture')
        self.parent=self.root/'approved-base';self.parent.mkdir()
        document={'intake':{'source_path':str(source),'source_sha256':digest_file(source),'units':'mm','blockers':[]},'entities':[{'entity_id':'wall','handle':'wall','type':'LINE','owner_block':'*Model_Space','layer':'Walls','original_layer':'Walls','start':{'x':0,'y':0},'end':{'x':4000,'y':0}},{'entity_id':'note','handle':'note','type':'TEXT','owner_block':'*Model_Space','layer':'Notes','text':'SSL -0.100 m'}]}
        decisions={'source_sha256':digest_file(source),'default_height_mm':3000,'opening_height_mm':2100,'prepared_categories_by_primary_layer':{'Walls':['architectural_wall']},'layer_mapping':{'Walls':{'category':'architectural_wall'}}}
        payload={'schema':'dwg-skp-model-v2','units':'mm','groups':[{'id':'wall','name':'wall','category':'architectural_wall','source_layer':'Walls','source_layers':['Walls'],'tag':'Walls','source_entity_ids':['wall'],'mode':'solid_extrusion','outer':[[0,0],[4000,0],[4000,200],[0,200]],'holes':[],'base_z_mm':0,'height_mm':3000,'evidence':['synthetic measured wall']}],'references':[],'blockers':[],'warnings':[],'excluded':[],'tag_mapping':[{'source_id':str(source),'source_layer':'Walls','tag':'Walls','reason':'original_name'}],'source_manifest':document['intake'],'coordinate_policy':'source mm','document_digest':_json_hash(document),'classification_digest':_json_hash({}),'decisions_digest':_json_hash(decisions)}
        for name,value in {'document.json':document,'classification.json':{},'rules.json':{},'layers.json':[],'job.json':{},'decisions.json':decisions,'candidate.json':payload}.items():_write(self.parent/name,value)
        review=review_job(self.parent,self.parent/'candidate.json')
        self.assertFalse(review['blockers'])
        approve_job(self.parent,confirmation='Synthetic test base only',expected_review=review['review_sha256'])
        self.config={'enabled':True,'confirmed_scope':True,'derived_tag':{'tag':'RC slab derived','source_layer':'RC slab derived','kind':'derived'},'floor_datum_m':0,'systems':[{'id':'FLOOR','role':'floor','extent':{'outer':[[0,0],[4000,0],[4000,4000],[0,4000]],'holes':[]},'source_entity_ids':['wall'],'boundary_source_layers':['Walls'],'annotation_source_ids':['note'],'default_top_z_mm':-100,'default_thickness_mm':180,'zones':[],'openings':[],'evidence':['source-bound test slab']}]}
        self.configpath=self.root/'slabs.json';_write(self.configpath,self.config)
        # Fixed-standard revisions require an actual overhead datum. This
        # source-bound synthetic roof is exactly at the original wall top.
        roof=copy.deepcopy(self.config['systems'][0])
        roof.update(id='ROOF',role='overhead',default_top_z_mm=3180)
        self.config['systems'].append(roof)
        _write(self.configpath,self.config)

    def attach(self):
        self.destination=self.root/'rc-revision'
        return attach_slab_job(self.parent,self.destination,self.configpath)

    def test_complete_slab_revision_preserves_base_and_requires_new_approval(self):
        before={p.name:digest_file(p)for p in self.parent.iterdir()if p.is_file()}
        review=self.attach()
        self.assertFalse(review['blockers'],review['blockers'])
        payload=_read(self.destination/'model.plan.json')
        self.assertEqual(_read(self.parent/'model.plan.json')['groups'][0],payload['groups'][0])
        self.assertTrue(any(group['category']=='slab'for group in payload['groups']))
        self.assertFalse((self.destination/'approval.json').exists())
        self.assertFalse(list(self.destination.rglob('*.skp')))
        self.assertEqual(before,{p.name:digest_file(p)for p in self.parent.iterdir()if p.is_file()})
        with self.assertRaises(WorkflowError):build_job(self.destination)

    def test_missing_slab_thickness_proposes_200_without_approving_revision(self):
        self.config['systems'][0]['default_thickness_mm']=None
        _write(self.configpath,self.config)
        review=self.attach()
        self.assertFalse(review['blockers'],review['blockers'])
        slabs=[g for g in _read(self.destination/'model.plan.json')['groups'] if g['category']=='slab']
        self.assertTrue(slabs)
        self.assertTrue(all(g['height_mm']==200 for g in slabs if g['slab_system_id']=='FLOOR'))
        self.assertFalse((self.destination/'approval.json').exists())
        with self.assertRaises(WorkflowError):build_job(self.destination)

    def test_special_slab_still_blocks_when_explicit_thickness_is_required(self):
        self.config['systems'][0].update(default_thickness_mm=None,thickness_policy='explicit_required')
        _write(self.configpath,self.config)
        review=self.attach()
        self.assertTrue(review['blockers'])
        self.assertTrue(_read(self.destination/'model.plan.json')['slab_audit'])
        with self.assertRaises(WorkflowError):approve_job(self.destination,confirmation='Fixture approval cannot invent missing dimensions',expected_review=review['review_sha256'])

    def test_source_bound_review_exceptions_survive_attach_and_new_decision_wins(self):
        import shutil
        candidate=_read(self.parent/'candidate.json')
        previous=self.parent
        self.parent=self.root/'policy-base'
        # Source-bound exceptions can originate in a historical pre-standard
        # approved building base. Construct that isolated fixture explicitly;
        # normal revise now requires an actual roof before approval.
        self.parent.mkdir()
        for name in ('document.json','classification.json','decisions.json','rules.json','layers.json','job.json'):
            shutil.copy2(previous/name,self.parent/name)
        decisions=_read(self.parent/'decisions.json')
        decisions['slabs']={'source_sha256':decisions['source_sha256'],'region_overrides':{'note':{'action':'level_change','reason':'prior case correction'}},'symbol_endpoint_tolerance_mm':2}
        _write(self.parent/'decisions.json',decisions)
        # Refresh the synthetic approved base after adding its policy metadata.
        from dwg_semantic.modeling import _json_hash
        candidate['decisions_digest']=_json_hash(decisions)
        _write(self.parent/'candidate.json',candidate)
        review=review_job(self.parent,self.parent/'candidate.json')
        approve_job(self.parent,confirmation='Synthetic base policy test only',expected_review=review['review_sha256'])
        self.config['region_overrides']={'note':{'action':'retain','reason':'new explicit decision'}}
        _write(self.configpath,self.config)
        review=self.attach()
        self.assertFalse(review['blockers'],review['blockers'])
        actual=_read(self.destination/'decisions.json')['slabs']
        self.assertEqual('retain',actual['region_overrides']['note']['action'])
        self.assertEqual(2,actual['symbol_endpoint_tolerance_mm'])
        self.assertEqual(self.config['systems'],actual['systems'])
        self.assertFalse((self.destination/'approval.json').exists())

    def test_real_source_tag_is_reused_without_creating_a_false_derived_layer(self):
        self.config.pop('derived_tag')
        self.config['tag_binding']={'tag':'Walls','source_layer':'Walls','kind':'source'}
        _write(self.configpath,self.config)
        review=self.attach()
        self.assertFalse(review['blockers'],review['blockers'])
        payload=_read(self.destination/'model.plan.json')
        self.assertEqual(1,len(payload['tag_mapping']))
        self.assertTrue(all(group['tag']=='Walls'for group in payload['groups']))

    def test_prepared_slab_holes_cannot_differ_from_the_reviewed_decisions(self):
        self.attach()
        from dwg_semantic.prepared import validate_prepared
        payload=_read(self.destination/'model.plan.json')
        slab=next(group for group in payload['groups']if group['category']=='slab')
        slab['holes']=[[[500,500],[800,500],[800,800],[500,800]]]
        result=validate_prepared(payload,_read(self.destination/'document.json'),_read(self.destination/'classification.json'),_read(self.destination/'decisions.json'))
        self.assertTrue(any(issue['code']=='prepared_slab_geometry_differs_from_decisions'for issue in result['blockers']))

    def test_original_model_inputs_changed_after_revision_are_rejected(self):
        self.attach()
        original=_read(self.parent/'model.plan.json')
        original['groups'][0]['height_mm']=9999
        _write(self.parent/'model.plan.json',original)
        with self.assertRaises(ValueError):review_job(self.destination)

    def test_derived_tag_collision_cannot_rename_an_existing_raw_tag(self):
        self.config['derived_tag'].update(tag='Walls',source_layer='Walls')
        _write(self.configpath,self.config)
        with self.assertRaises(ValueError):self.attach()

    def test_user_facing_rc_preview_is_bound_to_the_same_approval(self):
        review=self.attach()
        (self.destination/'RC.html').write_text('Synthetic slab scope preview',encoding='utf-8')
        _write(self.destination/'presentation-files.json',{'files':['RC.html']})
        review=review_job(self.destination)
        self.assertIn('RC.html',review['presentation_hashes'])
        (self.destination/'RC.html').write_text('Different holes or levels',encoding='utf-8')
        with self.assertRaises(WorkflowError):approve_job(self.destination,confirmation='Fixture confirmation',expected_review=review['review_sha256'])

    def test_extra_preview_cannot_escape_the_revision_folder(self):
        self.attach()
        _write(self.destination/'presentation-files.json',{'files':['../fixture.dwg']})
        with self.assertRaises(WorkflowError):review_job(self.destination)

    def test_existing_target_or_original_job_is_not_overwritten(self):
        with self.assertRaises(WorkflowError):attach_slab_job(self.parent,self.parent,self.configpath)
        destination=self.root/'existing';destination.mkdir()
        with self.assertRaises(WorkflowError):attach_slab_job(self.parent,destination,self.configpath)

    def test_disabled_material_cuts_keep_original_building_through_review(self):
        self.attach()
        decisions=_read(self.destination/'decisions.json')
        decisions['building_slab_cuts']={'enabled':False}
        _write(self.destination/'decisions.json',decisions)
        review=review_job(self.destination)
        self.assertFalse(review['blockers'],review['blockers'])
        payload=_read(self.destination/'model.plan.json')
        self.assertEqual(_read(self.parent/'model.plan.json')['groups'][0],payload['groups'][0])
        self.assertFalse(payload['base_component_cut_audit']['enabled'])

    def test_exact_curtain_cut_composition_preserves_trusted_base_snapshot(self):
        from dwg_semantic.slab_workflow import compose_slab_payload
        from dwg_semantic.slab_modeling import prepare_slabs
        from dwg_semantic.slab_contacts import resolve_building_slab_cuts
        from dwg_semantic.prepared import validate_prepared
        base=_read(self.parent/'model.plan.json')
        base['groups'][0]['category']='curtain'
        original=copy.deepcopy(base)
        decisions=_read(self.parent/'decisions.json')
        decisions['prepared_categories_by_primary_layer']={'Walls':['curtain']}
        config=copy.deepcopy(self.config)
        config['systems']=config['systems'][:1]
        config['systems'][0].update(role='overhead',default_top_z_mm=2000,default_thickness_mm=200)
        decisions['slabs']=config
        decisions['building_slab_cuts']={'enabled':True,'operation':'subtract_rc_material_only','target_component_ids':['wall'],'target_categories':['curtain','curtain_accessory']}
        document=_read(self.parent/'document.json')
        slabs=prepare_slabs(document,config)
        cuts=resolve_building_slab_cuts(base['groups'],slabs['groups'],decisions['building_slab_cuts'])
        self.assertFalse(cuts['blockers'],cuts['blockers'])
        decisions['component_height_rules']=cuts['height_rules']
        payload=compose_slab_payload(base,document,{},decisions)
        checked=validate_prepared(payload,document,{},decisions,base_payload=base)
        self.assertFalse(checked['blockers'],checked['blockers'])
        self.assertEqual(original,base)
        building=[group for group in checked['groups']if group['category']!='slab']
        self.assertEqual(2,len(building))
        self.assertEqual({'Walls'},{group['tag']for group in building})
        self.assertAlmostEqual(4000*200*200,checked['base_component_cut_audit']['removed_volume_mm3'])
        self.assertFalse((self.parent/'delivery').exists())

    def test_roof_fit_extends_and_is_recomputed_from_the_trusted_base(self):
        from dwg_semantic.slab_workflow import compose_slab_payload,resolve_building_revisions
        from dwg_semantic.slab_modeling import prepare_slabs
        from dwg_semantic.prepared import validate_prepared
        base=_read(self.parent/'model.plan.json');original=copy.deepcopy(base)
        decisions=_read(self.parent/'decisions.json');document=_read(self.parent/'document.json')
        config=copy.deepcopy(self.config)
        config['systems']=config['systems'][:1]
        config['systems'][0].update(role='overhead',default_top_z_mm=3700,default_thickness_mm=200)
        decisions.update(slabs=config,wall_under_roof={'enabled':True,'operation':'fit_to_lowest_overhead_underside','target_categories':['architectural_wall']},presentation={'overhead_visible':True})
        slabs=prepare_slabs(document,config)
        revised=resolve_building_revisions(base['groups'],slabs['groups'],decisions)
        self.assertFalse(revised['blockers'],revised['blockers'])
        decisions['component_height_rules']=revised['height_rules']
        payload=compose_slab_payload(base,document,{},decisions)
        checked=validate_prepared(payload,document,{},decisions,base_payload=base)
        self.assertFalse(checked['blockers'],checked['blockers'])
        wall=next(g for g in checked['groups']if g['category']=='architectural_wall')
        self.assertEqual(3500,wall['height_mm'])
        self.assertEqual({'overhead_visible':True},payload['presentation'])
        self.assertEqual(original,base)
        forged=copy.deepcopy(payload)
        next(g for g in forged['groups']if g['category']=='architectural_wall')['height_mm']=3000
        bad=validate_prepared(forged,document,{},decisions,base_payload=base)
        self.assertTrue(any(e['code']=='prepared_building_cut_geometry_differs_from_immutable_base'for e in bad['blockers']))
        forged=copy.deepcopy(payload);forged['wall_under_roof_audit']['original_groups'][0]['height_mm']=9999
        bad=validate_prepared(forged,document,{},decisions,base_payload=base)
        self.assertTrue(any(e['code']=='prepared_roof_fit_audit_differs_from_recomputed_recipe'for e in bad['blockers']))


if __name__=='__main__':unittest.main()

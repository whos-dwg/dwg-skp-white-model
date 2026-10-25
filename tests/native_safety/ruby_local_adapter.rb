# Isolated native-API-shaped test data. Reuse only the existing fixture classes;
# the production inspector/planner/apply implementations are never substituted.
fixture_path = File.join(ENV.fetch('DWG_AUDIT_FIXTURE_ROOT'), 'ruby_bridge_adapter.rb')
eval(File.read(fixture_path).split('request=JSON.parse', 2).first, TOPLEVEL_BINDING, fixture_path)

class AuditBounds
  def initialize(points)
    values = points.empty? ? [[0.0,0.0,0.0]] : points.map(&:to_a)
    @low = 3.times.map { |axis| values.map { |v| v[axis] }.min }
    @high = 3.times.map { |axis| values.map { |v| v[axis] }.max }
  end
  def corner(index)
    Geom::Point3d.new(3.times.map { |axis| ((index >> axis) & 1) == 1 ? @high[axis] : @low[axis] })
  end
  def min; Geom::Point3d.new(@low); end
  def max; Geom::Point3d.new(@high); end
  def valid?; true; end
  def empty?; false; end
end

module Sketchup
  class Group
    attr_accessor :audit_guid
    def local_points
      entities.flat_map do |item|
        if item.is_a?(Sketchup::Face)
          item.vertices.map(&:position)
        elsif item.is_a?(Sketchup::Edge)
          [item.start.position,item.end.position]
        elsif item.is_a?(Sketchup::Group) && item.valid?
          8.times.map { |i| item.bounds.corner(i) }
        else
          []
        end
      end
    end
    def bounds
      AuditBounds.new(local_points.map { |p| transformation*p })
    end
    def definition
      group=self
      definition=Object.new
      definition.define_singleton_method(:guid) { group.audit_guid || "definition:#{group.persistent_id}:initial" }
      definition.define_singleton_method(:entities) { group.entities }
      definition.define_singleton_method(:bounds) { AuditBounds.new(group.local_points) }
      definition
    end
    def typename; 'Group'; end
    def deleted?; !valid?; end
  end
end
class AuditModel
  attr_accessor :active_path
end

module Geom
  Transformation = AuditTransform
end
module AuditPrimitiveIdentity
  def persistent_id; @audit_primitive_id ||= ($audit_primitive_id = ($audit_primitive_id || 100_000) + 1); end
end
Sketchup::Face.include(AuditPrimitiveIdentity)
Sketchup::Edge.include(AuditPrimitiveIdentity)
class Sketchup::Face
  attr_accessor :back_material
end

request=JSON.parse(File.read(ENV.fetch('DWG_AUDIT_REQUEST_FILE')))
load ENV.fetch('DWG_AUDIT_BRIDGE')
bridge=DWGLiveRepair::Bridge
snapshot=request.fetch('fixture')
model=AuditModel.new(snapshot)
root_data=snapshot.fetch('root')
root=model.register(Sketchup::Group.new(root_data.fetch('entity_id').split(':').last.to_i,'DWG model',{
  'schema'=>'dwg-skp-model-v2','payload_sha256'=>root_data['payload_sha256'],
  'source_manifest'=>JSON.generate(root_data['source_manifest']),
  'tag_mapping'=>JSON.generate(root_data['tag_mapping'] || []),
  'source_tag_policy'=>root_data.fetch('source_tag_policy','effective'),
  'modeling_standard'=>(root_data['modeling_standard'] ? JSON.generate(root_data['modeling_standard']) : nil),
  'fixed_standard_audit'=>(root_data['fixed_standard_audit'] ? JSON.generate(root_data['fixed_standard_audit']) : nil),
  'live_geometry_state'=>root_data['live_geometry_state']
},root_data['world_transform']))
model.entities << root
snapshot.fetch('containers').each do |row|
  group=model.register(Sketchup::Group.new(row['entity_id'].split(':').last.to_i,row['name'],{},row['world_transform'] || {}))
  parent=model.registry[(row['parent_entity_id'] || root_data['entity_id']).split(':').last.to_i]
  group.parent=parent;parent.entities << group
end
rows=[*snapshot.fetch('entities'),*snapshot.fetch('protected_entities',[]),*snapshot.fetch('references',[])]
rows.each do |row|
  group=model.register(Sketchup::Group.new(row['entity_id'].split(':').last.to_i,row['name'] || row['group_id'],{
    'id'=>row['group_id'],'provenance'=>JSON.generate(row['provenance']),
    'live_footprint'=>JSON.generate(row['footprint'])
  },row['transform'] || {}))
  group.parent=model.registry[row['parent_entity_id'].split(':').last.to_i]
  group.parent.entities << group
  group.layer=AuditLayer.new(row['tag'] || row.dig('provenance','tag') || 'A-WALL')
  group.material=AuditMaterial.new(row['material'] || 'DWG white model')
  mesh=row['mesh']
  group.entities=AuditEntities.new.concat(mesh['faces'].map { |indices| Sketchup::Face.new(indices.map { |i| mesh['vertices'][i] }) }) if mesh
  row.fetch('segments',[]).each { |a,b| group.entities << Sketchup::Edge.new(a,b) }
  group.audit_guid=row['definition_guid'] if row['definition_guid']
end
snapshot.fetch('selection_entity_ids',[]).each { |id| model.selection.add(model.registry[id.split(':').last.to_i]) }
model.active_path=request['active_path']&.map { |id| model.registry[id.split(':').last.to_i] }
Sketchup.active_model=model
$audit_inject_face_failure=request['inject_face_failure']
$audit_reverse_created_faces=request['reverse_created_faces']

# Count calls to the expensive production geometry readers. Native bounds do
# not materialize meshes; our synthetic bounds derive their coordinates solely
# to reproduce that native query behavior without a SketchUp process.
heavy=Hash.new { |h,k| h[k]=[] }
%i[leaf_summary world_face_points entity_signature bottom_triangles_mm].each do |method|
  implementation=bridge.method(method)
  bridge.define_singleton_method(method) do |*args|
    heavy[method.to_s] << stable_id(args.first)
    implementation.call(*args)
  end
end

result=nil
begin
  if request['mode']=='apply'
    plan=request.fetch('plan')
    receipt=bridge.send(:apply_live_repair,{'plan'=>plan,'confirmation_sha256'=>plan['plan_sha256'], 'current_cad_model'=>request['current_cad_model']})
    result={'accepted'=>true,'receipt'=>receipt}
  elsif request['mode']=='full'
    result={'accepted'=>true,'inspection'=>bridge.send(:inspect_white_model)}
  elsif request['mode']=='global'
    result={'accepted'=>true,'inspection'=>bridge.send(:inspect_white_model_global)}
  else
    result={'accepted'=>true,'inspection'=>bridge.send(:inspect_white_model_local,request.fetch('query'))}
  end
rescue StandardError => error
  result={'accepted'=>false,'code'=>(error.respond_to?(:code) ? error.code : error.class.name),
          'error'=>error.message,'backtrace'=>error.backtrace.first(4)}
end
result['operations']=model.operations
result['heavy_reads']=heavy
JSON.generate(result)

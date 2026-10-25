# Pure synthetic SketchUp adapter: executes production Ruby methods in a child
# process. It has no native SketchUp API and must never start a socket listener.
require 'json'
require 'matrix'

def file_loaded?(_path); true; end
def file_loaded(_path); end
module UI
  def self.start_timer(*_args); raise 'Audit forbids UI timers'; end
end
module Geom
  class Vector3d
    def initialize(values); @values=values; end
    def to_a; @values; end
    def dot(other); @values.zip(other.to_a).sum { |a,b|a*b }; end
    def cross(other)
      a=@values;b=other.to_a
      Vector3d.new([a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]])
    end
  end
  class Point3d
    attr_accessor :x, :y, :z
    def initialize(values); @x,@y,@z=values; end
    def to_a; [@x,@y,@z]; end
    def -(other); Vector3d.new(to_a.zip(other.to_a).map { |a,b|a-b }); end
  end
end
class AuditTransform
  attr_reader :origin, :xaxis, :yaxis, :zaxis
  def initialize(summary={})
    @origin=Geom::Point3d.new((summary['origin_mm'] || [0,0,0]).map { |v| v.to_f/25.4 })
    @xaxis=Geom::Point3d.new(summary['xaxis'] || [1.0,0.0,0.0])
    @yaxis=Geom::Point3d.new(summary['yaxis'] || [0.0,1.0,0.0])
    @zaxis=Geom::Point3d.new(summary['zaxis'] || [0.0,0.0,1.0])
  end
  def *(other)
    if other.is_a?(AuditTransform)
      return AuditTransform.from_matrix(matrix * other.matrix)
    end
    point=matrix * Vector[other.x,other.y,other.z,1.0]
    Geom::Point3d.new(point.to_a[0,3])
  end
  def matrix
    Matrix.columns([@xaxis.to_a+[0.0],@yaxis.to_a+[0.0],@zaxis.to_a+[0.0],@origin.to_a+[1.0]])
  end
  def self.from_matrix(matrix)
    AuditTransform.new('origin_mm'=>matrix.column(3).to_a[0,3].map { |v|v*25.4 },
                       'xaxis'=>matrix.column(0).to_a[0,3],
                       'yaxis'=>matrix.column(1).to_a[0,3],
                       'zaxis'=>matrix.column(2).to_a[0,3])
  end
  def inverse; AuditTransform.from_matrix(matrix.inverse); end
  def identity?; to_a == AuditTransform.new.to_a; end
  def to_a; [*@xaxis.to_a,0.0,*@yaxis.to_a,0.0,*@zaxis.to_a,0.0,*@origin.to_a,1.0]; end
end
class AuditLayer
  attr_accessor :name
  def initialize(name); @name=name; end
end
class AuditMaterial
  attr_accessor :name
  def initialize(name); @name=name; end
end
class AuditVertex
  attr_reader :position
  def initialize(point); @position=Geom::Point3d.new(point.map { |v|v.to_f/25.4 }); end
end
class AuditLoop
  attr_reader :vertices
  def initialize(vertices); @vertices=vertices; end
  def outer?; true; end
end
class AuditEntities < Array
  def add_group
    model=Sketchup.active_model
    identity=(model.registry.keys.max || 0)+1
    group=Sketchup::Group.new(identity,'')
    model.register(group)
    self << group
    group
  end
  def add_face(points)
    return nil if $audit_inject_face_failure
    face=Sketchup::Face.new(points.map { |p|p.to_a.map { |v|v*25.4 } })
    face.reverse! if $audit_reverse_created_faces # Exercise native add_face orientation changes.
    self << face
    face
  end
end
class AuditLayers
  def initialize; @layers={'Untagged'=>AuditLayer.new('Untagged')}; end
  def [](name); return @layers['Untagged'] if name==0; @layers[name]; end
  def add(name); @layers[name] ||= AuditLayer.new(name); end
end
module Sketchup
  class Face
    attr_reader :vertices
    attr_accessor :layer, :material
    def initialize(points)
      @vertices=points.map { |p| AuditVertex.new(p) }
      @layer=AuditLayer.new('Untagged')
      @material=nil
    end
    def loops; [AuditLoop.new(vertices)]; end
    def normal; (vertices[1].position-vertices[0].position).cross(vertices[2].position-vertices[0].position); end
    def reverse!; @vertices.reverse!; self; end
    def mesh(_flags=0)
      mesh=Object.new
      points=vertices.map(&:position)
      mesh.define_singleton_method(:point_at) { |index| points.fetch(index-1) }
      mesh.define_singleton_method(:polygons) { (2...points.length).map { |i| [1,i,i+1] } }
      mesh
    end
  end
  class Edge
    attr_accessor :layer
    attr_reader :start,:end
    def initialize(a,b)
      @start=AuditVertex.new(a);@end=AuditVertex.new(b);@layer=AuditLayer.new('Untagged')
    end
  end
  class Group
    attr_accessor :name,:layer,:material,:transformation,:entities,:persistent_id,:erased,:parent
    def initialize(id,name,attrs={},transform={})
      @persistent_id=id;@name=name;@attrs=attrs;@transformation=AuditTransform.new(transform)
      @layer=AuditLayer.new('Untagged');@material=nil;@entities=AuditEntities.new;@erased=false;@volume=nil
    end
    def get_attribute(_dictionary,key,default=nil); @attrs.fetch(key,default); end
    def set_attribute(_dictionary,key,value); @attrs[key]=value; end
    def valid?; !@erased; end
    def manifold?; @attrs.fetch('audit_manifold',true); end
    def volume
      return @volume unless @volume.nil?
      @entities.grep(Sketchup::Face).sum do |face|
        points=face.vertices.map { |v|v.position.to_a }
        a=points.first
        points[1..-1].each_cons(2).sum do |b,c|
          (a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0]))/6.0
        end
      end.abs
    end
    def volume=(value); @volume=value; end
    def locked?; false; end
    def hidden?; false; end
    def erase!; @erased=true; end
  end
  def self.active_model; @model; end
  def self.active_model=(value); @model=value; end
  def self.version; 'audit-adapter'; end
end
class AuditSelection < Array
  def add(group); self << group; end
end
class AuditModel
  attr_accessor :guid,:path,:entities,:selection
  attr_reader :operations,:registry,:layers,:materials
  def initialize(snapshot)
    @guid=snapshot['model']['guid'];@path=snapshot['model']['path'];@entities=[]
    @selection=AuditSelection.new;@operations=[];@registry={};@layers=AuditLayers.new
    @materials={'DWG white model'=>AuditMaterial.new('DWG white model')}
  end
  def title; 'Isolated synthetic model'; end
  def modified?; true; end
  def options; {'UnitsOptions'=>{'LengthUnit'=>2}}; end
  def register(group); @registry[group.persistent_id]=group;group; end
  def find_entity_by_persistent_id(id); @registry[id]; end
  def start_operation(name,*_args)
    @operations << 'start';@before=@registry.transform_values { |g| g.erased };@before_ids=@registry.keys
  end
  def commit_operation; @operations << 'commit'; end
  def abort_operation
    @operations << 'abort'
    @before.each { |id,erased| @registry[id].erased=erased }
    (@registry.keys-@before_ids).each { |id| @registry[id].erased=true }
  end
end

request=JSON.parse(ENV.fetch('DWG_AUDIT_REQUEST'))
bridge_path=ENV.fetch('DWG_AUDIT_BRIDGE')
load bridge_path
bridge=DWGLiveRepair::Bridge
snapshot=request.fetch('snapshot')
plan=request.fetch('plan')
model=AuditModel.new(snapshot)
root_data=snapshot.fetch('root')
root=model.register(Sketchup::Group.new(root_data.fetch('entity_id').split(':').last.to_i,'DWG model',{
  'schema'=>'dwg-skp-model-v2', 'payload_sha256'=>root_data['payload_sha256'],
  'source_manifest'=>JSON.generate(root_data['source_manifest'] || {}),
  'modeling_standard'=>(root_data['modeling_standard'] ? JSON.generate(root_data['modeling_standard']) : nil),
  'fixed_standard_audit'=>(root_data['fixed_standard_audit'] ? JSON.generate(root_data['fixed_standard_audit']) : nil),
  'live_geometry_state'=>root_data['live_geometry_state']
},root_data['world_transform']))
model.entities << root
snapshot.fetch('containers').each do |row|
  group=model.register(Sketchup::Group.new(row['entity_id'].split(':').last.to_i,row['name'],{},row['world_transform'] || {}))
  group.parent=root;root.entities << group
end
snapshot.fetch('entities').each do |row|
  group=model.register(Sketchup::Group.new(row['entity_id'].split(':').last.to_i,row['name'],{
    'id'=>row['group_id'],'provenance'=>JSON.generate(row['provenance']),
    'live_footprint'=>JSON.generate(row['footprint'])
  }))
  group.parent=model.registry[row['parent_entity_id'].split(':').last.to_i]
  group.parent.entities << group
end
if request['reference_fixture']
  container=model.register(Sketchup::Group.new(30,'reference'))
  container.parent=root;root.entities << container
  reference=model.register(Sketchup::Group.new(301,'Synthetic source reference',{
    'id'=>'reference:301', 'provenance'=>JSON.generate({
      'id'=>'reference:301','mode'=>'reference_edges','category'=>'reference',
      'source_entity_ids'=>['source::R1'],'source_layer'=>'A-NOTE','source_layers'=>['A-NOTE'],
      'segments'=>[{'start'=>[0,0],'end'=>[1000,0]}], 'base_z_mm'=>0
    })
  }))
  reference.parent=container;reference.entities << Sketchup::Edge.new([0,0,0],[1000,0,0])
  container.entities << reference
end
Sketchup.active_model=model

# Read-only inspection returns a controlled live snapshot. The production
# apply/preflight/digest/operation methods still execute unchanged.
symbolized=Hash[snapshot.map { |k,v| [k.to_sym,v] }]
%w[model root].each { |name| symbolized[name.to_sym]=Hash[snapshot[name].map { |k,v| [k.to_sym,v] }] }
%w[entities containers protected_entities].each { |name| symbolized[name.to_sym]=(snapshot[name] || []).map { |row| Hash[row.map { |k,v| [k.to_sym,v] }] } }
bridge.define_singleton_method(:inspect_white_model) { symbolized }
bridge.define_singleton_method(:create_group) do |_model,parent,_parent_world,row,digest|
  raise 'Synthetic face creation failure' if request['inject_create_failure']
  group=Sketchup::Group.new(10_000+model.registry.length,row['name'],{
    'id'=>row['group_id'],'provenance'=>JSON.generate(row['provenance']),
    'live_footprint'=>JSON.generate(row['footprint']),'payload_sha256'=>"live-repair:#{digest}",
    'live_repair_plan_sha256'=>digest
  })
  group.layer=AuditLayer.new(row['tag']);group.material=AuditMaterial.new(row['material']) if row['material']
  group.parent=parent;parent.entities << group
  group.entities=row['mesh']['faces'].map { |indices| Sketchup::Face.new(indices.map { |i|row['mesh']['vertices'][i] }) }
  group.volume=row['mesh']['expected_volume_mm3']/(25.4**3)
  model.register(group)
  group
end

result=nil
begin
  if request['production_create']
    load bridge_path
    bridge.define_singleton_method(:inspect_white_model) { symbolized }
    $audit_inject_face_failure=request['inject_create_failure']
  end
  if request['mode']=='digest'
    result={'accepted'=>true,'digest'=>bridge.send(:validate_plan_digest,plan),'operations'=>model.operations}
  elsif request['mode']=='readback'
    row=plan.fetch('create').first
    parent=model.registry[row['parent_entity_id'].split(':').last.to_i]
    group=bridge.send(:create_group,model,parent,AuditTransform.new,row,plan['plan_sha256'])
    case request['mutation']
    when 'tag'; group.layer=AuditLayer.new('WRONG-TAG')
    when 'provenance'; group.set_attribute(DWGLiveRepair::DICTIONARY,'provenance','{}')
    when 'raw_tag'; group.entities.grep(Sketchup::Face).first.layer=AuditLayer.new('WRONG-RAW-TAG')
    when 'offset'
      group.entities.grep(Sketchup::Face).each { |face| face.vertices.each { |v|v.position.x += 1.0 } }
    end
    bridge.send(:verify_created!,[group],[row],bridge.send(:hierarchy_index,root))
    result={'accepted'=>true,'operations'=>model.operations}
  elsif request['mode']=='inspect_geometry'
    row=plan.fetch('create').first
    parent=model.registry[row['parent_entity_id'].split(':').last.to_i]
    group=bridge.send(:create_group,model,parent,AuditTransform.new,row,plan['plan_sha256'])
    case request['mutation']
    when 'sloped_top'
      group.entities.grep(Sketchup::Face).each do |face|
        face.vertices.each { |v|v.position.z += 100.0/25.4 if (v.position.z*25.4-row['height_mm']).abs<0.001 && (v.position.x*25.4-6000).abs<0.001 }
      end
    when 'sheared_top'
      group.entities.grep(Sketchup::Face).each do |face|
        face.vertices.each { |v|v.position.x += 100.0/25.4 if (v.position.z*25.4-row['height_mm']).abs<0.001 }
      end
    when 'nested_group'
      group.entities << Sketchup::Group.new(8989,'Unsupported hand-made nested group')
    when 'conditioned_local_geometry'
      scale=10_000.0
      origin=[123456.789,-98765.432,0.0]
      group.entities.grep(Sketchup::Face).each do |face|
        face.vertices.each do |v|
          v.position.x=(v.position.x-origin[0]/25.4)*scale
          v.position.y=(v.position.y-origin[1]/25.4)*scale
          v.position.z=(v.position.z-origin[2]/25.4)*scale
        end
      end
      group.transformation=AuditTransform.new('origin_mm'=>origin,'xaxis'=>[1/scale,0,0],'yaxis'=>[0,1/scale,0],'zaxis'=>[0,0,1/scale])
      # A local-volume API must not be mistaken for world-space evidence.
      group.volume=row['mesh']['expected_volume_mm3']*scale**3/(25.4**3)
    when 'stepped_slab_companion'
      category=model.register(Sketchup::Group.new(31,'overhead_slab'))
      category.parent=root;root.entities << category
      section=[[0,0],[1000,0],[1000,-100],[2000,-100],[2000,100],[1000,100],[1000,200],[0,200]]
      front=section.map { |x,z|[x,1000,z+4000] }
      back=section.reverse.map { |x,z|[x,2000,z+4000] }
      faces=[front,back]
      section.each_with_index do |(x,z),i|
        nx,nz=section[(i+1)%section.length]
        faces << [[x,1000,z+4000],[nx,1000,nz+4000],[nx,2000,nz+4000],[x,2000,z+4000]]
      end
      slab=model.register(Sketchup::Group.new(302,'Synthetic whole stepped slab',{
        'id'=>'stepped-slab:302', 'provenance'=>JSON.generate({
          'id'=>'stepped-slab:302','category'=>'slab','mode'=>'boundary_mesh',
          'source_entity_ids'=>['source::S1'],'source_layer'=>'A-SLAB','source_layers'=>['A-SLAB']
        })
      }))
      slab.parent=category;slab.entities=AuditEntities.new.concat(faces.map { |p|Sketchup::Face.new(p) })
      category.entities << slab
    end
    load bridge_path
    result={'accepted'=>true,'inspection'=>bridge.send(:inspect_white_model),'operations'=>model.operations}
  elsif request['mode']=='inspect'
    # This mode is reserved for real inspector behavior. Remove only our
    # controlled snapshot override and use the production Ruby method again.
    bridge.singleton_class.send(:remove_method,:inspect_white_model)
    # The method was originally defined on this singleton class, so it cannot
    # be restored with remove_method. Evaluate the production definition from
    # a second load, still protected by file_loaded? and no socket startup.
    load bridge_path
    result=bridge.send(:inspect_white_model)
    result={'accepted'=>true,'inspection'=>result,'operations'=>model.operations}
  else
    receipt=bridge.send(:apply_live_repair,{'plan'=>plan,'confirmation_sha256'=>plan['plan_sha256']})
    result={'accepted'=>true,'receipt'=>receipt,'operations'=>model.operations,
            'selection_entity_ids'=>model.selection.map { |group| "entity:#{group.persistent_id}" }}
  end
rescue StandardError => error
  result={'accepted'=>false,'error'=>error.message,'code'=>(error.respond_to?(:code) ? error.code : error.class.name),
          'operations'=>model.operations}
end
JSON.generate(result)



# DWG_LIVE_REPAIR_BRIDGE
# Purpose-built localhost bridge for undoable DWG white-model geometry repair.
# It accepts bounded model inspection and hash-bound apply_live_repair requests.

require 'socket'
require 'json'
require 'digest'
require 'digest/sha2'
require 'time'

module DWGLiveRepair
  DICTIONARY = 'DWG_LAYERED_SKP'.freeze
  PROTOCOL = 'dwg-live-repair'.freeze
  PROTOCOL_VERSION = '2.0'.freeze
  HOST = '127.0.0.1'.freeze
  PORT = 51_236
  MAX_REQUEST_BYTES = 32 * 1024 * 1024
  POLL_SECONDS = 0.05
  SUPPORTED_CATEGORIES = %w[
    architectural_wall structural_wall column curtain curtain_accessory
  ].freeze
  FIXED_MODELING_STANDARD = {
    'version' => 'fixed-white-model-20261021', 'slab_assembly_mode' => 'whole_uniform_thickness',
    'preserve_openings_and_outer_boundary' => true, 'verticals_to_slab_underside' => true,
    'stair_opening_scope' => 'entire_enclosure_including_landings',
    'fire_door_and_shutter_bodies' => 'exclude_3d_keep_opening_evidence'
  }.freeze

  class RepairError < StandardError
    attr_reader :code, :details

    def initialize(code, message, details = {})
      super(message)
      @code = code
      @details = details
    end
  end

  module Bridge
    class << self
      def start
        return true if running?
        @server = TCPServer.new(HOST, PORT)
        @server.listen(8)
        @clients = {}
        @timer = UI.start_timer(POLL_SECONDS, true) { poll }
        puts "DWG live-repair bridge listening on #{HOST}:#{PORT}"
        true
      rescue Errno::EADDRINUSE
        @server = nil
        warn "DWG live-repair bridge port #{PORT} is already in use"
        false
      end

      def stop
        UI.stop_timer(@timer) if @timer
        (@clients || {}).each_key { |socket| safe_close(socket) }
        safe_close(@server)
        @timer = nil
        @server = nil
        @clients = {}
        true
      end

      def running?
        !@server.nil? && !@server.closed?
      end

      def poll
        accept_clients
        read_clients
      rescue StandardError => error
        warn "DWG live-repair bridge poll error: #{error.class}: #{error.message}"
      end

      private

      def accept_clients
        loop do
          socket = @server.accept_nonblock
          @clients[socket] = +''
        rescue IO::WaitReadable, Errno::EINTR
          break
        end
      end

      def read_clients
        @clients.keys.each do |socket|
          begin
            loop do
              @clients[socket] << socket.read_nonblock(65_536)
              if @clients[socket].bytesize > MAX_REQUEST_BYTES
                raise RepairError.new('REQUEST_TOO_LARGE', 'Repair request exceeds the bridge limit.')
              end
            end
          rescue IO::WaitReadable
            process_client(socket) if @clients[socket].include?("\n")
          rescue EOFError
            process_client(socket) unless @clients[socket].empty?
          rescue StandardError => error
            write_response(socket, failure(nil, error))
            close_client(socket)
          end
        end
      end

      def process_client(socket)
        request_id = nil
        begin
          line = @clients[socket].split("\n", 2).first
          request = JSON.parse(line)
          request_id = request['request_id']
          method = request['method']
          params = request['params'] || {}
          unless request_id.is_a?(String) && !request_id.empty? && params.is_a?(Hash)
            raise RepairError.new('INVALID_ARGUMENT', 'request_id and params are required.')
          end
          data = case method
                 when 'health' then health
                 when 'inspect_white_model' then inspect_white_model
                   when 'inspect_white_model_global' then inspect_white_model_global
                 when 'inspect_white_model_local' then inspect_white_model_local(params['query'])
                 when 'apply_live_repair' then apply_live_repair(params)
                 else
                   raise RepairError.new('UNSUPPORTED_OPERATION', 'Bridge method is not approved.', method: method)
                 end
          write_response(socket, {
            request_id: request_id, ok: true, data: data, warnings: [],
            meta: { bridge: PROTOCOL, protocol_version: PROTOCOL_VERSION, sketchup_version: Sketchup.version }
          })
        rescue JSON::ParserError => error
          write_response(socket, failure(request_id, RepairError.new('INVALID_JSON', error.message)))
        rescue StandardError => error
          write_response(socket, failure(request_id, error))
        ensure
          close_client(socket)
        end
      end

      def health
        model = Sketchup.active_model
        {
          bridge: 'connected', protocol: PROTOCOL, protocol_version: PROTOCOL_VERSION,
          fixed_modeling_standard_version: FIXED_MODELING_STANDARD['version'],
          sketchup_version: Sketchup.version, model_available: !model.nil?, port: PORT,
          supported_operations: %w[inspect_white_model inspect_white_model_local inspect_white_model_global apply_live_repair],
          partial_snapshot_version: '1.0',
          whole_model_sync_version: '1.0',
          slab_sync_version: '1.0',
          writes_new_skp: false, saves_model: false, undoable: true
        }
      end

      def model!
        Sketchup.active_model || raise(RepairError.new('MODEL_NOT_READY', 'No active SketchUp model is available.'))
      end

      def white_model_root(model)
        roots = model.entities.grep(Sketchup::Group).select do |group|
          group.get_attribute(DICTIONARY, 'schema').to_s.start_with?('dwg-skp-model-')
        end
        unless roots.length == 1
          raise RepairError.new(
            'WHITE_MODEL_ROOT_AMBIGUOUS',
            'The active model must contain exactly one source-bound DWG model root.',
            count: roots.length
          )
        end
        roots.first
      end

      def inspect_white_model
        model = model!
        root = white_model_root(model)
        root_world = root.transformation
        containers = []
        leaves = []
        @inspection_references = []
        @inspection_protected = []
        transform_index = { stable_id(root) => root_world }
        traverse_groups(root.entities, root_world, stable_id(root), containers, leaves, transform_index)
        snapshot_summary(model, root, containers, leaves, @inspection_references, @inspection_protected)
      end

      def inspect_white_model_global
        model = model!
        if (model.active_path || []).any? { |g| g.respond_to?(:get_attribute) && !g.get_attribute(DICTIONARY, 'id').to_s.empty? }
          raise RepairError.new('GLOBAL_EDIT_CONTEXT_OPEN', 'Exit source-leaf editing before whole-model comparison.')
        end
        snapshot = inspect_white_model
        rows = []
        visit = lambda do |entities, world, path, stack|
          raise RepairError.new('GLOBAL_READ_LIMIT', 'Complete model proof exceeds the entity limit.') if rows.length > 500_000
          entities.each do |entity|
            raise RepairError.new('GLOBAL_READ_LIMIT', 'Complete model proof exceeds the entity limit.') if rows.length >= 500_000
            row = {id: stable_id(entity), path: path, type: entity.class.name}
            row[:tag] = entity.layer.name.to_s if entity.respond_to?(:layer) && entity.layer
            row[:material] = entity.material.name.to_s if entity.respond_to?(:material) && entity.material
            row[:hidden] = entity.hidden? if entity.respond_to?(:hidden?)
            row[:locked] = entity.locked? if entity.respond_to?(:locked?)
            if entity.respond_to?(:attribute_dictionaries) && entity.attribute_dictionaries
              row[:attributes] = entity.attribute_dictionaries.map { |dict| [dict.name, dict.map { |key,value| [key,value.to_s] }.sort_by(&:first)] }.sort_by(&:first)
            end
            point = lambda { |p| (world * p).to_a.map { |v| (v.to_f * 25.4).round(9) } }
            if entity.is_a?(Sketchup::Face)
              row[:loops] = entity.loops.map { |loop| loop.vertices.map { |v| point.call(v.position) } }
              row[:back_material] = entity.back_material.name.to_s if entity.back_material
            elsif entity.is_a?(Sketchup::Edge)
              row[:points] = [point.call(entity.start.position), point.call(entity.end.position)].sort
            elsif entity.is_a?(Sketchup::Group) || (entity.respond_to?(:definition) && entity.respond_to?(:transformation))
              row[:name] = entity.name.to_s
              transform = world * entity.transformation
              row[:world_transform] = transform_summary(transform)
              contents = entity.is_a?(Sketchup::Group) ? entity.entities : entity.definition.entities
              key = entity.is_a?(Sketchup::Group) ? stable_id(entity) : entity.definition.guid
              raise RepairError.new('GLOBAL_READ_CYCLE', 'Recursive component prevents complete model inspection.') if stack.include?(key)
              visit.call(contents, transform, path + [stable_id(entity)], stack + [key])
            end
            rows << row
          end
        end
        visit.call(model.entities, Geom::Transformation.new, [], [])
        snapshot[:global_read] = {schema: 'dwg-skp-global-read-v1', entity_count: rows.length,
          inventory_sha256: Digest::SHA256.hexdigest(JSON.generate(canonical(rows.sort_by { |r| [r[:path].join('/'),r[:id]] })))}
        snapshot[:read_stats] = {geometry_scope: 'whole_model', geometry_groups: snapshot[:entities].length + snapshot[:protected_entities].length,
                                reference_groups: snapshot[:references].length, actual_entity_count: rows.length}
        snapshot
      end

      def snapshot_summary(model, root, containers, leaves, references, protected)
        root_world = root.transformation
        selected = model.selection.map { |entity| stable_id(entity) if entity.respond_to?(:persistent_id) }.compact
        leaf_ids = leaves.map { |row| row[:entity_id] }
        {
          schema: 'dwg-skp-live-snapshot-v1', status: 'ready',
          observed_at_utc: Time.now.utc.iso8601(6),
          model: {
            guid: model.guid, path: model.path.to_s, name: model.title.to_s,
            modified: model.modified?, units: units_name(model)
          },
          root: {
            entity_id: stable_id(root), name: root.name.to_s,
            locked: root.locked?,
            payload_sha256: root.get_attribute(DICTIONARY, 'payload_sha256'),
            schema: root.get_attribute(DICTIONARY, 'schema'),
            live_geometry_state: root.get_attribute(DICTIONARY, 'live_geometry_state'),
            source_manifest: parse_json_attribute(root, 'source_manifest'),
            tag_mapping: parse_json_attribute(root, 'tag_mapping'),
            source_tag_policy: root.get_attribute(DICTIONARY, 'source_tag_policy', 'effective'),
            modeling_standard: parse_json_attribute(root, 'modeling_standard'),
            fixed_standard_audit: parse_json_attribute(root, 'fixed_standard_audit'),
            world_transform: transform_summary(root_world)
          },
          containers: containers,
          entities: leaves,
          references: references,
          protected_entities: protected,
          selection_entity_ids: selected & leaf_ids
        }
      end

      def traverse_groups(entities, parent_world, parent_id, containers, leaves, transform_index)
        entities.grep(Sketchup::Group).each do |group|
          world = parent_world * group.transformation
          entity_id = stable_id(group)
          transform_index[entity_id] = world
          group_id = group.get_attribute(DICTIONARY, 'id')
          if group_id && !group_id.to_s.empty?
            provenance = parse_json_attribute(group, 'provenance') || {}
            if provenance['mode'] == 'reference_edges' || provenance['category'] == 'reference'
              @inspection_references << {entity_id: entity_id, parent_entity_id: parent_id,
                                         group_id: group_id, provenance: provenance,
                                         signature: entity_signature(group, world)}
            elsif !SUPPORTED_CATEGORIES.include?(provenance['category']) || provenance['mode'] == 'boundary_mesh'
              @inspection_protected << protected_summary(group, world, parent_id, provenance)
            else
              leaves << leaf_summary(group, world, parent_id)
            end
          else
            containers << {
              entity_id: entity_id, parent_entity_id: parent_id,
              name: group.name.to_s, world_transform: transform_summary(world), locked: group.locked?
            }
            traverse_groups(group.entities, world, entity_id, containers, leaves, transform_index)
          end
        end
      end

      def protected_summary(group, world, parent_id, provenance)
        triangles = group.entities.grep(Sketchup::Face).flat_map do |face|
          mesh = face.mesh(0)
          mesh.polygons.map do |indices|
            indices.map { |i| p=world*mesh.point_at(i.abs); [p.x,p.y,p.z].map { |v| v.to_f*25.4 } }
          end
        end
        {entity_id: stable_id(group), parent_entity_id: parent_id,
         group_id: group.get_attribute(DICTIONARY, 'id'), category: provenance['category'], provenance: provenance,
         manifold: group.manifold?, triangles_mm: triangles,
         name: group.name.to_s, material: group.material ? group.material.name.to_s : nil,
         locked: group.locked?, hidden: group.hidden?, volume_mm3: safe_volume_mm3(group, world),
         nested_geometry: group.entities.any? { |e| e.is_a?(Sketchup::Group) || (defined?(Sketchup::ComponentInstance) && e.is_a?(Sketchup::ComponentInstance)) },
         tag: group.layer.name.to_s, signature: entity_signature(group,world),
         world_transform: transform_summary(world)}
      end

      # Partial reads are stateless. Every request re-reads the actual hierarchy,
      # world bounds and definition revisions, then reads full geometry only for
      # the server-expanded dependency region. No cached mesh authorizes a write.
      def inspect_white_model_local(query)
        query = validate_local_query(query)
        model = model!
        root = white_model_root(model)
        if model.respond_to?(:active_path) && (model.active_path || []).any? { |g| g.respond_to?(:get_attribute) && !g.get_attribute(DICTIONARY, 'id').to_s.empty? }
          raise RepairError.new('LOCAL_EDIT_CONTEXT_OPEN', 'Exit source-leaf editing before a bounded sync read; category context may remain open.')
        end
        containers, rows, census = [], [], []
        census << inventory_row(root, root.transformation, nil, nil, false)
        collect_local_inventory(root.entities, root.transformation, stable_id(root), containers, rows, census)
        selected = model.selection.map { |e| stable_id(e) if e.respond_to?(:persistent_id) }.compact
        explicit = query['target_entity_ids']
        unless (explicit - rows.select { |r| r[:kind] == :leaf }.map { |r| r[:meta][:entity_id] }).empty?
          raise RepairError.new('LOCAL_TARGET_NOT_FOUND', 'An explicit local target is not a source leaf.')
        end
        bounds = query['bounds_xy_mm']
        identifiers = query['source_identifiers']
        seeds = rows.select { |r| r[:kind] == :leaf }.select do |row|
          meta, proof = row[:meta], row[:provenance]
          source_ids = [meta[:group_id], proof['id'], *Array(proof['host_lineage_ids']), *Array(proof['source_entity_ids']), *Array(proof['source_handles'])].compact.map(&:to_s)
          spatial = bounds && xy_intersects?(meta[:bounds_mm], bounds)
          family_bounds = original_family_bounds(proof)
          wall_family = query['wall_layer'] && meta[:tag] == query['wall_layer'] && family_bounds && bounds && xy_intersects?(family_bounds, bounds)
          spatial || !(source_ids & identifiers).empty? || explicit.include?(meta[:entity_id]) ||
            (query['include_selected'] && selected.include?(meta[:entity_id])) || wall_family
        end
        families = seeds.map { |r| local_family_id(r[:provenance]) }.compact.uniq
        seeds |= rows.select { |r| r[:kind] == :leaf }.select { |r| families.include?(local_family_id(r[:provenance])) }
        if seeds.empty? && bounds.nil?
          raise RepairError.new('LOCAL_SCOPE_EMPTY', 'No CAD bounds or source-leaf targets define a local read.')
        end
        region = seeds.reduce(bounds) { |area, row| xy_union(area, row[:meta][:bounds_mm]) }
        unless region
          raise RepairError.new('LOCAL_SCOPE_EMPTY', 'A bounded local read region is required.')
        end
        included = rows.reject { |r| r[:kind] == :reference }.select { |r| xy_intersects?(r[:meta][:bounds_mm], region) }
        leaves, protected = [], []
        included.each do |row|
          if row[:kind] == :protected
            protected << protected_summary(row[:group], row[:world], row[:meta][:parent_entity_id], row[:provenance])
          else
            leaves << leaf_summary(row[:group], row[:world], row[:meta][:parent_entity_id])
          end
        end
        result = snapshot_summary(model, root, containers, leaves, [], protected)
        result[:local_read] = {
          schema: 'dwg-skp-local-read-v1', query: query, bounds_xy_mm: region,
          inventory_sha256: Digest::SHA256.hexdigest(JSON.generate(canonical(census.sort_by { |r| r[:entity_id] }))),
          entity_ids: leaves.map { |r| r[:entity_id] }.sort,
          protected_entity_ids: protected.map { |r| r[:entity_id] }.sort
        }
        result[:read_stats] = {inventory_groups: census.length, geometry_groups: included.length,
                              reference_groups: rows.count { |r| r[:kind] == :reference },
                              local_entities: leaves.length, local_protected_entities: protected.length,
                              geometry_scope: 'local', inventory_mode: 'fresh_hierarchy_bounds_and_revisions'}
        result
      end

      def validate_local_query(query)
        unless query.is_a?(Hash) && query['schema'] == 'dwg-skp-local-query-v1' &&
               query['source_identifiers'].is_a?(Array) && query['target_entity_ids'].is_a?(Array) &&
               [true, false].include?(query['include_selected']) &&
               (query['wall_layer'].nil? || query['wall_layer'].is_a?(String))
          raise RepairError.new('INVALID_LOCAL_QUERY', 'A complete bounded local query is required.')
        end
        unless (query['source_identifiers'] + query['target_entity_ids']).all? { |v| v.is_a?(String) && !v.empty? }
          raise RepairError.new('INVALID_LOCAL_QUERY', 'Source and target identifiers must be nonempty strings.')
        end
        bounds = query['bounds_xy_mm']
        if bounds
          unless bounds.is_a?(Array) && bounds.length == 4
            raise RepairError.new('INVALID_LOCAL_QUERY', 'Local bounds must be four finite XY coordinates.')
          end
          bounds = bounds.map { |v| finite_float(v, 'local XY bound') }
          unless bounds[0] < bounds[2] && bounds[1] < bounds[3]
            raise RepairError.new('INVALID_LOCAL_QUERY', 'Local XY bounds must enclose a nonempty area.')
          end
        end
        {'schema'=>'dwg-skp-local-query-v1', 'bounds_xy_mm'=>bounds,
         'source_identifiers'=>query['source_identifiers'].uniq.sort,
         'target_entity_ids'=>query['target_entity_ids'].uniq.sort,
         'include_selected'=>query['include_selected'], 'wall_layer'=>query['wall_layer']}
      end

      def collect_local_inventory(entities, parent_world, parent_id, containers, rows, census)
        entities.grep(Sketchup::Group).each do |group|
          world = parent_world * group.transformation
          group_id = group.get_attribute(DICTIONARY, 'id')
          proof = parse_json_attribute(group, 'provenance') || {}
          reference = proof['mode'] == 'reference_edges' || proof['category'] == 'reference'
          meta = inventory_row(group, world, parent_world, parent_id, reference)
          census << meta
          if group_id && !group_id.to_s.empty?
            kind = if reference then :reference
                   elsif !SUPPORTED_CATEGORIES.include?(proof['category']) || proof['mode'] == 'boundary_mesh' then :protected
                   else :leaf end
            rows << {group: group, world: world, meta: meta, provenance: proof, kind: kind}
          else
            containers << {entity_id: stable_id(group), parent_entity_id: parent_id,
                           name: group.name.to_s, world_transform: transform_summary(world), locked: group.locked?}
            collect_local_inventory(group.entities, world, stable_id(group), containers, rows, census)
          end
        end
      end

      def inventory_row(group, world, parent_world, parent_id, reference)
        definition_guid = group.respond_to?(:definition) && group.definition.respond_to?(:guid) ? group.definition.guid.to_s : ''
        if definition_guid.empty? && !reference
          raise RepairError.new('LOCAL_REVISION_UNAVAILABLE', 'A source group lacks an actual definition revision; local reads cannot be proved.', entity_id: stable_id(group))
        end
        bounds = nil
        # Reference leaves are never material dependencies or spatial seeds.
        # Their live transforms/revisions/metadata remain in the census, while
        # asking thousands of 2D reference groups for bounds would read unused
        # geometry and defeat the bounded material inspector.
        # Container aggregate boxes are not query candidates. Reading them
        # would ask SketchUp to aggregate every descendant reference again;
        # all material/protected leaves have their own fresh world boxes.
        if parent_world && !reference && !group.get_attribute(DICTIONARY, 'id').to_s.empty?
          box = group.bounds
          if !box.respond_to?(:valid?) || box.valid?
            points = (0..7).map { |i| p=parent_world*box.corner(i); [p.x,p.y,p.z].map { |v| finite_float(v.to_f*25.4, 'world inventory bound') } }
            bounds = [points.map { |p| p[0] }.min, points.map { |p| p[1] }.min, points.map { |p| p[2] }.min,
                      points.map { |p| p[0] }.max, points.map { |p| p[1] }.max, points.map { |p| p[2] }.max]
          end
          unless bounds || reference || group.get_attribute(DICTIONARY, 'id').to_s.empty?
            raise RepairError.new('LOCAL_BOUNDS_UNAVAILABLE', 'A source group has no actual world bounds.', entity_id: stable_id(group))
          end
        end
        {entity_id: stable_id(group), parent_entity_id: parent_id, group_id: group.get_attribute(DICTIONARY, 'id').to_s,
         name: group.name.to_s, tag: group.layer ? group.layer.name.to_s : 'Untagged',
         material: group.material ? group.material.name.to_s : nil,
         locked: group.locked?, hidden: group.hidden?, world_transform: transform_summary(world),
         bounds_mm: bounds, definition_guid: definition_guid, entity_count: group.entities.length,
         provenance_sha256: Digest::SHA256.hexdigest(group.get_attribute(DICTIONARY, 'provenance').to_s)}
      end

      def local_family_id(proof)
        proof['wall_system_family_id'] || (proof['material_partition_evidence'] || {})['source_candidate_id'] || proof['automatic_recognition_id']
      end

      def original_family_bounds(proof)
        footprint = proof['wall_system_match_footprint'] || (proof['material_partition_evidence'] || {})['original_footprint']
        return nil unless footprint.is_a?(Hash) && footprint['outer'].is_a?(Array) && footprint['outer'].length >= 3
        points = footprint['outer'].map { |p| [finite_float(p[0], 'family X'), finite_float(p[1], 'family Y')] }
        [points.map(&:first).min, points.map(&:last).min, points.map(&:first).max, points.map(&:last).max]
      end

      def xy_bounds(box)
        box.length == 6 ? [box[0], box[1], box[3], box[4]] : box
      end

      def xy_intersects?(box, area)
        return false unless box && area
        a, b = xy_bounds(box), xy_bounds(area)
        a[0] <= b[2] + 0.02 && a[2] >= b[0] - 0.02 && a[1] <= b[3] + 0.02 && a[3] >= b[1] - 0.02
      end

      def xy_union(area, box)
        return area unless box
        b = xy_bounds(box)
        return b.dup unless area
        [ [area[0], b[0]].min, [area[1], b[1]].min, [area[2], b[2]].max, [area[3], b[3]].max ]
      end

      def validate_local_coverage!(plan, snapshot)
        coverage = snapshot[:local_read]
        return unless coverage
        bounds = coverage[:bounds_xy_mm]
        unless Array(plan['targets']).all? { |r| coverage[:entity_ids].include?(r['entity_id']) }
          raise RepairError.new('LOCAL_COVERAGE_INCOMPLETE', 'A target falls outside the proven local read.')
        end
        Array(plan['create']).each do |row|
          points = row.dig('mesh', 'vertices')
          unless points.is_a?(Array) && !points.empty? && points.all? { |p| p.is_a?(Array) && p.length >= 2 &&
                   (x=finite_float(p[0], 'create X')) >= bounds[0]-0.02 && x <= bounds[2]+0.02 &&
                   (y=finite_float(p[1], 'create Y')) >= bounds[1]-0.02 && y <= bounds[3]+0.02 }
            raise RepairError.new('LOCAL_COVERAGE_INCOMPLETE', 'Replacement geometry exceeds the proven local dependency read.')
          end
        end
      end

      def leaf_summary(group, world, parent_id)
        if group.entities.any? { |item| item.is_a?(Sketchup::Group) || (defined?(Sketchup::ComponentInstance) && item.is_a?(Sketchup::ComponentInstance)) }
          raise RepairError.new('NESTED_TARGET_GEOMETRY', 'A repairable source leaf cannot contain arbitrary nested instances.')
        end
        provenance = parse_json_attribute(group, 'provenance') || {}
        points = world_face_points(group, world)
        raise RepairError.new('EMPTY_TARGET_GEOMETRY', 'A white-model leaf group has no faces.', entity_id: stable_id(group)) if points.empty?
        z_values = points.flatten(1).map { |point| point[2] }
        low, high = z_values.min, z_values.max
        bottom_xy = points.flatten(1).select { |p| (p[2]-low).abs < 1.0e-4 }.map { |p| p[0,2].map { |v| v.round(6) } }.uniq.sort
        top_xy = points.flatten(1).select { |p| (p[2]-high).abs < 1.0e-4 }.map { |p| p[0,2].map { |v| v.round(6) } }.uniq.sort
        prism = group.manifold? && high-low > 1.0e-4 && bottom_xy == top_xy &&
          z_values.all? { |z| (z-low).abs < 1.0e-4 || (z-high).abs < 1.0e-4 }
        points.each do |face|
          next if face.all? { |p| (p[2]-face.first[2]).abs < 1.0e-4 }
          xy = face.map { |p| p[0,2] }.uniq
          a,b = xy[0],xy[1]
          prism &&= xy.length >= 2 && xy.all? { |p| ((b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0])).abs < 1.0e-4 }
        end
        raise RepairError.new('UNSUPPORTED_EDITED_GEOMETRY', 'Actual leaf is not an unchanged horizontal-footprint vertical prism.') unless prism
        footprint = exact_or_stored_footprint(group, world, provenance, z_values.min)
        category = provenance['category'].to_s
        category = parent_name(group) if category.empty?
        {
          entity_id: stable_id(group), parent_entity_id: parent_id,
          group_id: group.get_attribute(DICTIONARY, 'id').to_s,
          name: group.name.to_s, category: category,
          tag: group.layer ? group.layer.name.to_s : 'Untagged',
          material: group.material ? group.material.name.to_s : nil,
          hidden: group.hidden?, locked: group.locked?, manifold: group.manifold?,
          volume_mm3: safe_volume_mm3(group, world),
          bottom_triangles_mm: bottom_triangles_mm(group, world, low),
          base_z_mm: z_values.min, height_mm: z_values.max - z_values.min,
          footprint: footprint, signature: entity_signature(group, world),
          provenance: provenance, world_transform: transform_summary(world),
          raw_geometry_tags: group.entities.select { |item| item.is_a?(Sketchup::Face) || item.is_a?(Sketchup::Edge) }
                                  .map { |item| item.layer.name.to_s }.uniq.sort
        }
      end

      def parent_name(group)
        group.parent.respond_to?(:instances) ? group.parent.name.to_s : ''
      rescue StandardError
        ''
      end

      def exact_or_stored_footprint(group, world, provenance, minimum_z)
        tolerance = 1.0e-4
        bottom_faces = group.entities.grep(Sketchup::Face).select do |face|
          face.vertices.all? do |vertex|
            ((world * vertex.position).z.to_f * 25.4 - minimum_z).abs <= tolerance
          end
        end
        if bottom_faces.length == 1
          face = bottom_faces.first
          outer = nil
          holes = []
          face.loops.each do |loop|
            points = loop.vertices.map do |vertex|
              point = world * vertex.position
              [point.x.to_f * 25.4, point.y.to_f * 25.4]
            end
            if loop.outer?
              outer = points
            else
              holes << points
            end
          end
          return { 'outer' => outer, 'holes' => holes } if outer && outer.length >= 3
        end
        # Generated live meshes have triangulated bottom caps. Reconstruct the
        # boundary from their actual loops, never stale provenance metadata.
        boundary = {}
        bottom_faces.each do |face|
          face.loops.each do |loop|
            ring = loop.vertices.map do |vertex|
              point = world * vertex.position
              [point.x.to_f * 25.4, point.y.to_f * 25.4].map { |v| v.round(6) }
            end
            ring.each_with_index do |a, i|
              b = ring[(i+1)%ring.length]
              key = [a,b].sort
              boundary[key] = (boundary[key] || 0) + 1
            end
          end
        end
        edges = boundary.select { |_edge, count| count == 1 }.keys
        rings = []
        until edges.empty?
          edge = edges.shift
          ring = edge.dup
          until ring.last == ring.first
            matches = edges.each_index.select { |i| edges[i].include?(ring.last) }
            raise RepairError.new('FOOTPRINT_UNAVAILABLE', 'Actual bottom boundary is not a simple closed loop.') unless matches.length == 1
            following = edges.delete_at(matches.first)
            ring << (following[0] == ring.last ? following[1] : following[0])
          end
          rings << ring[0...-1]
        end
        raise RepairError.new('FOOTPRINT_UNAVAILABLE', 'No actual horizontal bottom boundary.') if rings.empty?
        area = lambda { |ring| ring.each_with_index.sum { |a,i| b=ring[(i+1)%ring.length]; a[0]*b[1]-b[0]*a[1] }.abs/2.0 }
        outer = rings.max_by { |ring| area.call(ring) }
        holes = rings.reject { |ring| ring.equal?(outer) }
        # Connected manifold + matching actual prism volume is also checked by
        # Python. Multiple disjoint outer loops cannot become an edited prism.
        { 'outer' => outer, 'holes' => holes }
      end

      def bottom_triangles_mm(group, world, minimum_z)
        group.entities.grep(Sketchup::Face).flat_map do |face|
          next [] unless face.vertices.all? { |vertex| ((world * vertex.position).z.to_f * 25.4 - minimum_z).abs <= 1.0e-4 }
          mesh = face.mesh(0)
          mesh.polygons.flat_map do |indices|
            points = indices.map { |i| p = world * mesh.point_at(i.abs); [p.x.to_f*25.4, p.y.to_f*25.4] }
            (1...points.length-1).map { |i| [points[0], points[i], points[i+1]] }
          end
        end
      end

      def world_face_points(group, world)
        group.entities.grep(Sketchup::Face).map do |face|
          face.vertices.map do |vertex|
            point = world * vertex.position
            [point.x.to_f * 25.4, point.y.to_f * 25.4, point.z.to_f * 25.4]
          end
        end
      end

      def entity_signature(group, world)
        faces = world_face_points(group, world).map do |face|
          face.map { |point| point.map { |value| value.round(6) } }
        end
        faces = faces.sort_by { |face| JSON.generate(face) }
        payload = {
          id: group.get_attribute(DICTIONARY, 'id').to_s,
          name: group.name.to_s,
          tag: group.layer ? group.layer.name.to_s : 'Untagged',
          material: group.material ? group.material.name.to_s : nil,
          faces: faces
        }
        Digest::SHA256.hexdigest(JSON.generate(canonical(payload)))
      end

      def apply_live_repair(params)
        model = model!
        plan = params['plan']
        confirmation = params['confirmation_sha256'].to_s
        unless plan.is_a?(Hash) && plan['schema'] == 'dwg-skp-live-repair-plan-v1'
          raise RepairError.new('INVALID_PLAN', 'A live-repair plan is required.')
        end
        digest = validate_plan_digest(plan)
        unless digest == confirmation
          raise RepairError.new('CONFIRMATION_MISMATCH', 'Plan confirmation hash does not match.')
        end
        validate_plan_contract!(plan)
        validate_model_binding(model, plan['model'])
        local_read = plan.dig('model', 'local_read')
        global_read = plan.dig('model', 'global_read')
        snapshot = if global_read
                     inspect_white_model_global
                   elsif local_read
                     inspect_white_model_local(local_read['query'])
                   else
                     inspect_white_model
                   end
        if global_read && canonical(snapshot[:global_read]) != global_read
          raise RepairError.new('MODEL_CONTEXT_CHANGED', 'Whole-model geometry, hierarchy or reference geometry changed after comparison.')
        end
        validate_whole_cad_observation!(plan, params['current_cad_model']) if global_read
        if local_read && canonical(snapshot[:local_read]) != local_read
          raise RepairError.new('MODEL_CONTEXT_CHANGED', 'Local membership, world bounds or model definition revisions changed after planning.')
        end
        validate_local_coverage!(plan, snapshot)
        actual_context = canonical({root: snapshot[:root], containers: snapshot[:containers], entities: snapshot[:entities],
                                    protected_entities: snapshot[:protected_entities] || []})
        unless actual_context == plan.dig('model', 'snapshot_context')
          raise RepairError.new('MODEL_CONTEXT_CHANGED', 'Source binding, transforms, geometry or provenance changed after planning.')
        end
        validate_fixed_live_standard!(plan, snapshot)
        slab_enabled = global_read && plan['slab_sync_version'] == '1.0'
        categories = slab_enabled ? (SUPPORTED_CATEGORIES + ['slab']) : SUPPORTED_CATEGORIES
        repairable = snapshot[:entities] + (slab_enabled ? snapshot[:protected_entities].select { |r| r[:category] == 'slab' } : [])
        leaf_index = repairable.each_with_object({}) { |row, result| result[row[:entity_id]] = row }
        container_index = snapshot[:containers].each_with_object({}) { |row, result| result[row[:entity_id]] = row }
        root_id = snapshot[:root][:entity_id]
        target_rows = plan['targets'] || []
        create_rows = plan['create'] || []
        targets = target_rows.map do |expected|
          actual = leaf_index[expected['entity_id']]
          unless actual && actual[:group_id] == expected['group_id'] && actual[:signature] == expected['signature'] &&
                 actual[:parent_entity_id] == expected['parent_entity_id'] && categories.include?(actual[:category])
            raise RepairError.new(
              'TARGET_CHANGED',
              'A target changed after planning; inspect and plan again.',
              entity_id: expected['entity_id']
            )
          end
          raise RepairError.new('TARGET_LOCKED', 'A target is locked.', entity_id: expected['entity_id']) if actual[:locked]
          if actual[:category] == 'slab' && (actual[:nested_geometry] || !%w[floor_slab overhead_slab].include?(actual.dig(:provenance,'slab_role')))
            raise RepairError.new('UNSUPPORTED_SLAB_TARGET', 'Only a source-bound floor/overhead slab leaf may be synchronized.')
          end
          entity = resolve_entity(model, expected['entity_id'])
          unless entity.is_a?(Sketchup::Group)
            raise RepairError.new('UNSUPPORTED_TARGET', 'Only source-bound white-model groups may be repaired.')
          end
          entity
        end
        new_containers = global_read ? (plan['create_containers'] || []) : []
        required_parents = create_rows.map { |r| r['parent_entity_id'] }.select { |id| id.to_s.start_with?('new-category:') }.uniq.sort
        unless new_containers.is_a?(Array) && new_containers.all? { |r| r.is_a?(Hash) } &&
               new_containers.map { |r| r['entity_id'] }.sort == required_parents &&
               new_containers.map { |r| r['name'] }.uniq.length == new_containers.length
          raise RepairError.new('INVALID_PLAN_CONTRACT', 'New category containers must exactly match replacement parents.')
        end
        if global_read && targets.empty? && create_rows.empty?
          return {schema: 'dwg-skp-live-repair-receipt-v1', status: 'unchanged', action: plan['action'],
                  plan_sha256: digest, model_guid: model.guid, model_path: model.path.to_s,
                  removed: [], created: [], undo_operation: nil, model_saved: false, new_skp_created: false,
                  verified: true, snapshot_mode: 'whole_model', read_stats: snapshot[:read_stats]}
        end
        allowed_parents = (target_rows.map { |row| row['parent_entity_id'] } + container_index.keys).uniq
        new_containers.each do |row|
          unless categories.include?(row['name']) && row['entity_id'] == "new-category:#{row['name']}" &&
                 row['parent_entity_id'] == root_id && !container_index.values.any? { |old| old[:name] == row['name'] }
            raise RepairError.new('PARENT_NOT_ALLOWED', 'New container must be an absent supported category under the bound root.')
          end
          allowed_parents << row['entity_id']
          container_index[row['entity_id']] = {name: row['name']}
        end
        create_rows.each do |row|
          unless categories.include?(row['category'].to_s)
            raise RepairError.new('UNSUPPORTED_CATEGORY', 'Replacement category is not supported.', category: row['category'])
          end
          unless allowed_parents.include?(row['parent_entity_id'])
            raise RepairError.new('PARENT_NOT_ALLOWED', 'Replacement parent is outside the white-model containers.')
          end
          container = container_index[row['parent_entity_id']]
          unless container && container[:name] == row['category']
            raise RepairError.new('PARENT_CATEGORY_MISMATCH', 'Replacement category must match its actual source-model container.')
          end
          validate_mesh(row['mesh'])
        end

        root = resolve_entity(model, root_id)
        hierarchy = hierarchy_index(root)
        parent_entities = {}
        create_rows.each do |row|
          next if row['parent_entity_id'].start_with?('new-category:') && new_containers.any? { |c| c['entity_id'] == row['parent_entity_id'] }
          parent = resolve_entity(model, row['parent_entity_id'])
          unless parent.is_a?(Sketchup::Group) && hierarchy.key?(stable_id(parent))
            raise RepairError.new('PARENT_NOT_FOUND', 'Replacement parent is not in the active white-model root.')
          end
          raise RepairError.new('PARENT_LOCKED', 'Replacement parent is locked.') if parent.locked?
          parent_entities[row['parent_entity_id']] = parent
        end

        operation_started = false
        created = []
        before = target_rows.map(&:dup)
        begin
          model.start_operation("DWG live repair: #{plan['action']}", true)
          operation_started = true
          new_containers.each do |row|
            group = root.entities.add_group
            group.name = row['name']
            parent_entities[row['entity_id']] = group
            hierarchy[stable_id(group)] = root.transformation * group.transformation
          end
          targets.each(&:erase!)
          create_rows.each do |row|
            parent = parent_entities.fetch(row['parent_entity_id'])
            parent_world = hierarchy.fetch(stable_id(parent))
            created << create_group(model, parent, parent_world, row, digest)
          end
          created_hierarchy = hierarchy_index(root)
          verify_created!(created, create_rows, created_hierarchy, parent_entities)
          journal = append_journal(root, plan, created, digest)
          model.selection.clear
          created.each { |group| model.selection.add(group) }
          model.commit_operation
          operation_started = false
          {
            schema: 'dwg-skp-live-repair-receipt-v1', status: 'applied',
            action: plan['action'], plan_sha256: digest,
            model_guid: model.guid, model_path: model.path.to_s,
            removed: before,
            created: created.map do |group|
              {
                entity_id: stable_id(group), group_id: group.get_attribute(DICTIONARY, 'id'),
                name: group.name.to_s, manifold: group.manifold?, volume_mm3: safe_volume_mm3(group, created_hierarchy.fetch(stable_id(group)))
              }
            end,
            undo_operation: "DWG live repair: #{plan['action']}",
            model_saved: false, new_skp_created: false,
            root_live_geometry_state: journal['plan_sha256'], verified: true,
            **(local_read ? {snapshot_mode: 'local_geometry', read_stats: snapshot[:read_stats]} : {}),
            **(global_read ? {snapshot_mode: 'whole_model', read_stats: snapshot[:read_stats]} : {})
          }
        rescue StandardError => error
          model.abort_operation if operation_started
          raise error if error.is_a?(RepairError)
          raise RepairError.new('WRITE_FAILED', 'Live repair was aborted.', exception: error.class.name, detail: error.message)
        end
      end

      def create_group(model, parent, parent_world, row, digest)
        group = parent.entities.add_group
        group.name = row['name'].to_s
        layer_name = row['tag'].to_s
        group.layer = model.layers[layer_name] || model.layers.add(layer_name)
        material_name = row['material'].to_s
        group.material = model.materials[material_name] unless material_name.empty? || model.materials[material_name].nil?
        inverse = parent_world.inverse
        vertices = row.fetch('mesh').fetch('vertices')
        row.fetch('mesh').fetch('faces').each do |indices|
          points = indices.map do |index|
            values = vertices.fetch(Integer(index))
            world_point = Geom::Point3d.new(values.map { |value| finite_float(value, 'mesh vertex') / 25.4 })
            inverse * world_point
          end
          face = group.entities.add_face(points)
          raise RepairError.new('FACE_CREATION_FAILED', 'SketchUp rejected a replacement mesh face.') if face.nil?
          if row['category'] == 'slab'
            # SketchUp may orient a newly added face from its neighbours or the
            # ground plane. Preserve the reviewed shell's outward winding.
            wanted = (points[1] - points[0]).cross(points[2] - points[0])
            face.reverse! if face.normal.dot(wanted) < 0
          end
        end
        raw_layer = model.layers[0]
        group.entities.each do |item|
          item.layer = raw_layer if item.is_a?(Sketchup::Face) || item.is_a?(Sketchup::Edge)
        end
        group.set_attribute(DICTIONARY, 'id', row['group_id'].to_s)
        group.set_attribute(DICTIONARY, 'provenance', JSON.generate(row['provenance'] || {}))
        group.set_attribute(DICTIONARY, 'payload_sha256', "live-repair:#{digest}")
        group.set_attribute(DICTIONARY, 'live_repair_plan_sha256', digest)
        group.set_attribute(DICTIONARY, 'live_footprint', JSON.generate(row['footprint']))
        group
      end

      def triangle_area_xy(points)
        return 0.0 if points.length < 3
        origin = points.first
        points.each_with_index.sum do |a,i|
          b=points[(i+1)%points.length]
          (a[0]-origin[0])*(b[1]-origin[1])-(b[0]-origin[0])*(a[1]-origin[1])
        end.abs/2.0
      end

      def clipped_triangle_area(subject, clip)
        # Convex triangle clipping operates on actual mesh triangles only;
        # no stored slab footprint or sampled point may stand in for coverage.
        orientation = (clip[1][0]-clip[0][0])*(clip[2][1]-clip[0][1])-(clip[1][1]-clip[0][1])*(clip[2][0]-clip[0][0])
        clip=clip.reverse if orientation < 0
        output=subject.map { |p|p[0,2] }
        clip.each_with_index do |a,i|
          b=clip[(i+1)%clip.length]
          input=output;output=[]
          break if input.empty?
          previous=input.last
          distance=lambda { |p| (b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0]) }
          input.each do |current|
            first=distance.call(previous);second=distance.call(current)
            first_in=first >= -1.0e-7;second_in=second >= -1.0e-7
            if first_in != second_in
              t=first/(first-second)
              output << [previous[0]+t*(current[0]-previous[0]),previous[1]+t*(current[1]-previous[1])]
            end
            output << current if second_in
            previous=current
          end
        end
        triangle_area_xy(output)
      end

      def validate_fixed_live_standard!(plan, snapshot)
        root=snapshot[:root]
        marker=root[:modeling_standard]
        audit=root[:fixed_standard_audit]
        if marker.nil? && audit.nil?
          unless plan['modeling_standard'].nil? && plan['fixed_standard_audit'].nil?
            raise RepairError.new('FIXED_STANDARD_MISMATCH', 'A historical model cannot acquire an unbound policy in a live plan.')
          end
          return
        end
        unless marker == FIXED_MODELING_STANDARD && audit.is_a?(Hash) && audit['standard'] == marker &&
               audit['all_pass'] == true && (!audit['issues'] || audit['issues'].empty?) &&
               plan['modeling_standard'] == marker && plan['fixed_standard_audit'] == audit
          raise RepairError.new('FIXED_STANDARD_MISMATCH', 'The saved fixed standard or its audit is missing, altered or downgraded.')
        end
        return unless %w[add_missing replace_from_cad synchronize_model].include?(plan['action'])
        fire_ids=audit['fire_body_source_entity_ids'] || []
        roofs=(snapshot[:protected_entities] || []).select do |row|
          provenance=row[:provenance] || {}
          row[:category] == 'slab' && ((provenance['slab_role'] || provenance['semantic_role']) == 'overhead_slab' ||
                                     provenance['host_slab_role'] == 'overhead_slab')
        end
        if plan['action'] == 'synchronize_model' && plan['slab_sync_version'] == '1.0'
          removed=(plan['targets'] || []).map { |row| row['entity_id'] }
          roofs.reject! { |row| removed.include?(row[:entity_id]) }
          (plan['create'] || []).each do |row|
            next unless row['category'] == 'slab' && row.dig('provenance','slab_role') == 'overhead_slab'
            mesh=row['mesh']
            triangles=mesh['faces'].flat_map { |face| (1...face.length-1).map { |i| [face[0],face[i],face[i+1]].map { |n| mesh['vertices'][n] } } }
            roofs << {category: 'slab', manifold: true, triangles_mm: triangles, provenance: row['provenance']}
          end
        end
        horizontal=[]
        roofs.each do |roof|
          unless roof[:manifold] == true && roof[:triangles_mm].is_a?(Array) && !roof[:triangles_mm].empty?
            raise RepairError.new('FIXED_ROOF_UNPROVEN', 'Actual closed overhead mesh is required for a fixed-standard repair.')
          end
          roof[:triangles_mm].each do |triangle|
            unless triangle.length == 3 && triangle.all? { |point|point.is_a?(Array) && point.length == 3 }
              raise RepairError.new('FIXED_ROOF_UNPROVEN', 'Actual roof mesh is not a triangle mesh.')
            end
            zs=triangle.map { |p| finite_float(p[2], 'actual roof Z') }
            next if zs.max-zs.min > 1.0e-6 || triangle_area_xy(triangle) <= 0.1
            horizontal << [zs.sum/3.0, triangle]
          end
        end
        (plan['create'] || []).each do |row|
          next if row['category'] == 'slab' && plan['action'] == 'synchronize_model' && plan['slab_sync_version'] == '1.0'
          provenance=row['provenance'] || {}
          replaced=provenance['live_repair_replaced_group_ids'] || []
          actual_targets=(snapshot[:entities] || []).select do |entity|
            (plan['targets'] || []).any? { |target|target['entity_id'] == entity[:entity_id] } &&
              (plan['action'] != 'synchronize_model' || replaced.include?(entity[:group_id]))
          end
          sills=actual_targets.select { |entity|(entity[:provenance] || {})['opening_part_role'] == 'sill' }
          claims_sill=provenance['opening_part_role'] == 'sill'
          if claims_sill || !sills.empty?
            original=sills.first
            opening_ids=original && (original[:provenance] || {})['opening_ids']
            unless %w[replace_from_cad synchronize_model].include?(plan['action']) && actual_targets.length == 1 && sills.length == 1 &&
                   replaced == [original[:group_id]] && claims_sill && original[:category] == row['category'] &&
                   opening_ids.is_a?(Array) && !opening_ids.empty? && provenance['opening_ids'] == opening_ids
              raise RepairError.new('FIXED_SILL_SCOPE', 'Only the unique actual source-bound sill target may preserve an opening-bottom cap.')
            end
            unless ((provenance['source_entity_ids'] || []) & fire_ids).empty?
              raise RepairError.new('FIXED_FIRE_BODY_FORBIDDEN', 'A sill role cannot authorize a fire-door or shutter body.')
            end
            base=finite_float(original[:base_z_mm], 'actual sill base')
            height=finite_float(original[:height_mm], 'actual sill height')
            vertices=row.fetch('mesh').fetch('vertices')
            zs=vertices.map { |point|finite_float(point[2], 'replacement sill mesh Z') }
            unless (finite_float(row['base_z_mm'], 'replacement sill base')-base).abs <= 1.0e-6 &&
                   (finite_float(row['height_mm'], 'replacement sill height')-height).abs <= 1.0e-6 &&
                   (zs.min-base).abs <= 1.0e-6 && (zs.max-base-height).abs <= 1.0e-6
              raise RepairError.new('FIXED_SILL_CAP_CHANGED', 'Replacement sill must preserve its actual base and opening-bottom top.')
            end
            next
          end
          lintels=(snapshot[:entities] || []).select do |entity|
            replaced.include?(entity[:group_id]) && entity[:category] == row['category'] &&
              (entity[:provenance] || {})['semantic_role'] == 'door_lintel'
          end
          trusted_lintel=plan['action'] == 'replace_from_cad' && replaced.length == 1 && lintels.length == 1 &&
            provenance['semantic_role'] == 'door_lintel' && (plan['targets'] || []).length == 1
          unless ((provenance['source_entity_ids'] || []) & fire_ids).empty? || trusted_lintel
            raise RepairError.new('FIXED_FIRE_BODY_FORBIDDEN', 'Fire-door and rolling-shutter bodies remain excluded; cut their openings instead.')
          end
          subrole=provenance['semantic_subrole'].to_s.downcase
          next if row['category'] == 'curtain_accessory' && subrole.start_with?('railing','欄杆','栏杆')
          base=finite_float(row['base_z_mm'], 'replacement base')
          top=base+finite_float(row['height_mm'], 'replacement height')
          vertices=row.fetch('mesh').fetch('vertices')
          caps=row.fetch('mesh').fetch('faces').select do |indices|
            indices.length == 3 && indices.all? { |index|(vertices[index][2]-base).abs <= 1.0e-6 }
          end.map { |indices|indices.map { |index|vertices[index] } }
          area=caps.sum { |triangle|triangle_area_xy(triangle) }
          raise RepairError.new('FIXED_CAP_UNPROVEN', 'No actual replacement bottom triangulation.') if area <= 0.1 || top <= base
          coverage=0.0
          caps.each do |subject|
            horizontal.each do |cap,triangle|
              next if cap > top+1.0e-5
              intersection=clipped_triangle_area(subject,triangle)
              next if intersection <= 1.0e-6
              if cap < top-1.0e-5
                raise RepairError.new('FIXED_CAP_MISMATCH', 'Replacement does not end at the lowest actual local overhead underside.')
              end
              coverage+=intersection
            end
          end
          unless (coverage-area).abs <= [0.1,area*1.0e-8].max
            raise RepairError.new('FIXED_CAP_UNCOVERED', 'Actual local roof mesh does not completely cover the replacement footprint.')
          end
        end
      end

      def verify_created!(created, rows, hierarchy, parent_entities = {})
        unless created.length == rows.length && created.all?(&:valid?)
          raise RepairError.new('READBACK_MISMATCH', 'Replacement group count or validity does not match.')
        end
        created.zip(rows).each do |group, row|
          unless group.manifold?
            raise RepairError.new('NON_MANIFOLD_RESULT', 'A replacement is not a closed SketchUp solid.', group_id: row['group_id'])
          end
          expected = finite_float(row.dig('mesh', 'expected_volume_mm3'), 'expected volume')
          world = hierarchy.fetch(stable_id(group))
          actual = safe_volume_mm3(group, world)
          tolerance = [1.0, expected.abs * 0.001].max
          unless actual && (actual - expected).abs <= tolerance
            raise RepairError.new(
              'VOLUME_MISMATCH', 'Replacement solid volume does not match the plan.',
              group_id: row['group_id'], expected_mm3: expected, actual_mm3: actual
            )
          end
          unless group.get_attribute(DICTIONARY, 'id').to_s == row['group_id'].to_s
            raise RepairError.new('METADATA_MISMATCH', 'Replacement identity did not survive readback.')
          end
          unless group.layer.name.to_s == row['tag'].to_s &&
                 parse_json_attribute(group, 'provenance') == row['provenance'] &&
                 parse_json_attribute(group, 'live_footprint') == row['footprint'] &&
                 (parent_entities[row['parent_entity_id']] || resolve_entity(model!, row['parent_entity_id'])).entities.include?(group)
            raise RepairError.new('METADATA_MISMATCH', 'Replacement Tag, parent or source provenance differs from the plan.')
          end
          actual_material = group.material ? group.material.name.to_s : nil
          unless actual_material == row['material']
            raise RepairError.new('MATERIAL_MISMATCH', 'Replacement material differs from the plan.')
          end
          raw = group.entities.select { |item| item.is_a?(Sketchup::Face) || item.is_a?(Sketchup::Edge) }
          unless raw.all? { |item| %w[Untagged Layer0].include?(item.layer.name.to_s) }
            raise RepairError.new('RAW_TAG_MISMATCH', 'Raw edges and faces must be Untagged.')
          end
          actual_points = world_face_points(group, world).flatten(1).map { |point| point.map { |v| v.round(6) } }.uniq.sort
          expected_points = row.fetch('mesh').fetch('vertices').map { |point| point.map { |v| v.round(6) } }.uniq.sort
          unless actual_points == expected_points
            raise RepairError.new('WORLD_GEOMETRY_MISMATCH', 'Replacement world coordinates differ from the approved mesh.')
          end
          actual_faces = group.entities.grep(Sketchup::Face).map do |face|
            face.loops.map { |loop| loop.vertices.map { |v| p=world*v.position; [p.x,p.y,p.z].map { |n| n.to_f*25.4 } } }
          end
          expected_faces = row['mesh']['faces'].map { |indices| [indices.map { |i| row['mesh']['vertices'][i] }] }
          unless surface_boundary(actual_faces) == surface_boundary(expected_faces)
            raise RepairError.new('SURFACE_MISMATCH', 'Actual mesh surfaces differ from the approved material boundary.')
          end
        end
      end

      def surface_boundary(faces)
        planes = {}
        faces.each do |loops|
          points = loops.first
          a = points.first
          normal = nil
          points[1..-1].each_cons(2) do |b,c|
            u = 3.times.map { |i| b[i]-a[i] }; v = 3.times.map { |i| c[i]-a[i] }
            n = [u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]]
            length = Math.sqrt(n.sum { |x| x*x })
            if length > 1.0e-9
              normal = n.map { |x| x/length }; break
            end
          end
          raise RepairError.new('INVALID_SURFACE', 'Degenerate actual face.') unless normal
          normal = normal.map { |x| -x } if normal.find { |x| x.abs > 1.0e-9 } < 0
          key = [*normal, 3.times.sum { |i| normal[i]*a[i] }].map { |x| x.round(6) }
          edges = (planes[key] ||= {})
          loops.each do |ring|
            ring.each_with_index do |p,i|
              q = ring[(i+1)%ring.length]
              edge = [p.map { |x| x.round(6) }, q.map { |x| x.round(6) }].sort
              edges[edge] = (edges[edge] || 0) + 1
            end
          end
        end
        planes.transform_values { |edges| edges.select { |_edge,count| count.odd? }.keys.sort }
      end

      def append_journal(root, plan, created, digest)
        previous = parse_json_attribute(root, 'live_repair_journal')
        previous = [] unless previous.is_a?(Array)
        entry = {
          'applied_at_utc' => Time.now.utc.iso8601(6),
          'plan_sha256' => digest,
          'action' => plan['action'],
          'target_entity_ids' => (plan['targets'] || []).map { |row| row['entity_id'] },
          'created_entity_ids' => created.map { |group| stable_id(group) },
          'source' => plan['source']
        }
        journal = (previous + [entry]).last(100)
        root.set_attribute(DICTIONARY, 'live_repair_journal', JSON.generate(journal))
        root.set_attribute(DICTIONARY, 'live_geometry_state', digest)
        root.set_attribute(DICTIONARY, 'payload_dirty_by_live_repair', true)
        entry
      end

      def validate_model_binding(model, expected)
        expected ||= {}
        unless expected['guid'].to_s == model.guid.to_s
          raise RepairError.new('MODEL_CHANGED', 'The active SketchUp model GUID differs from the plan.')
        end
        actual_path = File.expand_path(model.path.to_s).downcase
        expected_path = File.expand_path(expected['path'].to_s).downcase
        unless !actual_path.empty? && actual_path == expected_path
          raise RepairError.new('MODEL_CHANGED', 'The active SketchUp model path differs from the plan.')
        end
        root = white_model_root(model)
        raise RepairError.new('ROOT_LOCKED', 'The source-bound model root is locked.') if root.locked?
        unless stable_id(root) == expected['root_entity_id'].to_s
          raise RepairError.new('MODEL_CHANGED', 'The DWG white-model root differs from the plan.')
        end
        current_payload = root.get_attribute(DICTIONARY, 'payload_sha256')
        if expected['root_payload_sha256'] && current_payload.to_s != expected['root_payload_sha256'].to_s
          raise RepairError.new('MODEL_CHANGED', 'The root payload identity differs from the plan.')
        end
      end

      def hierarchy_index(root)
        result = { stable_id(root) => root.transformation }
        walk = lambda do |entities, parent_world|
          entities.grep(Sketchup::Group).each do |group|
            world = parent_world * group.transformation
            result[stable_id(group)] = world
            walk.call(group.entities, world)
          end
        end
        walk.call(root.entities, root.transformation)
        result
      end

      def validate_mesh(mesh)
        unless mesh.is_a?(Hash) && mesh['coordinate_space'] == 'model_mm'
          raise RepairError.new('INVALID_MESH', 'Replacement mesh must use model_mm coordinates.')
        end
        vertices = mesh['vertices']
        faces = mesh['faces']
        unless vertices.is_a?(Array) && vertices.length >= 4 && faces.is_a?(Array) && faces.length >= 4
          raise RepairError.new('INVALID_MESH', 'Replacement mesh is incomplete.')
        end
        vertices.each { |point| vector3(point, 'mesh vertex') }
        faces.each do |face|
          unless face.is_a?(Array) && face.length >= 3 && face.all? { |index| index.is_a?(Integer) && index >= 0 && index < vertices.length }
            raise RepairError.new('INVALID_MESH', 'Replacement mesh contains invalid face indices.')
          end
        end
        finite_float(mesh['expected_volume_mm3'], 'expected volume')
      end

      def validate_plan_digest(plan)
        expected = plan['plan_sha256'].to_s
        body = plan.reject { |key, _value| %w[plan_sha256 canonical_body_json].include?(key.to_s) }
        encoded = plan['canonical_body_json']
        unless encoded.is_a?(String) && JSON.parse(encoded) == body
          raise RepairError.new('PLAN_DIGEST_MISMATCH', 'Canonical bytes and plan fields differ.')
        end
        actual = Digest::SHA256.hexdigest(encoded)
        unless !expected.empty? && expected == actual
          raise RepairError.new('PLAN_DIGEST_MISMATCH', 'The live-repair plan was changed after planning.')
        end
        actual
      end

      def validate_plan_contract!(plan)
        action = plan['action']
        targets, create = plan['targets'], plan['create']
        policy = {'active_model_only'=>true,'create_new_skp'=>false,'save_model'=>false,
                  'single_undo_operation'=>true,'verify_before_commit'=>true}
        valid = %w[cut_opening add_missing replace_from_cad remove_wrong synchronize_model].include?(action) &&
                %w[ready_to_apply up_to_date].include?(plan['status']) && targets.is_a?(Array) && create.is_a?(Array) &&
                plan['write_policy'] == policy
        valid &&= case action
                  when 'remove_wrong' then !targets.empty? && create.empty?
                  when 'add_missing' then targets.empty? && !create.empty?
                  when 'synchronize_model' then plan.dig('model','global_read','schema') == 'dwg-skp-global-read-v1' &&
                    plan.dig('source','sync_model','schema') == 'dwg-cad-model-observation-v1' &&
                    plan['status'] == ((targets.empty? && create.empty?) ? 'up_to_date' : 'ready_to_apply')
                  else !targets.empty? && !create.empty?
                  end
        unless valid
          raise RepairError.new('INVALID_PLAN_CONTRACT', 'Action, operation shape or write policy is outside the narrow repair contract.')
        end
        manifest = plan.dig('source','manifest')
        expected = plan.dig('model','snapshot_context','root','source_manifest')
        unless manifest.is_a?(Hash) && manifest == expected && plan.dig('source','sha256') == manifest['source_sha256'] &&
               plan.dig('source','path') == manifest['source_path']
          raise RepairError.new('SOURCE_BINDING_MISMATCH', 'Repair source differs from the model source manifest.')
        end
        entries = [{'path'=>manifest['source_path'],'sha256'=>manifest['source_sha256']}]+(manifest['dependencies'] || [])
        entries.each do |entry|
          next if %w[not_used overlay_skipped].include?(entry['status']) && !entry['sha256']
          unless entry['path'] && File.file?(entry['path']) && Digest::SHA256.file(entry['path']).hexdigest == entry['sha256']
            raise RepairError.new('SOURCE_CHANGED', 'DWG or reference changed after planning.')
          end
        end
        ids = create.map { |row| row['group_id'] }
        unless ids.uniq.length == ids.length && ids.all? { |id| id.is_a?(String) && !id.empty? }
          raise RepairError.new('INVALID_PLAN_CONTRACT', 'Replacement identities must be unique.')
        end
        create.each do |row|
          proof = row['provenance']
          footprint = row['footprint']
          unless proof.is_a?(Hash) && footprint.is_a?(Hash) &&
                 proof['id'] == row['group_id'] && proof['category'] == row['category'] && proof['tag'] == row['tag'] &&
                 proof['outer'] == footprint['outer'] && (proof['holes'] || []) == (footprint['holes'] || []) &&
                 proof['base_z_mm'] == row['base_z_mm'] && proof['height_mm'] == row['height_mm'] &&
                 proof['source_entity_ids'].is_a?(Array) && !proof['source_entity_ids'].empty?
            raise RepairError.new('INCOHERENT_PROVENANCE', 'Replacement provenance must bind its actual identity, footprint, source and Z.')
          end
          bottom = finite_float(row['base_z_mm'], 'base Z')
          top = bottom + finite_float(row['height_mm'], 'height')
          raise RepairError.new('INVALID_MESH', 'Replacement height must be positive.') unless top > bottom
          rings = [footprint['outer'], *(footprint['holes'] || [])]
          expected_faces = [rings.map { |ring| ring.map { |p| [p[0],p[1],bottom] } },
                            rings.map { |ring| ring.map { |p| [p[0],p[1],top] } }]
          rings.each do |ring|
            ring.each_with_index do |p,i|
              q=ring[(i+1)%ring.length]
              expected_faces << [[[p[0],p[1],bottom],[q[0],q[1],bottom],[q[0],q[1],top],[p[0],p[1],top]]]
            end
          end
          validate_mesh(row['mesh'])
          actual_faces=row['mesh']['faces'].map { |indices| [indices.map { |i| row['mesh']['vertices'][i] }] }
          if row['category'] == 'slab'
            unless action == 'synchronize_model' && plan['slab_sync_version'] == '1.0' &&
                   proof['mode'] == 'boundary_mesh' && proof['assembly_mode'] == 'whole_uniform_thickness' &&
                   %w[floor_slab overhead_slab].include?(proof['slab_role']) && proof['faces'].is_a?(Array) &&
                   proof['construction_cells'].is_a?(Array) && !proof['construction_cells'].empty?
              raise RepairError.new('INVALID_SLAB_CONTRACT', 'Whole slab recipe and whole-model source evidence are required.')
            end
            slab_faces=proof['faces'].map { |face| [face['outer'],*(face['holes'] || [])] }
            unless surface_boundary(slab_faces) == surface_boundary(actual_faces)
              raise RepairError.new('SLAB_MESH_MISMATCH', 'Slab mesh differs from its complete source-bound material boundary.')
            end
            next
          end
          unless surface_boundary(expected_faces) == surface_boundary(actual_faces)
            raise RepairError.new('MESH_FOOTPRINT_MISMATCH', 'Approved mesh is not the exact vertical extrusion of its bound footprint.')
          end
        end
      end

      def validate_whole_cad_observation!(plan, current)
        original = plan.dig('source', 'sync_model')
        unless current.is_a?(Hash) && original.is_a?(Hash) &&
               current['schema'] == 'dwg-cad-model-observation-v1' && current['selection_count'] == 0 &&
               current['geometry_signature'] == original['geometry_signature'] && current['document'] == original['document']
          raise RepairError.new('CAD_MODEL_CHANGED', 'A fresh complete CAD reread with unchanged contents and empty selection is required.')
        end
        begin
          now = Time.iso8601(current.fetch('observed_at_utc'))
          previous = Time.iso8601(original.fetch('observed_at_utc'))
          raise 'stale read' unless now > previous && Time.now - now <= 30 && Time.now - now >= -5
        rescue StandardError
          raise RepairError.new('CAD_MODEL_STALE', 'CAD whole-model evidence is expired or reused.')
        end
      end

      def canonical(value)
        case value
        when Hash
          value.keys.map(&:to_s).sort.each_with_object({}) do |key, result|
            source_key = value.key?(key) ? key : value.keys.find { |candidate| candidate.to_s == key }
            result[key] = canonical(value[source_key])
          end
        when Array
          value.map { |item| canonical(item) }
        else
          value
        end
      end

      def resolve_entity(model, id)
        match = /\Aentity:(\d+)\z/.match(id.to_s)
        unless match && model.respond_to?(:find_entity_by_persistent_id)
          raise RepairError.new('TARGET_NOT_FOUND', 'Entity ID is invalid.', entity_id: id)
        end
        entity = model.find_entity_by_persistent_id(match[1].to_i)
        entity || raise(RepairError.new('TARGET_NOT_FOUND', 'Entity no longer exists.', entity_id: id))
      end

      def stable_id(entity)
        "entity:#{entity.persistent_id}"
      end

      def parse_json_attribute(entity, key)
        value = entity.get_attribute(DICTIONARY, key)
        return value if value.is_a?(Hash) || value.is_a?(Array)
        return nil unless value.is_a?(String) && !value.empty?
        JSON.parse(value)
      rescue JSON::ParserError
        nil
      end

      def safe_volume_mm3(group, world)
        # The native writer may condition tiny source edges in large local
        # coordinates and retain an inverse instance transform. group.volume
        # is local: integrate actual world triangles instead. A nearby origin
        # avoids cancellation at survey-sized coordinates; no source is moved.
        origin = nil
        volume = 0.0
        group.entities.grep(Sketchup::Face).each do |face|
          mesh = face.mesh(0)
          mesh.polygons.each do |indices|
            points = indices.map do |i|
              p = world * mesh.point_at(i.abs)
              [p.x.to_f*25.4, p.y.to_f*25.4, p.z.to_f*25.4]
            end
            origin ||= points.first
            shifted = points.map { |p| p.zip(origin).map { |a,b| a-b } }
            (1...shifted.length-1).each do |i|
              a,b,c = shifted[0], shifted[i], shifted[i+1]
              volume += (a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0]))/6.0
            end
          end
        end
        value = volume.abs
        origin && value.finite? ? value : nil
      rescue StandardError
        nil
      end

      def transform_summary(transform)
        {
          'origin_mm' => transform.origin.to_a.map { |value| value.to_f * 25.4 },
          'xaxis' => transform.xaxis.to_a.map(&:to_f),
          'yaxis' => transform.yaxis.to_a.map(&:to_f),
          'zaxis' => transform.zaxis.to_a.map(&:to_f)
        }
      end

      def vector3(values, label)
        unless values.is_a?(Array) && values.length == 3
          raise RepairError.new('INVALID_ARGUMENT', "#{label} must contain three numbers.")
        end
        values.map { |value| finite_float(value, label) }
      end

      def finite_float(value, label)
        number = Float(value)
        unless number.finite?
          raise RepairError.new('INVALID_ARGUMENT', "#{label} must be finite.")
        end
        number
      rescue ArgumentError, TypeError
        raise RepairError.new('INVALID_ARGUMENT', "#{label} must be numeric.")
      end

      def units_name(model)
        units = model.options['UnitsOptions']['LengthUnit']
        { 0 => 'inches', 1 => 'feet', 2 => 'millimeters', 3 => 'centimeters', 4 => 'meters', 5 => 'yards' }.fetch(units, 'unknown')
      end

      def failure(request_id, error)
        wrapped = error.is_a?(RepairError) ? error : RepairError.new(
          'INTERNAL_ERROR', 'Live-repair bridge failed internally.',
          exception: error.class.name, detail: error.message
        )
        {
          request_id: request_id, ok: false,
          error: { code: wrapped.code, message: wrapped.message, details: wrapped.details },
          warnings: [], meta: { bridge: PROTOCOL, protocol_version: PROTOCOL_VERSION }
        }
      end

      def write_response(socket, response)
        socket.write(JSON.generate(response) + "\n") unless socket.closed?
      rescue StandardError
        nil
      end

      def close_client(socket)
        @clients.delete(socket) if @clients
        safe_close(socket)
      end

      def safe_close(socket)
        socket.close if socket && !socket.closed?
      rescue StandardError
        nil
      end
    end
  end
end

unless file_loaded?(__FILE__)
  DWGLiveRepair::Bridge.start
  file_loaded(__FILE__)
end

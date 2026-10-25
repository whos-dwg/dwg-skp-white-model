# Isolated dispatch/loopback harness. No SketchUp API and no real sockets.
require 'json'
require 'socket'
def file_loaded?(_path); true; end
def file_loaded(_path); end
module UI
  def self.start_timer(*_args); 42; end
  def self.stop_timer(*_args); end
end
module Sketchup
  def self.active_model; nil; end
  def self.version; 'synthetic-only'; end
  class Group
    def initialize(schema); @schema=schema; end
    def get_attribute(_dictionary,key); key=='schema' ? @schema : nil; end
  end
end
class AuditServer
  def listen(number); @backlog=number; end
  def closed?; @closed || false; end
  def close; @closed=true; end
end
TCPServer.define_singleton_method(:new) do |host,port|
  $audit_bind=[host,port]
  AuditServer.new
end
class AuditSocket
  attr_reader :output
  def initialize; @output=''; @closed=false; end
  def write(text); @output << text; end
  def closed?; @closed; end
  def close; @closed=true; end
end
load ENV.fetch('DWG_AUDIT_BRIDGE')
bridge=DWGLiveRepair::Bridge
bridge.start
bridge.stop
bridge.define_singleton_method(:inspect_white_model) { {'synthetic'=>'inspect'} }
bridge.define_singleton_method(:apply_live_repair) { |_params| {'synthetic'=>'apply'} }
allowed=%w[health inspect_white_model apply_live_repair]
forbidden=%w[eval ruby execute_script run_script save save_as delete_all]
results={}
(allowed+forbidden).each do |name|
  socket=AuditSocket.new
  request=JSON.generate({'request_id'=>'synthetic-'+name,'method'=>name,
                         'params'=>{'script'=>'$audit_forbidden_executed=true'}})+"\n"
  bridge.instance_variable_set(:@clients,{socket=>request})
  bridge.send(:process_client,socket)
  response=JSON.parse(socket.output)
  results[name]={'ok'=>response['ok'],'code'=>response.dig('error','code'),'socket_closed'=>socket.closed?}
end
root_results={}
{'none'=>[], 'one'=>[Sketchup::Group.new('dwg-skp-model-v2')],
 'two'=>[Sketchup::Group.new('dwg-skp-model-v2'),Sketchup::Group.new('dwg-skp-model-v2')]}.each do |name,groups|
  begin
    bridge.send(:white_model_root,Struct.new(:entities).new(groups))
    root_results[name]={'accepted'=>true}
  rescue DWGLiveRepair::RepairError => error
    root_results[name]={'accepted'=>false,'code'=>error.code}
  end
end
JSON.generate({'bind'=>$audit_bind,'results'=>results,'root_rules'=>root_results,
               'forbidden_script_executed'=>!!$audit_forbidden_executed})

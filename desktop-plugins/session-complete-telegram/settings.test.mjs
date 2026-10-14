// Controller contracts only; native React/GUI verification is separate.
import test from 'node:test'
import assert from 'node:assert/strict'
import {registerHooks} from 'node:module'
registerHooks({resolve(s,c,n){
 if(s==='@hermes/plugin-sdk')return{url:'data:text/javascript,export const host={};export const pluginSettingsHref=()=>"/settings?tab=plugins";export const ROUTES_AREA="routes";export const SIDEBAR_NAV_AREA="sidebar.nav"',shortCircuit:true}
 if(s==='react/jsx-runtime')return{url:'data:text/javascript,export const jsx=(type,props,key)=>({type,props,key})',shortCircuit:true}
 return n(s,c)
}})
const {SETTINGS_ROWS,VERSION,validateSettingsStatus,readBridgeSettings,writeBridgeSetting,registerBridgeSettings,NativeHelperError,CompletionMonitor}=await import('./desktop/plugin.js')
function status(){return {ok:true,version:VERSION,read_only:true,selection_reason:'ready',
 settings:Object.fromEntries(SETTINGS_ROWS.map(r=>[r.key,true])),
 effective:Object.fromEntries(SETTINGS_ROWS.filter(r=>r.module).map(r=>[r.module,true])),
 receiver:{version:VERSION,heartbeat_ready:true},
 sender:{version:VERSION,available:true,awaiting_recovery:0,uncertain_delivery:0,sent:0,expired:0}}}
function fixture(){
 const state=status(),calls=[]
 const monitor={stopped:false,refreshRoutes:async force=>{assert.equal(force,true)},helper:async request=>{
   calls.push(structuredClone(request))
   if(request.action==='settings_status')return structuredClone(state)
   assert.equal(request.action,'settings_set')
   assert.equal(request.expected,state.settings[request.key])
   state.settings[request.key]=request.value
   return {...structuredClone(state),read_only:false,write_status:'verified',
     readback:{key:request.key,value:request.value},cleanup:{ok:true}}
 }}
 return {state,calls,monitor}
}
for(const row of SETTINGS_ROWS)test('single native leaf write preserves other choices: '+row.key,async()=>{
 const f=fixture(),before=structuredClone(f.state.settings)
 const reply=await writeBridgeSetting(f.monitor,{key:row.key,value:false,expected:true})
 assert.equal(reply.settings[row.key],false)
 assert.deepEqual(f.calls.map(c=>c.action),['settings_status','settings_set'])
 assert.deepEqual(f.state.settings,{...before,[row.key]:false})
})
test('service disabled status is readable and can enable only master',async()=>{
 const f=fixture();f.state.settings.service_enabled=false;f.state.selection_reason='service_disabled'
 const snapshot=await readBridgeSettings(f.monitor);assert.equal(snapshot.settings.service_enabled,false)
 await writeBridgeSetting(f.monitor,{key:'service_enabled',value:true,expected:false})
 assert.equal(f.state.settings.service_enabled,true)
})
test('changed baseline never silently rebases or writes',async()=>{
 const f=fixture();f.state.settings.service_enabled=false
 await assert.rejects(writeBridgeSetting(f.monitor,{key:'service_enabled',value:false,expected:true}),/다른 화면/)
 assert.equal(f.calls.some(c=>c.action==='settings_set'),false)
 assert.equal(f.monitor.settingsNeedsRefresh,true)
})
test('ambiguous native write blocks subsequent clicks without replay until explicit refresh',async()=>{
 const f=fixture(),original=f.monitor.helper
 f.monitor.helper=async r=>{if(r.action==='settings_set'){f.calls.push(r);throw new NativeHelperError('unknown',true)}return original(r)}
 await assert.rejects(writeBridgeSetting(f.monitor,{key:'service_enabled',value:false,expected:true}),e=>e.ambiguousWrite===true)
 await assert.rejects(writeBridgeSetting(f.monitor,{key:'service_enabled',value:false,expected:true}))
 await readBridgeSettings(f.monitor)
 assert.equal(f.monitor.settingsNeedsRefresh,true,'background reads do not authorize replay')
 assert.equal(f.calls.filter(c=>c.action==='settings_set').length,1)
})
test('malformed result after dispatch is uncertain rather than retryable',async()=>{
 const f=fixture(),original=f.monitor.helper
 f.monitor.helper=r=>r.action==='settings_set'?Promise.resolve({ok:true}):original(r)
 await assert.rejects(writeBridgeSetting(f.monitor,{key:'service_enabled',value:false,expected:true}),e=>e.ambiguousWrite===true)
 assert.equal(f.monitor.settingsNeedsRefresh,true)
})
test('invalid types, unknown keys, disposal and busy state cannot invoke a helper',async()=>{
 for(const request of [{key:'root',value:false,expected:true},{key:'service_enabled',value:'false',expected:true},{key:'service_enabled',value:false,expected:1}]){
   const f=fixture();await assert.rejects(writeBridgeSetting(f.monitor,request));assert.equal(f.calls.length,0)
 }
 for(const flag of ['stopped','settingsBusy','settingsNeedsRefresh']){
   const f=fixture();f.monitor[flag]=true
   await assert.rejects(writeBridgeSetting(f.monitor,{key:'service_enabled',value:false,expected:true}));assert.equal(f.calls.length,0)
 }
})
test('native ownership/policy refusal remains non-writing',async()=>{
 const f=fixture();f.state.ok=false;f.state.selection_reason='native_plugin_home_mismatch'
 await assert.rejects(writeBridgeSetting(f.monitor,{key:'service_enabled',value:false,expected:true}))
 assert.equal(f.calls.some(c=>c.action==='settings_set'),false)
})
test('strict DTO rejects old version, incomplete flags and truthy coercions',()=>{
 for(const mutate of [v=>{v.version='1.7.0'},v=>{delete v.settings.service_enabled},v=>{v.settings.service_enabled=1},v=>{v.effective.approvals='true'},v=>{v.receiver.heartbeat_ready=1}]){
   const v=status();mutate(v);assert.throws(()=>validateSettingsStatus(v))
 }
})
test('registers dedicated native settings page, preserving older route fallback',()=>{
 const pages=[],routes=[]
 registerBridgeSettings({registerSettingsPage:p=>pages.push(p)},fixture().monitor)
 assert.equal(pages.length,1);assert.equal(pages[0].id,'telegram');assert.equal(typeof pages[0].render,'function')
 registerBridgeSettings({register:p=>routes.push(p)},fixture().monitor)
 assert.deepEqual(routes.map(r=>r.area),['routes','sidebar.nav'])
})
test('settings_set framed transport is an uncertain write and is never silently retried',async()=>{
 const route={mode:'local',profile:'default',targetProfile:'default',connectionId:'local'}
 let count=0
 const m=new CompletionMonitor({profileRoutes:async()=>[route],requestProfile:async()=>{count++;throw Error('timeout')}},{},()=>1)
 await assert.rejects(m.helper({action:'settings_set',key:'service_enabled',value:false,expected:true}),e=>e.ambiguousWrite===true)
 assert.equal(count,1)
})

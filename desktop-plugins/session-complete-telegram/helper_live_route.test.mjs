// Helper transport contracts; SDK cold-dial/CLI receipts are simulated here.
// Live Desktop/native acceptance is a separate operational gate.
import test from 'node:test'
import assert from 'node:assert/strict'
import {registerHooks} from 'node:module'
registerHooks({resolve(specifier,context,next){
 if(specifier==='@hermes/plugin-sdk')return {url:'data:text/javascript,export const host={}',shortCircuit:true}
 if(specifier==='react/jsx-runtime')return {url:'data:text/javascript,export const jsx=()=>({})',shortCircuit:true}
 return next(specifier,context)
}})
const {CompletionMonitor,NativeHelperError}=await import('./desktop/plugin.js')
const DEFAULT={mode:'local',connectionId:'local',profile:'default',targetProfile:'default'}
const CLAIRE={mode:'local',connectionId:'local',profile:'claire',targetProfile:'claire'}
const frame=value=>({blocked:false,code:0,output:'HERMES_TELEGRAM_BRIDGE_RESULT_V1_BEGIN\n'+Buffer.from(JSON.stringify(value)).toString('base64url')+'\nHERMES_TELEGRAM_BRIDGE_RESULT_V1_END\n'})
function fixture({connection='local',profile='claire',socket='open',routes=[DEFAULT,CLAIRE],activeError=null}={}){
 const calls=[]
 const current={request:async(method,params,timeout)=>{
  calls.push({transport:'captured-active',method,params:structuredClone(params),timeout})
  if(activeError)throw activeError
  return frame({ok:true,read_only:true,marker:'synthetic fixed native CLI receipt'})
 }}
 const api={state:{gateway:{get:()=>socket},connectionId:{get:()=>connection},profile:{get:()=>profile}},
  profileRoutes:async()=>routes,getGateway:()=>current,
  requestProfile:async(route,method,params,timeout)=>{
   calls.push({transport:'pooled',route,method,params:structuredClone(params),timeout})
   throw Error('simulated unavailable pooled default route before CLI dispatch')
  }}
 return {monitor:new CompletionMonitor(api,{},()=>1000),api,calls,current}
}
for(const action of ['settings_status','approval_owner_heartbeat','approval_registration_lookup']){
 test('fixed '+action+' uses the already-open same-local-source conduit without dialing default',async()=>{
  const f=fixture()
  try{
   const result=await f.monitor.helper({action})
   assert.equal(result.ok,true)
   assert.deepEqual(f.calls.map(x=>x.transport),['captured-active'])
   assert.deepEqual(f.calls[0].params.argv.slice(0,4),['--profile','default','desktop-telegram-bridge','--request'])
   const request=JSON.parse(Buffer.from(f.calls[0].params.argv[4],'base64url').toString())
   assert.deepEqual(request,{action})
   assert.equal(f.calls[0].params.timeout,25,'transport repair does not change the child deadline')
   assert.equal(f.calls[0].timeout,35000,'transport repair does not change the outer deadline')
  }finally{f.monitor.dispose()}
 })
}
test('a possibly-dispatched approval write never falls back to another conduit',async()=>{
 const f=fixture({activeError:Error('synthetic lost response')})
 try{
  await assert.rejects(f.monitor.helper({action:'approval_register'}),e=>e instanceof NativeHelperError&&e.ambiguousWrite===true)
  assert.deepEqual(f.calls.map(x=>x.transport),['captured-active'])
 }finally{f.monitor.dispose()}
})
for(const [name,options] of [
 ['remote foreground',{connection:'remote'}],
 ['closed foreground socket',{socket:'closed'}],
 ['different local source',{connection:'local-b',routes:[DEFAULT,{...CLAIRE,connectionId:'local-b'}]}]
])test(name+' never borrows its active socket for the local default sender',async()=>{
 const f=fixture(options)
 try{
  await assert.rejects(f.monitor.helper({action:'settings_status'}),NativeHelperError)
  assert.equal(f.calls.some(x=>x.transport==='captured-active'),false)
  assert.deepEqual(f.calls.map(x=>x.transport),['pooled'],'preserve ordinary local-default fallback, not recovery policy')
  assert.equal(f.calls[0].route.connectionId,'local')
 }finally{f.monitor.dispose()}
})
test('ambiguous default sender routes fail before either transport',async()=>{
 const f=fixture({routes:[DEFAULT,{...DEFAULT,connectionId:'local-b'},CLAIRE]})
 try{
  await assert.rejects(f.monitor.helper({action:'settings_status'}),NativeHelperError)
  assert.equal(f.calls.length,0)
 }finally{f.monitor.dispose()}
})
test('explicit native pre-launch refusal does not retry or claim an ambiguous write',async()=>{
 const f=fixture()
 f.current.request=async()=>{f.calls.push({transport:'captured-active'});return {blocked:true,code:-1,output:''}}
 try{
  await assert.rejects(f.monitor.helper({action:'approval_register'}),e=>e instanceof NativeHelperError&&e.ambiguousWrite===false)
  assert.deepEqual(f.calls.map(x=>x.transport),['captured-active'])
 }finally{f.monitor.dispose()}
})
test('a changed captured gateway cannot run a helper through a mixed owner',async()=>{
 const f=fixture();let reads=0
 f.api.getGateway=()=>reads++===0?f.current:{request:async()=>{throw Error('must not run')}}
 try{
  await assert.rejects(f.monitor.helper({action:'approval_owner_heartbeat'}),NativeHelperError)
  assert.equal(f.calls.length,0)
 }finally{f.monitor.dispose()}
})
test('ordinary background-profile payload keeps its explicit target and default credential owner',async()=>{
 const f=fixture()
 const payload={action:'inspect',profile:'background-profile',session_key:'synthetic-durable'}
 try{
  await f.monitor.helper(payload)
  assert.equal(f.calls[0].transport,'captured-active')
  assert.deepEqual(f.calls[0].params.argv.slice(0,4),['--profile','default','desktop-telegram-bridge','--request'])
  assert.deepEqual(JSON.parse(Buffer.from(f.calls[0].params.argv[4],'base64url').toString()),payload)
 }finally{f.monitor.dispose()}
})
test('older hosts without the public live-gateway getter keep ordinary routed helpers',async()=>{
 const f=fixture();delete f.api.getGateway
 f.api.requestProfile=async(route,method,params)=>{
  f.calls.push({transport:'pooled',route,method,params:structuredClone(params)})
  return frame({ok:true,read_only:true})
 }
 try{
  assert.equal((await f.monitor.helper({action:'settings_status'})).ok,true)
  assert.deepEqual(f.calls.map(x=>x.transport),['pooled'])
 }finally{f.monitor.dispose()}
})
test('recovery actions retain the existing active-target scope check',async()=>{
 const f=fixture()
 try{
  await assert.rejects(f.monitor.helper({action:'completion_recovery_list',route:{...CLAIRE,profile:'foreign',targetProfile:'foreign'}}),NativeHelperError)
  assert.equal(f.calls.length,0)
 }finally{f.monitor.dispose()}
})

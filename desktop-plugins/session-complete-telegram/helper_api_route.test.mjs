// Public Desktop API transport contract. Every transport is stubbed; no native
// Python helper, provider credential, backend HTTP, or Telegram request is used.
import test from 'node:test'
import assert from 'node:assert/strict'
import {registerHooks} from 'node:module'
registerHooks({resolve(specifier,context,next){
 if(specifier==='@hermes/plugin-sdk')return {url:'data:text/javascript,export const host={}',shortCircuit:true}
 if(specifier==='react/jsx-runtime')return {url:'data:text/javascript,export const jsx=()=>({})',shortCircuit:true}
 return next(specifier,context)
}})
const {CompletionMonitor,NativeHelperError,decodeResultFrame}=await import('./desktop/plugin.js')

const BASE='/api/plugins/session-complete-telegram/bridge/v1'
const DEFAULT={mode:'local',connectionId:'local',profile:'default',targetProfile:'default'}
const CLAIRE={mode:'local',connectionId:'local',profile:'claire',targetProfile:'claire'}
const capability={ok:true,transport:'desktop-telegram-api-v1',plugin:'session-complete-telegram',profile:'default'}
const helperResult={ok:true,marker:'synthetic bounded helper result'}
const frame=value=>({blocked:false,code:0,output:'HERMES_TELEGRAM_BRIDGE_RESULT_V1_BEGIN\n'+Buffer.from(JSON.stringify(value)).toString('base64url')+'\nHERMES_TELEGRAM_BRIDGE_RESULT_V1_END\n'})
const deferred=()=>{
 let resolve,reject
 const promise=new Promise((yes,no)=>{resolve=yes;reject=no})
 return {promise,resolve,reject}
}
const tick=()=>new Promise(resolve=>setImmediate(resolve))

function fixture({routes=[DEFAULT,CLAIRE],activeConnection='local',activeProfile='claire',socket='open',apiHandler}={}){
 const apiCalls=[]
 const cliCalls=[]
 const pooledCalls=[]
 let liveConnection=activeConnection
 let liveProfile=activeProfile
 let liveSocket=socket
 let gateway={request:async(method,params,timeout)=>{
  cliCalls.push({method,params:structuredClone(params),timeout})
  return frame({ok:true,transport:'synthetic-cli'})
 }}
 let routeRows=routes
 const api={
  state:{gateway:{get:()=>liveSocket},connectionId:{get:()=>liveConnection},profile:{get:()=>liveProfile}},
  profileRoutes:async()=>structuredClone(routeRows),
  getGateway:()=>gateway,
  requestProfile:async(route,method,params,timeout)=>{
   pooledCalls.push({route:structuredClone(route),method,params:structuredClone(params),timeout})
   return frame({ok:true,transport:'synthetic-pooled-cli'})
  }
 }
 const priorWindow=globalThis.window
 const desktop={}
 if(apiHandler!==null)desktop.api=apiHandler||(async request=>{
  apiCalls.push(structuredClone(request))
  if(request.method==='GET')return structuredClone(capability)
  if(request.method==='POST')return {transport:'desktop-telegram-api-v1',dispatched:true,result:structuredClone(helperResult)}
  throw Error('unexpected API method')
 })
 globalThis.window={hermesDesktop:desktop}
 const monitor=new CompletionMonitor(api,{},()=>1000)
 return {
  monitor,api,desktop,apiCalls,cliCalls,pooledCalls,
  setConnection(value){liveConnection=value},setProfile(value){liveProfile=value},setSocket(value){liveSocket=value},
  setGateway(value){gateway=value},setRoutes(value){routeRows=value},
  cleanup(){monitor.dispose();if(priorWindow===undefined)delete globalThis.window;else globalThis.window=priorWindow}
 }
}

test('capability cache is bounded across profile/source churn',async()=>{
 const f=fixture()
 try{
  for(let i=0;i<48;i++)await f.monitor.helperApiCapabilities({desktop:f.desktop,api:f.desktop.api},`route-${i}`,async()=>true)
  assert.equal(f.monitor.helperApiCapabilityCache.get(f.desktop).size,32)
 }finally{f.cleanup()}
})
test('saturated pending capabilities refuse another read without evicting in-flight ownership',async()=>{
 const gate=deferred()
 const f=fixture({apiHandler:async()=>gate.promise})
 const pending=[]
 try{
  for(let i=0;i<32;i++)pending.push(f.monitor.helperApiCapabilities({desktop:f.desktop,api:f.desktop.api},`pending-${i}`,async()=>true))
  await assert.rejects(f.monitor.helperApiCapabilities({desktop:f.desktop,api:f.desktop.api},'overflow',async()=>true),NativeHelperError)
  assert.equal(f.monitor.helperApiCapabilityCache.get(f.desktop).size,32)
 }finally{
  gate.resolve(structuredClone(capability))
  await Promise.all(pending)
  f.cleanup()
 }
})

test('API result depth matches the existing Python result64 budget, excluding its envelope',async()=>{
 let payload=null
 for(let i=0;i<63;i++)payload={nested:payload}
 const result={ok:true,payload}
 assert.deepEqual(decodeResultFrame(frame(result).output),result)
 const f=fixture({apiHandler:async request=>request.method==='GET'?structuredClone(capability):({transport:'desktop-telegram-api-v1',dispatched:true,result})})
 try{assert.deepEqual(await f.monitor.helper({action:'approval_poll'}),result);assert.equal(f.cliCalls.length,0)}finally{f.cleanup()}
})
test('API result over depth64 stays uncertain and cannot fall back to CLI',async()=>{
 let payload=null
 for(let i=0;i<64;i++)payload={nested:payload}
 const f=fixture({apiHandler:async request=>request.method==='GET'?structuredClone(capability):({transport:'desktop-telegram-api-v1',dispatched:true,result:{ok:true,payload}})})
 try{await assert.rejects(f.monitor.helper({action:'approval_poll'}),error=>error instanceof NativeHelperError&&error.ambiguousWrite===true);assert.equal(f.cliCalls.length,0)}finally{f.cleanup()}
})

function exactApiScope(call,method,path){
 assert.equal(call.path,`${BASE}/${path}`)
 assert.equal(call.method,method)
 assert.equal(call.connectionId,'local')
 assert.equal(call.profile,'default')
 assert.equal(call.passive,true)
 assert.equal(call.timeoutMs,method==='GET'?8_000:35_000)
 assert.equal(Object.hasOwn(call,'priority'),false)
}
function decodePayload(call){return JSON.parse(Buffer.from(call.body.request,'base64url').toString('utf8'))}
function httpError(status,message=`${status}: {"detail":"synthetic"}`){
 const error=Error(message)
 error.statusCode=status
 return error
}

test('first helper use negotiates the exact API capability then dispatches one immutable default-owner payload',async()=>{
 const f=fixture()
 const payload={action:'settings_set',targetProfile:'claire',profile:'claire',key:'replies_enabled',value:false}
 try{
  assert.deepEqual(await f.monitor.helper(payload),helperResult)
  assert.equal(f.apiCalls.length,2)
  exactApiScope(f.apiCalls[0],'GET','capabilities')
  assert.equal(Object.hasOwn(f.apiCalls[0],'body'),false)
  exactApiScope(f.apiCalls[1],'POST','dispatch')
  assert.deepEqual(Object.keys(f.apiCalls[1].body),['request'])
  assert.deepEqual(decodePayload(f.apiCalls[1]),payload,'payload.profile/targetProfile remain caller-owned and unchanged')
  assert.equal(f.cliCalls.length,0)
  assert.equal(f.pooledCalls.length,0)
 }finally{f.cleanup()}
})

test('concurrent first use shares only the route-bound capability read and dispatches each helper once',async()=>{
 const gate=deferred()
 const f=fixture({apiHandler:async request=>{
  f.apiCalls.push(structuredClone(request))
  if(request.method==='GET')return gate.promise
  return {transport:'desktop-telegram-api-v1',dispatched:true,result:{ok:true,sequence:f.apiCalls.filter(x=>x.method==='POST').length}}
 }})
 try{
  const one=f.monitor.helper({action:'settings_status',serial:1})
  const two=f.monitor.helper({action:'settings_status',serial:2})
  await tick()
  assert.equal(f.apiCalls.filter(x=>x.method==='GET').length,1)
  gate.resolve(structuredClone(capability))
  const [a,b]=await Promise.all([one,two])
  assert.equal(f.apiCalls.filter(x=>x.method==='POST').length,2)
  assert.equal(f.cliCalls.length,0)
  assert.deepEqual([a.ok,b.ok],[true,true])
 }finally{f.cleanup()}
})

test('a validated capability is cached only for the same captured bridge and exact active route',async()=>{
 const f=fixture()
 try{
  await f.monitor.helper({action:'settings_status'})
  await f.monitor.helper({action:'settings_status'})
  assert.equal(f.apiCalls.filter(x=>x.method==='GET').length,1)
  assert.equal(f.apiCalls.filter(x=>x.method==='POST').length,2)
  f.setProfile('default')
  await f.monitor.helper({action:'settings_status'})
  assert.equal(f.apiCalls.filter(x=>x.method==='GET').length,2,'profile route change renegotiates')
  globalThis.window={hermesDesktop:{api:f.desktop.api}}
  await f.monitor.helper({action:'settings_status'})
  assert.equal(f.apiCalls.filter(x=>x.method==='GET').length,3,'a different captured public bridge renegotiates')
 }finally{f.cleanup()}
})

test('only a confirmed read-only capabilities 404 falls back to the unchanged ordinary CLI envelope',async()=>{
 const f=fixture({apiHandler:async request=>{
  f.apiCalls.push(structuredClone(request))
  if(request.method==='GET')throw Error(`Error invoking remote method 'hermes:api': Error: 404: {"detail":"No such API endpoint: ${BASE}/capabilities"}`)
  throw Error('dispatch must not run')
 }})
 try{
  assert.equal((await f.monitor.helper({action:'settings_set',key:'x',value:false})).transport,'synthetic-cli')
  assert.equal(f.apiCalls.length,1)
  exactApiScope(f.apiCalls[0],'GET','capabilities')
  assert.equal(f.cliCalls.length,1)
  assert.deepEqual(f.cliCalls[0].params.argv.slice(0,4),['--profile','default','desktop-telegram-bridge','--request'])
  assert.equal(f.cliCalls[0].params.timeout,25)
  assert.equal(f.cliCalls[0].timeout,35_000)
 }finally{f.cleanup()}
})

test('an absent public bridge function preserves loaded/reply-register 60/75-second fixed CLI fallbacks',async()=>{
 const f=fixture({apiHandler:null})
 try{
  assert.equal((await f.monitor.helper({action:'loaded',profiles:['claire'],version:'1.8.1'})).transport,'synthetic-cli')
  assert.equal((await f.monitor.helper({action:'reply_register'})).transport,'synthetic-cli')
  assert.equal(f.apiCalls.length,0)
  assert.equal(f.cliCalls.length,2)
  assert.deepEqual(f.cliCalls.map(call=>call.params.timeout),[60,60])
  assert.deepEqual(f.cliCalls.map(call=>call.timeout),[75_000,75_000])
 }finally{f.cleanup()}
})

test('capability permission denial, timeout, and malformed/extra-key responses fail closed without CLI',async t=>{
 const cases=[
  ['permission',()=>{throw httpError(403)}],
  ['timeout',()=>{const error=Error('synthetic timeout');error.code='ETIMEDOUT';throw error}],
  ['malformed capability',()=>({ ...capability, unexpected:true })],
  ['wrong capability owner',()=>({ ...capability, profile:'claire' })]
 ]
 for(const [name,make] of cases){
  await t.test(name,async()=>{
   const f=fixture({apiHandler:async request=>{
    f.apiCalls.push(structuredClone(request))
    if(request.method==='GET')return make()
    return {transport:'desktop-telegram-api-v1',dispatched:true,result:helperResult}
   }})
   try{
    await assert.rejects(f.monitor.helper({action:'settings_set',key:'x',value:true}),
     error=>error instanceof NativeHelperError&&error.ambiguousWrite===false)
    assert.equal(f.apiCalls.filter(x=>x.method==='GET').length,1)
    assert.equal(f.apiCalls.filter(x=>x.method==='POST').length,0)
    assert.equal(f.cliCalls.length,0)
    assert.equal(f.pooledCalls.length,0)
   }finally{f.cleanup()}
  })
 }
})

test('malformed dispatch receipts and lost POST receipts never retry through CLI',async t=>{
 const cases=[
  ['lost receipt',()=>{throw Error('synthetic lost receipt')}],
  ['outer unknown key',()=>({transport:'desktop-telegram-api-v1',dispatched:true,result:helperResult,extra:true})],
  ['truthy dispatched coercion',()=>({transport:'desktop-telegram-api-v1',dispatched:1,result:helperResult})],
  ['truthy result.ok coercion',()=>({transport:'desktop-telegram-api-v1',dispatched:true,result:{ok:'true'}})],
  ['contradictory pre-dispatch success',()=>({transport:'desktop-telegram-api-v1',dispatched:false,result:{ok:true}})]
 ]
 for(const [name,make] of cases){
  await t.test(name,async()=>{
   const f=fixture({apiHandler:async request=>{
    f.apiCalls.push(structuredClone(request))
    if(request.method==='GET')return structuredClone(capability)
    return make()
   }})
   try{
    await assert.rejects(f.monitor.helper({action:'settings_set',key:'x',value:true}),
     error=>error instanceof NativeHelperError&&error.ambiguousWrite===true)
    assert.deepEqual(f.apiCalls.map(x=>x.method),['GET','POST'])
    assert.equal(f.cliCalls.length,0)
    assert.equal(f.pooledCalls.length,0)
   }finally{f.cleanup()}
  })
 }
})

test('a genuine dispatcher pre-launch refusal is returned without any CLI fallback',async()=>{
 const f=fixture({apiHandler:async request=>{
  f.apiCalls.push(structuredClone(request))
  return request.method==='GET'?structuredClone(capability):
   {transport:'desktop-telegram-api-v1',dispatched:false,result:{ok:false,reason:'not_dispatched'}}
 }})
 try{
  assert.deepEqual(await f.monitor.helper({action:'settings_set',key:'x',value:true}),{ok:false,reason:'not_dispatched'})
  assert.deepEqual(f.apiCalls.map(x=>x.method),['GET','POST'])
  assert.equal(f.cliCalls.length,0)
 }finally{f.cleanup()}
})

test('a dispatched domain refusal stays on that endpoint and is returned without retry',async()=>{
 const refusal={ok:false,reason:'permission_denied'}
 const f=fixture({apiHandler:async request=>{
  f.apiCalls.push(structuredClone(request))
  return request.method==='GET'?structuredClone(capability):
   {transport:'desktop-telegram-api-v1',dispatched:true,result:refusal}
 }})
 try{
  assert.deepEqual(await f.monitor.helper({action:'settings_set',key:'x',value:true}),refusal)
  assert.deepEqual(f.apiCalls.map(x=>x.method),['GET','POST'])
  assert.equal(f.cliCalls.length,0)
  assert.equal(f.pooledCalls.length,0)
 }finally{f.cleanup()}
})

test('active profile/connection/gateway switch during capability read blocks dispatch and CLI fallback',async()=>{
 const gate=deferred()
 const f=fixture({apiHandler:async request=>{
  f.apiCalls.push(structuredClone(request))
  if(request.method==='GET')return gate.promise
  throw Error('POST must not run')
 }})
 try{
  const pending=f.monitor.helper({action:'settings_set',key:'x',value:true})
  await tick()
  f.setProfile('other-profile')
  f.setConnection('other-local-source')
  f.setGateway({request:async()=>{throw Error('stale gateway must not run')}})
  gate.resolve(structuredClone(capability))
  await assert.rejects(pending,NativeHelperError)
  assert.deepEqual(f.apiCalls.map(x=>x.method),['GET'])
  assert.equal(f.cliCalls.length,0)
  assert.equal(f.pooledCalls.length,0)
 }finally{f.cleanup()}
})

test('disposal during the read-only capability request prevents POST and CLI fallback',async()=>{
 const gate=deferred()
 const f=fixture({apiHandler:async request=>{
  f.apiCalls.push(structuredClone(request))
  return request.method==='GET'?gate.promise:{transport:'desktop-telegram-api-v1',dispatched:true,result:helperResult}
 }})
 try{
  const pending=f.monitor.helper({action:'settings_set',key:'x',value:true})
  await tick()
  f.monitor.dispose()
  gate.resolve(structuredClone(capability))
  await assert.rejects(pending,NativeHelperError)
  assert.deepEqual(f.apiCalls.map(x=>x.method),['GET'])
  assert.equal(f.cliCalls.length,0)
 }finally{f.cleanup()}
})

test('foreign/remote foreground and ambiguous default sender never borrow the Desktop API route',async t=>{
 for(const [name,options] of [
  ['remote foreground',{activeConnection:'remote-a',routes:[DEFAULT,{...CLAIRE,mode:'remote',connectionId:'remote-a'}]}],
  ['ambiguous default senders',{routes:[DEFAULT,{...DEFAULT,connectionId:'local-b'},CLAIRE]}]
 ]){
  await t.test(name,async()=>{
   const f=fixture(options)
   try{
    if(name.startsWith('ambiguous')){
     await assert.rejects(f.monitor.helper({action:'settings_status'}),NativeHelperError)
     assert.equal(f.pooledCalls.length,0)
    }else{
     await f.monitor.helper({action:'settings_status'})
     assert.equal(f.pooledCalls.length,1)
     assert.equal(f.pooledCalls[0].route.connectionId,'local')
    }
    assert.equal(f.apiCalls.length,0)
    assert.equal(f.cliCalls.length,0)
   }finally{f.cleanup()}
  })
 }
})

test('recovery remains on its stricter already-open target route and never uses the API/no-cold pool',async()=>{
 const f=fixture()
 try{
  const route={mode:'local',connectionId:'local',profile:'claire',targetProfile:'claire'}
  assert.equal((await f.monitor.helper({action:'completion_recovery_get',route,candidate_id:'a'.repeat(64)})).transport,'synthetic-cli')
  assert.equal(f.apiCalls.length,0)
  assert.equal(f.cliCalls.length,1)
  assert.equal(f.pooledCalls.length,0)
  assert.equal(f.cliCalls[0].params.timeout,60)
  assert.equal(f.cliCalls[0].timeout,75_000)
 }finally{f.cleanup()}
})

test('settings_set remains an ambiguous write after a dispatched POST loses its receipt',async()=>{
 const f=fixture({apiHandler:async request=>{
  f.apiCalls.push(structuredClone(request))
  if(request.method==='GET')return structuredClone(capability)
  throw Error('synthetic lost post-write receipt')
 }})
 try{
  await assert.rejects(f.monitor.helper({action:'settings_set',key:'completion_notifications_enabled',value:false}),
   error=>error instanceof NativeHelperError&&error.ambiguousWrite===true)
  assert.deepEqual(f.apiCalls.map(x=>x.method),['GET','POST'])
  assert.equal(f.cliCalls.length,0)
 }finally{f.cleanup()}
})

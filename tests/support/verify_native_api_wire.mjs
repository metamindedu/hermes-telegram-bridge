// Fixed, development-only peer for the pre-import-fenced native API fixture.
// Real CompletionMonitor + installed Electron routing; only IPC/HTTP are bridged
// over stdio to the parent's real native authenticated ASGI app. No sockets/CLI.
import assert from 'node:assert/strict'
import {readFileSync,existsSync} from 'node:fs'
import {registerHooks,stripTypeScriptTypes} from 'node:module'
import {resolve} from 'node:path'
import {pathToFileURL,fileURLToPath} from 'node:url'
import {createInterface} from 'node:readline'
const coreElectron=pathToFileURL(resolve(process.env.HERMES_CORE_ROOT,'apps/desktop/electron')+'/').href
registerHooks({
 resolve(specifier,context,next){
  if(specifier==='@hermes/plugin-sdk')return {url:'data:text/javascript,export const host={}',shortCircuit:true}
  if(specifier==='react/jsx-runtime')return {url:'data:text/javascript,export const jsx=()=>({})',shortCircuit:true}
  if(specifier.startsWith('.')&&context.parentURL?.startsWith(coreElectron)){
   const file=new URL(specifier,context.parentURL)
   if(!file.pathname.endsWith('.ts')&&existsSync(fileURLToPath(file)+'.ts'))return {url:file.href+'.ts',shortCircuit:true}
  }
  return next(specifier,context)
 },
 load(url,context,next){
  if(url.startsWith(coreElectron)&&url.endsWith('.ts'))return {format:'module',source:stripTypeScriptTypes(readFileSync(fileURLToPath(url),'utf8'),{mode:'transform'}),shortCircuit:true}
  return next(url,context)
 }
})
const input=createInterface({input:process.stdin,crlfDelay:Infinity})
const iterator=input[Symbol.asyncIterator]()
const read=async()=>{const x=await iterator.next();if(x.done)throw Error('native ASGI peer closed');return JSON.parse(x.value)}
const emit=value=>process.stdout.write(JSON.stringify(value)+'\n')
const {pathForRegistryBackendRequest,apiRequestRegistryConnectionId}=await import(new URL('connection-config.ts',coreElectron))
const {CompletionMonitor,decodeResultFrame}=await import(pathToFileURL(resolve('desktop/plugin.js')))
const first=await read()
assert.deepEqual(Object.keys(first),['requests'])
// Branch/path qualification only: native backend selection/spawn is not run.
assert.equal(apiRequestRegistryConnectionId({connectionId:'local',profile:'default'}),'local')
assert.equal(apiRequestRegistryConnectionId({connectionId:'remote',profile:'default'}),'remote')
assert.equal(apiRequestRegistryConnectionId({profile:'default'}),null)
assert.equal(pathForRegistryBackendRequest('/owned','default',{mode:'local'}),'/owned?profile=default')
assert.equal(pathForRegistryBackendRequest('/owned','claire',{mode:'local'}),'/owned?profile=claire')
const route={mode:'local',connectionId:'local',profile:'claire',targetProfile:'claire'}
const sender={...route,profile:'default',targetProfile:'default'}
let cliCalls=0
const gateway={request:async()=>{cliCalls++;throw Error('native API fixture must not invoke a CLI')}}
const state=value=>({get:()=>value})
const api={state:{gateway:state('open'),connectionId:state('local'),profile:state('claire')},getGateway:()=>gateway,profileRoutes:async()=>[sender,route]}
globalThis.window={hermesDesktop:{api:async request=>{
 assert.equal(request.connectionId,'local');assert.equal(request.profile,'default');assert.equal(request.passive,true)
 assert.equal(apiRequestRegistryConnectionId(request),'local')
 assert.match(request.path,/^\/api\/plugins\/session-complete-telegram\/bridge\/v1\/(?:capabilities|dispatch)$/)
 // Explicit connectionId selects the registry branch, not legacy profile
 // routing. The resolved local descriptor is fixture input; backend lifecycle
 // and passive spawn behavior are deliberately not exercised by this peer.
 const path=pathForRegistryBackendRequest(request.path,request.profile,{mode:'local'})
 assert.equal(new URL(path,'http://fixture.invalid').searchParams.get('profile'),'default')
 emit({type:'http',request:{...request,path},routing:{registryConnectionId:'local',backendLifecycle:'not_exercised'}})
 const reply=await read();assert.equal(reply.type,'reply')
 if(reply.legacy_frame!==undefined)assert.deepEqual(decodeResultFrame(reply.legacy_frame),reply.value.result)
 if(reply.status>=400){const error=new Error(`${reply.status}: ${JSON.stringify(reply.value)}`);error.statusCode=reply.status;throw error}
 return reply.value
}}}
const monitor=new CompletionMonitor(api,{setTimeout:()=>()=>{}})
try{
 const results=[]
 let registered=null,claimed=null
 for(const original of first.requests){
  const request=structuredClone(original)
  if(request.observations?.[0]?.id==='$registered')request.observations[0].id=registered.id
  if(request.action==='approval_settle'){
   assert.equal(request.id,'$registered');assert.equal(request.claim,'$claimed')
   assert.ok(claimed?.claim)
   request.id=registered.id;request.claim=claimed.claim
  }
  const result=await monitor.helper(request);results.push(result)
  if(request.action==='approval_register'){
   assert.equal(result.ok,true);registered=result
   emit({type:'registered',id:registered.id})
   const choice=await read();assert.deepEqual(choice,{type:'choice',ok:true})
  }
  if(request.action==='approval_poll'&&result.items?.length){assert.equal(result.items.length,1);assert.equal(claimed,null);claimed=result.items[0]}
 }
 emit({type:'done',results,cli_calls:cliCalls})
}finally{monitor.dispose();input.close()}

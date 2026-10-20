// Canonical session-authority events and the frozen native approval CLI mode.
// Wire/authority/registered CLI/PTB behavior is exercised in test_canonical_integration.py.
import test from 'node:test'
import assert from 'node:assert/strict'
import {registerHooks, stripTypeScriptTypes} from 'node:module'
import {existsSync, readFileSync} from 'node:fs'
import {resolve} from 'node:path'

registerHooks({resolve(specifier, context, nextResolve) {
  if (specifier === '@hermes/plugin-sdk') return {url:'data:text/javascript,export const host = {}', shortCircuit:true}
  if (specifier === 'react/jsx-runtime') return {url:'data:text/javascript,export const jsx=(type,props,key)=>({type,props,key})', shortCircuit:true}
  return nextResolve(specifier, context)
}})
const {CompletionMonitor, APPROVAL_CAPABILITY, APPROVAL_COMPAT_VERSION, stableJSON} =
  await import('./desktop/plugin.js')

const prompt = (generation=7, promptId='opaque.native.ID/reused') => ({
  kind:'approval', prompt_id:promptId, execution_generation:generation,
  command:'synthetic operation', description:'complete native review',
  choices:['once','deny'], edit:{path:'fixture.txt', before:'old', after:'new'}
})

const sdkPath=process.env.HERMES_CORE_ROOT && resolve(process.env.HERMES_CORE_ROOT,
  'apps/desktop/src/api/canonical-protocol.ts')
const hasCanonicalSDK=Boolean(sdkPath && existsSync(sdkPath))
async function actualSDK() {
  assert.ok(sdkPath,'HERMES_CORE_ROOT must identify the tested host')
  const source=readFileSync(sdkPath,'utf8')
  const code=stripTypeScriptTypes(source,{mode:'transform'})
  const module=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'))
  return new module.CanonicalDesktopProtocol()
}

function fixture(event=null, nativeOwnerPrompt=null) {
  const route={mode:'local',connectionId:'connection-canonical',profile:'claire',targetProfile:'claire'}
  const routes=[route,{mode:'local',connectionId:'connection-canonical',profile:'default',targetProfile:'default'}]
  const reads=[]
  const notices=[]
  const atom=value=>({get:()=>value})
  const api={profileRoutes:async()=>routes,
    requestProfile:async(_route,method)=>{reads.push(method);throw new Error('canonical approval must not use SDK legacy reads/writes')},
    notify:value=>notices.push(value),
    state:{gateway:atom('open'),connectionId:atom(route.connectionId),profile:atom(route.profile)}}
  const monitor=new CompletionMonitor(api,{setTimeout:()=>()=>{}})
  monitor.routes=routes;monitor.routesAt=Date.now();monitor.approvalCapable=true
  const calls=[]
  const nativePrompt=nativeOwnerPrompt ?? event?.payload ?? prompt()
  const descriptor={dispatch_mode:'canonical_native',owner:monitor.replyOwner,
    connectionId:route.connectionId,profile:route.profile,targetProfile:route.targetProfile,
    root_binding:'a'.repeat(64),target_home_binding:'b'.repeat(64),sid:event?.session_id ?? 'opaque-session-ID',
    session_key:'public-stored-session-key',instance_id:'native-instance',authority_epoch:event?.authority_epoch ?? 9,
    replay_epoch:event?.replay_epoch ?? 'native-replay-epoch',transport_protocol:'hermes-gateway-v1',
    prompt_id:nativePrompt.prompt_id,execution_generation:nativePrompt.execution_generation,
    request_fingerprint:'c'.repeat(64),pending_fingerprint:'d'.repeat(64)}
  const pending={session_id:descriptor.sid,prompt_id:descriptor.prompt_id,
    execution_generation:descriptor.execution_generation,instance_id:descriptor.instance_id,
    authority_epoch:descriptor.authority_epoch,replay_epoch:descriptor.replay_epoch}
  monitor.helper=async request=>{
    calls.push(request)
    if(request.action==='canonical_approval_observe')return {ok:true,descriptor,request:nativePrompt,pending}
    if(request.action==='approval_register')return {ok:true,id:'f'.repeat(32),status:'open'}
    if(request.action==='approval_owner_heartbeat')return {ok:true,feature_ready:true,
      version:APPROVAL_COMPAT_VERSION,capability:APPROVAL_CAPABILITY}
    if(request.action==='approval_native_observe')return {ok:true,id:'f'.repeat(32),
      request_fingerprint:descriptor.request_fingerprint,pending_fingerprint:descriptor.pending_fingerprint,
      authority:'e'.repeat(64)}
    if(request.action==='approval_poll')return {ok:true,items:[{id:'f'.repeat(32),claim:'b'.repeat(32),
      deadline:Date.now()/1000+30,descriptor,request:nativePrompt,pending,choice:'once'}],terminal:[]}
    if(request.action==='approval_native_apply')return {ok:true,status:'applied',receipt:'decision_resolved',reason:null}
    if(request.action==='approval_notice')return {ok:true,notification_only:true}
    throw new Error('unexpected helper action '+request.action)
  }
  return {monitor,route,routes,reads,notices,calls,prompt:nativePrompt,descriptor,pending}
}

test('canonical approval.request reaches the SDK event callback and fixed CLI observe/register', async () => {
  const f=fixture()
  const event={type:'approval.request',session_id:f.descriptor.sid,profile:f.route.profile,
    connectionId:f.route.connectionId,authority_epoch:9,replay_epoch:'native-replay-epoch',seq:41,
    payload:f.prompt}
  await f.monitor.onEvent(event)
  const actions=f.calls.map(call=>call.action)
  assert.deepEqual(actions,['canonical_approval_observe','approval_owner_heartbeat','approval_register'])
  assert.deepEqual(f.calls[0].prompt,f.prompt)
  assert.equal(f.calls[0].route.targetProfile,'claire')
  assert.equal(f.calls[1].owner,f.monitor.replyOwner)
  assert.equal(f.calls[1].capability,APPROVAL_CAPABILITY)
  assert.equal(f.calls[2].descriptor.dispatch_mode,'canonical_native')
  assert.equal(f.calls[2].request.method,undefined)
  assert.equal(f.calls[2].request.params,undefined)
  assert.deepEqual(f.reads,[])
  assert.equal([...f.monitor.records.values()][0].approval.id,'f'.repeat(32))
})

test('canonical heartbeat failure is fail-closed but does not poison the next validated read', async () => {
  const f=fixture()
  const event={type:'approval.request',session_id:f.descriptor.sid,profile:f.route.profile,
    connectionId:f.route.connectionId,authority_epoch:9,replay_epoch:'native-replay-epoch',seq:41,
    payload:f.prompt}
  let heartbeats=0
  f.monitor.helper=async request=>{
    f.calls.push(request)
    if(request.action==='canonical_approval_observe')return {ok:true,descriptor:f.descriptor,
      request:f.prompt,pending:f.pending}
    if(request.action==='approval_owner_heartbeat'){
      heartbeats++
      return heartbeats===1
        ? {ok:false,feature_ready:false,version:'1.8.0',capability:'desktop-command-approval-v5'}
        : {ok:true,feature_ready:true,version:APPROVAL_COMPAT_VERSION,capability:APPROVAL_CAPABILITY}
    }
    if(request.action==='approval_register')return {ok:true,id:'f'.repeat(32),status:'open'}
    throw new Error('unexpected helper action '+request.action)
  }
  await f.monitor.onEvent(event)
  const record=[...f.monitor.records.values()][0]
  assert.deepEqual(f.calls.map(call=>call.action),['canonical_approval_observe','approval_owner_heartbeat'])
  assert.equal(record.approval,undefined)
  assert.equal(record.canonicalObservedKey,undefined)
  await f.monitor.checkCanonicalApproval(record)
  assert.deepEqual(f.calls.map(call=>call.action),[
    'canonical_approval_observe','approval_owner_heartbeat',
    'canonical_approval_observe','approval_owner_heartbeat','approval_register'])
  assert.equal(record.approval.id,'f'.repeat(32))
})

test('stale receiver, v4/v5 skew, and approvals-off heartbeats create no canonical card or write', async () => {
  const cases=[
    ['stale receiver',{ok:false,feature_ready:false,version:APPROVAL_COMPAT_VERSION,capability:APPROVAL_CAPABILITY}],
    ['v4 receiver',{ok:true,feature_ready:true,version:APPROVAL_COMPAT_VERSION,capability:'desktop-command-approval-v4'}],
    ['v5 receiver',{ok:true,feature_ready:true,version:APPROVAL_COMPAT_VERSION,capability:'desktop-command-approval-v5'}],
    ['approvals disabled',{ok:false,feature_ready:false,reason:'approvals_disabled'}],
  ]
  for(const [label,heartbeat] of cases){
    const f=fixture()
    const event={type:'approval.request',session_id:f.descriptor.sid,profile:f.route.profile,
      connectionId:f.route.connectionId,authority_epoch:9,replay_epoch:'native-replay-epoch',seq:41,
      payload:f.prompt}
    f.monitor.helper=async request=>{
      f.calls.push(request)
      if(request.action==='canonical_approval_observe')return {ok:true,descriptor:f.descriptor,
        request:f.prompt,pending:f.pending}
      if(request.action==='approval_owner_heartbeat')return heartbeat
      if(request.action==='approval_register')return {ok:true,id:'f'.repeat(32),status:'open'}
      if(request.action==='approval_native_apply')return {ok:true,status:'applied'}
      throw new Error('unexpected helper action '+request.action)
    }
    await f.monitor.onEvent(event)
    const record=[...f.monitor.records.values()][0]
    assert.deepEqual(f.calls.map(call=>call.action),
      ['canonical_approval_observe','approval_owner_heartbeat'],label)
    assert.equal(record.approval,undefined,label)
    assert.equal(record.canonicalObservedKey,undefined,label)
    assert.equal(f.calls.some(call=>call.action==='approval_native_apply'),false,label)
    assert.equal(f.notices.length,0,label)
  }
})

test('native opaque ID reused at a new generation retires the old card without legacy mode downgrade', async () => {
  const f=fixture()
  const record={key:JSON.stringify([f.route.connectionId,f.route.profile,f.descriptor.sid]),
    profile:f.route.profile,connectionId:f.route.connectionId,sid:f.descriptor.sid,
    generation:0,candidate:null,cancel:null,todos:null,seq:40,settledFinals:new Set(),checking:false,
    retries:0,ask:null,askChecking:false,canonicalAsk:{prompt:f.prompt,seq:40,
      replayEpoch:'native-replay-epoch',authorityEpoch:9},canonicalSettled:null,
    approval:{id:'f'.repeat(32),descriptor:f.descriptor,request:f.prompt,pending:f.pending,
      route:{...f.route}}}
  f.monitor.records.set(record.key,record)
  const next=prompt(8)
  const nextDescriptor={...f.descriptor,execution_generation:8}
  let observedRequest
  f.monitor.helper=async request=>{
    if(request.action==='canonical_approval_observe'){
      observedRequest=request
      return {ok:true,descriptor:nextDescriptor,request:next,pending:{...f.pending,execution_generation:8}}
    }
    return {ok:false,reason:'unexpected_action'}
  }
  await f.monitor.onEvent({type:'approval.request',session_id:f.descriptor.sid,
    profile:f.route.profile,connectionId:f.route.connectionId,authority_epoch:9,
    replay_epoch:'native-replay-epoch',seq:41,payload:next})
  assert.equal(observedRequest.prompt.prompt_id,'opaque.native.ID/reused')
  assert.equal(observedRequest.prompt.execution_generation,8)
  assert.deepEqual([...f.monitor.approvalSweepAbsent],['f'.repeat(32)])
  assert.equal(record.approval.id,'f'.repeat(32))
  assert.equal(record.canonicalSettled,null)
  assert.equal(record.canonicalAsk.prompt.execution_generation,8)
  assert.equal(record.approval.descriptor.dispatch_mode,'canonical_native')
  assert.deepEqual(f.reads,[])
})

test('canonical poll claim uses the helper-owned native four-field write and never SDK approval.respond/settle', async () => {
  const f=fixture()
  const record={key:'record',profile:f.route.profile,connectionId:f.route.connectionId,sid:f.descriptor.sid,
    generation:0,candidate:null,cancel:null,todos:null,seq:41,settledFinals:new Set(),checking:false,
    retries:0,ask:{id:f.prompt.prompt_id,method:'approval'},askChecking:false,
    approval:{id:'f'.repeat(32),descriptor:f.descriptor,request:f.prompt,pending:f.pending,
      requestFingerprint:f.descriptor.request_fingerprint,pendingFingerprint:f.descriptor.pending_fingerprint,
      route:{...f.route}}}
  f.monitor.records.set(record.key,record)
  await f.monitor.pumpApprovals()
  const actions=f.calls.map(call=>call.action)
  assert.equal(actions.filter(action=>action==='approval_native_observe').length,1)
  assert.equal(actions.filter(action=>action==='approval_native_apply').length,1)
  assert.equal(actions.includes('approval_claim_valid'),false)
  assert.equal(actions.includes('approval_settle'),false)
  assert.equal(f.reads.length,0)
  assert.equal(f.monitor.records.get(record.key).approval,null)
  assert.equal(f.monitor.lastApprovalPollSucceeded,true)
  assert.equal(f.notices.length,0)
  assert.equal(stableJSON(f.descriptor),stableJSON(record.approval?.descriptor ?? f.descriptor))
})

test('settled native event retires only matching prompt generation and does not synthesize srq', async () => {
  const f=fixture()
  const record={key:JSON.stringify([f.route.connectionId,f.route.profile,f.descriptor.sid]),
    profile:f.route.profile,connectionId:f.route.connectionId,sid:f.descriptor.sid,
    generation:0,candidate:null,cancel:null,todos:null,seq:41,settledFinals:new Set(),checking:false,
    retries:0,ask:{id:f.prompt.prompt_id,method:'approval'},askChecking:false,
    canonicalAsk:{prompt:f.prompt,seq:41,replayEpoch:f.descriptor.replay_epoch,authorityEpoch:9},
    canonicalSettled:null,approval:{id:'f'.repeat(32),descriptor:f.descriptor}}
  f.monitor.records.set(record.key,record)
  await f.monitor.onEvent({type:'approval.settled',session_id:f.descriptor.sid,
    profile:f.route.profile,connectionId:f.route.connectionId,payload:{prompt_id:f.prompt.prompt_id,
      execution_generation:f.prompt.execution_generation}})
  assert.equal(record.canonicalAsk,null)
  assert.equal(record.canonicalSettled.prompt_id,'opaque.native.ID/reused')
  assert.equal(record.canonicalSettled.execution_generation,7)
  assert.deepEqual(f.calls,[])
  assert.deepEqual(f.reads,[])
})

test('matching SDK request_id alias is removed only on a detached native prompt copy', async () => {
  const native=prompt(7,' opaque.native.ID/reused ')
  const event={type:'approval.request',session_id:'opaque-session-ID',profile:'claire',
    payload:{...structuredClone(native),request_id:native.prompt_id}}
  const f=fixture(event,native)
  await f.monitor.onEvent(event)
  assert.deepEqual(f.calls.map(call=>call.action),['canonical_approval_observe','approval_owner_heartbeat','approval_register'])
  assert.deepEqual(f.calls[0].prompt,native)
  assert.equal(f.calls[0].prompt.prompt_id,' opaque.native.ID/reused ')
  assert.equal(f.calls[0].prompt.execution_generation,7)
  assert.notStrictEqual(f.calls[0].prompt.edit,event.payload.edit)
  assert.equal(event.payload.request_id,native.prompt_id,'never mutate the host SDK payload')
  event.payload.edit.before='later host mutation'
  assert.equal(f.calls[0].prompt.edit.before,'old','review data must be detached from later host mutation')
})

test('mismatched SDK alias cannot observe or register another native prompt', async () => {
  const native=prompt()
  const event={type:'approval.request',session_id:'opaque-session-ID',profile:'claire',
    payload:{...native,request_id:'another-prompt'}}
  const f=fixture(event,native)
  await f.monitor.onEvent(event)
  assert.deepEqual(f.calls,[])
  assert.deepEqual(f.reads,[])
})

test('SDK alias removal never drops unknown review fields to manufacture a valid native prompt', async () => {
  const native=prompt()
  const event={type:'approval.request',session_id:'opaque-session-ID',profile:'claire',
    payload:{...native,request_id:native.prompt_id,future_review:{must_not_disappear:true}}}
  const f=fixture(event,native)
  f.monitor.helper=async request=>{
    f.calls.push(request)
    assert.equal(request.action,'canonical_approval_observe')
    assert.deepEqual(request.prompt.future_review,{must_not_disappear:true})
    assert.equal(Object.hasOwn(request.prompt,'request_id'),false)
    return {ok:false,reason:'invalid_native_candidate'}
  }
  await f.monitor.onEvent(event)
  assert.deepEqual(f.calls.map(call=>call.action),['canonical_approval_observe'])
  assert.deepEqual(f.reads,[])
})

test('actual installed canonical SDK transformation preserves the native observer contract',
  {skip:!hasCanonicalSDK && process.env.HERMES_CORE_GENERATION!=='current'}, async () => {
  const native=prompt()
  const event={type:'approval.request',session_id:'opaque-session-ID',profile:'claire',
    payload:structuredClone(native)}
  ;(await actualSDK()).event(event)
  assert.equal(event.payload.request_id,native.prompt_id)
  const f=fixture(event,native)
  await f.monitor.onEvent(event)
  assert.deepEqual(f.calls.map(call=>call.action),['canonical_approval_observe','approval_owner_heartbeat','approval_register'])
  assert.deepEqual(f.calls[0].prompt,native)
  assert.equal(event.payload.request_id,native.prompt_id)
  assert.deepEqual(f.reads,[])
})

test('actual core approval event enters the SDK callback adapter and fixed CLI observer', {skip:!process.env.HERMES_CANONICAL_EVENT_JSON}, async () => {
  const event=JSON.parse(process.env.HERMES_CANONICAL_EVENT_JSON)
  assert.equal(event.type,'approval.request')
  assert.equal(event.payload?.kind,'approval')
  const native=structuredClone(event.payload)
  ;(await actualSDK()).event(event)
  assert.equal(event.payload.request_id,native.prompt_id)
  const f=fixture(event,native)
  await f.monitor.onEvent(event)
  const actions=f.calls.map(call=>call.action)
  assert.deepEqual(actions,['canonical_approval_observe','approval_owner_heartbeat','approval_register'])
  assert.deepEqual(f.calls[0].prompt,native)
  assert.equal(f.calls[0].route.targetProfile,'claire')
  assert.equal(f.calls[2].descriptor.dispatch_mode,'canonical_native')
  assert.deepEqual(f.reads,[])
})

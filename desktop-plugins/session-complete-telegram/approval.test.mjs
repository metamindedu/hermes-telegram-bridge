// Desktop approval contracts: RPC transport is mocked, native queue semantics
// and Telegram Application dispatch are exercised in test_approval_bridge.py.
import test from 'node:test'
import assert from 'node:assert/strict'
import {registerHooks} from 'node:module'

registerHooks({resolve(specifier, context, nextResolve) {
  if (specifier === '@hermes/plugin-sdk') return {url:'data:text/javascript,export const host = {}', shortCircuit:true}
  return nextResolve(specifier)
}})
const {CompletionMonitor, validateApprovalPair, validApprovalSnapshot, approvalFingerprint,
  APPROVAL_CAPABILITY, VERSION} = await import('./desktop/plugin.js')

function fixture() {
  const request={id:'srq-aabbcc112233',method:'approval',params:{session_id:'runtime-1',request_id:'a'.repeat(32),
    command:'rm -rf /tmp/example',description:'recursive delete',pattern_key:'recursive delete',
    pattern_keys:['recursive delete'],allow_permanent:true,allow_session:true,
    choices:['once','session','always','deny']}}
  const pending={request_id:'a'.repeat(32),command:request.params.command,description:request.params.description,
    pattern_key:request.params.pattern_key,pattern_keys:['recursive delete'],allow_permanent:true,allow_session:true}
  const route={mode:'local',connectionId:'conn-1',profile:'default',targetProfile:'default'}
  const row={id:'runtime-1',session_key:'durable-1',status:'waiting'}
  const snapshot={epoch:'epoch-1',open_requests:[request]}
  const owner='desktop-owner'
  return {request,pending,route,row,snapshot,owner}
}

async function bridgeFixture() {
  const f=fixture()
  const descriptor={owner:f.owner,connectionId:f.route.connectionId,profile:f.route.profile,
    targetProfile:f.route.targetProfile,sid:f.row.id,session_key:f.row.session_key,epoch:'epoch-1',
    request_id:f.request.id,queue_id:f.pending.request_id,
    request_fingerprint:await approvalFingerprint(f.request),pending_fingerprint:await approvalFingerprint(f.pending)}
  return {...f,descriptor,bridge:{id:'approval-row-1',descriptor,request:f.request,pending:f.pending,
    requestFingerprint:descriptor.request_fingerprint,pendingFingerprint:descriptor.pending_fingerprint,
    route:f.route}}
}

test('approval pump without an owned card leaves ordinary notification sweeps untouched', async () => {
  const f=fixture()
  const api={state:{profile:{get:()=> 'default'}},profileRoutes:async()=>[f.route],notify:()=>{}}
  const monitor=new CompletionMonitor(api,{setTimeout:()=>()=>{}})
  monitor.records.set('ordinary',{sid:f.row.id})
  const calls=[]
  monitor.helper=async request=>{calls.push(request);return {ok:false}}
  await monitor.pumpApprovals()
  assert.deepEqual(calls,[])
})

test('only the exact request/queue pair for a dangerous command is accepted', () => {
  const f=fixture()
  assert.equal(validateApprovalPair(f.request,f.pending,f.row.id),true)
  for (const mutate of [
    p=>{p.method='sudo'}, p=>{p.id='srq-evil'}, p=>{p.params.session_id='other'},
    p=>{p.params.request_id='b'.repeat(32)}, p=>{p.params.pattern_keys=['plugin_rule:x']},
    p=>{p.params.pattern_key='execute_code'}, p=>{p.params.allow_session=false},
    p=>{p.params.smart_denied=true}, p=>{p.params.command+=' changed'},
    p=>{p.params.extra='not-native'}
  ]) {
    const request=structuredClone(f.request); mutate(request)
    assert.equal(validateApprovalPair(request,f.pending,f.row.id),false)
  }
  const plugin=structuredClone(f.pending);plugin.pattern_key='plugin_rule:abc';plugin.pattern_keys=[plugin.pattern_key]
  const pluginRequest=structuredClone(f.request);pluginRequest.params.pattern_key=plugin.pattern_key;pluginRequest.params.pattern_keys=[plugin.pattern_key]
  assert.equal(validateApprovalPair(pluginRequest,plugin,f.row.id),false)
  const code=structuredClone(f.pending);code.pattern_key='execute_code';code.pattern_keys=[code.pattern_key]
  const codeRequest=structuredClone(f.request);codeRequest.params.pattern_key=code.pattern_key;codeRequest.params.pattern_keys=[code.pattern_key]
  assert.equal(validateApprovalPair(codeRequest,code,f.row.id),false)
})

test('route/runtime/durable/epoch/request/queue/fingerprint authority dimensions fail independently', async () => {
  const f=await bridgeFixture()
  assert.equal(validApprovalSnapshot(f.bridge,f.route,f.row,f.snapshot,[f.pending],f.owner),true)
  const changedRoute={...f.route,connectionId:'other'}
  assert.equal(validApprovalSnapshot(f.bridge,changedRoute,f.row,f.snapshot,[f.pending],f.owner),false)
  for (const field of ['profile','targetProfile']) {
    assert.equal(validApprovalSnapshot(f.bridge,{...f.route,[field]:'other'},f.row,f.snapshot,[f.pending],f.owner),false)
  }
  assert.equal(validApprovalSnapshot(f.bridge,f.route,{...f.row,id:'other'},f.snapshot,[f.pending],f.owner),false)
  assert.equal(validApprovalSnapshot(f.bridge,f.route,{...f.row,session_key:'other'},f.snapshot,[f.pending],f.owner),false)
  assert.equal(validApprovalSnapshot(f.bridge,f.route,f.row,{...f.snapshot,epoch:'other'},[f.pending],f.owner),false)
  const wrongRequest={...f.request,id:'srq-ffeeddccbbaa'}
  assert.equal(validApprovalSnapshot(f.bridge,f.route,f.row,{...f.snapshot,open_requests:[wrongRequest]},[f.pending],f.owner),false)
  const wrongPending={...f.pending,request_id:'b'.repeat(32)}
  assert.equal(validApprovalSnapshot(f.bridge,f.route,f.row,f.snapshot,[wrongPending],f.owner),false)
  assert.equal(validApprovalSnapshot(f.bridge,f.route,f.row,f.snapshot,[f.pending],'other-owner'),false)
  for (const key of ['request_fingerprint','pending_fingerprint']) {
    const descriptor={...f.descriptor,[key]:'0'.repeat(64)}
    const bridge={...f.bridge,descriptor}
    assert.equal(validApprovalSnapshot(bridge,f.route,f.row,f.snapshot,[f.pending],f.owner),false,key)
  }
})

test('approval registration freezes full fingerprints and binds queue not just session', async () => {
  const f=await bridgeFixture()
  const api={state:{profile:{get:()=> 'default'}},profileRoutes:async()=>[f.route],notify:()=>{},
    requestProfile:async(_route,method)=>method==='approval.pending'?{approvals:[f.pending]}:{}}
  const monitor=new CompletionMonitor(api,{setTimeout:()=>()=>{}})
  monitor.replyOwner=f.owner;monitor.approvalCapable=true
  const actions=[]
  monitor.helper=async request=>{actions.push(request);if(request.action==='approval_owner_heartbeat')
    return {ok:true,feature_ready:true,version:VERSION,capability:APPROVAL_CAPABILITY}
    return {ok:true,new:true,id:'stored-card',status:'open'}}
  const record={sid:f.row.id,approval:null,askChecking:false}
  assert.equal(validateApprovalPair(f.request,f.pending,f.row.id),true)
  const handled=await monitor.checkApproval(record,f.route,f.row,f.snapshot,f.request)
  assert.equal(handled,true,JSON.stringify({actions,request:f.request,pending:f.pending,row:f.row}))
  assert.equal(record.approval.descriptor.queue_id,f.pending.request_id)
  assert.equal(record.approval.descriptor.request_id,f.request.id)
  assert.equal(record.approval.descriptor.request_fingerprint,await approvalFingerprint(f.request))
  assert.equal(actions[1].action,'approval_register')
  assert.equal(actions[1].pending.request_id,f.pending.request_id)
})

async function runPump(resolved, {throwRpc=false, changeOnFinal=false, claimValidity=true,
  gateVariant=null, pausePostClaim=false, runStoppedOnGate=false, expireOnGate=false}={}) {
  const f=await bridgeFixture();const owner=f.owner
  const item={id:'stored-card',claim:'b'.repeat(32),deadline:Date.now()/1000+30,descriptor:f.descriptor,
    request:f.request,pending:f.pending,choice:'once'}
  const calls=[];const helperCalls=[];let snapshots=0
  let resumeSnapshot
  let reachSnapshot
  const postClaimSnapshotReached=new Promise(resolve=>{reachSnapshot=resolve})
  const resumePostClaimSnapshot=new Promise(resolve=>{resumeSnapshot=resolve})
  const api={state:{profile:{get:()=> 'default'}},profileRoutes:async()=>[f.route],notify:()=>{},
    requestProfile:async(route,method,args)=>{
      calls.push({route,method,args})
      if(method==='session.active_list')return {sessions:[f.row]}
      if(method==='session.events.since')return f.snapshot
      if(method==='approval.pending')return {approvals:[f.pending]}
      if(method==='approval.respond') {if(throwRpc)throw new Error('synthetic timeout');return resolved}
      throw new Error('unexpected RPC '+method)
    }}
  const monitor=new CompletionMonitor(api,{setTimeout:()=>()=>{}})
  monitor.replyOwner=owner
  const bridge={id:item.id,descriptor:f.descriptor,request:f.request,pending:f.pending,
    requestFingerprint:f.descriptor.request_fingerprint,pendingFingerprint:f.descriptor.pending_fingerprint,route:f.route}
  monitor.records.set('record',{approval:bridge})
  monitor.approvalSnapshot=async()=>{
    snapshots++
    if(pausePostClaim&&snapshots===2){reachSnapshot();await resumePostClaimSnapshot}
    if(changeOnFinal && snapshots===2)return null
    return {route:f.route,row:f.row,snapshot:f.snapshot,request:f.request,pending:f.pending}
  }
  monitor.helper=async request=>{
    helperCalls.push(request)
    if(request.action==='approval_owner_heartbeat')return {ok:true,feature_ready:true,version:VERSION,capability:APPROVAL_CAPABILITY}
    if(request.action==='approval_poll')return {ok:true,items:[item],terminal:[]}
    if(request.action==='approval_claim_valid'){
      if(runStoppedOnGate)monitor.stopped=true
      if(expireOnGate)Date.now=()=>item.deadline*1000
      const receipt={ok:true,valid:claimValidity,id:item.id,claim:item.claim,
        authority:await approvalFingerprint(item.descriptor),deadline:item.deadline}
      if(gateVariant==='id')receipt.id='wrong-id'
      if(gateVariant==='claim')receipt.claim='wrong-claim'
      if(gateVariant==='authority')receipt.authority='0'.repeat(64)
      if(gateVariant==='deadline')receipt.deadline+=1
      if(gateVariant==='shape')receipt.extra='not-typed'
      return receipt
    }
    if(request.action==='approval_settle')return {ok:true,status:request.status}
    throw new Error('unexpected helper '+request.action)
  }
  const pump=monitor.pumpApprovals()
  if(!pausePostClaim)await pump
  return {calls,helperCalls,monitor,pump,postClaimSnapshotReached,
    resumePostClaimSnapshot:()=>resumeSnapshot()}
}

test('resolved 0 is expired, exact queue id/all false, and there is no request.answer fallback', async () => {
  const out=await runPump({resolved:0})
  const writes=out.calls.filter(call=>call.method==='approval.respond')
  assert.equal(writes.length,1)
  assert.deepEqual(writes[0].args,{session_id:'runtime-1',request_id:'a'.repeat(32),choice:'once',all:false})
  assert.equal(out.calls.some(call=>call.method==='request.answer'),false)
  const settle=out.helperCalls.find(call=>call.action==='approval_settle')
  assert.equal(settle.status,'expired')
})

test('malformed numeric/bool response receipts are unknown and never retried', async () => {
  for (const receipt of [{resolved:true},{resolved:'1'},{resolved:2},{resolved:1,all:true}]) {
    const out=await runPump(receipt)
    assert.equal(out.calls.filter(call=>call.method==='approval.respond').length,1)
    assert.equal(out.calls.some(call=>call.method==='request.answer'),false)
    assert.equal(out.helperCalls.find(call=>call.action==='approval_settle').status,'unknown')
  }
})

test('final fresh snapshot race rejects without native write; transport ambiguity is terminal unknown', async () => {
  const raced=await runPump({resolved:1},{changeOnFinal:true})
  assert.equal(raced.calls.filter(call=>call.method==='approval.respond').length,0)
  assert.equal(raced.helperCalls.find(call=>call.action==='approval_settle').status,'rejected')
  const ambiguous=await runPump({},{throwRpc:true})
  assert.equal(ambiguous.calls.filter(call=>call.method==='approval.respond').length,1)
  assert.equal(ambiguous.calls.some(call=>call.method==='request.answer'),false)
  assert.equal(ambiguous.helperCalls.filter(call=>call.action==='approval_poll').length,1)
  assert.equal(ambiguous.helperCalls.find(call=>call.action==='approval_settle').status,'unknown')
})

test('old sender/renderer capability cannot enable approval registration', async () => {
  const f=fixture();const api={state:{profile:{get:()=> 'default'}},profileRoutes:async()=>[f.route],notify:()=>{}}
  const monitor=new CompletionMonitor(api,{setTimeout:()=>()=>{}})
  const actions=[];monitor.helper=async req=>{actions.push(req);return {ok:true,version:'1.5.0',capabilities:[]}}
  await monitor.mounted()
  assert.equal(monitor.approvalCapable,false)
  const record={sid:f.row.id,approval:null}
  assert.equal(await monitor.checkApproval(record,f.route,f.row,f.snapshot,f.request),false)
  assert.equal(actions.length,1)
  assert.equal(actions[0].action,'loaded')
})
test('approval_claim_valid is explicitly dispatched but remains a read-only non-ambiguous helper action', async () => {
  const f=fixture();const transported=[]
  const api={profileRoutes:async()=>[f.route],requestProfile:async(_route,method,args)=>{
    assert.equal(method,'cli.exec')
    const request=JSON.parse(Buffer.from(args.argv[4],'base64url').toString('utf8'))
    transported.push(request)
    const receipt={ok:true,valid:true,id:request.id,claim:request.claim,authority:request.authority,deadline:request.deadline}
    const output=['HERMES_TELEGRAM_BRIDGE_RESULT_V1_BEGIN',Buffer.from(JSON.stringify(receipt)).toString('base64url'),
      'HERMES_TELEGRAM_BRIDGE_RESULT_V1_END',''].join('\n')
    return {blocked:false,code:0,output}
  }}
  const monitor=new CompletionMonitor(api,{setTimeout:()=>()=>{}})
  const request={action:'approval_claim_valid',id:'a'.repeat(32),claim:'b'.repeat(32),authority:'c'.repeat(64),deadline:123}
  const receipt=await monitor.helper(request)
  assert.deepEqual(transported,[request]);assert.equal(receipt.valid,true)
  api.requestProfile=async()=>{throw new Error('synthetic transport outage')}
  await assert.rejects(monitor.helper(request),error=>error?.ambiguousWrite===false)
})

test('new pre-dispatch authorization contract requires 1.6.1 approval-v2 handshake', () => {
  assert.equal(VERSION,'1.6.1')
  assert.equal(APPROVAL_CAPABILITY,'desktop-command-approval-v2')
})

test('claim validity is the last fresh gate before approval.respond and accepts only an exact typed receipt', async () => {
  const positive=await runPump({resolved:1})
  assert.equal(positive.helperCalls.filter(call=>call.action==='approval_claim_valid').length,1)
  assert.equal(positive.calls.filter(call=>call.method==='approval.respond').length,1)
  assert.equal(positive.calls.some(call=>call.method==='request.answer'),false)
  for(const gateVariant of ['id','claim','authority','deadline','shape']){
    const invalid=await runPump({resolved:1},{gateVariant})
    assert.equal(invalid.calls.filter(call=>call.method==='approval.respond').length,0,gateVariant)
    assert.equal(invalid.calls.some(call=>call.method==='request.answer'),false,gateVariant)
    assert.equal(invalid.helperCalls.find(call=>call.action==='approval_settle').status,'rejected',gateVariant)
  }
  const lost=await runPump({resolved:1},{claimValidity:false})
  assert.equal(lost.calls.filter(call=>call.method==='approval.respond').length,0)
  assert.equal(lost.helperCalls.find(call=>call.action==='approval_settle').status,'rejected')
})

test('post-claim snapshot barrier rechecks boot-change and ingress-disable gates without replay', async () => {
  for(const cause of ['receiver-boot-changed','ingress-disabled']){
    const out=await runPump({resolved:1},{pausePostClaim:true,claimValidity:false})
    await out.postClaimSnapshotReached
    // Real SQLite receiver/enablement flips are exercised by test_approval_bridge;
    // this barrier verifies the renderer never starts approval.respond afterward.
    out.resumePostClaimSnapshot()
    await out.pump
    await out.monitor.pumpApprovals()
    assert.equal(out.calls.filter(call=>call.method==='approval.respond').length,0,cause)
    assert.equal(out.helperCalls.filter(call=>call.action==='approval_poll').length,1,cause)
    assert.equal(out.helperCalls.filter(call=>call.action==='approval_claim_valid').length,1,cause)
    assert.equal(out.calls.some(call=>call.method==='request.answer'),false,cause)
  }
})

test('stop and deadline are rechecked after a valid native claim gate before RPC', async () => {
  const originalNow=Date.now
  try{
    const stopped=await runPump({resolved:1},{runStoppedOnGate:true})
    assert.equal(stopped.calls.filter(call=>call.method==='approval.respond').length,0)
    assert.equal(stopped.helperCalls.find(call=>call.action==='approval_settle').status,'rejected')
    Date.now=originalNow
    const expired=await runPump({resolved:1},{expireOnGate:true})
    assert.equal(expired.calls.filter(call=>call.method==='approval.respond').length,0)
    assert.equal(expired.helperCalls.find(call=>call.action==='approval_settle').status,'rejected')
  }finally{Date.now=originalNow}
})

test('unsafe approval registration reasons from checkAsk only reach notification-only approval_notice', async () => {
  const reasons=['review_text_missing','redaction_not_stable','redaction_omission','redaction_collision',
    'review_unicode_ambiguous','review_control_character','review_card_too_large','review_unavailable',
    'timeout_unavailable','invalid_review_text','approval_result_too_large']
  for(const reason of reasons){
    const f=fixture();const calls=[];const notices=[]
    const command='rm -rf /tmp/example && echo synthetic-secret-value-739102'
    const request={id:'srq-aabbcc112233',method:'approval',params:{session_id:f.row.id,request_id:'a'.repeat(32),
      command,description:'recursive delete',pattern_key:'recursive delete',pattern_keys:['recursive delete'],
      allow_permanent:true,allow_session:true,choices:['once','session','always','deny']}}
    const pending={request_id:'a'.repeat(32),command,description:'recursive delete',pattern_key:'recursive delete',
      pattern_keys:['recursive delete'],allow_permanent:true,allow_session:true}
    const api={state:{profile:{get:()=> 'default'}},profileRoutes:async()=>[f.route],notify:notice=>notices.push(notice),
      requestProfile:async(_route,method)=>{
        if(method==='session.events.since')return {epoch:'epoch-1',open_requests:[request]}
        if(method==='approval.pending')return {approvals:[pending]}
        throw new Error('unexpected RPC '+method)
      }}
    const monitor=new CompletionMonitor(api,{setTimeout:()=>()=>{}})
    monitor.approvalCapable=true;monitor.replyOwner='owner'
    monitor.helper=async payload=>{
      calls.push(payload)
      if(payload.action==='approval_owner_heartbeat')return {ok:true,feature_ready:true,version:VERSION,capability:APPROVAL_CAPABILITY}
      if(payload.action==='approval_register')return {ok:false,reason}
      if(payload.action==='approval_notice')return {ok:true,status:'sent',notification_only:true}
      if(payload.action==='attention')return {ok:true,status:'sent'}
      throw new Error('unexpected helper '+payload.action)
    }
    const r={sid:f.row.id,ask:null,approval:null,askChecking:false}
    await monitor.checkAsk(r,f.route,{...f.row,status:'waiting'},
      {epoch:'epoch-1',open_requests:[request]},request)
    assert.deepEqual(calls.map(call=>call.action),['approval_owner_heartbeat','approval_register','approval_notice'],reason)
    assert.equal(calls.some(call=>call.action==='attention'),false,reason)
    assert.equal(calls.find(call=>call.action==='approval_notice').reason,reason)
    assert.equal(JSON.stringify(notices).includes(command),false,reason)
    assert.equal(JSON.stringify(notices).includes('synthetic-secret-value-739102'),false,reason)
    assert.equal(r.ask.id,request.id,reason)
  }
})

test('disconnected receiver cannot poll or apply an already clicked decision', async () => {
  const f=await bridgeFixture();const calls=[]
  const api={state:{profile:{get:()=> 'default'}},profileRoutes:async()=>[f.route],notify:()=>{},requestProfile:async()=>{calls.push('rpc')}}
  const m=new CompletionMonitor(api,{setTimeout:()=>()=>{}});m.replyOwner=f.owner
  m.records.set('r',{sid:f.row.id,approval:{id:'stored',descriptor:f.descriptor,request:f.request,pending:f.pending,
    requestFingerprint:f.descriptor.request_fingerprint,pendingFingerprint:f.descriptor.pending_fingerprint}})
  const actions=[];m.helper=async r=>{actions.push(r);return {ok:false,feature_ready:false}}
  await m.pumpApprovals()
  assert.equal(actions.some(r=>r.action==='approval_poll'),false)
  assert.deepEqual(calls,[])
})


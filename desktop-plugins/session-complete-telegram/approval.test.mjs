// Desktop approval contracts: RPC transport is mocked, native queue semantics
// and Telegram Application dispatch are exercised in test_approval_bridge.py.
import test from 'node:test'
import assert from 'node:assert/strict'
import {registerHooks} from 'node:module'

registerHooks({resolve(specifier, context, nextResolve) {
  if (specifier === '@hermes/plugin-sdk') return {url:'data:text/javascript,export const host = {}', shortCircuit:true}
  if (specifier === 'react/jsx-runtime') return {url: 'data:text/javascript,export const jsx=(type,props,key)=>({type,props,key})', shortCircuit:true}
  return nextResolve(specifier, context)
}})
const {CompletionMonitor, validateApprovalPair, validApprovalSnapshot, approvalFingerprint, classifyApprovalAuthority,
  APPROVAL_CAPABILITY, APPROVAL_COMPAT_VERSION, VERSION} = await import('./desktop/plugin.js')

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
    request_id:f.request.id,queue_id:f.pending.request_id,dispatch_mode:'legacyqueue',
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

test('native approval authority is the exact request/queue envelope, not detector membership', () => {
  const f=fixture()
  assert.equal(validateApprovalPair(f.request,f.pending,f.row.id),true)
  for (const mutate of [
    p=>{p.method='sudo'}, p=>{p.id='srq-evil'}, p=>{p.params.session_id='other'},
    p=>{p.params.request_id='b'.repeat(32)}, p=>{p.params.command+=' changed'},
    p=>{p.params.extra='not-in-queue'}
  ]) {
    const request=structuredClone(f.request); mutate(request)
    assert.equal(validateApprovalPair(request,f.pending,f.row.id),false)
  }
  for (const key of ['plugin_rule:fixture','execute_code','tirith:secret','future_native_rule']) {
    const pending={...f.pending,pattern_key:key,pattern_keys:[key]}
    const request=structuredClone(f.request)
    request.params.pattern_key=key;request.params.pattern_keys=[key]
    assert.equal(validateApprovalPair(request,pending,f.row.id),true,key)
  }
  const scope=structuredClone(f.request);scope.params.allow_session=false
  assert.equal(validateApprovalPair(scope,f.pending,f.row.id),false)
})

test('all native producer families preserve full payloads and reject malformed scope choices', () => {
  const f=fixture()
  const cases=[
    ['execute_code',"execute_code <<'PY'\\nprint(\"full body\")\\nPY",{allow_permanent:true,allow_session:true}],
    ['protected_instruction_file','<write to AGENTS.md>',{allow_permanent:false,allow_session:false}],
    ['ssh_config_write','<write to ~/.ssh/config>',{allow_permanent:true,allow_session:true}],
    ['plugin_rule:fixture','<fixture_tool> (plugin approval rule)',{allow_permanent:true,allow_session:true}],
    ['cua:click:foreground','computer_use: click at 40,20',{allow_permanent:true,allow_session:true}],
    ['recursive delete','rm -rf /tmp/example',{allow_permanent:false,allow_session:false,smart_denied:true}],
    ['mcp_elicitation','Request to publish the package',{choices:['once','session','always','deny']}],
    ['editor-'+'e'.repeat(32),'Edit src/example.ts',{allow_permanent:false,allow_session:false,
      edit:{path:'src/example.ts',old_text:'old line\\n',new_text:'new line\\n',arguments:{diff:'-old line\\n+new line'}}}]
  ]
  for(const [key,command,extra] of cases){
    const pending={request_id:f.pending.request_id,command,description:`native ${key} summary`,pattern_key:key,pattern_keys:[key],...extra}
    const choices=extra.choices || (extra.smart_denied || extra.allow_session===false ? ['once','deny'] : ['once','session','always','deny'])
    const request={id:f.request.id,method:'approval',params:{session_id:f.row.id,request_id:f.pending.request_id,
      command,description:pending.description,pattern_key:key,pattern_keys:[key],choices,...extra}}
    assert.equal(validateApprovalPair(request,pending,f.row.id),true,key)
  }
  for(const choices of [['once','deny','deny'],['once','session','always','maybe','deny'],['deny'],null]){
    const request=structuredClone(f.request);request.params.choices=choices
    const pending={...f.pending,choices}
    assert.equal(validateApprovalPair(request,pending,f.row.id),false,JSON.stringify(choices))
  }
  const contradiction=structuredClone(f.request);contradiction.params.allow_session=false
  contradiction.params.choices=['once','session','deny']
  const queue={...f.pending,allow_session:false,choices:contradiction.params.choices}
  assert.equal(validateApprovalPair(contradiction,queue,f.row.id),false)
})

test('healthy empty queue freezes server-request authority; failures and conflicts never downgrade', () => {
  const f=fixture()
  const empty=classifyApprovalAuthority(f.request,{approvals:[]},f.row.id)
  assert.equal(empty?.mode,'legacyserver_request')
  assert.deepEqual(empty?.pending,{request_id:f.request.params.request_id,missing:true})
  for(const queue of [null,{}, {approvals:'bad'}, {approvals:[{}]},
    {approvals:[f.pending,f.pending]},
    {approvals:[{...f.pending,command:'conflicting native command'}]},
    {approvals:[{...f.pending,request_id:'b'.repeat(32)}]}]){
    assert.equal(classifyApprovalAuthority(f.request,queue,f.row.id),null,JSON.stringify(queue))
  }
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
    return {ok:true,feature_ready:true,version:APPROVAL_COMPAT_VERSION,capability:APPROVAL_CAPABILITY}
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
  gateVariant=null, pausePostClaim=false, runStoppedOnGate=false, expireOnGate=false,
  dispatchMode='legacyqueue', modeChangeOnFinal=false}={}) {
  const f=await bridgeFixture();const owner=f.owner
  const pending=dispatchMode==='legacyqueue' ? f.pending : {request_id:f.request.params.request_id,missing:true}
  const descriptor={...f.descriptor,dispatch_mode:dispatchMode,
    pending_fingerprint:await approvalFingerprint(pending)}
  const item={id:'stored-card',claim:'b'.repeat(32),deadline:Date.now()/1000+30,descriptor,
    request:f.request,pending,choice:'once'}
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
      if(method==='approval.pending')return {approvals:dispatchMode==='legacyqueue'?[f.pending]:[]}
      if(method==='approval.respond'||method==='request.answer') {if(throwRpc)throw new Error('synthetic timeout');return resolved}
      throw new Error('unexpected RPC '+method)
    }}
  const monitor=new CompletionMonitor(api,{setTimeout:()=>()=>{}})
  monitor.replyOwner=owner
  const bridge={id:item.id,descriptor,request:f.request,pending,
    requestFingerprint:descriptor.request_fingerprint,pendingFingerprint:descriptor.pending_fingerprint,route:f.route}
  monitor.records.set('record',{approval:bridge})
  monitor.approvalSnapshot=async()=>{
    snapshots++
    if(pausePostClaim&&snapshots===2){reachSnapshot();await resumePostClaimSnapshot}
    if(changeOnFinal && snapshots===2)return null
    const currentMode=modeChangeOnFinal&&snapshots===2?'legacyqueue':dispatchMode
    const currentPending=currentMode==='legacyqueue'?f.pending:{request_id:f.request.params.request_id,missing:true}
    return {route:f.route,row:f.row,snapshot:f.snapshot,request:f.request,pending:currentPending,dispatchMode:currentMode}
  }
  monitor.helper=async request=>{
    helperCalls.push(request)
    if(request.action==='approval_owner_heartbeat')return {ok:true,feature_ready:true,version:APPROVAL_COMPAT_VERSION,capability:APPROVAL_CAPABILITY}
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

test('server-request authority sends once to the frozen srq and never treats gateway acceptance as delivery', async () => {
  const accepted=await runPump({status:'ok'},{dispatchMode:'legacyserver_request'})
  const writes=accepted.calls.filter(call=>call.method==='request.answer')
  assert.equal(writes.length,1)
  assert.deepEqual(writes[0].args,{id:'srq-aabbcc112233',result:{choice:'once',all:false}})
  assert.equal(accepted.calls.some(call=>call.method==='approval.respond'),false)
  assert.equal(accepted.helperCalls.find(call=>call.action==='approval_settle').status,'unknown')
  const expired=await runPump({status:'expired'},{dispatchMode:'legacyserver_request'})
  assert.equal(expired.helperCalls.find(call=>call.action==='approval_settle').status,'expired')
  for(const receipt of [{status:'ok',extra:true},{status:'resolved'},{},null]){
    const out=await runPump(receipt,{dispatchMode:'legacyserver_request'})
    assert.equal(out.calls.filter(call=>call.method==='request.answer').length,1)
    assert.equal(out.calls.some(call=>call.method==='approval.respond'),false)
    assert.equal(out.helperCalls.find(call=>call.action==='approval_settle').status,'unknown')
    assert.equal(out.helperCalls.filter(call=>call.action==='approval_poll').length,1)
  }
  const ambiguous=await runPump({}, {dispatchMode:'legacyserver_request',throwRpc:true})
  assert.equal(ambiguous.calls.filter(call=>call.method==='request.answer').length,1)
  assert.equal(ambiguous.calls.some(call=>call.method==='approval.respond'),false)
  assert.equal(ambiguous.helperCalls.find(call=>call.action==='approval_settle').status,'unknown')
})

test('a frozen server-request card cannot switch to queue dispatch when mode changes after its tap', async () => {
  const changed=await runPump({resolved:1},{dispatchMode:'legacyserver_request',modeChangeOnFinal:true})
  assert.equal(changed.calls.some(call=>call.method==='request.answer'||call.method==='approval.respond'),false)
  assert.equal(changed.helperCalls.find(call=>call.action==='approval_settle').status,'rejected')
  assert.equal(changed.helperCalls.filter(call=>call.action==='approval_poll').length,1)
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
test('cached approval-v4 receiver helper cannot enable v5 authority modes', async () => {
  const f=fixture();const api={state:{profile:{get:()=> 'default'}},profileRoutes:async()=>[f.route],notify:()=>{}}
  const monitor=new CompletionMonitor(api,{setTimeout:()=>()=>{}})
  const actions=[];monitor.helper=async request=>{actions.push(request);return {ok:true,version:'1.8.0',capabilities:['desktop-command-approval-v4']}}
  await monitor.mounted()
  assert.equal(monitor.approvalCapable,false)
  assert.deepEqual(actions.map(request=>request.action),['loaded'])
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

test('package version 1.8.1 keeps the 1.8.0 approval compatibility value and fences with v6', () => {
  assert.equal(VERSION,'1.8.1')
  assert.equal(APPROVAL_COMPAT_VERSION,'1.8.0')
  assert.equal(APPROVAL_CAPABILITY,'desktop-command-approval-v6')
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
    'timeout_unavailable','invalid_review_text','approval_result_too_large',
    'approval_store_unavailable','AttributeError','disabled','receiver_unavailable','unexpected_failure']
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
      if(payload.action==='approval_owner_heartbeat')return {ok:true,feature_ready:true,version:APPROVAL_COMPAT_VERSION,capability:APPROVAL_CAPABILITY}
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

test('supported approval failures never fall into attention even without a typed reason or ready receiver', async () => {
  for(const variant of ['receiver-unready','null','empty','invalid-reason']){
    const f=fixture();const calls=[];const notices=[]
    const api={state:{profile:{get:()=> 'default'}},profileRoutes:async()=>[f.route],notify:n=>notices.push(n),
      requestProfile:async(_route,method)=>method==='approval.pending' ? {approvals:[f.pending]} : f.snapshot}
    const monitor=new CompletionMonitor(api,{setTimeout:()=>()=>{}});monitor.approvalCapable=true;monitor.replyOwner=f.owner
    monitor.helper=async payload=>{
      calls.push(payload.action)
      if(payload.action==='approval_owner_heartbeat')return {ok:true,feature_ready:variant!=='receiver-unready',version:APPROVAL_COMPAT_VERSION,capability:APPROVAL_CAPABILITY}
      if(payload.action==='approval_register')return variant==='null' ? null : variant==='empty' ? {} : {ok:false,reason:123}
      if(payload.action==='approval_notice')return {ok:true,status:'sent',notification_only:true}
      if(payload.action==='attention')return {ok:true,status:'sent'}
      throw Error(payload.action)
    }
    const record={sid:f.row.id,ask:null,approval:null,askChecking:false}
    await monitor.checkAsk(record,f.route,f.row,f.snapshot,f.request)
    assert.deepEqual(calls,variant==='receiver-unready' ? ['approval_owner_heartbeat','approval_notice'] :
      ['approval_owner_heartbeat','approval_register','approval_notice'],variant)
    assert.equal(record.approval,null);assert.equal(record.ask.id,f.request.id)
    assert.equal(JSON.stringify(notices).includes(f.request.params.command),false)
  }
})

test('generic native approval producers can register only from a healthy empty parent queue', async () => {
  for(const key of ['plugin_rule:fixture','execute_code','tirith:secret','future_native_rule']){
    const f=fixture();f.request.params.pattern_key=key;f.request.params.pattern_keys=[key]
    const calls=[]
    const api={state:{profile:{get:()=> 'default'}},profileRoutes:async()=>[f.route],notify:()=>{},
      requestProfile:async(_route,method)=>method==='approval.pending' ? {approvals:[]} : f.snapshot}
    const monitor=new CompletionMonitor(api,{setTimeout:()=>()=>{}});monitor.approvalCapable=true;monitor.replyOwner=f.owner
    monitor.helper=async payload=>{
      calls.push(payload)
      if(payload.action==='approval_owner_heartbeat')return {ok:true,feature_ready:true,version:APPROVAL_COMPAT_VERSION,capability:APPROVAL_CAPABILITY}
      if(payload.action==='approval_register')return {ok:true,new:true,id:'stored-'+key,status:'open'}
      throw Error(payload.action)
    }
    const record={sid:f.row.id,ask:null,approval:null,askChecking:false}
    await monitor.checkAsk(record,f.route,f.row,f.snapshot,f.request)
    assert.deepEqual(calls.map(call=>call.action),['approval_owner_heartbeat','approval_register'],key)
    assert.equal(record.approval?.descriptor.dispatch_mode,'legacyserver_request',key)
    assert.equal(record.approval?.request.params.pattern_key,key)
  }
})

test('unhealthy approval.pending reads never become server-request authority', async () => {
  const f=fixture()
  for(const response of [null,{}, {approvals:'bad'}, {approvals:[{}]},
    {approvals:[f.pending,f.pending]}, {approvals:[{...f.pending,command:'conflict'}]}]){
    const calls=[]
    const api={state:{profile:{get:()=> 'default'}},profileRoutes:async()=>[f.route],notify:()=>{},
      requestProfile:async(_route,method)=>method==='approval.pending'?response:f.snapshot}
    const monitor=new CompletionMonitor(api,{setTimeout:()=>()=>{}});monitor.approvalCapable=true;monitor.replyOwner=f.owner
    monitor.helper=async payload=>{calls.push(payload.action);return {ok:true,status:'sent',notification_only:true}}
    const record={sid:f.row.id,ask:null,approval:null,askChecking:false}
    await monitor.checkAsk(record,f.route,f.row,f.snapshot,f.request)
    assert.equal(calls.includes('approval_register'),false,JSON.stringify(response))
    assert.equal(record.approval,null)
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


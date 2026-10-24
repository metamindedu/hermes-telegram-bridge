// First-time admission scheduling only. Native RPC/helper transports are synthetic;
// real validators, checkAsk/checkApproval and publication bookkeeping remain intact.
import test from 'node:test'
import assert from 'node:assert/strict'
import {registerHooks} from 'node:module'
registerHooks({resolve(s,c,next){
  if(s==='@hermes/plugin-sdk')return {url:'data:text/javascript,export const host={}',shortCircuit:true}
  if(s==='react/jsx-runtime')return {url:'data:text/javascript,export const jsx=()=>({})',shortCircuit:true}
  return next(s,c)
}})
const {CompletionMonitor,APPROVAL_CAPABILITY,APPROVAL_COMPAT_VERSION}=await import('./desktop/plugin.js')
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve}}
function fixture({activeBlocked=false,activeUnavailable=false,invalidQueue=false}={}){
  const policy=deferred(),active=deferred(),policyEntered=deferred(),registered=deferred(),calls=[]
  const route={mode:'local',connectionId:'local',profile:'synthetic',targetProfile:'synthetic'}
  const request={id:'srq-aabbcc112233',method:'approval',params:{session_id:'runtime-1',request_id:'a'.repeat(32),
    command:'<write synthetic receipt>',description:'synthetic one-shot protected marker',
    pattern_key:'protected_instruction_file',pattern_keys:['protected_instruction_file'],
    allow_permanent:false,allow_session:false,choices:['once','deny']}}
  const pending={...request.params};delete pending.session_id;delete pending.choices
  const row={id:'runtime-1',session_key:'durable-1',status:'waiting'}
  const api={state:{profile:{get:()=>route.profile}},profileRoutes:async()=>[route],notify:()=>{},
    requestProfile:async(_route,method)=>{
      calls.push(method)
      if(method==='session.active_list'){
        if(activeBlocked)await active.promise
        if(activeUnavailable)throw new Error('synthetic native observation unavailable')
        return {sessions:[structuredClone(row)]}
      }
      if(method==='session.events.since')return {epoch:'epoch-1',open_requests:[structuredClone(request)],events:[],latest_seq:1,truncated:false}
      if(method==='approval.pending')return invalidQueue?{}:{approvals:[structuredClone(pending)]}
      throw new Error('unexpected synthetic RPC')
    }}
  const m=new CompletionMonitor(api,{setTimeout:()=>()=>{}})
  m.recoveryCapable=true;m.approvalCapable=true
  m.routeFor=async()=>route
  m.pumpReplies=async()=>{};m.pumpFollowups=async()=>{}
  m.helper=async req=>{
    calls.push(req.action)
    if(req.action==='completion_recovery_policy'){policyEntered.resolve();await policy.promise;return {ok:true,enabled:false,generation:'b'.repeat(32)}}
    if(req.action==='approval_owner_heartbeat')return {ok:true,feature_ready:true,version:APPROVAL_COMPAT_VERSION,capability:APPROVAL_CAPABILITY}
    if(req.action==='approval_register'){
      assert.deepEqual(req.request,request);assert.deepEqual(req.pending,pending)
      assert.equal(req.descriptor.session_key,row.session_key)
      assert.equal(req.descriptor.dispatch_mode,'legacyqueue')
      registered.resolve()
      return {ok:true,new:true,id:'c'.repeat(32),status:'open'}
    }
    if(req.action==='approval_poll')return {ok:true,items:[],terminal:[]}
    throw new Error('unexpected synthetic helper')
  }
  m.onEvent({type:'tool.start',profile:route.profile,connectionId:route.connectionId,session_id:row.id,seq:1,payload:{}})
  return {m,policy,active,policyEntered,registered,calls,request,pending}
}
async function beforeDeadline(promise){
  let timer
  try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('new approval admission waited behind unrelated recovery policy')),1000)})])}
  finally{clearTimeout(timer)}
}
async function cleanup(f,work){f.policy.resolve();f.active.resolve();await work;await f.m.recoveryPolicyRefreshing;f.m.dispose()}

test('new one-shot approval reaches real registration while recovery policy is still pending',async()=>{
  const f=fixture();const work=f.m.tick()
  try{
    await f.policyEntered.promise
    await beforeDeadline(f.registered.promise)
    await beforeDeadline(work)
    assert.equal(f.m.sweeping,false,'optional policy must not hold the next discovery tick')
    await beforeDeadline(f.m.tick())
    assert.equal(f.calls.filter(x=>x==='approval_register').length,1)
    assert.equal(f.calls.filter(x=>x==='completion_recovery_policy').length,1)
    assert.ok(f.m.recoveryPolicyRefreshing)
  }finally{await cleanup(f,work)}
})

test('unchanged native active-list barrier still blocks new registration',async()=>{
  const f=fixture({activeBlocked:true});const work=f.m.tick()
  try{
    await f.policyEntered.promise;await new Promise(r=>setImmediate(r))
    assert.equal(f.calls.includes('approval_register'),false)
    f.active.resolve();f.policy.resolve();await work
    assert.equal(f.calls.filter(x=>x==='approval_register').length,1)
  }finally{await cleanup(f,work)}
})

test('malformed native queue never registers even without the policy prerequisite',async()=>{
  const f=fixture({invalidQueue:true});const work=f.m.tick()
  try{f.policy.resolve();await work;assert.equal(f.calls.includes('approval_register'),false)}
  finally{await cleanup(f,work)}
})

test('failed native active-list read is not replaced by cached identity or empty authority',async()=>{
  const f=fixture({activeUnavailable:true});const work=f.m.tick()
  try{
    await beforeDeadline(work)
    assert.equal(f.calls.includes('session.events.since'),false)
    assert.equal(f.calls.includes('approval_register'),false)
    assert.equal(f.calls.filter(x=>x==='completion_recovery_policy').length,1)
  }finally{await cleanup(f,work)}
})

test('disposed monitor ignores a delayed recovery policy result',async()=>{
  const f=fixture();let accepted=0
  f.m.acceptRecoveryPolicy=async()=>{accepted++;return null}
  const read=f.m.refreshRecoveryPolicy();await f.policyEntered.promise
  f.m.dispose();f.policy.resolve();await read
  assert.equal(accepted,0,'an old asynchronous read must not revive retired monitor policy')
})

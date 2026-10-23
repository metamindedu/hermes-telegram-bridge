import test from 'node:test'
import assert from 'node:assert/strict'
import {registerHooks} from 'node:module'
registerHooks({resolve(specifier,context,next){
  if(specifier==='@hermes/plugin-sdk')return {url:'data:text/javascript,export const host={}',shortCircuit:true}
  if(specifier==='react/jsx-runtime')return {url:'data:text/javascript,export const jsx=()=>({})',shortCircuit:true}
  return next(specifier,context)
}})
const {default:plugin,CompletionMonitor,APPROVAL_COMPAT_VERSION,APPROVAL_CAPABILITY}=await import('./desktop/plugin.js')
const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r});return {promise,resolve}}
const drain=async()=>{for(let i=0;i<20;i++)await Promise.resolve()}
function pendingMonitor(){
  const monitor=new CompletionMonitor({},{}),firstHeartbeat=deferred(),bulk=deferred(),calls=[]
  const descriptor={dispatch_mode:'legacyqueue',request_fingerprint:'synthetic-request',pending_fingerprint:'synthetic-pending'}
  const bridge={id:'a'.repeat(32),descriptor}
  monitor.records.set('current',{approval:bridge})
  monitor.approvalSnapshot=async()=>({route:{mode:'local',connectionId:'local',profile:'claire',targetProfile:'claire'}})
  monitor.helper=async req=>{
    calls.push(req.action)
    if(req.action==='approval_owner_heartbeat'){
      if(calls.filter(x=>x==='approval_owner_heartbeat').length===1)await firstHeartbeat.promise
      return {ok:true,feature_ready:true,version:APPROVAL_COMPAT_VERSION,capability:APPROVAL_CAPABILITY}
    }
    assert.equal(req.action,'approval_poll')
    return {ok:true,items:[],terminal:[]}
  }
  monitor.sweep=async()=>{await bulk.promise}
  return {monitor,firstHeartbeat,bulk,calls}
}

test('registered interval services an existing approval while ordinary sweep is blocked',async t=>{
  const f=pendingMonitor(),intervals=[]
  t.mock.method(CompletionMonitor.prototype,'mounted',function(){
    // Keep the real registered monitor, injecting only its transport boundary.
    this.records=f.monitor.records
    this.helper=f.monitor.helper
    this.approvalSnapshot=f.monitor.approvalSnapshot
    this.sweep=f.monitor.sweep
    f.registered=this
    return Promise.resolve()
  })
  const ctx={registerSettingsPage(){},onEvent(){},onDispose(){},setInterval(fn){intervals.push(fn)}}
  plugin.register(ctx)
  assert.equal(intervals.length,1,'reuse the existing interval rather than adding a timer or poller')
  try{
    intervals[0]();await drain()
    assert.equal(f.calls.filter(x=>x==='approval_owner_heartbeat').length,1,
      'queued approval must not wait behind completion/reply work before its owner heartbeat')
    intervals[0]();await drain()
    assert.equal(f.calls.filter(x=>x==='approval_owner_heartbeat').length,1,
      'another tick must join the in-flight approval pump')
    f.firstHeartbeat.resolve();await f.registered.approvalPumping
    assert.equal(f.calls.filter(x=>x==='approval_poll').length,1,
      'claim polling must finish even though the unrelated sweep is still waiting')
  }finally{f.firstHeartbeat.resolve();f.bulk.resolve();await drain();f.registered?.dispose()}
})

test('simultaneous explicit approval pumps share one in-flight operation',async()=>{
  const f=pendingMonitor()
  const one=f.monitor.pumpApprovals(),two=f.monitor.pumpApprovals()
  try{
    await drain()
    assert.strictEqual(one,two,'the same owner cannot start overlapping poll/claim/apply sequences')
    assert.equal(f.calls.length,1)
  }finally{f.firstHeartbeat.resolve();f.bulk.resolve();await Promise.allSettled([one,two]);f.monitor.dispose()}
  assert.equal(f.calls.filter(x=>x==='approval_poll').length,1)
})

test('completed or failed pump releases its latch for the next legitimate cycle',async()=>{
  const m=new CompletionMonitor({},{});let calls=0
  assert.equal(typeof m.pumpApprovalsOnce,'function')
  m.pumpApprovalsOnce=async()=>{calls++;if(calls===1)throw new Error('synthetic read failure')}
  await assert.rejects(m.pumpApprovals(),/synthetic read failure/)
  await m.pumpApprovals()
  assert.equal(calls,2)
  m.dispose()
})

test('a stopped monitor never starts approval or ordinary work through a tick',async()=>{
  const m=new CompletionMonitor({},{});let calls=0
  m.pumpApprovals=()=>{calls++;return Promise.resolve()}
  m.sweep=()=>{calls++;return Promise.resolve()}
  m.dispose()
  assert.equal(typeof m.tick,'function')
  await m.tick()
  assert.equal(calls,0)
})

test('independent scheduling does not suppress the original snapshot/absence rejection',async()=>{
  const f=pendingMonitor();f.monitor.approvalSnapshot=async()=>null
  let polled=null
  f.monitor.helper=async req=>{
    if(req.action==='approval_owner_heartbeat')return {ok:true,feature_ready:true,version:APPROVAL_COMPAT_VERSION,capability:APPROVAL_CAPABILITY}
    assert.equal(req.action,'approval_poll');polled=req;return {ok:true,items:[],terminal:[]}
  }
  await f.monitor.pumpApprovals()
  assert.deepEqual(polled.observations,[])
  assert.deepEqual(polled.absent,['a'.repeat(32)])
  f.monitor.dispose()
})

test('publication during a snapshot defers the older owner-wide poll',async()=>{
  const f=pendingMonitor(),entered=deferred(),snapshot=deferred(),polls=[]
  f.firstHeartbeat.resolve()
  let paused=false
  f.monitor.approvalSnapshot=async()=>{
    if(!paused){paused=true;entered.resolve();await snapshot.promise}
    return {route:{mode:'local',connectionId:'local',profile:'claire',targetProfile:'claire'}}
  }
  const helper=f.monitor.helper
  f.monitor.helper=async req=>{if(req.action==='approval_poll')polls.push(req);return helper(req)}
  const pumping=f.monitor.pumpApprovals()
  try{
    await entered.promise
    await f.monitor.approvalPublication(async()=>{
      const bridge={...f.monitor.records.get('current').approval,id:'b'.repeat(32)}
      f.monitor.records.set('new',{approval:bridge})
    })
    snapshot.resolve();await pumping
    assert.equal(polls.length,0,'an invalidated view must not reach the owner-wide consumer')
    await f.monitor.pumpApprovals()
    assert.equal(polls.length,1)
    assert.deepEqual(polls[0].observations.map(x=>x.id).sort(),['a'.repeat(32),'b'.repeat(32)])
  }finally{snapshot.resolve();await pumping;f.monitor.dispose()}
})

test('publication cannot cross the actual owner-wide poll receipt',async()=>{
  const f=pendingMonitor(),entered=deferred(),poll=deferred()
  let published=false
  f.monitor.helper=async req=>{
    if(req.action==='approval_owner_heartbeat')return {ok:true,feature_ready:true,version:APPROVAL_COMPAT_VERSION,capability:APPROVAL_CAPABILITY}
    assert.equal(req.action,'approval_poll');entered.resolve();await poll.promise
    return {ok:true,items:[],terminal:[]}
  }
  const pumping=f.monitor.pumpApprovals()
  try{
    await entered.promise
    const publication=f.monitor.approvalPublication(async()=>{published=true})
    await drain();assert.equal(published,false)
    poll.resolve();await Promise.all([pumping,publication])
    assert.equal(published,true)
  }finally{poll.resolve();await pumping;f.monitor.dispose()}
})

test('unbound uncertain registration defers polling rather than relaxing queued rejection',async()=>{
  const f=pendingMonitor();f.firstHeartbeat.resolve()
  f.monitor.records.set('uncertain',{approvalRegistration:{
    descriptor:{request_id:'srq-synthetic'},recoveryStarted:Date.now(),recoveryDeadline:Date.now()+120_000}})
  f.monitor.recoverApprovalRegistration=async()=>{}
  await f.monitor.pumpApprovals()
  assert.equal(f.calls.filter(x=>x==='approval_poll').length,0)
  assert.ok(f.monitor.records.get('uncertain').approvalRegistration)
  assert.equal(f.calls.filter(x=>x==='approval_owner_heartbeat').length,1)
  f.monitor.dispose()
})

test('publication exceptions release the local fence without reusing an old view token',async()=>{
  const m=new CompletionMonitor({},{}),before=m.approvalPublicationEpoch
  await assert.rejects(m.approvalPublication(async()=>{throw new Error('synthetic publication failure')}),/synthetic publication failure/)
  assert.notStrictEqual(m.approvalPublicationEpoch,before)
  assert.equal(await m.approvalPublication(async()=> 'next'), 'next')
  m.dispose()
})

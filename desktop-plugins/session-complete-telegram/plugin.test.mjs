import test from 'node:test'
import assert from 'node:assert/strict'
import { registerHooks } from 'node:module'

registerHooks({resolve(specifier, context, nextResolve) {
  if (specifier === '@hermes/plugin-sdk') return {url: 'data:text/javascript,export const host = {}', shortCircuit:true}
  return nextResolve(specifier, context)
}})
const {CompletionMonitor, QUIET_MS, blockingProcess, unfinishedControl} = await import('./plugin.js')

function fixture(profile='default') {
  let time = 1000
  const sent = [], notices = [], timers = []
  const routes = [{mode:'local',profile:'default',targetProfile:'default',connectionId:'local'}, {mode:'local',profile,targetProfile:profile,connectionId:'local'}].filter((r,i,a)=>a.findIndex(x=>x.profile===r.profile)===i)
  const data = {
    'session.active_list': {sessions:[{id:'ui-1',status:'idle',session_key:'stored-1'}]},
    'subagent.list': {subagents:[],delegations:[]},
    'process.list': {processes:[]},
    'session.control.read': {control:{goal:null,loop:null,heartbeat:null}},
    'session.events.since': {events:[],open_requests:[],truncated:false}
  }
  const api = {state:{profile:{get:()=>profile}},profileRoutes:async()=>routes,notify:v=>notices.push(v),requestProfile:async(route,method,args)=>{
    if (method==='shell.exec') {
      assert.equal(route.profile,'default')
      const request=JSON.parse(Buffer.from(args.command.split('--request ')[1],'base64url').toString())
      if (request.action === 'followup_owner_heartbeat') return {code:0,stdout:JSON.stringify({ok:true,feature_ready:false})}
      sent.push(request)
      return {code:0,stdout:JSON.stringify({ok:true,status:'sent',verified:true})}
    }
    const value=data[method]
    if(value instanceof Error) throw value
    return structuredClone(value)
  }}
  const ctx = {setTimeout:(fn,delay)=>{const t={fn,delay,cancelled:false};timers.push(t);return ()=>{t.cancelled=true}}}
  const m = new CompletionMonitor(api,ctx,()=>time)
  const event = (type,payload={},seq=1)=>m.onEvent({type,payload,seq,session_id:'ui-1',profile,connectionId:'local'})
  const done=(seq=2,finalId=42)=>event('message.complete',{status:'complete',text:'done',persisted_turn:{final_assistant_row_id:finalId}},seq)
  const record=()=>[...m.records.values()][0]
  const check=async()=>{time+=QUIET_MS+1;await m.check(record())}
  return {m,api,ctx,routes,data,sent,notices,timers,event,done,record,check,setTime:v=>time=v}
}

test('quiet window then notification for each completed follow-up in same session',async()=>{
  const f=fixture();f.event('message.start');f.done();await f.m.check(f.record());assert.equal(f.sent.length,0)
  await f.check();assert.equal(f.sent.length,1)
  f.event('message.start',{},3);f.done(4,44);await f.check();assert.equal(f.sent.length,2)
  f.event('message.start',{},5);f.done(6,46);await f.check();assert.equal(f.sent.length,3)
  assert.deepEqual(f.sent.map(x=>x.final_row_id),[42,44,46])
})
test('duplicate callback for same final never resends',async()=>{
  const f=fixture();f.done();await f.check();f.done(3);await f.check();assert.equal(f.sent.length,1)
})
test('follow-up completing during old send is preserved',async()=>{
  const f=fixture();f.done();const original=f.api.requestProfile;let raced=false
  f.api.requestProfile=async(...args)=>{
    if(args[1]==='shell.exec'&&!raced){raced=true;f.event('message.start',{},3);f.done(4,44)}
    return original(...args)
  }
  await f.check();assert.equal(f.record().candidate.finalId,44)
  await f.check();assert.deepEqual(f.sent.map(x=>x.final_row_id),[42,44])
})
test('late duplicate from prior request does not discard follow-up completion',async()=>{
  const f=fixture();f.done();await f.check()
  f.event('message.start',{},3);f.done(4,44);f.done(5,42)
  await f.check();assert.deepEqual(f.sent.map(x=>x.final_row_id),[42,44])
})
test('many internal turns coalesce to one last completion',async()=>{
  const f=fixture();for(let i=0;i<30;i++){f.event('message.start',{},i*2+1);f.done(i*2+2)}
  await f.check();assert.equal(f.sent.length,1)
})
for(const status of ['error','interrupted',undefined]) test(`does not notify ${status} final`,async()=>{
  const f=fixture();f.event('message.complete',{status,persisted_turn:{final_assistant_row_id:42}});await f.check();assert.equal(f.sent.length,0)
})
for(const status of ['working','waiting','starting']) test(`blocks live ${status}`,async()=>{
  const f=fixture();f.done();f.data['session.active_list'].sessions[0].status=status;await f.check();assert.equal(f.sent.length,0)
})
test('unfinished child requires subsequent parent synthesis',async()=>{
  const f=fixture();f.done();f.data['subagent.list'].subagents=[{status:'running'}];await f.check();assert.equal(f.sent.length,0)
  f.data['subagent.list'].subagents=[];await f.check();assert.equal(f.sent.length,0)
  f.event('message.start',{},3);f.done(4);await f.check();assert.equal(f.sent.length,1)
})
test('late child completion cancels premature parent candidate',async()=>{
  const f=fixture();f.done();f.event('subagent.complete',{status:'completed'},3);await f.check();assert.equal(f.sent.length,0)
})
test('running bounded or unclassified process blocks',async()=>{
  const f=fixture();f.done();f.data['process.list'].processes=[{status:'running',notify_on_complete:true}];await f.check();assert.equal(f.sent.length,0)
  assert.equal(blockingProcess({status:'running',notify_on_complete:false}),true)
  assert.equal(blockingProcess({status:'running',detached:true}),false)
  assert.equal(blockingProcess({status:'running',watch_patterns:['ready'],watch_hit:true}),false)
})
test('active goal, paused goal and pending approvals block',async()=>{
  for(const control of [{goal:{status:'active'},loop:null,heartbeat:null},{goal:{status:'paused'},loop:null,heartbeat:null}]) {
    const f=fixture();f.done();f.data['session.control.read'].control=control;await f.check();assert.equal(f.sent.length,0)
  }
  const f=fixture();f.done();f.data['session.events.since'].open_requests=[{method:'approval'}];await f.check();assert.equal(f.sent.length,0)
  assert.equal(unfinishedControl({goal:{status:'done'},loop:null,heartbeat:null}),false)
})
test('pending tasks suppress even when parent turn ended',async()=>{
  const f=fixture();f.event('todo.updated',{todos:[{status:'in_progress'}]},1);f.done();await f.check();assert.equal(f.sent.length,0)
})
test('replay work / lost event prevents a false quiet result',async()=>{
  const f=fixture();f.done();f.data['session.events.since'].events=[{type:'message.start',seq:3}];await f.check();assert.equal(f.sent.length,0)
})
test('new profile requires no profile-specific install',async()=>{
  const f=fixture('brand_new_profile');f.done();await f.check();assert.equal(f.sent[0].profile,'brand_new_profile')
})
test('two unrelated profile sessions are not merged',async()=>{
  const f=fixture('claire');f.done();f.m.onEvent({type:'message.complete',session_id:'ui-1',profile:'default',connectionId:'local',seq:2,payload:{status:'complete',persisted_turn:{final_assistant_row_id:43}}});
  f.setTime(100000);for(const r of f.m.records.values()) await f.m.check(r)
  assert.deepEqual(f.sent.map(x=>x.profile).sort(),['claire','default'])
})
test('new work racing the snapshot cannot notify',async()=>{
  const f=fixture();f.done();const original=f.api.requestProfile;let raced=false
  f.api.requestProfile=async(...args)=>{const reply=await original(...args);if(!raced&&args[1]==='session.active_list'){raced=true;f.event('message.start',{},3)}return reply}
  await f.check();assert.equal(f.sent.length,0)
})
test('RPC failure retries boundedly without sending',async()=>{
  const f=fixture();f.done();f.data['process.list']=new Error('unavailable');for(let i=0;i<5;i++)await f.check();assert.equal(f.sent.length,0);assert.equal(f.notices.length,1)
})
test('replayed completion never creates a candidate',()=>{
  const f=fixture();f.m.onEvent({type:'message.complete',replayed:true,profile:'default',session_id:'old',payload:{status:'complete',persisted_turn:{final_assistant_row_id:42}}});assert.equal(f.m.records.size,0)
})
test('post-turn goal done bookkeeping preserves completion candidate',async()=>{
  const f=fixture();f.done();f.event('session.control.update',{control:{goal:{status:'done'},loop:null,heartbeat:null}},3)
  f.data['session.control.read'].control={goal:{status:'done'},loop:null,heartbeat:null}
  await f.check();assert.equal(f.sent.length,1)
})

test('unload cancels scheduled work',async()=>{
  const f=fixture();f.done();f.m.dispose();await f.timers[0].fn();assert.equal(f.sent.length,0);assert.equal(f.timers[0].cancelled,true)
})

test('input-needed sweep notifies once per human ask',async()=>{
  const f=fixture();f.event('message.start')
  f.data['session.active_list'].sessions[0].status='waiting'
  f.data['session.events.since'].open_requests=[{id:'srq-aaaabbbbcccc',method:'clarify',params:{question:'색?',choices:['빨강','파랑']}}]
  await f.m.sweep()
  assert.equal(f.sent.length,1)
  assert.equal(f.sent[0].action,'attention')
  assert.equal(f.sent[0].kind,'ask')
  assert.equal(f.sent[0].ask_id,'srq-aaaabbbbcccc')
  assert.equal(f.sent[0].ask_method,'clarify')
  assert.deepEqual(f.sent[0].ask_params,{question:'색?',choices:['빨강','파랑']})
  assert.equal(f.sent[0].session_key,'stored-1')
  assert.equal(f.sent[0].profile,'default')
  await f.m.sweep()
  assert.equal(f.sent.length,1)
})
test('answered ask then a new ask notifies again',async()=>{
  const f=fixture();f.event('message.start')
  const row=f.data['session.active_list'].sessions[0]
  row.status='waiting'
  f.data['session.events.since'].open_requests=[{id:'srq-111111111111',method:'approval',params:{command:'rm x'}}]
  await f.m.sweep()
  row.status='working';await f.m.sweep()
  row.status='waiting'
  f.data['session.events.since'].open_requests=[{id:'srq-222222222222',method:'clarify',params:{question:'다음은?'}}, {id:'srq-333333333333',method:'approval',params:{command:'y'}}]
  await f.m.sweep()
  assert.deepEqual(f.sent.map(x=>x.ask_id),['srq-111111111111','srq-222222222222'])
})
test('GUI bridge requests never raise input-needed',async()=>{
  const f=fixture();f.event('message.start')
  f.data['session.active_list'].sessions[0].status='waiting'
  f.data['session.events.since'].open_requests=[{id:'srq-444444444444',method:'window.read',params:{}},{id:'srq-555555555555',method:'tour',params:{action:'start'}}]
  await f.m.sweep()
  assert.equal(f.sent.length,0)
})
test('sweep polls only observed routes and prunes dead sessions',async()=>{
  const f=fixture('claire');f.event('message.start')
  let activeCalls=0
  const original=f.api.requestProfile
  f.api.requestProfile=async(route,method,args)=>{
    if(method==='session.active_list'){activeCalls++;assert.equal(route.profile,'claire')}
    return original(route,method,args)
  }
  await f.m.sweep();assert.equal(activeCalls,1)
  f.data['session.active_list'].sessions=[]
  await f.m.sweep()
  assert.equal(f.m.records.size,0)
  await f.m.sweep();assert.equal(activeCalls,2)
})
test('attention transport failure retries while the ask stays open',async()=>{
  const f=fixture();f.event('message.start')
  f.data['session.active_list'].sessions[0].status='waiting'
  f.data['session.events.since'].open_requests=[{id:'srq-666666666666',method:'clarify',params:{question:'q'}}]
  const original=f.api.requestProfile
  let fail=true
  f.api.requestProfile=async(route,method,args)=>{
    if(method==='shell.exec'&&fail){fail=false;throw Error('unavailable')}
    return original(route,method,args)
  }
  await f.m.sweep();assert.equal(f.sent.length,0)
  await f.m.sweep();assert.equal(f.sent.length,1)
  await f.m.sweep();assert.equal(f.sent.length,1)
})
test('ambiguous attention send warns and is never retried',async()=>{
  const f=fixture();f.event('message.start')
  f.data['session.active_list'].sessions[0].status='waiting'
  f.data['session.events.since'].open_requests=[{id:'srq-777777777777',method:'approval',params:{command:'x'}}]
  let shellCalls=0
  f.api.requestProfile=async(route,method,args)=>{
    if(method==='shell.exec'){
      const request=JSON.parse(Buffer.from(args.command.split('--request ')[1],'base64url').toString())
      if(request.action==='followup_owner_heartbeat')return {code:0,stdout:'{"feature_ready":false}'}
      shellCalls++;return {code:0,stdout:JSON.stringify({ok:false,status:'unknown'})}
    }
    return structuredClone(f.data[method])
  }
  await f.m.sweep();await f.m.sweep()
  assert.equal(shellCalls,1)
  assert.equal(f.notices.length,1)
})

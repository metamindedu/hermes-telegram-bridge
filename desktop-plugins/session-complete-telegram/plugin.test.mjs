import test from 'node:test'
import assert from 'node:assert/strict'
import { registerHooks } from 'node:module'

registerHooks({resolve(specifier, context, nextResolve) {
  if (specifier === '@hermes/plugin-sdk') return {url: 'data:text/javascript,export const host = {}', shortCircuit:true}
  if (specifier === 'react/jsx-runtime') return {url: 'data:text/javascript,export const jsx=(type,props,key)=>({type,props,key})', shortCircuit:true}
  return nextResolve(specifier, context)
}})
const {CompletionMonitor, QUIET_MS, blockingProcess, unfinishedControl, stableJSON, encodeRequest, decodeResultFrame, NativeHelperError} = await import('./desktop/plugin.js')

const FRAME_BEGIN = 'HERMES_TELEGRAM_BRIDGE_RESULT_V1_BEGIN'
const FRAME_END = 'HERMES_TELEGRAM_BRIDGE_RESULT_V1_END'
function frame(value) {
  return `${FRAME_BEGIN}\n${Buffer.from(JSON.stringify(value), 'utf8').toString('base64url')}\n${FRAME_END}\n`
}
function decodeArgvRequest(args) {
  assert.deepEqual(args.argv.slice(0, 4), ['--profile', 'default', 'desktop-telegram-bridge', '--request'])
  assert.match(args.argv[4], /^[A-Za-z0-9_-]+$/u)
  return JSON.parse(Buffer.from(args.argv[4], 'base64url').toString('utf8'))
}

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
  const api = {state:{profile:{get:()=>profile}},profileRoutes:async()=>routes,notify:v=>notices.push(v),requestProfile:async(route,method,args,outerTimeout)=>{
    if (method==='cli.exec') {
      assert.equal(route.profile,'default')
      assert.equal(args.timeout,25)
      assert.equal(outerTimeout,35_000)
      const request=decodeArgvRequest(args)
      if (request.action === 'followup_owner_heartbeat') return {blocked:false,code:0,output:frame({ok:true,feature_ready:false})}
      sent.push(request)
      return {blocked:false,code:0,output:frame({ok:true,status:'sent',verified:true})}
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
    if(args[1]==='cli.exec'&&!raced){raced=true;f.event('message.start',{},3);f.done(4,44)}
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
test('definite CLI block before launch retries while the ask stays open',async()=>{
  const f=fixture();f.event('message.start')
  f.data['session.active_list'].sessions[0].status='waiting'
  f.data['session.events.since'].open_requests=[{id:'srq-666666666666',method:'clarify',params:{question:'q'}}]
  const original=f.api.requestProfile
  let blocked=true
  f.api.requestProfile=async(route,method,args,outerTimeout)=>{
    if(method==='cli.exec'&&blocked){blocked=false;return {blocked:true,code:-1,output:'',hint:'denied'}}
    return original(route,method,args,outerTimeout)
  }
  await f.m.sweep();assert.equal(f.sent.length,0)
  await f.m.sweep();assert.equal(f.sent.length,1)
  await f.m.sweep();assert.equal(f.sent.length,1)
})
test('ambiguous attention send warns and is never retried',async()=>{
  const f=fixture();f.event('message.start')
  f.data['session.active_list'].sessions[0].status='waiting'
  f.data['session.events.since'].open_requests=[{id:'srq-777777777777',method:'approval',params:{command:'x'}}]
  let helperCalls=0
  f.api.requestProfile=async(route,method,args)=>{
    if(method==='cli.exec'){
      const request=decodeArgvRequest(args)
      if(request.action==='followup_owner_heartbeat')return {blocked:false,code:0,output:frame({ok:true,feature_ready:false})}
      helperCalls++;return {blocked:false,code:0,output:frame({ok:false,status:'unknown'})}
    }
    return structuredClone(f.data[method])
  }
  await f.m.sweep();await f.m.sweep()
  assert.equal(helperCalls,1)
  assert.equal(f.notices.length,1)
})

test('default route CLI framing tolerates startup logs but rejects duplicates and malformed frames',()=>{
  assert.deepEqual(decodeResultFrame(`Hermes startup log\n${frame({ok:true,version:'1.5.0'})}ready`),{ok:true,version:'1.5.0'})
  assert.throws(()=>decodeResultFrame(frame({ok:true})+frame({ok:true})),NativeHelperError)
  assert.throws(()=>decodeResultFrame(`${FRAME_BEGIN}\nnot-base64!\n${FRAME_END}\n`),NativeHelperError)
  assert.throws(()=>decodeResultFrame('{"ok":true}'),NativeHelperError)
})

test('fixed request encoding preserves original Windows argv envelope and Unicode fingerprints',()=>{
  const encoded=encodeRequest({action:'send',profile:'default'})
  assert.match(encoded,/^[A-Za-z0-9_-]+$/u)
  assert.equal(JSON.parse(Buffer.from(encoded,'base64url').toString('utf8')).action,'send')
  const ask={id:'srq-aabbcc112233',method:'clarify',params:{session_id:'session-korean',
    question:'가'.repeat(2495)+'😊"\\',choices:['나'.repeat(496)+'😊"\\']}}
  const request={action:'reply_register',descriptor:{owner:'owner',connectionId:'local',profile:'default',
    targetProfile:'default',sid:'session-korean',session_key:'stored',epoch:'epoch',request_id:ask.id,
    fingerprint:stableJSON(ask)},ask}
  const korean=encodeRequest(request)
  assert.ok(korean.length<30_000)
  assert.deepEqual(JSON.parse(Buffer.from(korean,'base64url').toString('utf8')),request)
  const jsonSize=Buffer.byteLength(JSON.stringify({data:''}),'utf8')
  const boundary=encodeRequest({data:'x'.repeat(22_500-jsonSize)})
  assert.equal(boundary.length,30_000)
  assert.throws(()=>encodeRequest({data:'x'.repeat(22_500-jsonSize+1)}),error=>
    error instanceof NativeHelperError&&error.permanent&&!error.ambiguousWrite)
  assert.throws(()=>encodeRequest({data:1n}),error=>
    error instanceof NativeHelperError&&error.permanent&&!error.ambiguousWrite)
  for(const invalid of [{data:Number.NaN},{data:undefined}])assert.throws(()=>encodeRequest(invalid),error=>
    error instanceof NativeHelperError&&error.permanent&&!error.ambiguousWrite)
  const cyclic={};cyclic.self=cyclic
  assert.throws(()=>encodeRequest(cyclic),error=>
    error instanceof NativeHelperError&&error.permanent&&!error.ambiguousWrite)
})

test('permanent local ask encoding failure gives guidance once and never launches CLI retry',async()=>{
  const f=fixture();f.event('message.start')
  f.data['session.active_list'].sessions[0].status='waiting'
  f.data['session.events.since']={epoch:'epoch-1',events:[],truncated:false,open_requests:[{id:'srq-bbccdd223344',method:'clarify',params:{
    session_id:'ui-1',question:'😊'.repeat(2500),choices:['나'.repeat(500)]}}]}
  await f.m.sweep();await f.m.sweep();await f.m.sweep()
  assert.equal(f.sent.filter(request=>request.action==='reply_register').length,0)
  assert.equal(f.notices.length,1)
  assert.match(f.notices[0].message,/전송 크기 제한/u)
  assert.match(f.notices[0].message,/짧게 줄여/u)
})

test('completion send stays available when ingress feature is disabled',async()=>{
  const f=fixture();f.done();await f.check();await f.m.sweep()
  assert.equal(f.sent[0].action,'send')
  assert.equal(f.sent.filter(request=>request.action==='reply_register').length,0)
})

test('ambiguous CLI frame after a write is never retried',async()=>{
  const f=fixture();f.done();let writes=0
  f.api.requestProfile=async(route,method,args)=>{
    if(method==='cli.exec'){
      const request=decodeArgvRequest(args)
      if(request.action==='followup_owner_heartbeat')return {blocked:false,code:0,output:frame({ok:true,feature_ready:false})}
      writes++
      return {blocked:false,code:0,output:frame({ok:true})+frame({ok:true})}
    }
    return structuredClone(f.data[method])
  }
  await f.check();await f.check();f.done(3,42);await f.check()
  assert.equal(writes,1)
  assert.equal(f.notices.length,1)
})

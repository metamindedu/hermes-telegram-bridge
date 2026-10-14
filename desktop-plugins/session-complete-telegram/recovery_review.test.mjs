import test from 'node:test'
import assert from 'node:assert/strict'
import {registerHooks} from 'node:module'
registerHooks({resolve(specifier, context, nextResolve) {
  if (specifier === '@hermes/plugin-sdk') return {url:'data:text/javascript,export const host={}', shortCircuit:true}
  if (specifier === 'react/jsx-runtime') return {url:'data:text/javascript,export const jsx=(type,props,key)=>({type,props,key})', shortCircuit:true}
  return nextResolve(specifier, context)
}})
const {CompletionMonitor, VERSION, APPROVAL_COMPAT_VERSION, APPROVAL_CAPABILITY,
  COMPLETION_RECOVERY_CAPABILITY, QUIET_MS} = await import('./desktop/plugin.js')

const BEGIN='HERMES_TELEGRAM_BRIDGE_RESULT_V1_BEGIN'
const END='HERMES_TELEGRAM_BRIDGE_RESULT_V1_END'
const frame=value=>`${BEGIN}\n${Buffer.from(JSON.stringify(value),'utf8').toString('base64url')}\n${END}\n`
const DEFAULT_ROUTE={mode:'local',connectionId:'local',profile:'default',targetProfile:'default'}
const GEN='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'

function fixture({profile='claire', policy={ok:true,enabled:true,generation:GEN}, beforeHelper=null}={}) {
  let now=1_800_000_000_000
  let socket='open'
  let activeProfile=profile
  let currentPolicy=structuredClone(policy)
  let classification='completion'
  let sendOutcome='sent'
  let claimHook=null
  let nextId=1
  const calls=[], requests=[], timers=[], notices=[], legacySends=[]
  const route={mode:'local',connectionId:'local',profile,targetProfile:profile}
  const routes=profile==='default'?[DEFAULT_ROUTE,{mode:'local',connectionId:'local',profile:'claire',targetProfile:'claire'}]:[DEFAULT_ROUTE,route]
  const state={gateway:{get:()=>socket},connectionId:{get:()=> 'local'},profile:{get:()=>activeProfile}}
  const snapshots={
    'session.active_list':{sessions:[{id:'runtime-1',status:'idle',session_key:'durable-1',started_at:1700000000}]},
    'subagent.list':{subagents:[],delegations:[]},
    'process.list':{processes:[]},
    'session.control.read':{control:{goal:null,loop:null,heartbeat:null}},
    'session.events.since':{events:[],latest_seq:2,truncated:false,epoch:'epoch-a',open_requests:[]}
  }
  const candidates=new Map(), tombstones=new Set()
  const itemFor=(c,id=(nextId++).toString(16).padStart(64,'0'))=>({
    candidate_id:id,revision:1,state:'pending',route:structuredClone(c.route),runtime_id:c.runtime_id,
    session_key:c.session_key,started_at:c.started_at,epoch:c.epoch,seq:c.seq,bookkeeping_seq:c.seq,
    final_row_id:c.final_row_id,created_at:c.created_at,quiet_at:c.quiet_at,
    expires_at:c.created_at+1800,generation:c.generation
  })
  const event=(type,payload={},seq=1,extra={})=>monitor.onEvent({type,payload,seq,session_id:'runtime-1',profile,connectionId:'local',...extra})
  const dispatch=async(transport,method,args,timeout)=>{
    calls.push({transport,method,args:structuredClone(args),timeout})
    if(method!=='cli.exec') {
      const value=snapshots[method]
      if(value instanceof Error) throw value
      const cloned=structuredClone(value)
      if(method==='session.events.since' && Array.isArray(cloned?.events)) {
        cloned.events=cloned.events.filter(item=>Number.isSafeInteger(item.seq) && item.seq>(args?.last_seen||0))
      }
      return cloned
    }
    assert.deepEqual(args.argv.slice(0,4),['--profile','default','desktop-telegram-bridge','--request'])
    const request=JSON.parse(Buffer.from(args.argv[4],'base64url').toString('utf8'))
    requests.push({transport,request:structuredClone(request)})
    const override=beforeHelper?await beforeHelper(request):undefined
    if(override!==undefined)return {blocked:false,code:0,output:frame(override)}
    let value
    if(request.action==='loaded') {
      value={ok:true,version:APPROVAL_COMPAT_VERSION,package_version:VERSION,
        completion_policy:structuredClone(currentPolicy),
        capabilities:[APPROVAL_CAPABILITY,COMPLETION_RECOVERY_CAPABILITY]}
    } else if(request.action==='completion_recovery_policy') {
      value=structuredClone(currentPolicy)
    } else if(request.action==='completion_recovery_record') {
      const c=request.candidate
      const key=JSON.stringify([c.route.connectionId,c.route.profile,c.runtime_id,c.final_row_id])
      if(tombstones.has(key)) value={ok:false,reason:'candidate_terminal'}
      else {
        const existing=[...candidates.values()].find(item=>JSON.stringify([item.route.connectionId,item.route.profile,item.runtime_id,item.final_row_id])===key)
        if(existing) value={ok:true,duplicate:true,...structuredClone(existing)}
        else {const item=itemFor(c);candidates.set(item.candidate_id,item);value={ok:true,...structuredClone(item)}}
      }
    } else if(request.action==='completion_recovery_list') {
      const r=request.route
      const all=[...candidates.values()].filter(item=>item.route.connectionId===r.connectionId &&
        item.route.profile===r.profile && item.route.targetProfile===r.targetProfile &&
        ['pending','read_blocked','claimed','unsent_retryable'].includes(item.state))
        .sort((a,b)=>a.candidate_id.localeCompare(b.candidate_id))
      const after=all.filter(item=>!request.cursor || item.candidate_id>request.cursor)
      const items=after.slice(0,16)
      value={ok:true,version:VERSION,items:structuredClone(items),cursor:after.length>16?items.at(-1).candidate_id:null}
    } else if(request.action==='completion_recovery_get') {
      const item=candidates.get(request.candidate_id)
      const matches=item && item.route.connectionId===request.route.connectionId &&
        item.route.profile===request.route.profile && item.route.targetProfile===request.route.targetProfile
      value={ok:true,version:VERSION,item:matches?structuredClone(item):null}
    } else if(request.action==='completion_recovery_cancel') {
      const i=request.identity
      const key=JSON.stringify([i.route.connectionId,i.route.profile,i.runtime_id,i.final_row_id])
      tombstones.add(key)
      const item=[...candidates.values()].find(row=>JSON.stringify([row.route.connectionId,row.route.profile,row.runtime_id,row.final_row_id])===key)
      if(item && !['sent','unknown','failed','rejected','cancelled','expired','disabled','dispatching'].includes(item.state)) {
        item.revision++;item.state='cancelled'
      }
      value={ok:true,state:item?.state||'cancelled',revision:item?.revision||1}
    } else if(request.action==='completion_recovery_touch') {
      const item=candidates.get(request.candidate.candidate_id)
      if(item && request.candidate.generation===item.generation && request.bookkeeping_seq>item.bookkeeping_seq) {
        item.revision++;item.bookkeeping_seq=request.bookkeeping_seq
        item.quiet_at=Math.max(item.quiet_at,request.quiet_at);item.state='pending'
      }
      value={ok:Boolean(item && request.candidate.generation===item.generation),revision:item?.revision,
        bookkeeping_seq:item?.bookkeeping_seq,quiet_at:item?.quiet_at}
    } else if(request.action==='completion_recovery_claim') {
      const item=candidates.get(request.candidate.candidate_id)
      if(claimHook) await claimHook(item,request)
      if(!item || item.revision!==request.candidate.revision || request.candidate.generation!==item.generation) value={ok:false,reason:'candidate_changed'}
      else if(item.state==='claimed' && item.claim_until>now/1000) value={ok:false,reason:'claim_busy',retry_at:item.claim_until}
      else {
        item.revision++;item.state='claimed';item.claim_until=now/1000+20
        value={ok:true,candidate_id:item.candidate_id,revision:item.revision,claim_token:'b'.repeat(32),
          claim_until:item.claim_until,state:'claimed',generation:item.generation,
          bookkeeping_seq:item.bookkeeping_seq,quiet_at:item.quiet_at,expires_at:item.expires_at}
      }
    } else if(request.action==='completion_recovery_classify') {
      value={ok:true,kind:classification}
    } else if(request.action==='completion_recovery_send') {
      const item=candidates.get(request.candidate.candidate_id)
      if(!item || request.candidate.generation!==item.generation) {
        value={ok:false,phase:'pre_dispatch',retryable:false,reason:'generation_changed'}
        return {blocked:false,code:0,output:frame(value)}
      }
      if(sendOutcome==='throw') throw new Error('simulated transport loss before receipt')
      if(sendOutcome==='pre_dispatch') value={ok:false,phase:'pre_dispatch',retryable:true,reason:'state_unavailable'}
      else if(sendOutcome==='attention_required') value={ok:false,phase:'pre_dispatch',retryable:false,reason:'attention_required'}
      else if(sendOutcome==='dispatching') {
        if(item){item.revision++;item.state='dispatching'}
        throw new Error('simulated ambiguous send result')
      } else {
        if(item){item.revision++;item.state='sent'}
        value={ok:true,status:'sent',verified:true}
      }
    } else if(request.action==='send') {
      legacySends.push(structuredClone(request))
      value={ok:true,status:'sent',verified:true}
    } else value={ok:false,reason:'unknown_action'}
    return {blocked:false,code:0,output:frame(value)}
  }
  const gateway={request:(method,args,timeout)=>dispatch('captured-active-gateway',method,args,timeout)}
  const api={state,getGateway:()=>socket==='open'?gateway:null,
    notify:value=>notices.push(value),profileRoutes:async()=>routes,
    requestProfile:(r,method,args,timeout)=>dispatch(`profile:${r.profile}`,method,args,timeout)}
  const ctx={setTimeout:(fn,delay)=>{const timer={fn,delay,cancelled:false};timers.push(timer);return()=>{timer.cancelled=true}}}
  const monitor=new CompletionMonitor(api,ctx,()=>now)
  const currentRecord=()=>monitor.records.get(JSON.stringify(['local',profile,'runtime-1']))
  const complete=(seq=2,finalId=42)=>event('message.complete',{status:'complete',persisted_turn:{final_assistant_row_id:finalId}},seq)
  const advance=ms=>{now+=ms}
  const check=async()=>{advance(QUIET_MS+1);await monitor.check(currentRecord())}
  return {monitor,api,state,gateway,routes,snapshots,candidates,tombstones,calls,requests,timers,notices,legacySends,ctx,
    event,complete,currentRecord,advance,check,setNow:v=>{now=v},now:()=>now,setSocket:v=>{socket=v},setProfile:v=>{activeProfile=v},
    setPolicy:v=>{currentPolicy=structuredClone(v)},setClassification:v=>{classification=v},
    setSendOutcome:v=>{sendOutcome=v},setClaimHook:v=>{claimHook=v},itemFor}
}
async function flush(){for(let i=0;i<10;i++)await new Promise(resolve=>setImmediate(resolve))}
async function ready(f){await f.monitor.mounted();await flush()}
async function register(f,seq=2,final=42){f.complete(seq,final);await flush();return f.currentRecord()?.candidate}
const nativeTail=(runtime='runtime-1',key='durable-1')=>[
  {type:'status.update',session_id:runtime,seq:3,payload:{kind:'goal',text:'Goal complete'}},
  {type:'status.update',session_id:runtime,seq:4,payload:{kind:'loop',text:'Loop tick settled'}},
  {type:'session.control.update',session_id:runtime,seq:5,payload:{control:{goal:{status:'done'},loop:{status:'done'},heartbeat:null}}},
  {type:'session.info',session_id:runtime,seq:6,payload:{running:false,stored_session_id:key,profile_name:'claire'}}
]

test('named-profile recovery uses one captured local socket and fixed default-home CLI argv',async()=>{
  const f=fixture({profile:'claire'});await ready(f);await register(f)
  await f.check()
  const recoveryActions=f.requests.filter(x=>x.request.action.startsWith('completion_recovery_'))
  assert.ok(recoveryActions.length>0)
  assert.ok(recoveryActions.every(x=>x.transport==='captured-active-gateway'))
  assert.equal(f.calls.some(x=>x.method==='cli.exec'&&x.transport==='profile:default'),false)
  assert.ok(f.calls.some(x=>x.method==='session.active_list'&&x.transport==='captured-active-gateway'))
  assert.ok(recoveryActions.every(x=>x.request.action==='completion_recovery_policy' ||
    x.request.action==='completion_recovery_list' || x.request.action==='completion_recovery_record' ||
    x.request.action==='completion_recovery_claim' || x.request.action==='completion_recovery_classify' ||
    x.request.action==='completion_recovery_send'))
  assert.deepEqual(f.requests.find(x=>x.request.action==='completion_recovery_send')?.request.route,
    {mode:'local',connectionId:'local',profile:'claire',targetProfile:'claire'})
  assert.equal(f.candidates.values().next().value.state,'sent')
})

test('nonforeground fresh completion keeps legacy all-profile send and is not recovery-admitted',async()=>{
  const f=fixture({profile:'default'});await ready(f)
  f.event('message.complete',{status:'complete',persisted_turn:{final_assistant_row_id:77}},2,
    {profile:'claire',connectionId:'local'})
  await flush();f.advance(QUIET_MS+1)
  const record=f.monitor.records.get(JSON.stringify(['local','claire','runtime-1']))
  const candidate=record.candidate
  assert.equal(candidate.recoveryGeneration,null)
  await f.monitor.check(record)
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_record').length,0)
  assert.equal(f.legacySends.length,1)
  assert.equal(f.legacySends[0].action,'send')
  assert.equal(f.legacySends[0].profile,'claire')
})

test('installed native post-turn goal/loop/control/session.info tail advances the bookkeeping watermark once',async()=>{
  const f=fixture({profile:'claire'});await ready(f);await register(f)
  const originalQuiet=f.currentRecord().candidate.quietAt
  f.snapshots['session.events.since']={events:nativeTail(),latest_seq:6,truncated:false,epoch:'epoch-a',open_requests:[]}
  await f.check()
  const item=f.candidates.values().next().value
  assert.equal(item.bookkeeping_seq,6)
  assert.equal(item.seq,2,'admission watermark stays immutable')
  assert.ok(item.quiet_at*1000>originalQuiet)
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_touch').length,1)
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_send').length,0)
  await f.check()
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_touch').length,1,
    'replaying the same native done events must not renew quiet')
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_send').length,1)
})

test('reload verifies already-bookkept native replay without touching quiet a second time',async()=>{
  const f=fixture({profile:'claire'});await ready(f);await register(f)
  const item=f.candidates.values().next().value
  item.revision++
  item.bookkeeping_seq=3
  item.quiet_at=item.created_at
  f.snapshots['session.events.since']={events:[{type:'session.control.update',session_id:'runtime-1',seq:3,
    payload:{control:{goal:{status:'done'},loop:null,heartbeat:null}}}],latest_seq:3,truncated:false,
    epoch:'epoch-a',open_requests:[]}
  f.snapshots['session.control.read']={control:{goal:{status:'done'},loop:null,heartbeat:null}}
  f.setNow(item.quiet_at*1000+QUIET_MS+1)
  f.monitor.dispose()
  f.monitor=new CompletionMonitor(f.api,f.ctx,()=>f.now())
  await ready(f)
  const restored=f.monitor
  const restoredRecord=restored.records.get(JSON.stringify(['local','claire','runtime-1']))
  assert.ok(restoredRecord)
  await restored.check(restoredRecord)
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_touch').length,0)
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_send').length,1)
  assert.equal(item.state,'sent')
})

test('socket loss and foreground changes wait; gateway.ready restores despite loadedRecorded',async()=>{
  const f=fixture({profile:'claire'});await ready(f);await register(f)
  const item=f.candidates.values().next().value
  f.setSocket('connecting');await f.check()
  assert.notEqual(item.state,'cancelled')
  assert.equal(f.requests.some(x=>x.request.action==='completion_recovery_cancel'),false)
  f.setSocket('open');f.setProfile('default');f.monitor.onEvent({type:'gateway.ready'});await flush()
  assert.equal(f.monitor.loadedRecorded,true)
  assert.notEqual(item.state,'cancelled')
  f.setProfile('claire');f.monitor.onEvent({type:'gateway.ready'});await flush()
  assert.equal(f.monitor.records.size>0,true)
  await f.check()
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_send').length,1)
})

test('stale claim loser reads exact state and never cancels the winner',async()=>{
  const f=fixture({profile:'claire'});await ready(f);await register(f)
  f.setClaimHook(item=>{item.revision++;item.state='claimed';item.claim_until=1_800_000_100})
  await f.check()
  const item=f.candidates.values().next().value
  assert.equal(item.state,'claimed')
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_get').length,1)
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_cancel').length,0)
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_send').length,0)
  assert.ok(f.currentRecord().candidate)
})

test('foreground switch after claim cannot dispatch through another active profile socket',async()=>{
  const f=fixture({profile:'claire'});await ready(f);await register(f)
  f.setClaimHook(()=>{f.setProfile('default')})
  await f.check()
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_send').length,0)
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_cancel').length,0)
  assert.equal(f.candidates.values().next().value.state,'claimed')
  f.setClaimHook(null);f.setProfile('claire');f.advance(21_000)
  await f.monitor.check(f.currentRecord())
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_send').length,1)
  assert.equal(f.candidates.values().next().value.state,'sent')
})

test('observed OFF and generation rotation retire captured candidates without ON rebind',async()=>{
  const f=fixture({profile:'claire'});await ready(f);const candidate=await register(f)
  const originalId=candidate.recoveryId
  f.setPolicy({ok:true,enabled:false,generation:'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'})
  await f.monitor.refreshRecoveryPolicy();await flush()
  assert.equal(candidate.recoveryGeneration,null)
  assert.equal(f.candidates.get(originalId).state,'cancelled')
  f.setPolicy({ok:true,enabled:true,generation:'cccccccccccccccccccccccccccccccc'})
  await f.monitor.refreshRecoveryPolicy();await flush()
  assert.equal(candidate.recoveryGeneration,null)
  assert.equal(candidate.recoveryRetired,true)
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_record').length,1)
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_send').length,0)
  await f.check()
  assert.equal(f.legacySends.length,0,'policy rotation must not route the old completion through ordinary send')
})

test('typed pre-dispatch failure retains candidate and exact unsent state can retry within original TTL',async()=>{
  const f=fixture({profile:'claire'});await ready(f);await register(f)
  f.setSendOutcome('pre_dispatch');await f.check()
  const candidate=f.currentRecord().candidate
  assert.ok(candidate)
  assert.notEqual(candidate.state,'cancelled')
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_get').length,1)
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_cancel').length,0)
  f.setSendOutcome('sent');f.advance(21_000);await f.monitor.check(f.currentRecord())
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_send').length,2)
  assert.equal(f.candidates.values().next().value.state,'sent')
})

test('ambiguous dispatching outcome is exact-read, terminal locally, and never replayed',async()=>{
  const f=fixture({profile:'claire'});await ready(f);await register(f)
  f.setSendOutcome('dispatching');await f.check()
  assert.equal(f.candidates.values().next().value.state,'dispatching')
  assert.equal(f.currentRecord().candidate,null)
  await f.monitor.check(f.currentRecord())
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_send').length,1)
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_cancel').length,0)
})

test('attention classification hands off once through legacy send under fresh renderer gates',async()=>{
  const f=fixture({profile:'claire'});await ready(f);await register(f)
  f.setClassification('attention')
  f.snapshots['session.active_list']={sessions:[{id:'runtime-1',status:'waiting',session_key:'durable-1',started_at:1700000000}]}
  f.snapshots['session.events.since']={events:[],latest_seq:2,truncated:false,epoch:'epoch-a',
    open_requests:[{id:'srq-aabbccdd',method:'clarify',params:{session_id:'runtime-1',question:'Continue?'}}]}
  await f.check()
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_send').length,0)
  assert.equal(f.legacySends.length,1)
  assert.equal(f.legacySends[0].action,'send')
  await f.check()
  assert.equal(f.legacySends.length,1)
})

test('completion OFF at fresh capture still routes awaiting_answer through ordinary attention send',async()=>{
  const f=fixture({profile:'claire',policy:{ok:true,enabled:false,generation:'dddddddddddddddddddddddddddddddd'}})
  await ready(f);await register(f)
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_record').length,0)
  f.setClassification('attention')
  f.snapshots['session.active_list']={sessions:[{id:'runtime-1',status:'waiting',session_key:'durable-1',started_at:1700000000}]}
  f.snapshots['session.events.since']={events:[{type:'message.complete',session_id:'runtime-1',seq:2,
    payload:{status:'complete',persisted_turn:{final_assistant_row_id:42}}}],latest_seq:2,truncated:false,epoch:'epoch-a',
    open_requests:[{id:'srq-aabbccdd',method:'clarify',params:{session_id:'runtime-1',question:'Continue?'}}]}
  await f.check()
  assert.equal(f.legacySends.length,1)
  assert.equal(f.legacySends[0].action,'send')
})

test('an event observed while OFF uses ordinary classification after ON but is never recovery-admitted',async()=>{
  const f=fixture({profile:'claire',policy:{ok:true,enabled:false,generation:'dddddddddddddddddddddddddddddddd'}})
  await ready(f);const candidate=await register(f)
  f.setPolicy({ok:true,enabled:true,generation:'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'})
  await f.monitor.refreshRecoveryPolicy();await flush()
  assert.equal(candidate.recoveryGeneration,null)
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_record').length,0)
  f.snapshots['session.events.since']={events:[{type:'message.complete',session_id:'runtime-1',seq:2,
    payload:{status:'complete',persisted_turn:{final_assistant_row_id:42}}}],latest_seq:2,truncated:false,
    epoch:'epoch-a',open_requests:[]}
  await f.check()
  assert.equal(f.legacySends.length,1)
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_send').length,0)
})

test('retired completion may hand off native attention but cannot resend a normal completion',async()=>{
  const f=fixture({profile:'claire'});await ready(f);await register(f)
  f.setPolicy({ok:true,enabled:false,generation:'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'})
  await f.monitor.refreshRecoveryPolicy();await flush()
  f.setClassification('attention')
  f.snapshots['session.active_list']={sessions:[{id:'runtime-1',status:'waiting',session_key:'durable-1',started_at:1700000000}]}
  f.snapshots['session.events.since']={events:[{type:'message.complete',session_id:'runtime-1',seq:2,
    payload:{status:'complete',persisted_turn:{final_assistant_row_id:42}}}],latest_seq:2,truncated:false,epoch:'epoch-a',
    open_requests:[{id:'srq-aabbccdd',method:'clarify',params:{session_id:'runtime-1',question:'Continue?'}}]}
  await f.check()
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_send').length,0)
  assert.equal(f.legacySends.length,1)
  assert.equal(f.legacySends[0].action,'send')
})

test('bounded list keyset paging consumes multiple pages without truncating identifiers',async()=>{
  const f=fixture({profile:'claire'});await ready(f)
  f.candidates.clear()
  for(let n=1;n<=35;n++) {
    const id=n.toString(16).padStart(64,'0')
    const candidate={route:{mode:'local',connectionId:'local',profile:'claire',targetProfile:'claire'},
      runtime_id:`runtime-${n}`,session_key:`durable-${n}`,started_at:'1700000000',epoch:'epoch-a',seq:2,
      final_row_id:100+n,created_at:1_800_000_000,quiet_at:1_800_000_000,generation:GEN}
    const item=f.itemFor(candidate,id);f.candidates.set(id,item)
  }
  f.requests.length=0
  await f.monitor.mounted(true);await flush()
  const pages=f.requests.filter(x=>x.request.action==='completion_recovery_list')
  assert.equal(pages.length,3)
  assert.ok(pages.every(x=>x.request.cursor===null || /^[0-9a-f]{64}$/u.test(x.request.cursor)))
  assert.equal(f.monitor.records.size,35)
  assert.ok(pages.every(x=>x.transport==='captured-active-gateway'))
})

test('claim-time fresh done event advances quiet and prevents dispatch until its watermark is touched',async()=>{
  const f=fixture({profile:'claire'});await ready(f);await register(f)
  const oldQuiet=f.currentRecord().candidate.quietAt
  f.setClaimHook(async()=>{
    f.snapshots['session.events.since']={events:[{type:'status.update',session_id:'runtime-1',seq:3,
      payload:{kind:'goal',text:'Goal settled'}}],latest_seq:3,truncated:false,epoch:'epoch-a',open_requests:[]}
    f.event('status.update',{kind:'goal',text:'Goal settled'},3)
    await flush()
  })
  await f.check()
  const candidate=f.currentRecord().candidate
  assert.equal(candidate.bookkeepingSeq,3)
  assert.ok(candidate.quietAt>oldQuiet)
  assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_send').length,0)
  assert.equal(f.candidates.values().next().value.bookkeeping_seq,3)
})

test('malformed native tail is retained, while validated identity change cancels',async()=>{
  const malformed=fixture({profile:'claire'});await ready(malformed);await register(malformed)
  malformed.snapshots['session.events.since']={events:[{type:'status.update',session_id:'runtime-1',seq:3,
    payload:{kind:'process',text:'running'}}],latest_seq:3,truncated:false,epoch:'epoch-a',open_requests:[]}
  await malformed.check()
  assert.notEqual(malformed.candidates.values().next().value.state,'cancelled')
  assert.equal(malformed.requests.filter(x=>x.request.action==='completion_recovery_send').length,0)

  const changed=fixture({profile:'claire'});await ready(changed);await register(changed)
  changed.snapshots['session.events.since']={events:[{type:'session.info',session_id:'runtime-1',seq:3,
    payload:{running:false,stored_session_id:'different-durable',profile_name:'claire'}}],latest_seq:3,
    truncated:false,epoch:'epoch-a',open_requests:[]}
  await changed.check()
  assert.equal(changed.candidates.values().next().value.state,'cancelled')
})

function isolateCompletionSweep(monitor) {
  monitor.refreshRecoveryPolicy=async()=>{}
  monitor.pumpReplies=async()=>{}
  monitor.pumpApprovals=async()=>{}
  monitor.pumpFollowups=async()=>{}
}

test('sweep never pool-dials recovery after socket changes at route grouping',async()=>{
  const f=fixture();await ready(f);await register(f);isolateCompletionSweep(f.monitor)
  const before=f.calls.length, record=f.currentRecord(), candidate=record.candidate
  const live=f.monitor.currentLiveRoute.bind(f.monitor);let switched=false
  f.monitor.currentLiveRoute=route=>{
    const result=live(route)
    if(result&&!switched){switched=true;f.setSocket('connecting')}
    return result
  }
  await f.monitor.sweep()
  assert.equal(f.calls.slice(before).filter(c=>c.transport.startsWith('profile:')).length,0)
  assert.equal(f.currentRecord(),record);assert.equal(record.candidate,candidate)
  assert.equal(f.candidates.values().next().value.state,'pending')
})

test('sweep treats retired recovery metadata as live-only after foreground changes',async()=>{
  const f=fixture();await ready(f);await register(f);isolateCompletionSweep(f.monitor)
  const record=f.currentRecord(), candidate=record.candidate
  candidate.recoveryRetired=true;candidate.recoveryGeneration=null;f.setProfile('default')
  const before=f.calls.length;await f.monitor.sweep()
  assert.equal(f.calls.slice(before).filter(c=>c.transport.startsWith('profile:')).length,0)
  assert.equal(f.currentRecord(),record);assert.equal(record.candidate,candidate)
})

test('sweep cannot use a stale socket empty response as definitive absence',async()=>{
  const f=fixture();await ready(f);await register(f);isolateCompletionSweep(f.monitor)
  const record=f.currentRecord(),candidate=record.candidate
  const stale=async()=>{f.setProfile('default');return {sessions:[]}}
  f.gateway.request=stale;f.api.requestProfile=stale
  await f.monitor.sweep()
  assert.equal(f.currentRecord(),record);assert.equal(record.candidate,candidate)
})

test('same live captured socket can establish definitive recovery runtime absence',async()=>{
  const f=fixture();await ready(f);await register(f);isolateCompletionSweep(f.monitor)
  f.snapshots['session.active_list']={sessions:[]}
  await f.monitor.sweep();await flush()
  assert.equal(f.currentRecord(),undefined)
  assert.equal(f.candidates.values().next().value.state,'cancelled')
})

// The registered native CLI produces these receipts in test_recovery_review.py;
// Python asserts complete wire equality so this is not a hand-authored success.
const nativeExpiryFrames=JSON.parse(await (await import('node:fs/promises')).readFile(
  new URL('./fixtures/recovery-expiry-results.json',import.meta.url),'utf8'))
for(const kind of ['claim','claim_retry']){
  test(`native ${kind} rollback receipt keeps recovery and reacquires once`,async()=>{
    let blocked=false,sends=0
    const f=fixture({beforeHelper:(request)=>{
      if(request.action==='completion_recovery_send'){
        if(!blocked){blocked=true;return structuredClone(nativeExpiryFrames[kind])}
        sends++
      }
    }})
    await ready(f);await register(f)
    const expiry=f.currentRecord().candidate.expiresAt
    await f.check()
    assert.ok(f.currentRecord().candidate)
    assert.equal(f.currentRecord().candidate.unknownOutcome??false,false)
    assert.equal(sends,0)
    assert.equal(f.currentRecord().candidate.expiresAt,expiry)
    await f.check()
    assert.equal(sends,1)
    assert.equal(f.currentRecord().candidate,null)
    assert.equal(f.notices.length,0)
  })
}
for(const kind of ['ttl','ttl_retry']){
  test(`native ${kind} rollback receipt ends as expiry without unknown warning`,async()=>{
    const f=fixture({beforeHelper:request=>request.action==='completion_recovery_send'
      ?structuredClone(nativeExpiryFrames[kind]):undefined})
    await ready(f);await register(f);await f.check()
    assert.equal(f.requests.filter(x=>x.request.action==='completion_recovery_send').length,1)
    assert.notEqual(f.candidates.values().next().value.state,'sent')
    assert.equal(f.currentRecord().candidate,null)
    assert.equal(f.currentRecord().settledFinals.has(42),true)
    assert.equal(f.notices.length,0)
  })
}

import test from 'node:test'
import assert from 'node:assert/strict'
import {registerHooks} from 'node:module'
registerHooks({resolve(specifier, context, nextResolve) {
  if (specifier === '@hermes/plugin-sdk') return {url:'data:text/javascript,export const host={}', shortCircuit:true}
  if (specifier === 'react/jsx-runtime') return {url:'data:text/javascript,export const jsx=(type,props,key)=>({type,props,key})', shortCircuit:true}
  return nextResolve(specifier, context)
}})
const {CompletionMonitor, VERSION, APPROVAL_COMPAT_VERSION, APPROVAL_CAPABILITY,
  COMPLETION_RECOVERY_CAPABILITY, QUIET_MS, NativeHelperError} = await import('./desktop/plugin.js')
const BEGIN='HERMES_TELEGRAM_BRIDGE_RESULT_V1_BEGIN', END='HERMES_TELEGRAM_BRIDGE_RESULT_V1_END'
const route={mode:'local',connectionId:'local',profile:'default',targetProfile:'default'}
const frame=value=>`${BEGIN}\n${Buffer.from(JSON.stringify(value),'utf8').toString('base64url')}\n${END}\n`
const readFrame=output=>JSON.parse(Buffer.from(output.split(/\r?\n/u)[1],'base64url').toString('utf8'))

function fixture({loaded='new', profile='default', policy={ok:true,enabled:true,generation:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'}}={}) {
  let now=1_800_000_000_000
  const calls=[], helpers=[], timers=[], notices=[], legacySends=[]
  const defaultRoute={mode:'local',connectionId:'local',profile:'default',targetProfile:'default'}
  const namedRoute={mode:'local',connectionId:'local',profile,targetProfile:profile}
  const routes=profile==='default'?[defaultRoute]:[defaultRoute,namedRoute]
  const state={gateway:{get:()=> 'open'},connectionId:{get:()=> 'local'},profile:{get:()=> profile}}
  const gateway={request:async(method,args,timeout)=>dispatch('gateway',method,args,timeout)}
  const snapshots={
    'session.active_list':{sessions:[{id:'runtime-1',status:'idle',session_key:'durable-1',started_at:1700000000}]},
    'subagent.list':{subagents:[],delegations:[]},
    'process.list':{processes:[]},
    'session.control.read':{control:{goal:null,loop:null,heartbeat:null}},
    'session.events.since':{events:[],latest_seq:2,truncated:false,epoch:'epoch-a',open_requests:[]}
  }
  const candidates=new Map(), tombstones=new Set()
  let currentPolicy=structuredClone(policy)
  let classification='completion'
  let sendOutcome='sent'
  let claimHook=null
  function keyFor(c) {return JSON.stringify([c.route.connectionId,c.route.profile,c.runtime_id,c.final_row_id])}
  function itemFor(c,revision=1,state='pending') {
    return {candidate_id:'a'.repeat(64),revision,state,route:structuredClone(c.route),runtime_id:c.runtime_id,
      session_key:c.session_key,started_at:c.started_at,epoch:c.epoch,seq:c.seq,bookkeeping_seq:c.seq,
      final_row_id:c.final_row_id,created_at:c.created_at,quiet_at:c.quiet_at,expires_at:c.created_at+1800,
      generation:c.generation}
  }
  async function dispatch(transport,method,args,timeout) {
    calls.push({transport,method,args:structuredClone(args),timeout})
    if(method==='cli.exec') {
      assert.deepEqual(args.argv.slice(0,4),['--profile','default','desktop-telegram-bridge','--request'])
      const request=JSON.parse(Buffer.from(args.argv[4],'base64url').toString('utf8'))
      helpers.push(structuredClone(request))
      let value
      if(request.action==='loaded') {
        value={ok:true,version:APPROVAL_COMPAT_VERSION,
          ...(loaded==='new'?{package_version:VERSION,completion_policy:structuredClone(currentPolicy)}:{}),
          capabilities:loaded==='new'?[APPROVAL_CAPABILITY,COMPLETION_RECOVERY_CAPABILITY]:[APPROVAL_CAPABILITY]}
      } else if(request.action==='completion_recovery_policy') {
        value=structuredClone(currentPolicy)
      } else if(request.action==='completion_recovery_record') {
        const c=request.candidate
        const key=keyFor(c)
        if(tombstones.has(key)) value={ok:false,reason:'candidate_terminal'}
        else {
          const existing=[...candidates.values()].find(x=>keyFor(x)===key)
          if(existing) value={ok:true,duplicate:true,...structuredClone(existing)}
          else {
            const item=itemFor(c)
            candidates.set(item.candidate_id,item)
            value={ok:true,...structuredClone(item)}
          }
        }
      } else if(request.action==='completion_recovery_list') {
        const r=request.route
        const all=[...candidates.values()].filter(x=>x.route.connectionId===r.connectionId &&
          x.route.profile===r.profile && x.route.targetProfile===r.targetProfile &&
          ['pending','read_blocked','claimed','unsent_retryable'].includes(x.state)).sort((a,b)=>a.candidate_id.localeCompare(b.candidate_id))
        const after=all.filter(x=>!request.cursor || x.candidate_id>request.cursor)
        const items=after.slice(0,16)
        value={ok:true,version:VERSION,items:structuredClone(items),cursor:after.length>16?items.at(-1).candidate_id:null}
      } else if(request.action==='completion_recovery_get') {
        const item=candidates.get(request.candidate_id)
        const matches=item && item.route.connectionId===request.route.connectionId &&
          item.route.profile===request.route.profile && item.route.targetProfile===request.route.targetProfile
        value={ok:true,version:VERSION,item:matches?structuredClone(item):null}
      } else if(request.action==='completion_recovery_cancel') {
        const i=request.identity,key=keyFor({route:i.route,runtime_id:i.runtime_id,final_row_id:i.final_row_id})
        tombstones.add(key)
        const item=[...candidates.values()].find(x=>keyFor(x)===key)
        if(item && !['sent','unknown','failed','rejected','cancelled','expired','disabled','dispatching'].includes(item.state)) {
          item.state='cancelled';item.revision++
        }
        value={ok:true,state:item?.state||'cancelled',revision:item?.revision||1}
      } else if(request.action==='completion_recovery_read_blocked') {
        const item=candidates.get(request.candidate.candidate_id)
        if(item){item.revision++;item.state='read_blocked'}
        value={ok:Boolean(item),candidate_id:request.candidate.candidate_id,revision:item?.revision,state:item?.state}
      } else if(request.action==='completion_recovery_touch') {
        const item=candidates.get(request.candidate.candidate_id)
        if(item && request.candidate.generation===item.generation) {
          if(request.bookkeeping_seq>item.bookkeeping_seq) {
            item.revision++;item.bookkeeping_seq=request.bookkeeping_seq
            item.quiet_at=Math.max(item.quiet_at,request.quiet_at);item.state='pending'
          }
        }
        value={ok:Boolean(item && request.candidate.generation===item.generation),revision:item?.revision,
          bookkeeping_seq:item?.bookkeeping_seq,quiet_at:item?.quiet_at}
      } else if(request.action==='completion_recovery_claim') {
        const item=candidates.get(request.candidate.candidate_id)
        if(claimHook) await claimHook(item,request)
        if(!item || item.revision!==request.candidate.revision || request.candidate.generation!==item.generation)value={ok:false,reason:'candidate_changed'}
        else if(item.state==='claimed' && item.claim_until>now/1000)value={ok:false,reason:'claim_busy',retry_at:item.claim_until}
        else {
          item.revision++;item.state='claimed';item.claim_until=now/1000+20
          value={ok:true,candidate_id:item.candidate_id,revision:item.revision,
            claim_token:'b'.repeat(32),claim_until:item.claim_until,state:'claimed',
            generation:item.generation,bookkeeping_seq:item.bookkeeping_seq,quiet_at:item.quiet_at,expires_at:item.expires_at}
        }
      } else if(request.action==='completion_recovery_classify') {
        value={ok:true,kind:classification}
      } else if(request.action==='completion_recovery_send') {
        const item=candidates.get(request.candidate.candidate_id)
        if(!item || request.candidate.generation!==item.generation) {
          value={ok:false,phase:'pre_dispatch',retryable:false,reason:'generation_changed'}
          return {blocked:false,code:0,output:frame(value)}
        }
        if(sendOutcome==='throw') throw new Error('simulated helper transport loss')
        if(sendOutcome==='pre_dispatch') value={ok:false,phase:'pre_dispatch',retryable:true,reason:'state_unavailable'}
        else if(sendOutcome==='attention_required') value={ok:false,phase:'pre_dispatch',retryable:false,reason:'attention_required'}
        else if(sendOutcome==='dispatching') {
          if(item){item.revision++;item.state='dispatching'}
          throw new Error('simulated ambiguous dispatch')
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
    const value=snapshots[method]
    if(value instanceof Error)throw value
    const cloned=structuredClone(value)
    if(method==='session.events.since' && Array.isArray(cloned?.events)) {
      cloned.events=cloned.events.filter(item=>Number.isSafeInteger(item.seq) && item.seq>(args?.last_seen||0))
    }
    return cloned
  }
  const api={state,getGateway:()=>gateway,notify:value=>notices.push(value),profileRoutes:async()=>routes,
    requestProfile:async(_route,method,args,timeout)=>dispatch('requestProfile',method,args,timeout)}
  const ctx={setTimeout:(fn,delay)=>{const timer={fn,delay,cancelled:false};timers.push(timer);return()=>{timer.cancelled=true}}}
  const monitor=new CompletionMonitor(api,ctx,()=>now)
  const event=(type,payload={},seq=1,extra={})=>monitor.onEvent({type,payload,seq,session_id:'runtime-1',profile,connectionId:'local',...extra})
  const complete=(seq=2,finalId=42)=>event('message.complete',{status:'complete',persisted_turn:{final_assistant_row_id:finalId}},seq)
  const record=()=>monitor.records.get(JSON.stringify(['local',profile,'runtime-1']))
  const advance=ms=>{now+=ms}
  const check=async()=>{advance(QUIET_MS+1);await monitor.check(record())}
  return {monitor,api,state,gateway,routes,snapshots,candidates,tombstones,calls,helpers,timers,notices,legacySends,
    event,complete,record,advance,check,setNow:v=>{now=v},setPolicy:v=>{currentPolicy=structuredClone(v)},
    setClassification:v=>{classification=v},setSendOutcome:v=>{sendOutcome=v},setClaimHook:v=>{claimHook=v}}
}
async function flush(){for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve))}
async function registerFresh(f){f.complete();await flush();return f.record()?.candidate}

test('fresh persisted completion metadata crosses framed CLI only, with no content or destination',async()=>{
  const f=fixture();await f.monitor.mounted();await registerFresh(f)
  const record=f.helpers.find(x=>x.action==='completion_recovery_record')
  assert.ok(record)
  assert.deepEqual(Object.keys(record.candidate).sort(),[
    'created_at','epoch','final_row_id','generation','quiet_at','route','runtime_id','seq','session_key','started_at'
  ])
  const serialized=JSON.stringify(record)
  for(const forbidden of ['title','preview','Telegram','chat_id','bot_token','final response'])assert.equal(serialized.includes(forbidden),false,forbidden)
  assert.equal(f.candidates.size,1)
  assert.deepEqual(f.calls.filter(c=>c.method==='cli.exec').map(c=>c.args.argv[2]),[
    'desktop-telegram-bridge','desktop-telegram-bridge','desktop-telegram-bridge'])
})

test('duplicate completion callback cannot reset candidate watermark, quiet time, or lifetime',async()=>{
  const f=fixture();await f.monitor.mounted();const first=await registerFresh(f)
  const original={seq:first.seq,at:first.at,createdAt:first.createdAt,expiresAt:first.expiresAt}
  f.advance(10_000);f.complete(4,42);await flush()
  assert.equal(f.record().candidate,first)
  assert.deepEqual({seq:first.seq,at:first.at,createdAt:first.createdAt,expiresAt:first.expiresAt},original)
  assert.equal(f.helpers.filter(x=>x.action==='completion_recovery_record').length,1)
})

test('late candidate registration is fenced by a cancellation tombstone after new work',async()=>{
  const f=fixture();await f.monitor.mounted()
  let release,started
  const reached=new Promise(resolve=>{started=resolve})
  const original=f.gateway.request
  f.gateway.request=async(method,args,timeout)=>{
    if(method==='cli.exec'){
      const request=JSON.parse(Buffer.from(args.argv[4],'base64url').toString('utf8'))
      if(request.action==='completion_recovery_record') {started();await new Promise(resolve=>{release=resolve})}
    }
    return original(method,args,timeout)
  }
  f.complete();await reached
  f.event('message.start',{},3);await flush()
  assert.ok(f.tombstones.size>=1)
  release();await flush()
  assert.equal(f.record().candidate,null)
  assert.equal(f.candidates.size,0)
  assert.equal(f.helpers.some(x=>x.action==='completion_recovery_send'),false)
})

test('reload restores the same live route/runtime and sends once through framed helper without session creation',async()=>{
  const f=fixture();await f.monitor.mounted();await registerFresh(f)
  f.monitor.dispose()
  const restored=new CompletionMonitor(f.api,{setTimeout:(fn,delay)=>{const t={fn,delay,cancelled:false};f.timers.push(t);return()=>{t.cancelled=true}}},()=>1_800_000_000_000+QUIET_MS+1)
  f.monitor=restored
  await restored.mounted();await flush()
  const record=[...restored.records.values()][0]
  assert.equal(record.candidate.recoveryId,'a'.repeat(64))
  await restored.check(record)
  assert.equal(f.helpers.filter(x=>x.action==='completion_recovery_send').length,1)
  assert.equal([...f.candidates.values()][0].state,'sent')
  assert.equal(f.calls.some(c=>['session.create','session.resume','session.activate','prompt.submit'].includes(c.method)),false)
})

test('malformed snapshots retain bounded candidates while validated absence cancels them',async()=>{
  const f=fixture();await f.monitor.mounted();await registerFresh(f)
  const candidate=[...f.candidates.values()][0]
  f.snapshots['session.active_list']={wrong_shape:[]}
  await f.check()
  assert.notEqual(candidate.state,'cancelled')
  assert.equal(f.helpers.some(x=>x.action==='completion_recovery_send'),false)

  const g=fixture();await g.monitor.mounted();await registerFresh(g)
  g.snapshots['session.active_list']={sessions:[]}
  await g.check()
  assert.equal([...g.candidates.values()][0].state,'cancelled')
  assert.equal(g.helpers.some(x=>x.action==='completion_recovery_send'),false)
})

test('truncated replay is retained read-blocked and new replayed work is never sent',async()=>{
  const f=fixture();await f.monitor.mounted();await registerFresh(f)
  f.snapshots['session.events.since']={events:[],latest_seq:4,truncated:true,epoch:'epoch-a',open_requests:[]}
  await f.check()
  assert.notEqual([...f.candidates.values()][0].state,'cancelled')
  assert.equal(f.helpers.some(x=>x.action==='completion_recovery_send'),false)

  const g=fixture();await g.monitor.mounted();await registerFresh(g)
  g.snapshots['session.events.since']={events:[{type:'message.start',session_id:'runtime-1',seq:3}],latest_seq:3,truncated:false,epoch:'epoch-a',open_requests:[]}
  await g.check()
  assert.equal([...g.candidates.values()][0].state,'cancelled')
  assert.equal(g.helpers.some(x=>x.action==='completion_recovery_send'),false)
})

test('connection/profile changes never dial a recovery profile backend',async()=>{
  const f=fixture();f.state.gateway.get=()=> 'open';f.state.connectionId.get=()=> 'elsewhere';f.state.profile.get=()=> 'claire'
  f.monitor.recoveryCapable=true
  f.complete();await flush()
  assert.equal(f.calls.some(c=>c.method==='cli.exec'),false)
  assert.equal(f.calls.some(c=>c.method!=='cli.exec'),false)
  const error=await f.monitor.helper({action:'completion_recovery_list',route})
    .then(()=>null,error=>error)
  assert.ok(error instanceof NativeHelperError)
  assert.equal(f.calls.some(c=>c.method==='cli.exec'),false)
})

test('1.8.0 approval-v4 helper stays compatible while recovery requires 1.8.1 capability',async()=>{
  const old=fixture({loaded:'old'});await old.monitor.mounted()
  assert.equal(old.monitor.approvalCapable,true)
  assert.equal(old.monitor.recoveryCapable,false)
  const current=fixture();await current.monitor.mounted()
  assert.equal(current.monitor.approvalCapable,true)
  assert.equal(current.monitor.recoveryCapable,true)
})

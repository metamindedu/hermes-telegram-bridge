import test from 'node:test'
import assert from 'node:assert/strict'
import {registerHooks} from 'node:module'
registerHooks({resolve(specifier, context, nextResolve) {
  if (specifier === '@hermes/plugin-sdk') return {url:'data:text/javascript,export const host={}',shortCircuit:true}
  if (specifier === 'react/jsx-runtime') return {url:'data:text/javascript,export const jsx=(type,props)=>({type,props})',shortCircuit:true}
  return nextResolve(specifier,context)
}})
const {CompletionMonitor,NativeHelperError,APPROVAL_CAPABILITY,APPROVAL_COMPAT_VERSION}=await import('./desktop/plugin.js')

function fixture() {
  const route={mode:'local',connectionId:'fixture-connection',profile:'claire',targetProfile:'claire'}
  const row={id:'fixture-runtime',session_key:'fixture-stored-session',status:'waiting'}
  const asks=['a','b'].map(char=>({id:'srq-'+char.repeat(12),method:'approval',params:{
    session_id:row.id,request_id:char.repeat(32),command:'synthetic operation '+char,
    description:'synthetic protected edit '+char,pattern_key:'protected_instruction_file',
    pattern_keys:['protected_instruction_file'],allow_session:false,allow_permanent:false,choices:['once','deny']}}))
  let open=asks
  let active=[row]
  const queue=asks.map(ask=>Object.fromEntries(Object.entries(ask.params).filter(([key])=>!['session_id','choices'].includes(key))))
  const calls=[]
  const monitor=new CompletionMonitor({state:{profile:{get:()=>route.profile}},profileRoutes:async()=>[route],
    requestProfile:async(_route,method)=>{
      if(method==='session.events.since')return {epoch:'fixture-epoch',open_requests:open}
      if(method==='approval.pending')return {approvals:queue.filter(q=>open.some(a=>a.params.request_id===q.request_id))}
      if(method==='session.active_list')return {sessions:active}
      throw new Error('unexpected RPC '+method)
    },notify:()=>{}},{setTimeout:()=>()=>{}})
  monitor.approvalCapable=true
  monitor.helper=async req=>{
    calls.push(req)
    if(req.action==='approval_owner_heartbeat')return {ok:true,feature_ready:true,version:APPROVAL_COMPAT_VERSION,capability:APPROVAL_CAPABILITY}
    if(req.action==='approval_register')return {ok:true,id:req.request.params.request_id,status:'open'}
    if(req.action==='approval_poll')return {ok:true,items:[],terminal:req.absent}
    throw new Error('unexpected helper '+req.action)
  }
  const record={key:'parent',sid:row.id,profile:route.profile,connectionId:route.connectionId,seq:0,ask:null,askChecking:false}
  monitor.records.set(record.key,record)
  return {monitor,record,route,row,asks,calls,setOpen:value=>{open=value},setActive:value=>{active=value}}
}

for (const mode of ['lost receipt','structured failure','post-publication store failure','malformed success','unbound duplicate']) test('approval registration bookkeeping recovers after '+mode+' without a second send',async()=>{
  const f=fixture(),helper=f.monitor.helper
  f.setOpen([f.asks[0]])
  let writes=0,reads=0
  f.monitor.helper=async req=>{
    if(req.action==='approval_register'){
      writes++
      f.calls.push(req)
      if(mode==='lost receipt')throw new NativeHelperError('lost receipt',true)
      if(mode==='post-publication store failure')return {ok:false,reason:'approval_store_unavailable'}
      if(mode==='unbound duplicate')return {ok:true,duplicate:true,id:'1'.repeat(32),status:'open'}
      return mode==='structured failure'?{ok:false,reason:'RuntimeError'}:{ok:true}
    }
    if(req.action==='approval_registration_lookup'){
      reads++
      return {ok:true,found:true,id:'1'.repeat(32),status:'open',expires:Date.now()/1000+300}
    }
    return helper(req)
  }
  await f.monitor.checkAsk(f.record,f.route,f.row)
  assert.equal(f.monitor.approvalRecords().filter(r=>r.approval).length,0)
  await f.monitor.pumpApprovals()
  assert.equal(f.monitor.approvalRecords().filter(r=>r.approval).length,1)
  assert.equal(writes,1)
  assert.equal(reads,1)
  assert.deepEqual(f.calls.find(c=>c.action==='approval_poll').observations.map(x=>x.id),['1'.repeat(32)])
  await f.monitor.checkAsk(f.record,f.route,f.row)
  assert.equal(writes,1)
})

for(const mode of ['read failure','changed request','terminal','malformed','disposed']) test('approval registration recovery stays fenced on '+mode,async()=>{
  const f=fixture(),helper=f.monitor.helper
  f.setOpen([f.asks[0]])
  let writes=0,reads=0
  f.monitor.helper=async req=>{
    if(req.action==='approval_register'){writes++;throw new NativeHelperError('lost receipt',true)}
    if(req.action==='approval_registration_lookup'){
      reads++
      if(mode==='read failure'&&reads===1)throw Error('read gap')
      if(mode==='disposed')f.monitor.dispose()
      if(mode==='malformed')return {ok:true,found:true,id:'bad'}
      return {ok:true,found:true,id:'1'.repeat(32),status:mode==='terminal'?'expired':'open',expires:Date.now()/1000+300}
    }
    return helper(req)
  }
  await f.monitor.checkAsk(f.record,f.route,f.row)
  if(mode==='changed request')f.setOpen([])
  await f.monitor.pumpApprovals()
  assert.equal(f.monitor.approvalRecords().filter(r=>r.approval).length,0)
  assert.equal(writes,1)
  if(mode==='read failure'){
    await f.monitor.pumpApprovals()
    assert.equal(f.monitor.approvalRecords().filter(r=>r.approval).length,1)
    assert.equal(writes,1)
    assert.equal(reads,2)
  }
  if(mode==='changed request')assert.deepEqual(f.calls.find(c=>c.action==='approval_poll').absent,['1'.repeat(32)])
})

for(const mode of ['deadline','receiver unavailable','native absence','parent absence','parent no longer waiting']) test('unrecoverable approval registration payload is bounded on '+mode,async()=>{
  const f=fixture(),helper=f.monitor.helper
  f.setOpen([f.asks[0]])
  let clock=1000,writes=0,reads=0
  f.monitor.now=()=>clock
  f.monitor.helper=async req=>{
    if(req.action==='approval_register'){writes++;throw new NativeHelperError('lost receipt',true)}
    if(req.action==='approval_registration_lookup'){reads++;return {ok:true,found:false}}
    return helper(req)
  }
  await f.monitor.checkAsk(f.record,f.route,f.row)
  const slot=f.record.approvalSlots.get(f.asks[0].id)
  await f.monitor.pumpApprovals()
  assert.ok(slot.approvalRegistration,'a transient miss alone is not native absence')
  if(mode==='deadline'||mode==='receiver unavailable'){
    clock+=120_001
    if(mode==='receiver unavailable'){
      const current=f.monitor.helper
      f.monitor.helper=async req=>req.action==='approval_owner_heartbeat'?{ok:false,feature_ready:false}:current(req)
    }
    await f.monitor.pumpApprovals()
  }else if(mode==='native absence'){
    f.setOpen([]);await f.monitor.checkAsk(f.record,f.route,f.row)
  }else{
    if(mode==='parent absence')f.setActive([])
    else f.row.status='idle'
    await f.monitor.sweep()
  }
  assert.equal(slot.approvalRegistration,null)
  const oldReads=reads
  await f.monitor.pumpApprovals()
  assert.equal(reads,oldReads,'terminal bookkeeping must not keep querying')
  if(mode==='deadline'){
    f.setOpen([f.asks[0]])
    await f.monitor.checkAsk(f.record,f.route,f.row)
    assert.equal(slot.helperWriteBlockedAskId,f.asks[0].id)
  }
  if(mode==='parent absence')assert.equal(f.monitor.records.size,0)
  assert.equal(writes,1,'cleanup must not permit a second registration write')
})

test('parallel native approvals each register before either request is settled',async()=>{
  const f=fixture()
  await f.monitor.checkAsk(f.record,f.route,f.row)
  assert.deepEqual(f.calls.filter(c=>c.action==='approval_register').map(c=>c.request.id),f.asks.map(a=>a.id))
  await f.monitor.checkAsk(f.record,f.route,f.row)
  assert.equal(f.calls.filter(c=>c.action==='approval_register').length,2,'same snapshot must not post twice')
  await f.monitor.pumpApprovals()
  const poll=f.calls.find(c=>c.action==='approval_poll')
  assert.deepEqual(poll.observations.map(o=>o.id).sort(),f.asks.map(a=>a.params.request_id).sort())
})

test('settling one parallel approval retires only its own card and keeps the other observed',async()=>{
  const f=fixture()
  await f.monitor.checkAsk(f.record,f.route,f.row)
  f.setOpen([f.asks[1]])
  await f.monitor.checkAsk(f.record,f.route,f.row)
  await f.monitor.pumpApprovals()
  const poll=f.calls.find(c=>c.action==='approval_poll')
  assert.deepEqual(poll.absent,[f.asks[0].params.request_id])
  assert.deepEqual(poll.observations.map(o=>o.id),[f.asks[1].params.request_id])
  assert.equal(f.calls.filter(c=>c.action==='approval_register').length,2)
})

for (const [label,error] of [
  ['pre-write read',new Error('synthetic read gap')],
  ['permanent local size',new NativeHelperError('synthetic size boundary',false,true)],
  ['unknown write',new NativeHelperError('synthetic uncertain write',true)],
]) test(`a ${label} failure on one request cannot suppress its sibling`,async()=>{
  const f=fixture(),helper=f.monitor.helper
  let attempts=0
  f.monitor.helper=async req=>{
    if(req.action==='approval_register' && req.request.id===f.asks[0].id){attempts++;throw error}
    return helper(req)
  }
  await f.monitor.checkAsk(f.record,f.route,f.row)
  assert.deepEqual(f.calls.filter(c=>c.action==='approval_register').map(c=>c.request.id),[f.asks[1].id])
  assert.equal(f.monitor.approvalRecords().filter(r=>r.approval).length,1)
  await f.monitor.checkAsk(f.record,f.route,f.row)
  assert.equal(attempts,error instanceof NativeHelperError ? 1 : 2)
  assert.equal(f.calls.filter(c=>c.action==='approval_register').length,1,'never replay the sibling')
})

test('receiver outage recovers both requests without sending duplicate cards',async()=>{
  const f=fixture(),helper=f.monitor.helper
  let ready=false
  f.monitor.helper=async req=>{
    if(req.action==='approval_owner_heartbeat'&&!ready)return {ok:false,feature_ready:false}
    if(req.action==='approval_notice')return {ok:true,notification_only:true}
    return helper(req)
  }
  await f.monitor.checkAsk(f.record,f.route,f.row)
  assert.equal(f.calls.filter(c=>c.action==='approval_register').length,0)
  ready=true
  await f.monitor.checkAsk(f.record,f.route,f.row)
  assert.deepEqual(f.calls.filter(c=>c.action==='approval_register').map(c=>c.request.id),f.asks.map(a=>a.id))
})

test('parallel approvals are not hidden by an earlier non-approval human request',async()=>{
  const f=fixture()
  f.setOpen([{id:'srq-cccccccccccc',method:'sudo',params:{session_id:f.row.id}},...f.asks])
  const helper=f.monitor.helper
  f.monitor.helper=async req=>req.action==='attention'?{ok:true}:helper(req)
  await f.monitor.checkAsk(f.record,f.route,f.row)
  assert.equal(f.calls.filter(c=>c.action==='approval_register').length,2)
})

test('duplicate native request IDs fail closed before any registration',async()=>{
  const f=fixture()
  f.setOpen([f.asks[0],f.asks[0]])
  await f.monitor.checkAsk(f.record,f.route,f.row)
  assert.deepEqual(f.calls,[])
})

const clarify=(f)=>({id:'srq-cccccccccccc',method:'clarify',params:{session_id:f.row.id,question:'synthetic question'}})
for(const cached of [false,true]) test(`clarify ${cached?'cached':'new'} reply cannot erase an approval permanent fence`,async()=>{
  const f=fixture(),q=clarify(f),helper=f.monitor.helper
  let attempts=0,replies=0
  f.setOpen([q,...f.asks])
  f.monitor.helper=async req=>{
    if(req.action==='approval_register'&&req.request.id===f.asks[0].id){attempts++;throw new NativeHelperError('synthetic size',false,true)}
    if(req.action==='reply_register'){replies++;return {ok:true,new:true,id:'question-card'}}
    return helper(req)
  }
  if(cached){
    f.record.reply={id:'question-card',descriptor:{owner:f.monitor.replyOwner,connectionId:f.route.connectionId,
      profile:f.route.profile,targetProfile:f.route.targetProfile,sid:f.row.id,session_key:f.row.session_key,
      epoch:'fixture-epoch',request_id:q.id,fingerprint:JSON.stringify({id:q.id,method:q.method,params:{question:q.params.question,session_id:q.params.session_id}})}}
  }
  await f.monitor.checkAsk(f.record,f.route,f.row)
  await f.monitor.checkAsk(f.record,f.route,f.row)
  assert.equal(attempts,1)
  assert.ok(replies<=1)
  assert.equal(f.calls.filter(c=>c.action==='approval_register').length,1)
})

test('ambiguous approval and clarify writes retain independent no-replay fences',async()=>{
  const f=fixture(),q=clarify(f),helper=f.monitor.helper
  let attempts=0,replies=0
  f.setOpen([q,...f.asks])
  f.monitor.helper=async req=>{
    if(req.action==='approval_register'&&req.request.id===f.asks[0].id){attempts++;throw new NativeHelperError('synthetic unknown',true)}
    if(req.action==='reply_register'){replies++;throw new NativeHelperError('synthetic unknown reply',true)}
    return helper(req)
  }
  for(let i=0;i<3;i++)await f.monitor.checkAsk(f.record,f.route,f.row)
  assert.equal(attempts,1)
  assert.equal(replies,1)
  assert.equal(f.calls.filter(c=>c.action==='approval_register').length,1)
})

for(const permanent of [false,true]) test(`new request order preserves ${permanent?'permanent':'unknown'} approval fence`,async()=>{
  const f=fixture(),helper=f.monitor.helper
  let attempts=0
  f.monitor.helper=async req=>{
    if(req.action==='approval_register'&&req.request.id===f.asks[0].id){attempts++;throw new NativeHelperError('synthetic fence',!permanent,permanent)}
    return helper(req)
  }
  await f.monitor.checkAsk(f.record,f.route,f.row)
  const c=structuredClone(f.asks[0]);c.id='srq-cccccccccccc';c.params.request_id='c'.repeat(32);c.params.command='synthetic operation c'
  f.setOpen([c,...f.asks])
  await f.monitor.checkAsk(f.record,f.route,f.row)
  assert.equal(attempts,1)
  assert.deepEqual(f.calls.filter(c=>c.action==='approval_register').map(c=>c.request.id).sort(),[f.asks[1].id,c.id].sort())
})

for(const ambiguous of [false,true]) test(`earlier sudo notice is handled once alongside approvals (${ambiguous?'unknown':'sent'})`,async()=>{
  const f=fixture(),helper=f.monitor.helper
  let notices=0
  f.setOpen([{id:'srq-cccccccccccc',method:'sudo',params:{session_id:f.row.id}},...f.asks])
  f.monitor.helper=async req=>{
    if(req.action==='attention'){notices++;if(ambiguous)throw new NativeHelperError('synthetic unknown notice',true);return {ok:true}}
    return helper(req)
  }
  for(let i=0;i<3;i++)await f.monitor.checkAsk(f.record,f.route,f.row)
  assert.equal(notices,1)
  assert.equal(f.calls.filter(c=>c.action==='approval_register').length,2)
})

test('a failed route observation holds every sibling instead of expiring it',async()=>{
  const f=fixture()
  await f.monitor.checkAsk(f.record,f.route,f.row)
  f.monitor.api.requestProfile=async()=>{throw new Error('synthetic offline route')}
  await f.monitor.sweep()
  const poll=f.calls.find(c=>c.action==='approval_poll')
  assert.deepEqual(poll.unverified.sort(),f.asks.map(a=>a.params.request_id).sort())
  assert.deepEqual(poll.absent,[])
  assert.equal(f.monitor.hasApprovals(f.record),true)
})

test('working or absent parent retires all sibling cards before pruning the parent',async()=>{
  for(const state of ['working','absent']){
    const f=fixture()
    await f.monitor.checkAsk(f.record,f.route,f.row)
    const read=f.monitor.api.requestProfile
    f.monitor.api.requestProfile=async(route,method)=>method==='session.active_list'
      ? {sessions:state==='absent'?[]:[{...f.row,status:state}]} : read(route,method)
    await f.monitor.sweep()
    const poll=f.calls.find(c=>c.action==='approval_poll')
    assert.deepEqual(poll.absent.sort(),f.asks.map(a=>a.params.request_id).sort())
    assert.equal(f.monitor.hasApprovals(f.record),false)
    assert.equal(f.monitor.records.has(f.record.key),state!=='absent')
  }
})

for(const firstOutcome of ['applied','expired','unknown']) test(`two independent slots dispatch once each after first ${firstOutcome}`,async()=>{
  const f=fixture(),helper=f.monitor.helper,read=f.monitor.api.requestProfile
  const writes=[],settled=[]
  await f.monitor.checkAsk(f.record,f.route,f.row)
  f.monitor.api.requestProfile=async(route,method,args)=>{
    if(method==='approval.respond'){
      writes.push(args)
      if(writes.length===1&&firstOutcome==='unknown')throw new Error('synthetic response loss')
      return {resolved:writes.length===1&&firstOutcome==='expired'?0:1}
    }
    return read(route,method,args)
  }
  f.monitor.helper=async req=>{
    if(req.action==='approval_poll'){
      const slot=f.monitor.approvalRecords().find(s=>s.approval)
      if(!slot)return {ok:true,items:[],terminal:[]}
      const b=slot.approval
      return {ok:true,items:[{id:b.id,claim:'f'.repeat(32),deadline:Date.now()/1000+30,
        descriptor:b.descriptor,request:b.request,pending:b.pending,choice:writes.length?'deny':'once'}],terminal:[]}
    }
    if(req.action==='approval_claim_valid')return {ok:true,valid:true,id:req.id,claim:req.claim,authority:req.authority,deadline:req.deadline}
    if(req.action==='approval_settle'){settled.push(req.status);return {ok:true}}
    return helper(req)
  }
  await f.monitor.pumpApprovals()
  await f.monitor.pumpApprovals()
  await f.monitor.pumpApprovals()
  assert.deepEqual(writes,f.asks.map((a,index)=>({session_id:f.row.id,request_id:a.params.request_id,choice:index?'deny':'once',all:false})))
  assert.deepEqual(settled,[firstOutcome,'applied'])
  assert.equal(f.monitor.hasApprovals(f.record),false)
})

for(const transportFailure of ['timeout','bad-frame']) for(const failedHeartbeats of [1,2])
test(`real helper ${transportFailure} on ${failedHeartbeats} admission heartbeat(s) recovers without card-write fences`,async()=>{
  const f=fixture(),stubHelper=f.monitor.helper,read=f.monitor.api.requestProfile
  const defaultRoute={...f.route,profile:'default',targetProfile:'default'}
  f.monitor.api.profileRoutes=async()=>[f.route,defaultRoute]
  f.monitor.helper=CompletionMonitor.prototype.helper.bind(f.monitor)
  let failures=failedHeartbeats
  f.monitor.api.requestProfile=async(route,method,args)=>{
    if(method!=='cli.exec')return read(route,method,args)
    const req=JSON.parse(Buffer.from(args.argv.at(-1),'base64url').toString('utf8'))
    if(req.action==='approval_owner_heartbeat'&&failures-->0){
      if(transportFailure==='timeout')throw new Error('synthetic CLI timeout')
      return {blocked:false,code:0,output:'synthetic malformed native result frame'}
    }
    const result=await stubHelper(req)
    return {blocked:false,code:0,output:'HERMES_TELEGRAM_BRIDGE_RESULT_V1_BEGIN\n'+Buffer.from(JSON.stringify(result)).toString('base64url')+'\nHERMES_TELEGRAM_BRIDGE_RESULT_V1_END\n'}
  }
  await f.monitor.checkAsk(f.record,f.route,f.row)
  assert.equal(f.calls.filter(c=>c.action==='approval_register').length,2-failedHeartbeats)
  for(let i=0;i<3;i++)await f.monitor.checkAsk(f.record,f.route,f.row)
  assert.deepEqual(f.calls.filter(c=>c.action==='approval_register').map(c=>c.request.id).sort(),f.asks.map(a=>a.id).sort())
  assert.equal(f.monitor.approvalRecords().some(r=>r.helperWriteBlockedAskId),false)
})

test('heartbeat exception with an existing clarify reply preserves the unrelated episode',async()=>{
  const f=fixture(),q=clarify(f),helper=f.monitor.helper
  f.setOpen([q,...f.asks]);f.record.ask={id:q.id,method:'clarify'}
  f.record.reply={id:'question-card',descriptor:{owner:f.monitor.replyOwner,connectionId:f.route.connectionId,
    profile:f.route.profile,targetProfile:f.route.targetProfile,sid:f.row.id,session_key:f.row.session_key,
    epoch:'fixture-epoch',request_id:q.id,fingerprint:JSON.stringify({id:q.id,method:q.method,params:{question:q.params.question,session_id:q.params.session_id}})}}
  let fail=true
  f.monitor.helper=async req=>{
    if(req.action==='approval_owner_heartbeat'&&fail)throw new NativeHelperError('synthetic lost heartbeat',true)
    return helper(req)
  }
  await f.monitor.checkAsk(f.record,f.route,f.row)
  assert.deepEqual(f.record.ask,{id:q.id,method:'clarify'})
  assert.equal(f.record.helperWriteBlockedAskId,undefined)
  fail=false
  await f.monitor.checkAsk(f.record,f.route,f.row)
  assert.equal(f.calls.filter(c=>c.action==='approval_register').length,2)
  assert.deepEqual(f.record.ask,{id:q.id,method:'clarify'})
})

for(const remaining of [0,1]) test(`remaining slot ${remaining} survives a deferred terminal receipt and is later cleaned`,async()=>{
  const f=fixture(),helper=f.monitor.helper,read=f.monitor.api.requestProfile
  await f.monitor.checkAsk(f.record,f.route,f.row)
  f.setOpen([f.asks[remaining]])
  await f.monitor.checkAsk(f.record,f.route,f.row)
  await f.monitor.pumpApprovals()
  f.monitor.approvalSweepAbsent.clear()
  assert.equal(f.monitor.approvalRecords().filter(s=>s.approval).length,1)
  f.monitor.api.requestProfile=async(route,method)=>method==='session.active_list'?{sessions:[]}:read(route,method)
  let defer=true
  f.monitor.helper=async req=>req.action==='approval_poll'&&defer?{ok:true,items:[],terminal:[]}:helper(req)
  await f.monitor.sweep()
  assert.equal(f.monitor.records.has(f.record.key),true,'missing terminal ACK cannot discard the remaining slot')
  defer=false
  await f.monitor.sweep()
  assert.equal(f.monitor.records.has(f.record.key),false)
})

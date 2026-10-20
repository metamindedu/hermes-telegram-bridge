import test from 'node:test'
import assert from 'node:assert/strict'
import {registerHooks} from 'node:module'
registerHooks({resolve(s,c,n){
  if(s==='@hermes/plugin-sdk')return{url:'data:text/javascript,export const host={}',shortCircuit:true}
  if(s==='react/jsx-runtime')return{url:'data:text/javascript,export const jsx=(type,props,key)=>({type,props,key})',shortCircuit:true}
  return n(s,c)
}})
const {CompletionMonitor,NativeHelperError,stableJSON,validReplySnapshot}=await import('./desktop/plugin.js')
function fixture() {
  const route={mode:'local',connectionId:'local',profile:'default',targetProfile:'default'}
  const row={id:'sid1',session_key:'stored1',status:'waiting'}
  const ask={id:'srq-1122aabbccdd',method:'clarify',params:{session_id:'sid1',questions:[{qid:'q1',question:'Q?',choices:['A','B'],multi_select:false}]}}
  const snapshot={epoch:'epoch1',open_requests:[ask],events:[],truncated:false}
  const writes=[],settles=[],registrations=[],polls=[];let queue=false,registered,registerHandler=null,activeOverride
  const api={state:{profile:{get:()=> 'default'}},profileRoutes:async()=>[route],notify:()=>{},requestProfile:async(r,m,p)=>{
    if(m==='session.active_list'){
      if(typeof activeOverride==='function')return activeOverride()
      if(activeOverride!==undefined)return structuredClone(activeOverride)
      return {sessions:[structuredClone(row)]}
    }
    if(m==='session.events.since')return structuredClone(snapshot)
    if(m==='request.answer'){writes.push({route:r,params:p});snapshot.open_requests=[];row.status='working';return {status:'ok'}}
    throw Error(m)
  }}
  const mon=new CompletionMonitor(api,{setTimeout:()=>()=>{}},()=>1000)
  mon.helper=async r=>{
    if(r.action==='reply_register'){
      registrations.push(r)
      registered=r
      if(registerHandler)return registerHandler(r)
      return {ok:true,id:'form1',new:true}
    }
    if(r.action==='reply_poll'){
      polls.push(structuredClone(r))
      const observed=r.observations?.some(x=>x.id==='form1')&&!r.unverified?.includes('form1')
      return {ok:true,items:queue&&observed?[{id:'form1',claim:'claim1',deadline:Date.now()/1000+40,descriptor:registered.descriptor,result:{answers:{q1:'B'}}}]:[]}
    }
    if(r.action==='reply_claim_valid') return {ok:true,valid:true,id:r.id,claim:r.claim,
      authority:r.authority,deadline:r.deadline,reason:'ready'}
    if(r.action==='reply_settle'){settles.push(r);queue=false;return {ok:true}}
    return {ok:true}
  }
  mon.onEvent({type:'tool.start',session_id:'sid1',profile:'default',connectionId:'local',seq:1,payload:{}})
  const record=[...mon.records.values()][0]
  return {route,row,ask,snapshot,api,mon,record,writes,settles,registrations,polls,
    queue:()=>{queue=true},setRegisterHandler:fn=>{registerHandler=fn},setActive:value=>{activeOverride=value},
    register:()=>mon.checkAsk(record,route,row)}
}
for(const mode of ['off','wrong claim','expired after gate','disposed after gate','malformed receipt','read failure']) test('last reply gate denies '+mode+' without response write',async()=>{
  const f=fixture();await f.register();f.queue();const original=f.mon.helper, originalNow=Date.now
  f.mon.helper=async r=>{
    const result=await original(r)
    if(r.action==='reply_claim_valid'){
      if(mode==='off')return {...result,valid:false,reason:'service_disabled'}
      if(mode==='wrong claim')return {...result,claim:'foreign'}
      if(mode==='expired after gate')Date.now=()=>originalNow()+90_000
      if(mode==='disposed after gate')f.mon.dispose()
      if(mode==='malformed receipt')return {ok:true,valid:true}
      if(mode==='read failure')throw Error('readonly timeout')
    }
    return result
  }
  try { await f.mon.pumpReplies() } finally { Date.now=originalNow }
  assert.equal(f.writes.length,0)
  assert.notEqual(f.settles[0]?.status,'applied')
})

test('registers clarify and applies exact all-question response on captured route',async()=>{
  const f=fixture();await f.register();f.queue();await f.mon.pumpReplies()
  assert.equal(f.writes.length,1);assert.deepEqual(f.writes[0].params,{id:f.ask.id,result:{answers:{q1:'B'}}});assert.equal(f.settles[0].status,'applied')
})
for (const mode of ['multi-select canonical result','existing locked answer']) test(`preserves supported ${mode} beyond the free-text intake limit`, async()=>{
  const f=fixture()
  let answer
  if(mode==='multi-select canonical result'){
    const choices=['가','나','다','라'].map(char=>char.repeat(500))
    f.ask.params.questions[0].choices=choices
    f.ask.params.questions[0].multi_select=true
    answer=JSON.stringify(choices)
  }else{
    answer='기존'.repeat(1200)
    f.ask.params.answers={q1:answer}
    f.ask.params.questions.push({qid:'q2',question:'Remaining question?',choices:['A','B'],multi_select:false})
  }
  const expected={answers:{q1:answer,...(mode==='existing locked answer'?{q2:'B'}:{})}}
  assert.ok(Array.from(answer).length>2000)
  await f.register();f.queue()
  const original=f.mon.helper
  f.mon.helper=async request=>{
    const result=await original(request)
    if(request.action==='reply_poll') for(const item of result.items) item.result=expected
    return result
  }
  await f.mon.pumpReplies()
  assert.equal(f.writes.length,1)
  assert.deepEqual(f.writes[0].params.result,expected)
  assert.equal(f.settles[0].status,'applied')
})
for(const mutation of ['epoch','method','sid','stored','route','locks','choices','closed'])test('fails closed on changed '+mutation,async()=>{
  const f=fixture();await f.register();f.queue()
  if(mutation==='epoch')f.snapshot.epoch='new'
  if(mutation==='method')f.ask.method='approval'
  if(mutation==='sid')f.ask.params.session_id='elsewhere'
  if(mutation==='stored')f.row.session_key='newstored'
  if(mutation==='route')f.route.connectionId='remote'
  if(mutation==='locks')f.ask.params.answers={q1:'A'}
  if(mutation==='choices')f.ask.params.questions[0].choices=['C','D']
  if(mutation==='closed')f.snapshot.open_requests=[]
  await f.mon.pumpReplies();assert.equal(f.writes.length,0)
  if(mutation!=='route')assert.deepEqual(f.polls.at(-1).absent,['form1'])
})
test('timeout after write is unknown, not successful or retryable',async()=>{
  const f=fixture();await f.register();f.queue();const orig=f.api.requestProfile
  f.api.requestProfile=async(...args)=>{if(args[1]==='request.answer')throw Error('timeout');return orig(...args)}
  await f.mon.pumpReplies();assert.equal(f.settles[0].status,'unknown');await f.mon.pumpReplies();assert.equal(f.settles.length,1)
})
test('request expired receipt remains expired',async()=>{
  const f=fixture();await f.register();f.queue();const orig=f.api.requestProfile
  f.api.requestProfile=async(...args)=>args[1]==='request.answer'?{status:'expired'}:orig(...args)
  await f.mon.pumpReplies();assert.equal(f.settles[0].status,'expired')
})
test('dispose prevents response writes',async()=>{
  const f=fixture();await f.register();f.queue();f.mon.dispose();await f.mon.pumpReplies();assert.equal(f.writes.length,0)
})
test('approval requests remain notification-only',async()=>{
  const f=fixture();f.ask.method='approval';await f.register();assert.equal(f.record.reply,undefined)
})
test('expired claim never starts a write after slow snapshot',async()=>{
  const f=fixture();await f.register();f.queue();const original=f.mon.helper
  f.mon.helper=async r=>{const out=await original(r);if(r.action==='reply_poll')for(const i of out.items)i.deadline=Date.now()/1000-1;return out}
  await f.mon.pumpReplies();assert.equal(f.writes.length,0);assert.equal(f.settles[0].status,'expired')
})
test('assembles a bounded UTF-8 fragment, checks digest and identity, then answers once',async()=>{
  const f=fixture()
  f.ask.params.questions=Array.from({length:5},(_,i)=>({qid:`q${i+1}`,question:`Question ${i+1}?`,choices:[],multi_select:false}))
  await f.register()
  const result={answers:Object.fromEntries(f.ask.params.questions.map(q=>[q.qid,'😊'.repeat(2000)]))}
  const payload=Buffer.from(stableJSON({descriptor:f.record.reply.descriptor,result}),'utf8')
  const digestBytes=new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256',payload))
  const sha256=Array.from(digestBytes,b=>b.toString(16).padStart(2,'0')).join('')
  const chunkBytes=16*1024,chunks=Math.ceil(payload.byteLength/chunkBytes)
  const meta={sha256,bytes:payload.byteLength,chunks,chunk_bytes:chunkBytes}
  const claim='fragment-claim';let offered=true;const requests=[]
  f.mon.helper=async request=>{
    requests.push(structuredClone(request))
    if(request.action==='reply_poll'){
      if(!offered)return {ok:true,items:[]}
      offered=false
      return {ok:true,items:[{id:'form1',claim,deadline:Date.now()/1000+40,fragment:meta}]}
    }
    if(request.action==='reply_fragment'){
      const start=request.index*chunkBytes
      return {ok:true,id:'form1',claim,sha256,index:request.index,bytes:payload.byteLength,chunks,chunk_bytes:chunkBytes,
        data:payload.subarray(start,start+chunkBytes).toString('base64url')}
    }
    if(request.action==='reply_claim_valid')return {ok:true,valid:true,id:request.id,claim:request.claim,
      authority:request.authority,deadline:request.deadline,reason:'ready'}
    if(request.action==='reply_settle'){f.settles.push(request);return {ok:true}}
    return {ok:true}
  }
  await f.mon.pumpReplies()
  assert.equal(f.writes.length,1)
  assert.deepEqual(f.writes[0].params,{id:f.ask.id,result})
  assert.equal(f.settles[0].status,'applied')
  const fragments=requests.filter(request=>request.action==='reply_fragment')
  assert.equal(fragments.length,chunks)
  assert.ok(fragments.every(request=>request.owner===f.mon.replyOwner&&request.id==='form1'&&request.claim===claim))
  assert.deepEqual(fragments.map(request=>request.index),Array.from({length:chunks},(_,i)=>i))
  await f.mon.pumpReplies()
  assert.equal(f.writes.length,1)
  assert.equal(f.settles.length,1)
})

for(const failure of ['missing','corrupt','expired','malformed shape'])test(`${failure} fragment is terminal without request.answer`,async()=>{
  const f=fixture();await f.register()
  const valid={descriptor:f.record.reply.descriptor,result:{answers:{q1:'응답'}}}
  const frozen=Buffer.from(stableJSON(failure==='malformed shape'?{...valid,extra:true}:valid),'utf8')
  const digestBytes=new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256',frozen))
  const sha256=Array.from(digestBytes,b=>b.toString(16).padStart(2,'0')).join('')
  const meta={sha256,bytes:frozen.byteLength,chunks:1,chunk_bytes:16*1024}
  let settled
  f.mon.helper=async request=>{
    if(request.action==='reply_poll')return {ok:true,items:[{id:'form1',claim:'claim-'+failure,
      deadline:failure==='expired'?Date.now()/1000-1:Date.now()/1000+40,fragment:meta}]}
    if(request.action==='reply_fragment'){
      if(failure==='missing')return {ok:false,reason:'fragment_unavailable'}
      if(failure==='corrupt'){
        const altered=Buffer.from(frozen);altered[0]^=1
        return {ok:true,id:'form1',claim:'claim-'+failure,sha256,index:0,bytes:frozen.byteLength,chunks:1,
          chunk_bytes:16*1024,data:altered.toString('base64url')}
      }
      return {ok:true,id:'form1',claim:'claim-'+failure,sha256,index:0,bytes:frozen.byteLength,chunks:1,
        chunk_bytes:16*1024,data:frozen.toString('base64url')}
    }
    if(request.action==='reply_settle'){settled=request.status;f.settles.push(request);return {ok:true}}
    return {ok:true}
  }
  await f.mon.pumpReplies()
  assert.equal(f.writes.length,0)
  assert.equal(settled,failure==='expired'?'expired':'unknown')
})
test('poll uses compact identities rather than aggregate question bodies',async()=>{
  const f=fixture();f.ask.params.questions[0].question='가'.repeat(2000);await f.register()
  const original=f.mon.helper;let request
  f.mon.helper=async r=>{if(r.action==='reply_poll')request=r;return original(r)}
  await f.mon.pumpReplies();assert.deepEqual(request.observations,[{id:'form1'}]);assert.deepEqual(request.unverified,[]);assert.ok(JSON.stringify(request).length<200)
})
test('transient reply-snapshot failure holds the card as unverified and delivers after recovery',async()=>{
  const f=fixture();await f.register();f.queue()
  const original=f.mon.helper;let request
  f.mon.helper=async r=>{
    const out=await original(r)
    if(r.action==='reply_poll'){request=r;out.items=out.items.filter(i=>r.observations.some(o=>o.id===i.id))}
    return out
  }
  const healthy=f.api.requestProfile
  f.api.requestProfile=async()=>{throw Error('profile rpc down')}
  await f.mon.pumpReplies()
  assert.deepEqual(request.unverified,['form1']);assert.deepEqual(request.observations,[])
  assert.equal(f.writes.length,0);assert.equal(f.settles.length,0) // gap blocks, never expires or consumes
  f.api.requestProfile=healthy
  await f.mon.pumpReplies()
  assert.deepEqual(request.unverified,[]);assert.deepEqual(request.observations,[{id:'form1'}])
  assert.equal(f.writes.length,1);assert.equal(f.settles[0].status,'applied') // same owner renewed the surviving card
})
for(const [shape,value] of [['empty object',{}],['null sessions',{sessions:null}],
  ['missing row status',{sessions:[{id:'sid1',session_key:'stored1'}]}]]) test('sweep preserves and marks active-list '+shape+' unverified, then recovers',async()=>{
  const f=fixture();await f.register();f.queue();f.setActive(value)
  await f.mon.sweep()
  assert.equal(f.record.reply.id,'form1')
  assert.deepEqual(f.polls.at(-1).observations,[])
  assert.deepEqual(f.polls.at(-1).unverified,['form1'])
  assert.equal(f.writes.length,0);assert.equal(f.settles.length,0)
  f.setActive(undefined)
  await f.mon.sweep()
  assert.deepEqual(f.polls.at(-1).observations,[{id:'form1'}])
  assert.deepEqual(f.polls.at(-1).unverified,[])
  assert.equal(f.writes.length,1);assert.equal(f.settles[0].status,'applied')
})
test('sweep active-list transport throw is unverified and same owner recovers',async()=>{
  const f=fixture();await f.register();f.queue()
  f.setActive(()=>{throw Error('active list transport down')})
  await f.mon.sweep()
  assert.equal(f.record.reply.id,'form1')
  assert.deepEqual(f.polls.at(-1).unverified,['form1'])
  assert.equal(f.writes.length,0)
  f.setActive(undefined)
  await f.mon.sweep()
  assert.equal(f.writes.length,1);assert.equal(f.settles[0].status,'applied')
})
for(const shape of ['missing open_requests','missing epoch']) test('checkAsk and replySnapshot hold '+shape+' as transport-unverified',async()=>{
  const f=fixture();await f.register();f.queue()
  const saved=structuredClone(f.snapshot)
  if(shape==='missing open_requests')f.snapshot.open_requests=null
  else delete f.snapshot.epoch
  await f.mon.sweep()
  assert.equal(f.record.reply.id,'form1')
  assert.deepEqual(f.polls.at(-1).observations,[])
  assert.deepEqual(f.polls.at(-1).unverified,['form1'])
  assert.equal(f.writes.length,0);assert.equal(f.settles.length,0)
  Object.assign(f.snapshot,saved)
  await f.mon.sweep()
  assert.equal(f.writes.length,1);assert.equal(f.settles[0].status,'applied')
})
for(const [shape,mutate] of [
  ['missing params',ask=>{delete ask.params}],
  ['null params',ask=>{ask.params=null}],
  ['missing session_id',ask=>{delete ask.params.session_id}],
  ['null session_id',ask=>{ask.params.session_id=null}],
  ['numeric session_id',ask=>{ask.params.session_id=123}],
  ['missing questions',ask=>{delete ask.params.questions}],
  ['null question row',ask=>{ask.params.questions=[null]}],
  ['missing question text',ask=>{delete ask.params.questions[0].question}],
  ['missing batch qid',ask=>{delete ask.params.questions[0].qid}],
  ['non-string choice',ask=>{ask.params.questions[0].choices=[null]}],
  ['malformed answers',ask=>{ask.params.answers={q1:null}}]
]) test('sweep holds incomplete clarify '+shape+' and recovers the same bridge',async()=>{
  const f=fixture();await f.register();f.queue()
  const originalParams=structuredClone(f.ask.params)
  mutate(f.ask)
  await f.mon.sweep()
  assert.equal(f.record.reply?.id,'form1')
  assert.deepEqual(f.polls.at(-1).observations,[])
  assert.deepEqual(f.polls.at(-1).unverified,['form1'])
  assert.equal(f.writes.length,0);assert.equal(f.settles.length,0)
  assert.equal(f.registrations.length,1)
  f.ask.params=originalParams
  await f.mon.sweep()
  assert.deepEqual(f.polls.at(-1).observations,[{id:'form1'}])
  assert.deepEqual(f.polls.at(-1).unverified,[])
  assert.equal(f.writes.length,1);assert.equal(f.settles[0].status,'applied')
  assert.equal(f.registrations.length,1)
})
test('different new request registers after definitive retirement of the old bridge',async()=>{
  const f=fixture();await f.register()
  f.ask.id='srq-new-request'
  f.setRegisterHandler(async request=>{
    assert.equal(request.descriptor.request_id,'srq-new-request')
    return {ok:true,new:true,id:'form2'}
  })
  await f.mon.sweep()
  assert.equal(f.record.reply,null)
  assert.equal(f.registrations.length,1)
  assert.deepEqual(f.polls.at(-1).observations,[])
  assert.deepEqual(f.polls.at(-1).unverified,[])
  await f.mon.sweep()
  assert.equal(f.record.reply.id,'form2')
  assert.equal(f.record.reply.descriptor.request_id,'srq-new-request')
  assert.equal(f.registrations.length,2)
})
test('healthy empty active list is explicit absence, not unverified',async()=>{
  const f=fixture();await f.register();f.queue();f.setActive({sessions:[]})
  await f.mon.sweep()
  assert.deepEqual(f.polls.at(-1).observations,[])
  assert.deepEqual(f.polls.at(-1).unverified,[])
  assert.equal(f.record.reply,null)
  assert.equal(f.mon.records.size,0)
  assert.equal(f.writes.length,0)
})
test('sweep treats changed durable session identity as definitive old-bridge absence',async()=>{
  const f=fixture();await f.register();f.queue();f.row.session_key='different-session-key'
  await f.mon.sweep()
  assert.equal(f.registrations.length,1)
  assert.deepEqual(f.polls.at(-1).observations,[])
  assert.deepEqual(f.polls.at(-1).unverified,[])
  assert.equal(f.record.reply,null)
  assert.equal(f.writes.length,0)
})
for(const mutation of ['question','choices','partial answers']) test('sweep/checkAsk retires immutable same-request '+mutation+' mutation',async()=>{
  const f=fixture();await f.register()
  const old=f.record.reply
  f.setRegisterHandler(async request=>{
    assert.equal(f.record.reply,old,'old bridge stays attached until helper confirms retirement')
    assert.equal(request.descriptor.request_id,old.descriptor.request_id)
    assert.equal(request.descriptor.epoch,old.descriptor.epoch)
    assert.notEqual(request.descriptor.fingerprint,old.descriptor.fingerprint)
    return {ok:false,reason:'immutable_mismatch'}
  })
  if(mutation==='question')f.ask.params.questions[0].question='changed question'
  if(mutation==='choices')f.ask.params.questions[0].choices=['C','D']
  if(mutation==='partial answers')f.ask.params.answers={q1:'local draft'}
  await f.mon.sweep()
  assert.equal(f.registrations.length,2)
  assert.equal(f.record.reply,null)
  assert.deepEqual(f.polls.at(-1).observations,[])
  assert.deepEqual(f.polls.at(-1).unverified,[])
  assert.equal(f.writes.length,0)
})
for(const prior of [{ok:true,duplicate:true,id:'prior-form',status:'open'},
  {ok:true,duplicate:true,id:'terminal-form',status:'expired'},
  {ok:false,reason:'prior_owner'}]) test('does not attach duplicate or prior-owner bridge data',async()=>{
  const f=fixture();f.setRegisterHandler(async()=>prior)
  await f.register()
  assert.equal(f.record.reply,undefined)
})
for(const mode of ['lost receipt','structured failure','malformed success','same-owner duplicate']) test('read-only recovery binds an existing registration after '+mode+' without resending',async()=>{
  const f=fixture();let lookups=0
  f.setRegisterHandler(async()=>{
    if(mode==='lost receipt')throw new NativeHelperError('lost registration receipt',true)
    if(mode==='structured failure')return {ok:false,reason:'RuntimeError'}
    if(mode==='malformed success')return {ok:true}
    return {ok:true,duplicate:true,id:'form1',status:'open'}
  })
  const original=f.mon.helper
  f.mon.helper=async request=>{
    if(request.action==='reply_registration_lookup'){
      lookups++
      assert.deepEqual(request.descriptor,f.registrations[0].descriptor)
      return {ok:true,found:true,id:'form1',status:'open',expires:Date.now()/1000+3600}
    }
    return original(request)
  }
  await f.register()
  assert.equal(f.record.reply,undefined)
  await f.mon.sweep()
  assert.equal(f.record.reply?.id,'form1')
  assert.equal(f.registrations.length,1)
  assert.equal(lookups,1)
  assert.deepEqual(f.polls.at(-1).observations,[{id:'form1'}])
  assert.deepEqual(f.polls.at(-1).absent,[])
  f.queue();await f.mon.sweep()
  assert.equal(f.writes.length,1)
  assert.equal(f.settles[0].status,'applied')
  assert.equal(f.registrations.length,1)
})
test('a failed read-only registration lookup can recover later without another registration',async()=>{
  const f=fixture();let reads=0
  f.setRegisterHandler(async()=>{throw new NativeHelperError('lost receipt',true)})
  const original=f.mon.helper
  f.mon.helper=async request=>{
    if(request.action==='reply_registration_lookup'){
      if(++reads===1)throw Error('temporary read failure')
      return {ok:true,found:true,id:'form1',status:'open',expires:Date.now()/1000+3600}
    }
    return original(request)
  }
  await f.register();await f.mon.pumpReplies()
  assert.equal(f.record.reply,undefined)
  assert.deepEqual(f.polls.at(-1).absent,[])
  await f.mon.pumpReplies()
  assert.equal(f.record.reply?.id,'form1')
  assert.equal(f.registrations.length,1)
  assert.equal(reads,2)
})
for(const mode of ['missing','terminal','malformed','changed request','lookup failure','disposed']) test('registration recovery stays fail-closed on '+mode,async()=>{
  const f=fixture();f.setRegisterHandler(async()=>{throw new NativeHelperError('lost receipt',true)})
  const original=f.mon.helper
  f.mon.helper=async request=>{
    if(request.action==='reply_registration_lookup'){
      if(mode==='lookup failure')throw Error('read-only transport gap')
      if(mode==='missing')return {ok:true,found:false}
      if(mode==='malformed')return {ok:true,found:true,id:'form1'}
      if(mode==='disposed')f.mon.dispose()
      return {ok:true,found:true,id:'form1',status:mode==='terminal'?'expired':'open',expires:Date.now()/1000+3600}
    }
    return original(request)
  }
  await f.register()
  if(mode==='changed request')f.snapshot.open_requests=[]
  await f.mon.pumpReplies()
  assert.equal(f.record.reply,undefined)
  assert.equal(f.writes.length,0)
  assert.equal(f.registrations.length,1)
  if(mode==='changed request')assert.deepEqual(f.polls.at(-1).absent,['form1'])
})
test('canonical serialization preserves arrays and Korean',()=>{
  assert.equal(stableJSON({z:['나','가'],a:1}),'{'+'"a":1,"z":["나","가"]}')
})

import test from 'node:test'
import assert from 'node:assert/strict'
import {registerHooks} from 'node:module'
registerHooks({resolve(s,c,next){
  if(s==='@hermes/plugin-sdk')return {url:'data:text/javascript,export const host={}',shortCircuit:true}
  if(s==='react/jsx-runtime')return {url:'data:text/javascript,export const jsx=()=>({})',shortCircuit:true}
  return next(s,c)
}})
const {CompletionMonitor,NativeHelperError}=await import('./desktop/plugin.js')
const route={mode:'local',connectionId:'local',profile:'claire',targetProfile:'claire'}
function fixture({elapsed=0,fail=false}={}){
  const calls=[]
  const request=async(method,args,outer)=>{
    const payload=JSON.parse(Buffer.from(args.argv[4],'base64url').toString())
    calls.push({method,args,outer,payload})
    if(fail||args.timeout<elapsed)throw new Error('cli.exec: timeout')
    return {blocked:false,code:0,output:'HERMES_TELEGRAM_BRIDGE_RESULT_V1_BEGIN\n'+Buffer.from(JSON.stringify({ok:true})).toString('base64url')+'\nHERMES_TELEGRAM_BRIDGE_RESULT_V1_END\n'}
  }
  const gateway={request}
  const api={state:{gateway:{get:()=> 'open'},profile:{get:()=> 'claire'},connectionId:{get:()=> 'local'}},
    profileRoutes:async()=>[{...route,profile:'default',targetProfile:'default'},route],
    getGateway:()=>gateway,requestProfile:async(_route,...args)=>request(...args)}
  return {monitor:new CompletionMonitor(api,{setTimeout:()=>()=>{}}),calls}
}
test('recovery and handshake admit a simulated 26-second startup within bounded budgets',async()=>{
  const f=fixture({elapsed:26})
  const actions=['loaded','completion_recovery_policy','completion_recovery_record','completion_recovery_get','completion_recovery_classify','completion_recovery_claim','completion_recovery_send']
  for(const action of actions){assert.equal((await f.monitor.helper({action,route})).ok,true)}
  assert.equal(f.calls.length,actions.length)
  for(const call of f.calls){assert.equal(call.args.timeout,60);assert.equal(call.outer,75_000)}
})
test('clarify registration survives a 26-second native helper without orphaning its one-hour card',async()=>{
  const calls=[]
  const named={...route}
  const defaultRoute={...route,profile:'default',targetProfile:'default'}
  const row={id:'runtime-question',session_key:'stored-question',status:'waiting'}
  const ask={id:'srq-aabbcc112233',method:'clarify',params:{session_id:row.id,
    questions:[{qid:'q0',question:'Synthetic question?',choices:['A','B'],multi_select:false}]}}
  let descriptor=null,card=null,sends=0
  const api={state:{profile:{get:()=> 'claire'}},profileRoutes:async()=>[defaultRoute,named],notify:()=>{},
    requestProfile:async(_route,method,args,outer)=>{
      if(method==='session.active_list')return {sessions:[structuredClone(row)]}
      if(method==='session.events.since')return {epoch:'question-epoch',open_requests:[structuredClone(ask)]}
      assert.equal(method,'cli.exec')
      const request=JSON.parse(Buffer.from(args.argv[4],'base64url').toString())
      calls.push({request,timeout:args.timeout,outer})
      let result
      if(request.action==='reply_register'){
        descriptor=request.descriptor
        sends++
        card={id:'new-question-card',status:'open',created:1000,expires:4600}
        // The native child has already confirmed the send, but its response
        // misses the old 25s child deadline. Do not emulate a resend/retry.
        if(args.timeout<26)throw Error('cli.exec: timeout')
        result={ok:true,new:true,id:card.id}
      }else if(request.action==='reply_poll'){
        if(!request.observations.some(item=>item.id===card.id)&&!request.unverified.includes(card.id))card.status='expired'
        result={ok:true,items:[]}
      }else throw Error(request.action)
      return {blocked:false,code:0,output:'HERMES_TELEGRAM_BRIDGE_RESULT_V1_BEGIN\n'+
        Buffer.from(JSON.stringify(result)).toString('base64url')+'\nHERMES_TELEGRAM_BRIDGE_RESULT_V1_END\n'}
    }}
  const monitor=new CompletionMonitor(api,{setTimeout:()=>()=>{}},()=>1000)
  // An earlier question in this window has already enabled owner-wide polling.
  monitor.hadReplies=true
  monitor.onEvent({type:'tool.start',profile:'claire',connectionId:'local',session_id:row.id,seq:1,payload:{}})
  const record=[...monitor.records.values()][0]
  await monitor.sweep()
  assert.equal(sends,1)
  assert.equal(card.status,'open','a valid one-hour card must not be orphaned then retired as absent')
  assert.equal(record.reply?.id,card.id)
  assert.deepEqual(record.reply.descriptor,descriptor)
  await monitor.sweep()
  assert.equal(sends,1,'registration must not be replayed')
  assert.equal(card.status,'open')
  assert.equal(card.expires-card.created,3600)
  const registration=calls.find(call=>call.request.action==='reply_register')
  assert.equal(registration.timeout,60)
  assert.equal(registration.outer,75_000)
})
test('clarify registration keeps its ambiguous-write fence when the longer deadline is exceeded',async()=>{
  const f=fixture({elapsed:61})
  await assert.rejects(f.monitor.helper({action:'reply_register'}),
    error=>error instanceof NativeHelperError&&error.ambiguousWrite===true)
  assert.equal(f.calls.length,1)
  assert.equal(f.calls[0].args.timeout,60)
  assert.equal(f.calls[0].outer,75_000)
})
test('registration lookup transport failure is read-only and may be repeated without a write fence',async()=>{
  const f=fixture({fail:true})
  await assert.rejects(f.monitor.helper({action:'reply_registration_lookup'}),
    error=>error instanceof NativeHelperError&&error.ambiguousWrite===false)
  assert.equal(f.calls.length,1)
  assert.equal(f.calls[0].args.timeout,25)
  assert.equal(f.calls[0].outer,35_000)
})
test('ordinary send, attention and input paths retain their existing deadlines',async()=>{
  const f=fixture()
  const actions=['send','attention','reply_owner_heartbeat','reply_poll','reply_fragment','reply_settle','reply_claim_valid',
    'approval_owner_heartbeat','approval_register','followup_owner_heartbeat','settings_status']
  for(const action of actions)await f.monitor.helper({action})
  assert.equal(f.calls.length,actions.length)
  for(const call of f.calls){assert.equal(call.args.timeout,25);assert.equal(call.outer,35_000)}
})
test('longer recovery budget never changes unknown-send no-replay classification',async()=>{
  const f=fixture({fail:true})
  await assert.rejects(f.monitor.helper({action:'completion_recovery_send',route}),e=>e instanceof NativeHelperError&&e.ambiguousWrite===true)
  assert.equal(f.calls.length,1)
  assert.equal(f.calls[0].args.timeout,60)
  assert.equal(f.calls[0].outer,75_000)
})

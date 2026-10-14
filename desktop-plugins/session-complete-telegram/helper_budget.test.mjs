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
test('ordinary send, attention and input paths retain their existing deadlines',async()=>{
  const f=fixture()
  const actions=['send','attention','reply_owner_heartbeat','approval_owner_heartbeat','followup_owner_heartbeat','settings_status']
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

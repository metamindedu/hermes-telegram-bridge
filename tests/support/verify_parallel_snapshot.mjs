// Consume only synthetic native producer output from the guarded Python test.
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {registerHooks} from 'node:module'
registerHooks({resolve(s,c,n){
  if(s==='@hermes/plugin-sdk')return {url:'data:text/javascript,export const host={}',shortCircuit:true}
  if(s==='react/jsx-runtime')return {url:'data:text/javascript,export const jsx=()=>null',shortCircuit:true}
  return n(s,c)
}})
const {CompletionMonitor,APPROVAL_CAPABILITY,APPROVAL_COMPAT_VERSION}=await import('../../desktop-plugins/session-complete-telegram/desktop/plugin.js')
const x=JSON.parse(readFileSync(process.argv[2],'utf8'))
assert.equal(x.entries.length,2)
const requests=x.entries.map(e=>e.request),queue=x.entries.map(e=>e.pending),registered=[]
const monitor=new CompletionMonitor({state:{profile:{get:()=>x.route.profile}},profileRoutes:async()=>[x.route],
  notify:()=>{},requestProfile:async(_r,method)=>{
    if(method==='session.events.since')return {epoch:x.epoch,open_requests:requests}
    if(method==='approval.pending')return {approvals:queue}
    if(method==='session.active_list')return {sessions:[x.row]}
    throw new Error('unexpected native SDK operation '+method)
  }},{setTimeout:()=>()=>{}})
monitor.replyOwner=x.owner;monitor.approvalCapable=true
monitor.helper=async req=>{
  if(req.action==='approval_owner_heartbeat')return {ok:true,feature_ready:true,version:APPROVAL_COMPAT_VERSION,capability:APPROVAL_CAPABILITY}
  if(req.action==='approval_register'){
    const entry=x.entries.find(e=>e.request.id===req.request.id)
    assert.deepEqual(req.request,entry.request)
    assert.deepEqual(req.pending,entry.pending)
    assert.deepEqual(req.descriptor,entry.descriptor)
    registered.push(req.request.id)
    return entry.receipt
  }
  throw new Error('unexpected helper action '+req.action)
}
const record={key:'native-parent',sid:x.row.id,profile:x.route.profile,connectionId:x.route.connectionId,seq:0,ask:null}
monitor.records.set(record.key,record)
await monitor.checkAsk(record,x.route,x.row)
assert.deepEqual(registered,requests.map(r=>r.id))
assert.equal(monitor.approvalRecords().filter(r=>r.approval).length,2)
await monitor.checkAsk(record,x.route,x.row)
assert.equal(registered.length,2)
console.log('PASS: actual native parallel protected-write snapshots and Python card receipts match both renderer slots; no duplicate registration')

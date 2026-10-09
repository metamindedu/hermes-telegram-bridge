import test from 'node:test'
import assert from 'node:assert/strict'
import {registerHooks} from 'node:module'
registerHooks({resolve(s,c,n){return s==='@hermes/plugin-sdk'?{url:'data:text/javascript,export const host={}',shortCircuit:true}:n(s,c)}})
const {CompletionMonitor}=await import('./plugin.js')
function fixture(){
 const route={mode:'local',connectionId:'local',profile:'default',targetProfile:'default'}
 const row={id:'sid',session_key:'durable',started_at:100,status:'idle'}
 const snap={epoch:'epoch',open_requests:[]}
 const writes=[],settles=[],helpers=[];let pending=true,claim=false
 const f={route,row,snap,writes,settles,helpers,enabled:true,voice:false,result:'streaming',deadline:Date.now()/1000+40,text:'다음 단계 진행'}
 const api={profileRoutes:async()=>[route],requestProfile:async(r,m,p)=>{
  if(m==='session.active_list')return {sessions:[row]}
  if(m==='session.events.since')return snap
  if(m==='voice.toggle'){assert.deepEqual(p,{action:'status'});return {enabled:f.voice}}
  if(m==='prompt.submit'){writes.push({r,p});if(f.throwWrite)throw Error('ambiguous');return {status:f.result}}
  throw Error('FORBIDDEN '+m)
 }}
 const mon=new CompletionMonitor(api,{setTimeout:()=>()=>{}})
 const d={owner:mon.replyOwner,connectionId:'local',profile:'default',targetProfile:'default',sid:'sid',session_key:'durable',started_at:'100',epoch:'epoch'}
 const item={bot:77,chat:99,message_id:200,binding_id:'binding',descriptor:d}
 mon.helper=async p=>{helpers.push(p);switch(p.action){
  case 'followup_owner_heartbeat':return {feature_ready:f.enabled}
  case 'followup_ready':return {items:pending?[item]:[]}
  case 'followup_reject':pending=false;return {ok:true}
  case 'followup_claim':if(claim)return {ok:false};claim=true;pending=false;return {ok:true,claim:'claim',descriptor:d,text:f.text}
  case 'followup_begin_submit':if(f.onBegin)f.onBegin();return {ok:true,deadline:f.deadline}
  case 'followup_settle':settles.push(p);return {ok:true}
  default:throw Error(p.action)
 }}
 return Object.assign(f,{api,mon,d})
}
for(const [status,result] of [['idle','streaming'],['working','queued']])test('same session '+status+' explicit queue no interrupt',async()=>{
 const f=fixture();f.row.status=status;f.result=result;await f.mon.pumpFollowups();await f.mon.pumpFollowups()
 assert.equal(f.writes.length,1);assert.deepEqual(f.writes[0].p,{session_id:'sid',text:f.text,queued:true});assert.equal(f.settles[0].status,'accepted_'+result)
 assert.ok(f.helpers.every(p=>!JSON.stringify(p).includes(f.text)))
})
for(const field of ['waiting','epoch','durable','runtime','started','connection','remote','profile','open','voice','disabled','deadline','slash','dispose'])test('reject '+field,async()=>{
 const f=fixture();
 if(field==='waiting')f.row.status='waiting'
 if(field==='epoch')f.snap.epoch='other'
 if(field==='durable')f.row.session_key='other'
 if(field==='runtime')f.row.id='other'
 if(field==='started')f.row.started_at=200
 if(field==='connection')f.route.connectionId='other'
 if(field==='remote')f.route.mode='remote'
 if(field==='profile')f.route.targetProfile='other'
 if(field==='open')f.snap.open_requests=[{method:'approval'}]
 if(field==='voice')f.voice=true
 if(field==='disabled')f.enabled=false
 if(field==='deadline')f.deadline=Date.now()/1000-1
 if(field==='slash')f.text=' /stop'
 if(field==='dispose')f.onBegin=()=>f.mon.dispose()
 await f.mon.pumpFollowups();assert.equal(f.writes.length,0)
})
for(const receipt of ['timeout','steered','redirected',undefined])test('ambiguous '+receipt+' never retry',async()=>{
 const f=fixture();f.result=receipt;if(receipt==='timeout')f.throwWrite=true
 await f.mon.pumpFollowups();await f.mon.pumpFollowups();assert.equal(f.writes.length,1);assert.equal(f.settles[0].status,'unknown')
})
test('concurrent pumps claim once',async()=>{const f=fixture();await Promise.all([f.mon.pumpFollowups(),f.mon.pumpFollowups()]);assert.equal(f.writes.length,1)})
test('fresh route inventory not thirty second cache',async()=>{const f=fixture();f.mon.routes=[{...f.route}];f.mon.routesAt=Date.now();f.route.connectionId='changed';await f.mon.pumpFollowups();assert.equal(f.writes.length,0)})

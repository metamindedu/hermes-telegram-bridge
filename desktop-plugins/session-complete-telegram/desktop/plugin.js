import { host } from '@hermes/plugin-sdk'
import * as UI from '@hermes/plugin-sdk'
import { jsx } from 'react/jsx-runtime'

// App-level disk plugin: explicit settings writes use the fixed native helper.
export const VERSION = '1.8.1'
export const APPROVAL_COMPAT_VERSION = '1.8.0'
export const APPROVAL_CAPABILITY = 'desktop-command-approval-v6'
export const COMPLETION_RECOVERY_CAPABILITY = 'desktop-completion-recovery-v1'
export const COMPLETION_RECOVERY_TTL_MS = 30 * 60_000
export const APPROVAL_LEASE_MS = 45_000

export function stableJSON(value) {
  if (Array.isArray(value)) return '[' + value.map(stableJSON).join(',') + ']'
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + stableJSON(value[k])).join(',') + '}'
  return JSON.stringify(value)
}

const SESSION_STATUSES = new Set(['idle', 'working', 'waiting', 'starting'])
const REPLY_ACTIVE_STATUSES = new Set(['posting', 'open', 'queued', 'applying'])

function validateSessionRow(row) {
  if (!row || typeof row !== 'object' || Array.isArray(row) || typeof row.id !== 'string' || !row.id ||
      typeof row.session_key !== 'string' || !row.session_key || !SESSION_STATUSES.has(row.status)) {
    throw new Error('Incomplete active-session row')
  }
  return row
}

function validateActiveSessions(active) {
  if (!active || typeof active !== 'object' || Array.isArray(active) || !Array.isArray(active.sessions)) {
    throw new Error('Incomplete active-session snapshot')
  }
  const ids = new Set()
  for (const row of active.sessions) {
    validateSessionRow(row)
    if (ids.has(row.id)) throw new Error('Duplicate active-session identity')
    ids.add(row.id)
  }
  return active.sessions
}

function validateClarifyParams(params) {
  if (!params || typeof params !== 'object' || Array.isArray(params) ||
      typeof params.session_id !== 'string' || !params.session_id) {
    throw new Error('Incomplete clarify parameters')
  }
  // Match the bridge's single/batch shape before treating a different
  // fingerprint as definitive change. Partial transport data is unverified.
  const batch = Array.isArray(params.questions) && params.questions.length > 0
  const questions = batch ? params.questions : [params]
  const qids = new Set()
  for (const question of questions) {
    if (!question || typeof question !== 'object' || Array.isArray(question) ||
        typeof question.question !== 'string' || !question.question.trim() ||
        (question.choices != null && (!Array.isArray(question.choices) ||
          question.choices.some(choice => typeof choice !== 'string' || !choice.trim())))) {
      throw new Error('Incomplete clarify question')
    }
    const qid = batch ? question.qid : 'single'
    if (typeof qid !== 'string' || !qid || qids.has(qid)) throw new Error('Incomplete clarify question identity')
    qids.add(qid)
  }
  if (params.answers != null && (typeof params.answers !== 'object' || Array.isArray(params.answers) ||
      Object.entries(params.answers).some(([qid, answer]) => !qids.has(qid) || typeof answer !== 'string'))) {
    throw new Error('Incomplete clarify answers')
  }
}

function validateOpenRequests(snapshot) {
  if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot) || !Array.isArray(snapshot.open_requests)) {
    throw new Error('Incomplete open-request snapshot')
  }
  for (const request of snapshot.open_requests) {
    if (!request || typeof request !== 'object' || Array.isArray(request) ||
        typeof request.id !== 'string' || !request.id || typeof request.method !== 'string' ||
        (request.params != null && (typeof request.params !== 'object' || Array.isArray(request.params)))) {
      throw new Error('Malformed open request')
    }
  }
  return snapshot.open_requests
}

function validateReplySnapshot(snapshot) {
  const requests = validateOpenRequests(snapshot)
  // Reply capabilities require a complete clarify shape; notification-only
  // legacy requests still use validateOpenRequests without this action gate.
  for (const request of requests) if (request.method === 'clarify') validateClarifyParams(request.params)
  if (!((typeof snapshot.epoch === 'string' && snapshot.epoch.length > 0) ||
        (typeof snapshot.epoch === 'number' && Number.isFinite(snapshot.epoch)))) {
    throw new Error('Missing clarify epoch')
  }
  return requests
}

export function validReplySnapshot(bridge, route, row, snapshot) {
  const d = bridge.descriptor
  const requests = validateReplySnapshot(snapshot)
  if (row != null) validateSessionRow(row)
  const ask = requests.find(a => a.id === d.request_id)
  return route.mode === 'local' && route.connectionId === d.connectionId && route.profile === d.profile &&
      route.targetProfile === d.targetProfile && row?.id === d.sid && row?.session_key === d.session_key &&
    row?.status === 'waiting' && String(snapshot.epoch) === d.epoch && ask?.method === 'clarify' &&
    ask.params?.session_id === d.sid && stableJSON(ask) === d.fingerprint
}

const NATIVE_APPROVAL_CHOICES = new Set(['once','session','always','deny'])
function nativeJSONValue(value, depth=0) {
  if (depth > 32) return false
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return true
  if (typeof value === 'number') return Number.isFinite(value)
  if (Array.isArray(value)) return value.every(item => nativeJSONValue(item, depth + 1))
  if (!value || typeof value !== 'object' ||
      ![Object.prototype,null].includes(Object.getPrototypeOf(value))) return false
  return Object.entries(value).every(([key,item]) => typeof key === 'string' && nativeJSONValue(item, depth + 1))
}

function validApprovalChoices(params) {
  const choices=params?.choices
  if (!Array.isArray(choices) || choices.length<2 || choices.length>4 ||
      choices.some(choice=>typeof choice!=='string'||!NATIVE_APPROVAL_CHOICES.has(choice)) ||
      new Set(choices).size!==choices.length || !choices.includes('once') || !choices.includes('deny')) return false
  for(const key of ['allow_permanent','allow_session','smart_denied']){
    if(Object.hasOwn(params,key)&&typeof params[key]!=='boolean')return false
  }
  if(params.allow_session===false&&(choices.includes('session')||choices.includes('always')))return false
  if(params.allow_permanent===false&&choices.includes('always'))return false
  if(params.allow_permanent===true&&params.allow_session===false)return false
  if(params.smart_denied===true&&
      (params.allow_session!==false||params.allow_permanent!==false||stableJSON([...choices].sort())!==stableJSON(['deny','once'])))return false
  return true
}

function validNativeApprovalRequest(request,runtimeSid) {
  if(!request||typeof request!=='object'||Array.isArray(request)||request.method!=='approval'||
      typeof request.id!=='string'||!/^srq-[0-9a-f]{12}$/u.test(request.id)||
      !request.params||typeof request.params!=='object'||Array.isArray(request.params)||
      !nativeJSONValue(request)||typeof runtimeSid!=='string'||!runtimeSid)return false
  const p=request.params
  if(p.session_id!==runtimeSid||typeof p.request_id!=='string'||!/^[0-9a-f]{32}$/u.test(p.request_id)||
      typeof p.command!=='string'||!p.command||typeof p.description!=='string'||!p.description||
      typeof p.pattern_key!=='string'||!p.pattern_key||!Array.isArray(p.pattern_keys)||!p.pattern_keys.length||
      p.pattern_keys.some(key=>typeof key!=='string'||!key)||new Set(p.pattern_keys).size!==p.pattern_keys.length||
      !p.pattern_keys.includes(p.pattern_key)||
      (p.pattern_key.startsWith('editor-')&&!/^editor-[0-9a-f]{32}$/u.test(p.pattern_key))||
      !validApprovalChoices(p))return false
  if(Object.hasOwn(p,'session_key')&&(typeof p.session_key!=='string'||!p.session_key))return false
  try{return new TextEncoder().encode(stableJSON(request)).byteLength<=22_500}catch{return false}
}

function validNativeQueueRow(row) {
  if(!row||typeof row!=='object'||Array.isArray(row)||!nativeJSONValue(row)||
      typeof row.request_id!=='string'||! /^[0-9a-f]{32}$/u.test(row.request_id)||
      typeof row.command!=='string'||!row.command||typeof row.description!=='string'||!row.description||
      typeof row.pattern_key!=='string'||!row.pattern_key||!Array.isArray(row.pattern_keys)||!row.pattern_keys.length||
      row.pattern_keys.some(key=>typeof key!=='string'||!key)||new Set(row.pattern_keys).size!==row.pattern_keys.length||
      !row.pattern_keys.includes(row.pattern_key)||
      (row.pattern_key.startsWith('editor-')&&!/^editor-[0-9a-f]{32}$/u.test(row.pattern_key)))return false
  for(const key of ['allow_permanent','allow_session','smart_denied']){
    if(Object.hasOwn(row,key)&&typeof row[key]!=='boolean')return false
  }
  return !Object.hasOwn(row,'choices')||validApprovalChoices(row)
}

function queuePairMatches(request,pending,runtimeSid) {
  if(!validNativeApprovalRequest(request,runtimeSid)||!validNativeQueueRow(pending)||
      pending.request_id!==request.params.request_id||
      (Object.hasOwn(pending,'choices')&&stableJSON(pending.choices)!==stableJSON(request.params.choices)))return false
  const requestFields=Object.fromEntries(Object.entries(request.params).filter(([key])=>!['session_id','choices'].includes(key)))
  const queueFields=Object.fromEntries(Object.entries(pending).filter(([key])=>key!=='choices'))
  return stableJSON(requestFields)===stableJSON(queueFields)
}

function queuePayloadConflicts(request,row) {
  const p=request.params
  if(row.request_id===p.request_id)return false
  const keys=['command','description','pattern_key','pattern_keys','edit'].filter(key=>
    Object.hasOwn(p,key)&&Object.hasOwn(row,key))
  try{return keys.length>=2&&keys.every(key=>stableJSON(p[key])===stableJSON(row[key]))}catch{return true}
}

export function classifyApprovalAuthority(request,queueSnapshot,runtimeSid) {
  if(!validNativeApprovalRequest(request,runtimeSid)||!queueSnapshot||typeof queueSnapshot!=='object'||
      Array.isArray(queueSnapshot)||!exactKeys(queueSnapshot,['approvals'])||!Array.isArray(queueSnapshot.approvals)||
      queueSnapshot.approvals.length>100)return null
  const ids=new Set()
  for(const row of queueSnapshot.approvals){
    if(!validNativeQueueRow(row)||ids.has(row.request_id))return null
    ids.add(row.request_id)
  }
  const matches=queueSnapshot.approvals.filter(row=>row.request_id===request.params.request_id)
  if(matches.length){
    if(matches.length!==1||!queuePairMatches(request,matches[0],runtimeSid))return null
    return {mode:'legacyqueue',pending:matches[0]}
  }
  if(queueSnapshot.approvals.some(row=>queuePayloadConflicts(request,row)))return null
  return {mode:'legacyserver_request',pending:{request_id:request.params.request_id,missing:true}}
}

export function validateApprovalPair(request,pending,runtimeSid,dispatchMode='legacyqueue') {
  if(!validNativeApprovalRequest(request,runtimeSid))return false
  if(dispatchMode==='legacyqueue')return queuePairMatches(request,pending,runtimeSid)
  return dispatchMode==='legacyserver_request'&&stableJSON(pending)===stableJSON(
    {request_id:request.params.request_id,missing:true})
}

export async function approvalFingerprint(value) {
  if (!globalThis.crypto?.subtle) throw new Error('approval_digest_unavailable')
  const bytes = new TextEncoder().encode(stableJSON(value))
  const digest = new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', bytes))
  return Array.from(digest, byte => byte.toString(16).padStart(2, '0')).join('')
}

export function validApprovalSnapshot(bridge, route, row, snapshot, pending, owner) {
  try {
    const d = bridge?.descriptor
    if (!d || d.owner !== owner || bridge.requestFingerprint !== d.request_fingerprint ||
        bridge.pendingFingerprint !== d.pending_fingerprint || !route || route.mode !== 'local' ||
        route.connectionId !== d.connectionId || route.profile !== d.profile ||
        route.targetProfile !== d.targetProfile || row?.id !== d.sid ||
        row?.session_key !== d.session_key || row?.status !== 'waiting' ||
        !snapshot || !((typeof snapshot.epoch === 'string' && snapshot.epoch) ||
          (typeof snapshot.epoch === 'number' && Number.isFinite(snapshot.epoch))) ||
        !Array.isArray(snapshot.open_requests) || !Array.isArray(pending)) return false
    const requests = validateOpenRequests(snapshot)
    const matches = requests.filter(q => q.id === d.request_id)
    if (matches.length !== 1 || matches[0].method !== 'approval') return false
    if (matches[0].params?.request_id !== d.queue_id ||
        (matches[0].params?.session_key != null && matches[0].params.session_key !== d.session_key)) return false
    const authority=classifyApprovalAuthority(matches[0],{approvals:pending},d.sid)
    if(!authority||authority.mode!==d.dispatch_mode||
        !validateApprovalPair(matches[0],authority.pending,d.sid,d.dispatch_mode)||
        stableJSON(authority.pending)!==stableJSON(bridge.pending))return false
    return stableJSON(matches[0]) === stableJSON(bridge.request) &&
      stableJSON(authority.pending) === stableJSON(bridge.pending) &&
      String(snapshot.epoch) === d.epoch
  } catch {
    return false
  }
}

function exactKeys(value, keys) {
  return value && typeof value === 'object' && !Array.isArray(value) &&
    Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key))
}

function validReplyResult(bridge, result) {
  const questions = bridge.ask.params.questions
  if (Array.isArray(questions) && questions.length) {
    if (!exactKeys(result, ['answers']) || !exactKeys(result.answers, questions.map(q => q.qid).sort())) return false
    // The backend enforces the direct-text limit. Canonical multi-select
    // strings and existing locked answers have distinct supported semantics.
    return questions.every(q => typeof result.answers[q.qid] === 'string' && result.answers[q.qid].trim())
  }
  return exactKeys(result, ['answer']) && typeof result.answer === 'string' && result.answer.trim()
}

function fragmentError(message, claimExpired = false) {
  const error = new Error(message)
  error.claimExpired = claimExpired
  return error
}

function decodeFragmentChunk(value, expectedBytes) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]+$/u.test(value) || value.length > Math.ceil(FRAGMENT_CHUNK_BYTES * 4 / 3)) {
    throw fragmentError('invalid_fragment_chunk')
  }
  const base64 = value.replaceAll('-', '+').replaceAll('_', '/') + '='.repeat((4 - value.length % 4) % 4)
  const binary = atob(base64)
  if (binary.length !== expectedBytes) throw fragmentError('invalid_fragment_chunk')
  const bytes = Uint8Array.from(binary, char => char.charCodeAt(0))
  let canonical = ''
  for (const byte of bytes) canonical += String.fromCharCode(byte)
  if (btoa(canonical).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/u, '') !== value) {
    throw fragmentError('invalid_fragment_chunk')
  }
  return bytes
}
export const QUIET_MS = 60_000
export const ATTENTION_POLL_MS = 8_000
// Server→client requests that block the agent on a HUMAN decision. GUI bridges
// (terminal.read / preview.act / window.read / tour) are agent↔renderer
// automation and must never raise "input needed".
export const HUMAN_ASK_METHODS = new Set(['clarify', 'approval', 'sudo', 'secret', 'vault.unlock_prompt', 'vault.save_login', 'vault.code'])
const ID = 'session-complete-telegram'
const MAX_REQUEST_BYTES = 22_500
const MAX_RESULT_BYTES = 24 * 1024
const MAX_FRAGMENT_BYTES = 256 * 1024
const FRAGMENT_CHUNK_BYTES = 16 * 1024
const MAX_FRAGMENT_CHUNKS = MAX_FRAGMENT_BYTES / FRAGMENT_CHUNK_BYTES
const RESULT_FRAME_BEGIN = 'HERMES_TELEGRAM_BRIDGE_RESULT_V1_BEGIN'
const RESULT_FRAME_END = 'HERMES_TELEGRAM_BRIDGE_RESULT_V1_END'
const HELPER_WRITE_ACTIONS = new Set([
  'send', 'attention', 'completion_recovery_record', 'completion_recovery_cancel',
  'completion_recovery_touch', 'completion_recovery_read_blocked',
  'completion_recovery_claim', 'completion_recovery_send',
  'reply_register', 'reply_poll', 'reply_settle', 'reply_fragment',
  'approval_owner_heartbeat', 'approval_register', 'approval_poll', 'approval_settle', 'approval_notice',
  'canonical_approval_observe', 'approval_native_observe', 'approval_native_apply',
  'followup_claim', 'followup_begin_submit', 'followup_reject', 'followup_settle',
  'loaded', 'diagnostic', 'settings_set'
])
const RECOVERY_PRE_DISPATCH_ACTIONS = new Set([
  'completion_recovery_record', 'completion_recovery_cancel', 'completion_recovery_touch',
  'completion_recovery_read_blocked', 'completion_recovery_claim', 'completion_recovery_list',
  'completion_recovery_policy', 'completion_recovery_get', 'completion_recovery_classify'
])

const RECOVERY_RESTORABLE_STATES = new Set(['pending','read_blocked','claimed','unsent_retryable'])
const RECOVERY_TERMINAL_STATES = new Set(['sent','unknown','failed','rejected','cancelled','expired','disabled'])
const RECOVERY_ALL_STATES = new Set([...RECOVERY_RESTORABLE_STATES, ...RECOVERY_TERMINAL_STATES, 'dispatching'])

export function validateCompletionRecoveryPolicy(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  if (value.ok === false && typeof value.reason === 'string' && /^[a-z][a-z0-9_]{0,63}$/u.test(value.reason)) {
    return {ok:false, reason:value.reason}
  }
  if (value.ok !== true || typeof value.enabled !== 'boolean' || typeof value.generation !== 'string' ||
      !/^[0-9a-f]{32}$/u.test(value.generation)) return null
  return {ok:true, enabled:value.enabled, generation:value.generation}
}

function validateRecoveryPublicItem(item, route) {
  if (!item || typeof item !== 'object' || Array.isArray(item) ||
      typeof item.candidate_id !== 'string' || !/^[0-9a-f]{64}$/u.test(item.candidate_id) ||
      !Number.isSafeInteger(item.revision) || item.revision < 1 || !RECOVERY_ALL_STATES.has(item.state) ||
      !item.route || item.route.mode !== 'local' || typeof item.route.connectionId !== 'string' || !item.route.connectionId ||
      typeof item.route.profile !== 'string' || !item.route.profile ||
      typeof item.route.targetProfile !== 'string' || !item.route.targetProfile ||
      typeof item.runtime_id !== 'string' || !item.runtime_id ||
      typeof item.session_key !== 'string' || !item.session_key ||
      typeof item.started_at !== 'string' || !item.started_at ||
      typeof item.epoch !== 'string' || !item.epoch ||
      typeof item.generation !== 'string' || !validateCompletionRecoveryPolicy({ok:true,enabled:true,generation:item.generation}) ||
      !Number.isSafeInteger(item.seq) || item.seq < 1 ||
      !Number.isSafeInteger(item.bookkeeping_seq) || item.bookkeeping_seq < item.seq ||
      !Number.isSafeInteger(item.final_row_id) || item.final_row_id < 1 ||
      !Number.isFinite(item.created_at) || !Number.isFinite(item.quiet_at) || !Number.isFinite(item.expires_at) ||
      item.expires_at <= item.created_at || item.quiet_at < item.created_at) return false
  if (route && (item.route.connectionId !== route.connectionId || item.route.profile !== route.profile ||
      item.route.targetProfile !== route.targetProfile)) return false
  return true
}

function publicRecoveryRoute(route) {
  return {mode:'local', connectionId:route.connectionId, profile:route.profile, targetProfile:route.targetProfile}
}

export class NativeHelperError extends Error {
  constructor(message, ambiguousWrite = false, permanent = false) {
    super(message)
    this.name = 'NativeHelperError'
    this.ambiguousWrite = ambiguousWrite
    this.permanent = permanent
  }
}

function validateRequestJSON(value, seen = new Set()) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new TypeError('non_finite_number')
    return
  }
  if (!value || typeof value !== 'object' || seen.has(value)) throw new TypeError('unsupported_json_value')
  if (!Array.isArray(value)) {
    const prototype = Object.getPrototypeOf(value)
    if (prototype !== Object.prototype && prototype !== null) throw new TypeError('unsupported_json_object')
  }
  seen.add(value)
  if (Array.isArray(value)) {
    for (let index = 0; index < value.length; index++) {
      if (!Object.hasOwn(value, index)) throw new TypeError('sparse_json_array')
      validateRequestJSON(value[index], seen)
    }
  } else {
    for (const key of Object.keys(value)) validateRequestJSON(value[key], seen)
  }
  seen.delete(value)
}

export function encodeRequest(value) {
  let bytes
  try {
    validateRequestJSON(value)
    const json = JSON.stringify(value)
    if (typeof json !== 'string') throw new TypeError('not_json')
    bytes = new TextEncoder().encode(json)
  } catch {
    throw new NativeHelperError('Helper request could not be encoded', false, true)
  }
  if (bytes.byteLength > MAX_REQUEST_BYTES) throw new NativeHelperError('Helper request is too large', false, true)
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/u, '')
}

export function decodeResultFrame(output) {
  if (typeof output !== 'string' || output.length > 48_000) throw new NativeHelperError('Helper response is unavailable')
  const lines = output.split(/\r?\n/u)
  const beginLines = lines.map((line, index) => ({line, index})).filter(x => x.line.includes(RESULT_FRAME_BEGIN))
  const endLines = lines.map((line, index) => ({line, index})).filter(x => x.line.includes(RESULT_FRAME_END))
  if (beginLines.length !== 1 || endLines.length !== 1 ||
      beginLines[0].line !== RESULT_FRAME_BEGIN || endLines[0].line !== RESULT_FRAME_END ||
      endLines[0].index !== beginLines[0].index + 2) {
    throw new NativeHelperError('Helper response framing is invalid')
  }
  const encoded = lines[beginLines[0].index + 1]
  if (!/^[A-Za-z0-9_-]+$/u.test(encoded) || encoded.length > Math.ceil(MAX_RESULT_BYTES * 4 / 3)) {
    throw new NativeHelperError('Helper response framing is invalid')
  }
  try {
    const base64 = encoded.replaceAll('-', '+').replaceAll('_', '/') + '='.repeat((4 - encoded.length % 4) % 4)
    const binary = atob(base64)
    if (binary.length > MAX_RESULT_BYTES) throw new Error('oversized')
    const bytes = Uint8Array.from(binary, char => char.charCodeAt(0))
    const value = JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(bytes))
    if (!value || typeof value !== 'object' || Array.isArray(value) || typeof value.ok !== 'boolean') throw new Error('shape')
    return value
  } catch {
    throw new NativeHelperError('Helper response payload is invalid')
  }
}

export function unfinishedControl(control) {
  if (!control || !Object.hasOwn(control, 'goal') || !Object.hasOwn(control, 'loop') || !Object.hasOwn(control, 'heartbeat')) return true
  if (control.goal && !['done', 'cleared'].includes(control.goal.status)) return true
  if (control.loop && !['stopped', 'done', 'completed'].includes(control.loop.status)) return true
  if (control.heartbeat && !['stopped', 'cleared'].includes(control.heartbeat.status)) return true
  return false
}

export function blockingProcess(process) {
  if (process.status !== 'running') return false
  // Exclude only explicitly detached services / satisfied readiness watches.
  // An unclassified background command is unfinished, not assumed to be a server.
  return process.detached !== true && !(process.watch_patterns?.length && process.watch_hit === true && process.notify_on_complete !== true)
}

function validateRecoveryLiveRow(row) {
  validateSessionRow(row)
  if (typeof row.session_key !== 'string' || !row.session_key || row.session_key.length > 200 ||
      typeof row.started_at !== 'number' || !Number.isFinite(row.started_at) || row.started_at < 0) {
    throw new Error('Incomplete recovery runtime identity')
  }
  return row
}

function validateRecoveryReplayIdentity(snapshot, watermark) {
  if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot) ||
      !Array.isArray(snapshot.events) || !Array.isArray(snapshot.open_requests) ||
      typeof snapshot.truncated !== 'boolean' || !Number.isSafeInteger(snapshot.latest_seq) || snapshot.latest_seq < 0 ||
      !((typeof snapshot.epoch === 'string' && snapshot.epoch.length > 0) ||
        (typeof snapshot.epoch === 'number' && Number.isFinite(snapshot.epoch))) ||
      snapshot.latest_seq < watermark) throw new Error('Incomplete recovery replay identity')
  return String(snapshot.epoch)
}

function validateRecoveryReplay(snapshot, candidate) {
  const epoch = validateRecoveryReplayIdentity(snapshot, candidate.seq)
  if (snapshot.truncated !== false || epoch !== candidate.epoch ||
      (candidate.bookkeepingSeq ?? candidate.seq) > snapshot.latest_seq ||
      (candidate.nativeBookkeepingSeq ?? candidate.seq) > snapshot.latest_seq) throw new Error('Recovery replay is incomplete or changed')
  let expected = candidate.seq + 1
  let work = false
  let identityChanged = false
  let doneBookkeepingSeq = candidate.bookkeepingSeq ?? candidate.seq
  const workEvents = new Set(['message.start','message.delta','message.interim','message.complete',
    'reasoning.delta','thinking.delta','tool.start','tool.delta','tool.complete','tool.generating',
    'todo.updated','subagent.start','subagent.complete','error'])
  for (const event of snapshot.events) {
    if (!event || typeof event !== 'object' || Array.isArray(event) || typeof event.type !== 'string' ||
        !Number.isSafeInteger(event.seq) || event.seq !== expected++ || event.session_id !== candidate.runtimeId) {
      throw new Error('Recovery replay has an invalid event identity or sequence')
    }
    if (workEvents.has(event.type)) {
      work = true
      continue
    }
    if (event.type === 'session.control.update') {
      const payload = event.payload
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('Malformed control bookkeeping event')
      const control = payload.control ?? event.control
      if (!validateRecoveryControl(control)) work = true
      else doneBookkeepingSeq = Math.max(doneBookkeepingSeq, event.seq)
      continue
    }
    if (event.type === 'session.info') {
      const info = event.payload
      if (!info || typeof info !== 'object' || Array.isArray(info) || typeof info.running !== 'boolean' ||
          typeof info.stored_session_id !== 'string' || !info.stored_session_id ||
          typeof info.profile_name !== 'string' || !info.profile_name.trim()) {
        throw new Error('Malformed native session.info bookkeeping event')
      }
      if (info.stored_session_id !== candidate.sessionKey) {
        identityChanged = true
        continue
      }
      if (info.running) work = true
      else doneBookkeepingSeq = Math.max(doneBookkeepingSeq, event.seq)
      continue
    }
    if (event.type === 'status.update') {
      const payload = event.payload
      if (!payload || typeof payload !== 'object' || Array.isArray(payload) ||
          !exactKeys(payload, ['kind','text']) || !['goal','loop'].includes(payload.kind) ||
          typeof payload.text !== 'string' || !payload.text.trim()) {
        throw new Error('Unclassified native status.update event')
      }
      doneBookkeepingSeq = Math.max(doneBookkeepingSeq, event.seq)
      continue
    }
    throw new Error('Recovery replay contains an unclassified event')
  }
  if (expected - 1 !== snapshot.latest_seq) throw new Error('Recovery replay does not reach the live watermark')
  const openRequests = validateOpenRequests(snapshot)
  if (openRequests.some(request => !HUMAN_ASK_METHODS.has(request.method))) work = true
  return {work, identityChanged, doneBookkeepingSeq, openRequests}
}

function validateRecoveryControl(control) {
  if (!control || typeof control !== 'object' || Array.isArray(control) ||
      !['goal','loop','heartbeat'].every(key => Object.hasOwn(control, key))) throw new Error('Incomplete recovery control snapshot')
  for (const key of ['goal','loop','heartbeat']) {
    const item = control[key]
    if (item !== null && (!item || typeof item !== 'object' || Array.isArray(item) ||
        typeof item.status !== 'string' || !item.status)) throw new Error('Malformed recovery control state')
  }
  return !unfinishedControl(control)
}

function validateRecoveryChildren(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      !Array.isArray(value.subagents) || !Array.isArray(value.delegations)) throw new Error('Incomplete child snapshot')
  const childBlocked = value.subagents.some(item => {
    if (!item || typeof item !== 'object' || Array.isArray(item) || typeof item.status !== 'string' || !item.status) {
      throw new Error('Malformed child snapshot')
    }
    return !['completed','failed','interrupted','cancelled'].includes(item.status)
  })
  const delegationBlocked = value.delegations.some(item => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) throw new Error('Malformed delegation snapshot')
    const status = item.status || item.state
    if (typeof status !== 'string' || !status) throw new Error('Malformed delegation snapshot')
    return !['completed','delivered'].includes(status)
  })
  return childBlocked || delegationBlocked
}

function validateRecoveryProcesses(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || !Array.isArray(value.processes)) {
    throw new Error('Incomplete process snapshot')
  }
  const known = new Set(['running','completed','failed','interrupted','cancelled','stopped','terminated',
    'succeeded','error','timeout','exited','queued','pending','starting'])
  for (const process of value.processes) {
    if (!process || typeof process !== 'object' || Array.isArray(process) ||
        typeof process.status !== 'string' || !known.has(process.status)) throw new Error('Malformed process snapshot')
  }
  return value.processes.some(process => ['queued','pending','starting'].includes(process.status) || blockingProcess(process))
}

export class CompletionMonitor {
  constructor(api, ctx, now = () => Date.now()) {
    this.api = api
    this.ctx = ctx
    this.now = now
    this.records = new Map()
    this.routes = []
    this.routesAt = 0
    this.stopped = false
    this.loadedRecorded = false
    this.replyOwner = globalThis.crypto.randomUUID()
    this.hadReplies = false
    this.replySweepUnverified = new Set()
    this.replySweepAbsent = new Set()
    this.lastReplyPollSucceeded = false
    this.approvalCapable = false
    this.recoveryCapable = false
    this.recoveryPolicy = null
    this.recoveryPolicyAvailable = false
    this.recoveryPolicyRefreshing = null
    this.recoveryRestoreStarted = false
    this.approvalSweepUnverified = new Set()
    this.approvalSweepAbsent = new Set()
    this.lastApprovalPollSucceeded = false
  }

  async refreshRoutes(force = false) {
    if (force || !this.routes.length || this.now() - this.routesAt > 30_000) {
      this.routes = (await this.api.profileRoutes()).filter(r => r.mode === 'local')
      this.routesAt = this.now()
    }
    return this.routes
  }

  async routeFor(record) {
    const routes = await this.refreshRoutes()
    const matches = routes.filter(r => r.profile === record.profile && (!record.connectionId || r.connectionId === record.connectionId))
    if (matches.length !== 1) throw new Error('Ambiguous or unavailable local profile route')
    return matches[0]
  }

  currentLiveRoute(route) {
    const read = atom => {
      try { return typeof atom?.get === 'function' ? atom.get() : undefined } catch { return undefined }
    }
    return Boolean(route && route.mode === 'local' && typeof route.connectionId === 'string' && route.connectionId &&
      typeof route.profile === 'string' && route.profile && typeof route.targetProfile === 'string' && route.targetProfile &&
      read(this.api.state?.gateway) === 'open' && read(this.api.state?.connectionId) === route.connectionId &&
      read(this.api.state?.profile) === route.profile)
  }

  currentEventIsActiveLocal(profile, connectionId) {
    const read = atom => {
      try { return typeof atom?.get === 'function' ? atom.get() : undefined } catch { return undefined }
    }
    const activeRoutes = this.routes.filter(route => route.mode === 'local' && route.connectionId === connectionId && route.profile === profile)
    const senders = this.routes.filter(route => route.mode === 'local' && route.connectionId === connectionId &&
      route.profile === 'default' && route.targetProfile === 'default')
    return Boolean(typeof connectionId === 'string' && connectionId && typeof profile === 'string' && profile &&
      read(this.api.state?.gateway) === 'open' && read(this.api.state?.connectionId) === connectionId &&
      read(this.api.state?.profile) === profile && activeRoutes.length === 1 && senders.length === 1)
  }

  activeLocalRoute(routes) {
    const read = atom => {
      try { return typeof atom?.get === 'function' ? atom.get() : undefined } catch { return undefined }
    }
    const connectionId = read(this.api.state?.connectionId)
    const profile = read(this.api.state?.profile)
    if (read(this.api.state?.gateway) !== 'open' || typeof connectionId !== 'string' || !connectionId ||
        typeof profile !== 'string' || !profile) return null
    const active = routes.filter(route => route.mode === 'local' && route.connectionId === connectionId && route.profile === profile)
    const sender = routes.filter(route => route.mode === 'local' && route.connectionId === connectionId &&
      route.profile === 'default' && route.targetProfile === 'default')
    if (active.length !== 1 || sender.length !== 1 || !this.currentLiveRoute(active[0])) return null
    return active[0]
  }

  async requestCurrentRoute(route, method, params = {}, timeout) {
    if (!this.currentLiveRoute(route)) throw new Error('Recovery route is not the already-live gateway')
    const gateway = this.api.getGateway?.()
    if (!gateway || typeof gateway.request !== 'function' || !this.currentLiveRoute(route) ||
        this.api.getGateway?.() !== gateway) throw new Error('Captured active gateway unavailable')
    return timeout === undefined ? gateway.request(method, params) : gateway.request(method, params, timeout)
  }

  async helper(payload, expectedRecoveryRoute = null) {
    const routes = await this.refreshRoutes()
    const sender = routes.filter(r => r.mode === 'local' && r.targetProfile === 'default' && r.profile === 'default')
    if (sender.length !== 1) throw new NativeHelperError('Default local sender route unavailable')
    const action = payload && typeof payload.action === 'string' ? payload.action : ''
    const recoveryAction = action.startsWith('completion_recovery_')
    const activeRoute = this.activeLocalRoute(routes)
    const useActiveSocket = recoveryAction || (action === 'loaded' && activeRoute !== null)
    let requestCLI
    if (useActiveSocket) {
      if (!activeRoute || activeRoute.connectionId !== sender[0].connectionId) {
        throw new NativeHelperError('Recovery requires the already-open local sender connection')
      }
      if (recoveryAction && action !== 'completion_recovery_policy') {
        const expected = expectedRecoveryRoute || payload.route || payload.candidate?.route || payload.identity?.route
        if (!expected || expected.mode !== 'local' || expected.connectionId !== activeRoute.connectionId ||
            expected.profile !== activeRoute.profile || expected.targetProfile !== activeRoute.targetProfile) {
          throw new NativeHelperError('Recovery action route is not the currently active local route')
        }
      }
      const gateway = this.api.getGateway?.()
      if (!gateway || typeof gateway.request !== 'function' || !this.currentLiveRoute(activeRoute) ||
          this.api.getGateway?.() !== gateway) {
        throw new NativeHelperError('Recovery requires the already-open local gateway')
      }
      requestCLI = (method, params, timeout) => timeout === undefined
        ? gateway.request(method, params) : gateway.request(method, params, timeout)
    } else {
      requestCLI = (method, params, timeout) => this.api.requestProfile(sender[0], method, params, timeout)
    }
    const ambiguousWrite = HELPER_WRITE_ACTIONS.has(action) && !RECOVERY_PRE_DISPATCH_ACTIONS.has(action)
    const argv = ['--profile', 'default', 'desktop-telegram-bridge', '--request', encodeRequest(payload)]
    let result
    try {
      // cli.exec's child budget is seconds; SDK's outer deadline is milliseconds.
      // Clarify registration includes native startup plus the confirmed card
      // send/readback. A 25s cutoff can orphan a sent card before r.reply binds;
      // the next owner poll then retires it as absent. Use the existing bounded
      // startup budget, without retrying any ambiguous write or changing TTL.
      const startupBudget = recoveryAction || action === 'loaded' || action === 'reply_register'
      result = await requestCLI('cli.exec', {argv, timeout: startupBudget ? 60 : 25},
        startupBudget ? 75_000 : 35_000)
    } catch {
      throw new NativeHelperError('Notification helper did not complete', ambiguousWrite)
    }
    if (result?.blocked === true) {
      // A host block is proof the CLI never ran, so this is not an ambiguous write.
      throw new NativeHelperError('Notification helper was blocked before launch')
    }
    if (!result || result.blocked !== false || result.code !== 0 || typeof result.output !== 'string') {
      throw new NativeHelperError('Notification helper did not complete', ambiguousWrite)
    }
    try {
      return decodeResultFrame(result.output)
    } catch {
      throw new NativeHelperError('Notification helper response could not be verified', ambiguousWrite)
    }
  }

  async acceptRecoveryPolicy(value) {
    const policy = validateCompletionRecoveryPolicy(value)
    if (!policy || policy.ok !== true) {
      this.recoveryPolicyAvailable = false
      return null
    }
    const previous = this.recoveryPolicy
    this.recoveryPolicy = policy
    this.recoveryPolicyAvailable = true
    if (previous && (previous.enabled !== policy.enabled || previous.generation !== policy.generation)) {
      await this.retireRecoveryAdmissions(policy)
    }
    return policy
  }

  async refreshRecoveryPolicy() {
    if (this.stopped || !this.recoveryCapable) return null
    if (this.recoveryPolicyRefreshing) return this.recoveryPolicyRefreshing
    const task = (async () => {
      try {
        const reply = await this.helper({action:'completion_recovery_policy'})
        return await this.acceptRecoveryPolicy(reply)
      } catch {
        this.recoveryPolicyAvailable = false
        return null
      }
    })()
    this.recoveryPolicyRefreshing = task
    try { return await task }
    finally { if (this.recoveryPolicyRefreshing === task) this.recoveryPolicyRefreshing = null }
  }

  async retireRecoveryAdmissions(policy) {
    const retirements = []
    for (const record of this.records.values()) {
      const candidate = record.candidate
      if (!candidate?.recoveryGeneration ||
          (policy.enabled && candidate.recoveryGeneration === policy.generation)) continue
      const identity = {...candidate, route:candidate.route}
      const canHandoffAttention = !candidate.unknownOutcome
      const keepExactReadback = candidate.sendAttempted === true && !candidate.unknownOutcome
      candidate.retiredRecoveryGeneration = candidate.recoveryGeneration
      candidate.recoveryGeneration = null
      candidate.recoveryRetired = true
      candidate.ordinaryOnly = false
      candidate.bookkeepingPending = false
      candidate.touchPending = false
      if (candidate.pendingQuietAt != null) {
        candidate.quietAt = Math.max(candidate.quietAt ?? candidate.at, candidate.pendingQuietAt)
        candidate.at = candidate.quietAt
        candidate.pendingQuietAt = null
      }
      if (!keepExactReadback) {
        candidate.recoveryId = null
        candidate.revision = null
      }
      candidate.state = 'retired'
      if (identity.recoveryId) retirements.push(this.cancelRecoveryCandidate(record, identity, 'settings_disabled'))
      if (canHandoffAttention) {
        candidate.at = candidate.quietAt ?? candidate.at
        const delay = Math.max(0, QUIET_MS - (this.now() - candidate.at))
        this.schedule(record, delay)
      } else {
        this.dropLocalCandidate(record, candidate, false)
      }
    }
    await Promise.all(retirements)
  }

  async mounted(force = false) {
    if (this.stopped || (this.loadedRecorded && !force)) return
    try {
      const routes = await this.refreshRoutes(true)
      const reply = await this.helper({action: 'loaded', profiles: routes.map(r => r.profile), version: VERSION})
      this.loadedRecorded = reply.ok === true
      const capable = reply.ok === true && reply.version === APPROVAL_COMPAT_VERSION &&
        Array.isArray(reply.capabilities) && reply.capabilities.includes(APPROVAL_CAPABILITY)
      const recoveryCapable = reply.ok === true && reply.package_version === VERSION &&
        Array.isArray(reply.capabilities) && reply.capabilities.includes(COMPLETION_RECOVERY_CAPABILITY)
      if (capable && !this.approvalCapable) {
        for (const record of this.records.values()) {
          if (record.ask?.method === 'approval') record.ask = null
        }
      }
      this.approvalCapable = capable
      this.recoveryCapable = recoveryCapable
      if (recoveryCapable) {
        let policy = await this.acceptRecoveryPolicy(reply.completion_policy)
        if (!policy) policy = await this.refreshRecoveryPolicy()
        if (policy?.enabled === true && this.recoveryPolicyAvailable) await this.restoreCompletionCandidates()
        for (const record of this.records.values()) {
          if (record.candidate?.recoveryGeneration && !record.candidate.recoveryId) {
            void this.captureCompletionCandidate(record, record.candidate)
          }
        }
      } else {
        for (const record of this.records.values()) {
          const candidate = record.candidate
          if (candidate?.fresh && candidate.recoveryGeneration) {
            candidate.recoveryGeneration = null
            this.schedule(record, Math.max(0, QUIET_MS - (this.now() - (candidate.quietAt ?? candidate.at))))
          }
        }
      }
    } catch {
      // Startup may precede gateway.ready. It is retried by that event, not by
      // launching all profile backends or restarting the user's current session.
    }
  }

  recoveryIdentity(record, candidate) {
    const route = candidate.route || {mode: 'local', connectionId: record.connectionId,
      profile: record.profile, targetProfile: record.profile}
    return {route:{mode:'local', connectionId:route.connectionId, profile:route.profile,
      targetProfile:route.targetProfile || route.profile}, runtime_id:record.sid,
      final_row_id:candidate.finalId}
  }

  async cancelRecoveryCandidate(record, candidate, reason='new_work') {
    if (!this.recoveryCapable || !candidate) return
    try {
      const identity = this.recoveryIdentity(record,candidate)
      await this.helper({action:'completion_recovery_cancel', identity, reason}, identity.route)
    } catch { /* cancellation is retried by late-registration fencing and replay validation */ }
  }

  syncRecoveryItem(record, candidate, item) {
    const expectedGeneration = candidate.recoveryGeneration || candidate.retiredRecoveryGeneration
    if (!validateRecoveryPublicItem(item, candidate.route) || item.candidate_id !== candidate.recoveryId ||
        item.generation !== expectedGeneration || item.runtime_id !== record.sid ||
        item.final_row_id !== candidate.finalId || item.seq !== candidate.seq ||
        item.session_key !== candidate.sessionKey || item.started_at !== candidate.startedAt ||
        String(item.epoch) !== String(candidate.epoch)) return false
    candidate.revision = Math.max(candidate.revision || 0, item.revision)
    candidate.state = item.state
    candidate.bookkeepingSeq = Math.max(candidate.bookkeepingSeq ?? candidate.seq, item.seq)
    candidate.nativeBookkeepingSeq = Math.max(candidate.nativeBookkeepingSeq ?? candidate.seq, item.bookkeeping_seq)
    candidate.quietAt = Math.max(candidate.quietAt ?? candidate.at, item.quiet_at * 1000)
    candidate.at = candidate.quietAt
    if (item.bookkeeping_seq >= candidate.bookkeepingSeq) candidate.pendingQuietAt = null
    candidate.expiresAt = Math.min(candidate.expiresAt || Infinity, item.expires_at * 1000)
    candidate.touchPending = candidate.bookkeepingSeq > candidate.nativeBookkeepingSeq
    return true
  }

  async getRecoveryItem(candidate, route) {
    const reply = await this.helper({action:'completion_recovery_get', candidate_id:candidate.recoveryId,
      route:publicRecoveryRoute(route)})
    if (!exactKeys(reply, ['ok','version','item']) || reply.ok !== true || reply.version !== VERSION) {
      throw new Error('Recovery exact-state receipt is malformed')
    }
    if (reply.item === null) return null
    if (!validateRecoveryPublicItem(reply.item, route)) throw new Error('Recovery exact-state item is malformed')
    return reply.item
  }

  async touchRecoveryCandidate(record, candidate, bookkeepingSeq = candidate.bookkeepingSeq) {
    if (!candidate?.recoveryId || !Number.isSafeInteger(candidate.revision) || !Number.isSafeInteger(bookkeepingSeq)) return false
    const generation = record.generation
    candidate.pendingBookkeepingSeq = Math.max(candidate.pendingBookkeepingSeq ?? candidate.bookkeepingSeq ?? candidate.seq,
      candidate.seq, bookkeepingSeq)
    candidate.touchPending = candidate.touchPending ||
      candidate.pendingBookkeepingSeq > (candidate.nativeBookkeepingSeq ?? candidate.seq)
    if (!candidate.touchPending) return true
    if (candidate.touchInFlight) {
      try { await candidate.touchInFlight } catch { /* the in-flight owner records its own uncertainty */ }
      if (!candidate.touchPending) return true
      if (this.stopped || generation !== record.generation || record.candidate !== candidate) return false
      return this.touchRecoveryCandidate(record, candidate, candidate.pendingBookkeepingSeq)
    }
    const operation = (async () => {
      for (let attempt=0; attempt<3; attempt++) {
        if (this.stopped || generation !== record.generation || record.candidate !== candidate) return false
        const requestedSeq = Math.max(candidate.seq, candidate.pendingBookkeepingSeq ?? candidate.bookkeepingSeq)
        const requestedQuietAt = Math.max(candidate.quietAt ?? candidate.at, candidate.pendingQuietAt ?? 0)
        const nativeBookkeepingSeq = candidate.nativeBookkeepingSeq ?? candidate.seq
        try {
          const reply = await this.helper({action:'completion_recovery_touch',
            candidate:{candidate_id:candidate.recoveryId, revision:candidate.revision,
              generation:candidate.recoveryGeneration},
            bookkeeping_seq:requestedSeq, quiet_at:requestedQuietAt / 1000}, candidate.route)
          if (reply?.ok === true && Number.isSafeInteger(reply.revision) &&
              Number.isSafeInteger(reply.bookkeeping_seq) && Number.isFinite(reply.quiet_at)) {
            candidate.revision = Math.max(candidate.revision, reply.revision)
            candidate.bookkeepingSeq = Math.max(candidate.bookkeepingSeq ?? candidate.seq, requestedSeq)
            candidate.nativeBookkeepingSeq = Math.max(nativeBookkeepingSeq, reply.bookkeeping_seq)
            candidate.quietAt = Math.max(candidate.quietAt ?? candidate.at, reply.quiet_at * 1000)
            candidate.at = candidate.quietAt
            candidate.state = 'pending'
            if (candidate.pendingQuietAt != null && reply.bookkeeping_seq >= requestedSeq) {
              candidate.pendingQuietAt = null
            }
            candidate.touchPending = candidate.bookkeepingSeq > candidate.nativeBookkeepingSeq
            if (!candidate.touchPending) return true
            continue
          }
        } catch { /* exact get below distinguishes a lost touch receipt from no state */ }
        try {
          const item = await this.getRecoveryItem(candidate, candidate.route)
          if (!item || !this.syncRecoveryItem(record, candidate, item)) return false
          if (item.bookkeeping_seq >= requestedSeq && item.quiet_at * 1000 >= requestedQuietAt) {
            candidate.touchPending = candidate.bookkeepingSeq > candidate.nativeBookkeepingSeq
            if (!candidate.touchPending) return true
            continue
          }
        } catch { /* keep bounded; no state is inferred from an unavailable read */ }
        return false
      }
      return false
    })()
    candidate.touchInFlight = operation
    let completed = false
    try {
      const result = await operation
      completed = result === true
      return result
    } finally {
      if (candidate.touchInFlight === operation) candidate.touchInFlight = null
      if (completed && !this.stopped && record.candidate === candidate && !candidate.touchPending) {
        const delay = Math.max(0, QUIET_MS - (this.now() - (candidate.quietAt ?? candidate.at)))
        this.schedule(record, delay)
      }
    }
  }

  async captureCompletionCandidate(record, candidate) {
    if (!this.recoveryCapable || this.stopped || !candidate?.recoveryGeneration || candidate.registering || candidate.recoveryId) return
    const generation = record.generation
    const capturedPolicyGeneration = candidate.recoveryGeneration
    const current = () => !this.stopped && generation === record.generation && record.candidate === candidate &&
      candidate.recoveryGeneration === capturedPolicyGeneration && this.recoveryPolicy?.enabled === true &&
      this.recoveryPolicy.generation === capturedPolicyGeneration
    if (!current()) return
    if (this.now() >= candidate.createdAt + COMPLETION_RECOVERY_TTL_MS) {
      void this.cancelRecoveryCandidate(record, candidate, 'ttl_expired')
      this.invalidate(record)
      return
    }
    candidate.registering = true
    try {
      const route = await this.routeFor(record)
      if (!current()) { await this.cancelRecoveryCandidate(record, {...candidate, route},
        candidate.recoveryGeneration === capturedPolicyGeneration ? 'new_work' : 'settings_disabled'); return }
      if (!this.currentLiveRoute(route)) throw new Error('Recovery route is not already live')
      const [active, replay] = await Promise.all([
        this.requestCurrentRoute(route, 'session.active_list', {}),
        this.requestCurrentRoute(route, 'session.events.since', {session_id:record.sid, last_seen:candidate.seq})
      ])
      if (!current()) { await this.cancelRecoveryCandidate(record, {...candidate, route},
        candidate.recoveryGeneration === capturedPolicyGeneration ? 'new_work' : 'settings_disabled'); return }
      if (!this.currentLiveRoute(route)) throw new Error('Recovery route became unavailable')
      const rows = validateActiveSessions(active)
      const live = rows.find(row => row.id === record.sid)
      if (!live) {
        await this.cancelRecoveryCandidate(record, {...candidate, route}, 'runtime_absent')
        if (current()) this.invalidate(record)
        return
      }
      validateRecoveryLiveRow(live)
      const epoch = validateRecoveryReplayIdentity(replay, candidate.seq)
      if (String(epoch) !== String(candidate.epoch || epoch)) throw new Error('Recovery epoch changed before registration')
      if (!this.currentLiveRoute(route)) throw new Error('Recovery route changed')
      candidate.route = route
      candidate.sessionKey = live.session_key
      candidate.startedAt = String(live.started_at)
      candidate.epoch = epoch
      const createdAt = candidate.createdAt / 1000
      const reply = await this.helper({action:'completion_recovery_record', candidate:{
        route:publicRecoveryRoute(route), runtime_id:record.sid, session_key:live.session_key,
        started_at:candidate.startedAt, epoch, seq:candidate.seq, final_row_id:candidate.finalId,
        created_at:createdAt, quiet_at:(candidate.pendingQuietAt ?? candidate.quietAt ?? candidate.at) / 1000,
        generation:capturedPolicyGeneration
      }})
      if (reply?.ok !== true || typeof reply.candidate_id !== 'string' || !/^[0-9a-f]{64}$/u.test(reply.candidate_id) ||
          !Number.isSafeInteger(reply.revision) || reply.generation !== capturedPolicyGeneration ||
          reply.seq !== candidate.seq || !Number.isSafeInteger(reply.bookkeeping_seq) || reply.bookkeeping_seq < candidate.seq ||
          !Number.isFinite(reply.created_at) || !Number.isFinite(reply.quiet_at) || !Number.isFinite(reply.expires_at)) {
        if (current()) {
          candidate.readBlocked = true
          this.schedule(record, Math.min(20_000, Math.max(1000, candidate.createdAt + COMPLETION_RECOVERY_TTL_MS - this.now())))
        } else await this.cancelRecoveryCandidate(record, {...candidate, route},
          candidate.recoveryGeneration === capturedPolicyGeneration ? 'new_work' : 'settings_disabled')
        return
      }
      const localBookkeepingSeq = candidate.bookkeepingSeq ?? candidate.seq
      candidate.recoveryId = reply.candidate_id
      candidate.revision = reply.revision
      candidate.state = reply.state
      candidate.bookkeepingSeq = Math.max(localBookkeepingSeq, candidate.seq)
      candidate.pendingBookkeepingSeq = Math.max(candidate.pendingBookkeepingSeq ?? candidate.seq, candidate.bookkeepingSeq)
      candidate.nativeBookkeepingSeq = reply.bookkeeping_seq
      candidate.quietAt = Math.max(candidate.quietAt ?? candidate.at, Number(reply.quiet_at) * 1000)
      candidate.at = candidate.quietAt
      if (candidate.pendingQuietAt != null && candidate.quietAt >= candidate.pendingQuietAt - 1) candidate.pendingQuietAt = null
      candidate.touchPending = candidate.bookkeepingSeq > candidate.nativeBookkeepingSeq
      candidate.expiresAt = Number(reply.expires_at) * 1000
      if (!current()) await this.cancelRecoveryCandidate(record, candidate,
        candidate.recoveryGeneration === capturedPolicyGeneration ? 'new_work' : 'settings_disabled')
    } catch {
      if (current() && this.now() < candidate.createdAt + COMPLETION_RECOVERY_TTL_MS) {
        candidate.readBlocked = true
        this.schedule(record, Math.min(20_000, Math.max(1000, candidate.createdAt + COMPLETION_RECOVERY_TTL_MS - this.now())))
      }
    } finally {
      candidate.registering = false
    }
  }

  async restoreCompletionCandidates() {
    if (this.stopped || !this.recoveryCapable || !this.recoveryPolicyAvailable ||
        this.recoveryPolicy?.enabled !== true || this.recoveryRestoreStarted) return
    this.recoveryRestoreStarted = true
    try {
      const routes = await this.refreshRoutes(true)
      const current = routes.filter(route => this.currentLiveRoute(route))
      if (current.length !== 1) return
      const route = current[0]
      const items = []
      let cursor = null
      let previousCursor = ''
      let lastId = ''
      let pages = 0
      while (pages < 256 && items.length < 256) {
        if (this.stopped || !this.currentLiveRoute(route) || !this.recoveryPolicyAvailable ||
            this.recoveryPolicy?.enabled !== true) return
        const reply = await this.helper({action:'completion_recovery_list', route:publicRecoveryRoute(route), cursor})
        pages++
        if (!exactKeys(reply, ['ok','version','items','cursor']) || reply.ok !== true || reply.version !== VERSION ||
            !Array.isArray(reply.items) || reply.items.length > 16 ||
            !(reply.cursor === null || (typeof reply.cursor === 'string' && /^[0-9a-f]{64}$/u.test(reply.cursor)))) return
        for (const item of reply.items) {
          if (!validateRecoveryPublicItem(item, route) || item.candidate_id <= lastId ||
              (previousCursor && item.candidate_id <= previousCursor)) return
          lastId = item.candidate_id
          if (RECOVERY_RESTORABLE_STATES.has(item.state) && item.generation === this.recoveryPolicy.generation) items.push(item)
        }
        if (reply.cursor === null) break
        if (reply.cursor <= previousCursor || (lastId && reply.cursor < lastId)) return
        previousCursor = reply.cursor
        cursor = reply.cursor
      }
      for (const item of items.slice(0, 256)) {
        if (this.stopped || !this.currentLiveRoute(route) || !this.recoveryPolicyAvailable ||
            this.recoveryPolicy?.enabled !== true || this.recoveryPolicy.generation !== item.generation) return
        const key = JSON.stringify([route.connectionId, route.profile, item.runtime_id])
        let record = this.records.get(key)
        if (!record) {
          record = {key, profile:route.profile, connectionId:route.connectionId, sid:item.runtime_id,
            generation:0, candidate:null, cancel:null, todos:null, seq:item.seq, settledFinals:new Set(),
            checking:false, retries:0, ask:null, askChecking:false}
          this.records.set(key, record)
        }
        if (record.candidate && (record.candidate.finalId !== item.final_row_id ||
            record.candidate.recoveryGeneration !== item.generation || record.candidate.seq !== item.seq)) continue
        if (!record.candidate) {
          record.candidate = {at:item.quiet_at*1000, quietAt:item.quiet_at*1000,
            createdAt:item.created_at*1000, expiresAt:item.expires_at*1000, finalId:item.final_row_id,
            runtimeId:item.runtime_id, seq:item.seq, bookkeepingSeq:item.seq,
            nativeBookkeepingSeq:item.bookkeeping_seq, pendingBookkeepingSeq:item.seq,
            recoveryGeneration:item.generation,
            recoveryId:item.candidate_id, revision:item.revision, state:item.state, route,
            sessionKey:item.session_key, startedAt:item.started_at, epoch:item.epoch,
            fresh:false, recovered:true}
          record.seq = Math.max(record.seq, item.seq)
          const delay = Math.max(0, QUIET_MS - (this.now() - record.candidate.quietAt))
          this.schedule(record, delay)
        } else if (!record.candidate.recoveryId && record.candidate.fresh) {
          const candidate = record.candidate
          candidate.recoveryId = item.candidate_id
          candidate.revision = item.revision
          candidate.state = item.state
          candidate.route = route
          candidate.sessionKey = item.session_key
          candidate.startedAt = item.started_at
          candidate.epoch = item.epoch
          candidate.nativeBookkeepingSeq = item.bookkeeping_seq
          candidate.pendingBookkeepingSeq = Math.max(candidate.pendingBookkeepingSeq ?? candidate.seq, candidate.bookkeepingSeq)
          candidate.touchPending = candidate.bookkeepingSeq > candidate.nativeBookkeepingSeq
          candidate.quietAt = Math.max(candidate.quietAt ?? candidate.at, item.quiet_at*1000)
          candidate.at = candidate.quietAt
          candidate.expiresAt = Math.min(candidate.expiresAt, item.expires_at*1000)
        }
      }
    } catch {
      // An unavailable route/list is unverifiable, not authoritative absence.
    } finally {
      this.recoveryRestoreStarted = false
    }
  }

  dropLocalCandidate(record, candidate, settled = false) {
    if (!record || record.candidate !== candidate) return
    record.generation++
    record.candidate = null
    record.cancel?.()
    record.cancel = null
    if (settled && Number.isSafeInteger(candidate.finalId)) record.settledFinals.add(candidate.finalId)
  }

  invalidate(record) {
    record.generation++
    const previous = record.candidate
    record.candidate = null
    record.cancel?.()
    record.cancel = null
    if (previous?.recoveryId || previous?.seq) void this.cancelRecoveryCandidate(record, previous, 'new_work')
  }

  schedule(record, delay = QUIET_MS) {
    record.cancel?.()
    record.cancel = this.ctx.setTimeout(() => { void this.check(record) }, delay)
  }

  onEvent(event) {
    if (this.stopped) return
    if (event?.type === 'gateway.ready') { void this.mounted(true); return }
    if (event?.type === 'approval.request' || event?.type === 'approval.settled') {
      if (typeof event.session_id === 'string' && event.session_id) return this.handleCanonicalApprovalEvent(event)
      return
    }
    if (!event || typeof event !== 'object' || event.replayed || !event.session_id) return
    const recoveryBookkeeping = ['session.info','status.update'].includes(event.type)
    const interesting = ['message.start', 'message.complete', 'tool.start', 'tool.complete', 'todo.updated',
      'subagent.start', 'subagent.complete', 'error', 'session.control.update', ...['session.info','status.update']]
    if (!interesting.includes(event.type)) return
    const profile = event.profile || this.api.state.profile.get()
    const connectionId = event.connectionId || ''
    const key = JSON.stringify([connectionId, profile, event.session_id])
    let r = this.records.get(key)
    if (!r && recoveryBookkeeping) return
    if (!r) {
      r = {key, profile, connectionId, sid:event.session_id, generation:0, candidate:null, cancel:null,
        todos:null, seq:0, settledFinals:new Set(), checking:false, retries:0, ask:null, askChecking:false}
      this.records.set(key, r)
    }

    if (Number.isInteger(event.seq)) {
      if (event.seq <= r.seq) return
      r.seq = event.seq
    }
    const p = event.payload || {}
    if (event.type === 'todo.updated' && Array.isArray(p.todos)) r.todos = p.todos
    if (event.type === 'tool.complete' && Array.isArray(p.todos)) r.todos = p.todos
    if (event.type === 'message.complete') {
      // Watch-mirrored children (no status), interrupted/error/partial replies,
      // and non-persisted output never qualify as a completed parent response.
      const finalId = p.persisted_turn?.final_assistant_row_id
      if (r.settledFinals.has(finalId)) return
      if (r.candidate?.finalId === finalId) return
      this.invalidate(r)
      if (p.status !== 'complete' || p.error || p.failure_reason || p.partial || !Number.isSafeInteger(finalId) || finalId < 1) return
      const activeAtCompletion = this.currentEventIsActiveLocal(profile, connectionId)
      const recoveryEnabled = this.recoveryCapable && this.recoveryPolicyAvailable &&
        this.recoveryPolicy?.ok === true && this.recoveryPolicy.enabled === true && activeAtCompletion &&
        Number.isSafeInteger(event.seq) && event.seq > 0
      const recoveryGeneration = recoveryEnabled ? this.recoveryPolicy.generation : null
      const attentionOnly = this.recoveryCapable && this.recoveryPolicyAvailable &&
        this.recoveryPolicy?.ok === true && this.recoveryPolicy.enabled === false && activeAtCompletion &&
        Number.isSafeInteger(event.seq) && event.seq > 0
      const at = this.now()
      r.candidate = {at, quietAt:at, createdAt:at, expiresAt:at+COMPLETION_RECOVERY_TTL_MS,
        finalId, seq:Number.isSafeInteger(event.seq) && event.seq > 0 ? event.seq : 0,
        runtimeId:r.sid, bookkeepingSeq:Number.isSafeInteger(event.seq) && event.seq > 0 ? event.seq : 0,
        recoveryGeneration, recoveryRetired:attentionOnly, ordinaryOnly:attentionOnly,
        route:null, recoveryId:null, revision:null, state:attentionOnly?'retired':'registering', fresh:true, recovered:false}
      r.retries = 0
      this.schedule(r)
      if (recoveryGeneration) void this.captureCompletionCandidate(r, r.candidate)
      return
    }
    if (event.type === 'session.control.update') {
      const control = p.control ?? event.control
      let done = false
      try { done = validateRecoveryControl(control) } catch { /* malformed live control is never bookkeeping */ }
      if (!done) { this.invalidate(r); return }
      if (r.candidate) {
        if (r.candidate.recoveryGeneration) this.observeRecoveryBookkeeping(r, r.candidate, event.seq)
        else {
          r.candidate.at = this.now()
          r.candidate.quietAt = r.candidate.at
          this.schedule(r)
        }
      }
      return
    }
    if (event.type === 'session.info' || event.type === 'status.update') {
      const candidate = r.candidate
      if (!candidate?.recoveryGeneration) return
      if (event.type === 'session.info') {
        if (p.running === true) { this.invalidate(r); return }
        if (p.running !== false || typeof p.stored_session_id !== 'string' ||
            !candidate.sessionKey || p.stored_session_id !== candidate.sessionKey ||
            typeof p.profile_name !== 'string' || !p.profile_name.trim()) return
      } else if (!exactKeys(p, ['kind','text']) || !['goal','loop'].includes(p.kind) ||
          typeof p.text !== 'string' || !p.text.trim()) return
      this.observeRecoveryBookkeeping(r, candidate, event.seq)
      return
    }
    this.invalidate(r)
  }

  observeRecoveryBookkeeping(record, candidate, seq) {
    if (!Number.isSafeInteger(seq) || seq <= (candidate.bookkeepingSeq ?? candidate.seq)) return
    candidate.bookkeepingSeq = seq
    candidate.pendingBookkeepingSeq = Math.max(candidate.pendingBookkeepingSeq ?? candidate.seq, seq)
    if (seq <= (candidate.nativeBookkeepingSeq ?? candidate.seq)) {
      candidate.pendingQuietAt = null
      candidate.touchPending = candidate.bookkeepingSeq > (candidate.nativeBookkeepingSeq ?? candidate.seq)
      const delay = Math.max(0, QUIET_MS - (this.now() - (candidate.quietAt ?? candidate.at)))
      this.schedule(record, delay)
      return
    }
    candidate.pendingQuietAt = Math.max(candidate.pendingQuietAt ?? candidate.quietAt ?? candidate.at, this.now())
    candidate.touchPending = true
    this.schedule(record, QUIET_MS)
    if (candidate.recoveryId) void this.touchRecoveryCandidate(record, candidate, seq)
  }

  async recoverySnapshot(record, candidate, route, current) {
    if (!this.currentLiveRoute(route)) return {unavailable:true}
    const request = (method, params={}) => this.requestCurrentRoute(route, method,
      method === 'session.active_list' ? params : {session_id:record.sid, ...params})
    const [active, children, processes, controls, replay] = await Promise.all([
      this.requestCurrentRoute(route, 'session.active_list', {}),
      request('subagent.list'), request('process.list'), request('session.control.read'),
      request('session.events.since', {last_seen:candidate.seq})
    ])
    if (!current()) return {stale:true}
    if (!this.currentLiveRoute(route)) return {unavailable:true}
    const rows = validateActiveSessions(active)
    const live = rows.find(row => row.id === record.sid)
    if (!live) return {terminal:'runtime_absent'}
    validateRecoveryLiveRow(live)
    if (live.session_key !== candidate.sessionKey || String(live.started_at) !== candidate.startedAt) {
      return {terminal:'identity_changed'}
    }
    if (!['idle','waiting'].includes(live.status)) return {terminal:'new_work'}
    if (validateRecoveryChildren(children) || validateRecoveryProcesses(processes) ||
        validateRecoveryControl(controls?.control) === false ||
        (record.todos && record.todos.some(todo => !todo || !['completed','cancelled'].includes(todo.status)))) {
      return {terminal:'new_work'}
    }
    const epoch = validateRecoveryReplayIdentity(replay, candidate.seq)
    if (String(epoch) !== String(candidate.epoch)) return {terminal:'identity_changed'}
    const replayResult = validateRecoveryReplay(replay, candidate)
    if (replayResult.identityChanged) return {terminal:'identity_changed'}
    if (replayResult.work) return {terminal:'new_work'}
    if (replayResult.doneBookkeepingSeq > (candidate.bookkeepingSeq ?? candidate.seq)) {
      candidate.bookkeepingSeq = replayResult.doneBookkeepingSeq
      candidate.pendingBookkeepingSeq = Math.max(candidate.pendingBookkeepingSeq ?? candidate.seq, replayResult.doneBookkeepingSeq)
      if (candidate.recoveryRetired && !candidate.recoveryId) {
        if (replayResult.doneBookkeepingSeq > (candidate.nativeBookkeepingSeq ?? candidate.seq)) {
          candidate.quietAt = Math.max(candidate.quietAt ?? candidate.at, this.now())
          candidate.at = candidate.quietAt
          this.schedule(record, QUIET_MS)
          return {quiet:true}
        }
        candidate.pendingQuietAt = null
        candidate.touchPending = false
      } else if (replayResult.doneBookkeepingSeq > (candidate.nativeBookkeepingSeq ?? candidate.seq)) {
        candidate.pendingQuietAt = Math.max(candidate.pendingQuietAt ?? candidate.quietAt ?? candidate.at, this.now())
        candidate.touchPending = true
        const touched = await this.touchRecoveryCandidate(record, candidate, replayResult.doneBookkeepingSeq)
        if (!current()) return {stale:true}
        if (!touched) return {unavailable:true, touchPending:true}
        this.schedule(record, Math.max(0, QUIET_MS - (this.now() - (candidate.quietAt ?? candidate.at))))
        return {quiet:true}
      }
      candidate.pendingQuietAt = null
      candidate.touchPending = false
    }
    if (candidate.touchPending) {
      if (!candidate.recoveryId && candidate.recoveryRetired) {
        candidate.touchPending = false
        this.schedule(record, QUIET_MS)
        return {quiet:true}
      }
      const touched = await this.touchRecoveryCandidate(record, candidate, candidate.bookkeepingSeq)
      if (!current()) return {stale:true}
      if (!touched) return {unavailable:true, touchPending:true}
      this.schedule(record, QUIET_MS)
      return {quiet:true}
    }
    if (this.now() - (candidate.quietAt ?? candidate.at) < QUIET_MS) {
      this.schedule(record, Math.max(1000, QUIET_MS - (this.now() - (candidate.quietAt ?? candidate.at))))
      return {quiet:true}
    }
    return {ok:true, live, children, processes, controls, replay, replayResult}
  }

  scheduleRecoveryRetry(record, candidate, delay=5000) {
    const expiry = candidate.expiresAt || candidate.createdAt + COMPLETION_RECOVERY_TTL_MS
    if (this.now() >= expiry) {
      if (!candidate.unknownOutcome && !candidate.recoveryRetired) {
        void this.cancelRecoveryCandidate(record, candidate, 'ttl_expired')
      }
      if (record.candidate === candidate) this.dropLocalCandidate(record, candidate, true)
      return
    }
    this.schedule(record, Math.min(Math.max(1000, delay), Math.max(1000, expiry - this.now())))
  }

  async reconcileRecoveryState(record, candidate, route, {unknownOutcome=false, delay=5000}={}) {
    if (!this.currentLiveRoute(route)) {
      if (unknownOutcome) candidate.unknownOutcome = true
      this.scheduleRecoveryRetry(record, candidate, delay)
      return
    }
    try {
      const item = await this.getRecoveryItem(candidate, route)
      if (record.candidate !== candidate) return
      if (item === null) {
        if (unknownOutcome) {
          candidate.unknownOutcome = true
          candidate.state = 'outcome_unknown'
          this.scheduleRecoveryRetry(record, candidate, delay)
        } else this.dropLocalCandidate(record, candidate, true)
        return
      }
      if (!this.syncRecoveryItem(record, candidate, item)) {
        if (unknownOutcome) {
          candidate.unknownOutcome = true
          candidate.state = 'outcome_unknown'
          this.scheduleRecoveryRetry(record, candidate, delay)
        } else this.dropLocalCandidate(record, candidate, true)
        return
      }
      if (item.state === 'dispatching' || RECOVERY_TERMINAL_STATES.has(item.state) ||
          this.now() >= item.expires_at * 1000) {
        this.dropLocalCandidate(record, candidate, true)
        return
      }
      if (unknownOutcome && RECOVERY_RESTORABLE_STATES.has(item.state)) candidate.unknownOutcome = false
      const nativeLeaseUntil = Number.isFinite(item.claim_until) ? item.claim_until * 1000 : candidate.claimUntil
      const leaseDelay = Number.isFinite(nativeLeaseUntil) ? Math.max(1000, nativeLeaseUntil - this.now()) : delay
      const quietDelay = Math.max(1000, QUIET_MS - (this.now() - (candidate.quietAt ?? candidate.at)))
      this.scheduleRecoveryRetry(record, candidate,
        Math.max(delay, item.state === 'claimed' ? leaseDelay : 0, quietDelay))
    } catch {
      if (unknownOutcome) {
        candidate.unknownOutcome = true
        candidate.state = 'outcome_unknown'
      }
      this.scheduleRecoveryRetry(record, candidate, delay)
    }
  }

  async handoffRecoveryAttention(record, candidate, route, current) {
    const recoveryIdentity = {...candidate, route:candidate.route || route}
    if (candidate.recoveryId) await this.cancelRecoveryCandidate(record, recoveryIdentity, 'new_work')
    if (!current()) return
    candidate.recoveryGeneration = null
    candidate.recoveryRetired = true
    candidate.ordinaryOnly = false
    candidate.recoveryId = null
    candidate.revision = null
    candidate.state = 'attention_handoff'
    candidate.touchPending = false
    let fresh
    try { fresh = await this.recoverySnapshot(record, candidate, route, current) }
    catch { this.scheduleRecoveryRetry(record, candidate, 5000); return }
    if (!current() || fresh.stale) return
    if (fresh.unavailable || fresh.quiet) { this.scheduleRecoveryRetry(record, candidate, 5000); return }
    if (fresh.terminal) {
      if (fresh.terminal !== 'ttl_expired') await this.cancelRecoveryCandidate(record, recoveryIdentity, fresh.terminal)
      if (current()) this.dropLocalCandidate(record, candidate, true)
      return
    }
    if (!fresh.ok || (fresh.live.status !== 'idle' && fresh.live.status !== 'waiting') ||
        fresh.replayResult.openRequests.some(request => !HUMAN_ASK_METHODS.has(request.method))) {
      this.scheduleRecoveryRetry(record, candidate, 5000)
      return
    }
    const followup = route.connectionId && route.profile && route.targetProfile && fresh.live.session_key &&
      fresh.live.started_at != null && fresh.replay.epoch != null
      ? {owner:this.replyOwner, connectionId:route.connectionId, profile:route.profile,
        targetProfile:route.targetProfile, sid:record.sid, session_key:fresh.live.session_key,
        epoch:String(fresh.replay.epoch), started_at:String(fresh.live.started_at), final_row_id:candidate.finalId}
      : null
    const payload = {action:'send', profile:route.targetProfile, session_key:fresh.live.session_key,
      final_row_id:candidate.finalId, desktop_observed:true}
    if (followup) payload.followup = followup
    candidate.sendAttempted = true
    let reply
    try { reply = await this.helper(payload) }
    catch {
      if (current()) {
        this.dropLocalCandidate(record, candidate, true)
        this.api.notify({kind:'warning', title:'세션 완료 알림',
          message:'전송 결과를 확인하지 못했습니다. 중복 방지를 위해 자동 재전송하지 않습니다.'})
      }
      return
    }
    if (!current()) return
    if (reply?.ok === true || reply?.duplicate === true) {
      this.dropLocalCandidate(record, candidate, true)
      return
    }
    this.dropLocalCandidate(record, candidate, true)
    if (reply?.status === 'unknown' || reply?.status === 'failed') {
      this.api.notify({kind:'warning', title:'세션 완료 알림',
        message:reply.status === 'failed' ? '텔레그램이 알림을 거절했습니다. 자동 재전송하지 않습니다.' :
          '전송 결과를 확인하지 못했습니다. 중복 방지를 위해 자동 재전송하지 않습니다.'})
    }
  }

  async checkRetiredCandidate(record, candidate, route, current) {
    const expireLocally = () => {
      if (record.candidate === candidate) this.dropLocalCandidate(record, candidate, true)
    }
    if (candidate.unknownOutcome) { expireLocally(); return }
    const expiry = candidate.expiresAt || candidate.createdAt + COMPLETION_RECOVERY_TTL_MS
    if (this.now() >= expiry) { expireLocally(); return }
    if (!this.currentLiveRoute(route)) { this.scheduleRecoveryRetry(record, candidate, 5000); return }
    if (candidate.route && (candidate.route.connectionId !== route.connectionId ||
        candidate.route.profile !== route.profile || candidate.route.targetProfile !== route.targetProfile)) {
      expireLocally()
      return
    }
    try {
      const request = (method, params={}) => this.requestCurrentRoute(route, method,
        method === 'session.active_list' ? params : {session_id:record.sid, ...params})
      const [active, children, processes, controls, replay] = await Promise.all([
        this.requestCurrentRoute(route, 'session.active_list', {}), request('subagent.list'), request('process.list'),
        request('session.control.read'), request('session.events.since', {last_seen:Math.max(0, candidate.seq - 1)})
      ])
      if (!current()) return
      if (!this.currentLiveRoute(route)) { this.scheduleRecoveryRetry(record, candidate, 5000); return }
      const rows = validateActiveSessions(active)
      const live = rows.find(row => row.id === record.sid)
      if (!live) { expireLocally(); return }
      validateRecoveryLiveRow(live)
      if ((candidate.sessionKey && live.session_key !== candidate.sessionKey) ||
          (candidate.startedAt && String(live.started_at) !== candidate.startedAt) ||
          !['idle','waiting'].includes(live.status) || validateRecoveryChildren(children) ||
          validateRecoveryProcesses(processes) || validateRecoveryControl(controls?.control) === false ||
          (record.todos && record.todos.some(todo => !todo || !['completed','cancelled'].includes(todo.status)))) {
        expireLocally()
        return
      }
      const epoch = validateRecoveryReplayIdentity(replay, Math.max(0, candidate.seq - 1))
      if (replay.truncated !== false || (candidate.epoch && String(epoch) !== String(candidate.epoch)) ||
          !Array.isArray(replay.events) || !replay.events.length) {
        this.scheduleRecoveryRetry(record, candidate, 5000)
        return
      }
      let expected = candidate.seq
      for (const item of replay.events) {
        if (!item || item.session_id !== record.sid || !Number.isSafeInteger(item.seq) || item.seq !== expected++) {
          this.scheduleRecoveryRetry(record, candidate, 5000)
          return
        }
      }
      if (expected - 1 !== replay.latest_seq) { this.scheduleRecoveryRetry(record, candidate, 5000); return }
      const first = replay.events[0]
      const firstPayload = first?.payload
      if (first?.seq !== candidate.seq || first.type !== 'message.complete' ||
          firstPayload?.status !== 'complete' || firstPayload.error || firstPayload.failure_reason || firstPayload.partial ||
          firstPayload.persisted_turn?.final_assistant_row_id !== candidate.finalId) {
        expireLocally()
        return
      }
      candidate.route = route
      candidate.runtimeId = record.sid
      candidate.sessionKey = live.session_key
      candidate.startedAt = String(live.started_at)
      candidate.epoch = String(epoch)
      const tail = {...replay, events:replay.events.slice(1)}
      const replayResult = validateRecoveryReplay(tail, candidate)
      if (replayResult.identityChanged || replayResult.work) { expireLocally(); return }
      if (replayResult.doneBookkeepingSeq > (candidate.bookkeepingSeq ?? candidate.seq)) {
        candidate.bookkeepingSeq = replayResult.doneBookkeepingSeq
        candidate.pendingBookkeepingSeq = Math.max(candidate.pendingBookkeepingSeq ?? candidate.seq,
          replayResult.doneBookkeepingSeq)
        if (replayResult.doneBookkeepingSeq > (candidate.nativeBookkeepingSeq ?? candidate.seq)) {
          candidate.quietAt = Math.max(candidate.quietAt ?? candidate.at, this.now())
          candidate.at = candidate.quietAt
        }
        this.schedule(record, Math.max(0, QUIET_MS - (this.now() - (candidate.quietAt ?? candidate.at))))
        return
      }
      if (this.now() - (candidate.quietAt ?? candidate.at) < QUIET_MS) {
        this.schedule(record, Math.max(1000, QUIET_MS - (this.now() - (candidate.quietAt ?? candidate.at))))
        return
      }
      const classified = await this.helper({action:'completion_recovery_classify', route:publicRecoveryRoute(route),
        profile:route.targetProfile, session_key:live.session_key, final_row_id:candidate.finalId,
        desktop_observed:true})
      if (!current()) return
      if (classified?.ok === true && classified.kind === 'attention') {
        await this.handoffRecoveryAttention(record, candidate, route, current)
        return
      }
      if (classified?.ok === true && classified.kind === 'completion' && candidate.ordinaryOnly &&
          live.status === 'idle' && replayResult.openRequests.length === 0) {
        await this.handoffRecoveryAttention(record, candidate, route, current)
        return
      }
      if (classified?.ok === true && ['completion','disabled'].includes(classified.kind)) {
        expireLocally()
        return
      }
      this.scheduleRecoveryRetry(record, candidate, 5000)
    } catch {
      if (current()) this.scheduleRecoveryRetry(record, candidate, 5000)
    }
  }

  async checkRecovery(record, candidate, route, current) {
    const cancel = async (reason='new_work') => {
      await this.cancelRecoveryCandidate(record, candidate, reason)
      if (current()) this.dropLocalCandidate(record, candidate, true)
    }
    if (this.now() >= (candidate.expiresAt || candidate.createdAt + COMPLETION_RECOVERY_TTL_MS)) {
      await cancel('ttl_expired')
      return
    }
    if (!this.currentLiveRoute(route)) {
      this.scheduleRecoveryRetry(record, candidate, 5000)
      return
    }
    if (candidate.route && (candidate.route.connectionId !== route.connectionId ||
        candidate.route.profile !== route.profile || candidate.route.targetProfile !== route.targetProfile)) {
      await cancel('identity_changed')
      return
    }
    if (!candidate.recoveryId) {
      void this.captureCompletionCandidate(record, candidate)
      this.scheduleRecoveryRetry(record, candidate, 5000)
      return
    }
    if (candidate.unknownOutcome) {
      await this.reconcileRecoveryState(record, candidate, route, {unknownOutcome:true, delay:5000})
      return
    }
    if (candidate.touchPending) {
      const touched = await this.touchRecoveryCandidate(record, candidate, candidate.bookkeepingSeq)
      if (!touched) this.scheduleRecoveryRetry(record, candidate, 5000)
      return
    }
    if (!this.recoveryPolicyAvailable || this.recoveryPolicy?.enabled !== true ||
        this.recoveryPolicy.generation !== candidate.recoveryGeneration) {
      this.scheduleRecoveryRetry(record, candidate, 5000)
      return
    }

    try {
      let snapshot = await this.recoverySnapshot(record, candidate, route, current)
      if (!current() || snapshot.stale) return
      if (snapshot.unavailable || snapshot.quiet) { this.scheduleRecoveryRetry(record, candidate, 5000); return }
      if (snapshot.terminal) { await cancel(snapshot.terminal); return }
      if (!snapshot.ok) { this.scheduleRecoveryRetry(record, candidate, 5000); return }

      const claimBookkeepingSeq = candidate.bookkeepingSeq
      const claimNativeBookkeepingSeq = candidate.nativeBookkeepingSeq ?? candidate.seq
      const claimQuietAt = candidate.quietAt ?? candidate.at
      const claimRevision = candidate.revision
      const claimed = await this.helper({action:'completion_recovery_claim',
        candidate:{candidate_id:candidate.recoveryId, revision:candidate.revision,
          generation:candidate.recoveryGeneration}, route:publicRecoveryRoute(route)})
      if (!current()) return
      if (claimed?.ok !== true || typeof claimed.claim_token !== 'string' || !/^[0-9a-f]{32}$/u.test(claimed.claim_token) ||
          !Number.isSafeInteger(claimed.revision) || claimed.candidate_id !== candidate.recoveryId ||
          claimed.generation !== candidate.recoveryGeneration || !Number.isFinite(claimed.claim_until) || !Number.isSafeInteger(claimed.bookkeeping_seq) ||
          !Number.isFinite(claimed.quiet_at)) {
        const wait = Number.isFinite(claimed?.retry_at) ? Math.max(1000, claimed.retry_at * 1000 - this.now()) : 5000
        await this.reconcileRecoveryState(record, candidate, route, {delay:wait})
        return
      }
      candidate.claimUntil = claimed.claim_until * 1000
      if (candidate.bookkeepingSeq !== claimBookkeepingSeq || (candidate.quietAt ?? candidate.at) !== claimQuietAt ||
          candidate.revision !== claimRevision || claimed.bookkeeping_seq !== claimNativeBookkeepingSeq ||
          claimed.quiet_at * 1000 > claimQuietAt + 1 || this.now() - claimed.quiet_at * 1000 < QUIET_MS) {
        await this.reconcileRecoveryState(record, candidate, route, {delay:5000})
        return
      }
      candidate.revision = claimed.revision
      candidate.nativeBookkeepingSeq = claimed.bookkeeping_seq
      candidate.quietAt = Math.max(candidate.quietAt ?? candidate.at, claimed.quiet_at * 1000)
      candidate.at = candidate.quietAt
      candidate.state = 'claimed'
      const policy = await this.refreshRecoveryPolicy()
      if (!current()) return
      if (!policy) { this.scheduleRecoveryRetry(record, candidate, 5000); return }
      if (!candidate.recoveryGeneration) return // observed OFF/change retired this admission
      if (!policy.enabled || policy.generation !== candidate.recoveryGeneration) {
        this.scheduleRecoveryRetry(record, candidate, 5000)
        return
      }
      if (candidate.bookkeepingSeq !== claimBookkeepingSeq || (candidate.quietAt ?? candidate.at) !== claimQuietAt ||
          candidate.revision !== claimed.revision || this.now() - claimQuietAt < QUIET_MS) {
        await this.reconcileRecoveryState(record, candidate, route, {delay:5000})
        return
      }

      snapshot = await this.recoverySnapshot(record, candidate, route, current)
      if (!current() || snapshot.stale) return
      if (snapshot.unavailable || snapshot.quiet) { this.scheduleRecoveryRetry(record, candidate, 5000); return }
      if (snapshot.terminal) { await cancel(snapshot.terminal); return }
      if (!snapshot.ok) { this.scheduleRecoveryRetry(record, candidate, 5000); return }
      const classifyPayload = {action:'completion_recovery_classify', route:publicRecoveryRoute(route),
        profile:route.targetProfile, session_key:snapshot.live.session_key,
        final_row_id:candidate.finalId, desktop_observed:true}
      const classified = await this.helper(classifyPayload)
      if (!current()) return
      if (classified?.ok !== true || !['completion','attention','disabled'].includes(classified.kind)) {
        await this.reconcileRecoveryState(record, candidate, route, {delay:5000})
        return
      }
      if (classified.kind === 'attention') {
        await this.handoffRecoveryAttention(record, candidate, route, current)
        return
      }
      if (classified.kind === 'disabled') {
        await cancel('settings_disabled')
        return
      }
      if (snapshot.live.status !== 'idle' || snapshot.replayResult.openRequests.length) {
        await cancel('new_work')
        return
      }

      const finalPolicy = await this.refreshRecoveryPolicy()
      if (!current()) return
      if (!finalPolicy) { this.scheduleRecoveryRetry(record, candidate, 5000); return }
      if (!candidate.recoveryGeneration) return
      if (!finalPolicy.enabled || finalPolicy.generation !== candidate.recoveryGeneration) {
        this.scheduleRecoveryRetry(record, candidate, 5000)
        return
      }
      const finalSnapshot = await this.recoverySnapshot(record, candidate, route, current)
      if (!current() || finalSnapshot.stale) return
      if (finalSnapshot.unavailable || finalSnapshot.quiet) { this.scheduleRecoveryRetry(record, candidate, 5000); return }
      if (finalSnapshot.terminal) { await cancel(finalSnapshot.terminal); return }
      if (!finalSnapshot.ok || finalSnapshot.live.status !== 'idle' || finalSnapshot.replayResult.openRequests.length) {
        if (finalSnapshot.ok && finalSnapshot.live.status === 'waiting') {
          const secondClassify = await this.helper({...classifyPayload})
          if (secondClassify?.ok === true && secondClassify.kind === 'attention') {
            await this.handoffRecoveryAttention(record, candidate, route, current)
            return
          }
        }
        await cancel('new_work')
        return
      }
      if (!this.recoveryPolicyAvailable || this.recoveryPolicy?.enabled !== true ||
          this.recoveryPolicy.generation !== candidate.recoveryGeneration ||
          this.now() - (candidate.quietAt ?? candidate.at) < QUIET_MS || candidate.touchPending) {
        this.scheduleRecoveryRetry(record, candidate, 5000)
        return
      }
      const fresh = finalSnapshot.live
      const followup = route.connectionId && route.profile && route.targetProfile && fresh.session_key &&
        fresh.started_at != null && finalSnapshot.replay.epoch != null
        ? {owner:this.replyOwner, connectionId:route.connectionId, profile:route.profile,
          targetProfile:route.targetProfile, sid:record.sid, session_key:fresh.session_key,
          epoch:String(finalSnapshot.replay.epoch), started_at:String(fresh.started_at), final_row_id:candidate.finalId}
        : null
      const payload = {action:'completion_recovery_send',
        candidate:{candidate_id:candidate.recoveryId, revision:claimed.revision,
          claim_token:claimed.claim_token, generation:candidate.recoveryGeneration},
        route:publicRecoveryRoute(route), profile:route.targetProfile, session_key:fresh.session_key,
        final_row_id:candidate.finalId, runtime_id:record.sid, started_at:String(fresh.started_at),
        epoch:String(finalSnapshot.replay.epoch), desktop_observed:true}
      if (followup) payload.followup = followup
      candidate.sendAttempted = true
      let reply
      try { reply = await this.helper(payload) }
      catch {
        await this.reconcileRecoveryState(record, candidate, route, {unknownOutcome:true, delay:5000})
        return
      }
      if (!current()) return
      if (reply?.phase === 'pre_dispatch' && reply.retryable === true) {
        await this.reconcileRecoveryState(record, candidate, route, {delay:5000})
        return
      }
      if (reply?.phase === 'pre_dispatch' && reply.retryable === false && reply.reason === 'candidate_expired') {
        this.dropLocalCandidate(record, candidate, true)
        return
      }
      if (reply?.phase === 'pre_dispatch' && reply.retryable === false && reply.reason === 'attention_required') {
        await this.handoffRecoveryAttention(record, candidate, route, current)
        return
      }
      if (reply?.reason === 'quiet_period') {
        await this.reconcileRecoveryState(record, candidate, route, {delay:10_000})
        return
      }
      if (reply?.ok === true && reply.status === 'sent' || reply?.duplicate === true && reply.status === 'sent') {
        this.dropLocalCandidate(record, candidate, true)
        return
      }
      if (reply?.status === 'unknown' || reply?.status === 'failed') {
        this.dropLocalCandidate(record, candidate, true)
        if (reply.status === 'unknown' || reply.status === 'failed') {
          this.api.notify({kind:'warning', title:'세션 완료 알림',
            message:reply.status === 'failed' ? '텔레그램이 알림을 거절했습니다. 자동 재전송하지 않습니다.' :
              '전송 결과를 확인하지 못했습니다. 중복 방지를 위해 자동 재전송하지 않습니다.'})
        }
        return
      }
      await this.reconcileRecoveryState(record, candidate, route, {unknownOutcome:true, delay:5000})
    } catch {
      if (!current()) return
      if (!this.currentLiveRoute(route)) {
        this.scheduleRecoveryRetry(record, candidate, 5000)
        return
      }
      this.scheduleRecoveryRetry(record, candidate, 5000)
    }
  }

  async check(r) {
    if (this.stopped || !r.candidate || r.checking) return
    const generation = r.generation
    const candidate = r.candidate
    const quietAt = candidate.quietAt ?? candidate.at
    if (this.now() - quietAt < QUIET_MS) { this.schedule(r, QUIET_MS - (this.now() - quietAt)); return }
    const current = () => !this.stopped && generation === r.generation && r.candidate === candidate
    r.checking = true
    try {
      const route = await this.routeFor(r)
      if (candidate.recoveryRetired) {
        await this.checkRetiredCandidate(r, candidate, route, current)
        return
      }
      if (candidate.recoveryGeneration && this.recoveryCapable) {
        const policy = await this.refreshRecoveryPolicy()
        if (!current()) return
        if (candidate.recoveryRetired) {
          await this.checkRetiredCandidate(r, candidate, route, current)
          return
        }
        if (!policy) { this.scheduleRecoveryRetry(r, candidate, 5000); return }
        if (candidate.recoveryGeneration) {
          if (!policy.enabled || policy.generation !== candidate.recoveryGeneration) {
            this.scheduleRecoveryRetry(r, candidate, 5000)
            return
          }
          await this.checkRecovery(r, candidate, route, current)
          if (!current()) return
          if (candidate.recoveryGeneration) return
          if (candidate.recoveryRetired) {
            await this.checkRetiredCandidate(r, candidate, route, current)
            return
          }
          // A retired recovery admission may hand off only a freshly gated
          // native attention result; ordinary completions are not re-admitted.
        }
      }
      const request = (method, params = {}) => this.api.requestProfile(route, method, {session_id: r.sid, ...params})
      const [active, children, processes, controls, replay] = await Promise.all([
        this.api.requestProfile(route, 'session.active_list', {}),
        request('subagent.list'), request('process.list'), request('session.control.read'),
        request('session.events.since', {last_seen: candidate.seq})
      ])
      if (!current()) return
      const live = active.sessions?.find(s => s.id === r.sid)
      if (!live || live.status !== 'idle') { this.invalidate(r); return }
      // Unknown shapes / missed events fail closed, never silently bypass a gate.
      if (!Array.isArray(children.subagents) || !Array.isArray(processes.processes) || !Array.isArray(replay.open_requests) || !Array.isArray(replay.events) || replay.truncated) throw new Error('Incomplete completion snapshot')
      if (children.subagents.some(s => !['completed', 'failed', 'interrupted', 'cancelled'].includes(s.status)) ||
          (Array.isArray(children.delegations) && children.delegations.some(d => !['completed', 'delivered'].includes(d.status || d.state))) ||
          processes.processes.some(blockingProcess) || replay.open_requests.length || unfinishedControl(controls.control) ||
          (r.todos && r.todos.some(t => !['completed', 'cancelled'].includes(t.status)))) {
        this.invalidate(r); return
      }
      // Backend replay may know of work the renderer missed while reconnecting.
      const workEvents = new Set(['message.start','message.delta','message.interim','message.complete','reasoning.delta',
        'thinking.delta','tool.start','tool.delta','tool.complete','tool.generating','todo.updated','subagent.start','subagent.complete','error'])
      if (replay.events.some(e => workEvents.has(e.type) && (e.seq || 0) > candidate.seq)) { this.invalidate(r); return }
      // A second idle read narrows races with queued prompt drain. The sender
      // then independently checks the durable final row, leases and todo state.
      const fresh = await this.api.requestProfile(route, 'session.active_list', {})
      const freshLive = fresh.sessions?.find(s => s.id === r.sid)
      if (!current() || freshLive?.status !== 'idle') return
      const followup = route.connectionId && route.profile && route.targetProfile && live.session_key &&
        live.started_at != null && replay.epoch != null && freshLive.session_key === live.session_key &&
        String(freshLive.started_at) === String(live.started_at)
        ? {owner: this.replyOwner, connectionId: route.connectionId, profile: route.profile,
          targetProfile: route.targetProfile, sid: r.sid, session_key: live.session_key,
          epoch: String(replay.epoch), started_at: String(live.started_at), final_row_id: candidate.finalId}
        : null
      const send = {action: 'send', profile: route.targetProfile, session_key: live.session_key,
        final_row_id: candidate.finalId, desktop_observed: true}
      if (followup) send.followup = followup
      const reply = await this.helper(send)
      // Settle only this final; a follow-up can start while send is in flight.
      if (reply.ok || reply.duplicate || ['failed', 'unknown'].includes(reply.status)) r.settledFinals.add(candidate.finalId)
      if (!current()) return
      if (reply.ok || reply.duplicate) {
        this.invalidate(r)
      } else if (reply.reason === 'quiet_period') {
        this.schedule(r, 10_000)
      } else {
        this.invalidate(r)
        if (reply.status === 'failed' || reply.status === 'unknown') this.api.notify({kind: 'warning', title: '세션 완료 알림', message: '텔레그램 전송을 확인하지 못했습니다. 중복 방지를 위해 자동 재전송하지 않습니다.'})
      }
    } catch (error) {
      if (current() && error instanceof NativeHelperError && error.ambiguousWrite) {
        r.settledFinals.add(candidate.finalId)
        this.invalidate(r)
        this.api.notify({kind: 'warning', title: '세션 완료 알림', message: '전송 결과를 확인하지 못했습니다. 중복 방지를 위해 다시 보내지 않습니다.'})
      } else if (current() && candidate.recoveryGeneration) {
        this.scheduleRecoveryRetry(r, candidate, 5000)
      } else if (current() && ++r.retries <= 3) this.schedule(r, 20_000)
      else if (current()) {
        this.invalidate(r)
        this.api.notify({kind: 'warning', title: '세션 완료 알림', message: '완료 상태를 확인하지 못해 알림을 보내지 않았습니다.'})
      }
    } finally {
      r.checking = false
      if (!this.stopped && r.candidate && r.candidate !== candidate) {
        const pendingQuietAt = r.candidate.quietAt ?? r.candidate.at
        this.schedule(r, Math.max(0, QUIET_MS - (this.now() - pendingQuietAt)))
      }
    }
  }

  async followupSnapshot(descriptor) {
    if (this.stopped || !descriptor || descriptor.profile !== descriptor.targetProfile) return null
    const routes = await this.refreshRoutes(true)
    const matches = routes.filter(r => r.mode === 'local' && r.connectionId === descriptor.connectionId &&
      r.profile === descriptor.profile && r.targetProfile === descriptor.targetProfile)
    if (matches.length !== 1) return null
    const route = matches[0]
    const [active, snapshot] = await Promise.all([
      this.api.requestProfile(route, 'session.active_list', {}),
      this.api.requestProfile(route, 'session.events.since', {session_id: descriptor.sid, last_seen: 0})
    ])
    const rows = Array.isArray(active.sessions) ? active.sessions : []
    const row = rows.find(s => s.id === descriptor.sid)
    if (!row || !['idle', 'working'].includes(row.status) || row.session_key !== descriptor.session_key ||
        row.started_at == null || String(row.started_at) !== descriptor.started_at ||
        String(snapshot.epoch) !== descriptor.epoch || !Array.isArray(snapshot.open_requests) || snapshot.open_requests.length ||
        !['idle', 'working'].includes(row.status)) return null
    return {route, row, snapshot}
  }

  async rejectFollowup(item, owner = this.replyOwner) {
    try { await this.helper({action: 'followup_reject', owner, bot: item.bot, chat: item.chat,
      inbound_id: item.message_id, binding_id: item.binding_id}) } catch { /* input stays consumed in native receipt */ }
  }

  async pumpFollowups() {
    if (this.stopped || this.followupPumping) return
    this.followupPumping = true
    try {
      const heartbeat = await this.helper({action: 'followup_owner_heartbeat', owner: this.replyOwner})
      if (!heartbeat?.feature_ready) return
      const pending = await this.helper({action: 'followup_ready', owner: this.replyOwner})
      for (const item of pending.items || []) {
        if (this.stopped) return
        const d = item.descriptor
        let snapshot = null
        try { snapshot = await this.followupSnapshot(d) } catch { /* fail closed */ }
        if (!snapshot) { await this.rejectFollowup(item); continue }
        let claimed
        try {
          claimed = await this.helper({action: 'followup_claim', owner: this.replyOwner, bot: item.bot, chat: item.chat,
            inbound_id: item.message_id, binding_id: item.binding_id, descriptor: d})
        } catch { continue }
        if (!claimed?.ok || stableJSON(claimed.descriptor) !== stableJSON(d) || typeof claimed.text !== 'string' ||
            !claimed.text.trim() || claimed.text.length > 2000 || claimed.text.trimStart().startsWith('/')) {
          if (claimed?.ok && claimed.claim) await this.helper({action: 'followup_settle', owner: this.replyOwner, bot: item.bot,
            chat: item.chat, inbound_id: item.message_id, claim: claimed.claim, status: 'rejected'})
          else await this.rejectFollowup(item)
          continue
        }
        let outcome = 'rejected'
        try {
          const voice = await this.api.requestProfile(snapshot.route, 'voice.toggle', {action: 'status'}, 15_000)
          if (typeof voice?.enabled !== 'boolean') throw new Error('unknown_voice_state')
          // Do not trigger backend voice-stop phrase handling. Desktop renderer
          // voice is separate; this guard concerns the backend voice loop only.
          if (voice.enabled) throw new Error('backend_voice_active')
          const fresh = await this.followupSnapshot(d)
          if (!fresh || this.stopped) throw new Error('session_changed')
          const begun = await this.helper({action: 'followup_begin_submit', owner: this.replyOwner, bot: item.bot,
            chat: item.chat, inbound_id: item.message_id, claim: claimed.claim})
          if (!begun?.ok || !Number.isFinite(begun.deadline) || Date.now() >= begun.deadline * 1000 || this.stopped) throw new Error('claim_lost')
          outcome = 'unknown'
          const receipt = await this.api.requestProfile(fresh.route, 'prompt.submit', {
            session_id: d.sid, text: claimed.text, queued: true
          }, 15_000)
          if (receipt?.status === 'streaming') outcome = 'accepted_streaming'
          else if (receipt?.status === 'queued') outcome = 'accepted_queued'
        } catch {
          // Ambiguous RPC or malformed receipt is terminal unknown; never retry.
        }
        try {
          await this.helper({action: 'followup_settle', owner: this.replyOwner, bot: item.bot, chat: item.chat,
            inbound_id: item.message_id, claim: claimed.claim, status: outcome})
        } catch { /* submitting lease becomes unknown; never issue a second prompt */ }
      }
    } catch {
      // The ordinary completion and clarify paths continue if the separate inbox is unavailable.
    } finally {
      this.followupPumping = false
    }
  }

  // Input-needed watcher: a session paused on a human shows live status
  // "waiting" (an open server→client request). Poll only routes that already
  // have observed records — never every profile — so no backend is launched
  // just to be polled.
  async sweep() {
    if (this.stopped || this.sweeping) return
    this.sweeping = true
    this.replySweepUnverified = new Set()
    this.replySweepAbsent = new Set()
    this.approvalSweepUnverified = new Set()
    this.approvalSweepAbsent = new Set()
    for (const record of this.records.values()) record.sweepAbsent = false
    try {
      if (this.recoveryCapable) await this.refreshRecoveryPolicy()
      const byRoute = new Map()
      const recoveryOwned = r => Boolean(r.candidate &&
        (r.candidate.recoveryId || r.candidate.recoveryGeneration || r.candidate.recoveryRetired))
      const unverified = r => {
        if (r.reply) this.replySweepUnverified.add(r.reply.id)
        for (const slot of this.approvalRecords(r)) {
          if (slot.approval) this.approvalSweepUnverified.add(slot.approval.id)
        }
      }
      for (const r of [...this.records.values()]) {
        try {
          const route = await this.routeFor(r)
          if (recoveryOwned(r) && !this.currentLiveRoute(route)) {
            unverified(r)
            continue
          }
          if (!byRoute.has(route)) byRoute.set(route, [])
          byRoute.get(route).push(r)
        } catch {
          // Route unavailable right now: keep the record, retry next sweep.
          unverified(r)
        }
      }
      for (const [route, grouped] of byRoute) {
        let rows
        let records = grouped
        const guarded = grouped.some(recoveryOwned)
        let gateway = null
        try {
          if (guarded) {
            if (!this.currentLiveRoute(route)) {
              grouped.filter(recoveryOwned).forEach(unverified)
              records = grouped.filter(r => !recoveryOwned(r))
              if (!records.length) continue
            } else {
              gateway = this.api.getGateway?.()
              if (!gateway || typeof gateway.request !== 'function') throw new Error('Recovery gateway unavailable')
            }
          }
          const active = gateway
            ? await this.requestCurrentRoute(route, 'session.active_list', {})
            : await this.api.requestProfile(route, 'session.active_list', {})
          if (gateway && (!this.currentLiveRoute(route) || this.api.getGateway?.() !== gateway)) {
            throw new Error('Recovery observation socket changed')
          }
          rows = validateActiveSessions(active)
        } catch {
          records.forEach(unverified)
          continue
        }
        for (const r of records) {
          if (this.stopped) return
          // Awaiting an earlier record's input handling may change sockets.
          if (recoveryOwned(r) && (!gateway || !this.currentLiveRoute(route) || this.api.getGateway?.() !== gateway)) {
            unverified(r)
            continue
          }
          const row = rows.find(s => s.id === r.sid)
          if (!row) {
            // A healthy, validated list is definitive absence, unlike a failed
            // or malformed read. Retire completion recovery before pruning it.
            if (r.candidate && this.recoveryCapable) this.invalidate(r)
            if (r.reply) this.replySweepAbsent.add(r.reply.id)
            for (const slot of this.approvalRecords(r)) {
              if (slot.approval) this.approvalSweepAbsent.add(slot.approval.id)
            }
            r.sweepAbsent = true
            if (!r.reply && !this.hasApprovals(r)) this.records.delete(r.key)
            continue
          }
          r.sweepAbsent = false
          if (row.status !== 'waiting') {
            r.ask = null
            r.canonicalAsk = null
            for (const slot of this.approvalRecords(r)) {
              if (slot.approval) this.approvalSweepAbsent.add(slot.approval.id)
            }
            continue
          }
          if (r.canonicalSettled) {
            if (r.approval) this.approvalSweepAbsent.add(r.approval.id)
            continue
          }
          if (r.canonicalAsk) {
            await this.checkCanonicalApproval(r)
            continue
          }
          await this.checkAsk(r, route, row)
        }
      }
      await this.pumpReplies()
      if (this.lastReplyPollSucceeded) {
        for (const r of [...this.records.values()]) {
          if (r.sweepAbsent && !this.hasApprovals(r)) this.records.delete(r.key)
        }
      }
      await this.pumpApprovals()
      if (this.lastApprovalPollSucceeded) {
        for (const r of [...this.records.values()]) {
          if (r.sweepAbsent && !r.reply && !this.hasApprovals(r)) this.records.delete(r.key)
        }
      }
      await this.pumpFollowups()
    } finally {
      this.replySweepUnverified.clear()
      this.replySweepAbsent.clear()
      this.approvalSweepUnverified.clear()
      this.approvalSweepAbsent.clear()
      this.sweeping = false
    }
  }

  async handleCanonicalApprovalEvent(event) {
    if (this.stopped || !event || typeof event !== 'object') return
    const read = atom => {
      try { return typeof atom?.get === 'function' ? atom.get() : undefined } catch { return undefined }
    }
    const profile = typeof event.profile === 'string' && event.profile ? event.profile : read(this.api.state?.profile)
    const connectionId = typeof event.connectionId === 'string' && event.connectionId
      ? event.connectionId : read(this.api.state?.connectionId)
    if (typeof profile !== 'string' || !profile || typeof connectionId !== 'string' || !connectionId ||
        typeof event.session_id !== 'string' || !event.session_id) return
    let routes
    try { routes = await this.refreshRoutes(true) } catch { return }
    const matches = routes.filter(route => route.mode === 'local' &&
      route.connectionId === connectionId && route.profile === profile)
    if (matches.length !== 1 || !this.currentEventIsActiveLocal(profile, connectionId)) return
    const key = JSON.stringify([connectionId, profile, event.session_id])
    let record = this.records.get(key)
    if (!record) {
      record = {key, profile, connectionId, sid:event.session_id, generation:0, candidate:null,
        cancel:null, todos:null, seq:0, settledFinals:new Set(), checking:false, retries:0,
        ask:null, askChecking:false, canonicalAsk:null, canonicalSettled:null}
      this.records.set(key, record)
    }
    if (Number.isSafeInteger(event.seq) && event.seq > record.seq) record.seq = event.seq
    const payload = event.payload
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return
    if (event.type === 'approval.settled') {
      if (typeof payload.prompt_id !== 'string' || !payload.prompt_id ||
          !Number.isSafeInteger(payload.execution_generation) || payload.execution_generation < 1) return
      const prior = record.canonicalAsk
      if (prior && prior.prompt.prompt_id === payload.prompt_id &&
          prior.prompt.execution_generation === payload.execution_generation) record.canonicalAsk = null
      if (record.approval?.descriptor?.dispatch_mode === 'canonical_native' &&
          record.approval.descriptor.prompt_id === payload.prompt_id &&
          record.approval.descriptor.execution_generation === payload.execution_generation &&
          (!record.canonicalAsk || (record.canonicalAsk.prompt.prompt_id === payload.prompt_id &&
            record.canonicalAsk.prompt.execution_generation === payload.execution_generation))) {
        record.canonicalSettled = {prompt_id:payload.prompt_id, execution_generation:payload.execution_generation}
      }
      if (!record.canonicalAsk || (record.canonicalAsk.prompt.prompt_id === payload.prompt_id &&
          record.canonicalAsk.prompt.execution_generation === payload.execution_generation)) {
        record.ask = {id:payload.prompt_id, method:'approval'}
      }
      return
    }
    if (event.type !== 'approval.request' || payload.kind !== 'approval' ||
        typeof payload.prompt_id !== 'string' || !payload.prompt_id ||
        !Number.isSafeInteger(payload.execution_generation) || payload.execution_generation < 1) return
    // The public host adapter projects native prompt_id as a legacy request_id alias.
    // Remove only that verified same-ID projection on our own detached copy; never
    // mutate the SDK cache, repair an ID, or discard unknown native review fields.
    let nativePrompt
    try {
      nativePrompt = structuredClone(payload)
      if (Object.prototype.hasOwnProperty.call(nativePrompt, 'request_id')) {
        if (nativePrompt.request_id !== nativePrompt.prompt_id) return
        delete nativePrompt.request_id
      }
    } catch { return }
    const previous = record.canonicalAsk
    if (previous && (previous.prompt.prompt_id !== nativePrompt.prompt_id ||
        previous.prompt.execution_generation !== nativePrompt.execution_generation ||
        stableJSON(previous.prompt) !== stableJSON(nativePrompt) ||
        previous.replayEpoch !== (event.replay_epoch ?? null) ||
        previous.authorityEpoch !== (event.authority_epoch ?? null))) {
      if (record.approval?.descriptor?.dispatch_mode === 'canonical_native') {
        this.approvalSweepAbsent.add(record.approval.id)
      }
    }
    record.canonicalAsk = {prompt:nativePrompt, seq:Number.isSafeInteger(event.seq) ? event.seq : null,
      replayEpoch:event.replay_epoch ?? null, authorityEpoch:event.authority_epoch ?? null}
    record.canonicalSettled = null
    record.ask = {id:nativePrompt.prompt_id, method:'approval'}
    await this.checkCanonicalApproval(record)
  }

  async checkCanonicalApproval(record) {
    if (this.stopped || !this.approvalCapable || !record?.canonicalAsk || record.canonicalChecking) return
    const prompt = record.canonicalAsk.prompt
    const key = stableJSON([prompt.prompt_id, prompt.execution_generation,
      record.canonicalAsk.replayEpoch, record.canonicalAsk.authorityEpoch, record.canonicalAsk.seq])
    const currentApproval = record.approval?.descriptor
    if (record.canonicalObservedKey === key) return
    if (currentApproval?.dispatch_mode === 'canonical_native' &&
        currentApproval.prompt_id === prompt.prompt_id &&
        currentApproval.execution_generation === prompt.execution_generation) {
      const replayMatches = record.canonicalAsk.replayEpoch == null ||
        currentApproval.replay_epoch === record.canonicalAsk.replayEpoch
      const authorityMatches = record.canonicalAsk.authorityEpoch == null ||
        currentApproval.authority_epoch === record.canonicalAsk.authorityEpoch
      if (replayMatches && authorityMatches) return
      this.approvalSweepAbsent.add(record.approval.id)
      return
    }
    record.canonicalChecking = true
    try {
      const routes = await this.refreshRoutes(true)
      const matches = routes.filter(route => route.mode === 'local' &&
        route.connectionId === record.connectionId && route.profile === record.profile)
      if (matches.length !== 1 || !this.currentEventIsActiveLocal(record.profile, record.connectionId)) return
      const route = matches[0]
      const publicRoute = {mode:'local', connectionId:route.connectionId,
        profile:route.profile, targetProfile:route.targetProfile}
      const observed = await this.helper({action:'canonical_approval_observe', capability:APPROVAL_CAPABILITY,
        owner:this.replyOwner, route:publicRoute, session_id:record.sid, prompt})
      if (!observed || observed.ok !== true ||
          observed.descriptor?.dispatch_mode !== 'canonical_native' ||
          observed.descriptor.owner !== this.replyOwner ||
          observed.descriptor.connectionId !== route.connectionId ||
          observed.descriptor.profile !== route.profile ||
          observed.descriptor.targetProfile !== route.targetProfile ||
          observed.descriptor.sid !== record.sid ||
          observed.descriptor.prompt_id !== prompt.prompt_id ||
          observed.descriptor.execution_generation !== prompt.execution_generation ||
          stableJSON(observed.request) !== stableJSON(prompt) ||
          !observed.pending || typeof observed.pending !== 'object' || Array.isArray(observed.pending)) {
        record.canonicalObservedKey = key
        this.api.notify({kind:'warning', title:'명령 승인 검토 제한',
          message:'현재 요청을 안전하게 확인할 수 없습니다. Desktop에서 실제 승인 상태를 확인해 주세요.'})
        return
      }
      const descriptor = observed.descriptor
      const previous = record.approval
      if (previous && previous.descriptor.dispatch_mode === 'canonical_native' &&
          stableJSON(previous.descriptor) !== stableJSON(descriptor)) {
        this.approvalSweepAbsent.add(previous.id)
        return
      }
      // A new canonical-only monitor has no owner lease yet. Establish the same
      // exact v6 receiver/owner capability gate used by the legacy registration
      // path before asking the fixed CLI to create the first card. A transient,
      // stale, or disabled heartbeat is read-only and remains eligible for the
      // next sweep; it must not poison this native event permanently.
      let heartbeat
      try {
        heartbeat = await this.helper({action:'approval_owner_heartbeat', owner:this.replyOwner,
          capability:APPROVAL_CAPABILITY})
      } catch { return }
      if (heartbeat?.ok !== true || heartbeat.feature_ready !== true ||
          heartbeat.version !== APPROVAL_COMPAT_VERSION || heartbeat.capability !== APPROVAL_CAPABILITY) return
      const registration = await this.helper({action:'approval_register', capability:APPROVAL_CAPABILITY,
        owner:this.replyOwner, descriptor, request:observed.request, pending:observed.pending,
        route:publicRoute, session:{id:record.sid, session_key:descriptor.session_key, status:'waiting'}})
      if (registration?.ok === true && typeof registration.id === 'string' &&
          ['posting','open','queued','applying','dispatching'].includes(registration.status)) {
        record.approval = {id:registration.id, descriptor, request:observed.request,
          pending:observed.pending, requestFingerprint:descriptor.request_fingerprint,
          pendingFingerprint:descriptor.pending_fingerprint, route:{...route}}
        record.canonicalObservedKey = key
        record.ask = {id:prompt.prompt_id, method:'approval'}
        return
      }
      if (registration?.duplicate && typeof registration.id === 'string' &&
          ['posting','open','queued','applying','dispatching'].includes(registration.status)) {
        record.approval = {id:registration.id, descriptor, request:observed.request,
          pending:observed.pending, requestFingerprint:descriptor.request_fingerprint,
          pendingFingerprint:descriptor.pending_fingerprint, route:{...route}}
        record.canonicalObservedKey = key
        return
      }
      if (registration?.reason === 'prior_owner' ||
          ['expired','rejected','unknown','applied'].includes(registration?.status)) {
        record.canonicalObservedKey = key
        return
      }
      // Once a real canonical approval is recognized, no failure can fall back to
      // legacy srq handling or a generic attention card containing partial command text.
      try {
        await this.helper({action:'approval_notice', capability:APPROVAL_CAPABILITY,
          owner:this.replyOwner, descriptor, request:observed.request, pending:observed.pending,
          reason:typeof registration?.reason === 'string' ? registration.reason : 'review_unavailable'})
      } catch { /* one command-free notice attempt; never replay an uncertain send */ }
      record.canonicalObservedKey = key
      this.api.notify({kind:'warning', title:'명령 승인 검토 제한',
        message:'이 요청은 Telegram에서 안전하게 검토할 수 없습니다. 전체 명령과 현재 승인을 Desktop에서 확인해 주세요.'})
    } catch {
      // A failed helper invocation is not retried within this approval event.
      record.canonicalObservedKey = key
      this.api.notify({kind:'warning', title:'명령 승인 검토 제한',
        message:'현재 요청을 확인하지 못했습니다. 중복 방지를 위해 Telegram 승인 카드를 만들지 않았습니다.'})
    } finally {
      record.canonicalChecking = false
    }
  }

  async checkAsk(r, route, row) {
    if (this.stopped || r.askChecking) return
    r.askChecking = true
    let currentAskId = null
    let currentAskFingerprint = null
    let currentAskMethod = null
    let attemptedRegistration = null
    try {
      validateSessionRow(row)
      if (row.id !== r.sid) throw new Error('Clarify session identity changed')
      if (r.reply && row.session_key !== r.reply.descriptor.session_key) {
        // The runtime ID now names a different durable session. Retire the old
        // bridge as explicit absence before allowing this row to register anew.
        this.replySweepAbsent.add(r.reply.id)
        return
      }
      const since = await this.api.requestProfile(route, 'session.events.since', { session_id: r.sid, last_seen: r.seq })
      const open = validateOpenRequests(since)
      const ask = open.find(q => q && HUMAN_ASK_METHODS.has(q.method))
      currentAskId = ask?.id || null
      currentAskMethod = ask?.method || null
      if (ask?.method === 'approval') currentAskFingerprint = stableJSON(ask)
      // Parallel tools can leave several native approvals open in one session.
      // Per-SRQ slots change bookkeeping only, never native authority or IDs.
      const approvals = open.filter(q => q.method === 'approval')
      if (approvals.length > 100 || new Set(open.map(q => q.id)).size !== open.length) {
        for (const slot of this.approvalRecords(r)) {
          if (slot.approval) this.approvalSweepUnverified.add(slot.approval.id)
        }
        return
      }
      r.approvalSlots ??= new Map()
      const liveIds = new Set(approvals.map(q => q.id))
      for (const slot of this.approvalRecords(r)) {
        if (slot.approval && !liveIds.has(slot.approval.descriptor.request_id)) {
          this.approvalSweepAbsent.add(slot.approval.id)
        }
      }
      for (const [id, slot] of r.approvalSlots) {
        if (!slot.approval && !liveIds.has(id)) r.approvalSlots.delete(id)
      }
      let firstHandled = false
      for (const approval of approvals) {
        if (this.stopped) return
        if (!route.connectionId || approval.params?.session_id !== r.sid) continue
        let slot = r.approvalSlots.get(approval.id)
        if (!slot) {
          // Even the first request owns its own slot before registration can
          // fail. Never share clarify/attention ask or transport-fence state.
          slot = {sid:r.sid, profile:r.profile, connectionId:r.connectionId, ask:null}
          r.approvalSlots.set(approval.id, slot)
        }
        const fingerprint = stableJSON(approval)
        if (slot.helperWriteBlockedFingerprint === fingerprint || slot.helperWriteBlockedAskId === approval.id) {
          if (approval === ask) firstHandled = true
          continue
        }
        try {
          const handled = await this.checkApproval(slot, route, row, since, approval)
          if (approval === ask) firstHandled = handled
        } catch (error) {
          // One oversized/failed request must not hide independent siblings.
          // Only pre-write reads may retry; unknown writes retain the exact SRQ
          // fence, and permanent local failures retain their full fingerprint.
          if (error instanceof NativeHelperError && error.permanent) {
            slot.helperWriteBlockedFingerprint = fingerprint
            this.api.notify({kind:'warning', title:'명령 승인 전송 제한',
              message:'이 승인 요청이 로컬 전송 제한을 초과했습니다. 해당 명령은 Desktop에서 확인해 주세요. 다른 승인 요청은 별도로 처리합니다.'})
          } else if (error instanceof NativeHelperError && error.ambiguousWrite) {
            slot.helperWriteBlockedAskId = approval.id
            this.api.notify({kind:'warning', title:'명령 승인 전송 확인 필요',
              message:'이 승인 카드의 전송 결과를 확인하지 못해 다시 보내지 않습니다. 해당 요청은 Desktop에서 확인해 주세요.'})
          }
          if (slot.approval) this.approvalSweepUnverified.add(slot.approval.id)
          if (approval === ask) firstHandled = true
        }
      }
      if (firstHandled) {
        r.ask = {id:ask.id, method:ask.method}
        return
      }
      if (ask?.method === 'clarify' && route.connectionId && ask.params?.session_id === r.sid) {
        validateReplySnapshot(since)
        currentAskFingerprint = stableJSON(ask)
        if (r.helperWriteBlockedFingerprint === currentAskFingerprint) { r.ask = {id: ask.id}; return }
        if (r.helperWriteBlockedFingerprint) r.helperWriteBlockedFingerprint = null
        if (r.helperWriteBlockedAskId === ask.id) { r.ask = {id: ask.id}; return }
        if (r.reply && validReplySnapshot(r.reply, route, row, since)) return
        const descriptor = { owner: this.replyOwner, connectionId: route.connectionId, profile: route.profile,
          targetProfile: route.targetProfile, sid: r.sid, session_key: row.session_key,
          epoch: String(since.epoch), request_id: ask.id, fingerprint: currentAskFingerprint }
        if (r.replyRegistration?.descriptor.request_id === ask.id) return
        const previous = r.reply
        if (previous && !['connectionId','profile','targetProfile','sid','session_key','epoch','request_id']
          .every(key => previous.descriptor[key] === descriptor[key])) {
          // A different durable request identity must retire the old bridge
          // before a new registration can replace its local reference.
          this.replySweepAbsent.add(previous.id)
          return
        }
        attemptedRegistration = {descriptor, route:{...route}, ask:structuredClone(ask)}
        const reply = await this.helper({action:'reply_register', descriptor, ask})
        if (reply?.ok === true && reply.new === true && reply.id) {
          r.replyRegistration = null
          r.reply = {id:reply.id, descriptor, route:{...route}, ask}
          this.hadReplies = true
          r.ask = {id:ask.id}
          return
        }
        const sameRequest = previous && ['connectionId','profile','targetProfile','sid','session_key','epoch','request_id']
          .every(key => previous.descriptor[key] === descriptor[key])
        if (reply?.reason === 'immutable_mismatch') {
          // The helper has atomically retired the old immutable descriptor.
          if (sameRequest) r.reply = null
          r.ask = {id:ask.id}
          return
        }
        if (reply?.duplicate) {
          if (previous && previous.id === reply.id && stableJSON(previous.descriptor) === stableJSON(descriptor) &&
              REPLY_ACTIVE_STATUSES.has(reply.status)) {
            r.reply = previous
          } else if (previous && previous.id === reply.id && !REPLY_ACTIVE_STATUSES.has(reply.status)) {
            r.reply = null
          }
          // A duplicate alone grants no binding. Only a separate read-only
          // exact-owner lookup plus fresh native snapshot may recover it.
          if (!previous && ['open','queued'].includes(reply.status)) {
            r.replyRegistration = attemptedRegistration
            this.hadReplies = true
          }
          r.ask = {id:ask.id}
          return
        }
        if (reply?.reason === 'prior_owner') {
          r.ask = {id:ask.id}
          return
        }
        if (previous) {
          // Transport/receiver failure does not discard the existing bridge.
          r.ask = {id:ask.id}
          return
        }
        if (!['receiver_unavailable','capacity'].includes(reply?.reason) && reply?.feature_ready !== false) {
          r.replyRegistration = attemptedRegistration
          this.hadReplies = true
          r.ask = {id:ask.id}
          return // a post-registration structured error must not resend/fall back
        }
      }
      if (!ask || (r.ask && r.ask.id === ask.id)) return
      const reply = await this.helper({
        action: 'attention', kind: 'ask', profile: route.targetProfile, session_key: row.session_key,
        ask_id: ask.id, ask_method: ask.method,
        // Unverified/unsupported approval notifications remain metadata-only;
        // complete command review belongs exclusively to the approval path.
        ask_params: ask.method === 'approval' ? {} : (ask.params || {}), desktop_observed: true
      })
      // Any terminal answer settles the episode: gate rejections are
      // deterministic and an ambiguous send must never be retried. Only the
      // transport-failure catch below re-attempts while the ask stays open.
      r.ask = { id: ask.id, method: ask.method }
      if (!reply?.ok && !reply?.duplicate && ['failed', 'unknown'].includes(reply?.status)) {
        this.api.notify({ kind: 'warning', title: '입력 대기 알림', message: '텔레그램 전송을 확인하지 못했습니다. 중복 방지를 위해 자동 재전송하지 않습니다.' })
      }
    } catch (error) {
      if (currentAskId && error instanceof NativeHelperError && error.permanent) {
        r.helperWriteBlockedFingerprint = currentAskFingerprint
        r.ask = {id: currentAskId}
        this.api.notify({kind: 'warning', title: '질문 응답 전송 제한', message: '질문이 로컬 전송 크기 제한을 초과해 텔레그램 연결을 시작하지 않았습니다. 질문을 짧게 줄여 다시 요청해 주세요.'})
      } else if (currentAskId && error instanceof NativeHelperError && error.ambiguousWrite) {
        if (attemptedRegistration) {
          r.replyRegistration = attemptedRegistration
          this.hadReplies = true
        }
        r.helperWriteBlockedAskId = currentAskId
        r.ask = {id: currentAskId}
        this.api.notify({kind: 'warning', title: '입력 대기 알림', message: '결과를 확인하지 못했습니다. 중복 방지를 위해 다시 시도하지 않습니다.'})
      }
      // A transport failure before a native write is retried on the next sweep.
    } finally {
      r.askChecking = false
    }
  }

  async checkApproval(r, route, row, since, ask) {
    if (!this.approvalCapable) return false
    validateSessionRow(row)
    const requests = validateOpenRequests(since)
    const matches = requests.filter(value => value.id === ask.id)
    if (matches.length !== 1 || matches[0].method !== 'approval') return false
    const pendingReply = await this.api.requestProfile(route, 'approval.pending', {session_id:r.sid})
    if (!pendingReply || !Array.isArray(pendingReply.approvals)) throw new Error('approval pending snapshot unavailable')
    const authority = classifyApprovalAuthority(ask,pendingReply,r.sid)
    if (!authority) {
      if(r.approval){this.approvalSweepUnverified.add(r.approval.id);return true}
      return false
    }
    const pending=authority.pending
    const dispatchMode=authority.mode
    const requestFingerprint = await approvalFingerprint(ask)
    const pendingFingerprint = await approvalFingerprint(pending)
    const descriptor = {
      owner:this.replyOwner, connectionId:route.connectionId, profile:route.profile,
      targetProfile:route.targetProfile, sid:r.sid, session_key:row.session_key,
      epoch:String(since.epoch), request_id:ask.id, queue_id:ask.params.request_id,
      dispatch_mode:dispatchMode, request_fingerprint:requestFingerprint, pending_fingerprint:pendingFingerprint
    }
    if (!descriptor.connectionId || !descriptor.profile || !descriptor.targetProfile || !descriptor.session_key ||
        !((typeof since.epoch === 'string' && since.epoch) || (typeof since.epoch === 'number' && Number.isFinite(since.epoch)))) {
      if (r.approval) this.approvalSweepUnverified.add(r.approval.id)
      return true
    }
    const previous = r.approval
    if (previous) {
      const same = stableJSON(previous.descriptor) === stableJSON(descriptor) &&
        stableJSON(previous.request) === stableJSON(ask) && stableJSON(previous.pending) === stableJSON(pending)
      if (!same) {
        this.approvalSweepAbsent.add(previous.id)
        return true
      }
      r.ask = {id:ask.id, method:'approval'}
      return true
    }
    let heartbeat
    try {
      heartbeat = await this.helper({action:'approval_owner_heartbeat', owner:this.replyOwner,
        capability:APPROVAL_CAPABILITY})
    } catch {
      // This admission-only lease operation cannot post a new card. Losing its
      // receipt must defer registration, not create a card-send no-replay fence.
      // Registration/notice ambiguity below still propagates to the SRQ slot.
      return true
    }
    let registered = {ok:false, reason:'receiver_unavailable'}
    if (heartbeat?.feature_ready === true && heartbeat.version === APPROVAL_COMPAT_VERSION && heartbeat.capability === APPROVAL_CAPABILITY) {
      registered = await this.helper({action:'approval_register', capability:APPROVAL_CAPABILITY,
        owner:this.replyOwner, descriptor, request:ask, pending, route:{
          mode:route.mode, connectionId:route.connectionId, profile:route.profile, targetProfile:route.targetProfile
        }, session:{id:row.id, session_key:row.session_key, status:row.status}, epoch:since.epoch})
    }
    if (registered?.ok === true && typeof registered.id === 'string' &&
        ['posting','open','queued','applying'].includes(registered.status)) {
      r.approval = {id:registered.id, descriptor, request:ask, pending,
        requestFingerprint, pendingFingerprint, route:{...route}}
      r.ask = {id:ask.id, method:'approval'}
      return true
    }
    if (registered?.reason === 'prior_owner' || registered?.duplicate || registered?.status &&
        ['expired','rejected','unknown','applied'].includes(registered.status)) {
      r.ask = {id:ask.id, method:'approval'}
      return true
    }
    // Once the pair is supported, no failure category may re-enter generic
    // attention with command text. Unknown/malformed failures are guidance-only.
    const notice = await this.helper({action:'approval_notice', capability:APPROVAL_CAPABILITY,
      owner:this.replyOwner, descriptor, request:ask, pending,
      reason:typeof registered?.reason === 'string' ? registered.reason : 'review_unavailable'})
    r.ask = {id:ask.id, method:'approval'}
    this.api.notify({kind:'warning', title:'명령 승인 검토 제한',
      message:'이 명령은 Telegram에서 안전하게 검토할 수 없습니다. 전체 명령과 현재 승인을 Desktop에서 확인해 주세요.'})
    if (!notice?.ok && !notice?.duplicate && ['failed','unknown'].includes(notice?.status)) {
      this.api.notify({kind:'warning', title:'입력 대기 알림',
        message:'알림 전송 결과를 확인하지 못했습니다. 중복 방지를 위해 다시 보내지 않았습니다.'})
    }
    return true
  }

  async approvalSnapshot(bridge) {
    const d = bridge.descriptor
    if (d.dispatch_mode === 'canonical_native') {
      const routes = await this.refreshRoutes(true)
      const matches = routes.filter(route => route.mode === 'local' &&
        route.connectionId === d.connectionId && route.profile === d.profile &&
        route.targetProfile === d.targetProfile)
      if (matches.length !== 1 || !this.currentEventIsActiveLocal(d.profile, d.connectionId)) {
        throw new Error('canonical approval route is not the captured active owner')
      }
      const reply = await this.helper({action:'approval_native_observe', capability:APPROVAL_CAPABILITY,
        owner:this.replyOwner, id:bridge.id})
      if (exactKeys(reply, ['ok','id','request_fingerprint','pending_fingerprint','authority']) &&
          reply.ok === true && reply.id === bridge.id &&
          reply.request_fingerprint === d.request_fingerprint &&
          reply.pending_fingerprint === d.pending_fingerprint &&
          typeof reply.authority === 'string' && /^[0-9a-f]{64}$/u.test(reply.authority)) {
        return {route:bridge.route, request:bridge.request, pending:bridge.pending,
          dispatchMode:'canonical_native', observation:reply}
      }
      const reason = typeof reply?.reason === 'string' ? reply.reason : 'native_snapshot_unavailable'
      if (['snapshot_stale','prompt_stale','authority_changed','binding_changed','approval_terminal',
        'approval_missing_or_unowned','not_found','stale_generation','permission_denied',
        'profile_mismatch','runtime_retired','root_binding_changed'].includes(reason)) return null
      throw new Error(reason)
    }
    const routes = await this.refreshRoutes(true)
    const matches = routes.filter(route => route.mode === 'local' && route.connectionId === d.connectionId &&
      route.profile === d.profile && route.targetProfile === d.targetProfile)
    if (matches.length !== 1) throw new Error('approval route unavailable')
    const route = matches[0]
    const [active, snapshot, pendingReply] = await Promise.all([
      this.api.requestProfile(route, 'session.active_list', {}),
      this.api.requestProfile(route, 'session.events.since', {session_id:d.sid, last_seen:0}),
      this.api.requestProfile(route, 'approval.pending', {session_id:d.sid})
    ])
    const rows = validateActiveSessions(active)
    const row = rows.find(value => value.id === d.sid)
    if (!pendingReply || !Array.isArray(pendingReply.approvals)) throw new Error('approval pending snapshot unavailable')
    const request=snapshot?.open_requests?.filter(value=>value?.id===d.request_id)
    if(!Array.isArray(request)||request.length!==1)return null
    const currentRequest=request[0]
    const authority=classifyApprovalAuthority(currentRequest,pendingReply,d.sid)
    if(!authority||authority.mode!==d.dispatch_mode||
        !validApprovalSnapshot(bridge,route,row,snapshot,pendingReply.approvals,this.replyOwner))return null
    if (await approvalFingerprint(currentRequest) !== d.request_fingerprint ||
        await approvalFingerprint(authority.pending) !== d.pending_fingerprint) return null
    return {route,row,snapshot,request:currentRequest,pending:authority.pending,dispatchMode:authority.mode}
  }

  approvalRecords(record = null) {
    const parents = record ? [record] : [...this.records.values()]
    return parents.flatMap(parent => [parent, ...(parent.approvalSlots?.values() || [])])
  }

  hasApprovals(record) {
    return this.approvalRecords(record).some(slot => slot.approval)
  }

  async pumpApprovals() {
    this.lastApprovalPollSucceeded = false
    // No owned approval means there is no input to deliver. Registration owns
    // its own capability/receiver heartbeat; do not launch idle helper RPCs on
    // every ordinary completion/clarify sweep.
    if (this.stopped || !this.approvalRecords().some(record => record.approval)) return
    try {
      const heartbeat = await this.helper({action:'approval_owner_heartbeat', owner:this.replyOwner,
        capability:APPROVAL_CAPABILITY})
      if (heartbeat?.feature_ready !== true || heartbeat.version !== APPROVAL_COMPAT_VERSION || heartbeat.capability !== APPROVAL_CAPABILITY) return
    } catch { return }
    const bridges = new Map()
    const observations = []
    const unverified = [...this.approvalSweepUnverified]
    const absent = [...this.approvalSweepAbsent]
    for (const record of this.approvalRecords()) {
      const bridge = record.approval
      if (!bridge || unverified.includes(bridge.id) || absent.includes(bridge.id)) continue
      try {
        const current = await this.approvalSnapshot(bridge)
        if (!current) { absent.push(bridge.id); continue }
        if (bridge.descriptor.dispatch_mode === 'canonical_native') {
          observations.push(current.observation)
        } else {
          const authority = await approvalFingerprint(bridge.descriptor)
          observations.push({id:bridge.id, request_fingerprint:bridge.descriptor.request_fingerprint,
            pending_fingerprint:bridge.descriptor.pending_fingerprint, authority})
        }
        bridges.set(bridge.id, {...bridge, route:current.route})
      } catch {
        unverified.push(bridge.id)
      }
    }
    let polled
    try {
      const heartbeat = await this.helper({action:'approval_owner_heartbeat', owner:this.replyOwner,
        capability:APPROVAL_CAPABILITY})
      if (heartbeat?.feature_ready !== true || heartbeat.version !== APPROVAL_COMPAT_VERSION || heartbeat.capability !== APPROVAL_CAPABILITY) return
      polled = await this.helper({action:'approval_poll', owner:this.replyOwner, capability:APPROVAL_CAPABILITY,
        observations, unverified:[...new Set(unverified)], absent:[...new Set(absent)]})
    } catch { return }
    if (polled?.ok !== true || !Array.isArray(polled.items)) return
    this.lastApprovalPollSucceeded = true
    for (const terminal of polled.terminal || []) {
      if (typeof terminal !== 'string') continue
      for (const record of this.approvalRecords()) if (record.approval?.id === terminal) record.approval = null
    }
    for (const item of polled.items) {
      const bridge = bridges.get(item?.id)
      let status = 'rejected'
      try {
        if (!bridge || this.stopped || !['once','deny'].includes(item.choice) ||
            bridge.id !== item.id || typeof item.claim !== 'string' || !/^[0-9a-f]{32}$/u.test(item.claim) ||
            stableJSON(item.descriptor) !== stableJSON(bridge.descriptor) ||
            !Number.isFinite(item.deadline) || Date.now() >= item.deadline * 1000) {
          throw new Error('approval claim invalid')
        }
        if (bridge.descriptor.dispatch_mode === 'canonical_native') {
          // The fixed CLI reads the SQLite row as authority, reattaches through the
          // native client, refreshes the complete snapshot, CASes dispatching, and
          // sends the exact four native fields once. Desktop never RPCs approval.respond.
          status = 'unknown'
          try {
            const receipt = await this.helper({action:'approval_native_apply',
              capability:APPROVAL_CAPABILITY, owner:this.replyOwner, id:item.id, claim:item.claim})
            if (receipt?.ok === true && receipt.status === 'applied' &&
                receipt.receipt === 'decision_resolved') status = 'applied'
            else if (['expired','rejected','unknown'].includes(receipt?.status)) status = receipt.status
          } catch { /* write boundary is ambiguous; the stored claim is never replayed */ }
          for (const record of this.approvalRecords()) if (record.approval?.id === item.id) record.approval = null
          continue
        }
        // The legacy queue modes retain their original SDK dispatch semantics.
        const fresh = await this.approvalSnapshot(bridge)
        if (!fresh || fresh.dispatchMode !== bridge.descriptor.dispatch_mode || this.stopped || Date.now() >= item.deadline * 1000 ||
            stableJSON(item.request) !== stableJSON(fresh.request) ||
            stableJSON(item.pending) !== stableJSON(fresh.pending)) throw new Error('approval snapshot changed')
        const authority = await approvalFingerprint(bridge.descriptor)
        const gate = await this.helper({action:'approval_claim_valid', capability:APPROVAL_CAPABILITY,
          owner:this.replyOwner, id:item.id, claim:item.claim, descriptor:bridge.descriptor,
          authority, deadline:item.deadline})
        if (!exactKeys(gate, ['ok','valid','id','claim','authority','deadline']) || gate.ok !== true ||
            gate.valid !== true || gate.id !== item.id || gate.claim !== item.claim ||
            gate.authority !== authority || gate.deadline !== item.deadline || !Number.isFinite(gate.deadline)) {
          throw new Error('approval claim authority lost')
        }
        // This is the last fresh read, not atomic server-side fingerprint CAS.
        // Recheck local lifetime immediately before starting the one-way RPC.
        if (this.stopped || Date.now() >= item.deadline * 1000) throw new Error('approval claim expired')
        status = 'unknown'
        let receipt
        try {
          if (bridge.descriptor.dispatch_mode === 'legacyqueue') {
            receipt = await this.api.requestProfile(fresh.route, 'approval.respond', {
              session_id:bridge.descriptor.sid, request_id:bridge.descriptor.queue_id,
              choice:item.choice, all:false
            }, 15_000)
            if (!exactKeys(receipt, ['resolved']) || typeof receipt.resolved !== 'number' ||
                !Number.isFinite(receipt.resolved)) {
              status = 'unknown'
            } else if (receipt.resolved === 1) {
              status = 'applied'
            } else if (receipt.resolved === 0) {
              status = 'expired'
            } else {
              status = 'unknown'
            }
          } else if (bridge.descriptor.dispatch_mode === 'legacyserver_request') {
            receipt = await this.api.requestProfile(fresh.route, 'request.answer', {
              id:bridge.descriptor.request_id, result:{choice:item.choice, all:false}
            }, 15_000)
            if (exactKeys(receipt, ['status']) && receipt.status === 'expired') status = 'expired'
            else status = 'unknown' // Gateway acceptance is not child queue settlement or command completion.
          } else {
            throw new Error('approval dispatch mode invalid')
          }
        } catch { throw new Error('approval response unknown') }
      } catch {
        if (status !== 'unknown') status = 'rejected'
      }
      try {
        const settled = await this.helper({action:'approval_settle', capability:APPROVAL_CAPABILITY,
          id:item.id, claim:item.claim, status})
        if (settled?.ok) {
          for (const record of this.approvalRecords()) if (record.approval?.id === item.id) record.approval = null
        }
      } catch { /* applying lease becomes unknown; never replay */ }
    }
  }

  async replySnapshot(bridge) {
    const route = (await this.refreshRoutes()).find(x => x.mode === 'local' && x.connectionId === bridge.descriptor.connectionId && x.profile === bridge.descriptor.profile && x.targetProfile === bridge.descriptor.targetProfile)
    // A missing route or incomplete snapshot is a transport gap, not an
    // invalidation: throw so the sweep holds the card instead of expiring it.
    if (!route) throw new Error('reply route unavailable')
    const active = await this.api.requestProfile(route, 'session.active_list', {})
    const rows = validateActiveSessions(active)
    const row = rows.find(x => x.id === bridge.descriptor.sid)
    const snapshot = await this.api.requestProfile(route, 'session.events.since', {session_id:bridge.descriptor.sid, last_seen:0})
    return validReplySnapshot(bridge, route, row, snapshot)
  }

  async replyFragment(item, bridge) {
    const meta = item.fragment
    if (!exactKeys(meta, ['sha256','bytes','chunks','chunk_bytes']) || !/^[0-9a-f]{64}$/u.test(meta.sha256) ||
        !Number.isSafeInteger(meta.bytes) || meta.bytes < 1 || meta.bytes > MAX_FRAGMENT_BYTES ||
        meta.chunk_bytes !== FRAGMENT_CHUNK_BYTES || !Number.isSafeInteger(meta.chunks) ||
        meta.chunks !== Math.ceil(meta.bytes / FRAGMENT_CHUNK_BYTES) || meta.chunks < 1 || meta.chunks > MAX_FRAGMENT_CHUNKS ||
        typeof item.id !== 'string' || typeof item.claim !== 'string' || !Number.isFinite(item.deadline)) {
      throw fragmentError('invalid_fragment_metadata')
    }
    if (Date.now() >= item.deadline * 1000) throw fragmentError('fragment_claim_expired', true)
    const chunks = []
    let total = 0
    for (let index = 0; index < meta.chunks; index++) {
      if (this.stopped) throw fragmentError('fragment_aborted')
      if (Date.now() >= item.deadline * 1000) throw fragmentError('fragment_claim_expired', true)
      const response = await this.helper({action:'reply_fragment', owner:this.replyOwner, id:item.id,
        claim:item.claim, sha256:meta.sha256, index})
      if (response?.ok !== true) {
        throw fragmentError('fragment_unavailable', response?.reason === 'claim_expired' || Date.now() >= item.deadline * 1000)
      }
      if (!exactKeys(response, ['ok','id','claim','sha256','index','bytes','chunks','chunk_bytes','data']) ||
          response.id !== item.id || response.claim !== item.claim || response.sha256 !== meta.sha256 ||
          response.index !== index || response.bytes !== meta.bytes || response.chunks !== meta.chunks ||
          response.chunk_bytes !== FRAGMENT_CHUNK_BYTES) throw fragmentError('fragment_metadata_mismatch')
      const expectedBytes = Math.min(FRAGMENT_CHUNK_BYTES, meta.bytes - index * FRAGMENT_CHUNK_BYTES)
      const bytes = decodeFragmentChunk(response.data, expectedBytes)
      total += bytes.byteLength
      if (total > meta.bytes || total > MAX_FRAGMENT_BYTES) throw fragmentError('fragment_size_mismatch')
      chunks.push(bytes)
      if (Date.now() >= item.deadline * 1000) throw fragmentError('fragment_claim_expired', true)
    }
    if (total !== meta.bytes) throw fragmentError('fragment_size_mismatch')
    const joined = new Uint8Array(total)
    let offset = 0
    for (const chunk of chunks) { joined.set(chunk, offset); offset += chunk.byteLength }
    if (!globalThis.crypto?.subtle) throw fragmentError('fragment_digest_unavailable')
    const digestBytes = new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', joined))
    const digest = Array.from(digestBytes, byte => byte.toString(16).padStart(2, '0')).join('')
    if (digest !== meta.sha256) throw fragmentError('fragment_digest_mismatch')
    let payload
    try { payload = JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(joined)) }
    catch { throw fragmentError('fragment_json_invalid') }
    if (!exactKeys(payload, ['descriptor','result']) || stableJSON(payload.descriptor) !== stableJSON(bridge.descriptor) ||
        !validReplyResult(bridge, payload.result)) throw fragmentError('fragment_payload_invalid')
    return payload.result
  }

  async recoverReplyRegistration(record) {
    const pending = record.replyRegistration
    if (!pending || record.reply || this.stopped) return
    try {
      const found = await this.helper({action:'reply_registration_lookup',
        descriptor:pending.descriptor, ask:pending.ask})
      if (this.stopped || record.replyRegistration !== pending || record.reply) return
      if (!exactKeys(found, ['ok','found','id','status','expires']) || found.ok !== true || found.found !== true ||
          typeof found.id !== 'string' || !found.id || !['open','queued'].includes(found.status) ||
          !Number.isFinite(found.expires) || Date.now() >= found.expires * 1000) return
      const bridge = {id:found.id, ...pending}
      let valid
      try { valid = await this.replySnapshot(bridge) }
      catch {
        if (!this.stopped && record.replyRegistration === pending) this.replySweepUnverified.add(found.id)
        return
      }
      if (this.stopped || record.replyRegistration !== pending || record.reply) return
      if (!valid) {
        this.replySweepAbsent.add(found.id)
        record.replyRegistration = null
        return
      }
      record.reply = bridge
      record.replyRegistration = null
      // Never clear the write fence: this operation only rediscovers an
      // already-published, exact-owner form and cannot resend a registration.
    } catch { /* repeat only this read-only lookup after a transport gap */ }
  }

  async pumpReplies() {
    this.lastReplyPollSucceeded = false
    if (!this.hadReplies || this.stopped) return
    for (const record of this.records.values()) await this.recoverReplyRegistration(record)
    if (this.stopped) return
    const bridges = new Map()
    const unverified = [...this.replySweepUnverified]
    const definitiveAbsent = new Set(this.replySweepAbsent)
    for (const r of this.records.values()) {
      if (!r.reply) continue
      if (this.replySweepUnverified.has(r.reply.id)) continue
      if (this.replySweepAbsent.has(r.reply.id)) { definitiveAbsent.add(r.reply.id); continue }
      try {
        if (await this.replySnapshot(r.reply)) bridges.set(r.reply.id, r.reply)
        else definitiveAbsent.add(r.reply.id)
      } catch { unverified.push(r.reply.id) } // transport gap: block, never expire
    }
    let polled
    try { polled = await this.helper({action:'reply_poll', owner:this.replyOwner,
      observations:[...bridges.values()].map(b => ({id:b.id})), unverified, absent:[...definitiveAbsent]}) } catch { return }
    if (polled?.ok !== true || !Array.isArray(polled.items)) return
    this.lastReplyPollSucceeded = true
    for (const item of polled.items || []) {
      const bridge = bridges.get(item.id)
      const isFragment = Boolean(item.fragment)
      let status = 'rejected'
      try {
        if (!this.stopped && bridge) {
          const result = isFragment ? await this.replyFragment(item, bridge) : item.result
          const identityMatches = isFragment || stableJSON(item.descriptor) === stableJSON(bridge.descriptor)
          if (identityMatches && await this.replySnapshot(bridge)) {
            if (validReplyResult(bridge, result) && Number.isFinite(item.deadline) && Date.now() < item.deadline * 1000) {
              const authority = await approvalFingerprint(bridge.descriptor)
              const gate = await this.helper({action:'reply_claim_valid', owner:this.replyOwner,
                id:item.id, claim:item.claim, descriptor:bridge.descriptor, authority, deadline:item.deadline})
              if (!exactKeys(gate, ['ok','valid','id','claim','authority','deadline','reason']) ||
                  gate.ok !== true || gate.valid !== true || gate.id !== item.id || gate.claim !== item.claim ||
                  gate.authority !== authority || gate.deadline !== item.deadline || gate.reason !== 'ready' ||
                  this.stopped || Date.now() >= item.deadline * 1000) throw new Error('reply claim no longer authorized')
              // Set unknown BEFORE a write: a timeout can mean the response was applied.
              status = 'unknown'
              const receipt = await this.api.requestProfile(bridge.route, 'request.answer', {id:bridge.descriptor.request_id, result}, 15_000)
              status = receipt.status === 'ok' ? 'applied' : receipt.status === 'expired' ? 'expired' : 'unknown'
            } else if (Number.isFinite(item.deadline) && Date.now() >= item.deadline * 1000) {
              status = 'expired'
            }
          }
        }
      } catch (error) {
        if (isFragment) status = error?.claimExpired || (Number.isFinite(item.deadline) && Date.now() >= item.deadline * 1000) ? 'expired' : 'unknown'
        // Keep unknown after any attempted write; fragment failures are terminal too.
      }
      try { await this.helper({action:'reply_settle', id:item.id, claim:item.claim, status}) } catch { /* never resubmit */ }
    }
    for (const r of this.records.values()) {
      if (r.reply && definitiveAbsent.has(r.reply.id)) r.reply = null
    }
  }

  dispose() {
    this.stopped = true
    for (const record of this.records.values()) record.cancel?.()
    this.records.clear()
  }
}

export const SETTINGS_ROWS = Object.freeze([
  {key:'service_enabled', label:'전체 알림과 Telegram 입력 사용', description:'모든 기능의 공통 스위치입니다. 꺼도 아래 개별 선택값은 보존됩니다.'},
  {key:'completion_notifications_enabled', module:'completion_notifications', label:'작업 완료 알림', description:'작업과 대기 항목이 정리된 뒤 60초간 조용하면 완료 카드를 보냅니다.'},
  {key:'attention_notifications_enabled', module:'attention_notifications', label:'질문·승인 대기 알림', description:'사용자 입력이 필요해 작업이 멈추면 알려줍니다.'},
  {key:'clarify_replies_enabled', module:'clarify_replies', label:'Telegram에서 질문에 답하기', description:'현재 질문 카드에 대한 답변만 해당 Desktop 질문에 전달합니다.'},
  {key:'completion_followups_enabled', module:'completion_followups', label:'완료 카드로 후속 요청하기', description:'완료 카드에 답장하여 같은 작업에 후속 요청을 보냅니다.'},
  {key:'approvals_enabled', module:'approvals', label:'명령 한 번 허용·거절', description:'일회성 허용·거절만 제공합니다. 영구 허용이나 자동 승인을 켜지 않습니다.'},
  {key:'replies_enabled', label:'Telegram 답장 입력 허용', description:'질문 답변·후속 요청의 공통 허용 조건입니다. 기존 답장 허용값을 그대로 보존합니다.'}
])
const SETTINGS_MODULES = Object.freeze(SETTINGS_ROWS.filter(row => row.module).map(row => row.module))

export function validateSettingsStatus(value) {
  const object = v => v && typeof v === 'object' && !Array.isArray(v)
  if (!object(value) || value.version !== VERSION || typeof value.ok !== 'boolean' ||
      typeof value.read_only !== 'boolean' || typeof value.selection_reason !== 'string' ||
      !object(value.settings) || !object(value.effective) || !object(value.receiver) || !object(value.sender) ||
      typeof value.receiver.heartbeat_ready !== 'boolean' || value.sender.version !== VERSION ||
      typeof value.sender.available !== 'boolean' ||
      ['awaiting_recovery','uncertain_delivery','sent','expired'].some(key =>
        !Number.isSafeInteger(value.sender[key]) || value.sender[key] < 0) ||
      SETTINGS_ROWS.some(row => typeof value.settings[row.key] !== 'boolean') ||
      SETTINGS_MODULES.some(key => typeof value.effective[key] !== 'boolean')) {
    throw new NativeHelperError('설정 응답을 확인할 수 없습니다. 설치 버전과 연결을 확인해 주세요.')
  }
  return value
}

export async function readBridgeSettings(monitor) {
  if (monitor.stopped) throw new NativeHelperError('플러그인이 비활성화되었습니다.')
  await monitor.refreshRoutes(true)
  return validateSettingsStatus(await monitor.helper({action:'settings_status'}))
}

export async function writeBridgeSetting(monitor, request) {
  if (monitor.stopped || monitor.settingsBusy || monitor.settingsNeedsRefresh || !request ||
      !SETTINGS_ROWS.some(row => row.key === request.key) ||
      typeof request.value !== 'boolean' || typeof request.expected !== 'boolean') {
    throw new NativeHelperError('현재 설정을 변경할 수 없습니다.')
  }
  const frozen = Object.freeze({action:'settings_set', key:request.key,
    value:request.value, expected:request.expected})
  monitor.settingsBusy = true
  let dispatched = false
  try {
    // Fresh read first, but never rebase a user's click onto a changed baseline.
    const fresh = await readBridgeSettings(monitor)
    if (!fresh.ok || !['ready','service_disabled'].includes(fresh.selection_reason)) {
      throw new NativeHelperError('설정이 읽기 전용입니다. 플러그인 선택과 연결을 확인해 주세요.')
    }
    if (fresh.settings[frozen.key] !== frozen.expected) {
      monitor.settingsNeedsRefresh = true
      throw new NativeHelperError('다른 화면에서 설정이 바뀌었습니다. 새로고침 후 다시 선택해 주세요.')
    }
    if (monitor.stopped) throw new NativeHelperError('플러그인이 비활성화되었습니다.')
    // One native write, no automatic retry or fallback transport.
    dispatched = true
    const reply = validateSettingsStatus(await monitor.helper(frozen))
    if (!reply.ok || reply.write_status !== 'verified' || reply.readback?.key !== frozen.key ||
        reply.readback?.value !== frozen.value || reply.cleanup?.ok !== true ||
        reply.settings[frozen.key] !== frozen.value) {
      throw new NativeHelperError('변경 결과를 확정하지 못했습니다. 새로고침하여 실제 값을 확인해 주세요.', true)
    }
    return reply
  } catch (error) {
    if (dispatched) {
      monitor.settingsNeedsRefresh = true
      throw new NativeHelperError('변경 결과를 확정하지 못했습니다. 재시도하지 말고 새로고침하여 실제 값을 확인해 주세요.', true)
    }
    throw error
  } finally { monitor.settingsBusy = false }
}

function BridgeSettingsPage({monitor}) {
  const queryKey = [ID, VERSION, 'settings']
  const query = UI.useQuery({queryKey, queryFn:() => readBridgeSettings(monitor),
    retry:false, staleTime:0, refetchInterval:60_000})
  const client = UI.useQueryClient()
  const mutation = UI.useMutation({mutationFn:request => writeBridgeSetting(monitor, request), retry:false,
    onSuccess:reply => { client.setQueryData(queryKey, reply); void client.invalidateQueries({queryKey}) }})
  const refresh = UI.useMutation({mutationFn:() => readBridgeSettings(monitor), retry:false,
    onSuccess:reply => {
      monitor.settingsNeedsRefresh = false
      mutation.reset()
      client.setQueryData(queryKey, reply)
    }})
  const snapshot = query.data
  const verified = snapshot?.ok === true && snapshot.version === VERSION && !query.error
  const editable = verified && ['ready','service_disabled'].includes(snapshot.selection_reason) &&
    !monitor.stopped && !monitor.settingsNeedsRefresh && !mutation.isPending && !refresh.isPending && !query.isFetching
  const reasonText = snapshot?.selection_reason === 'ready' ? '기존 허용 조건을 충족합니다.' :
    snapshot?.selection_reason === 'service_disabled' ? '전체 스위치가 꺼져 있습니다. 개별 선택값은 보존됩니다.' :
    '원본 설정·플러그인 선택·연결을 확인할 수 없어 변경을 차단했습니다.'
  const row = entry => jsx(UI.ToggleRow, {
    label:entry.label, description:entry.description,
    checked:verified && snapshot.settings[entry.key] === true,
    disabled:!editable,
    onChange:value => mutation.mutate(Object.freeze({key:entry.key, value, expected:snapshot.settings[entry.key]})),
    below:entry.module ? jsx('div', {className:'mt-1 text-xs text-muted-foreground', children:
      !verified ? '상태 확인 대기' : snapshot.effective[entry.module] ? '설정상 사용 가능' :
      snapshot.settings[entry.key] ? '선택값 켜짐 · 공통 스위치 또는 허용 조건으로 실행 차단' : '개별 기능 꺼짐'}) : null
  }, entry.key)
  const section = (title, entries) => jsx('section', {className:'mb-6', children:[
    jsx('h2', {className:'mb-2 text-sm font-medium', children:title}, title),
    ...entries.map(row)
  ]}, title)
  const error = mutation.error || refresh.error || query.error
  return jsx('div', {className:'max-w-3xl px-5 py-4', children:[
    jsx('div', {className:'mb-5 flex flex-wrap items-center gap-3', children:[
      jsx('h1', {className:'text-base font-medium', children:'Telegram Bridge'}, 'title'),
      jsx(UI.Badge, {variant:'muted', children:!verified ? '상태 미확인' :
        snapshot.settings.service_enabled ? '전체 켜짐' : '전체 꺼짐'}, 'master'),
      jsx(UI.Button, {type:'button', size:'sm', variant:'outline', disabled:mutation.isPending || refresh.isPending || query.isFetching,
        onClick:() => refresh.mutate(), children:'새로고침'}, 'refresh')
    ]}, 'header'),
    jsx('p', {className:'mb-4 text-sm text-muted-foreground', children:
      '이 로컬 앱의 모든 프로필에 공통 적용됩니다. 하나를 변경할 때마다 실제 저장값을 다시 확인합니다.'}, 'scope'),
    jsx('div', {className:'mb-5 rounded-md border border-border px-3 py-3 text-sm', children:[
      jsx('div', {children:verified ? `설정 도우미 ${snapshot.version} · 수신 신호 ${snapshot.receiver.heartbeat_ready ? '확인됨' : '미확인 또는 입력 기능 꺼짐'}` :
        '설정 조회 중이거나 조회에 실패했습니다. 꺼짐으로 단정하지 않습니다.'}, 'status'),
      jsx('div', {className:'mt-1 text-xs text-muted-foreground', children:!verified ? '발신 상태 미확인' :
        !snapshot.sender.available ? '발신 상태를 확인할 수 없습니다.' :
        `발신 · 복구 대기 ${snapshot.sender.awaiting_recovery} · 전달 불명 ${snapshot.sender.uncertain_delivery} · 전송 완료 ${snapshot.sender.sent} · 만료 ${snapshot.sender.expired}`}, 'sender-status'),
      jsx('p', {className:'mt-2 text-xs text-muted-foreground', children:reasonText}, 'reason'),
      jsx('p', {className:'mt-2 text-xs text-muted-foreground', children:
        '수신 신호는 Telegram 실제 전송·명령 실행 성공의 증거가 아닙니다. Gateway 장애 중에는 오래된 카드에 답하지 마세요.'}, 'health-boundary')
    ]}, 'diagnostics'),
    error ? jsx('div', {role:'alert', className:'mb-4 rounded-md border border-border px-3 py-3 text-sm', children:
      error instanceof NativeHelperError ? error.message : '설정을 확인하지 못했습니다. 새로고침하여 실제 값을 확인해 주세요.'}, 'error') : null,
    section('전체 제어', [SETTINGS_ROWS[0]]),
    section('알림', SETTINGS_ROWS.slice(1,3)),
    section('Telegram 입력', SETTINGS_ROWS.slice(3,6)),
    section('답장 공통 허용', [SETTINGS_ROWS[6]]),
    jsx('p', {className:'text-xs text-muted-foreground', children:
      'Desktop 플러그인 자체를 끄면 이 전용 화면도 내려갑니다. 다시 켜기는 기본 설정의 플러그인 목록에서 할 수 있습니다.'}, 'desktop-toggle-note'),
    jsx(UI.Button, {type:'button', variant:'text', size:'sm', onClick:() => host.navigate(UI.pluginSettingsHref ? UI.pluginSettingsHref() : '/settings?tab=plugins'),
      children:'Desktop 플러그인 목록 열기'}, 'desktop-toggle')
  ]})
}

export function registerBridgeSettings(ctx, monitor) {
  const render = () => jsx(BridgeSettingsPage, {monitor})
  if (typeof ctx.registerSettingsPage === 'function' && typeof UI.pluginSettingsHref === 'function') {
    ctx.registerSettingsPage({id:'telegram', title:'Telegram Bridge', icon:'bell', render})
  } else if (UI.ROUTES_AREA && UI.SIDEBAR_NAV_AREA && typeof ctx.register === 'function') {
    // Older supported SDKs retain a dedicated route without a core patch.
    const path = '/telegram-bridge-settings'
    ctx.register({id:'telegram-settings-route', area:UI.ROUTES_AREA, data:{path}, render})
    ctx.register({id:'telegram-settings-nav', area:UI.SIDEBAR_NAV_AREA,
      data:{path,label:'Telegram Bridge',codicon:'bell'}})
  }
}

export default {
  id: ID,
  name: 'Session Completion → Telegram',
  description: '모든 로컬 프로필에서 요청 작업이 끝나고 60초간 대기할 때마다 한 번 텔레그램으로 알립니다. 같은 세션의 후속 요청도 포함하며, 작업 중 사용자 승인·질문이 필요해 멈추면 즉시 별도로 알립니다.',
  version: VERSION,
  defaultEnabled: true,
  register(ctx) {
    const monitor = new CompletionMonitor(host, ctx)
    registerBridgeSettings(ctx, monitor)
    ctx.onEvent('*', event => monitor.onEvent(event))
    ctx.onDispose(() => monitor.dispose())
    ctx.setInterval(() => void monitor.sweep(), ATTENTION_POLL_MS)
    void monitor.mounted()
  }
}

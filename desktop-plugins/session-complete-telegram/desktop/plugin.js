import { host } from '@hermes/plugin-sdk'

// App-level disk plugin: no profile configuration, agent prompt or core changes.
export const VERSION = '1.5.0'

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
  'send', 'attention', 'reply_register', 'reply_poll', 'reply_settle',
  'followup_claim', 'followup_begin_submit', 'followup_reject', 'followup_settle',
  'loaded', 'diagnostic'
])

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

  async helper(payload) {
    const routes = await this.refreshRoutes()
    const sender = routes.filter(r => r.mode === 'local' && r.targetProfile === 'default' && r.profile === 'default')
    if (sender.length !== 1) throw new NativeHelperError('Default local sender route unavailable')
    const ambiguousWrite = HELPER_WRITE_ACTIONS.has(payload.action)
    const argv = ['--profile', 'default', 'desktop-telegram-bridge', '--request', encodeRequest(payload)]
    let result
    try {
      // cli.exec's child budget is seconds; SDK's outer deadline is milliseconds.
      result = await this.api.requestProfile(sender[0], 'cli.exec', {argv, timeout: 25}, 35_000)
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

  async mounted() {
    if (this.stopped || this.loadedRecorded) return
    try {
      const routes = await this.refreshRoutes()
      const reply = await this.helper({action: 'loaded', profiles: routes.map(r => r.profile), version: VERSION})
      this.loadedRecorded = reply.ok === true
    } catch {
      // Startup may precede gateway.ready. It is retried by that event, not by
      // launching all profile backends or restarting the user's current session.
    }
  }

  invalidate(record) {
    record.generation++
    record.candidate = null
    record.cancel?.()
    record.cancel = null
  }

  schedule(record, delay = QUIET_MS) {
    record.cancel?.()
    record.cancel = this.ctx.setTimeout(() => { void this.check(record) }, delay)
  }

  onEvent(event) {
    if (this.stopped) return
    if (event.type === 'gateway.ready') { void this.mounted(); return }
    if (event.replayed || !event.session_id) return
    const interesting = ['message.start', 'message.complete', 'tool.start', 'tool.complete', 'todo.updated', 'subagent.start', 'subagent.complete', 'error', 'session.control.update']
    if (!interesting.includes(event.type)) return
    const profile = event.profile || this.api.state.profile.get()
    const connectionId = event.connectionId || ''
    const key = JSON.stringify([connectionId, profile, event.session_id])
    let r = this.records.get(key)
    if (!r) {
      r = { key, profile, connectionId, sid: event.session_id, generation: 0, candidate: null, cancel: null, todos: null, seq: 0, settledFinals: new Set(), checking: false, retries: 0, ask: null, askChecking: false }
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
      this.invalidate(r)
      if (p.status !== 'complete' || p.error || p.failure_reason || p.partial || !Number.isInteger(finalId)) return
      r.candidate = { at: this.now(), finalId, seq: r.seq }
      r.retries = 0
      this.schedule(r)
      return
    }
    // A child's late completion invalidates the waiting parent's final. Only
    // the parent's subsequent synthesis/final response may re-arm notification.
    if (event.type === 'session.control.update') {
      if (unfinishedControl(p.control)) this.invalidate(r)
      else if (r.candidate) {
        // The goal judge publishes AFTER message.complete. A done/cleared
        // verdict is the final bookkeeping, not another unfinished turn.
        r.candidate.at = this.now()
        this.schedule(r)
      }
      return
    }
    this.invalidate(r)
  }

  async check(r) {
    if (this.stopped || !r.candidate || r.checking) return
    const generation = r.generation
    const candidate = r.candidate
    if (this.now() - candidate.at < QUIET_MS) { this.schedule(r, QUIET_MS - (this.now() - candidate.at)); return }
    const current = () => !this.stopped && generation === r.generation && r.candidate === candidate
    r.checking = true
    try {
      const route = await this.routeFor(r)
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
      const workEvents = new Set(['message.start', 'message.complete', 'tool.start', 'tool.complete', 'todo.updated', 'subagent.start', 'subagent.complete', 'error'])
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
      } else if (current() && ++r.retries <= 3) this.schedule(r, 20_000)
      else if (current()) {
        this.invalidate(r)
        this.api.notify({kind: 'warning', title: '세션 완료 알림', message: '완료 상태를 확인하지 못해 알림을 보내지 않았습니다.'})
      }
    } finally {
      r.checking = false
      if (!this.stopped && r.candidate && r.candidate !== candidate) {
        this.schedule(r, Math.max(0, QUIET_MS - (this.now() - r.candidate.at)))
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
    for (const record of this.records.values()) record.sweepAbsent = false
    try {
      const byRoute = new Map()
      for (const r of [...this.records.values()]) {
        try {
          const route = await this.routeFor(r)
          if (!byRoute.has(route)) byRoute.set(route, [])
          byRoute.get(route).push(r)
        } catch {
          // Route unavailable right now: keep the record, retry next sweep.
        }
      }
      for (const [route, records] of byRoute) {
        let active
        let rows
        try {
          active = await this.api.requestProfile(route, 'session.active_list', {})
          rows = validateActiveSessions(active)
        } catch {
          for (const r of records) if (r.reply) this.replySweepUnverified.add(r.reply.id)
          continue
        }
        for (const r of records) {
          if (this.stopped) return
          const row = rows.find(s => s.id === r.sid)
          if (!row) {
            // A healthy, validated list is definitive absence, but keep the
            // bridge until reply_poll retires it. Incomplete lists never reach
            // this branch.
            if (r.reply) this.replySweepAbsent.add(r.reply.id)
            r.sweepAbsent = true
            if (!r.reply) this.records.delete(r.key)
            continue
          }
          r.sweepAbsent = false
          if (row.status !== 'waiting') { r.ask = null; continue }
          await this.checkAsk(r, route, row)
        }
      }
      await this.pumpReplies()
      if (this.lastReplyPollSucceeded) {
        for (const r of [...this.records.values()]) {
          if (r.sweepAbsent) this.records.delete(r.key)
        }
      }
      await this.pumpFollowups()
    } finally {
      this.replySweepUnverified.clear()
      this.replySweepAbsent.clear()
      this.sweeping = false
    }
  }

  async checkAsk(r, route, row) {
    if (this.stopped || r.askChecking) return
    r.askChecking = true
    let currentAskId = null
    let currentAskFingerprint = null
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
        const previous = r.reply
        if (previous && !['connectionId','profile','targetProfile','sid','session_key','epoch','request_id']
          .every(key => previous.descriptor[key] === descriptor[key])) {
          // A different durable request identity must retire the old bridge
          // before a new registration can replace its local reference.
          this.replySweepAbsent.add(previous.id)
          return
        }
        const reply = await this.helper({action:'reply_register', descriptor, ask})
        if (reply.ok && reply.new === true && reply.id) {
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
          // A duplicate is evidence of a permanent prior send claim, not
          // authority to bind this monitor to terminal or prior-owner data.
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
      }
      if (!ask || (r.ask && r.ask.id === ask.id)) return
      const reply = await this.helper({
        action: 'attention', kind: 'ask', profile: route.targetProfile, session_key: row.session_key,
        ask_id: ask.id, ask_method: ask.method, ask_params: ask.params || {}, desktop_observed: true
      })
      // Any terminal answer settles the episode: gate rejections are
      // deterministic and an ambiguous send must never be retried. Only the
      // transport-failure catch below re-attempts while the ask stays open.
      r.ask = { id: ask.id }
      if (!reply?.ok && !reply?.duplicate && ['failed', 'unknown'].includes(reply?.status)) {
        this.api.notify({ kind: 'warning', title: '입력 대기 알림', message: '텔레그램 전송을 확인하지 못했습니다. 중복 방지를 위해 자동 재전송하지 않습니다.' })
      }
    } catch (error) {
      if (currentAskId && error instanceof NativeHelperError && error.permanent) {
        r.helperWriteBlockedFingerprint = currentAskFingerprint
        r.ask = {id: currentAskId}
        this.api.notify({kind: 'warning', title: '질문 응답 전송 제한', message: '질문이 로컬 전송 크기 제한을 초과해 텔레그램 연결을 시작하지 않았습니다. 질문을 짧게 줄여 다시 요청해 주세요.'})
      } else if (currentAskId && error instanceof NativeHelperError && error.ambiguousWrite) {
        r.helperWriteBlockedAskId = currentAskId
        r.ask = {id: currentAskId}
        this.api.notify({kind: 'warning', title: '입력 대기 알림', message: '결과를 확인하지 못했습니다. 중복 방지를 위해 다시 시도하지 않습니다.'})
      }
      // A transport failure before a native write is retried on the next sweep.
    } finally {
      r.askChecking = false
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

  async pumpReplies() {
    this.lastReplyPollSucceeded = false
    if (!this.hadReplies || this.stopped) return
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
      observations:[...bridges.values()].map(b => ({id:b.id})), unverified}) } catch { return }
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

export default {
  id: ID,
  name: 'Session Completion → Telegram',
  description: '모든 로컬 프로필에서 요청 작업이 끝나고 60초간 대기할 때마다 한 번 텔레그램으로 알립니다. 같은 세션의 후속 요청도 포함하며, 작업 중 사용자 승인·질문이 필요해 멈추면 즉시 별도로 알립니다.',
  version: VERSION,
  defaultEnabled: true,
  register(ctx) {
    const monitor = new CompletionMonitor(host, ctx)
    ctx.onEvent('*', event => monitor.onEvent(event))
    ctx.onDispose(() => monitor.dispose())
    ctx.setInterval(() => void monitor.sweep(), ATTENTION_POLL_MS)
    void monitor.mounted()
  }
}

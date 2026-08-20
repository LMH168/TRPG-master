/** Phase 1C《追书人》真实 HTTP 回合门禁，覆盖路线、结局和幂等回放。 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { createRoomWithModule } from './helpers.ts'

const API_BASE = `${process.env.E2E_BASE_URL ?? 'http://127.0.0.1:8000'}/api/v1`

interface Envelope<T> {
  success: boolean
  data: T | null
  error: { code: string; message: string } | null
}

interface CommandResult {
  revision: number
  projection: { sceneId?: string; endingId?: string | null }
}

/** 通过新 GM REST 边界完成墓地调查、识别人影并提交和平结局。 */
test('Phase 1C：HTTP 回合可以完成《追书人》和平结局并安全回放', async () => {
  const room = await createRoomWithModule('gm-phase1c', 1)
  const headers = {
    Authorization: `Bearer ${room.host.token}`,
    'X-Reconnect-Token': room.reconnectToken,
    'Content-Type': 'application/json',
  }
  const sessionResponse = await fetch(`${API_BASE}/gm/sessions`, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      roomId: room.roomId,
      moduleId: 'paper-chase',
      actorId: room.hostPlayerId,
      displayName: 'E2E 调查员',
    }),
  })
  assert.equal(sessionResponse.status, 201)

  async function command(clientRequestId: string, expectedRevision: number, command: object) {
    const response = await fetch(`${API_BASE}/gm/sessions/${room.roomId}/turns`, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        clientRequestId,
        expectedRevision,
        actorId: room.hostPlayerId,
        command,
      }),
    })
    assert.equal(response.status, 200)
    const envelope = (await response.json()) as Envelope<CommandResult>
    assert.equal(envelope.success, true)
    assert.ok(envelope.data)
    return envelope.data
  }

  const moved = await command('phase1c-move', 0, {
    kind: 'move_actor',
    targetId: 'cemetery',
  })
  const night = await command('phase1c-night', moved.revision, {
    kind: 'wait_until',
    targetTime: '1920-09-15T20:00:00-04:00',
  })
  const started = await command('phase1c-watch-start', night.revision, {
    kind: 'start_check',
    checkId: 'phase1c-night-watch',
    skillId: 'luck',
    goal: 'night_watch',
  })
  const watched = await command('phase1c-watch-roll', started.revision, {
    kind: 'roll_check',
    checkId: 'phase1c-night-watch',
  })
  const identified = await command('phase1c-identify', watched.revision, {
    kind: 'inspect_target',
    targetId: 'call_douglas',
  })
  const ended = await command('phase1c-ending', identified.revision, {
    kind: 'choose_option',
    optionId: 'peaceful_resolution',
  })
  const replay = await command('phase1c-ending', 0, {
    kind: 'choose_option',
    optionId: 'peaceful_resolution',
  })
  assert.equal(ended.projection.endingId, 'peaceful_resolution')
  assert.equal(replay.revision, ended.revision)
  assert.equal(replay.projection.endingId, ended.projection.endingId)
})

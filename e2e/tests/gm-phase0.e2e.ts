/** Phase 0 新 GM 会话、Wait 命令和幂等回执端到端验证。 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { createRoomWithModule } from './helpers.ts'

const API_BASE = `${process.env.E2E_BASE_URL ?? 'http://127.0.0.1:8000'}/api/v1`

interface Envelope<T> {
  success: boolean
  data: T | null
  error: { code: string; message: string } | null
}

interface SessionResult {
  sessionId: string
  projection: { actorId: string; revision: number; worldTime: string }
}

interface CommandResult {
  revision: number
  events: Array<{ eventId: string; eventType: string }>
}

/** 使用真实 HTTP 边界创建会话并验证重复命令不会产生第二个事件。 */
test('Phase 0：建房后提交 Wait，重复请求返回同一回执', async () => {
  const room = await createRoomWithModule('gm-phase0', 1)
  const actorId = room.hostPlayerId
  const sessionResponse = await fetch(`${API_BASE}/gm/sessions`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${room.host.token}`,
      'Content-Type': 'application/json',
      'X-Reconnect-Token': room.reconnectToken,
    },
    body: JSON.stringify({
      roomId: room.roomId,
      moduleId: 'paper-chase',
      actorId,
      displayName: 'E2E 调查员',
    }),
  })
  assert.equal(sessionResponse.status, 201)
  const sessionEnvelope = (await sessionResponse.json()) as Envelope<SessionResult>
  assert.equal(sessionEnvelope.success, true)
  assert.ok(sessionEnvelope.data)

  const targetTime = new Date(
    new Date(sessionEnvelope.data.projection.worldTime).getTime() + 2 * 60 * 60 * 1000,
  ).toISOString()
  const submit = () => fetch(`${API_BASE}/gm/sessions/${room.roomId}/turns`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Reconnect-Token': room.reconnectToken,
    },
    body: JSON.stringify({
      clientRequestId: 'e2e-wait-1',
      expectedRevision: 0,
      actorId,
      command: { kind: 'wait_until', targetTime },
    }),
  })
  const firstResponse = await submit()
  const secondResponse = await submit()
  assert.equal(firstResponse.status, 200)
  assert.equal(secondResponse.status, 200)
  const first = (await firstResponse.json()) as Envelope<CommandResult>
  const second = (await secondResponse.json()) as Envelope<CommandResult>
  assert.equal(first.data?.revision, 1)
  assert.equal(second.data?.revision, 1)
  assert.equal(first.data?.events[0]?.eventId, second.data?.events[0]?.eventId)
  assert.equal(first.data?.events[0]?.eventType, 'time_advanced')
})

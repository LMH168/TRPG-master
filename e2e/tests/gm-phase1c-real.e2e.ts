/** 真实 provider 的《追书人》自然语言完整局门禁；默认跳过，避免 CI 产生外部调用费用。 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { createRoomWithModule } from './helpers.ts'

const API_BASE = `${process.env.E2E_BASE_URL ?? 'http://127.0.0.1:8000'}/api/v1`

interface Envelope<T> {
  success: boolean
  data: T | null
  error: { code: string; message: string } | null
}

interface TurnResult {
  status: string
  revision: number
  narration?: string | null
  commandResult?: { projection: { endingId?: string | null } }
}

test('真实模型：自然语言完成《追书人》和平结局', { skip: process.env.E2E_REAL_MODEL !== '1' }, async () => {
  const room = await createRoomWithModule('gm-real-paper-chase', 1)
  const headers = {
    Authorization: `Bearer ${room.host.token}`,
    'X-Reconnect-Token': room.reconnectToken,
    'Content-Type': 'application/json',
  }
  const session = await fetch(`${API_BASE}/gm/sessions`, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      roomId: room.roomId,
      moduleId: 'paper-chase',
      actorId: room.hostPlayerId,
      displayName: '真实模型调查员',
    }),
  })
  assert.equal(session.status, 201)

  let revision = 0
  let requestNumber = 0
  async function freeText(input: string): Promise<TurnResult> {
    const requestId = `real-paper-${++requestNumber}`
    const response = await fetch(`${API_BASE}/gm/sessions/${room.roomId}/turns/free-text`, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        clientRequestId: requestId,
        actorId: room.hostPlayerId,
        expectedRevision: revision,
        input,
      }),
    })
    const envelope = (await response.json()) as Envelope<TurnResult>
    assert.equal(response.status, 200, JSON.stringify(envelope))
    assert.equal(envelope.success, true)
    assert.ok(envelope.data)
    revision = envelope.data.revision
    return envelope.data
  }

  await freeText('我想前往墓地调查')
  await freeText('我呼喊道格拉斯的名字，想和他谈谈')
  const ending = await freeText('我选择礼貌地交谈后离开')
  assert.equal(ending.status, 'completed')
  assert.equal(ending.commandResult?.projection.endingId, 'peaceful_resolution')
})


/**
 * 房间容量约束 + WebSocket 的端到端验证。
 *
 * 当前唯一真实模组《追书人》只支持一名调查员，因此这里不再依赖已删除的多人
 * 假模组。多人边界保留在房间服务的单元测试中；这组 E2E 验证真实模组会收紧
 * 房间容量，以及单人房间的完整实时协议。
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'

import type { ServerToClientEvent } from 'trpg-sdk'

import { createRoomWithModule, legalCharacterPayload, registerPlayer } from './helpers.ts'

const LEGAL_ATTRIBUTES = {
  STR: 50, CON: 50, POW: 50, DEX: 50,
  APP: 50, SIZ: 50, INT: 50, EDU: 50, LUCK: 50,
}

/** 等一个满足条件的服务端事件，超时就失败——不要用固定 sleep。 */
function waitForEvent(
  socketOwner: { roomSocket: { onMessage: (h: (e: ServerToClientEvent) => void) => () => void } },
  predicate: (event: ServerToClientEvent) => boolean,
  timeoutMs = 5_000
): Promise<ServerToClientEvent> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      off()
      reject(new Error(`等待事件超时（${timeoutMs}ms）`))
    }, timeoutMs)
    const off = socketOwner.roomSocket.onMessage((event) => {
      if (!predicate(event)) return
      clearTimeout(timer)
      off()
      resolve(event)
    })
  })
}

/** 建好角色卡并标记完成——`game.start` 要求全员建完卡。 */
async function buildCharacter(
  sdk: Awaited<ReturnType<typeof registerPlayer>>['sdk'],
  roomId: string,
  reconnectToken: string
): Promise<void> {
  const draft = await sdk.characters.createDraft(roomId, reconnectToken)
  await sdk.characters.save(
    roomId,
    draft.characterId,
    legalCharacterPayload(LEGAL_ATTRIBUTES),
    reconnectToken
  )
  await sdk.characters.complete(roomId, draft.characterId, reconnectToken)
}

test('选择《追书人》后房间容量收紧为一人，并拒绝第二名玩家', async () => {
  const room = await createRoomWithModule('mp')
  const guest = await registerPlayer('guest')

  const preview = await room.host.sdk.rooms.getInfo(room.roomCode)
  assert.equal(preview.maxPlayers, 1)
  assert.equal(preview.players.length, 1)
  await assert.rejects(
    guest.sdk.rooms.join(room.roomCode, { nickname: '访客' }, guest.token)
  )
})

test('WS 生命周期：join → session.bound，完成建卡后房主可以 game.start', async () => {
  const room = await createRoomWithModule('ws')

  // 房间生命周期是 Lobby →(start_story)→ Building →(game.start)→ InGame。
  // 少了 start_story 这步，房间还在 Lobby，game.start 会被拒——第一版就是漏了
  // 它，现象是干等 session.bound 之后的旁白直到超时。
  await room.host.sdk.rooms.startStory(room.roomId, room.reconnectToken)

  // 当前真实模组是单人模组，房主完成建卡后即可开始。
  await buildCharacter(room.host.sdk, room.roomId, room.reconnectToken)

  // ⚠️ `try` 必须从 **connect() 之后的第一行**就开始，把 waitForOpen 和绑定
  // 阶段也罩进去。这两步同样会失败/超时，而句柄那时已经建立了——漏在 try 外面
  // 的话 disconnect() 不会执行，WS 句柄会让 node 一直不退出，表现成"测试跑完了
  // 但命令挂住"，最后只能等 job 超时。
  const hostSocket = room.host.sdk.roomSocket.connect(room.roomId, room.host.token)
  try {
    await room.host.sdk.roomSocket.waitForOpen(hostSocket)

    const bound = waitForEvent(room.host.sdk, (e) => e.type === 'session.bound')
    room.host.sdk.roomSocket.joinRoom(room.hostPlayerId, {
      reconnectToken: room.reconnectToken,
    })
    const boundEvent = await bound
    assert.equal(boundEvent.type, 'session.bound')

    // 房主开始游戏 → 应该收到开场旁白
    const narration = waitForEvent(room.host.sdk, (e) => e.type === 'narration.push')
    room.host.sdk.roomSocket.startGame(room.hostPlayerId)
    const narrationEvent = await narration
    assert.equal(narrationEvent.type, 'narration.push')
  } finally {
    room.host.sdk.roomSocket.disconnect()
  }
})

test('提交行动使用 GameView 版本，并收到带请求 ID 的结果广播', async () => {
  const room = await createRoomWithModule('broadcast')

  await room.host.sdk.rooms.startStory(room.roomId, room.reconnectToken)
  await buildCharacter(room.host.sdk, room.roomId, room.reconnectToken)

  const hostSocket = room.host.sdk.roomSocket.connect(room.roomId, room.host.token)
  try {
    await room.host.sdk.roomSocket.waitForOpen(hostSocket)
    room.host.sdk.roomSocket.joinRoom(room.hostPlayerId, {
      reconnectToken: room.reconnectToken,
    })
    await waitForEvent(room.host.sdk, (e) => e.type === 'session.bound')

    const initialView = waitForEvent(room.host.sdk, (e) => e.type === 'game.view')
    room.host.sdk.roomSocket.startGame(room.hostPlayerId)
    const viewEvent = await initialView
    assert.equal(viewEvent.type, 'game.view')

    const clientActionId = `broadcast-${Date.now()}`
    const actionResult = waitForEvent(
      room.host.sdk,
      (e) => e.type === 'narration.push' && e.payload.requestId === clientActionId
    )
    room.host.sdk.roomSocket.submitAction(room.hostPlayerId, {
      clientActionId,
      utterance: '我观察房间',
      sourceRevision: viewEvent.payload.stateRevision,
    })
    await actionResult
  } finally {
    room.host.sdk.roomSocket.disconnect()
  }
})

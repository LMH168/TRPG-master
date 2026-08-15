/** 可靠回合 REST 资源的路径、过滤参数与房间凭证测试。 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { ApiClient } from '../client';
import { TurnsResource } from './turns';

function fixture() {
  const requests: Array<{ url: string; init?: RequestInit }> = [];
  const fetchImpl: typeof fetch = async (input, init) => {
    requests.push({ url: String(input), init });
    return new Response(JSON.stringify({ success: true, data: [], error: null }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };
  return {
    turns: new TurnsResource(new ApiClient({ baseUrl: 'https://example.test/api/v1', fetch: fetchImpl })),
    requests,
  };
}

test('listTurns 使用查询参数和房间重连凭证', async () => {
  const { turns, requests } = fixture();
  await turns.listTurns('room/1', 'room-secret', {
    clientActionId: 'action 1',
    activeOnly: true,
    limit: 1,
  });

  assert.equal(
    requests[0]?.url,
    'https://example.test/api/v1/rooms/room%2F1/turns?clientActionId=action+1&activeOnly=true&limit=1'
  );
  assert.equal(new Headers(requests[0]?.init?.headers).get('X-Reconnect-Token'), 'room-secret');
});

test('findTurnByClientAction 在没有匹配项时返回 null', async () => {
  const { turns } = fixture();
  assert.equal(await turns.findTurnByClientAction('room-1', 'action-1', 'token'), null);
});

test('resumeTurn 使用稳定 turnId 的恢复端点', async () => {
  const { turns, requests } = fixture();
  await turns.resumeTurn('room-1', 'turn/1', 'token');
  assert.equal(
    requests[0]?.url,
    'https://example.test/api/v1/rooms/room-1/turns/turn%2F1/resume'
  );
  assert.equal(requests[0]?.init?.method, 'POST');
});

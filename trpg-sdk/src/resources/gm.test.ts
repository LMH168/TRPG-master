/** GM REST 资源的请求路径和房间身份契约测试。 */

import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiClient } from '../client';
import { GmResource } from './gm';

test('GM 自然语言回合携带重连凭证和稳定幂等字段', async () => {
  const requests: Request[] = [];
  const client = new ApiClient({
    baseUrl: 'https://example.test/api/v1',
    fetch: async (input, init) => {
      requests.push(new Request(input, init));
      return new Response(JSON.stringify({
        success: true,
        data: { clientRequestId: 'turn-1', status: 'clarification', revision: 0 },
        error: null,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    },
  });
  await new GmResource(client).submitFreeText(
    'room/1',
    { clientRequestId: 'turn-1', actorId: 'actor-1', expectedRevision: 0, input: '过个侦察' },
    'room-secret',
  );
  assert.equal(requests[0]?.url, 'https://example.test/api/v1/gm/sessions/room%2F1/turns/free-text');
  assert.equal(requests[0]?.headers.get('X-Reconnect-Token'), 'room-secret');
  assert.deepEqual(await requests[0]?.clone().json(), {
    clientRequestId: 'turn-1',
    actorId: 'actor-1',
    expectedRevision: 0,
    input: '过个侦察',
  });
});

test('GM 会话创建同时携带账号和房间身份', async () => {
  const requests: Request[] = [];
  const client = new ApiClient({
    baseUrl: 'https://example.test/api/v1',
    fetch: async (input, init) => {
      requests.push(new Request(input, init));
      return new Response(JSON.stringify({
        success: true,
        data: {
          sessionId: 'room-1',
          moduleId: 'paper-chase',
          moduleVersion: 'source-1',
          projection: {},
        },
        error: null,
      }), { status: 201, headers: { 'Content-Type': 'application/json' } });
    },
  });
  await new GmResource(client).createSession(
    { roomId: 'room-1', moduleId: 'paper-chase', actorId: 'actor-1', displayName: '调查员' },
    'room-secret',
    'account-secret',
  );
  assert.equal(requests[0]?.headers.get('Authorization'), 'Bearer account-secret');
  assert.equal(requests[0]?.headers.get('X-Reconnect-Token'), 'room-secret');
});

test('GM 投影查询使用后端约定的 actor_id 参数', async () => {
  const requests: Request[] = [];
  const client = new ApiClient({
    baseUrl: 'https://example.test/api/v1',
    fetch: async (input, init) => {
      requests.push(new Request(input, init));
      return new Response(JSON.stringify({
        success: true,
        data: { revision: 0, checks: [] },
        error: null,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    },
  });
  await new GmResource(client).getProjection('room-1', 'actor-1', 'room-secret');
  assert.equal(
    requests[0]?.url,
    'https://example.test/api/v1/gm/sessions/room-1/projection?actor_id=actor-1',
  );
});

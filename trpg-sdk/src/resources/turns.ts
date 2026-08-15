/**
 * 可靠回合 REST 资源。
 *
 * WebSocket 只负责实时进度；刷新、断线和投递失败后的权威恢复统一读取这里。
 */
import type { ApiClient } from '../client';
import type { TurnRead } from '../types';

export interface ListTurnsOptions {
  clientActionId?: string;
  activeOnly?: boolean;
  limit?: number;
}

export class TurnsResource {
  constructor(private readonly client: ApiClient) {}

  /** 房间重连凭证代表当前玩家身份，不能使用账号 token 替代。 */
  private roomAuth(reconnectToken: string): RequestInit {
    return { headers: { 'X-Reconnect-Token': reconnectToken } };
  }

  /** 读取一个稳定 turnId 对应的玩家安全结果。 */
  getTurn(roomId: string, turnId: string, reconnectToken: string): Promise<TurnRead> {
    return this.client.get<TurnRead>(
      `/rooms/${encodeURIComponent(roomId)}/turns/${encodeURIComponent(turnId)}`,
      this.roomAuth(reconnectToken)
    );
  }

  /** 列出当前玩家自己的回合，可按幂等键或活动状态筛选。 */
  listTurns(
    roomId: string,
    reconnectToken: string,
    options: ListTurnsOptions = {}
  ): Promise<TurnRead[]> {
    const params = new URLSearchParams();
    if (options.clientActionId) params.set('clientActionId', options.clientActionId);
    if (options.activeOnly !== undefined) params.set('activeOnly', String(options.activeOnly));
    if (options.limit !== undefined) params.set('limit', String(options.limit));
    const query = params.size > 0 ? `?${params.toString()}` : '';
    return this.client.get<TurnRead[]>(
      `/rooms/${encodeURIComponent(roomId)}/turns${query}`,
      this.roomAuth(reconnectToken)
    );
  }

  /** 用 clientActionId 找回幂等创建的同一回合。 */
  async findTurnByClientAction(
    roomId: string,
    clientActionId: string,
    reconnectToken: string
  ): Promise<TurnRead | null> {
    const turns = await this.listTurns(roomId, reconnectToken, {
      clientActionId,
      limit: 1,
    });
    return turns[0] ?? null;
  }

  /** 请求服务端按持久化恢复点继续推进，不重复执行已提交步骤。 */
  resumeTurn(roomId: string, turnId: string, reconnectToken: string): Promise<TurnRead> {
    return this.client.post<TurnRead>(
      `/rooms/${encodeURIComponent(roomId)}/turns/${encodeURIComponent(turnId)}/resume`,
      null,
      this.roomAuth(reconnectToken)
    );
  }
}

/** `/api/v1/gm` 的安全自然语言回合资源。 */

import type { ApiClient } from '../client';
import type {
  CommandEnvelope,
  CommandResult,
  PlayerProjection,
  SessionCreateBody,
  SessionRead,
  TurnInputBody,
  GmTurnRead,
} from '../generated/dto';

export class GmResource {
  constructor(private readonly client: ApiClient) {}

  /** 房间重连凭证只代表当前玩家，不能替代账号凭证。 */
  private roomAuth(reconnectToken: string): RequestInit {
    return { headers: { 'X-Reconnect-Token': reconnectToken } };
  }

  /** 创建或恢复固定模组版本的 GM 会话。 */
  createSession(
    payload: SessionCreateBody,
    reconnectToken: string,
    accountToken: string,
  ): Promise<SessionRead> {
    return this.client.post<SessionRead>('/gm/sessions', payload, {
      headers: {
        Authorization: `Bearer ${accountToken}`,
        'X-Reconnect-Token': reconnectToken,
      },
    });
  }

  /** 刷新时读取当前玩家投影。 */
  getProjection(
    roomId: string,
    actorId: string,
    reconnectToken: string,
  ): Promise<PlayerProjection> {
    const query = `?actorId=${encodeURIComponent(actorId)}`;
    return this.client.get<PlayerProjection>(
      `/gm/sessions/${encodeURIComponent(roomId)}/projection${query}`,
      this.roomAuth(reconnectToken),
    );
  }

  /** 提交一回合自然语言，返回澄清或 Kernel 权威结果。 */
  submitFreeText(
    roomId: string,
    payload: TurnInputBody,
    reconnectToken: string,
  ): Promise<GmTurnRead> {
    return this.client.post<GmTurnRead>(
      `/gm/sessions/${encodeURIComponent(roomId)}/turns/free-text`,
      payload,
      this.roomAuth(reconnectToken),
    );
  }

  /** 提交服务端权威命令；投骰请求只能携带 checkId，不能携带骰点。 */
  submitCommand(
    roomId: string,
    payload: CommandEnvelope,
    reconnectToken: string,
  ): Promise<CommandResult> {
    return this.client.post<CommandResult>(
      `/gm/sessions/${encodeURIComponent(roomId)}/turns`,
      payload,
      this.roomAuth(reconnectToken),
    );
  }
}

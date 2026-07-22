import { providerLabel } from '../../lib/monopolyRoom';
import type { AgentStatus, MonopolyActor, MonopolyRoom, ParsedBoard, PendingDecision, ProviderSnapshot } from '../../lib/monopolyTypes';

const ACTOR_META: Record<MonopolyActor, { name: string; symbol: string; className: string }> = {
  haya: { name: '哈娅', symbol: '♥', className: 'haya' },
  cc: { name: 'CC', symbol: '✦', className: 'cc' },
  codex: { name: 'Codex', symbol: '◆', className: 'codex' },
};

function agentReady(status?: AgentStatus): boolean {
  if (!status || Object.keys(status).length === 0) return true;
  if (typeof status.ready === 'boolean') return status.ready;
  return !['offline', 'error', 'signed_out'].includes(String(status.state ?? '').toLowerCase());
}

export function PlayerRail({
  room, board, pending, providers, agents, streaming,
}: {
  room: MonopolyRoom;
  board: ParsedBoard;
  pending: PendingDecision | null;
  providers: Partial<Record<MonopolyActor, ProviderSnapshot | undefined>>;
  agents: Record<'cc' | 'codex', AgentStatus>;
  streaming: Partial<Record<'cc' | 'codex', string>>;
}) {
  return (
    <section className="mono-player-rail" aria-label="房间席位">
      {(['haya', 'cc', 'codex'] as const).map((actor) => {
        const meta = ACTOR_META[actor];
        const observer = actor === 'codex';
        const active = room.active_actor === actor && !observer && !['paused', 'finished', 'engine_down'].includes(room.status);
        const ready = observer ? agentReady(agents.codex) : actor === 'cc' ? agentReady(agents.cc) : true;
        const player = actor === 'haya' ? board.haya : actor === 'cc' ? board.cc : null;
        const provider = actor === 'haya'
          ? '真人 · 玩家一'
          : actor === 'codex'
            ? 'OpenAI Official · Codex'
            : providerLabel(providers.cc) || '跟随主聊天线路';
        const status = observer
          ? (ready ? '观察席' : '等待官端登录')
          : room.status === 'paused'
            ? '已暂停'
            : room.status === 'finished'
              ? '终局'
              : room.status === 'engine_down'
                ? '棋盘冻结'
                : pending?.actor === actor
                  ? '悬账处理中'
                  : active ? '行动中' : '等待';
        return (
          <article key={actor} className={`mono-player-card ${meta.className} ${active ? 'active' : ''} ${ready ? '' : 'offline'}`}>
            <div className="mono-player-name-row">
              <span className="mono-player-avatar">{meta.symbol}</span>
              <strong>{meta.name}</strong>
              {!!streaming[actor as 'cc' | 'codex'] && <i className="mono-typing-dot" />}
            </div>
            <span className="mono-player-provider" title={provider}>{provider}</span>
            <div className="mono-player-stat-row">
              {player && <span className="mono-player-coins">⛁ {player.coins}</span>}
              <span className="mono-player-status">{status}</span>
            </div>
            {player && <span className="mono-player-identity">{player.identity === '未发放' ? '身份待发放' : player.identity}</span>}
          </article>
        );
      })}
    </section>
  );
}

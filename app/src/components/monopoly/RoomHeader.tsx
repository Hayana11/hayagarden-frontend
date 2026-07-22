import type { MonopolyRoom, MonopolyRoomStatus } from '../../lib/monopolyTypes';

function PauseIcon({ paused }: { paused: boolean }) {
  return paused ? (
    <svg viewBox="0 0 24 24" width={15} height={15} fill="currentColor" aria-hidden="true"><path d="M8 5l12 7-12 7z" /></svg>
  ) : (
    <svg viewBox="0 0 24 24" width={15} height={15} fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" aria-hidden="true"><path d="M10 5v14M15 5v14" /></svg>
  );
}

function SlidersIcon() {
  return (
    <svg viewBox="0 0 24 24" width={15} height={15} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" aria-hidden="true">
      <path d="M4 7h16M4 16h16" /><circle cx={9} cy={7} r={2.4} fill="var(--mono-card)" /><circle cx={15} cy={16} r={2.4} fill="var(--mono-card)" />
    </svg>
  );
}

const STATUS_LABEL: Partial<Record<MonopolyRoomStatus, string>> = {
  lobby: '等待开局', setup: '设置中', paused: '局面已保存', engine_down: '引擎离线', finished: '本局结束',
};

export function RoomHeader({
  room, round, maxRound, connection, busy, onPause, onSetup, onLeave,
}: {
  room: MonopolyRoom;
  round: number;
  maxRound: number;
  connection: 'connecting' | 'open' | 'closed';
  busy: boolean;
  onPause: () => void;
  onSetup: () => void;
  onLeave: () => void;
}) {
  const paused = room.status === 'paused';
  const engineLabel = room.status === 'engine_down'
    ? '游戏服务暂时离线'
    : STATUS_LABEL[room.status] ?? (connection === 'open' ? '引擎在线' : connection === 'connecting' ? '正在连接' : '连接中断');

  return (
    <header className="mono-room-header">
      <div className="mono-room-header-inner">
        <button type="button" className="mono-icon-button mono-leave-button" onClick={onLeave} aria-label="返回通讯录">‹</button>
        <div className="mono-room-mark" aria-hidden="true">⚄</div>
        <div className="mono-room-heading">
          <div className="mono-room-title-line">
            <span>大富翁游戏室</span>
            <i className={`mono-live-dot ${connection === 'open' && room.status !== 'engine_down' ? '' : 'offline'}`} />
          </div>
          <span className="mono-room-subtitle">葡萄海 · #{room.id.slice(0, 6).toUpperCase()} · {engineLabel}</span>
        </div>
        <div className="mono-room-header-actions">
          <span className="mono-round-chip">第 {round || 0} / {maxRound || 12} 回合</span>
          {room.game_id && room.status !== 'finished' && room.status !== 'engine_down' && (
            <button type="button" className={`mono-icon-button ${paused ? 'paused' : ''}`} onClick={onPause} disabled={busy} aria-label={paused ? '恢复对局' : '暂停对局'}>
              <PauseIcon paused={paused} />
            </button>
          )}
          <button type="button" className="mono-icon-button" onClick={onSetup} aria-label="打开设置"><SlidersIcon /></button>
        </div>
      </div>
    </header>
  );
}

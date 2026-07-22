import type { MonopolyActor, MonopolyRoomStatus, ParsedBoard } from '../../lib/monopolyTypes';

const GRID = [[1,1],[1,2],[1,3],[1,4],[1,5],[1,6],[1,7],[1,8],[2,8],[3,8],[4,8],[4,7],[4,6],[4,5],[4,4],[4,3],[4,2],[4,1],[3,1],[2,1]];
const TILE_META: Record<string, { name: string; symbol: string }> = {
  start: { name: '起点', symbol: '◉' }, task: { name: '任务', symbol: '✦' }, truth: { name: '真心', symbol: '❦' },
  chance: { name: '机会', symbol: '✧' }, shop: { name: '商店', symbol: '⚑' }, mystery: { name: '神秘', symbol: '✱' },
  jail: { name: '监狱', symbol: '▦' }, duel: { name: '对决', symbol: '⚔' }, toll: { name: '过路', symbol: '⛁' }, super: { name: '超级', symbol: '★' },
};

export function MonopolyBoard({
  board, activeActor, status, dice, rolling, lastEvent, canRoll, onRoll,
}: {
  board: ParsedBoard;
  activeActor: Exclude<MonopolyActor, 'codex'> | null;
  status: MonopolyRoomStatus;
  dice: number;
  rolling: boolean;
  lastEvent: string;
  canRoll: boolean;
  onRoll: () => void;
}) {
  const stopped = status === 'paused' || status === 'engine_down' || status === 'finished';
  const turnLabel = status === 'finished' ? '对局结束' : status === 'paused' ? '已暂停' : status === 'engine_down' ? '棋盘已冻结' : `轮到 ${activeActor === 'cc' ? 'CC' : '哈娅'}`;
  const diceHint = status === 'engine_down'
    ? '局面已保存，服务恢复后自动对账'
    : status === 'paused'
      ? '聊天照常 · 棋盘暂停'
      : canRoll ? '点骰子走棋' : activeActor === 'cc' ? 'CC 正在决定并掷骰' : '先处理当前悬账';

  return (
    <section className="mono-board-card" aria-label="大富翁棋盘">
      <div className="mono-board-grid">
        {board.tileTypes.map((type, index) => {
          const meta = TILE_META[type] ?? TILE_META.task;
          const tokens: MonopolyActor[] = [];
          if (board.haya.position === index) tokens.push('haya');
          if (board.cc.position === index) tokens.push('cc');
          return (
            <div
              key={index}
              className={`mono-board-tile ${type} ${tokens.length ? 'occupied' : ''}`}
              style={{ gridRow: GRID[index][0], gridColumn: GRID[index][1] }}
              aria-label={`第 ${index} 格 ${meta.name}`}
            >
              <span className="mono-tile-symbol">{meta.symbol}</span>
              <span className="mono-tile-name">{meta.name}</span>
              <div className="mono-tile-tokens">
                {tokens.map((actor) => <i key={actor} className={`mono-token ${actor}`} />)}
              </div>
            </div>
          );
        })}
        <div className="mono-board-center">
          <button type="button" className={`mono-dice ${canRoll ? 'ready' : ''} ${rolling ? 'rolling' : ''}`} disabled={!canRoll || rolling || stopped} onClick={onRoll} aria-label="掷骰子">
            {rolling ? '✦' : dice || '⚄'}
          </button>
          <div className="mono-turn-copy">
            <div><i className={`mono-turn-dot ${activeActor ?? 'none'}`} /><strong>{turnLabel}</strong></div>
            <span>{lastEvent || '等待第一枚骰子落下'}</span>
            <small>{diceHint}</small>
          </div>
        </div>
      </div>
    </section>
  );
}

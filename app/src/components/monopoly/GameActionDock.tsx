import type { MonopolyRoom, PendingDecision } from '../../lib/monopolyTypes';

interface DockAction {
  label: string;
  sub?: string;
  kind?: 'primary' | 'choice' | 'ghost';
  disabled?: boolean;
  onClick: () => void;
}

function selectedWinner(pending: PendingDecision): 'haya' | 'cc' | null {
  const value = pending.chosen?.duel_winner ?? pending.chosen?.winner;
  if (value === 'haya' || value === '哈娅' || value === 'p1') return 'haya';
  if (value === 'cc' || value === 'CC' || value === 'p2') return 'cc';
  return null;
}

export function GameActionDock({
  room, pending, busy, onAction, onResume, onRefresh, onNewRoom,
}: {
  room: MonopolyRoom;
  pending: PendingDecision | null;
  busy: boolean;
  onAction: (action: string, args?: Record<string, unknown>) => void;
  onResume: () => void;
  onRefresh: () => void;
  onNewRoom: () => void;
}) {
  let chip = '等待掷骰 · IDLE';
  let note = room.active_actor === 'haya' ? '轮到你 · 点棋盘中央的骰子走棋。' : 'CC 的回合 · 它自己决定、自己掷。';
  let actions: DockAction[] = [];
  const myTurn = room.active_actor === 'haya';

  const decideOrRoll = (decision: Record<string, unknown>) => {
    if (myTurn) onAction('roll', decision);
    else onAction('decide', decision);
  };

  if (room.status === 'paused') {
    chip = '已暂停 · PAUSED';
    note = '安全词或手动暂停已生效 · 棋盘冻结，聊天照常，局面已保存。';
    actions = [{ label: '恢复对局', kind: 'primary', onClick: onResume }];
  } else if (room.status === 'engine_down') {
    chip = '引擎离线 · ENGINE_DOWN';
    note = '游戏服务暂时离线 · 局面已保存。聊天不受影响，恢复后会先对账。';
    actions = [{ label: '重新检查引擎', kind: 'primary', onClick: onRefresh }];
  } else if (room.status === 'finished') {
    chip = '终局 · FINISHED';
    note = '本局已经打满。终局结果仍以引擎返回为准。';
    actions = [{ label: '重新开一间游戏室', kind: 'primary', onClick: onNewRoom }];
  } else if (room.status === 'jail_turn') {
    chip = '狱中回合 · JAIL_TURN';
    note = myTurn ? '狱中回合 · 任对方处置。认命之后照常掷骰。' : 'CC 正在处理狱中回合。';
    if (myTurn) actions = [{ label: '认命，掷骰', kind: 'primary', onClick: () => onAction('roll') }];
  } else if (pending?.kind === 'duel') {
    const winner = selectedWinner(pending);
    chip = '对决悬账 · DUEL_PENDING';
    note = '对决没有默认值：先点出胜者，决定会保存在房间里。';
    actions = [
      { label: '胜者：哈娅', kind: winner === 'haya' ? 'choice' : 'ghost', onClick: () => onAction('duel_result', { winner: 'haya' }) },
      { label: '胜者：CC', kind: winner === 'cc' ? 'choice' : 'ghost', onClick: () => onAction('duel_result', { winner: 'cc' }) },
    ];
    if (winner && myTurn) actions.push({ label: '掷骰 · 结算对决', sub: '+ / − 随本次掷骰入账', kind: 'primary', onClick: () => onAction('roll') });
    if (winner && !myTurn) note = '胜者已保存 · 等 CC 掷下一轮并结清对决。';
  } else if (pending && pending.actor === 'cc') {
    chip = `CC 回合 · ${pending.kind.toUpperCase()}_PENDING`;
    note = pending.chosen
      ? 'CC 的决定已经保存；轮到你时，点骰子会先结清这笔悬账。'
      : '这是 CC 的悬账 · 它自己演、自己决定，不替它描写。';
    if (myTurn && pending.chosen) actions = [{ label: '按 CC 的决定结算并掷骰', kind: 'primary', onClick: () => onAction('roll') }];
  } else if (pending?.kind === 'task') {
    chip = '任务悬账 · TASK_PENDING';
    note = '玩的时间 · 现在不单独结算；你的决定会随下一次掷骰一起走。';
    actions = [
      { label: '做完了，掷下一轮', sub: myTurn ? '随骰结算' : '决定保存后交给 CC', kind: 'primary', onClick: () => decideOrRoll({ task: 'done' }) },
      { label: '跳过这道', sub: '按引擎规则结算', onClick: () => decideOrRoll({ task: 'skip' }) },
      { label: '换一张', sub: '即时换卡 · 赔币', onClick: () => onAction('swap') },
    ];
  } else if (pending?.kind === 'truth') {
    chip = '真心话悬账 · TRUTH_PENDING';
    note = '在聊天里答，答完再保存决定。';
    actions = [
      { label: '答了，掷下一轮', kind: 'primary', onClick: () => decideOrRoll({ task: 'done' }) },
      { label: '跳过', onClick: () => decideOrRoll({ task: 'skip' }) },
    ];
  } else if (pending?.kind === 'toll') {
    chip = '过路费挂账 · TOLL_PENDING';
    note = '选择会保存到悬账里，并随下一枚骰子结清。';
    actions = [
      { label: '交钱，掷下一轮', sub: '默认选项', kind: 'primary', onClick: () => decideOrRoll({ toll: 'pay' }) },
      { label: '劳动抵债，掷下一轮', sub: '内容当面兑现', onClick: () => decideOrRoll({ toll: 'serve' }) },
    ];
  } else if (pending?.kind === 'super') {
    chip = '超级任务 · SUPER_PENDING';
    actions = [
      { label: '做完 +5 币，掷下一轮', kind: 'primary', onClick: () => decideOrRoll({ super_action: 'done' }) },
      { label: '花 8 币买断', sub: '随下一次掷骰结算', onClick: () => decideOrRoll({ super_action: 'buyout' }) },
    ];
  } else if (room.status === 'lobby' || room.status === 'setup') {
    chip = '等待设置 · LOBBY';
    note = '先核对安全设置，再把真实配置交给引擎创建棋局。';
  }

  return (
    <section className={`mono-action-dock status-${room.status}`}>
      <div className="mono-dock-heading"><span>ACTION DOCK</span><em>{chip}</em></div>
      <p>{note}</p>
      {actions.length > 0 && (
        <div className="mono-dock-actions">
          {actions.map((action) => (
            <button key={action.label} type="button" className={action.kind ?? 'ghost'} disabled={busy || action.disabled} onClick={action.onClick}>
              <strong>{action.label}</strong>{action.sub && <small>{action.sub}</small>}
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

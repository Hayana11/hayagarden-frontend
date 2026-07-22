import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { RoomHeader } from '../components/monopoly/RoomHeader';
import { PlayerRail } from '../components/monopoly/PlayerRail';
import { MonopolyBoard } from '../components/monopoly/MonopolyBoard';
import { PendingCard } from '../components/monopoly/PendingCard';
import { GameActionDock } from '../components/monopoly/GameActionDock';
import { HandDock } from '../components/monopoly/HandDock';
import { MiniRoomChat } from '../components/monopoly/MiniRoomChat';
import { SetupDrawer } from '../components/monopoly/SetupDrawer';
import { useMonopolyRoom } from '../hooks/useMonopolyRoom';
import { monopolyApi, parseBoard, pendingText, providerLabel } from '../lib/monopolyRoom';
import type { MonopolySetupValues } from '../lib/monopolyTypes';
import './MonopolyRoomScreen.css';

function stringifyReminder(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(String).join(' · ');
  if (value && typeof value === 'object') return Object.entries(value as Record<string, unknown>).map(([key, item]) => `${key}：${String(item)}`).join(' · ');
  return '';
}

function eventCopy(type: string | undefined, payload: Record<string, unknown> | undefined): string {
  if (!type || !payload) return '';
  if (typeof payload.say === 'string') return payload.say;
  if (type === 'settled' && typeof payload.response === 'object') {
    const response = payload.response as Record<string, unknown>;
    if (typeof response.settled === 'string') return response.settled;
  }
  return {
    task_drawn: '抽到一张任务卡', truth_drawn: '抽到一张真心话卡', duel_drawn: '触发同格对决',
    toll_drawn: '过路费已经挂账', super_drawn: '抽到超级任务', settled: '上一笔悬账已经结清', game_over: '终局结果已生成',
  }[type] ?? type.replaceAll('_', ' ');
}

export function MonopolyRoomScreen() {
  const { roomId } = useParams<{ roomId: string }>();
  const navigate = useNavigate();
  const creatingRef = useRef(false);
  const [createError, setCreateError] = useState('');
  const [setupOpen, setSetupOpen] = useState(false);
  const [rolling, setRolling] = useState(false);
  const [dice, setDice] = useState(4);
  const [guess, setGuess] = useState<'大' | '小' | null>(null);
  const room = useMonopolyRoom(roomId);

  useEffect(() => {
    if (roomId !== 'new' || creatingRef.current) return;
    creatingRef.current = true;
    monopolyApi.createRoom()
      .then((snapshot) => navigate(`/monopoly/${snapshot.room.id}`, { replace: true }))
      .catch((error: unknown) => setCreateError(error instanceof Error ? error.message : '建房失败'));
  }, [navigate, roomId]);

  useEffect(() => {
    if (room.snapshot?.room.status === 'lobby' || room.snapshot?.room.status === 'setup') setSetupOpen(true);
  }, [room.snapshot?.room.status]);

  useEffect(() => {
    const value = room.lastEvent?.payload.dice;
    if (typeof value === 'number') setDice(value);
  }, [room.lastEvent]);

  const board = useMemo(() => parseBoard(room.snapshot?.state ?? {}), [room.snapshot?.state]);
  const pendingCopy = useMemo(() => pendingText(room.pending, room.lastEvent), [room.lastEvent, room.pending]);
  const identityReminder = stringifyReminder(room.snapshot?.state.identity_reminder);
  const lastEventText = eventCopy(room.lastEvent?.type, room.lastEvent?.payload);

  const handleAction = useCallback(async (name: string, args: Record<string, unknown> = {}) => {
    if (name === 'roll') {
      setRolling(true);
      const timer = window.setInterval(() => setDice(1 + Math.floor(Math.random() * 6)), 90);
      const rollArgs = { ...args, ...(guess ? { guess } : {}) };
      await room.action(name, rollArgs);
      window.clearInterval(timer);
      setRolling(false);
      setGuess(null);
      return;
    }
    await room.action(name, args);
  }, [guess, room]);

  const canRoll = useMemo(() => {
    const status = room.snapshot?.room.status;
    if (!room.snapshot || room.busy || rolling || room.snapshot.room.active_actor !== 'haya') return false;
    if (status === 'idle' || status === 'jail_turn') return true;
    return !!room.pending?.chosen;
  }, [rolling, room.busy, room.pending?.chosen, room.snapshot]);

  const submitSetup = useCallback(async (values: MonopolySetupValues) => !!(await room.setup(values)), [room]);

  if (roomId === 'new' || room.loading || !room.snapshot) {
    return (
      <div className="mono-loading dash-fullscreen-page">
        <div className="mono-loading-dice">⚄</div>
        <strong>{createError || room.error || '正在准备大富翁游戏室…'}</strong>
        {(createError || room.error) && <button type="button" onClick={() => navigate('/contacts')}>返回通讯录</button>}
      </div>
    );
  }

  const snapshot = room.snapshot;
  const providers = {
    cc: room.providerFor('cc'), codex: room.providerFor('codex'),
  };

  return (
    <div className="mono-room-root dash-fullscreen-page">
      <main className="mono-game-column">
        <RoomHeader
          room={snapshot.room}
          round={board.round}
          maxRound={board.maxRound}
          connection={room.connection}
          busy={room.busy}
          onPause={() => void room.togglePause()}
          onSetup={() => setSetupOpen(true)}
          onLeave={() => navigate('/contacts')}
        />
        <div className="mono-game-scroll">
          <div className="mono-game-content">
            <PlayerRail room={snapshot.room} board={board} pending={room.pending} providers={providers} agents={room.agents} streaming={room.streaming} />
            <MonopolyBoard board={board} activeActor={snapshot.room.active_actor} status={snapshot.room.status} dice={dice} rolling={rolling} lastEvent={lastEventText} canRoll={canRoll} onRoll={() => void handleAction('roll')} />
            {room.pending && <PendingCard pending={room.pending} text={pendingCopy} />}
            <GameActionDock
              room={snapshot.room}
              pending={room.pending}
              busy={room.busy}
              onAction={(action, args) => void handleAction(action, args)}
              onResume={() => void room.togglePause()}
              onRefresh={() => void room.refresh()}
              onNewRoom={() => navigate('/monopoly/new')}
            />
            {snapshot.room.game_id && (
              <HandDock
                player={board.haya}
                pending={room.pending}
                reminder={identityReminder}
                busy={room.busy}
                guess={guess}
                onGuess={setGuess}
                onAction={(action, args) => void handleAction(action, args)}
              />
            )}
            <div className="mono-room-fineprint">
              <span>棋盘真值 · spicy-monopoly</span><i />
              <span>CC {providerLabel(providers.cc) || '跟随主聊天'}</span><i />
              <span>Codex 官端固定</span>
            </div>
          </div>
        </div>
      </main>

      <MiniRoomChat
        messages={snapshot.messages}
        streaming={room.streaming}
        busy={room.busy}
        paused={snapshot.room.status === 'paused'}
        onSend={room.sendMessage}
        onSpeak={() => void room.speak()}
      />

      <SetupDrawer
        open={setupOpen}
        locked={!!snapshot.room.game_id}
        busy={room.busy}
        confirmation={room.setupConfirmation}
        onClose={() => setSetupOpen(false)}
        onSubmit={submitSetup}
      />

      {room.error && <button type="button" className="mono-error-toast" onClick={room.clearError}>{room.error}<span>×</span></button>}
    </div>
  );
}

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { HttpError } from '../lib/http';
import {
  actorFromEngineName,
  monopolyApi,
  pendingStatus,
  reduceGameEventStatus,
  resolveRoomStatus,
} from '../lib/monopolyRoom';
import type {
  AgentStatus,
  MonopolyActor,
  MonopolyGameEvent,
  MonopolyMessage,
  MonopolySetupValues,
  MonopolySnapshot,
  PendingDecision,
  SetupConfirmation,
  StreamEnvelope,
} from '../lib/monopolyTypes';
import { useRoomStream } from './useRoomStream';

function upsertMessage(messages: MonopolyMessage[], incoming: MonopolyMessage): MonopolyMessage[] {
  const index = messages.findIndex((message) => message.id === incoming.id);
  if (index < 0) return [...messages, incoming].sort((a, b) => a.id - b.id);
  const next = [...messages];
  next[index] = incoming;
  return next;
}

function errorDetail(error: unknown): string {
  if (error instanceof HttpError) return error.detail || error.message;
  return error instanceof Error ? error.message : '请求失败，请稍后再试';
}

export function useMonopolyRoom(roomId: string | undefined) {
  const [snapshot, setSnapshot] = useState<MonopolySnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [connection, setConnection] = useState<'connecting' | 'open' | 'closed'>('connecting');
  const [lastEvent, setLastEvent] = useState<MonopolyGameEvent | null>(null);
  const [agents, setAgents] = useState<Record<'cc' | 'codex', AgentStatus>>({ cc: {}, codex: {} });
  const [streaming, setStreaming] = useState<Partial<Record<'cc' | 'codex', string>>>({});
  const [setupConfirmation, setSetupConfirmation] = useState<SetupConfirmation | null>(null);
  const seqRef = useRef(0);

  const applySnapshot = useCallback((next: MonopolySnapshot) => {
    seqRef.current = Math.max(seqRef.current, Number(next.room.event_seq) || 0);
    setSnapshot(next);
    setLoading(false);
  }, []);

  const refresh = useCallback(async () => {
    if (!roomId || roomId === 'new') return;
    try {
      applySnapshot(await monopolyApi.room(roomId));
      setError('');
    } catch (requestError) {
      setError(errorDetail(requestError));
      setLoading(false);
    }
  }, [applySnapshot, roomId]);

  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;

  useEffect(() => {
    setSnapshot(null);
    setLastEvent(null);
    setSetupConfirmation(null);
    setLoading(true);
    void refresh();
  }, [refresh]);

  const handleEnvelope = useCallback((envelope: StreamEnvelope) => {
    if (envelope.type === 'room.snapshot') {
      applySnapshot(envelope.data);
      return;
    }
    if (envelope.type === 'chat.message') {
      setSnapshot((current) => current ? { ...current, messages: upsertMessage(current.messages, envelope.data) } : current);
      return;
    }
    if (envelope.type === 'chat.start') {
      setStreaming((current) => ({ ...current, [envelope.actor]: '' }));
      return;
    }
    if (envelope.type === 'chat.delta') {
      const delta = envelope.delta ?? envelope.content ?? '';
      setStreaming((current) => ({ ...current, [envelope.actor]: `${current[envelope.actor] ?? ''}${delta}` }));
      return;
    }
    if (envelope.type === 'chat.done') {
      setStreaming((current) => ({ ...current, [envelope.actor]: undefined }));
      return;
    }
    if (envelope.type === 'agent.status') {
      setAgents((current) => ({ ...current, [envelope.actor]: envelope.data }));
      return;
    }
    if (envelope.type === 'room.error') {
      const nextSeq = Number(envelope.seq) || 0;
      if (nextSeq) seqRef.current = Math.max(seqRef.current, nextSeq);
      setSnapshot((current) => current ? {
        ...current,
        room: { ...current.room, event_seq: Math.max(current.room.event_seq, nextSeq) },
      } : current);
      setError(envelope.data.detail || envelope.data.code || '房间发生错误');
      return;
    }

    const nextSeq = 'seq' in envelope ? Number(envelope.seq) || 0 : 0;
    if (nextSeq) seqRef.current = Math.max(seqRef.current, nextSeq);
    if (envelope.type === 'game.state') {
      const active = actorFromEngineName(envelope.data.turn);
      setSnapshot((current) => current ? {
        ...current,
        state: envelope.data,
        room: {
          ...current.room,
          state: envelope.data,
          active_actor: active === 'haya' || active === 'cc' ? active : current.room.active_actor,
          event_seq: Math.max(current.room.event_seq, nextSeq),
        },
      } : current);
      return;
    }
    if (envelope.type === 'game.pending') {
      const pending = envelope.data && Object.keys(envelope.data).length
        ? envelope.data as PendingDecision
        : null;
      setSnapshot((current) => {
        if (!current) return current;
        const status = resolveRoomStatus(
          current.room.status,
          envelope.status,
          pending,
        );
        return {
          ...current,
          pending,
          room: {
            ...current.room,
            pending: pending ?? {},
            status,
            event_seq: Math.max(current.room.event_seq, nextSeq),
          },
        };
      });
      return;
    }
    if (envelope.type === 'game.event') {
      const event = envelope.data;
      if (['rolled', 'task_drawn', 'truth_drawn', 'duel_drawn', 'toll_drawn', 'super_drawn', 'settled', 'game_over'].includes(event.type)) {
        setLastEvent(event);
      }
      if (event.type === 'setup_confirmed') {
        setSetupConfirmation({
          activeLimits: event.payload.active_limits,
          historyNote: typeof event.payload.history_note === 'string' ? event.payload.history_note : '',
          intensityNote: typeof event.payload.intensity_note === 'string' ? event.payload.intensity_note : undefined,
        });
      }
      if (event.type === 'roll_outcome_unknown' || event.type === 'game_resumed') {
        void refreshRef.current();
      }
      setSnapshot((current) => {
        if (!current) return current;
        const status = reduceGameEventStatus(current.room.status, event.type, event.payload);
        return { ...current, room: { ...current.room, status, event_seq: Math.max(current.room.event_seq, nextSeq) } };
      });
    }
  }, [applySnapshot]);

  useRoomStream(roomId, 0, handleEnvelope, setConnection);

  const run = useCallback(async (operation: () => Promise<MonopolySnapshot>) => {
    if (busy) return null;
    setBusy(true);
    setError('');
    try {
      const next = await operation();
      applySnapshot(next);
      return next;
    } catch (requestError) {
      if (
        requestError instanceof HttpError
        && [400, 409, 422, 428].includes(requestError.status)
      ) {
        await refresh();
      }
      setError(errorDetail(requestError));
      return null;
    } finally {
      setBusy(false);
    }
  }, [applySnapshot, busy, refresh]);

  const eventSeq = snapshot?.room.event_seq ?? seqRef.current;
  const action = useCallback((name: string, args: Record<string, unknown> = {}) => {
    if (!roomId) return Promise.resolve(null);
    return run(() => monopolyApi.action(roomId, name, args, eventSeq));
  }, [eventSeq, roomId, run]);

  const setup = useCallback((values: MonopolySetupValues) => {
    if (!roomId) return Promise.resolve(null);
    setSetupConfirmation(null);
    return run(() => monopolyApi.setup(roomId, values, eventSeq));
  }, [eventSeq, roomId, run]);

  const togglePause = useCallback(() => {
    if (!roomId || !snapshot) return Promise.resolve(null);
    return run(() => snapshot.room.status === 'paused'
      ? monopolyApi.resume(roomId, eventSeq)
      : monopolyApi.pause(roomId, eventSeq));
  }, [eventSeq, roomId, run, snapshot]);

  const sendMessage = useCallback(async (content: string, targets: Array<'cc' | 'codex'>) => {
    if (!roomId || busy) return false;
    setBusy(true);
    setError('');
    try {
      const result = await monopolyApi.message(roomId, content, targets, content.trim() === '404');
      applySnapshot(result.snapshot);
      return true;
    } catch (requestError) {
      setError(errorDetail(requestError));
      return false;
    } finally {
      setBusy(false);
    }
  }, [applySnapshot, busy, roomId]);

  const speak = useCallback(async (targets: Array<'cc' | 'codex'> = ['cc', 'codex']) => {
    if (!roomId) return;
    try {
      await monopolyApi.speak(roomId, targets);
    } catch (requestError) {
      setError(errorDetail(requestError));
    }
  }, [roomId]);

  const pending = useMemo<PendingDecision | null>(() => {
    const value = snapshot?.pending;
    return value && 'kind' in value ? value as PendingDecision : null;
  }, [snapshot?.pending]);

  const providerFor = useCallback((actor: MonopolyActor) => {
    if (actor === 'haya') return undefined;
    const recent = [...(snapshot?.messages ?? [])].reverse().find((message) => message.author === actor && message.provider_meta);
    return recent?.provider_meta ?? agents[actor].provider;
  }, [agents, snapshot?.messages]);

  return {
    snapshot, pending, loading, busy, error, connection, lastEvent, agents, streaming,
    setupConfirmation, refresh, action, setup, togglePause, sendMessage, speak, providerFor,
    clearError: () => setError(''),
  };
}

// Re-export for tests / consumers that need pending status mapping.
export { pendingStatus };

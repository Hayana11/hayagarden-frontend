import { useEffect, useRef } from 'react';
import { monopolyApi } from '../lib/monopolyRoom';
import type { StreamEnvelope } from '../lib/monopolyTypes';

const EVENT_NAMES = [
  'room.snapshot', 'game.event', 'game.state', 'game.pending', 'chat.message',
  'chat.start', 'chat.delta', 'chat.done', 'agent.status', 'room.error',
] as const;

export function useRoomStream(
  roomId: string | undefined,
  after: number,
  onEnvelope: (event: StreamEnvelope) => void,
  onConnection: (state: 'connecting' | 'open' | 'closed') => void,
) {
  const envelopeRef = useRef(onEnvelope);
  const connectionRef = useRef(onConnection);
  envelopeRef.current = onEnvelope;
  connectionRef.current = onConnection;

  useEffect(() => {
    if (!roomId || roomId === 'new') return;
    connectionRef.current('connecting');
    const source = new EventSource(monopolyApi.streamUrl(roomId, after), { withCredentials: true });
    source.onopen = () => connectionRef.current('open');
    source.onerror = () => connectionRef.current('closed');

    const handlers = EVENT_NAMES.map((name) => {
      const handler = (raw: Event) => {
        const message = raw as MessageEvent<string>;
        try {
          envelopeRef.current(JSON.parse(message.data) as StreamEnvelope);
        } catch {
          // A malformed optional live event must not tear down the room stream.
        }
      };
      source.addEventListener(name, handler);
      return [name, handler] as const;
    });

    return () => {
      handlers.forEach(([name, handler]) => source.removeEventListener(name, handler));
      source.close();
    };
  }, [after, roomId]);
}

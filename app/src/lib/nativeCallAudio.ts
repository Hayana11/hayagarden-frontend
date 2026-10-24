// P2H-1 native call audio (Canary shell only): his lines play through Android's
// communication audio path, earpiece by default, speaker on request. The page
// names a line by call id and seq; the shell builds the URL and authenticates
// it itself. Without the bridge, CallScreen keeps its <audio> element.
export type CallAudioRoute = 'earpiece' | 'speaker' | 'other';

export type CallAudioEvent =
  | { type: 'started' | 'ended'; seq: number }
  | { type: 'error'; seq: number; code?: string }
  | { type: 'route'; route: CallAudioRoute };

type ElpisCallAudioBridge = {
  isAvailable?: () => boolean;
  begin?: () => void;
  play?: (callId: string, seq: number) => void;
  stop?: () => void;
  setSpeaker?: (on: boolean) => void;
  end?: () => void;
};

declare global {
  interface Window {
    ElpisCallAudio?: ElpisCallAudioBridge;
    __elpisCallAudio?: (json: string) => void;
  }
}

const listeners = new Set<(event: CallAudioEvent) => void>();
const ROUTES: Record<string, CallAudioRoute> = { earpiece: 'earpiece', speaker: 'speaker', other: 'other' };

/** A native event, or null when it is not one this page understands. */
export function parseCallAudioEvent(json: string): CallAudioEvent | null {
  let raw: unknown;
  try {
    raw = JSON.parse(json);
  } catch {
    return null;
  }
  if (!raw || typeof raw !== 'object') return null;
  const event = raw as Record<string, unknown>;
  if (event.type === 'route') {
    const route = typeof event.route === 'string' ? ROUTES[event.route] : undefined;
    return route ? { type: 'route', route } : null;
  }
  const seq = event.seq;
  if (typeof seq !== 'number' || !Number.isInteger(seq) || seq <= 0) {
    return null;
  }
  if (event.type === 'started' || event.type === 'ended') return { type: event.type, seq };
  if (event.type === 'error') return { type: 'error', seq, code: typeof event.code === 'string' ? event.code : undefined };
  return null;
}

function bridge(): ElpisCallAudioBridge | undefined {
  return typeof window === 'undefined' ? undefined : window.ElpisCallAudio;
}

function install(): void {
  if (typeof window === 'undefined' || window.__elpisCallAudio) return;
  window.__elpisCallAudio = (json: string) => {
    const event = parseCallAudioEvent(json);
    if (event) listeners.forEach((listener) => listener(event));
  };
}

function call<T>(fn: (bridge: ElpisCallAudioBridge) => T, fallback: T): T {
  const current = bridge();
  if (!current) return fallback;
  try {
    return fn(current);
  } catch {
    return fallback;
  }
}

export const nativeCallAudio = {
  supported(): boolean {
    const current = bridge();
    if (!current || typeof current.play !== 'function') return false;
    return call((value) => !!(value.isAvailable && value.isAvailable()), false);
  },

  begin(): void {
    install();
    call((value) => value.begin && value.begin(), undefined);
  },

  play(callId: string, seq: number): void {
    install();
    call((value) => value.play && value.play(callId, seq), undefined);
  },

  stop(): void {
    call((value) => value.stop && value.stop(), undefined);
  },

  setSpeaker(on: boolean): void {
    call((value) => value.setSpeaker && value.setSpeaker(on), undefined);
  },

  end(): void {
    call((value) => value.end && value.end(), undefined);
  },

  subscribe(listener: (event: CallAudioEvent) => void): () => void {
    install();
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
};

// P2F native voice adapter for the Elpis Canary shell.
export type VoiceEvent = {
  type: 'permission' | 'ready' | 'speech_start' | 'utterance_ready' | 'utterance_discarded' | 'error' | 'paused' | 'resumed';
  seq?: number;
  duration_ms?: number;
  bytes?: number;
  granted?: boolean;
  code?: string;
  reason?: string;
};

type ElpisVoiceBridge = {
  isAvailable?: () => boolean;
  hasPermission?: () => boolean;
  requestPermission?: () => void;
  start?: () => void;
  stop?: () => void;
  cancel?: () => void;
  takeUtterance?: (seq: number) => string;
};

declare global {
  interface Window {
    ElpisVoice?: ElpisVoiceBridge;
    __elpisVoice?: (json: string) => void;
  }
}

const listeners = new Set<(event: VoiceEvent) => void>();
const PERMISSION_TIMEOUT_MS = 60000;

function bridge(): ElpisVoiceBridge | undefined {
  return typeof window === 'undefined' ? undefined : window.ElpisVoice;
}

function install(): void {
  if (typeof window === 'undefined' || window.__elpisVoice) return;
  window.__elpisVoice = (json: string) => {
    let event: VoiceEvent;
    try {
      event = JSON.parse(json) as VoiceEvent;
    } catch {
      return;
    }
    listeners.forEach((listener) => listener(event));
  };
}

function call<T>(fn: (bridge: ElpisVoiceBridge) => T, fallback: T): T {
  const current = bridge();
  if (!current) return fallback;
  try {
    return fn(current);
  } catch {
    return fallback;
  }
}

export type VoiceStatus = 'ready' | 'no_bridge' | 'browser';

function inElpisShell(): boolean {
  if (typeof window === 'undefined') return false;
  const current = window as unknown as Record<string, unknown>;
  return !!(current.ElpisNative || current.ElpisSpeech || current.ElpisHealth || current.ElpisNotifications);
}

export const nativeVoice = {
  status(): VoiceStatus {
    const current = bridge();
    if (!current || typeof current.start !== 'function') {
      return inElpisShell() ? 'no_bridge' : 'browser';
    }
    return call((value) => !!(value.isAvailable && value.isAvailable()), false) ? 'ready' : 'no_bridge';
  },

  supported(): boolean {
    return nativeVoice.status() === 'ready';
  },

  hasPermission(): boolean {
    return call((value) => !!(value.hasPermission && value.hasPermission()), false);
  },

  requestPermission(): Promise<boolean> {
    if (nativeVoice.hasPermission()) return Promise.resolve(true);
    return new Promise((resolve) => {
      let settled = false;
      const unsubscribe = nativeVoice.subscribe((event) => {
        if (event.type === 'permission') finish(!!event.granted);
      });
      const finish = (granted: boolean) => {
        if (settled) return;
        settled = true;
        unsubscribe();
        window.clearTimeout(timer);
        resolve(granted);
      };
      const timer = window.setTimeout(() => finish(nativeVoice.hasPermission()), PERMISSION_TIMEOUT_MS);
      call((value) => value.requestPermission && value.requestPermission(), undefined);
    });
  },

  start(): void {
    install();
    call((value) => value.start && value.start(), undefined);
  },

  stop(): void {
    call((value) => value.stop && value.stop(), undefined);
  },

  cancel(): void {
    call((value) => value.cancel && value.cancel(), undefined);
  },

  takeUtterance(seq: number): Blob | null {
    const encoded = call((value) => value.takeUtterance ? value.takeUtterance(seq) : '', '');
    if (!encoded) return null;
    try {
      const binary = atob(encoded);
      const bytes = new Uint8Array(binary.length);
      for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
      return new Blob([bytes], { type: 'audio/wav' });
    } catch {
      return null;
    }
  },

  subscribe(listener: (event: VoiceEvent) => void): () => void {
    install();
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
};

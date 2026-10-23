// Speech-to-text through the Elpis Canary shell (window.ElpisSpeech, P2E).
//
// The shell runs android.speech.SpeechRecognizer: one utterance per start(),
// partial results while she talks, a final result after ~2s of silence. It
// reports back by calling window.__elpisSpeech(json). Outside the shell (desktop
// browser, older Canary) supported() is false and the call page keeps typing.

export type SpeechEvent = {
  type: 'ready' | 'speech_start' | 'partial' | 'final' | 'end' | 'error' | 'permission';
  text?: string;
  code?: string;
  granted?: boolean;
};

type ElpisSpeechBridge = {
  isAvailable?: () => boolean;
  hasPermission?: () => boolean;
  requestPermission?: () => void;
  start?: (language: string) => void;
  stop?: () => void;
  cancel?: () => void;
};

declare global {
  interface Window {
    ElpisSpeech?: ElpisSpeechBridge;
    __elpisSpeech?: (json: string) => void;
  }
}

const PERMISSION_TIMEOUT_MS = 60000;
const listeners: Array<(event: SpeechEvent) => void> = [];

function bridge(): ElpisSpeechBridge | undefined {
  return typeof window === 'undefined' ? undefined : window.ElpisSpeech;
}

function install(): void {
  if (typeof window === 'undefined' || window.__elpisSpeech) return;
  window.__elpisSpeech = (json: string) => {
    let event: SpeechEvent;
    try {
      event = JSON.parse(json) as SpeechEvent;
    } catch {
      return;
    }
    listeners.slice().forEach((listener) => listener(event));
  };
}

function call<T>(fn: (b: ElpisSpeechBridge) => T, fallback: T): T {
  const b = bridge();
  if (!b) return fallback;
  try {
    return fn(b);
  } catch {
    return fallback;
  }
}

export const nativeSpeech = {
  supported(): boolean {
    return call((b) => typeof b.start === 'function' && !!(b.isAvailable && b.isAvailable()), false);
  },

  hasPermission(): boolean {
    return call((b) => !!(b.hasPermission && b.hasPermission()), false);
  },

  /** Resolves true once RECORD_AUDIO is granted; asks the shell only if needed. */
  requestPermission(): Promise<boolean> {
    if (nativeSpeech.hasPermission()) return Promise.resolve(true);
    return new Promise((resolve) => {
      let settled = false;
      const finish = (granted: boolean) => {
        if (settled) return;
        settled = true;
        unsubscribe();
        window.clearTimeout(timer);
        resolve(granted);
      };
      const unsubscribe = nativeSpeech.subscribe((event) => {
        if (event.type === 'permission') finish(!!event.granted);
      });
      const timer = window.setTimeout(() => finish(nativeSpeech.hasPermission()), PERMISSION_TIMEOUT_MS);
      call((b) => b.requestPermission && b.requestPermission(), undefined);
    });
  },

  start(language: string): void {
    install();
    call((b) => b.start && b.start(language), undefined);
  },

  stop(): void {
    call((b) => b.stop && b.stop(), undefined);
  },

  cancel(): void {
    call((b) => b.cancel && b.cancel(), undefined);
  },

  subscribe(listener: (event: SpeechEvent) => void): () => void {
    install();
    listeners.push(listener);
    return () => {
      const index = listeners.indexOf(listener);
      if (index !== -1) listeners.splice(index, 1);
    };
  },
};

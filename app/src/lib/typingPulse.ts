// Composer typing pulse for the Wake call Gate: while she edits the Chat
// draft, tell the server "typing" at most once per interval. The server holds
// it for a short TTL and lets it lapse, so there is nothing to clear on blur,
// unmount or a lost network. Best effort: a failed pulse is dropped silently.

export const TYPING_PULSE_URL = '/api/chat/typing';
export const TYPING_PULSE_INTERVAL_MS = 4000;

type Post = (url: string) => Promise<unknown>;

export interface TypingPulseOptions {
  intervalMs?: number;
  now?: () => number;
  post?: Post;
}

function defaultPost(url: string): Promise<unknown> {
  return fetch(url, { method: 'POST', credentials: 'include', keepalive: true });
}

/** Returns a function to call on every draft edit; it posts at most once per interval. */
export function createTypingPulse(opts: TypingPulseOptions = {}): () => void {
  const intervalMs = opts.intervalMs ?? TYPING_PULSE_INTERVAL_MS;
  const now = opts.now ?? (() => Date.now());
  const post = opts.post ?? defaultPost;
  let last = -Infinity;
  return () => {
    const t = now();
    if (t - last < intervalMs) return;
    last = t;
    try {
      void Promise.resolve(post(TYPING_PULSE_URL)).catch(() => undefined);
    } catch {
      // fetch unavailable or threw synchronously: the pulse is best effort.
    }
  };
}

// Voice call client for /api/call/* (owner-only; see voice_call_routes.py).
//
// The page holds one EventSource at a time. The server ends each stream after
// ~25s with a `state` event carrying the last seq; the page then reopens with
// that seq, so a reconnect only fetches lines it has not seen.
import { http, sseUrl } from './http';

export type CallState = 'ringing' | 'active' | 'ended' | 'missed' | 'declined' | 'canceled';

export type VoiceCall = {
  id: string;
  state: CallState;
  created_at: number;
  answered_at: number | null;
  ended_at: number | null;
};

export type CallLine = {
  seq: number;
  speaker: 'her' | 'him';
  text: string;
  audio_key?: string | null;
};

export type CallSummary = {
  call: VoiceCall;
  record: string;
  duration?: string;
};

export type VoiceHearResult = {
  utterance: number;
  text: string;
  line: CallLine | null;
  stale?: boolean;
  duplicate?: boolean;
  discarded?: boolean;
};

export const FINAL_CALL_STATES: readonly CallState[] = ['ended', 'missed', 'declined', 'canceled'];

const base = (callId: string) => `/api/call/${encodeURIComponent(callId)}`;

export const voiceCall = {
  current: () => http.get<{ call: VoiceCall | null }>('/api/call/current').then((r) => r.call),
  ring: () => http.post<{ call: VoiceCall }>('/api/call/ring').then((r) => r.call),
  load: (callId: string) => http.get<{ call: VoiceCall | null; lines: CallLine[] }>(base(callId)),
  answer: (callId: string) => http.post<{ call: VoiceCall }>(`${base(callId)}/answer`).then((r) => r.call),
  decline: (callId: string) => http.post<CallSummary>(`${base(callId)}/decline`),
  hangup: (callId: string) => http.post<CallSummary>(`${base(callId)}/hangup`),
  interrupt: (callId: string, utteranceSeq: number) =>
    http.post<{ ok: boolean }>(`${base(callId)}/interrupt?seq=${encodeURIComponent(String(utteranceSeq))}`),
  hear: (callId: string, wavBlob: Blob, utteranceSeq: number, language: string) =>
    http.post<VoiceHearResult>(
      `${base(callId)}/hear?lang=${encodeURIComponent(language)}&seq=${encodeURIComponent(String(utteranceSeq))}`,
      wavBlob,
      { headers: { 'Content-Type': 'audio/wav', Accept: 'application/json' } },
    ).then((result) => result),
  say: (callId: string, text: string) =>
    http.post<{ line: CallLine }>(`${base(callId)}/say`, { text }).then((r) => r.line),
  streamUrl: (callId: string, after: number) => sseUrl(`${base(callId)}/stream?after=${after}`),
  audioUrl: (callId: string, seq: number) => sseUrl(`${base(callId)}/audio/${seq}`),
  /** Survives the page being backgrounded or closed, unlike fetch. */
  hangupBeacon: (callId: string): boolean => {
    try {
      return navigator.sendBeacon(sseUrl(`${base(callId)}/hangup`));
    } catch {
      return false;
    }
  },
};

export function formatCallTimer(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const mm = Math.floor(s / 60);
  const ss = s % 60;
  return `${mm < 10 ? '0' : ''}${mm}:${ss < 10 ? '0' : ''}${ss}`;
}

/** Merge newly streamed lines into what the page already shows, by seq. */
export function mergeLines(existing: CallLine[], incoming: CallLine[]): CallLine[] {
  if (!incoming.length) return existing;
  const seen: Record<number, true> = {};
  for (const line of existing) seen[line.seq] = true;
  const added = incoming.filter((line) => !seen[line.seq]);
  if (!added.length) return existing;
  return existing.concat(added).sort((a, b) => a.seq - b.seq);
}

export function lastSeq(lines: CallLine[]): number {
  return lines.length ? lines[lines.length - 1].seq : 0;
}

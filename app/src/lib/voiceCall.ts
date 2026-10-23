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
  /** A code from voiceCall.languages(); null is the default (Chinese) call. */
  language?: string | null;
};

/** One configured call language; `speech` is the Android fallback recognizer's locale. */
export type CallLanguage = { code: string; label: string; speech: string | null };

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
  languages: () => http.get<{ languages: CallLanguage[] }>('/api/call/languages').then((r) => r.languages || []),
  ring: (language?: string) =>
    http.post<{ call: VoiceCall }>('/api/call/ring', language ? { language } : undefined).then((r) => r.call),
  load: (callId: string) => http.get<{ call: VoiceCall | null; lines: CallLine[] }>(base(callId)),
  answer: (callId: string) => http.post<{ call: VoiceCall }>(`${base(callId)}/answer`).then((r) => r.call),
  decline: (callId: string) => http.post<CallSummary>(`${base(callId)}/decline`),
  hangup: (callId: string) => http.post<CallSummary>(`${base(callId)}/hangup`),
  interrupt: (callId: string, utteranceSeq?: number, prev?: number) =>
    http.post<{ ok: boolean }>(
      `${base(callId)}/interrupt${utteranceSeq === undefined ? '' : `?${seqQuery(utteranceSeq, prev)}`}`,
    ),
  hear: (callId: string, wavBlob: Blob, utteranceSeq: number, language: string, prev?: number) =>
    http.post<VoiceHearResult>(
      `${base(callId)}/hear?lang=${encodeURIComponent(language)}&${seqQuery(utteranceSeq, prev)}`,
      wavBlob,
      { headers: { 'Content-Type': 'audio/wav', Accept: 'application/json' } },
    ).then((result) => result),
  discard: (callId: string, utteranceSeq: number, prev?: number) =>
    http.post<VoiceHearResult>(
      `${base(callId)}/hear?${seqQuery(utteranceSeq, prev)}&discard=1`,
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

function seqQuery(seq: number, prev?: number): string {
  const query = `seq=${encodeURIComponent(String(seq))}`;
  return prev === undefined ? query : `${query}&prev=${encodeURIComponent(String(prev))}`;
}

/**
 * Her utterances in one call, as a predecessor chain. The shell's seq is
 * process-wide, so every interrupt/hear/discard also names the seq she started
 * just before it in this call (`prev`, 0 for the first) and the server orders
 * by that chain. `prev` is fixed at speech_start: by utterance_ready a newer
 * one may already have begun.
 */
export type UtteranceChain = { callId: string; last: number; prevBySeq: Record<number, number> };

export function newUtteranceChain(): UtteranceChain {
  return { callId: '', last: 0, prevBySeq: {} };
}

/** speech_start(seq) in this call: fix and return its prev. A new call starts a new chain. */
export function startUtterance(chain: UtteranceChain, callId: string, seq: number): number | undefined {
  if (chain.callId !== callId) {
    chain.callId = callId;
    chain.last = 0;
    chain.prevBySeq = {};
  }
  const known = chain.prevBySeq[seq];
  if (known !== undefined) return known;
  if (seq <= chain.last) return undefined;  // not a successor: send no prev rather than a wrong one
  chain.prevBySeq[seq] = chain.last;
  chain.last = seq;
  return chain.prevBySeq[seq];
}

/** The prev fixed at speech_start, or undefined if that start was not seen in this call. */
export function utterancePrev(chain: UtteranceChain, callId: string, seq: number): number | undefined {
  return chain.callId === callId ? chain.prevBySeq[seq] : undefined;
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

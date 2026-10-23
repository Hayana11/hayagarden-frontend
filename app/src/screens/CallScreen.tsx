// /dash/call — voice call page (desktop typing mode first; phone speech input comes later).
// backend: /api/call/* via lib/voiceCall.ts; his lines stream over SSE and are spoken one by one.
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';
import { HttpError } from '../lib/http';
import { ROUTES } from '../navigation';
import {
  FINAL_CALL_STATES,
  formatCallTimer,
  lastSeq,
  mergeLines,
  voiceCall,
  type CallLine,
  type VoiceCall,
} from '../lib/voiceCall';
import './CallScreen.css';

// Zero-length WAV: played inside the answer tap so later play() calls are allowed.
const SILENT_WAV = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=';
const IDLE_POLL_MS = 5000;
const STREAM_RETRY_MS = 2000;

function isFinal(call: VoiceCall | null): boolean {
  return !!call && FINAL_CALL_STATES.indexOf(call.state) !== -1;
}

function describeError(err: unknown): string {
  if (err instanceof HttpError && err.status === 401) return '需要主人授权后才能通话';
  if (err instanceof HttpError && err.detail) return err.detail;
  return '网络不太好，稍后再试';
}

export function CallScreen() {
  const navigate = useNavigate();
  const [call, setCall] = useState<VoiceCall | null>(null);
  const [lines, setLines] = useState<CallLine[]>([]);
  const [record, setRecord] = useState('');
  const [error, setError] = useState('');
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [voiceless, setVoiceless] = useState<Record<number, true>>({});

  const callRef = useRef<VoiceCall | null>(null);
  const linesRef = useRef<CallLine[]>([]);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const queueRef = useRef<number[]>([]);
  const playingRef = useRef<number | null>(null);
  const streamRef = useRef<EventSource | null>(null);
  const retryRef = useRef<number | null>(null);
  const composingRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  callRef.current = call;
  linesRef.current = lines;

  // ── audio queue ─────────────────────────────────────────
  const ensureAudio = useCallback((): HTMLAudioElement => {
    if (!audioRef.current) audioRef.current = new Audio();
    return audioRef.current;
  }, []);

  const playNext = useCallback(() => {
    const current = callRef.current;
    if (playingRef.current !== null || !current) return;
    const seq = queueRef.current.shift();
    if (seq === undefined) return;
    const audio = ensureAudio();
    playingRef.current = seq;
    const done = (failed: boolean) => {
      if (playingRef.current !== seq) return;
      if (failed) setVoiceless((prev) => ({ ...prev, [seq]: true }));
      playingRef.current = null;
      playNext();
    };
    audio.onended = () => done(false);
    audio.onerror = () => done(true);
    audio.src = voiceCall.audioUrl(current.id, seq);
    const started = audio.play();
    if (started && typeof started.catch === 'function') started.catch(() => done(true));
  }, [ensureAudio]);

  const stopSpeaking = useCallback(() => {
    queueRef.current = [];
    playingRef.current = null;
    const audio = audioRef.current;
    if (audio) {
      audio.onended = null;
      audio.onerror = null;
      audio.pause();
    }
  }, []);

  const addLines = useCallback((incoming: CallLine[], speak: boolean) => {
    const before = linesRef.current;
    const merged = mergeLines(before, incoming);
    if (merged === before) return;
    linesRef.current = merged;
    setLines(merged);
    if (!speak) return;
    const known: Record<number, true> = {};
    for (const line of before) known[line.seq] = true;
    for (const line of incoming) {
      if (line.speaker === 'him' && !known[line.seq]) queueRef.current.push(line.seq);
    }
    playNext();
  }, [playNext]);

  // ── stream ──────────────────────────────────────────────
  const closeStream = useCallback(() => {
    if (retryRef.current !== null) {
      window.clearTimeout(retryRef.current);
      retryRef.current = null;
    }
    if (streamRef.current) {
      streamRef.current.close();
      streamRef.current = null;
    }
  }, []);

  const openStream = useCallback((callId: string) => {
    closeStream();
    const source = new EventSource(voiceCall.streamUrl(callId, lastSeq(linesRef.current)), { withCredentials: true });
    streamRef.current = source;
    source.addEventListener('line', (event) => {
      try {
        addLines([JSON.parse((event as MessageEvent).data) as CallLine], true);
      } catch { /* malformed line: skip */ }
    });
    source.addEventListener('state', (event) => {
      source.close();
      if (streamRef.current === source) streamRef.current = null;
      let state = '';
      try {
        state = (JSON.parse((event as MessageEvent).data) as { state: string }).state;
      } catch { /* treat as unknown */ }
      const current = callRef.current;
      if (!current || current.id !== callId) return;
      if (state && state !== current.state) {
        setCall({ ...current, state: state as VoiceCall['state'] });
      }
      if (state === 'active') openStream(callId);
    });
    source.onerror = () => {
      source.close();
      if (streamRef.current === source) streamRef.current = null;
      retryRef.current = window.setTimeout(() => {
        retryRef.current = null;
        const current = callRef.current;
        if (current && current.id === callId && current.state === 'active') openStream(callId);
      }, STREAM_RETRY_MS);
    };
  }, [addLines, closeStream]);

  // ── initial load + idle polling for an incoming call ────
  useEffect(() => {
    let cancelled = false;
    const check = () => {
      if (callRef.current && !isFinal(callRef.current)) return;
      voiceCall.current().then((incoming) => {
        if (cancelled || !incoming) return;
        if (callRef.current && callRef.current.id === incoming.id) return;
        setRecord('');
        setVoiceless({});
        linesRef.current = [];
        setLines([]);
        setCall(incoming);
        if (incoming.state === 'active') {
          voiceCall.load(incoming.id).then((loaded) => {
            if (!cancelled) addLines(loaded.lines, false);
          }).catch(() => undefined);
        }
      }).catch((err) => {
        if (!cancelled) setError(describeError(err));
      });
    };
    check();
    const timer = window.setInterval(check, IDLE_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [addLines]);

  // Ringing calls can time out or be withdrawn: refresh their state while ringing.
  useEffect(() => {
    if (!call || call.state !== 'ringing') return undefined;
    const timer = window.setInterval(() => {
      voiceCall.load(call.id).then((loaded) => {
        if (loaded.call && callRef.current && callRef.current.id === loaded.call.id) setCall(loaded.call);
      }).catch(() => undefined);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [call]);

  useEffect(() => {
    if (call && call.state === 'active') {
      openStream(call.id);
      return closeStream;
    }
    closeStream();
    if (isFinal(call)) stopSpeaking();
    return undefined;
  }, [call && call.id, call && call.state, openStream, closeStream, stopSpeaking]); // eslint-disable-line react-hooks/exhaustive-deps

  // Timer tick while talking.
  useEffect(() => {
    if (!call || call.state !== 'active') return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [call]);

  // Leaving the page (tab closed, app backgrounded and killed, SPA navigation) ends the call.
  useEffect(() => {
    const onPageHide = () => {
      const current = callRef.current;
      if (current && current.state === 'active') voiceCall.hangupBeacon(current.id);
    };
    window.addEventListener('pagehide', onPageHide);
    return () => {
      window.removeEventListener('pagehide', onPageHide);
      onPageHide();
      closeStream();
      stopSpeaking();
    };
  }, [closeStream, stopSpeaking]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length]);

  // ── actions ─────────────────────────────────────────────
  const run = async (action: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await action();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  };

  const answer = () => {
    if (!call) return;
    const audio = ensureAudio();
    audio.src = SILENT_WAV;
    const unlocked = audio.play();
    if (unlocked && typeof unlocked.catch === 'function') unlocked.catch(() => undefined);
    void run(async () => {
      setNow(Date.now());
      setCall(await voiceCall.answer(call.id));
    });
  };

  const decline = () => {
    if (!call) return;
    void run(async () => {
      const summary = await voiceCall.decline(call.id);
      setRecord(summary.record);
      setCall(summary.call);
    });
  };

  const hangup = () => {
    if (!call) return;
    stopSpeaking();
    void run(async () => {
      const summary = await voiceCall.hangup(call.id);
      setRecord(summary.record);
      setCall(summary.call);
    });
  };

  const send = () => {
    const text = draft.trim();
    if (!call || call.state !== 'active' || !text) return;
    stopSpeaking();
    setDraft('');
    void run(async () => {
      const line = await voiceCall.say(call.id, text);
      addLines([line], false);
    });
  };

  const testRing = () => {
    void run(async () => {
      setRecord('');
      setVoiceless({});
      linesRef.current = [];
      setLines([]);
      setCall(await voiceCall.ring());
    });
  };

  // ── render ──────────────────────────────────────────────
  const state = call ? call.state : null;
  const elapsed = call && call.answered_at ? now / 1000 - call.answered_at : 0;
  const status = state === 'ringing' ? '来电中…'
    : state === 'active' ? formatCallTimer(elapsed)
      : record || (state === 'missed' ? 'ᯅ 未接听' : state === 'canceled' ? 'ᯅ 对方已取消' : '');

  return (
    <div className="call-root dash-fullscreen-page">
      <PageHeader title="通话" onBack={() => navigate(ROUTES.chat)} backLabel="返回聊天" />

      <section className="call-hero vstack vstack-8">
        <div className={`call-avatar${state === 'ringing' ? ' is-ringing' : ''}`} aria-hidden="true">Θ</div>
        <div className="call-name">费奥多尔</div>
        <div className="call-status">{call ? status : '现在没有来电'}</div>
      </section>

      <div className="call-transcript hide-scrollbar" ref={scrollRef}>
        <div className="vstack vstack-10">
          {lines.map((line) => (
            <div key={line.seq} className={`call-line call-line--${line.speaker}`}>
              <span className="call-bubble">{line.text}</span>
              {line.speaker === 'him' && voiceless[line.seq] ? <span className="call-voiceless">仅文字</span> : null}
            </div>
          ))}
        </div>
      </div>

      {error ? <div className="call-error">{error}</div> : null}

      <footer className="call-controls">
        {state === 'ringing' ? (
          <div className="call-buttons hstack hstack-26">
            <button type="button" className="call-btn call-btn--decline" onClick={decline} disabled={busy}>拒绝</button>
            <button type="button" className="call-btn call-btn--answer" onClick={answer} disabled={busy}>接听</button>
          </div>
        ) : state === 'active' ? (
          <div className="vstack vstack-14">
            <form
              className="call-composer hstack hstack-8"
              onSubmit={(event) => { event.preventDefault(); send(); }}
            >
              <input
                className="call-input"
                value={draft}
                placeholder="打字代替说话，回车发送"
                onChange={(event) => setDraft(event.target.value)}
                onCompositionStart={() => { composingRef.current = true; }}
                onCompositionEnd={() => { composingRef.current = false; }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && (composingRef.current || event.nativeEvent.isComposing)) {
                    event.preventDefault();
                  }
                }}
              />
              <button type="submit" className="call-send" disabled={busy || !draft.trim()}>发送</button>
            </form>
            <div className="call-buttons hstack">
              <button type="button" className="call-btn call-btn--decline" onClick={hangup} disabled={busy}>挂断</button>
            </div>
          </div>
        ) : (
          <div className="call-buttons hstack">
            <button type="button" className="call-test" onClick={testRing} disabled={busy}>测试来电</button>
          </div>
        )}
      </footer>
    </div>
  );
}

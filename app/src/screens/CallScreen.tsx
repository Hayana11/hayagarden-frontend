// /dash/call — voice call page. Typing everywhere; in the Elpis Canary shell (P2E) the mic
// turns it into a hands-free call: she speaks, ~2s of silence ends the sentence, it is sent.
// The microphone is off while he speaks and comes back 1.5s after his last line finished.
// backend: /api/call/* via lib/voiceCall.ts; his lines stream over SSE and are spoken one by one.
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';
import { HttpError } from '../lib/http';
import { nativeSpeech, type SpeechEvent, type SpeechStatus } from '../lib/nativeSpeech';
import { nativeVoice, type VoiceEvent } from '../lib/nativeVoice';
import { ROUTES } from '../navigation';
import {
  FINAL_CALL_STATES,
  formatCallTimer,
  lastSeq,
  mergeLines,
  newUtteranceChain,
  startUtterance,
  utterancePrev,
  voiceCall,
  type CallLanguage,
  type CallLine,
  type VoiceCall,
} from '../lib/voiceCall';
import './CallScreen.css';

// Zero-length WAV: played inside the answer tap so later play() calls are allowed.
const SILENT_WAV = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=';
const IDLE_POLL_MS = 5000;
const STREAM_RETRY_MS = 2000;
const SPEECH_LANGUAGE = 'zh-CN';
const P2F_LANGUAGE = 'zho';
const RESUME_AFTER_HIM_MS = 1500;
const REPLY_TIMEOUT_MS = 45000;

function isFinal(call: VoiceCall | null): boolean {
  return !!call && FINAL_CALL_STATES.indexOf(call.state) !== -1;
}

// Why the mic is missing, inside the Elpis shell only (a desktop browser just types).
const SPEECH_HINT: Partial<Record<SpeechStatus, string>> = {
  no_bridge: '这个 Elpis 版本还不能直接说话，装新版 Canary（#143 或更新）就有开麦按钮',
  unavailable: '手机没有可用的语音识别服务，先打字吧（系统设置里搜“语音输入”，装或启用一个）',
};

function describeError(err: unknown): string {
  if (err instanceof HttpError && err.status === 401) return '需要主人授权后才能通话';
  if (err instanceof HttpError && err.detail) return err.detail;
  return '网络不太好，稍后再试';
}

/** The fallback recognizer's locale for this call: the one frozen with it at ring, else Chinese. */
function speechLanguageOf(call: VoiceCall | null): string {
  return (call && call.speech) || SPEECH_LANGUAGE;
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
  const [voiceStatus] = useState(() => nativeVoice.status());
  const voiceSupported = voiceStatus === 'ready';
  const [speechStatus] = useState(() => nativeSpeech.status());
  const speechSupported = !voiceSupported && speechStatus === 'ready';
  const micSupported = voiceSupported || speechSupported;
  const [micOn, setMicOn] = useState(false);
  const [hearing, setHearing] = useState(false);
  const [partial, setPartial] = useState('');
  const [speaking, setSpeaking] = useState(false);
  const [languages, setLanguages] = useState<CallLanguage[]>([]);
  const [language, setLanguage] = useState('');

  const callRef = useRef<VoiceCall | null>(null);
  const utteranceChainRef = useRef(newUtteranceChain());
  const linesRef = useRef<CallLine[]>([]);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const queueRef = useRef<number[]>([]);
  const playingRef = useRef<number | null>(null);
  const streamRef = useRef<EventSource | null>(null);
  const retryRef = useRef<number | null>(null);
  const composingRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const micOnRef = useRef(false);
  const listeningRef = useRef(false);
  const awaitingReplyRef = useRef(false);
  const resumeTimerRef = useRef<number | null>(null);
  const replyTimerRef = useRef<number | null>(null);
  const onDrainedRef = useRef<() => void>(() => undefined);
  const onHimLineRef = useRef<() => void>(() => undefined);

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
      if (playingRef.current === null && !queueRef.current.length) onDrainedRef.current();
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
    setSpeaking(false);
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
    let heardHim = false;
    for (const line of incoming) {
      if (line.speaker === 'him' && !known[line.seq]) {
        queueRef.current.push(line.seq);
        heardHim = true;
      }
    }
    if (heardHim) onHimLineRef.current();
    playNext();
  }, [playNext]);

  // ── speech (Canary shell only) ──────────────────────────
  const clearTimer = (ref: { current: number | null }) => {
    if (ref.current !== null) {
      window.clearTimeout(ref.current);
      ref.current = null;
    }
  };

  const stopListening = useCallback(() => {
    if (!voiceSupported && listeningRef.current) nativeSpeech.cancel();
    listeningRef.current = false;
    setHearing(false);
    setPartial('');
  }, [voiceSupported]);

  const listen = useCallback(() => {
    if (voiceSupported) return;
    const current = callRef.current;
    if (!micOnRef.current || listeningRef.current || !current || current.state !== 'active') return;
    // The shell destroys the recognizer when the app is paused; never start one while hidden.
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
    if (awaitingReplyRef.current || playingRef.current !== null || queueRef.current.length) return;
    listeningRef.current = true;
    setHearing(true);
    nativeSpeech.start(speechLanguageOf(current));
  }, [voiceSupported]);

  const resumeListening = useCallback((delayMs: number) => {
    clearTimer(resumeTimerRef);
    if (!micOnRef.current) return;
    resumeTimerRef.current = window.setTimeout(() => {
      resumeTimerRef.current = null;
      listen();
    }, delayMs);
  }, [listen]);

  onHimLineRef.current = () => {
    awaitingReplyRef.current = false;
    clearTimer(replyTimerRef);
    clearTimer(resumeTimerRef);
    if (!voiceSupported) stopListening();
    setSpeaking(true);
  };
  onDrainedRef.current = () => {
    setSpeaking(false);
    if (!voiceSupported) resumeListening(RESUME_AFTER_HIM_MS);
  };

  /** Shared by typing and speech; not gated by `busy` so a recognized sentence is never dropped. */
  const sayText = useCallback((text: string) => {
    const current = callRef.current;
    if (!current || current.state !== 'active' || !text.trim()) return;
    stopSpeaking();
    if (!voiceSupported) {
      stopListening();
      clearTimer(resumeTimerRef);
    }
    awaitingReplyRef.current = true;
    clearTimer(replyTimerRef);
    replyTimerRef.current = window.setTimeout(() => {
      replyTimerRef.current = null;
      awaitingReplyRef.current = false;
      listen();
    }, REPLY_TIMEOUT_MS);
    setError('');
    voiceCall.say(current.id, text.trim()).then((line) => {
      addLines([line], false);
    }).catch((err) => {
      setError(describeError(err));
      awaitingReplyRef.current = false;
      clearTimer(replyTimerRef);
      resumeListening(1000);
    });
  }, [addLines, listen, resumeListening, stopListening, stopSpeaking, voiceSupported]);

  // Which languages can be dialed; none (or a failed load) keeps today's single Chinese call.
  useEffect(() => {
    let cancelled = false;
    voiceCall.languages().then((list) => {
      if (cancelled) return;
      setLanguages(list);
      setLanguage((prev) => prev || (list.length ? list[0].code : ''));
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!voiceSupported) return undefined;
    return nativeVoice.subscribe((event: VoiceEvent) => {
      const current = callRef.current;
      if (event.type === 'speech_start' && event.seq !== undefined) {
        setHearing(true);
        setPartial('');
        stopSpeaking();
        setSpeaking(false);
        awaitingReplyRef.current = false;
        clearTimer(replyTimerRef);
        clearTimer(resumeTimerRef);
        if (current && current.state === 'active') {
          const prev = startUtterance(utteranceChainRef.current, current.id, event.seq);
          void voiceCall.interrupt(current.id, event.seq, prev).catch(() => undefined);
        }
      } else if (event.type === 'utterance_ready' && event.seq !== undefined) {
        setHearing(false);
        const wav = nativeVoice.takeUtterance(event.seq);
        if (!current || current.state !== 'active') return;
        const prev = utterancePrev(utteranceChainRef.current, current.id, event.seq);
        if (!wav) {
          setError('utterance_unavailable');
          void voiceCall.discard(current.id, event.seq, prev).catch(() => undefined);
          return;
        }
        void voiceCall.hear(current.id, wav, event.seq, P2F_LANGUAGE, prev).then((result) => {
          if (result.line && !result.stale) addLines([result.line], false);
        }).catch((err) => {
          // Scribe failure is recoverable; the native capture remains on for the next sentence.
          setError(describeError(err));
        });
      } else if (event.type === 'utterance_discarded' && event.seq !== undefined) {
        setHearing(false);
        if (current && current.state === 'active') {
          const prev = utterancePrev(utteranceChainRef.current, current.id, event.seq);
          void voiceCall.discard(current.id, event.seq, prev).catch(() => undefined);
        }
      } else if (event.type === 'error') {
        setHearing(false);
        setPartial('');
        setError(event.code || 'capture_failed');
        if (event.code === 'permission_denied' || event.code === 'no_permission') {
          micOnRef.current = false;
          setMicOn(false);
        }
      } else if (event.type === 'paused') {
        setHearing(false);
        setPartial('');
        setMicOn(false);
      } else if (event.type === 'resumed') {
        if (micOnRef.current && current && current.state === 'active') {
          setMicOn(true);
          nativeVoice.start();
        }
      }
    });
  }, [addLines, stopSpeaking, voiceSupported]);

  useEffect(() => {
    if (!speechSupported) return undefined;
    return nativeSpeech.subscribe((event: SpeechEvent) => {
      if (!micOnRef.current) return;
      if (event.type === 'partial') {
        setPartial(event.text || '');
      } else if (event.type === 'final') {
        listeningRef.current = false;
        setHearing(false);
        setPartial('');
        if ((event.text || '').trim()) sayText(event.text || '');
        else resumeListening(300);
      } else if (event.type === 'error') {
        listeningRef.current = false;
        setHearing(false);
        setPartial('');
        if (event.code === 'no_permission' || event.code === 'unavailable') {
          micOnRef.current = false;
          setMicOn(false);
          setError(event.code === 'no_permission' ? '需要麦克风权限才能直接说话' : '这台手机没有可用的语音识别，先打字吧');
        } else if (event.code !== 'paused') {
          // 'paused': the shell released the recognizer in onPause; 'resumed' brings it back.
          resumeListening(event.code === 'busy' || event.code === 'client' ? 800 : 300);
        }
      } else if (event.type === 'resumed') {
        resumeListening(300);
      }
    });
  }, [speechSupported, sayText, resumeListening]);

  // Activity/page pause stops native capture; a visible active call resumes the desired mic once.
  useEffect(() => {
    if (!voiceSupported && !speechSupported) return undefined;
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') {
        clearTimer(resumeTimerRef);
        if (voiceSupported) {
          nativeVoice.stop();
          setHearing(false);
          setMicOn(false);
        } else {
          stopListening();
        }
        return;
      }
      if (micOnRef.current) {
        if (voiceSupported) {
          setMicOn(true);
          nativeVoice.start();
        } else {
          resumeListening(300);
        }
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, [speechSupported, voiceSupported, stopListening, resumeListening]);

  const turnMicOff = useCallback(() => {
    micOnRef.current = false;
    setMicOn(false);
    clearTimer(resumeTimerRef);
    if (voiceSupported) nativeVoice.cancel();
    else stopListening();
  }, [stopListening, voiceSupported]);

  const toggleMic = () => {
    if (micOnRef.current) {
      turnMicOff();
      return;
    }
    const permission = voiceSupported ? nativeVoice.requestPermission() : nativeSpeech.requestPermission();
    permission.then((granted) => {
      if (!granted) {
        setError('permission_denied');
        return;
      }
      setError('');
      micOnRef.current = true;
      setMicOn(true);
      if (voiceSupported) nativeVoice.start();
      else listen();
    }).catch(() => undefined);
  };

  /** She wants to talk now: drop the rest of what he is saying and listen. */
  const interruptHim = () => {
    const current = callRef.current;
    stopSpeaking();
    if (current) voiceCall.interrupt(current.id).catch(() => undefined);
    awaitingReplyRef.current = false;
    clearTimer(replyTimerRef);
    clearTimer(resumeTimerRef);
    if (!voiceSupported) listen();
  };

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

  useEffect(() => {
    if (call && call.state === 'active') return;
    turnMicOff();
    utteranceChainRef.current = newUtteranceChain();  // the native seq itself never resets
    awaitingReplyRef.current = false;
    clearTimer(replyTimerRef);
  }, [call && call.state, turnMicOff]); // eslint-disable-line react-hooks/exhaustive-deps

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
      if (voiceSupported) nativeVoice.cancel();
      else nativeSpeech.cancel();
      clearTimer(resumeTimerRef);
      clearTimer(replyTimerRef);
    };
  }, [closeStream, stopSpeaking, voiceSupported]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length, partial]);

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

  /** Inside a tap: lets his lines play later without another gesture. */
  const unlockAudio = () => {
    const audio = ensureAudio();
    audio.src = SILENT_WAV;
    const unlocked = audio.play();
    if (unlocked && typeof unlocked.catch === 'function') unlocked.catch(() => undefined);
  };

  const answer = () => {
    if (!call) return;
    unlockAudio();
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
    setDraft('');
    sayText(text);
  };

  const resetForNewCall = () => {
    setRecord('');
    setVoiceless({});
    linesRef.current = [];
    setLines([]);
  };

  /** She calls him: ring and pick up at once, so the call opens already connected. */
  const dial = () => {
    unlockAudio();
    void run(async () => {
      resetForNewCall();
      const ringing = await voiceCall.ring(language || undefined);
      setNow(Date.now());
      setCall(await voiceCall.answer(ringing.id));
    });
  };

  const testRing = () => {
    void run(async () => {
      resetForNewCall();
      setCall(await voiceCall.ring(language || undefined));
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
          {partial ? (
            <div className="call-line call-line--her call-line--pending">
              <span className="call-bubble">{partial}</span>
            </div>
          ) : null}
        </div>
      </div>

      {error ? <div className="call-error">{error}</div> : null}
      {state === 'active' && !voiceSupported && SPEECH_HINT[speechStatus] ? (
        <div className="call-hint" data-testid="call-speech-hint">{SPEECH_HINT[speechStatus]}</div>
      ) : null}

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
              {micSupported ? (
                <button
                  type="button"
                  className={`call-mic${micOn ? ' is-on' : ''}${hearing ? ' is-hearing' : ''}`}
                  onClick={toggleMic}
                  aria-pressed={micOn}
                >
                  {micOn ? (hearing ? '在听…' : '麦已开') : '开麦'}
                </button>
              ) : null}
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
            <div className="call-buttons hstack hstack-26">
              {speaking ? (
                <button type="button" className="call-btn call-btn--interrupt" onClick={interruptHim}>打断</button>
              ) : null}
              <button type="button" className="call-btn call-btn--decline" onClick={hangup} disabled={busy}>挂断</button>
            </div>
          </div>
        ) : (
          <div className="vstack vstack-10">
            {languages.length ? (
              <div className="call-langs hstack hstack-8" role="radiogroup" aria-label="通话语言">
                {languages.map((entry) => (
                  <button
                    key={entry.code}
                    type="button"
                    role="radio"
                    aria-checked={entry.code === language}
                    className={`call-lang${entry.code === language ? ' is-on' : ''}`}
                    onClick={() => setLanguage(entry.code)}
                    disabled={busy}
                  >
                    {entry.label}
                  </button>
                ))}
              </div>
            ) : null}
            <div className="call-buttons hstack hstack-12">
              <button type="button" className="call-dial" onClick={dial} disabled={busy}>拨给费佳</button>
              <button type="button" className="call-test" onClick={testRing} disabled={busy}>测试来电</button>
            </div>
          </div>
        )}
      </footer>
    </div>
  );
}

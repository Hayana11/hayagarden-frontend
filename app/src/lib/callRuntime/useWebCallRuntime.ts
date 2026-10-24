// The browser call runtime (P2H-3B): everything a call does between the page and
// /api/call/* — capture (P2F native voice, or the P2E fallback recognizer), the
// utterance chain, the reply stream, his play queue (P2H-1 native audio or
// <audio>), barge-in, the reply timeout and hangup. Moved out of CallScreen
// unchanged; CallScreen now only renders what this returns.
import { useCallback, useEffect, useRef, useState } from 'react';
import { HttpError } from '../http';
import { nativeSpeech, type SpeechEvent } from '../nativeSpeech';
import { nativeVoice, type VoiceEvent } from '../nativeVoice';
import { nativeCallAudio } from '../nativeCallAudio';
import {
  FINAL_CALL_STATES,
  lastSeq,
  mergeLines,
  newUtteranceChain,
  startUtterance,
  utterancePrev,
  voiceCall,
  type CallLine,
  type VoiceCall,
} from '../voiceCall';
import type { CallRuntime } from './types';

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

function describeError(err: unknown): string {
  if (err instanceof HttpError && err.status === 401) return '需要主人授权后才能通话';
  if (err instanceof HttpError && err.detail) return err.detail;
  return '网络不太好，稍后再试';
}

/** The fallback recognizer's locale for this call: the one frozen with it at ring, else Chinese. */
function speechLanguageOf(call: VoiceCall | null): string {
  return (call && call.speech) || SPEECH_LANGUAGE;
}

export function useWebCallRuntime(): CallRuntime {
  const [call, setCall] = useState<VoiceCall | null>(null);
  const [lines, setLines] = useState<CallLine[]>([]);
  const [record, setRecord] = useState('');
  const [error, setError] = useState('');
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
  // Canary shell: his voice plays natively (earpiece by default); the button follows the shell's real route.
  const [nativeAudio] = useState(() => nativeCallAudio.supported());
  const [speaker, setSpeaker] = useState(false);

  const callRef = useRef<VoiceCall | null>(null);
  const utteranceChainRef = useRef(newUtteranceChain());
  const linesRef = useRef<CallLine[]>([]);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const queueRef = useRef<number[]>([]);
  const playingRef = useRef<number | null>(null);
  const nativeDoneRef = useRef<((failed: boolean) => void) | null>(null);
  const streamRef = useRef<EventSource | null>(null);
  const retryRef = useRef<number | null>(null);
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
    playingRef.current = seq;
    const done = (failed: boolean) => {
      if (playingRef.current !== seq) return;
      if (failed) setVoiceless((prev) => ({ ...prev, [seq]: true }));
      playingRef.current = null;
      playNext();
      if (playingRef.current === null && !queueRef.current.length) onDrainedRef.current();
    };
    if (nativeAudio) {
      nativeDoneRef.current = done;
      nativeCallAudio.play(current.id, seq);
      return;
    }
    const audio = ensureAudio();
    audio.onended = () => done(false);
    audio.onerror = () => done(true);
    audio.src = voiceCall.audioUrl(current.id, seq);
    const started = audio.play();
    if (started && typeof started.catch === 'function') started.catch(() => done(true));
  }, [ensureAudio, nativeAudio]);

  const stopSpeaking = useCallback(() => {
    queueRef.current = [];
    playingRef.current = null;
    setSpeaking(false);
    nativeDoneRef.current = null;
    if (nativeAudio) nativeCallAudio.stop();
    const audio = audioRef.current;
    if (audio) {
      audio.onended = null;
      audio.onerror = null;
      audio.pause();
    }
  }, [nativeAudio]);

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

  // Native playback events: a line ending moves the queue on; the route drives the speaker button.
  useEffect(() => {
    if (!nativeAudio) return undefined;
    return nativeCallAudio.subscribe((event) => {
      if (event.type === 'route') {
        setSpeaker(event.route === 'speaker');
        return;
      }
      if (event.type === 'started') return;
      const done = nativeDoneRef.current;
      if (done && playingRef.current === event.seq) {
        nativeDoneRef.current = null;
        done(event.type === 'error');
      }
    });
  }, [nativeAudio]);

  // Call audio mode only while the call is live; leaving it gives normal media routing back.
  useEffect(() => {
    if (!nativeAudio || !call || call.state !== 'active') return undefined;
    nativeCallAudio.begin();
    return () => {
      nativeCallAudio.end();
      setSpeaker(false);
    };
  }, [nativeAudio, call && call.id, call && call.state]); // eslint-disable-line react-hooks/exhaustive-deps

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
    if (nativeAudio) return;  // the shell plays natively; no autoplay gesture needed
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

  const resetForNewCall = () => {
    setRecord('');
    setVoiceless({});
    linesRef.current = [];
    setLines([]);
  };

  /** She calls him: ring and pick up at once, so the call opens already connected. */
  const dial = (language?: string) => {
    unlockAudio();
    void run(async () => {
      resetForNewCall();
      const ringing = await voiceCall.ring(language || undefined);
      setNow(Date.now());
      setCall(await voiceCall.answer(ringing.id));
    });
  };

  const testRing = (language?: string) => {
    void run(async () => {
      resetForNewCall();
      setCall(await voiceCall.ring(language || undefined));
    });
  };

  return {
    kind: 'web',
    call,
    lines,
    record,
    error,
    busy,
    now,
    voiceless,
    voiceSupported,
    speechStatus,
    micSupported,
    micOn,
    hearing,
    partial,
    speaking,
    speakerAvailable: nativeAudio,
    speaker,
    answer,
    decline,
    hangup,
    dial,
    testRing,
    say: sayText,
    toggleMic,
    interrupt: interruptHim,
    setSpeaker: (on: boolean) => nativeCallAudio.setSpeaker(on),
  };
}

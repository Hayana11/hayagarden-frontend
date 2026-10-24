// /dash/call — voice call page. Typing everywhere; in the Elpis Canary shell (P2E) the mic
// turns it into a hands-free call: she speaks, ~2s of silence ends the sentence, it is sent.
// The microphone is off while he speaks and comes back 1.5s after his last line finished.
// This page is UI only (P2H-3B): the call itself runs in a CallRuntime (lib/callRuntime),
// today the WebView runtime over /api/call/* (lib/voiceCall.ts).
import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';
import { useCallRuntime, type SpeechStatus } from '../lib/callRuntime';
import { useSubtitleMode } from '../lib/useSubtitleMode';
import { ROUTES } from '../navigation';
import {
  formatCallTimer,
  voiceCall,
  type CallLanguage,
  displayLineText,
  hasGloss,
  subtitleToggleLabel,
} from '../lib/voiceCall';
import './CallScreen.css';

// Why the mic is missing, inside the Elpis shell only (a desktop browser just types).
const SPEECH_HINT: Partial<Record<SpeechStatus, string>> = {
  no_bridge: '这个 Elpis 版本还不能直接说话，装新版 Canary（#143 或更新）就有开麦按钮',
  unavailable: '手机没有可用的语音识别服务，先打字吧（系统设置里搜“语音输入”，装或启用一个）',
};

export function CallScreen() {
  const navigate = useNavigate();
  const runtime = useCallRuntime();
  const {
    call, lines, record, error, busy, now, voiceless, voiceSupported, speechStatus,
    micSupported, micOn, hearing, partial, speaking, speakerAvailable, speaker,
  } = runtime;
  const [draft, setDraft] = useState('');
  const [subtitleMode, toggleSubtitleMode] = useSubtitleMode();
  const [languages, setLanguages] = useState<CallLanguage[]>([]);
  const [language, setLanguage] = useState('');
  const composingRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

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
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length, partial]);

  const send = () => {
    const text = draft.trim();
    if (!call || call.state !== 'active' || !text) return;
    setDraft('');
    runtime.say(text);
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
              <span className="call-bubble">
                {displayLineText(line, subtitleMode)}
                {hasGloss(line) ? (
                  <button type="button" className="call-subtitle-toggle" onClick={toggleSubtitleMode}>
                    {subtitleToggleLabel(subtitleMode)}
                  </button>
                ) : null}
              </span>
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
            <button type="button" className="call-btn call-btn--decline" onClick={runtime.decline} disabled={busy}>拒绝</button>
            <button type="button" className="call-btn call-btn--answer" onClick={runtime.answer} disabled={busy}>接听</button>
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
                  onClick={runtime.toggleMic}
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
                <button type="button" className="call-btn call-btn--interrupt" onClick={runtime.interrupt}>打断</button>
              ) : null}
              {speakerAvailable ? (
                <button
                  type="button"
                  className={`call-btn call-btn--speaker${speaker ? ' is-on' : ''}`}
                  onClick={() => runtime.setSpeaker(!speaker)}
                  aria-pressed={speaker}
                >
                  免提
                </button>
              ) : null}
              <button type="button" className="call-btn call-btn--decline" onClick={runtime.hangup} disabled={busy}>挂断</button>
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
              <button type="button" className="call-dial" onClick={() => runtime.dial(language)} disabled={busy}>拨给费佳</button>
              <button type="button" className="call-test" onClick={() => runtime.testRing(language)} disabled={busy}>测试来电</button>
            </div>
          </div>
        )}
      </footer>
    </div>
  );
}

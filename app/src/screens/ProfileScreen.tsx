import { useCallback, useEffect, useMemo, useState, type CSSProperties } from 'react';
import { useNavigate } from 'react-router-dom';
import { HttpError, http } from '../lib/http';
import { fetchToolCompanionHints, type ToolCompanionHints } from '../lib/toolCompanionHints';
import {
  fetchDisplayThinkingPrompt,
  resetDisplayThinkingPrompt,
  saveDisplayThinkingPrompt,
  validateDisplayThinkingPrompt,
} from '../lib/displayThinking';
import { estimatePersonaTokens } from '../lib/personaTokens';
import { FONT_CN, FONT_DISPLAY } from '../lib/typography';
import './ProfileScreen.css';

const LIGHT_VARS: Record<string, string> = {
  '--bg': '#F7F1EE', '--card': '#FFFFFF', '--card2': '#F6EFEC', '--bubble': '#F0DFDB',
  '--ink': '#4A3F3C', '--ink2': '#6B5A55', '--mut': '#8C7B76', '--faint': '#A99590', '--ghost': '#C4B4AF',
  '--line': '#F0E6E2', '--rose': '#B76E79', '--deep': '#9C3B4A', '--rosebg': 'rgba(183,110,121,0.10)',
  '--shadow': 'rgba(183,110,121,0.10)', '--shadow2': 'rgba(183,110,121,0.20)',
  '--ok': '#7A9B6D', '--err': '#C25450', '--gold': '#D9A441',
  '--serif': FONT_CN, '--display': FONT_DISPLAY,
};

const DARK_VARS: Record<string, string> = {
  '--bg': '#211A18', '--card': '#2B2220', '--card2': '#362B28', '--bubble': '#3E2E30',
  '--ink': '#EFE5E1', '--ink2': '#D9C9C3', '--mut': '#B4A19B', '--faint': '#93817C', '--ghost': '#6E5F5A',
  '--line': '#3B302D', '--rose': '#C98A93', '--deep': '#D89AA2', '--rosebg': 'rgba(201,138,147,0.16)',
  '--shadow': 'rgba(0,0,0,0.28)', '--shadow2': 'rgba(0,0,0,0.45)',
  '--ok': '#8FAF80', '--err': '#D97B76', '--gold': '#DFB25E',
  '--serif': FONT_CN, '--display': FONT_DISPLAY,
};

type PersonaResponse = { ok?: boolean; content?: string; error?: string };

export function ProfileScreen() {
  const navigate = useNavigate();
  const [theme] = useState(() => {
    try {
      const raw = localStorage.getItem('fyodor-chat-settings');
      const parsed = raw ? JSON.parse(raw) as { theme?: string } : {};
      return parsed.theme === 'dark' ? 'dark' : 'light';
    } catch {
      return 'light';
    }
  });
  const vars = theme === 'dark' ? DARK_VARS : LIGHT_VARS;
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState('');
  const [savedPersona, setSavedPersona] = useState('');
  const [draftPersona, setDraftPersona] = useState('');
  const [personaLoaded, setPersonaLoaded] = useState(false);
  const [personaLoadError, setPersonaLoadError] = useState('');
  const [savedDisplayThinkingPrompt, setSavedDisplayThinkingPrompt] = useState('');
  const [draftDisplayThinkingPrompt, setDraftDisplayThinkingPrompt] = useState('');
  const [displayThinkingDefaultPrompt, setDisplayThinkingDefaultPrompt] = useState('');
  const [displayThinkingLoaded, setDisplayThinkingLoaded] = useState(false);
  const [displayThinkingLoadError, setDisplayThinkingLoadError] = useState('');
  const [resetDisplayThinking, setResetDisplayThinking] = useState(false);
  const [toolHints, setToolHints] = useState<ToolCompanionHints | null>(null);
  const [toolHintsLoadError, setToolHintsLoadError] = useState('');

  const personaDirty = personaLoaded && draftPersona !== savedPersona;
  const displayThinkingDirty = displayThinkingLoaded
    && (draftDisplayThinkingPrompt !== savedDisplayThinkingPrompt || resetDisplayThinking);
  const dirty = personaDirty || displayThinkingDirty;
  const characterCount = useMemo(() => Array.from(draftPersona).length, [draftPersona]);
  const lineCount = useMemo(() => draftPersona ? draftPersona.split(/\r?\n/).length : 0, [draftPersona]);
  const personaTokenCount = useMemo(() => estimatePersonaTokens(draftPersona), [draftPersona]);

  const showToast = useCallback((message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(''), 2800);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setPersonaLoadError('');
    setDisplayThinkingLoadError('');
    setToolHintsLoadError('');
    const [personaResult, displayThinkingResult, hintsResult] = await Promise.allSettled([
      http.get<PersonaResponse>('/api/persona'),
      fetchDisplayThinkingPrompt(),
      fetchToolCompanionHints(),
    ]);

    if (personaResult.status === 'fulfilled' && personaResult.value.ok !== false) {
      const content = typeof personaResult.value.content === 'string' ? personaResult.value.content : '';
      setSavedPersona(content);
      setDraftPersona(content);
      setPersonaLoaded(true);
    } else {
      const error = personaResult.status === 'rejected'
        ? personaResult.reason
        : new Error(personaResult.value.error || '人设加载失败');
      const detail = error instanceof HttpError && error.detail
        ? error.detail
        : error instanceof Error ? error.message : '人设加载失败';
      setPersonaLoaded(false);
      setPersonaLoadError(detail);
      showToast(detail);
    }

    if (displayThinkingResult.status === 'fulfilled' && displayThinkingResult.value.ok !== false) {
      const response = displayThinkingResult.value;
      const prompt = typeof response.prompt === 'string' ? response.prompt : '';
      setSavedDisplayThinkingPrompt(prompt);
      setDraftDisplayThinkingPrompt(prompt);
      setDisplayThinkingDefaultPrompt(typeof response.default_prompt === 'string' ? response.default_prompt : '');
      setDisplayThinkingLoaded(true);
      setResetDisplayThinking(false);
    } else {
      const error = displayThinkingResult.status === 'rejected'
        ? displayThinkingResult.reason
        : new Error(displayThinkingResult.value.error || '可见思绪加载失败');
      const detail = error instanceof HttpError && error.detail
        ? error.detail
        : error instanceof Error ? error.message : '可见思绪加载失败';
      setDisplayThinkingLoaded(false);
      setDisplayThinkingLoadError(detail);
      showToast(detail);
    }

    if (hintsResult.status === 'fulfilled') {
      setToolHints(hintsResult.value);
    } else {
      const detail = hintsResult.reason instanceof HttpError && hintsResult.reason.detail
        ? hintsResult.reason.detail
        : hintsResult.reason instanceof Error ? hintsResult.reason.message : '工具直觉加载失败';
      setToolHints(null);
      setToolHintsLoadError(detail);
      showToast(detail);
    }
    setLoading(false);
  }, [showToast]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    const warnBeforeLeave = (event: BeforeUnloadEvent) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', warnBeforeLeave);
    return () => window.removeEventListener('beforeunload', warnBeforeLeave);
  }, [dirty]);

  const close = useCallback(() => {
    if (dirty && !window.confirm('档案有尚未保存的修改，确定离开吗？')) return;
    navigate('/chat');
  }, [dirty, navigate]);

  const reload = useCallback(() => {
    if (dirty && !window.confirm('确定放弃尚未保存的修改，重新读取吗？')) return;
    void load();
  }, [dirty, load]);

  const persist = useCallback(async () => {
    if (saving || !dirty) return;
    if (personaDirty && !draftPersona.trim()) {
      showToast('为了避免误操作，人设正文不能保存为空');
      return;
    }
    if (displayThinkingDirty && !resetDisplayThinking) {
      const validationError = validateDisplayThinkingPrompt(draftDisplayThinkingPrompt);
      if (validationError) {
        showToast(validationError);
        return;
      }
    }
    setSaving(true);
    try {
      if (personaDirty) {
        const response = await http.post<PersonaResponse>('/api/persona', { content: draftPersona });
        if (response.ok === false) throw new Error(response.error || '人设保存失败');
        setSavedPersona(draftPersona);
      }
      if (displayThinkingDirty) {
        const response = resetDisplayThinking
          ? await resetDisplayThinkingPrompt()
          : await saveDisplayThinkingPrompt(draftDisplayThinkingPrompt);
        if (response.ok === false) throw new Error(response.error || '可见思绪保存失败');
        const prompt = typeof response.prompt === 'string' ? response.prompt : draftDisplayThinkingPrompt;
        setSavedDisplayThinkingPrompt(prompt);
        setDraftDisplayThinkingPrompt(prompt);
        setDisplayThinkingDefaultPrompt(
          typeof response.default_prompt === 'string'
            ? response.default_prompt
            : displayThinkingDefaultPrompt,
        );
        setResetDisplayThinking(false);
      }
      const messages = [];
      if (personaDirty) messages.push('费佳人设已保存，聊天网关正在重启');
      if (displayThinkingDirty) messages.push('可见思绪已保存；下一轮聊天生效，未修改 persona.md');
      showToast(messages.join('；'));
    } catch (error) {
      const detail = error instanceof HttpError && error.detail
        ? error.detail
        : error instanceof Error ? error.message : '保存失败';
      showToast(detail);
    } finally {
      setSaving(false);
    }
  }, [
    dirty,
    displayThinkingDefaultPrompt,
    displayThinkingDirty,
    draftDisplayThinkingPrompt,
    draftPersona,
    personaDirty,
    resetDisplayThinking,
    saving,
    showToast,
  ]);

  return (
    <div className="profile-root dash-fullscreen-page" style={{ ...(vars as CSSProperties) }}>
      {loading ? <div className="profile-loading">正在读取费佳档案…</div> : (
        <div className="profile-page">
          <header className="profile-header">
            <button type="button" className="profile-round-button" aria-label="关闭费佳档案" onClick={close}>×</button>
            <div className="profile-title">Fyodor Profile</div>
            <button type="button" className="profile-save-button" aria-label="保存费佳档案" disabled={!dirty || saving} onClick={() => void persist()}>
              {saving ? '…' : '保存'}
            </button>
          </header>

          <section className="profile-hero">
            <div className="profile-avatar" aria-hidden="true">Θ</div>
            <div className="profile-hero-copy">
              <div className="profile-hero-name">费奥多尔</div>
              <div className="profile-hero-alias"><span className="font-display">Fyodor</span><span> · </span><span className="font-cn">费佳</span></div>
              <div className="profile-hero-tagline">他的身份、关系与工具直觉，都在这里保持透明。</div>
            </div>
          </section>

          <section className="profile-meta-grid" aria-label="人设信息">
            <div className="profile-meta-card"><strong>{characterCount.toLocaleString()}</strong><span>字符</span></div>
            <div className="profile-meta-card"><strong>{lineCount.toLocaleString()}</strong><span>行</span></div>
            <div className="profile-meta-card" aria-label="完整人设所用的 token">
              <strong>{personaTokenCount.toLocaleString()}</strong>
              <span>人设 token</span>
            </div>
          </section>

          <section className="profile-section">
            <div className="profile-persona-heading">
              <div>
                <div className="profile-section-title">费佳的完整人设</div>
                <div className="profile-help-text">这里直接读取实际生效的 persona.md。保存会写入 persona.md，并沿用现有行为重启聊天网关。</div>
              </div>
              <button type="button" className="profile-reload-button" disabled={saving} onClick={reload}>重新读取</button>
            </div>
            <div className="profile-persona-card">
              {personaLoadError ? (
                <div className="profile-help-text">读取 persona.md 失败：{personaLoadError}。当前不展示伪造的人设内容，请稍后重新读取。</div>
              ) : (
                <textarea className="profile-persona-editor" value={draftPersona} spellCheck={false} aria-label="费佳的完整人设正文" onChange={(event) => setDraftPersona(event.target.value)} />
              )}
            </div>
          </section>

          <section className="profile-section">
            <div className="profile-persona-heading">
              <div>
                <div className="profile-section-title">可见思绪</div>
                <div className="profile-help-text">控制回复前 &lt;思绪&gt;...&lt;/思绪&gt; 的写法。只影响界面展示的角色内心独白，不修改完整人设。</div>
              </div>
              <button
                type="button"
                className="profile-reload-button"
                disabled={saving || !displayThinkingDefaultPrompt}
                onClick={() => {
                  setDraftDisplayThinkingPrompt(displayThinkingDefaultPrompt);
                  setResetDisplayThinking(true);
                }}
              >
                恢复默认
              </button>
            </div>
            <div className="profile-persona-card">
              {displayThinkingLoadError ? (
                <div className="profile-help-text">读取可见思绪失败：{displayThinkingLoadError}。当前不展示伪造的 prompt，请稍后重新读取。</div>
              ) : (
                <textarea
                  className="profile-persona-editor profile-thinking-editor"
                  value={draftDisplayThinkingPrompt}
                  spellCheck={false}
                  aria-label="可见思绪 prompt"
                  maxLength={12000}
                  onChange={(event) => {
                    setDraftDisplayThinkingPrompt(event.target.value);
                    setResetDisplayThinking(false);
                  }}
                />
              )}
            </div>
          </section>

          <section className="profile-section">
            <div>
              <div className="profile-section-title">费佳的工具直觉</div>
              <div className="profile-help-text">只读展示当前注入给费佳的工具直觉。真实能力边界由系统固定。</div>
            </div>
            {toolHintsLoadError ? (
              <div className="profile-help-text">工具直觉暂时读不到：{toolHintsLoadError}。当前不展示伪造的注入预览，请稍后重新读取。</div>
            ) : toolHints?.prompt_preview ? (
              <div className="profile-tool-prompt-preview">
                <div className="profile-tool-prompt-preview__title">
                  <span>注入预览</span>
                </div>
                <p>{toolHints.prompt_preview}</p>
              </div>
            ) : (
              <div className="profile-help-text">当前没有可展示的注入预览。</div>
            )}
          </section>

          <div className="profile-persona-note">Persona 保存会写入 persona.md 并重启 frontend-gw。可见思绪保存写入 runtime_config，下一轮聊天生效，不修改 persona.md。系统不会自动截断或改写原文。</div>
        </div>
      )}
      {toast && <div className="profile-toast"><span>{toast}</span></div>}
    </div>
  );
}

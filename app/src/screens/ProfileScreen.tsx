import { useCallback, useEffect, useMemo, useState, type CSSProperties } from 'react';
import { useNavigate } from 'react-router-dom';
import { HttpError, http } from '../lib/http';
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

type PersonaResponse = {
  ok?: boolean;
  content?: string;
  error?: string;
};

export function ProfileScreen() {
  const navigate = useNavigate();
  const [theme] = useState(() => {
    try {
      const raw = localStorage.getItem('fyodor-chat-settings');
      if (!raw) return 'light';
      const parsed = JSON.parse(raw) as { theme?: string };
      return parsed.theme === 'dark' ? 'dark' : 'light';
    } catch {
      return 'light';
    }
  });
  const vars = theme === 'dark' ? DARK_VARS : LIGHT_VARS;

  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState('');
  const [savedContent, setSavedContent] = useState('');
  const [draftContent, setDraftContent] = useState('');

  const dirty = draftContent !== savedContent;
  const characterCount = useMemo(() => Array.from(draftContent).length, [draftContent]);
  const lineCount = useMemo(
    () => draftContent ? draftContent.split(/\r?\n/).length : 0,
    [draftContent],
  );

  const showToast = useCallback((message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(''), 2600);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await http.get<PersonaResponse>('/api/persona');
      if (data.ok === false) throw new Error(data.error || '人设加载失败');
      const content = typeof data.content === 'string' ? data.content : '';
      setSavedContent(content);
      setDraftContent(content);
    } catch (error) {
      const detail = error instanceof HttpError && error.detail
        ? error.detail
        : error instanceof Error ? error.message : '人设加载失败';
      showToast(detail);
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    void load();
  }, [load]);

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
    if (dirty && !window.confirm('人设有尚未保存的修改，确定离开吗？')) return;
    navigate('/chat');
  }, [dirty, navigate]);

  const reload = useCallback(() => {
    if (dirty && !window.confirm('确定放弃尚未保存的修改，重新读取服务器人设吗？')) return;
    void load();
  }, [dirty, load]);

  const persist = useCallback(async () => {
    if (saving || !dirty) return;
    if (!draftContent.trim()) {
      showToast('为了避免误操作，人设正文不能保存为空');
      return;
    }
    setSaving(true);
    try {
      const data = await http.post<PersonaResponse>('/api/persona', { content: draftContent });
      if (data.ok === false) throw new Error(data.error || '保存失败');
      setSavedContent(draftContent);
      showToast('费佳人设已保存，聊天网关正在重启');
    } catch (error) {
      const detail = error instanceof HttpError && error.detail
        ? error.detail
        : error instanceof Error ? error.message : '保存失败';
      showToast(detail);
    } finally {
      setSaving(false);
    }
  }, [dirty, draftContent, saving, showToast]);

  return (
    <div className="profile-root dash-fullscreen-page" style={{ ...(vars as CSSProperties) }}>
      {loading ? (
        <div className="profile-loading">正在读取费佳的人设…</div>
      ) : (
        <div className="profile-page">
          <header className="profile-header">
            <button type="button" className="profile-round-button" aria-label="关闭费佳档案" onClick={close}>×</button>
            <div className="profile-title">Fyodor Profile</div>
            <button
              type="button"
              className="profile-save-button"
              aria-label="保存费佳人设"
              disabled={!dirty || saving}
              onClick={() => void persist()}
            >
              {saving ? '…' : '✓'}
            </button>
          </header>

          <section className="profile-hero">
            <div className="profile-avatar" aria-hidden="true">Θ</div>
            <div className="profile-hero-copy">
              <div className="profile-hero-name">费奥多尔</div>
              <div className="profile-hero-alias">
                <span className="font-display">Fyodor</span>
                <span> · </span>
                <span className="font-cn">费佳</span>
              </div>
              <div className="profile-hero-tagline">他的身份、关系、语言与内在纹理，都住在下面这份人设里。</div>
            </div>
          </section>

          <section className="profile-meta-grid" aria-label="人设信息">
            <div className="profile-meta-card">
              <strong>{characterCount.toLocaleString()}</strong>
              <span>字符</span>
            </div>
            <div className="profile-meta-card">
              <strong>{lineCount.toLocaleString()}</strong>
              <span>行</span>
            </div>
            <div className="profile-meta-card">
              <strong className="profile-live-dot">已接入</strong>
              <span>聊天系统</span>
            </div>
          </section>

          <section className="profile-section">
            <div className="profile-persona-heading">
              <div>
                <div className="profile-section-title">费佳的完整人设</div>
                <div className="profile-help-text">这里直接读取 VPS 上实际生效的 persona.md，不是另一份空白 Profile。</div>
              </div>
              <button type="button" className="profile-reload-button" disabled={saving} onClick={reload}>重新读取</button>
            </div>
            <div className="profile-persona-card">
              <textarea
                className="profile-persona-editor"
                value={draftContent}
                spellCheck={false}
                aria-label="费佳的完整人设正文"
                onChange={(event) => setDraftContent(event.target.value)}
              />
            </div>
          </section>

          <div className="profile-persona-note">
            保存会立即写入 <code>persona.md</code> 并重启聊天网关。正在生成的回复可能被打断，已有聊天和记忆不会被修改。
          </div>
        </div>
      )}

      {toast && (
        <div className="profile-toast">
          <span>{toast}</span>
        </div>
      )}
    </div>
  );
}

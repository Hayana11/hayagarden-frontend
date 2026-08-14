import { useCallback, useEffect, useMemo, useState, type CSSProperties } from 'react';
import { useNavigate } from 'react-router-dom';
import { HttpError, http } from '../lib/http';
import {
  fetchToolCompanionHints,
  patchToolCompanionHint,
  type ToolCompanionHints,
} from '../lib/toolCompanion';
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

function cloneHints(value: ToolCompanionHints): ToolCompanionHints {
  return JSON.parse(JSON.stringify(value)) as ToolCompanionHints;
}
function previewFor(hints: ToolCompanionHints): string {
  const parts = ['## 费佳的工具直觉'];
  for (const group of hints.groups) {
    parts.push(`### ${group.label}`);
    for (const item of group.items) {
      parts.push(`【${item.display_label}】\n${item.companion_hint}\n真实能力边界：${item.physical_boundary}`);
    }
  }
  return parts.join('\n\n');
}

export function ProfileScreen() {
  const navigate = useNavigate();
  const [theme] = useState<'light' | 'dark'>(() => {
    try {
      return JSON.parse(localStorage.getItem('fyodor-chat-settings') || '{}').theme === 'dark' ? 'dark' : 'light';
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
  const [savedHints, setSavedHints] = useState<ToolCompanionHints | null>(null);
  const [draftHints, setDraftHints] = useState<ToolCompanionHints | null>(null);

  const dirtyPersona = draftContent !== savedContent;
  const dirtyHints = JSON.stringify(draftHints) !== JSON.stringify(savedHints);
  const dirty = dirtyPersona || dirtyHints;
  const characterCount = useMemo(() => Array.from(draftContent).length, [draftContent]);
  const lineCount = useMemo(
    () => draftContent ? draftContent.split(/\r?\n/).length : 0,
    [draftContent],
  );

  const showToast = useCallback((message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(''), 2800);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [persona, hints] = await Promise.all([
        http.get<PersonaResponse>('/api/persona'),
        fetchToolCompanionHints(),
      ]);
      if (persona.ok === false) throw new Error(persona.error || '人设加载失败');
      const content = typeof persona.content === 'string' ? persona.content : '';
      setSavedContent(content);
      setDraftContent(content);
      setSavedHints(hints);
      setDraftHints(cloneHints(hints));
    } catch (error) {
      const detail = error instanceof HttpError && error.detail
        ? error.detail
        : error instanceof Error ? error.message : '档案加载失败';
      showToast(detail);
    } finally {
      setLoading(false);
    }
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
    if (dirty && !window.confirm('确定放弃尚未保存的修改，重新读取服务器档案吗？')) return;
    void load();
  }, [dirty, load]);

  const updateItem = useCallback((capabilityId: string, field: 'display_label' | 'companion_hint', value: string) => {
    setDraftHints((current) => current ? {
      ...current,
      groups: current.groups.map((group) => ({
        ...group,
        items: group.items.map((item) => (
          item.capability_id === capabilityId ? { ...item, [field]: value } : item
        )),
      })),
    } : current);
  }, []);

  const resetItem = useCallback((capabilityId: string) => {
    setDraftHints((current) => current ? {
      ...current,
      groups: current.groups.map((group) => ({
        ...group,
        items: group.items.map((item) => (
          item.capability_id === capabilityId
            ? { ...item, display_label: item.default_display_label, companion_hint: item.default_companion_hint }
            : item
        )),
      })),
    } : current);
  }, []);

  const persist = useCallback(async () => {
    if (saving || !dirty) return;
    if (dirtyPersona && !draftContent.trim()) {
      showToast('为了避免误操作，人设正文不能保存为空');
      return;
    }
    setSaving(true);
    try {
      if (dirtyPersona) {
        const result = await http.post<PersonaResponse>('/api/persona', { content: draftContent });
        if (result.ok === false) throw new Error(result.error || '人设保存失败');
      }
      if (dirtyHints && draftHints && savedHints) {
        const savedById = new Map(savedHints.groups.flatMap((group) => group.items.map((item) => [item.capability_id, item])));
        for (const item of draftHints.groups.flatMap((group) => group.items)) {
          const old = savedById.get(item.capability_id);
          if (!old || (old.display_label === item.display_label && old.companion_hint === item.companion_hint)) continue;
          const reset = item.display_label === item.default_display_label
            && item.companion_hint === item.default_companion_hint;
          await patchToolCompanionHint(reset
            ? { capability_id: item.capability_id, reset: true }
            : { capability_id: item.capability_id, display_label: item.display_label, companion_hint: item.companion_hint });
        }
      }
      const refreshed = await fetchToolCompanionHints();
      setSavedContent(draftContent);
      setSavedHints(refreshed);
      setDraftHints(cloneHints(refreshed));
      showToast(dirtyPersona
        ? '档案已保存；工具说明将在下一次 resident 出生时生效'
        : '工具直觉已保存；下一次 resident 出生时生效');
    } catch (error) {
      const detail = error instanceof HttpError && error.detail
        ? error.detail
        : error instanceof Error ? error.message : '保存失败';
      showToast(detail);
    } finally {
      setSaving(false);
    }
  }, [dirty, dirtyHints, dirtyPersona, draftContent, draftHints, savedHints, saving, showToast]);

  return (
    <div className="profile-root dash-fullscreen-page" style={{ ...(vars as CSSProperties) }}>
      {loading ? <div className="profile-loading">正在读取费佳档案…</div> : (
        <div className="profile-page">
          <header className="profile-header">
            <button type="button" className="profile-round-button" aria-label="关闭费佳档案" onClick={close}>×</button>
            <div className="profile-title">Fyodor Profile</div>
            <button type="button" className="profile-save-button" aria-label="保存费佳档案" disabled={!dirty || saving} onClick={() => void persist()}>
              {saving ? '…' : '✓'}
            </button>
          </header>

          <section className="profile-hero">
            <div className="profile-avatar" aria-hidden="true">Θ</div>
            <div className="profile-hero-copy">
              <div className="profile-hero-name">费奥多尔</div>
              <div className="profile-hero-alias"><span className="font-display">Fyodor</span><span> · </span><span className="font-cn">费佳</span></div>
              <div className="profile-hero-tagline">他的身份、关系、工具直觉与内在纹理，都住在下面这份档案里。</div>
            </div>
          </section>

          <section className="profile-meta-grid" aria-label="人设信息">
            <div className="profile-meta-card"><strong>{characterCount.toLocaleString()}</strong><span>字符</span></div>
            <div className="profile-meta-card"><strong>{lineCount.toLocaleString()}</strong><span>行</span></div>
            <div className="profile-meta-card"><strong className="profile-live-dot">48h</strong><span>工具试用</span></div>
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
              <textarea className="profile-persona-editor" value={draftContent} spellCheck={false} aria-label="费佳的完整人设正文" onChange={(event) => setDraftContent(event.target.value)} />
            </div>
          </section>

          {draftHints && (
            <section className="profile-section">
              <div className="profile-persona-heading">
                <div>
                  <div className="profile-section-title">费佳的工具直觉 <span style={{ fontSize: 11, color: 'var(--rose)' }}>48h 试用</span></div>
                  <div className="profile-help-text">这里写的是费佳真正会看到的自然语言，不是 API 手册。首批覆盖正式 Chat 能力，并把任务型文件/代码能力单独收在最后一组。</div>
                </div>
              </div>

              {draftHints.groups.map((group) => (
                <div key={group.id} style={{ marginTop: 14, padding: 14, borderRadius: 16, background: 'var(--card2)' }}>
                  <div style={{ color: 'var(--ink)', fontSize: 13.5, letterSpacing: 1, marginBottom: 10 }}>{group.label}</div>
                  {group.items.map((item) => (
                    <div key={item.capability_id} style={{ marginTop: 9, padding: 12, borderRadius: 12, background: 'var(--card)' }}>
                      <div style={{ color: 'var(--mut)', fontSize: 11, marginBottom: 5 }}>小猫看到的工具名字 · {item.status_label}</div>
                      <input
                        value={item.display_label}
                        maxLength={80}
                        aria-label={`小猫看到的工具名字：${item.display_label}`}
                        onChange={(event) => updateItem(item.capability_id, 'display_label', event.target.value)}
                        style={{ width: '100%', boxSizing: 'border-box', border: '1px solid var(--line)', borderRadius: 9, padding: '8px 10px', background: 'var(--card)', color: 'var(--ink)' }}
                      />
                      <div style={{ color: 'var(--mut)', fontSize: 11, margin: '10px 0 5px' }}>工具直觉说明</div>
                      <textarea
                        value={item.companion_hint}
                        aria-label={`工具直觉说明：${item.display_label}`}
                        onChange={(event) => updateItem(item.capability_id, 'companion_hint', event.target.value)}
                        rows={3}
                        style={{ width: '100%', boxSizing: 'border-box', border: '1px solid var(--line)', borderRadius: 9, padding: '8px 10px', background: 'var(--card)', color: 'var(--ink)', lineHeight: 1.6, resize: 'vertical' }}
                      />
                      <div style={{ marginTop: 9, color: 'var(--mut)', fontSize: 11.5, lineHeight: 1.65 }}>
                        <strong style={{ color: 'var(--ink2)' }}>费佳实际会看到 · 逐字预览</strong>
                        <div style={{ whiteSpace: 'pre-wrap', marginTop: 4 }}>{`【${item.display_label}】\n${item.companion_hint}\n真实能力边界：${item.physical_boundary}`}</div>
                      </div>
                      <button type="button" onClick={() => resetItem(item.capability_id)} style={{ marginTop: 8, border: 0, background: 'transparent', color: 'var(--deep)', fontSize: 11.5, cursor: 'pointer' }}>恢复初始说明</button>
                    </div>
                  ))}
                </div>
              ))}

              <div style={{ marginTop: 14, padding: 14, borderRadius: 16, background: 'var(--card2)' }}>
                <div className="profile-section-title">费佳实际会看到 · 完整逐字预览</div>
                <pre style={{ whiteSpace: 'pre-wrap', margin: '10px 0 0', font: '12px/1.7 var(--serif)', color: 'var(--ink2)' }}>{previewFor(draftHints)}</pre>
              </div>
            </section>
          )}

          <div className="profile-persona-note">工具说明保存不会重启 frontend-gw；现有 resident 会按既有 system_changed 生命周期在下一次出生/换代时读取新的静态说明。</div>
        </div>
      )}
      {toast && <div className="profile-toast"><span>{toast}</span></div>}
    </div>
  );
}

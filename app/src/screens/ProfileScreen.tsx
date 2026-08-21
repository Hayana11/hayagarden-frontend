import { useCallback, useEffect, useMemo, useState, type CSSProperties } from 'react';
import { useNavigate } from 'react-router-dom';
import { HttpError, http } from '../lib/http';
import {
  fetchToolCompanionHints,
  patchToolCompanionHint,
  type ToolCompanionGroup,
  type ToolCompanionHints,
  type ToolCompanionTool,
} from '../lib/toolCompanionHints';
import { fetchCapabilityStates, type CapabilityState } from '../lib/capabilityStates';
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

function findTool(groups: ToolCompanionGroup[], capabilityId: string): ToolCompanionTool | null {
  for (const group of groups) {
    const tool = group.tools.find((item) => item.capability_id === capabilityId);
    if (tool) return tool;
  }
  return null;
}

function updateTool(
  value: ToolCompanionHints,
  capabilityId: string,
  update: Partial<Pick<ToolCompanionTool, 'display_label' | 'companion_hint'>>,
): ToolCompanionHints {
  const next = cloneHints(value);
  next.groups = next.groups.map((group) => ({
    ...group,
    tools: group.tools.map((tool) => (
      tool.capability_id === capabilityId ? { ...tool, ...update } : tool
    )),
  }));
  return next;
}

const RUNTIME_STATE_LABELS: Record<CapabilityState['runtime_state'], string> = {
  INHERIT: '默认开启',
  ON: '已开启',
  OFF: '已关闭',
  DENY: '不可用',
};

function presentCapabilityState(state: CapabilityState | undefined) {
  if (!state) {
    return {
      runtimeLabel: '状态未知',
      availabilityLabel: '当前不可确认',
      className: 'missing',
    };
  }
  return {
    runtimeLabel: RUNTIME_STATE_LABELS[state.runtime_state],
    availabilityLabel: state.effective_enabled ? '当前可用' : '当前不可用',
    className: state.runtime_state.toLowerCase(),
  };
}

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
  const [savedHints, setSavedHints] = useState<ToolCompanionHints | null>(null);
  const [draftHints, setDraftHints] = useState<ToolCompanionHints | null>(null);
  const [toolHintsLoadError, setToolHintsLoadError] = useState('');
  const [capabilityStates, setCapabilityStates] = useState<CapabilityState[]>([]);
  const [capabilityStateLoadError, setCapabilityStateLoadError] = useState('');
  const [resetCapabilities, setResetCapabilities] = useState<Record<string, boolean>>({});
  const [openTools, setOpenTools] = useState<Record<string, boolean>>({});

  const personaDirty = personaLoaded && draftPersona !== savedPersona;
  const toolDirty = useMemo(
    () => Boolean(draftHints && savedHints)
      && JSON.stringify(draftHints?.groups || []) !== JSON.stringify(savedHints?.groups || []),
    [draftHints, savedHints],
  );
  const hasResetIntent = Object.values(resetCapabilities).some(Boolean);
  const dirty = personaDirty || toolDirty || hasResetIntent;
  const characterCount = useMemo(() => Array.from(draftPersona).length, [draftPersona]);
  const lineCount = useMemo(() => draftPersona ? draftPersona.split(/\r?\n/).length : 0, [draftPersona]);

  const showToast = useCallback((message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(''), 2800);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setPersonaLoadError('');
    setToolHintsLoadError('');
    setCapabilityStateLoadError('');
    setCapabilityStates([]);
    setResetCapabilities({});
    const [personaResult, hintsResult, capabilityStateResult] = await Promise.allSettled([
      http.get<PersonaResponse>('/api/persona'),
      fetchToolCompanionHints(),
      fetchCapabilityStates(),
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

    if (hintsResult.status === 'fulfilled') {
      setSavedHints(cloneHints(hintsResult.value));
      setDraftHints(cloneHints(hintsResult.value));
    } else {
      const detail = hintsResult.reason instanceof HttpError && hintsResult.reason.detail
        ? hintsResult.reason.detail
        : hintsResult.reason instanceof Error ? hintsResult.reason.message : '工具直觉加载失败';
      setSavedHints(null);
      setDraftHints(null);
      setToolHintsLoadError(detail);
      showToast(detail);
    }
    if (capabilityStateResult.status === 'fulfilled') {
      setCapabilityStates(capabilityStateResult.value.states);
    } else {
      const detail = capabilityStateResult.reason instanceof HttpError && capabilityStateResult.reason.detail
        ? capabilityStateResult.reason.detail
        : capabilityStateResult.reason instanceof Error
          ? capabilityStateResult.reason.message
          : '真实能力状态暂时读不到';
      setCapabilityStates([]);
      setCapabilityStateLoadError(detail);
      showToast(detail);
    }
    setLoading(false);
  }, [showToast]);

  useEffect(() => { void load(); }, [load]);

  const capabilityStateById = useMemo(
    () => new Map(capabilityStates.map((state) => [state.capability_id, state])),
    [capabilityStates],
  );

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
    setSaving(true);
    try {
      if (personaDirty) {
        const response = await http.post<PersonaResponse>('/api/persona', { content: draftPersona });
        if (response.ok === false) throw new Error(response.error || '人设保存失败');
        setSavedPersona(draftPersona);
      }
      let latest = savedHints ? cloneHints(savedHints) : null;
      if (draftHints && savedHints && (toolDirty || hasResetIntent)) {
        latest = cloneHints(savedHints);
        for (const group of draftHints.groups) {
          for (const tool of group.tools) {
            const original = findTool(savedHints.groups, tool.capability_id);
            if (!original) continue;
            if (resetCapabilities[tool.capability_id]) {
              latest = await patchToolCompanionHint({
                capability_id: tool.capability_id,
                reset: true,
              });
              continue;
            }
            if (original.display_label === tool.display_label && original.companion_hint === tool.companion_hint) continue;
            latest = await patchToolCompanionHint({
              capability_id: tool.capability_id,
              display_label: tool.display_label,
              companion_hint: tool.companion_hint,
            });
          }
        }
      }
      if (latest) {
        setSavedHints(cloneHints(latest));
        setDraftHints(cloneHints(latest));
      }
      setResetCapabilities({});
      const messages = [];
      if (personaDirty) messages.push('费佳人设已保存，聊天网关正在重启');
      if (toolDirty || hasResetIntent) messages.push('工具直觉已保存；下一次 resident 启动时生效，未重启聊天网关');
      showToast(messages.join('；'));
    } catch (error) {
      const detail = error instanceof HttpError && error.detail
        ? error.detail
        : error instanceof Error ? error.message : '保存失败';
      showToast(detail);
    } finally {
      setSaving(false);
    }
  }, [dirty, draftHints, draftPersona, hasResetIntent, personaDirty, resetCapabilities, savedHints, saving, showToast, toolDirty]);

  const editTool = useCallback((capabilityId: string, field: 'display_label' | 'companion_hint', value: string) => {
    setDraftHints((current) => current ? updateTool(current, capabilityId, { [field]: value }) : current);
    setResetCapabilities((current) => {
      if (!current[capabilityId]) return current;
      const next = { ...current };
      delete next[capabilityId];
      return next;
    });
  }, []);

  const resetTool = useCallback((tool: ToolCompanionTool) => {
    setDraftHints((current) => current ? updateTool(current, tool.capability_id, {
      display_label: tool.default_display_label,
      companion_hint: tool.default_companion_hint,
    }) : current);
    setResetCapabilities((current) => ({ ...current, [tool.capability_id]: true }));
  }, []);

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

          <div className="profile-subtitle">身份 · 关系 · 工具直觉</div>

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
            <div className="profile-meta-card"><strong className="profile-live-dot">48h</strong><span>工具试用</span></div>
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
            <div className="profile-tool-heading">
              <div>
                <div className="profile-section-title">费佳的工具直觉</div>
                <div className="profile-help-text">48h 试用 · 只编辑工具名称与自然语言说明；真实能力边界由系统固定。</div>
              </div>
            </div>
            {capabilityStateLoadError && (
              <div className="profile-capability-state-error" role="status">
                真实能力状态暂时读不到：{capabilityStateLoadError}。工具直觉仍可编辑，当前不臆测开启状态。
              </div>
            )}
            <div className="profile-tool-groups">
              {toolHintsLoadError ? (
                <div className="profile-help-text">工具直觉暂时读不到：{toolHintsLoadError}。当前不展示伪造的工具数据，请稍后重新读取。</div>
              ) : (draftHints?.groups || []).map((group) => (
                <div className="profile-tool-group" key={group.id}>
                  <div className="profile-tool-group-title">{group.label}</div>
                  {group.tools.map((tool) => {
                    const open = Boolean(openTools[tool.capability_id]);
                    const capabilityState = capabilityStateById.get(tool.capability_id);
                    const statePresentation = presentCapabilityState(capabilityState);
                    const longHint = Array.from(tool.companion_hint).length > 800;
                    const preview = `【${tool.display_label}】\n${tool.companion_hint}\n真实能力边界：${tool.physical_boundary}`;
                    return (
                      <div className="profile-tool-card" key={tool.capability_id}>
                        <button type="button" className="profile-tool-card-toggle" onClick={() => setOpenTools((current) => ({ ...current, [tool.capability_id]: !open }))}>
                          <span>{tool.display_label}</span><span className="profile-tool-status">{tool.status_label}</span><span>{open ? '⌃' : '⌄'}</span>
                        </button>
                        <div className="profile-tool-boundary">{tool.physical_boundary}</div>
                        <div
                          className={`profile-capability-state-badge profile-capability-state-badge--${statePresentation.className}`}
                          data-runtime-state={capabilityState?.runtime_state ?? 'MISSING'}
                          aria-label={`能力状态：${statePresentation.runtimeLabel}，${statePresentation.availabilityLabel}`}
                        >
                          <span className="profile-capability-state-badge__label">真实能力</span>
                          <strong>{statePresentation.runtimeLabel}</strong>
                          <span>{statePresentation.availabilityLabel}</span>
                        </div>
                        {open && (
                          <div className="profile-tool-editor">
                            <label>小猫看到的工具名字<input value={tool.display_label} onChange={(event) => editTool(tool.capability_id, 'display_label', event.target.value)} /></label>
                            <label>工具直觉说明<textarea value={tool.companion_hint} onChange={(event) => editTool(tool.capability_id, 'companion_hint', event.target.value)} /></label>
                            {longHint && <div className="profile-tool-warning">这段说明比较长，会增加下一次 resident 启动的静态上下文；系统不会自动压缩或改写它。</div>}
                            <div className="profile-tool-preview-title">费佳实际会看到 · 逐字预览</div>
                            <pre className="profile-tool-preview">{preview}</pre>
                            <button type="button" className="profile-reset-button" onClick={() => resetTool(tool)}>恢复初始说明</button>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
          </section>

          <div className="profile-persona-note">Persona 保存会写入 persona.md 并重启 frontend-gw。工具直觉保存写入 runtime_config，不重启 frontend-gw；它会在下一次既有 resident static-system 生命周期生效，不会按 turn 广播。系统不会自动截断或改写原文。</div>
        </div>
      )}
      {toast && <div className="profile-toast"><span>{toast}</span></div>}
    </div>
  );
}

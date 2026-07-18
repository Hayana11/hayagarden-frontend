import { useCallback, useEffect, useMemo, useState, type CSSProperties } from 'react';
import { useNavigate } from 'react-router-dom';
import { HttpError } from '../lib/http';
import {
  blankProfile,
  cloneProfile,
  fetchProfile,
  normalizeProfile,
  saveProfile,
  type UserProfile,
} from '../lib/profile';
import './ProfileScreen.css';

const SERIF = "'Noto Serif SC', serif";
const DISPLAY = "'Bodoni Moda', serif";

const LIGHT_VARS: Record<string, string> = {
  '--bg': '#F7F1EE', '--card': '#FFFFFF', '--card2': '#F6EFEC', '--bubble': '#F0DFDB',
  '--ink': '#4A3F3C', '--ink2': '#6B5A55', '--mut': '#8C7B76', '--faint': '#A99590', '--ghost': '#C4B4AF',
  '--line': '#F0E6E2', '--rose': '#B76E79', '--deep': '#9C3B4A', '--rosebg': 'rgba(183,110,121,0.10)',
  '--shadow': 'rgba(183,110,121,0.10)', '--shadow2': 'rgba(183,110,121,0.20)',
  '--ok': '#7A9B6D', '--err': '#C25450', '--gold': '#D9A441',
  '--serif': SERIF, '--display': DISPLAY,
};

const DARK_VARS: Record<string, string> = {
  '--bg': '#211A18', '--card': '#2B2220', '--card2': '#362B28', '--bubble': '#3E2E30',
  '--ink': '#EFE5E1', '--ink2': '#D9C9C3', '--mut': '#B4A19B', '--faint': '#93817C', '--ghost': '#6E5F5A',
  '--line': '#3B302D', '--rose': '#C98A93', '--deep': '#D89AA2', '--rosebg': 'rgba(201,138,147,0.16)',
  '--shadow': 'rgba(0,0,0,0.28)', '--shadow2': 'rgba(0,0,0,0.45)',
  '--ok': '#8FAF80', '--err': '#D97B76', '--gold': '#DFB25E',
  '--serif': SERIF, '--display': DISPLAY,
};

type Page = 'home' | 'preferences';

function profilesEqual(a: UserProfile, b: UserProfile): boolean {
  return JSON.stringify(normalizeProfile(a)) === JSON.stringify(normalizeProfile(b));
}

function PencilIcon() {
  return (
    <svg viewBox="0 0 24 24" width={22} height={22} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />
    </svg>
  );
}

function BackChevron() {
  return (
    <svg viewBox="0 0 24 24" width={22} height={22} fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
      <path d="M15 18l-6-6 6-6" />
    </svg>
  );
}

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

  const [page, setPage] = useState<Page>('home');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState('');
  const [saved, setSaved] = useState<UserProfile>(blankProfile());
  const [draft, setDraft] = useState<UserProfile>(blankProfile());

  const dirty = useMemo(() => !profilesEqual(draft, saved), [draft, saved]);

  const showToast = useCallback((message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(''), 2200);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const profile = await fetchProfile();
      setSaved(profile);
      setDraft(cloneProfile(profile));
    } catch (error) {
      const detail = error instanceof HttpError && error.detail ? error.detail : 'Profile 加载失败';
      showToast(detail);
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    void load();
  }, [load]);

  const patchDraft = useCallback((updater: (prev: UserProfile) => UserProfile) => {
    setDraft((prev) => updater(cloneProfile(prev)));
  }, []);

  const persist = useCallback(async () => {
    if (saving) return;
    setSaving(true);
    try {
      // Preserve any existing savedMemories server-side values without exposing UI.
      const payload = {
        ...draft,
        savedMemories: saved.savedMemories,
      };
      const next = await saveProfile(payload);
      setSaved(next);
      setDraft(cloneProfile(next));
      showToast('Profile 已保存');
    } catch (error) {
      const detail = error instanceof HttpError && error.detail ? error.detail : '保存失败';
      showToast(detail);
    } finally {
      setSaving(false);
    }
  }, [draft, saved.savedMemories, saving, showToast]);

  const clearProfile = useCallback(async () => {
    if (!window.confirm('确定清空 Profile？姓名和偏好都会被清除。')) return;
    const empty = blankProfile();
    setDraft(empty);
    setSaving(true);
    try {
      const next = await saveProfile(empty);
      setSaved(next);
      setDraft(cloneProfile(next));
      showToast('Profile 已清空');
      setPage('home');
    } catch {
      showToast('清空失败');
    } finally {
      setSaving(false);
    }
  }, [showToast]);

  const preferencesNote = useMemo(() => {
    if (!draft.preferences.enabled) return 'Disabled';
    const text = draft.preferences.content.trim();
    if (!text) return 'No custom instructions';
    return text.length > 36 ? `${text.slice(0, 36)}…` : text;
  }, [draft.preferences]);

  const saveButton = (
    <button
      type="button"
      className="profile-save-button"
      aria-label="Save profile"
      disabled={!dirty || saving}
      onClick={() => void persist()}
    >
      ✓
    </button>
  );

  return (
    <div
      className="profile-root dash-fullscreen-page"
      style={{ ...(vars as CSSProperties) }}
    >
      {loading ? (
        <div className="profile-loading">加载 Profile…</div>
      ) : page === 'home' ? (
        <div className="profile-page">
          <header className="profile-header">
            <button type="button" className="profile-round-button" aria-label="Close profile" onClick={() => navigate('/chat')}>×</button>
            <div className="profile-title">Profile</div>
            {saveButton}
          </header>
          <p className="profile-subtitle">保存你的名字和回复偏好。它们会自动加入每次对话请求。</p>

          <section className="profile-section">
            <div className="profile-section-title">Name</div>
            <div className="profile-card">
              <label className="profile-name-row">
                <span className="profile-name-label">Full name</span>
                <input
                  className="profile-name-input"
                  value={draft.fullName}
                  placeholder="你的全名"
                  autoComplete="off"
                  onChange={(e) => patchDraft((p) => { p.fullName = e.target.value; return p; })}
                />
              </label>
              <div className="profile-divider" />
              <label className="profile-name-row">
                <span className="profile-name-label">Nickname</span>
                <input
                  className="profile-name-input"
                  value={draft.nickname}
                  placeholder="助手怎么称呼你"
                  autoComplete="off"
                  onChange={(e) => patchDraft((p) => { p.nickname = e.target.value; return p; })}
                />
              </label>
            </div>
          </section>

          <section className="profile-section">
            <button type="button" className="profile-nav-row" onClick={() => setPage('preferences')}>
              <span className="profile-nav-row-icon"><PencilIcon /></span>
              <span className="profile-nav-row-title">
                Preferences
                <span className="profile-nav-row-note">{preferencesNote}</span>
              </span>
              <span className="profile-nav-chevron">›</span>
            </button>
          </section>

          <div className="profile-footer-actions">
            <button type="button" className="profile-text-button" onClick={() => void clearProfile()}>Clear profile</button>
          </div>
        </div>
      ) : (
        <div className="profile-page">
          <header className="profile-header">
            <button type="button" className="profile-back-button" aria-label="Back" onClick={() => setPage('home')}>
              <BackChevron />
            </button>
            <div className="profile-title">Preferences</div>
            {saveButton}
          </header>
          <div className="profile-preferences-card">
            <div className="profile-card-top">
              <div>
                <div className="profile-section-title">Enable preferences</div>
                <div className="profile-help-text">When enabled, these instructions are included with every request.</div>
              </div>
              <button
                type="button"
                className={`profile-switch${draft.preferences.enabled ? ' on' : ''}`}
                aria-label="Toggle preferences"
                onClick={() => patchDraft((p) => {
                  p.preferences.enabled = !p.preferences.enabled;
                  return p;
                })}
              />
            </div>
            <div className="profile-divider" />
            <textarea
              className="profile-preferences-textarea"
              value={draft.preferences.content}
              placeholder="例如：用更亲近的语气回复；不要把日常聊天写成报告；重要事实要直接说。"
              onChange={(e) => patchDraft((p) => {
                p.preferences.content = e.target.value;
                return p;
              })}
            />
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

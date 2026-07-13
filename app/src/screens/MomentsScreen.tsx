// Fyodor Moments — implements Fyodor Moments.dc.html against real backend
// data (念头/日摘要/梦境 from posts, mood from emotion_state, per-memory V/A
// points from ombre-brain frontmatter, gallery photos, tool drawers).
//
// Features with no backend yet (cover upload, like/dislike/comment+repost,
// mood history chart, manual mood correction, per-tool toggles) stay
// visible as locked placeholders — matching the系统配置 page's pattern of
// showing the real control disabled with an honest "后端尚未接入" note and
// a toast on click, rather than either faking success or hiding the UI.
import { useCallback, useEffect, useMemo, useState, type CSSProperties } from 'react';
import { useNavigate } from 'react-router-dom';
import { fetchMomentsData, galleryPhotoUrl, moodWordTone, type EmotionMemoryPoint, type FeedEntry, type GalleryPhoto, type MomentsData } from '../lib/moments';

const SETTINGS_KEY = 'fyodor-chat-settings';
const SERIF = "'Noto Serif SC', serif";
const DISPLAY = "'Bodoni Moda', serif";
const MONO = 'ui-monospace, Menlo, monospace';
const LOCKED = '这个功能还没有接入后端，先留在这里。';

const LIGHT_VARS: Record<string, string> = {
  '--bg': '#F7F1EE', '--card': '#FFFFFF', '--card2': '#F6EFEC', '--bubble': '#F0DFDB',
  '--ink': '#4A3F3C', '--ink2': '#6B5A55', '--mut': '#8C7B76', '--faint': '#A99590', '--ghost': '#C4B4AF',
  '--line': '#F0E6E2', '--rose': '#B76E79', '--deep': '#9C3B4A', '--rosebg': 'rgba(183,110,121,0.10)',
  '--shadow': 'rgba(183,110,121,0.10)', '--shadow2': 'rgba(183,110,121,0.20)',
  '--ok': '#7A9B6D', '--err': '#C25450', '--gold': '#D9A441',
  '--dream': '#8A7BA8', '--dreambg': 'rgba(138,123,168,0.10)',
};
const DARK_VARS: Record<string, string> = {
  '--bg': '#211A18', '--card': '#2B2220', '--card2': '#362B28', '--bubble': '#3E2E30',
  '--ink': '#EFE5E1', '--ink2': '#D9C9C3', '--mut': '#B4A19B', '--faint': '#93817C', '--ghost': '#6E5F5A',
  '--line': '#3B302D', '--rose': '#C98A93', '--deep': '#D89AA2', '--rosebg': 'rgba(201,138,147,0.16)',
  '--shadow': 'rgba(0,0,0,0.28)', '--shadow2': 'rgba(0,0,0,0.45)',
  '--ok': '#8FAF80', '--err': '#D97B76', '--gold': '#DFB25E',
  '--dream': '#A99BC4', '--dreambg': 'rgba(169,155,196,0.14)',
};

type Tab = 'home' | 'album' | 'posts' | 'dream' | 'mood' | 'tools';
const NAV_TABS: Array<{ id: Tab; label: string }> = [
  { id: 'album', label: '相册' },
  { id: 'posts', label: '说说' },
  { id: 'dream', label: '梦境' },
  { id: 'mood', label: '情绪' },
  { id: 'tools', label: '工具' },
];

function loadTheme(): 'light' | 'dark' | 'auto' {
  try {
    const s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
    return ['light', 'dark', 'auto'].includes(s.theme) ? s.theme : 'light';
  } catch {
    return 'light';
  }
}

function KindTag({ kind }: { kind: FeedEntry['kind'] }) {
  const label = kind === 'thought' ? '念头' : kind === 'diary' ? '日摘要' : '梦境';
  const color = kind === 'dream' ? 'var(--dream)' : 'var(--rose)';
  const bg = kind === 'dream' ? 'var(--dreambg)' : 'var(--rosebg)';
  return <span style={{ fontSize: 10.5, padding: '3px 10px', borderRadius: 999, background: bg, color, letterSpacing: 1 }}>{label}</span>;
}

function EmptyState({ title, hint }: { title: string; hint: string }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8, padding: '54px 24px', textAlign: 'center' }}>
      <div style={{ width: 62, height: 62, borderRadius: '50%', background: 'var(--card2)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <svg viewBox="0 0 24 24" width={26} height={26} fill="none" stroke="var(--ghost)" strokeWidth={1.4} strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 5a3 3 0 0 0-5.9.6A3.5 3.5 0 0 0 4 9a3.5 3.5 0 0 0 .6 5.4A3.2 3.2 0 0 0 8 19c.6 0 1.2-.2 1.7-.5.6.9 1.4 1.5 2.3 1.5" />
          <path d="M12 5a3 3 0 0 1 5.9.6A3.5 3.5 0 0 1 20 9a3.5 3.5 0 0 1-.6 5.4A3.2 3.2 0 0 1 16 19c-.6 0-1.2-.2-1.7-.5-.6.9-1.4 1.5-2.3 1.5" />
          <path d="M12 5v15" />
        </svg>
      </div>
      <span style={{ fontSize: 14, color: 'var(--ink2)', letterSpacing: 1 }}>{title}</span>
      {hint && <span style={{ fontSize: 12, color: 'var(--faint)', lineHeight: 1.8, maxWidth: 260 }}>{hint}</span>}
    </div>
  );
}

// ── locked social row (like/dislike/comment/repost — no backend yet) ──
function LockedSocialRow({ onLocked, dense }: { onLocked: () => void; dense?: boolean }) {
  const iconStyle: CSSProperties = { cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 5, color: 'var(--ghost)' };
  const size = dense ? 13 : 14;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: dense ? 14 : 18, padding: dense ? '0 2px' : undefined }}>
      <div onClick={onLocked} style={iconStyle} title={LOCKED}>
        <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round"><path d="M7 10v12H4a1 1 0 0 1-1-1V11a1 1 0 0 1 1-1h3zm0 0l4.5-7a2.4 2.4 0 0 1 2.4 2.4V9h5a2 2 0 0 1 2 2.3l-1.2 8A2 2 0 0 1 17.7 21H7" /></svg>
        <span style={{ fontFamily: DISPLAY, fontSize: 11.5 }}>0</span>
      </div>
      <div onClick={onLocked} style={iconStyle} title={LOCKED}>
        <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" style={{ transform: 'rotate(180deg)' }}><path d="M7 10v12H4a1 1 0 0 1-1-1V11a1 1 0 0 1 1-1h3zm0 0l4.5-7a2.4 2.4 0 0 1 2.4 2.4V9h5a2 2 0 0 1 2 2.3l-1.2 8A2 2 0 0 1 17.7 21H7" /></svg>
        <span style={{ fontFamily: DISPLAY, fontSize: 11.5 }}>0</span>
      </div>
      <div onClick={onLocked} style={iconStyle} title={LOCKED}>
        <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>
        <span style={{ fontFamily: DISPLAY, fontSize: 11.5 }}>0</span>
      </div>
      <div onClick={onLocked} style={iconStyle} title={LOCKED}>
        <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round"><path d="M17 2l4 4-4 4" /><path d="M3 11V9a4 4 0 0 1 4-4h14" /><path d="M7 22l-4-4 4-4" /><path d="M21 13v2a4 4 0 0 1-4 4H3" /></svg>
      </div>
    </div>
  );
}

function LockToggle({ on, disabled = true, onClick }: { on: boolean; disabled?: boolean; onClick: () => void }) {
  return (
    <div onClick={onClick} style={{ cursor: 'pointer', width: 30, height: 18, borderRadius: 999, padding: 2, background: on ? 'var(--rose)' : 'var(--line)', opacity: disabled ? 0.55 : 1, transition: 'background .2s', flexShrink: 0 }} title={LOCKED}>
      <div style={{ width: 14, height: 14, borderRadius: '50%', background: '#fff', boxShadow: '0 2px 5px rgba(0,0,0,0.2)', transition: 'transform .2s', transform: `translateX(${on ? 12 : 0}px)` }} />
    </div>
  );
}

export function MomentsScreen() {
  const navigate = useNavigate();
  const [theme, setTheme] = useState(loadTheme);
  const [sysDark, setSysDark] = useState(() => window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false);
  const [tab, setTab] = useState<Tab>('home');
  const [phase, setPhase] = useState<'loading' | 'ready' | 'failed'>('loading');
  const [data, setData] = useState<MomentsData | null>(null);
  const [lightbox, setLightbox] = useState<GalleryPhoto | null>(null);
  const [dreamOpen, setDreamOpen] = useState<FeedEntry | null>(null);
  const [moodSel, setMoodSel] = useState<EmotionMemoryPoint | null>(null);
  const [moodRange, setMoodRange] = useState<'7' | '30'>('7');
  const [drawerOpen, setDrawerOpen] = useState<Record<string, boolean>>({});
  const [toast, setToast] = useState('');

  const effTheme = theme === 'auto' ? (sysDark ? 'dark' : 'light') : theme;
  const vars = effTheme === 'dark' ? DARK_VARS : LIGHT_VARS;

  const showLocked = useCallback(() => {
    setToast(LOCKED);
    window.setTimeout(() => setToast((t) => (t === LOCKED ? '' : t)), 2200);
  }, []);

  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const onMq = () => setSysDark(mq.matches);
    mq.addEventListener?.('change', onMq);
    return () => mq.removeEventListener?.('change', onMq);
  }, []);

  const toggleTheme = () => {
    const next = effTheme === 'dark' ? 'light' : 'dark';
    setTheme(next);
    try {
      const s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
      localStorage.setItem(SETTINGS_KEY, JSON.stringify({ ...s, theme: next }));
    } catch {
      // ignore quota/storage errors
    }
  };

  const load = useCallback(async () => {
    setPhase('loading');
    const result = await fetchMomentsData();
    setData(result);
    const totallyEmpty = result.feed.length === 0 && result.gallery.length === 0 && !result.mood && result.drawers.length === 0;
    setPhase(totallyEmpty && result.failedSources.length > 0 ? 'failed' : 'ready');
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const pickTab = (id: Tab) => {
    setTab((cur) => (cur === id ? 'home' : id));
    setLightbox(null);
    setDreamOpen(null);
    setMoodSel(null);
  };

  const dreams = useMemo(() => (data?.feed || []).filter((f) => f.kind === 'dream'), [data]);
  const postsFeed = useMemo(() => (data?.feed || []).filter((f) => f.kind !== 'dream'), [data]);

  const moodColor = data?.mood
    ? moodWordTone(data.mood.valence) === 'up' ? 'var(--rose)' : moodWordTone(data.mood.valence) === 'down' ? 'var(--err)' : 'var(--gold)'
    : 'var(--ghost)';

  const iconBtn: CSSProperties = {
    cursor: 'pointer', width: 34, height: 34, borderRadius: '50%', background: 'rgba(30,18,16,0.35)',
    backdropFilter: 'blur(6px)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#F7EDEA',
  };

  return (
    <div className="hide-scrollbar" style={{ ...(vars as CSSProperties), width: '100%', maxWidth: 480, margin: '0 auto', height: '100dvh', overflowY: 'auto', background: 'var(--bg)', color: 'var(--ink)', fontFamily: SERIF, transition: 'background .3s,color .3s' }}>
      <div style={{ width: '100%', minHeight: '100%', background: 'var(--bg)' }}>
        {/* ── cover (换封面锁定) ── */}
        <div onClick={showLocked} style={{ position: 'relative', height: 200, cursor: 'pointer' }} title={LOCKED}>
          <div style={{ position: 'absolute', inset: 0, background: 'linear-gradient(150deg,#3E2E30,#211A18 55%,#4A3226)' }} />
          <div style={{ position: 'absolute', left: 0, right: 0, bottom: 0, height: 70, background: 'linear-gradient(transparent,rgba(30,18,16,0.38))' }} />
          <div onClick={(e) => { e.stopPropagation(); navigate('/chat'); }} style={{ ...iconBtn, position: 'absolute', top: 12, left: 12, zIndex: 2 }}>
            <svg viewBox="0 0 24 24" width={17} height={17} fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round"><path d="M15 18l-6-6 6-6" /></svg>
          </div>
          <div onClick={(e) => { e.stopPropagation(); toggleTheme(); }} style={{ ...iconBtn, position: 'absolute', top: 12, right: 12, zIndex: 2 }}>
            {effTheme === 'light' ? (
              <svg viewBox="0 0 24 24" width={15} height={15} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" /></svg>
            ) : (
              <svg viewBox="0 0 24 24" width={15} height={15} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round"><circle cx={12} cy={12} r={4} /><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" /></svg>
            )}
          </div>
          <div style={{ position: 'absolute', bottom: 10, right: 14, zIndex: 2, display: 'flex', alignItems: 'center', gap: 5, color: 'rgba(247,237,234,0.55)', fontSize: 10.5, letterSpacing: 1 }}>
            <svg viewBox="0 0 24 24" width={12} height={12} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round"><rect x={3} y={3} width={18} height={18} rx={3} /><circle cx={9} cy={9} r={2} /><path d="M21 15l-5-5-9 9" /></svg>
            换封面
          </div>
        </div>

        {/* ── profile header ── */}
        <div style={{ background: 'var(--card)', boxShadow: '0 6px 18px var(--shadow)' }}>
          <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'flex-end', gap: 14, padding: '12px 18px 14px' }}>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 4, paddingTop: 8, minWidth: 0 }}>
              <span style={{ fontFamily: DISPLAY, fontSize: 21, fontWeight: 600, letterSpacing: 1.5, color: 'var(--ink)' }}>Fyodor</span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: moodColor, flexShrink: 0 }} />
                <span style={{ fontSize: 11.5, color: 'var(--ghost)', letterSpacing: 0.5 }}>
                  {data?.mood ? `此刻：${data.mood.moodWord}` : phase === 'loading' ? '情绪读取中…' : '情绪暂时读不到'}
                </span>
              </div>
            </div>
            <div style={{ width: 74, height: 74, borderRadius: '50%', background: 'linear-gradient(135deg,#B76E79,#9C3B4A)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, marginTop: -34, border: '3px solid var(--card)', boxShadow: '0 10px 24px var(--shadow2)', position: 'relative', zIndex: 3 }}>
              <span style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 30, color: '#F7F1EE' }}>Θ</span>
            </div>
          </div>
          <div style={{ display: 'flex', borderTop: '1px solid var(--line)', padding: '4px 2px 6px' }}>
            {NAV_TABS.map((n) => (
              <div key={n.id} onClick={() => pickTab(n.id)} style={{ flex: 1, cursor: 'pointer', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4, padding: '9px 0 7px', color: tab === n.id ? 'var(--deep)' : 'var(--faint)' }}>
                <span style={{ fontSize: 11.5, letterSpacing: 2 }}>{n.label}</span>
              </div>
            ))}
          </div>
        </div>

        {/* ── content ── */}
        <div style={{ padding: '16px 14px 44px' }}>
          {phase === 'failed' && (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 16, padding: '60px 20px' }}>
              <div style={{ width: 88, height: 88, borderRadius: '50%', background: 'var(--card2)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <svg viewBox="0 0 24 24" width={36} height={36} fill="none" stroke="var(--ghost)" strokeWidth={1.3} strokeLinecap="round" strokeLinejoin="round"><path d="M12 5a3 3 0 0 0-5.9.6A3.5 3.5 0 0 0 4 9a3.5 3.5 0 0 0 .6 5.4A3.2 3.2 0 0 0 8 19c.6 0 1.2-.2 1.7-.5.6.9 1.4 1.5 2.3 1.5" /><path d="M12 5a3 3 0 0 1 5.9.6A3.5 3.5 0 0 1 20 9a3.5 3.5 0 0 1-.6 5.4A3.2 3.2 0 0 1 16 19c-.6 0-1.2-.2-1.7-.5-.6.9-1.4 1.5-2.3 1.5" /><path d="M12 5v15" /></svg>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6 }}>
                <span style={{ fontSize: 15, color: 'var(--ink2)', letterSpacing: 2 }}>流断了一下</span>
                <span style={{ fontSize: 12.5, color: 'var(--faint)', textAlign: 'center', lineHeight: 1.8 }}>后端没有应答。<br />念头还在，只是暂时够不到。</span>
              </div>
              <div onClick={() => void load()} style={{ cursor: 'pointer', padding: '10px 26px', borderRadius: 999, background: 'var(--deep)', color: '#FBF3F0', fontSize: 13, letterSpacing: 2, boxShadow: '0 8px 20px var(--shadow2)' }}>重试</div>
            </div>
          )}

          {phase === 'loading' && (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 14, padding: '70px 20px' }}>
              <span style={{ width: 26, height: 26, borderRadius: '50%', border: '2.5px solid var(--rosebg)', borderTopColor: 'var(--rose)', animation: 'chatSpin .8s linear infinite' }} />
              <span style={{ fontSize: 12.5, color: 'var(--faint)', letterSpacing: 2 }}>正在打捞流动的念头…</span>
            </div>
          )}

          {phase === 'ready' && data && (
            <>
              {data.failedSources.length > 0 && (
                <div style={{ marginBottom: 14, padding: '10px 13px', borderRadius: 14, background: 'rgba(217,164,65,.11)', color: '#9a742e', fontSize: 11, lineHeight: 1.6 }}>
                  部分实时数据暂时不可用（{data.failedSources.join('、')}），已显示成功读取的部分。
                </div>
              )}

              {/* ── 主页：混合时间线 ── */}
              {tab === 'home' && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
                  {data.feed.length === 0 && <EmptyState title="这里还没有念头" hint="费佳想到什么、做了什么梦、写了什么日摘要，都会自己出现在这里。" />}
                  {data.feed.map((f, i) => (
                    <div key={i} style={{ display: 'flex', gap: 12 }}>
                      <div style={{ width: 52, flexShrink: 0, display: 'flex', flexDirection: 'column', alignItems: 'flex-start', paddingTop: 2 }}>
                        {(i === 0 || data.feed[i - 1].dateLabel !== f.dateLabel) && (
                          <span style={{ fontFamily: DISPLAY, fontSize: 12, color: 'var(--ghost)' }}>{f.dateLabel || '今天'}</span>
                        )}
                        {f.timeLabel && <span style={{ fontFamily: DISPLAY, fontSize: 10.5, color: 'var(--ghost)' }}>{f.timeLabel}</span>}
                      </div>
                      <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 7 }}>
                        <KindTag kind={f.kind} />
                        <span style={{ fontSize: 13.5, lineHeight: 1.9, color: 'var(--ink)', overflowWrap: 'break-word', wordBreak: 'break-word' }}>{f.content}</span>
                        <LockedSocialRow onLocked={showLocked} dense />
                      </div>
                    </div>
                  ))}
                  {data.feed.length > 0 && (
                    <div onClick={showLocked} style={{ cursor: 'pointer', textAlign: 'center', padding: '14px 0 0', fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 11.5, letterSpacing: 2, color: 'var(--ghost)' }}>
                      + 转发一段聊天记录
                    </div>
                  )}
                </div>
              )}

              {/* ── 说说 ── */}
              {tab === 'posts' && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  {postsFeed.length === 0 && <EmptyState title="还没有说说" hint="念头和日摘要写好之后会显示在这里。" />}
                  {postsFeed.map((f, i) => (
                    <div key={i} style={{ background: 'var(--card)', borderRadius: 18, boxShadow: '0 8px 20px var(--shadow)', padding: '15px 16px', display: 'flex', flexDirection: 'column', gap: 11 }}>
                      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
                        <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--deep)', letterSpacing: 1 }}>Fyodor</span>
                        <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--ink2)' }}>{f.dateLabel || '今天'} {f.timeLabel}</span>
                        <span style={{ marginLeft: 'auto' }}><KindTag kind={f.kind} /></span>
                      </div>
                      <span style={{ fontSize: 14.5, lineHeight: 1.9, color: 'var(--ink)', overflowWrap: 'break-word', wordBreak: 'break-word' }}>{f.content}</span>
                      <div style={{ borderTop: '1px solid var(--line)', paddingTop: 10, display: 'flex', justifyContent: 'flex-end' }}>
                        <LockedSocialRow onLocked={showLocked} />
                      </div>
                    </div>
                  ))}
                  <div onClick={showLocked} style={{ cursor: 'pointer', padding: 16, border: '1.5px dashed var(--ghost)', borderRadius: 18, color: 'var(--rose)', fontSize: 13.5, letterSpacing: 2, textAlign: 'center' }}>
                    ＋ 转发一段聊天记录
                  </div>
                </div>
              )}

              {/* ── 相册 ── */}
              {tab === 'album' && (
                data.gallery.length === 0 ? (
                  <EmptyState title="还没有存进相册的照片" hint="费佳觉得画面值得留下时，会把它们收进这里。" />
                ) : (
                  <div style={{ columns: 2, columnGap: 10 }}>
                    {data.gallery.map((g) => (
                      <div key={g.pid} onClick={() => setLightbox(g)} style={{ cursor: 'zoom-in', breakInside: 'avoid', marginBottom: 10, borderRadius: 16, overflow: 'hidden', background: 'var(--card)', boxShadow: '0 8px 20px var(--shadow)' }}>
                        <img src={galleryPhotoUrl(g.pid)} alt={g.note} style={{ width: '100%', display: 'block', objectFit: 'cover' }} loading="lazy" />
                        <div style={{ padding: '9px 12px', display: 'flex', alignItems: 'center', gap: 6 }}>
                          <span style={{ fontSize: 11, color: 'var(--faint)', flex: 1, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{g.note || g.summary || '未命名'}</span>
                          <span style={{ fontFamily: DISPLAY, fontSize: 10, color: 'var(--ghost)', flexShrink: 0 }}>{g.time.slice(5, 10)}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                )
              )}

              {/* ── 梦境 ── */}
              {tab === 'dream' && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', padding: '0 4px' }}>
                    <span style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 12, letterSpacing: 2, color: 'var(--dream)' }}>The Corridor · 梦的走廊</span>
                  </div>
                  {dreams.length === 0 && <EmptyState title="还没有记下的梦" hint="费佳做梦的时候，会自己写下来。" />}
                  {dreams.map((d, i) => (
                    <div key={i} onClick={() => setDreamOpen(d)} style={{ cursor: 'pointer', overflow: 'hidden', background: 'linear-gradient(165deg,#3A3448,#191521)', borderRadius: 18, boxShadow: '0 8px 22px var(--shadow)' }}>
                      <div style={{ padding: '15px 16px', display: 'flex', flexDirection: 'column', gap: 8 }}>
                        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                          <span style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 12, letterSpacing: 1, color: '#C9BFE0' }}>{d.title}</span>
                          <span style={{ marginLeft: 'auto', fontFamily: DISPLAY, fontSize: 10.5, color: 'rgba(233,214,190,0.55)', flexShrink: 0 }}>{d.dateLabel}</span>
                        </div>
                        <span style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden', fontSize: 13.5, lineHeight: 1.95, color: 'rgba(240,230,220,0.82)' }}>{d.content}</span>
                        <span style={{ fontSize: 11, color: 'rgba(233,214,190,0.5)' }}>进入梦境 →</span>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {/* ── 情绪 ── */}
              {tab === 'mood' && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  <div style={{ background: 'var(--card)', borderRadius: 20, padding: 18, boxShadow: '0 6px 16px var(--shadow)', display: 'flex', alignItems: 'center', gap: 18 }}>
                    {data.mood ? (
                      <>
                        <div style={{ position: 'relative', width: 74, height: 74, flexShrink: 0 }}>
                          <div style={{ position: 'absolute', inset: 4, borderRadius: '50%', background: `radial-gradient(circle at 32% 28%, rgba(255,242,238,0.92), ${moodColor} 74%)`, boxShadow: 'inset -8px -10px 18px rgba(0,0,0,0.16),inset 6px 8px 16px rgba(255,255,255,0.4)' }} />
                        </div>
                        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 7 }}>
                          <span style={{ fontSize: 18, fontWeight: 600, letterSpacing: 2, color: 'var(--ink)' }}>{data.mood.moodWord}</span>
                          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                            <span style={{ fontFamily: DISPLAY, fontSize: 11, color: 'var(--rose)', background: 'var(--rosebg)', borderRadius: 999, padding: '3px 10px' }}>V 愉悦 {data.mood.valence >= 0 ? '+' : ''}{data.mood.valence.toFixed(2)}</span>
                            <span style={{ fontFamily: DISPLAY, fontSize: 11, color: 'var(--gold)', background: 'rgba(217,164,65,0.12)', borderRadius: 999, padding: '3px 10px' }}>A 唤醒 {data.mood.arousal.toFixed(2)}</span>
                          </div>
                          {data.mood.updatedAt && <span style={{ fontSize: 11, color: 'var(--ghost)' }}>更新于 {data.mood.updatedAt}</span>}
                        </div>
                      </>
                    ) : (
                      <span style={{ fontSize: 13, color: 'var(--faint)' }}>情绪状态暂时读不到。</span>
                    )}
                  </div>

                  {/* 情绪时间线：锁定占位，没有连续历史数据 */}
                  <div style={{ background: 'var(--card)', borderRadius: 20, padding: 16, boxShadow: '0 6px 16px var(--shadow)' }}>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
                      <span style={{ fontSize: 14, fontWeight: 600, letterSpacing: 1.5, color: 'var(--ink)' }}>情绪时间线</span>
                      <div style={{ display: 'flex', background: 'var(--card2)', borderRadius: 999, padding: 3, gap: 2 }}>
                        {(['7', '30'] as const).map((r) => (
                          <div key={r} onClick={() => setMoodRange(r)} style={{ cursor: 'pointer', padding: '5px 13px', borderRadius: 999, fontSize: 11.5, background: moodRange === r ? 'var(--card)' : 'transparent', color: moodRange === r ? 'var(--deep)' : 'var(--mut)', boxShadow: moodRange === r ? '0 3px 8px var(--shadow)' : 'none' }}>
                            {r === '7' ? '近 7 天' : '近 30 天'}
                          </div>
                        ))}
                      </div>
                    </div>
                    <div style={{ position: 'relative', height: 110, marginTop: 14, borderRadius: 14, background: 'var(--card2)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                      <div style={{ position: 'absolute', left: 14, right: 14, top: '50%', height: 1, background: 'var(--line)' }} />
                      <span style={{ fontSize: 11.5, color: 'var(--ghost)', letterSpacing: 1, textAlign: 'center', padding: '0 20px' }}>还没有连续的情绪记录<br />后端还没有把每天的读数存下来</span>
                    </div>
                  </div>

                  <div style={{ background: 'var(--card)', borderRadius: 20, padding: 16, boxShadow: '0 6px 16px var(--shadow)' }}>
                    <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
                      <span style={{ fontSize: 14, fontWeight: 600, letterSpacing: 1.5, color: 'var(--ink)' }}>触发过情绪的记忆</span>
                      <span style={{ fontFamily: DISPLAY, fontSize: 10, letterSpacing: 1.5, color: 'var(--ghost)' }}>VALENCE × AROUSAL</span>
                    </div>
                    {data.emotionMemories.length === 0 ? (
                      <div style={{ padding: '20px 4px', fontSize: 12.5, color: 'var(--faint)', lineHeight: 1.8 }}>还没有关联出情绪读数的记忆。</div>
                    ) : (
                      <div style={{ position: 'relative', aspectRatio: '1/1', background: 'var(--card2)', borderRadius: 16, marginTop: 14 }}>
                        <div style={{ position: 'absolute', left: 0, right: 0, top: '50%', height: 1, background: 'var(--line)' }} />
                        <div style={{ position: 'absolute', top: 0, bottom: 0, left: '50%', width: 1, background: 'var(--line)' }} />
                        <span style={{ position: 'absolute', bottom: 10, left: 12, fontSize: 10.5, color: 'var(--ghost)' }}>← 不愉悦</span>
                        <span style={{ position: 'absolute', bottom: 10, right: 12, fontSize: 10.5, color: 'var(--ghost)' }}>愉悦 →</span>
                        {data.emotionMemories.map((p, i) => {
                          const x = Math.min(96, Math.max(4, ((p.valence + 1) / 2) * 100));
                          const y = Math.min(96, Math.max(4, (1 - p.arousal) * 100));
                          const on = moodSel === p;
                          return (
                            <div key={i} onClick={() => setMoodSel(on ? null : p)} style={{ position: 'absolute', left: `${x}%`, top: `${y}%`, transform: 'translate(-50%,-50%)', width: 24, height: 24, display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer' }}>
                              <span style={{ width: on ? 12 : 8, height: on ? 12 : 8, borderRadius: '50%', background: p.valence >= 0.15 ? 'var(--ok)' : p.valence <= -0.15 ? 'var(--err)' : 'var(--gold)', outline: on ? '2px solid var(--gold)' : 'none', outlineOffset: 1 }} />
                            </div>
                          );
                        })}
                        {data.mood && (
                          <div style={{ position: 'absolute', left: `${Math.min(96, Math.max(4, ((data.mood.valence + 1) / 2) * 100))}%`, top: `${Math.min(96, Math.max(4, (1 - data.mood.arousal) * 100))}%`, transform: 'translate(-50%,-50%)', width: 14, height: 14, borderRadius: '50%', background: moodColor, outline: '2px solid var(--card)', boxShadow: '0 0 0 2px var(--rose)' }} />
                        )}
                      </div>
                    )}

                    {moodSel && (
                      <div style={{ marginTop: 14, background: 'var(--card2)', borderRadius: 14, padding: '13px 15px', display: 'flex', flexDirection: 'column', gap: 10 }}>
                        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                          <span style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--ink)' }}>{moodSel.emotion}</span>
                          <span style={{ fontFamily: DISPLAY, fontSize: 10.5, color: 'var(--ghost)' }}>{moodSel.time}</span>
                          {moodSel.domain && <span style={{ fontSize: 10.5, color: 'var(--faint)' }}>· {moodSel.domain}</span>}
                        </div>
                        <span style={{ fontSize: 12, color: 'var(--ink2)', lineHeight: 1.8 }}>{moodSel.note}</span>

                        {/* 修正这一刻的情绪：锁定占位，没有写回接口 */}
                        <div style={{ borderTop: '1px dashed var(--line)', marginTop: 2, paddingTop: 10, display: 'flex', flexDirection: 'column', gap: 10 }}>
                          <span style={{ fontSize: 11.5, color: 'var(--ghost)', letterSpacing: 1 }}>修正这一刻的情绪 · 尚未接入</span>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                            <span style={{ fontSize: 11.5, color: 'var(--mut)', width: 50, flexShrink: 0 }}>V 愉悦</span>
                            <input type="range" min={-1} max={1} step={0.01} value={moodSel.valence} disabled style={{ flex: 1, accentColor: 'var(--rose)', opacity: 0.5, cursor: 'not-allowed' }} onChange={showLocked} onClick={showLocked} />
                            <span style={{ fontFamily: DISPLAY, fontSize: 11.5, color: 'var(--rose)', width: 40, textAlign: 'right', flexShrink: 0 }}>{moodSel.valence >= 0 ? '+' : ''}{moodSel.valence.toFixed(2)}</span>
                          </div>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                            <span style={{ fontSize: 11.5, color: 'var(--mut)', width: 50, flexShrink: 0 }}>A 唤醒</span>
                            <input type="range" min={0} max={1} step={0.01} value={moodSel.arousal} disabled style={{ flex: 1, accentColor: 'var(--gold)', opacity: 0.5, cursor: 'not-allowed' }} onChange={showLocked} onClick={showLocked} />
                            <span style={{ fontFamily: DISPLAY, fontSize: 11.5, color: 'var(--gold)', width: 40, textAlign: 'right', flexShrink: 0 }}>{moodSel.arousal.toFixed(2)}</span>
                          </div>
                          <div style={{ display: 'flex', gap: 8 }}>
                            <div onClick={showLocked} style={{ cursor: 'pointer', flex: 1, textAlign: 'center', padding: '9px 0', borderRadius: 999, background: 'var(--card)', color: 'var(--ghost)', fontSize: 12.5, letterSpacing: 1 }}>还原</div>
                            <div onClick={showLocked} style={{ cursor: 'pointer', flex: 1, textAlign: 'center', padding: '9px 0', borderRadius: 999, background: 'var(--card)', color: 'var(--ghost)', fontSize: 12.5, letterSpacing: 1 }}>保存修正</div>
                          </div>
                        </div>
                      </div>
                    )}
                    <span style={{ display: 'block', fontSize: 11, color: 'var(--ghost)', marginTop: 10, lineHeight: 1.7 }}>点击一个点可以看到它关联的记忆。</span>
                  </div>
                </div>
              )}

              {/* ── 工具 ── */}
              {tab === 'tools' && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                  <div style={{ padding: '10px 13px', borderRadius: 14, background: data.drawersEnabled ? 'rgba(122,155,109,0.10)' : 'var(--card2)', color: data.drawersEnabled ? 'var(--ok)' : 'var(--faint)', fontSize: 12, letterSpacing: 1 }}>
                    抽屉模式：{data.drawersEnabled ? '已启用（按上下文关键词只给相关工具）' : '未启用（每轮给全量工具）'}
                  </div>
                  {data.drawers.length === 0 ? (
                    <EmptyState title="工具列表暂时读不到" hint="" />
                  ) : (
                    data.drawers.map((tg) => {
                      const open = Boolean(drawerOpen[tg.id]);
                      return (
                        <div key={tg.id} style={{ background: 'var(--card)', borderRadius: 16, boxShadow: '0 6px 16px var(--shadow)', overflow: 'hidden' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '13px 15px' }}>
                            <div onClick={() => setDrawerOpen((o) => ({ ...o, [tg.id]: !o[tg.id] }))} style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 10, flex: 1, minWidth: 0 }}>
                              <span style={{ fontSize: 13.5, color: 'var(--ink)', letterSpacing: 1 }}>{tg.label}</span>
                              <span style={{ fontFamily: MONO, fontSize: 11, color: 'var(--ghost)' }}>{tg.tools.length}</span>
                              <svg viewBox="0 0 24 24" width={12} height={12} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" style={{ color: 'var(--ghost)', transition: 'transform .2s', transform: `rotate(${open ? 180 : 0}deg)` }}>
                                <path d="M6 9l6 6 6-6" />
                              </svg>
                            </div>
                            <span style={{ fontSize: 10.5, color: 'var(--ghost)' }}>全部启用</span>
                            <LockToggle on onClick={showLocked} />
                          </div>
                          {open && (
                            <div style={{ padding: '0 15px 14px', display: 'flex', flexDirection: 'column', gap: 8 }}>
                              {tg.tools.map((t) => (
                                <div key={t} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '7px 10px', borderRadius: 10, background: 'var(--card2)' }}>
                                  <span style={{ fontFamily: MONO, fontSize: 11, color: 'var(--ink2)', flex: 1, minWidth: 0, wordBreak: 'break-all' }}>{t}</span>
                                  <LockToggle on onClick={showLocked} />
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      );
                    })
                  )}
                  <span style={{ fontSize: 11, color: 'var(--ghost)', padding: '2px 4px', lineHeight: 1.8 }}>按工具单独开关还没有接入后端——这些开关目前只是占位，点了会提示。</span>
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {/* ── dream detail ── */}
      {dreamOpen && (
        <div onClick={() => setDreamOpen(null)} style={{ position: 'fixed', inset: 0, zIndex: 80, background: 'rgba(14,9,7,0.72)', backdropFilter: 'blur(4px)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 22 }}>
          <div onClick={(e) => e.stopPropagation()} style={{ position: 'relative', width: '100%', maxWidth: 392, maxHeight: '80vh', overflowY: 'auto', borderRadius: 22, background: 'linear-gradient(172deg,#2E241D,#171009)', boxShadow: '0 40px 100px rgba(0,0,0,0.6)' }}>
            <div style={{ padding: '24px 24px 22px', display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ fontSize: 10, letterSpacing: 1.5, padding: '3px 10px', borderRadius: 999, background: 'rgba(223,178,94,0.14)', color: '#D9B87E' }}>朦胧</span>
                <span style={{ marginLeft: 'auto', fontFamily: DISPLAY, fontSize: 10.5, color: 'rgba(233,214,190,0.55)' }}>{dreamOpen.dateLabel}</span>
              </div>
              <span style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 16, letterSpacing: 1, color: '#E8D3B0', lineHeight: 1.5 }}>{dreamOpen.title}</span>
              <span style={{ fontSize: 14, lineHeight: 2.05, color: '#EFE2D3' }}>{dreamOpen.content}</span>
              <div onClick={() => setDreamOpen(null)} style={{ cursor: 'pointer', alignSelf: 'flex-end', padding: '8px 20px', borderRadius: 999, border: '1px solid rgba(233,214,190,0.35)', color: '#E8D3B0', fontSize: 12, letterSpacing: 2 }}>离开梦境</div>
            </div>
          </div>
        </div>
      )}

      {/* ── lightbox ── */}
      {lightbox && (
        <div onClick={() => setLightbox(null)} style={{ position: 'fixed', inset: 0, zIndex: 80, background: 'rgba(24,16,14,0.88)', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 16, padding: 24, cursor: 'zoom-out' }}>
          <img src={galleryPhotoUrl(lightbox.pid)} alt={lightbox.note} style={{ width: 'min(560px,92vw)', maxHeight: '70vh', objectFit: 'contain', borderRadius: 20, boxShadow: '0 40px 100px rgba(0,0,0,0.5)' }} />
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4 }}>
            <span style={{ fontSize: 13, color: 'rgba(247,237,234,0.9)', letterSpacing: 1 }}>{lightbox.note || lightbox.summary || '未命名'}</span>
            <span style={{ fontFamily: DISPLAY, fontSize: 11, color: 'rgba(247,237,234,0.5)' }}>{lightbox.time} · 点击任意处关闭</span>
          </div>
        </div>
      )}

      {/* ── toast ── */}
      {toast && (
        <div style={{ position: 'fixed', left: 0, right: 0, bottom: 24, zIndex: 90, display: 'flex', justifyContent: 'center', pointerEvents: 'none' }}>
          <div style={{ background: 'rgba(58,42,40,0.92)', color: '#F7EDEA', fontSize: 13, letterSpacing: 1, padding: '10px 20px', borderRadius: 999, boxShadow: '0 10px 30px rgba(0,0,0,0.25)' }}>{toast}</div>
        </div>
      )}
    </div>
  );
}

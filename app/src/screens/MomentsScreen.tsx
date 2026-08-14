// Fyodor Moments — implements Fyodor Moments.dc.html against real backend
// data (念头/日摘要/梦境 from posts, mood from emotion_state, per-memory V/A
// points from ombre-brain frontmatter, gallery photos, Tool Drawer v2).
import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type CSSProperties } from 'react';
import { useNavigate } from 'react-router-dom';
import { smoothPath } from '../lib/format';
import {
  establishMomentsSession,
  fetchEmotionHistory,
  fetchMomentComments,
  fetchDreamsPage,
  fetchMomentsCover,
  fetchMomentsData,
  fetchMomentsFeed,
  fetchMomentsOwnerStatus,
  galleryPhotoUrl,
  moodWordTone,
  postMomentComment,
  reactToMoment,
  updateEmotionMemory,
  uploadMomentsCover,
  type DreamEntry,
  type EmotionHistoryPoint,
  type EmotionMemoryPoint,
  type FeedEntry,
  type FeedSocial,
  type FeedType,
  type GalleryPhoto,
  type MomentComment,
  type MomentsData,
  type MomentsOwnerStatus,
  type MoodState,
} from '../lib/moments';
import { HttpError } from '../lib/http';
import { FONT_CN, FONT_DISPLAY, fontFamilyForText, hasCJK } from '../lib/typography';

const SETTINGS_KEY = 'fyodor-chat-settings';

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
type NavIconKind = 'album' | 'posts' | 'dream' | 'mood' | 'tools';
const NAV_TABS: Array<{ id: Tab; label: string; icon: NavIconKind }> = [
  { id: 'album', label: '相册', icon: 'album' },
  { id: 'posts', label: '说说', icon: 'posts' },
  { id: 'dream', label: '梦境', icon: 'dream' },
  { id: 'mood', label: '情绪', icon: 'mood' },
  { id: 'tools', label: '工具', icon: 'tools' },
];

function NavIcon({ kind }: { kind: NavIconKind }) {
  const common = { viewBox: '0 0 24 24', width: 20, height: 20, fill: 'none', stroke: 'currentColor', strokeWidth: 1.6, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const };
  if (kind === 'album') return <svg {...common}><rect x={3} y={3} width={18} height={18} rx={3} /><circle cx={9} cy={9} r={2} /><path d="M21 15l-5-5-9 9" /></svg>;
  if (kind === 'posts') return <svg {...common}><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>;
  if (kind === 'dream') return <svg {...common}><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" /></svg>;
  if (kind === 'mood') return <svg {...common}><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z" /></svg>;
  return <svg {...common}><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" /></svg>;
}

type MoodIconKind = 'cloud' | 'sun' | 'zap' | 'heart' | 'moon';

function moodIconKind(mood: MoodState): MoodIconKind {
  if (mood.longing >= 0.6) return 'heart';
  if (mood.arousal >= 0.6 && mood.valence <= 0) return 'zap';
  if (mood.valence >= 0.15) return 'sun';
  if (mood.valence <= -0.15) return 'cloud';
  return 'moon';
}

function MoodIcon({ kind }: { kind: MoodIconKind }) {
  const common = { viewBox: '0 0 24 24', width: 11, height: 11, fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const };
  if (kind === 'cloud') return <svg {...common}><path d="M17.5 19a4.5 4.5 0 0 0 0-9h-1.1A7 7 0 1 0 6 18.7" /></svg>;
  if (kind === 'sun') return <svg {...common}><circle cx={12} cy={12} r={4} /><path d="M12 2v2" /><path d="M12 20v2" /><path d="M4.93 4.93l1.41 1.41" /><path d="M17.66 17.66l1.41 1.41" /><path d="M2 12h2" /><path d="M20 12h2" /></svg>;
  if (kind === 'zap') return <svg {...common}><path d="M13 2L3 14h7l-1 8 10-12h-7l1-8z" /></svg>;
  if (kind === 'heart') return <svg {...common}><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z" /></svg>;
  return <svg {...common}><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" /></svg>;
}

// Dream cards: scene background from tone/V/A, refined by imagery keywords.
// Priority (highest wins):
//   1. Concrete imagery keywords (blood/snow/rain/…) — visual anchor in the text
//   2. Backend tone (vivid/warm/anxious/heavy/drifting)
//   3. Russell V/A quadrant when tone is missing
//   4. neutral (雾)
interface DreamScene {
  label: string;
  base: string;
  glow: string;
}

const DREAM_SCENES: Record<string, DreamScene> = {
  rain: {
    label: '雨夜图书馆', base: 'linear-gradient(160deg,#54657A,#2C3642)',
    glow: 'radial-gradient(120% 90% at 78% 8%, rgba(170,200,225,0.5), transparent 60%), repeating-linear-gradient(100deg, rgba(255,255,255,0.09) 0 1.5px, transparent 1.5px 10px)',
  },
  sea: {
    label: '夜海棋局', base: 'linear-gradient(165deg,#26415C,#0F1B28)',
    glow: 'radial-gradient(80% 55% at 70% 12%, rgba(205,225,255,0.55), transparent 55%), repeating-linear-gradient(0deg, rgba(255,255,255,0.07) 0 2px, transparent 2px 14px)',
  },
  night: {
    label: '夜的走廊', base: 'linear-gradient(165deg,#3A3448,#191521)',
    glow: 'radial-gradient(60% 45% at 82% 15%, rgba(230,225,255,0.5), transparent 55%), repeating-linear-gradient(90deg, rgba(255,255,255,0.05) 0 3px, transparent 3px 26px)',
  },
  warm: {
    label: '有灯的屋子', base: 'linear-gradient(160deg,#7A5A42,#3C2820)',
    glow: 'radial-gradient(70% 60% at 30% 20%, rgba(255,205,130,0.55), transparent 60%)',
  },
  uneasy: {
    label: '追逐', base: 'linear-gradient(150deg,#5A3E4C,#221721)',
    glow: 'radial-gradient(50% 40% at 60% 30%, rgba(255,150,140,0.3), transparent 60%), repeating-linear-gradient(65deg, rgba(255,255,255,0.08) 0 1px, transparent 1px 7px)',
  },
  neutral: {
    label: '雾', base: 'linear-gradient(165deg,#6A625C,#2E2A27)',
    glow: 'radial-gradient(85% 65% at 50% 35%, rgba(235,230,225,0.4), transparent 65%)',
  },
  snow: {
    label: '雪原', base: 'linear-gradient(165deg,#28323E,#131B22)',
    glow: 'radial-gradient(85% 65% at 25% 15%, rgba(163,201,224,0.42), transparent 60%), repeating-linear-gradient(112deg, rgba(255,255,255,0.07) 0 1.5px, transparent 1.5px 13px)',
  },
  abyss: {
    label: '深渊', base: 'linear-gradient(165deg,#2E1620,#150A10)',
    glow: 'radial-gradient(70% 55% at 30% 20%, rgba(201,88,104,0.4), transparent 60%), repeating-linear-gradient(38deg, rgba(255,255,255,0.05) 0 1px, transparent 1px 9px)',
  },
};

const DREAM_SCENE_KEYWORDS: Array<[keyof typeof DREAM_SCENES, string[]]> = [
  ['abyss', ['血', '深渊', '枪', '刀', '尸']],
  ['snow', ['雪', '西伯利亚', '冰', '寒冬', '霜']],
  ['rain', ['雨', '图书馆', '书页', '书脊']],
  ['sea', ['海', '潮', '浪', '棋']],
  ['night', ['走廊', '门', '月光', '月亮']],
  ['warm', ['灯', '暖', '阳光', '向日葵', '沙发', '下午']],
  ['uneasy', ['追', '跑', '找不到', '未接', '电话', '慌']],
];

const TONE_SCENE: Record<string, keyof typeof DREAM_SCENES> = {
  warm: 'warm',
  anxious: 'uneasy',
  heavy: 'night',
  vivid: 'sea',
  drifting: 'neutral',
};

function classifyDreamScene(dream: Pick<DreamEntry, 'title' | 'content' | 'tone' | 'valence' | 'arousal'>): DreamScene {
  const text = `${dream.title} ${dream.content}`;

  // 1. Imagery keywords override tone — keeps visual texture tied to nouns in the dream.
  for (const [key, words] of DREAM_SCENE_KEYWORDS) {
    if (words.some((w) => text.includes(w))) return DREAM_SCENES[key];
  }

  // 2. Backend tone from dream_generator / dream_pool.
  const toneKey = TONE_SCENE[dream.tone];
  if (toneKey) return DREAM_SCENES[toneKey];

  // 3. Bipolar V/A fallback (API normalizes to [-1,1] / [0,1]).
  const v = (dream.valence + 1) / 2;
  const a = dream.arousal;
  if (v >= 0.6 && a >= 0.6) return DREAM_SCENES.sea;
  if (v >= 0.6 && a < 0.6) return DREAM_SCENES.warm;
  if (v < 0.4 && a >= 0.6) return DREAM_SCENES.uneasy;
  if (v < 0.4 && a < 0.6) return DREAM_SCENES.night;

  // 4. Default
  return DREAM_SCENES.neutral;
}

/** Decode entity spacers and keep paragraph breaks for dream detail rendering. */
function formatDreamBody(raw: string): string {
  return (raw || '')
    .replace(/&nbsp;/gi, '\n')
    .replace(/&#160;/g, '\n')
    .replace(/\u00a0/g, '\n')
    .replace(/\r\n/g, '\n')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function loadTheme(): 'light' | 'dark' | 'auto' {
  try {
    const s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
    return ['light', 'dark', 'auto'].includes(s.theme) ? s.theme : 'light';
  } catch {
    return 'light';
  }
}

function kindTagStyle(entry: FeedEntry): { label: string; color: string; bg: string } {
  if (entry.brewing) return { label: '酝酿中', color: 'var(--gold)', bg: 'rgba(217,164,65,0.12)' };
  if (entry.kind === 'repost') return { label: '转发', color: 'var(--deep)', bg: 'var(--rosebg)' };
  if (entry.kind === 'gallery') return { label: entry.tags[0] || '相册', color: 'var(--rose)', bg: 'var(--rosebg)' };
  return { label: entry.tags[0] || '念头', color: 'var(--rose)', bg: 'var(--rosebg)' };
}

function KindTag({ entry }: { entry: FeedEntry }) {
  const { label, color, bg } = kindTagStyle(entry);
  return <span style={{ fontSize: 10.5, padding: '3px 10px', borderRadius: 999, background: bg, color, letterSpacing: 1 }}>{label}</span>;
}

function RepostCard({ entry }: { entry: FeedEntry }) {
  const messages = entry.repost?.messages || [];
  const sourceLabel = entry.repost?.sourceLabel || '';
  return (
    <div className="vstack vstack-10">
      {entry.content ? (
        <span style={{ fontSize: 13.5, lineHeight: 1.9, color: 'var(--ink)', overflowWrap: 'break-word', wordBreak: 'break-word' }}>{entry.content}</span>
      ) : null}
      <div className="vstack vstack-10" style={{ borderRadius: 16, background: 'var(--card2)', padding: '12px 13px' }}>
        {sourceLabel ? (
          <span style={{ fontSize: 10.5, color: 'var(--ghost)', letterSpacing: 1 }}>
            <span style={{ fontFamily: fontFamilyForText(sourceLabel) }}>{sourceLabel}</span>
            <span style={{ fontFamily: FONT_CN }}> · 聊天记录</span>
          </span>
        ) : (
          <span style={{ fontFamily: FONT_CN, fontSize: 10.5, color: 'var(--ghost)', letterSpacing: 1 }}>聊天记录</span>
        )}
        {messages.map((m) => (
          <div key={m.messageId} className="vstack vstack-4" style={{ display: 'flex', flexDirection: 'column', alignItems: m.role === 'haya' ? 'flex-start' : 'flex-end' }}>
            <span style={{ fontSize: 10, color: 'var(--faint)', letterSpacing: 1 }}>{m.role === 'haya' ? 'Haya' : 'Fyodor'}</span>
            <div style={{
              maxWidth: '92%',
              padding: '8px 11px',
              borderRadius: m.role === 'haya' ? '14px 14px 14px 4px' : '14px 14px 4px 14px',
              background: m.role === 'haya' ? 'var(--bubble)' : 'var(--rosebg)',
              color: 'var(--ink)',
              fontSize: 13,
              lineHeight: 1.75,
              overflowWrap: 'break-word',
              wordBreak: 'break-word',
            }}>
              {m.text || (m.attachment ? `[${m.attachment.kind === 'image' ? '图片' : '附件'}]` : '')}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function GalleryFeedMedia({ entry, onOpen }: { entry: FeedEntry; onOpen?: () => void }) {
  const media = entry.media[0];
  if (!media) {
    return entry.content ? (
      <span style={{ fontSize: 13.5, lineHeight: 1.9, color: 'var(--ink)', overflowWrap: 'break-word', wordBreak: 'break-word' }}>{entry.content}</span>
    ) : null;
  }
  return (
    <div className="vstack vstack-10">
      {entry.content ? (
        <span style={{ fontSize: 13.5, lineHeight: 1.9, color: 'var(--ink)', overflowWrap: 'break-word', wordBreak: 'break-word' }}>{entry.content}</span>
      ) : null}
      <div
        onClick={onOpen}
        style={{ cursor: onOpen ? 'zoom-in' : 'default', borderRadius: 16, overflow: 'hidden', background: 'var(--card2)', boxShadow: '0 6px 16px var(--shadow)' }}
      >
        <img src={media.url} alt={media.note || '相册'} style={{ width: '100%', display: 'block', objectFit: 'cover', maxHeight: 320 }} loading="lazy" />
      </div>
    </div>
  );
}

function FeedEntryBody({ entry, onGalleryOpen }: { entry: FeedEntry; onGalleryOpen?: () => void }) {
  if (entry.kind === 'repost') return <RepostCard entry={entry} />;
  if (entry.kind === 'gallery') return <GalleryFeedMedia entry={entry} onOpen={onGalleryOpen} />;
  return (
    <span style={{ fontSize: 13.5, lineHeight: 1.9, color: 'var(--ink)', overflowWrap: 'break-word', wordBreak: 'break-word' }}>{entry.content}</span>
  );
}

function DateRail({ dateLabel, visible }: { dateLabel: string; visible: boolean }) {
  if (!visible) return <div style={{ width: 54, flexShrink: 0 }} />;

  if (!dateLabel) {
    return (
      <div style={{ width: 54, flexShrink: 0, display: 'flex', flexDirection: 'column', alignItems: 'flex-start', paddingTop: 1 }}>
        <span style={{ fontFamily: FONT_DISPLAY, fontSize: 20, fontWeight: 600, lineHeight: 1, color: 'var(--ghost)', letterSpacing: 0.5 }}>—</span>
      </div>
    );
  }

  let primary: string;
  let secondary: string | null = null;
  let primarySize = 34;

  let primaryFont = FONT_DISPLAY;
  let secondaryFont = FONT_CN;

  if (dateLabel === '今天') {
    primary = '今天';
    primarySize = 24;
    primaryFont = FONT_CN;
  } else if (dateLabel === '昨天') {
    primary = '昨天';
    primarySize = 22;
    primaryFont = FONT_CN;
  } else {
    const match = dateLabel.match(/^(\d+)月(\d+)日$/);
    if (match) {
      primary = match[2];
      secondary = `${match[1]}月`;
      primaryFont = FONT_DISPLAY;
      secondaryFont = FONT_CN;
    } else {
      primary = dateLabel;
      primarySize = 24;
      primaryFont = fontFamilyForText(dateLabel);
    }
  }

  return (
    <div style={{ width: 54, flexShrink: 0, display: 'flex', flexDirection: 'column', alignItems: 'flex-start', paddingTop: 1 }}>
      <span style={{ fontFamily: primaryFont, fontSize: primarySize, fontWeight: 600, lineHeight: 1, color: 'var(--mut)', letterSpacing: 0.5 }}>
        {primary}
      </span>
      {secondary && (
        <span style={{ fontFamily: secondaryFont, fontSize: 11, color: 'var(--ghost)', marginTop: 4, letterSpacing: 0.5 }}>
          {secondary}
        </span>
      )}
    </div>
  );
}

function EmptyState({ title, hint, onRetry }: { title: string; hint: string; onRetry?: () => void }) {
  return (
    <div className="vstack vstack-8" style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '54px 24px', textAlign: 'center' }}>
      <div style={{ width: 62, height: 62, borderRadius: '50%', background: 'var(--card2)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <svg viewBox="0 0 24 24" width={26} height={26} fill="none" stroke="var(--ghost)" strokeWidth={1.4} strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 5a3 3 0 0 0-5.9.6A3.5 3.5 0 0 0 4 9a3.5 3.5 0 0 0 .6 5.4A3.2 3.2 0 0 0 8 19c.6 0 1.2-.2 1.7-.5.6.9 1.4 1.5 2.3 1.5" />
          <path d="M12 5a3 3 0 0 1 5.9.6A3.5 3.5 0 0 1 20 9a3.5 3.5 0 0 1-.6 5.4A3.2 3.2 0 0 1 16 19c-.6 0-1.2-.2-1.7-.5-.6.9-1.4 1.5-2.3 1.5" />
          <path d="M12 5v15" />
        </svg>
      </div>
      <span style={{ fontSize: 14, color: 'var(--ink2)', letterSpacing: 1 }}>{title}</span>
      {hint && <span style={{ fontSize: 12, color: 'var(--faint)', lineHeight: 1.8, maxWidth: 260 }}>{hint}</span>}
      {onRetry && (
        <div onClick={onRetry} style={{ cursor: 'pointer', marginTop: 8, padding: '10px 26px', borderRadius: 999, background: 'var(--deep)', color: '#FBF3F0', fontSize: 13, letterSpacing: 2, boxShadow: '0 8px 20px var(--shadow2)' }}>重试</div>
      )}
    </div>
  );
}

// ── social row (like / dislike / comment) ──
function SocialRow({
  itemKey,
  social,
  dense,
  onSocialChange,
  onToast,
  onAuthRequired,
}: {
  itemKey: string;
  social: FeedSocial;
  dense?: boolean;
  onSocialChange: (itemKey: string, social: FeedSocial) => void;
  onToast: (msg: string) => void;
  onAuthRequired: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [commentsOpen, setCommentsOpen] = useState(false);
  const [comments, setComments] = useState<MomentComment[]>([]);
  const [commentDraft, setCommentDraft] = useState('');
  const [commentsLoading, setCommentsLoading] = useState(false);
  const [commentBusy, setCommentBusy] = useState(false);

  const iconStyle = (active: boolean): CSSProperties => ({
    cursor: busy ? 'default' : 'pointer',
    color: active ? 'var(--rose)' : 'var(--ghost)',
    opacity: busy ? 0.6 : 1,
  });
  const size = dense ? 13 : 14;

  const handleReact = async (reaction: 'like' | 'dislike') => {
    if (busy) return;
    setBusy(true);
    try {
      const updated = await reactToMoment(itemKey, reaction);
      onSocialChange(itemKey, updated);
    } catch (err) {
      if (err instanceof HttpError && err.status === 401) {
        onAuthRequired();
        onToast('需要主人授权后才能互动');
      } else {
        onToast('操作失败，请稍后再试');
      }
    } finally {
      setBusy(false);
    }
  };

  const loadComments = async () => {
    setCommentsLoading(true);
    try {
      const items = await fetchMomentComments(itemKey);
      setComments(items);
    } catch {
      onToast('评论读取失败');
    } finally {
      setCommentsLoading(false);
    }
  };

  const toggleComments = async () => {
    if (commentsOpen) {
      setCommentsOpen(false);
      return;
    }
    setCommentsOpen(true);
    if (comments.length === 0) {
      await loadComments();
    }
  };

  const submitComment = async () => {
    const text = commentDraft.trim();
    if (!text || commentBusy) return;
    setCommentBusy(true);
    try {
      const { comment, social: updated } = await postMomentComment(itemKey, text);
      setCommentDraft('');
      setComments((prev) => [comment, ...prev]);
      onSocialChange(itemKey, updated);
    } catch (err) {
      if (err instanceof HttpError && err.status === 401) {
        onAuthRequired();
        onToast('需要主人授权后才能评论');
      } else {
        onToast('评论发送失败');
      }
    } finally {
      setCommentBusy(false);
    }
  };

  return (
    <div style={{ flex: 1, minWidth: 0 }}>
      <div className={dense ? "hstack hstack-14" : "hstack hstack-18"} style={{ display: 'flex', alignItems: 'center', padding: dense ? '0 2px' : undefined }}>
        <div onClick={() => void handleReact('like')} className="hstack hstack-5" style={iconStyle(social.myReaction === 'like')} title="赞">
          <svg viewBox="0 0 24 24" width={size} height={size} fill={social.myReaction === 'like' ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round"><path d="M7 10v12H4a1 1 0 0 1-1-1V11a1 1 0 0 1 1-1h3zm0 0l4.5-7a2.4 2.4 0 0 1 2.4 2.4V9h5a2 2 0 0 1 2 2.3l-1.2 8A2 2 0 0 1 17.7 21H7" /></svg>
          <span style={{ fontFamily: FONT_DISPLAY, fontSize: 11.5 }}>{social.likes}</span>
        </div>
        <div onClick={() => void handleReact('dislike')} className="hstack hstack-5" style={iconStyle(social.myReaction === 'dislike')} title="踩">
          <svg viewBox="0 0 24 24" width={size} height={size} fill={social.myReaction === 'dislike' ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" style={{ transform: 'rotate(180deg)' }}><path d="M7 10v12H4a1 1 0 0 1-1-1V11a1 1 0 0 1 1-1h3zm0 0l4.5-7a2.4 2.4 0 0 1 2.4 2.4V9h5a2 2 0 0 1 2 2.3l-1.2 8A2 2 0 0 1 17.7 21H7" /></svg>
          <span style={{ fontFamily: FONT_DISPLAY, fontSize: 11.5 }}>{social.dislikes}</span>
        </div>
        <div onClick={() => void toggleComments()} className="hstack hstack-5" style={iconStyle(commentsOpen)} title="评论">
          <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>
          <span style={{ fontFamily: FONT_DISPLAY, fontSize: 11.5 }}>{social.comments}</span>
        </div>
      </div>
      {commentsOpen && (
        <div className="vstack vstack-8" style={{ marginTop: dense ? 8 : 10, padding: '10px 12px', borderRadius: 12, background: 'var(--card2)' }}>
          <div className="hstack hstack-8" style={{ display: 'flex' }}>
            <input
              value={commentDraft}
              onChange={(e) => setCommentDraft(e.target.value)}
              placeholder="写一句评论…"
              maxLength={500}
              style={{ flex: 1, border: '1px solid var(--line)', borderRadius: 999, padding: '8px 12px', background: 'var(--card)', color: 'var(--ink)', fontSize: 12.5, outline: 'none' }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  void submitComment();
                }
              }}
            />
            <div
              onClick={() => void submitComment()}
              style={{ cursor: commentBusy ? 'default' : 'pointer', padding: '8px 14px', borderRadius: 999, background: 'var(--deep)', color: '#FBF3F0', fontSize: 12, letterSpacing: 1, opacity: commentBusy ? 0.6 : 1, flexShrink: 0 }}
            >
              发送
            </div>
          </div>
          {commentsLoading ? (
            <span style={{ fontSize: 11.5, color: 'var(--ghost)' }}>读取评论中…</span>
          ) : comments.length === 0 ? (
            <span style={{ fontSize: 11.5, color: 'var(--ghost)' }}>还没有评论</span>
          ) : (
            comments.map((c) => (
              <div key={c.id} style={{ fontSize: 12.5, lineHeight: 1.55, color: 'var(--ink2)' }}>
                <span style={{ color: 'var(--deep)', marginRight: 6 }}>{c.author === 'haya' ? '哈娅' : c.author}</span>
                {c.content}
              </div>
            ))
          )}
        </div>
      )}
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
  const [feedItems, setFeedItems] = useState<FeedEntry[]>([]);
  const [feedCursor, setFeedCursor] = useState<string | null>(null);
  const [feedHasMore, setFeedHasMore] = useState(false);
  const [feedLoadingMore, setFeedLoadingMore] = useState(false);
  const [feedFailed, setFeedFailed] = useState(false);
  const [feedReloading, setFeedReloading] = useState(false);
  const [postsItems, setPostsItems] = useState<FeedEntry[]>([]);
  const [postsCursor, setPostsCursor] = useState<string | null>(null);
  const [postsHasMore, setPostsHasMore] = useState(false);
  const [postsLoadingMore, setPostsLoadingMore] = useState(false);
  const [postsFailed, setPostsFailed] = useState(false);
  const [postsReloading, setPostsReloading] = useState(false);
  const [postsLoaded, setPostsLoaded] = useState(false);
  const [dreams, setDreams] = useState<DreamEntry[]>([]);
  const [dreamsNextBefore, setDreamsNextBefore] = useState<number | null>(null);
  const [dreamsHasMore, setDreamsHasMore] = useState(false);
  const [dreamsLoadingMore, setDreamsLoadingMore] = useState(false);
  const dreamSentinelRef = useRef<HTMLDivElement | null>(null);
  const [lightbox, setLightbox] = useState<GalleryPhoto | null>(null);
  const [dreamOpen, setDreamOpen] = useState<DreamEntry | null>(null);
  const [hoveredDream, setHoveredDream] = useState<number | null>(null);
  const [moodSel, setMoodSel] = useState<EmotionMemoryPoint | null>(null);
  const [moodDraft, setMoodDraft] = useState<{ valence: number; arousal: number } | null>(null);
  const [moodRange, setMoodRange] = useState<'7' | '30'>('7');
  const [emotionHistory, setEmotionHistory] = useState<EmotionHistoryPoint[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState(false);
  const [historyReloadKey, setHistoryReloadKey] = useState(0);
  const [savingMood, setSavingMood] = useState(false);
  const [toast, setToast] = useState('');
  const [coverUrl, setCoverUrl] = useState<string | null>(null);
  const [coverUploading, setCoverUploading] = useState(false);
  const coverInputRef = useRef<HTMLInputElement>(null);
  const [ownerStatus, setOwnerStatus] = useState<MomentsOwnerStatus>({ configured: false, authenticated: false });
  const [unlockOpen, setUnlockOpen] = useState(false);
  const [unlockDraft, setUnlockDraft] = useState('');
  const [unlockBusy, setUnlockBusy] = useState(false);

  const effTheme = theme === 'auto' ? (sysDark ? 'dark' : 'light') : theme;
  const vars = effTheme === 'dark' ? DARK_VARS : LIGHT_VARS;

  const flashToast = useCallback((msg: string) => {
    setToast(msg);
    window.setTimeout(() => setToast((t) => (t === msg ? '' : t)), 2200);
  }, []);

  const patchFeedSocial = useCallback((itemKey: string, social: FeedSocial) => {
    setFeedItems((items) => items.map((f) => (f.itemKey === itemKey ? { ...f, social } : f)));
    setPostsItems((items) => items.map((f) => (f.itemKey === itemKey ? { ...f, social } : f)));
  }, []);

  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const onMq = () => setSysDark(mq.matches);
    mq.addEventListener?.('change', onMq);
    return () => mq.removeEventListener?.('change', onMq);
  }, []);

  useEffect(() => {
    void fetchMomentsCover().then(setCoverUrl);
  }, []);

  useEffect(() => {
    void fetchMomentsOwnerStatus().then(setOwnerStatus).catch(() => {});
  }, []);

  const requestOwnerUnlock = useCallback(() => {
    setUnlockOpen(true);
  }, []);

  const ensureOwner = useCallback(() => {
    if (ownerStatus.authenticated) return true;
    requestOwnerUnlock();
    flashToast('需要主人授权');
    return false;
  }, [flashToast, ownerStatus.authenticated, requestOwnerUnlock]);

  const submitOwnerUnlock = useCallback(async () => {
    const token = unlockDraft.trim();
    if (!token || unlockBusy) return;
    setUnlockBusy(true);
    try {
      await establishMomentsSession(token);
      setOwnerStatus({ configured: true, authenticated: true });
      setUnlockOpen(false);
      setUnlockDraft('');
      flashToast('已授权，可以互动了');
    } catch {
      flashToast('授权失败，请检查口令');
    } finally {
      setUnlockBusy(false);
    }
  }, [flashToast, unlockBusy, unlockDraft]);

  const pickCover = useCallback(() => {
    if (!ownerStatus.authenticated) {
      requestOwnerUnlock();
      flashToast('更换封面需要主人授权');
      return;
    }
    if (!coverUploading) coverInputRef.current?.click();
  }, [coverUploading, flashToast, ownerStatus.authenticated, requestOwnerUnlock]);

  const onCoverFile = useCallback(async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    setCoverUploading(true);
    const url = await uploadMomentsCover(file);
    setCoverUploading(false);
    if (url) {
      setCoverUrl(`${url}?t=${Date.now()}`);
      flashToast('封面已更新');
    } else {
      flashToast('封面上传失败');
    }
  }, [flashToast]);

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

  const mergeFeedPage = useCallback((current: FeedEntry[], pageItems: FeedEntry[]) => {
    const seen = new Set(current.map((item) => item.itemKey));
    const merged = [...current];
    for (const item of pageItems) {
      if (!seen.has(item.itemKey)) merged.push(item);
    }
    return merged;
  }, []);

  const load = useCallback(async () => {
    setPhase('loading');
    setFeedFailed(false);
    setPostsLoaded(false);
    const [auxResult, feedResult] = await Promise.allSettled([
      fetchMomentsData(),
      fetchMomentsFeed(undefined, 20, 'all'),
    ]);

    if (auxResult.status === 'fulfilled') {
      setData(auxResult.value);
      setDreams(auxResult.value.dreams);
      setDreamsHasMore(auxResult.value.dreamsHasMore);
      setDreamsNextBefore(auxResult.value.dreamsNextBefore);
    } else {
      setData(null);
      setDreams([]);
      setDreamsHasMore(false);
      setDreamsNextBefore(null);
    }

    if (feedResult.status === 'fulfilled') {
      setFeedItems(feedResult.value.items);
      setFeedCursor(feedResult.value.nextCursor);
      setFeedHasMore(feedResult.value.hasMore);
      setFeedFailed(false);
    } else {
      setFeedItems([]);
      setFeedCursor(null);
      setFeedHasMore(false);
      setFeedFailed(true);
    }

    setPostsItems([]);
    setPostsCursor(null);
    setPostsHasMore(false);
    setPostsFailed(false);

    const aux = auxResult.status === 'fulfilled' ? auxResult.value : null;
    const feedOk = feedResult.status === 'fulfilled';
    const totallyEmpty = !feedOk
      && (!aux || (aux.dreams.length === 0 && aux.gallery.length === 0 && !aux.mood && aux.toolGroups.length === 0));
    setPhase(totallyEmpty ? 'failed' : 'ready');
  }, []);

  const reloadFeed = useCallback(async (feedType: FeedType = 'all') => {
    if (feedType === 'posts') {
      setPostsReloading(true);
      setPostsFailed(false);
      try {
        const page = await fetchMomentsFeed(undefined, 20, 'posts');
        setPostsItems(page.items);
        setPostsCursor(page.nextCursor);
        setPostsHasMore(page.hasMore);
        setPostsLoaded(true);
      } catch {
        setPostsItems([]);
        setPostsCursor(null);
        setPostsHasMore(false);
        setPostsFailed(true);
      } finally {
        setPostsReloading(false);
      }
      return;
    }

    setFeedReloading(true);
    setFeedFailed(false);
    try {
      const page = await fetchMomentsFeed(undefined, 20, 'all');
      setFeedItems(page.items);
      setFeedCursor(page.nextCursor);
      setFeedHasMore(page.hasMore);
    } catch {
      setFeedItems([]);
      setFeedCursor(null);
      setFeedHasMore(false);
      setFeedFailed(true);
    } finally {
      setFeedReloading(false);
    }
  }, []);

  const loadPostsFeed = useCallback(async () => {
    if (postsLoaded || postsReloading) return;
    await reloadFeed('posts');
  }, [postsLoaded, postsReloading, reloadFeed]);

  const loadMore = useCallback(async (feedType: FeedType = 'all') => {
    if (feedType === 'posts') {
      if (!postsHasMore || postsLoadingMore || !postsCursor) return;
      setPostsLoadingMore(true);
      try {
        const page = await fetchMomentsFeed(postsCursor, 20, 'posts');
        setPostsItems((current) => mergeFeedPage(current, page.items));
        setPostsCursor(page.nextCursor);
        setPostsHasMore(page.hasMore);
      } catch {
        setToast('加载更多失败了，稍后再试。');
        window.setTimeout(() => setToast(''), 2200);
      } finally {
        setPostsLoadingMore(false);
      }
      return;
    }

    if (!feedHasMore || feedLoadingMore || !feedCursor) return;
    setFeedLoadingMore(true);
    try {
      const page = await fetchMomentsFeed(feedCursor, 20, 'all');
      setFeedItems((current) => mergeFeedPage(current, page.items));
      setFeedCursor(page.nextCursor);
      setFeedHasMore(page.hasMore);
    } catch {
      setToast('加载更多失败了，稍后再试。');
      window.setTimeout(() => setToast(''), 2200);
    } finally {
      setFeedLoadingMore(false);
    }
  }, [feedCursor, feedHasMore, feedLoadingMore, mergeFeedPage, postsCursor, postsHasMore, postsLoadingMore]);

  const loadMoreDreams = useCallback(async () => {
    if (!dreamsHasMore || dreamsLoadingMore || dreamsNextBefore == null) return;
    setDreamsLoadingMore(true);
    try {
      const page = await fetchDreamsPage(dreamsNextBefore, 20);
      setDreams((current) => {
        const seen = new Set(current.map((d) => d.id));
        const merged = [...current];
        for (const item of page.items) {
          if (!seen.has(item.id)) merged.push(item);
        }
        return merged;
      });
      setDreamsNextBefore(page.nextBefore);
      setDreamsHasMore(page.hasMore);
    } catch {
      setToast('梦境加载更多失败了，稍后再试。');
      window.setTimeout(() => setToast(''), 2200);
    } finally {
      setDreamsLoadingMore(false);
    }
  }, [dreamsHasMore, dreamsLoadingMore, dreamsNextBefore]);

  useEffect(() => {
    if (tab !== 'dream' || !dreamsHasMore) return;
    const node = dreamSentinelRef.current;
    if (!node) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          void loadMoreDreams();
        }
      },
      { root: null, rootMargin: '160px', threshold: 0 },
    );
    io.observe(node);
    return () => io.disconnect();
  }, [tab, dreamsHasMore, loadMoreDreams, dreams.length]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (tab === 'posts' && phase === 'ready') {
      void loadPostsFeed();
    }
  }, [tab, phase, loadPostsFeed]);

  const pickTab = (id: Tab) => {
    setTab((cur) => (cur === id ? 'home' : id));
    setLightbox(null);
    setDreamOpen(null);
    setMoodSel(null);
  };

  const openGalleryFromFeed = useCallback((entry: FeedEntry) => {
    const media = entry.media[0];
    if (!media) return;
    setLightbox({
      pid: media.pid,
      note: media.note,
      width: media.width,
      height: media.height,
      favorite: false,
      time: entry.createdAt || '',
      summary: media.note,
      emotion: '',
      keywords: entry.tags,
    });
  }, []);

  useEffect(() => {
    if (moodSel) {
      setMoodDraft({ valence: moodSel.valence, arousal: moodSel.arousal });
    } else {
      setMoodDraft(null);
    }
  }, [moodSel]);

  useEffect(() => {
    if (tab !== 'mood') return;
    let cancelled = false;
    setHistoryLoading(true);
    setHistoryError(false);
    void fetchEmotionHistory(moodRange === '7' ? 7 : 30)
      .then((series) => {
        if (!cancelled) {
          setEmotionHistory(series);
          setHistoryError(false);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setEmotionHistory([]);
          setHistoryError(true);
        }
      })
      .finally(() => {
        if (!cancelled) setHistoryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tab, moodRange, historyReloadKey]);

  const historyChart = useMemo(() => {
    if (emotionHistory.length < 2) return null;
    const vals = emotionHistory.map((point) => (point.valence + 1) / 2);
    const w = 280;
    const h = 86;
    const line = smoothPath(vals, w, h);
    const last = emotionHistory[emotionHistory.length - 1];
    return { line, w, h, last };
  }, [emotionHistory]);

  const saveMoodCorrection = useCallback(async () => {
    if (!moodSel?.path || !moodDraft || savingMood) return;
    if (!ensureOwner()) return;
    setSavingMood(true);
    try {
      const updated = await updateEmotionMemory(moodSel.path, moodDraft.valence, moodDraft.arousal);
      setData((prev) => (
        prev
          ? {
              ...prev,
              emotionMemories: prev.emotionMemories.map((point) => (
                point.path === updated.path ? updated : point
              )),
            }
          : prev
      ));
      setMoodSel(updated);
      flashToast('情绪修正已保存');
    } catch (err) {
      if (err instanceof HttpError && err.status === 401) requestOwnerUnlock();
      flashToast('保存失败');
    } finally {
      setSavingMood(false);
    }
  }, [ensureOwner, flashToast, moodDraft, moodSel, requestOwnerUnlock, savingMood]);

  const resetMoodDraft = useCallback(() => {
    if (!moodSel) return;
    setMoodDraft({ valence: moodSel.valence, arousal: moodSel.arousal });
  }, [moodSel]);

  const moodColor = data?.mood
    ? moodWordTone(data.mood.valence) === 'up' ? 'var(--rose)' : moodWordTone(data.mood.valence) === 'down' ? 'var(--err)' : 'var(--gold)'
    : 'var(--ghost)';

  const iconBtn: CSSProperties = {
    cursor: 'pointer', width: 34, height: 34, borderRadius: '50%', background: 'rgba(30,18,16,0.35)',
    backdropFilter: 'blur(6px)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#F7EDEA',
  };

  return (
    <div className="hide-scrollbar dash-fullscreen-page dash-scroll-page" style={{ ...(vars as CSSProperties), background: 'var(--bg)', color: 'var(--ink)', fontFamily: FONT_CN }}>
      <div style={{ width: '100%', background: 'var(--bg)' }}>
        {/* ── cover ── */}
        <input ref={coverInputRef} type="file" accept="image/*" style={{ display: 'none' }} onChange={(e) => void onCoverFile(e)} />
        <div onClick={pickCover} style={{ position: 'relative', height: 248, cursor: coverUploading ? 'wait' : 'pointer' }} title="点击更换封面">
          {coverUrl && (
            <img src={coverUrl} alt="" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover' }} />
          )}
          <div style={{ position: 'absolute', inset: 0, background: coverUrl ? 'linear-gradient(150deg,rgba(30,18,16,0.12),rgba(30,18,16,0.55))' : 'linear-gradient(150deg,#3E2E30,#211A18 55%,#4A3226)' }} />
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
          <div className="hstack hstack-5" style={{ position: 'absolute', bottom: 10, right: 14, zIndex: 2, color: 'rgba(247,237,234,0.55)', fontSize: 10.5, letterSpacing: 1 }}>
            <svg viewBox="0 0 24 24" width={12} height={12} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round"><rect x={3} y={3} width={18} height={18} rx={3} /><circle cx={9} cy={9} r={2} /><path d="M21 15l-5-5-9 9" /></svg>
            {coverUploading ? '上传中…' : '换封面'}
          </div>
        </div>

        {/* ── profile header ── */}
        <div style={{ background: 'var(--card)', boxShadow: '0 6px 18px var(--shadow)' }}>
          <div className="hstack hstack-14" style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'flex-end', padding: '12px 18px 14px' }}>
            <div className="vstack vstack-4" style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', paddingTop: 8, minWidth: 0 }}>
              <span style={{ fontFamily: FONT_DISPLAY, fontSize: 21, fontWeight: 600, letterSpacing: 1.5, color: 'var(--ink)' }}>Fyodor</span>
              <div className="hstack hstack-6" style={{ display: 'flex', alignItems: 'center' }}>
                <span style={{ width: 18, height: 18, borderRadius: '50%', background: data?.mood ? 'var(--rosebg)' : 'var(--card2)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: data?.mood ? 'var(--rose)' : 'var(--ghost)', flexShrink: 0, animation: data?.mood ? 'chatBreathe 3s ease-in-out infinite' : 'none' }}>
                  {data?.mood && <MoodIcon kind={moodIconKind(data.mood)} />}
                </span>
                <span style={{ fontSize: 11.5, color: 'var(--ghost)', letterSpacing: 0.5 }}>
                  {data?.mood ? `此刻：${data.mood.moodWord}` : phase === 'loading' ? '情绪读取中…' : '情绪暂时读不到'}
                </span>
              </div>
            </div>
            <div style={{ width: 74, height: 74, borderRadius: '50%', background: 'linear-gradient(135deg,#B76E79,#9C3B4A)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, marginTop: -34, border: '3px solid var(--card)', boxShadow: '0 10px 24px var(--shadow2)', position: 'relative', zIndex: 3 }}>
              <span style={{ fontFamily: FONT_DISPLAY, fontStyle: 'italic', fontSize: 30, color: '#F7F1EE' }}>Θ</span>
            </div>
          </div>
          <div style={{ display: 'flex', borderTop: '1px solid var(--line)', padding: '4px 2px 6px' }}>
            {NAV_TABS.map((n) => (
              <div key={n.id} onClick={() => pickTab(n.id)} className="vstack vstack-4" style={{ flex: 1, cursor: 'pointer', alignItems: 'center', padding: '9px 0 7px', color: tab === n.id ? 'var(--deep)' : 'var(--faint)', transition: 'color .2s' }}>
                <NavIcon kind={n.icon} />
                <span style={{ fontSize: 11.5, letterSpacing: 2 }}>{n.label}</span>
              </div>
            ))}
          </div>
        </div>

        {/* ── content ── */}
        <div style={{ padding: '16px 14px 44px' }}>
          {phase === 'failed' && (
            <div className="vstack vstack-16" style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '60px 20px' }}>
              <div style={{ width: 88, height: 88, borderRadius: '50%', background: 'var(--card2)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <svg viewBox="0 0 24 24" width={36} height={36} fill="none" stroke="var(--ghost)" strokeWidth={1.3} strokeLinecap="round" strokeLinejoin="round"><path d="M12 5a3 3 0 0 0-5.9.6A3.5 3.5 0 0 0 4 9a3.5 3.5 0 0 0 .6 5.4A3.2 3.2 0 0 0 8 19c.6 0 1.2-.2 1.7-.5.6.9 1.4 1.5 2.3 1.5" /><path d="M12 5a3 3 0 0 1 5.9.6A3.5 3.5 0 0 1 20 9a3.5 3.5 0 0 1-.6 5.4A3.2 3.2 0 0 1 16 19c-.6 0-1.2-.2-1.7-.5-.6.9-1.4 1.5-2.3 1.5" /><path d="M12 5v15" /></svg>
              </div>
              <div className="vstack vstack-6" style={{ alignItems: 'center' }}>
                <span style={{ fontSize: 15, color: 'var(--ink2)', letterSpacing: 2 }}>流断了一下</span>
                <span style={{ fontSize: 12.5, color: 'var(--faint)', textAlign: 'center', lineHeight: 1.8 }}>后端没有应答。<br />念头还在，只是暂时够不到。</span>
              </div>
              <div onClick={() => void load()} style={{ cursor: 'pointer', padding: '10px 26px', borderRadius: 999, background: 'var(--deep)', color: '#FBF3F0', fontSize: 13, letterSpacing: 2, boxShadow: '0 8px 20px var(--shadow2)' }}>重试</div>
            </div>
          )}

          {phase === 'loading' && (
            <div className="vstack vstack-14" style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '70px 20px' }}>
              <span style={{ width: 26, height: 26, borderRadius: '50%', border: '2.5px solid var(--rosebg)', borderTopColor: 'var(--rose)', animation: 'chatSpin .8s linear infinite' }} />
              <span style={{ fontSize: 12.5, color: 'var(--faint)', letterSpacing: 2 }}>正在打捞流动的念头…</span>
            </div>
          )}

          {phase === 'ready' && (
            <>
              {data && data.failedSources.length > 0 && (
                <div style={{ marginBottom: 14, padding: '10px 13px', borderRadius: 14, background: 'rgba(217,164,65,.11)', color: '#9a742e', fontSize: 11, lineHeight: 1.6 }}>
                  部分实时数据暂时不可用（{data.failedSources.join('、')}），已显示成功读取的部分。
                </div>
              )}

              {/* ── 主页：混合时间线 ── */}
              {tab === 'home' && (
                <div className="vstack vstack-22">
                  {feedFailed && feedItems.length === 0 && (
                    <EmptyState
                      title="时间线暂时读不到"
                      hint="后端没有应答。可以只重试念头流，其他内容不受影响。"
                      onRetry={feedReloading ? undefined : () => void reloadFeed()}
                    />
                  )}
                  {feedReloading && feedItems.length === 0 && (
                    <div className="vstack vstack-10" style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '24px 20px' }}>
                      <span style={{ width: 22, height: 22, borderRadius: '50%', border: '2.5px solid var(--rosebg)', borderTopColor: 'var(--rose)', animation: 'chatSpin .8s linear infinite' }} />
                      <span style={{ fontSize: 12, color: 'var(--faint)', letterSpacing: 2 }}>正在重新打捞念头…</span>
                    </div>
                  )}
                  {!feedFailed && !feedReloading && feedItems.length === 0 && (
                    <EmptyState title="这里还没有念头" hint="费佳想到什么，会自己出现在这里。" />
                  )}
                  {feedItems.map((f, i) => (
                    <div key={f.itemKey} className="hstack hstack-12" style={{ display: 'flex' }}>
                      <DateRail dateLabel={f.dateLabel} visible={i === 0 || f.dateLabel !== feedItems[i - 1].dateLabel} />
                      <div className="vstack vstack-8" style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
                        <FeedEntryBody entry={f} onGalleryOpen={f.kind === 'gallery' ? () => openGalleryFromFeed(f) : undefined} />
                        <div><KindTag entry={f} /></div>
                        <div className="hstack hstack-10" style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between' }}>
                          <SocialRow itemKey={f.itemKey} social={f.social} dense onSocialChange={patchFeedSocial} onToast={flashToast} onAuthRequired={requestOwnerUnlock} />
                          {f.timeLabel ? (
                            <span style={{ fontFamily: fontFamilyForText(f.timeLabel), fontSize: 11, color: 'var(--ghost)', flexShrink: 0, letterSpacing: 0.5 }}>
                              {f.timeLabel}
                            </span>
                          ) : null}
                        </div>
                      </div>
                    </div>
                  ))}
                  {feedItems.length > 0 && feedHasMore && (
                    <div
                      onClick={() => void loadMore()}
                      style={{ textAlign: 'center', padding: '10px 0 4px', fontFamily: FONT_CN, fontSize: 12, letterSpacing: 2, color: feedLoadingMore ? 'var(--ghost)' : 'var(--rose)', cursor: feedLoadingMore ? 'default' : 'pointer' }}
                    >
                      {feedLoadingMore ? '正在继续打捞…' : '加载更多'}
                    </div>
                  )}
                  {feedItems.length > 0 && !feedHasMore && (
                    <div style={{ textAlign: 'center', padding: '18px 0 4px', fontFamily: FONT_CN, fontSize: 11.5, letterSpacing: 2, color: 'var(--ghost)' }}>
                      — 流到这里就停了 —
                    </div>
                  )}
                </div>
              )}

              {/* ── 说说 ── */}
              {tab === 'posts' && (
                <div className="vstack vstack-12">
                  {postsFailed && postsItems.length === 0 && (
                    <EmptyState
                      title="说说暂时读不到"
                      hint="可以只重试说说流。"
                      onRetry={postsReloading ? undefined : () => void reloadFeed('posts')}
                    />
                  )}
                  {postsReloading && postsItems.length === 0 && (
                    <div className="vstack vstack-10" style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '24px 20px' }}>
                      <span style={{ width: 22, height: 22, borderRadius: '50%', border: '2.5px solid var(--rosebg)', borderTopColor: 'var(--rose)', animation: 'chatSpin .8s linear infinite' }} />
                      <span style={{ fontSize: 12, color: 'var(--faint)', letterSpacing: 2 }}>正在读取说说…</span>
                    </div>
                  )}
                  {!postsFailed && !postsReloading && postsItems.length === 0 && postsLoaded && (
                    <EmptyState title="还没有说说" hint="念头或聊天记录被收藏后，会显示在这里。" />
                  )}
                  {postsItems.map((f) => (
                    <div key={f.itemKey} className="vstack vstack-11" style={{ background: 'var(--card)', borderRadius: 18, boxShadow: '0 8px 20px var(--shadow)', padding: '15px 16px' }}>
                      <div className="hstack hstack-10" style={{ display: 'flex', alignItems: 'baseline' }}>
                        <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--deep)', letterSpacing: 1 }}>Fyodor</span>
                        {f.dateLabel ? (
                          <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--ink2)' }}>{f.dateLabel}</span>
                        ) : null}
                      </div>
                      <FeedEntryBody entry={f} />
                      <div><KindTag entry={f} /></div>
                      <div className="hstack hstack-10" style={{ borderTop: '1px solid var(--line)', paddingTop: 10, display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between' }}>
                        <SocialRow itemKey={f.itemKey} social={f.social} onSocialChange={patchFeedSocial} onToast={flashToast} onAuthRequired={requestOwnerUnlock} />
                        {f.timeLabel ? (
                          <span style={{ fontFamily: fontFamilyForText(f.timeLabel || ''), fontSize: 11.5, color: 'var(--ghost)', flexShrink: 0, letterSpacing: 0.5 }}>
                            {f.timeLabel}
                          </span>
                        ) : null}
                      </div>
                    </div>
                  ))}
                  {postsItems.length > 0 && postsHasMore && (
                    <div
                      onClick={() => void loadMore('posts')}
                      style={{ textAlign: 'center', padding: '10px 0 4px', fontFamily: FONT_CN, fontSize: 12, letterSpacing: 2, color: postsLoadingMore ? 'var(--ghost)' : 'var(--rose)', cursor: postsLoadingMore ? 'default' : 'pointer' }}
                    >
                      {postsLoadingMore ? '正在继续读取…' : '加载更多'}
                    </div>
                  )}
                </div>
              )}

              {/* ── 相册 ── */}
              {tab === 'album' && (
                (data?.gallery || []).length === 0 ? (
                  <EmptyState title="还没有存进相册的照片" hint="费佳觉得画面值得留下时，会把它们收进这里。" />
                ) : (
                  <div style={{ columns: 2, columnGap: 10 }}>
                    {(data?.gallery || []).map((g) => (
                      <div key={g.pid} onClick={() => setLightbox(g)} style={{ cursor: 'zoom-in', breakInside: 'avoid', marginBottom: 10, borderRadius: 16, overflow: 'hidden', background: 'var(--card)', boxShadow: '0 8px 20px var(--shadow)' }}>
                        <img src={galleryPhotoUrl(g.pid)} alt={g.note} style={{ width: '100%', display: 'block', objectFit: 'cover' }} loading="lazy" />
                        <div className="hstack hstack-6" style={{ padding: '9px 12px' }}>
                          <span style={{ fontSize: 11, color: 'var(--faint)', flex: 1, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{g.note || g.summary || '未命名'}</span>
                          <span style={{ fontFamily: FONT_DISPLAY, fontSize: 10, color: 'var(--ghost)', flexShrink: 0 }}>{g.time.slice(5, 10)}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                )
              )}

              {/* ── 梦境 ── */}
              {tab === 'dream' && (
                <div className="vstack vstack-12">
                  <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', padding: '0 4px' }}>
                    <span style={{ fontSize: 12, letterSpacing: 2, color: 'var(--dream)' }}>
                      <span style={{ fontFamily: FONT_DISPLAY, fontStyle: 'italic' }}>The Corridor</span>
                      <span style={{ fontFamily: FONT_CN, fontStyle: 'normal' }}> · 梦的走廊</span>
                    </span>
                    <span style={{ fontSize: 10.5, color: 'var(--ghost)' }}>按住看光 · 点击进入</span>
                  </div>
                  {dreams.length === 0 && <EmptyState title="还没有记下的梦" hint="费佳做梦的时候，会自己写下来。" />}
                  {dreams.map((d) => {
                    const scene = classifyDreamScene(d);
                    const glowing = hoveredDream === d.id;
                    const baseOp = glowing ? 0.68 : 0.06;
                    const glowOp = glowing ? 0.5 : 0;
                    const titleC = glowing ? '#EFDFC8' : 'var(--dream)';
                    const ghostC = glowing ? 'rgba(240,230,220,0.65)' : 'var(--ghost)';
                    const textC = glowing ? 'rgba(247,240,232,0.93)' : 'var(--ink2)';
                    const chipBg = glowing ? 'rgba(255,255,255,0.14)' : 'var(--dreambg)';
                    const chipC = glowing ? 'rgba(245,235,225,0.9)' : 'var(--dream)';
                    return (
                      <div
                        key={d.id}
                        onClick={() => setDreamOpen(d)}
                        onMouseEnter={() => setHoveredDream(d.id)}
                        onMouseLeave={() => setHoveredDream((cur) => (cur === d.id ? null : cur))}
                        onTouchStart={() => setHoveredDream(d.id)}
                        onTouchEnd={() => setHoveredDream((cur) => (cur === d.id ? null : cur))}
                        style={{ position: 'relative', cursor: 'pointer', overflow: 'hidden', background: 'var(--card)', borderRadius: 18, boxShadow: '0 8px 22px var(--shadow)' }}
                      >
                        <div style={{ position: 'absolute', inset: 0, background: scene.base, opacity: baseOp, transition: 'opacity 1.3s ease', pointerEvents: 'none' }} />
                        <div style={{ position: 'absolute', inset: '-18%', background: scene.glow, opacity: glowOp, transition: 'opacity 1.6s ease', animation: 'chatFogDrift 9.5s ease-in-out infinite alternate', pointerEvents: 'none' }} />
                        <div className="vstack vstack-8" style={{ position: 'relative', padding: '15px 16px', display: 'flex', flexDirection: 'column' }}>
                          <div className="hstack hstack-8" style={{ display: 'flex', alignItems: 'baseline' }}>
                            <span style={{ fontFamily: fontFamilyForText(d.title), fontStyle: hasCJK(d.title) ? 'normal' : 'italic', fontSize: 12, letterSpacing: 1, color: titleC, transition: 'color .9s ease' }}>{d.title}</span>
                            <span style={{ marginLeft: 'auto', fontFamily: fontFamilyForText(d.dateLabel), fontSize: 10.5, color: ghostC, flexShrink: 0, transition: 'color .9s ease' }}>{d.dateLabel}</span>
                          </div>
                          <span style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden', fontSize: 13.5, lineHeight: 1.95, color: textC, transition: 'color .9s ease', maskImage: 'linear-gradient(180deg,#000 52%,rgba(0,0,0,0.12) 100%)', WebkitMaskImage: 'linear-gradient(180deg,#000 52%,rgba(0,0,0,0.12) 100%)' }}>{d.content}</span>
                          <div className="flex-wrap-gap-8" style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap' }}>
                            <span style={{ fontSize: 10, letterSpacing: 1.5, padding: '3px 10px', borderRadius: 999, background: chipBg, color: chipC, transition: 'all .9s ease' }}>{scene.label}</span>
                            <span style={{ fontSize: 10, letterSpacing: 1.5, padding: '3px 10px', borderRadius: 999, background: glowing ? 'rgba(255,255,255,0.1)' : 'var(--card2)', color: glowing ? 'rgba(245,235,225,0.88)' : 'var(--mut)', transition: 'all .9s ease' }}>{d.emotion}</span>
                            <span style={{ marginLeft: 'auto', fontSize: 11, color: ghostC, transition: 'color .9s ease', letterSpacing: 1 }}>进入梦境 →</span>
                          </div>
                        </div>
                      </div>
                    );
                  })}
                  {dreamsHasMore && (
                    <div
                      ref={dreamSentinelRef}
                      onClick={() => void loadMoreDreams()}
                      style={{ textAlign: 'center', padding: '10px 0 4px', fontFamily: FONT_CN, fontSize: 12, letterSpacing: 2, color: dreamsLoadingMore ? 'var(--ghost)' : 'var(--dream)', cursor: dreamsLoadingMore ? 'default' : 'pointer' }}
                    >
                      {dreamsLoadingMore ? '梦还在继续涌上来…' : '下滑加载更多 · 或点这里'}
                    </div>
                  )}
                </div>
              )}

              {/* ── 情绪 ── */}
              {tab === 'mood' && (
                <div className="vstack vstack-12">
                  <div className="hstack hstack-18" style={{ background: 'var(--card)', borderRadius: 20, padding: 18, boxShadow: '0 6px 16px var(--shadow)' }}>
                    {data?.mood ? (
                      <>
                        <div style={{ position: 'relative', width: 74, height: 74, flexShrink: 0 }}>
                          <div style={{ position: 'absolute', top: 4, right: 4, bottom: 4, left: 4, borderRadius: '50%', background: `radial-gradient(circle at 32% 28%, rgba(255,242,238,0.92), ${moodColor} 74%)`, boxShadow: 'inset -8px -10px 18px rgba(0,0,0,0.16),inset 6px 8px 16px rgba(255,255,255,0.4)' }} />
                        </div>
                        <div className="vstack vstack-7" style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
                          <span style={{ fontSize: 18, fontWeight: 600, letterSpacing: 2, color: 'var(--ink)' }}>{data.mood.moodWord}</span>
                          <div className="flex-wrap-gap-8" style={{ display: 'flex', flexWrap: 'wrap' }}>
                            <span style={{ fontSize: 11, color: 'var(--rose)', background: 'var(--rosebg)', borderRadius: 999, padding: '3px 10px' }}>
                              <span style={{ fontFamily: FONT_DISPLAY }}>V </span>
                              <span style={{ fontFamily: FONT_CN }}>愉悦 </span>
                              <span style={{ fontFamily: FONT_DISPLAY }}>{data.mood.valence >= 0 ? '+' : ''}{data.mood.valence.toFixed(2)}</span>
                            </span>
                            <span style={{ fontSize: 11, color: 'var(--gold)', background: 'rgba(217,164,65,0.12)', borderRadius: 999, padding: '3px 10px' }}>
                              <span style={{ fontFamily: FONT_DISPLAY }}>A </span>
                              <span style={{ fontFamily: FONT_CN }}>唤醒 </span>
                              <span style={{ fontFamily: FONT_DISPLAY }}>{data.mood.arousal.toFixed(2)}</span>
                            </span>
                          </div>
                          {data.mood.updatedAt && <span style={{ fontSize: 11, color: 'var(--ghost)' }}>更新于 {data.mood.updatedAt}</span>}
                        </div>
                      </>
                    ) : (
                      <span style={{ fontSize: 13, color: 'var(--faint)' }}>情绪状态暂时读不到。</span>
                    )}
                  </div>

                  {/* 情绪时间线 */}
                  <div style={{ background: 'var(--card)', borderRadius: 20, padding: 16, boxShadow: '0 6px 16px var(--shadow)' }}>
                    <div className="hstack hstack-10" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                      <span style={{ fontSize: 14, fontWeight: 600, letterSpacing: 1.5, color: 'var(--ink)' }}>情绪时间线</span>
                      <div className="hstack hstack-2" style={{ display: 'flex', background: 'var(--card2)', borderRadius: 999, padding: 3 }}>
                        {(['7', '30'] as const).map((r) => (
                          <div key={r} onClick={() => setMoodRange(r)} style={{ cursor: 'pointer', padding: '5px 13px', borderRadius: 999, fontSize: 11.5, background: moodRange === r ? 'var(--card)' : 'transparent', color: moodRange === r ? 'var(--deep)' : 'var(--mut)', boxShadow: moodRange === r ? '0 3px 8px var(--shadow)' : 'none' }}>
                            {r === '7' ? '近 7 天' : '近 30 天'}
                          </div>
                        ))}
                      </div>
                    </div>
                    <div style={{ position: 'relative', height: 110, marginTop: 14, borderRadius: 14, background: 'var(--card2)', overflow: 'hidden' }}>
                      <div style={{ position: 'absolute', left: 14, right: 14, top: '50%', height: 1, background: 'var(--line)' }} />
                      {historyLoading ? (
                        <div className="c78-fill-absolute" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 11.5, color: 'var(--ghost)' }}>读取中…</div>
                      ) : historyError ? (
                        <div className="vstack vstack-10 c78-fill-absolute" style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '0 20px' }}>
                          <span style={{ fontSize: 11.5, color: 'var(--err)', letterSpacing: 1, textAlign: 'center' }}>情绪历史读取失败</span>
                          <div onClick={() => setHistoryReloadKey((key) => key + 1)} style={{ cursor: 'pointer', padding: '6px 14px', borderRadius: 999, background: 'var(--card)', color: 'var(--mut)', fontSize: 12, letterSpacing: 1 }}>重试</div>
                        </div>
                      ) : historyChart ? (
                        <svg viewBox={`0 0 ${historyChart.w} ${historyChart.h}`} preserveAspectRatio="none" style={{ position: 'absolute', top: 12, right: 14, bottom: 12, left: 14, width: 'calc(100% - 28px)', height: 'calc(100% - 24px)' }}>
                          <path d={historyChart.line} fill="none" stroke="var(--rose)" strokeWidth={2.2} strokeLinecap="round" />
                        </svg>
                      ) : (
                        <div className="c78-fill-absolute" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 11.5, color: 'var(--ghost)', letterSpacing: 1, textAlign: 'center', padding: '0 20px' }}>
                          还没有连续的情绪记录<br />对话评分后会自动积累
                        </div>
                      )}
                      {historyChart?.last && (
                        <span style={{ position: 'absolute', right: 12, bottom: 8, fontSize: 10, color: 'var(--ghost)' }}>
                          <span style={{ fontFamily: FONT_CN }}>最近 </span>
                          <span style={{ fontFamily: FONT_DISPLAY }}>{historyChart.last.day}</span>
                          <span style={{ fontFamily: FONT_CN }}> · </span>
                          <span style={{ fontFamily: FONT_DISPLAY }}>V {historyChart.last.valence >= 0 ? '+' : ''}{historyChart.last.valence.toFixed(2)}</span>
                        </span>
                      )}
                    </div>
                  </div>

                  <div style={{ background: 'var(--card)', borderRadius: 20, padding: 16, boxShadow: '0 6px 16px var(--shadow)' }}>
                    <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
                      <span style={{ fontSize: 14, fontWeight: 600, letterSpacing: 1.5, color: 'var(--ink)' }}>触发过情绪的记忆</span>
                      <span style={{ fontFamily: FONT_DISPLAY, fontSize: 10, letterSpacing: 1.5, color: 'var(--ghost)' }}>VALENCE × AROUSAL</span>
                    </div>
                    {(data?.emotionMemories || []).length === 0 ? (
                      <div style={{ padding: '20px 4px', fontSize: 12.5, color: 'var(--faint)', lineHeight: 1.8 }}>还没有关联出情绪读数的记忆。</div>
                    ) : (
                      <div className="moments-va-plot">
                        <div style={{ position: 'absolute', left: 0, right: 0, top: '50%', height: 1, background: 'var(--line)' }} />
                        <div style={{ position: 'absolute', top: 0, bottom: 0, left: '50%', width: 1, background: 'var(--line)' }} />
                        <span style={{ position: 'absolute', bottom: 10, left: 12, fontSize: 10.5, color: 'var(--ghost)' }}>← 不愉悦</span>
                        <span style={{ position: 'absolute', bottom: 10, right: 12, fontSize: 10.5, color: 'var(--ghost)' }}>愉悦 →</span>
                        {(data?.emotionMemories || []).map((p, i) => {
                          const x = Math.min(96, Math.max(4, ((p.valence + 1) / 2) * 100));
                          const y = Math.min(96, Math.max(4, (1 - p.arousal) * 100));
                          const on = moodSel === p;
                          return (
                            <div key={i} onClick={() => setMoodSel(on ? null : p)} style={{ position: 'absolute', left: `${x}%`, top: `${y}%`, transform: 'translate(-50%,-50%)', width: 24, height: 24, display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer' }}>
                              <span style={{ width: on ? 12 : 8, height: on ? 12 : 8, borderRadius: '50%', background: p.valence >= 0.15 ? 'var(--ok)' : p.valence <= -0.15 ? 'var(--err)' : 'var(--gold)', outline: on ? '2px solid var(--gold)' : 'none', outlineOffset: 1 }} />
                            </div>
                          );
                        })}
                        {data?.mood && (
                          <div style={{ position: 'absolute', left: `${Math.min(96, Math.max(4, ((data.mood.valence + 1) / 2) * 100))}%`, top: `${Math.min(96, Math.max(4, (1 - data.mood.arousal) * 100))}%`, transform: 'translate(-50%,-50%)', width: 14, height: 14, borderRadius: '50%', background: moodColor, outline: '2px solid var(--card)', boxShadow: '0 0 0 2px var(--rose)' }} />
                        )}
                      </div>
                    )}

                    {moodSel && (
                      <div className="vstack vstack-10" style={{ marginTop: 14, background: 'var(--card2)', borderRadius: 14, padding: '13px 15px', display: 'flex', flexDirection: 'column' }}>
                        <div className="hstack hstack-8" style={{ display: 'flex', alignItems: 'baseline' }}>
                          <span style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--ink)' }}>{moodSel.emotion}</span>
                          <span style={{ fontFamily: FONT_DISPLAY, fontSize: 10.5, color: 'var(--ghost)' }}>{moodSel.time}</span>
                          {moodSel.domain && <span style={{ fontSize: 10.5, color: 'var(--faint)' }}>· {moodSel.domain}</span>}
                        </div>
                        <span style={{ fontSize: 12, color: 'var(--ink2)', lineHeight: 1.8 }}>{moodSel.note}</span>

                        {moodDraft && (
                          <div className="vstack vstack-10" style={{ borderTop: '1px dashed var(--line)', marginTop: 2, paddingTop: 10, display: 'flex', flexDirection: 'column' }}>
                            <span style={{ fontSize: 11.5, color: 'var(--ghost)', letterSpacing: 1 }}>
                              修正这一刻的情绪{moodSel.path ? '' : ' · 该点缺少可写回路径'}
                            </span>
                            <div className="hstack hstack-10">
                              <span style={{ fontSize: 11.5, color: 'var(--mut)', width: 50, flexShrink: 0 }}>V 愉悦</span>
                              <input
                                type="range"
                                min={-1}
                                max={1}
                                step={0.01}
                                value={moodDraft.valence}
                                disabled={!moodSel.path || savingMood}
                                style={{ flex: 1, accentColor: 'var(--rose)', opacity: moodSel.path ? 1 : 0.5, cursor: moodSel.path ? 'pointer' : 'not-allowed' }}
                                onChange={(e) => setMoodDraft((draft) => draft ? { ...draft, valence: Number(e.target.value) } : draft)}
                              />
                              <span style={{ fontFamily: FONT_DISPLAY, fontSize: 11.5, color: 'var(--rose)', width: 40, textAlign: 'right', flexShrink: 0 }}>{moodDraft.valence >= 0 ? '+' : ''}{moodDraft.valence.toFixed(2)}</span>
                            </div>
                            <div className="hstack hstack-10">
                              <span style={{ fontSize: 11.5, color: 'var(--mut)', width: 50, flexShrink: 0 }}>A 唤醒</span>
                              <input
                                type="range"
                                min={0}
                                max={1}
                                step={0.01}
                                value={moodDraft.arousal}
                                disabled={!moodSel.path || savingMood}
                                style={{ flex: 1, accentColor: 'var(--gold)', opacity: moodSel.path ? 1 : 0.5, cursor: moodSel.path ? 'pointer' : 'not-allowed' }}
                                onChange={(e) => setMoodDraft((draft) => draft ? { ...draft, arousal: Number(e.target.value) } : draft)}
                              />
                              <span style={{ fontFamily: FONT_DISPLAY, fontSize: 11.5, color: 'var(--gold)', width: 40, textAlign: 'right', flexShrink: 0 }}>{moodDraft.arousal.toFixed(2)}</span>
                            </div>
                            <div className="hstack hstack-8" style={{ display: 'flex' }}>
                              <div onClick={resetMoodDraft} style={{ cursor: moodSel.path ? 'pointer' : 'not-allowed', flex: 1, textAlign: 'center', padding: '9px 0', borderRadius: 999, background: 'var(--card)', color: 'var(--mut)', fontSize: 12.5, letterSpacing: 1 }}>还原</div>
                              <div onClick={() => void saveMoodCorrection()} style={{ cursor: moodSel.path && !savingMood ? 'pointer' : 'not-allowed', flex: 1, textAlign: 'center', padding: '9px 0', borderRadius: 999, background: moodSel.path ? 'var(--rosebg)' : 'var(--card)', color: moodSel.path ? 'var(--deep)' : 'var(--ghost)', fontSize: 12.5, letterSpacing: 1 }}>{savingMood ? '保存中…' : '保存修正'}</div>
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                    <span style={{ display: 'block', fontSize: 11, color: 'var(--ghost)', marginTop: 10, lineHeight: 1.7 }}>点击一个点可以看到它关联的记忆。</span>
                  </div>
                </div>
              )}

              {/* ── 工具 ── */}
              {tab === 'tools' && (
                <div className="vstack vstack-14">
                  <div style={{ padding: '16px 17px', borderRadius: 16, background: 'linear-gradient(135deg,rgba(183,110,121,0.10),rgba(217,164,65,0.10))', color: 'var(--ink2)', fontSize: 13, lineHeight: 1.8 }}>
                    <div style={{ color: 'var(--deep)', fontSize: 15, letterSpacing: 1 }}>Tool Drawer v2 · 48 小时试用</div>
                    <div style={{ marginTop: 6 }}>固定工具墙，不再按关键词替换每轮工具。这里看费佳现在真正拥有的能力和边界；工具直觉说明放在费佳档案里编辑。</div>
                    <button type="button" onClick={() => navigate('/profile')} style={{ marginTop: 10, padding: 0, border: 0, background: 'transparent', color: 'var(--deep)', fontSize: 12.5, cursor: 'pointer' }}>去费佳档案改工具说明 ›</button>
                  </div>
                  {(data?.toolGroups || []).length === 0 ? (
                    <EmptyState title="工具列表暂时读不到" hint="" />
                  ) : (
                    (data?.toolGroups || []).map((group) => {
                      return (
                        <div key={group.id} style={{ background: 'var(--card)', borderRadius: 16, boxShadow: '0 6px 16px var(--shadow)', overflow: 'hidden' }}>
                          <div style={{ padding: '13px 15px', color: 'var(--deep)', fontSize: 13.5, letterSpacing: 1 }}>{group.label}</div>
                          <div className="vstack vstack-8" style={{ padding: '0 15px 15px', display: 'flex', flexDirection: 'column' }}>
                            {group.tools.map((tool) => (
                              <div key={tool.capability_id} style={{ padding: '11px 12px', borderRadius: 12, background: 'var(--card2)' }}>
                                <div className="hstack hstack-8" style={{ display: 'flex', alignItems: 'center' }}>
                                  <span style={{ fontSize: 13, color: 'var(--ink)', flex: 1 }}>{tool.display_label}</span>
                                  <span style={{ fontSize: 10.5, color: 'var(--ok)' }}>{tool.status_label}</span>
                                </div>
                                <div style={{ marginTop: 6, fontSize: 12, lineHeight: 1.7, color: 'var(--mut)' }}>{tool.physical_boundary}</div>
                              </div>
                            ))}
                          </div>
                        </div>
                      );
                    })
                  )}
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {/* ── dream detail ── */}
      {dreamOpen && (() => {
        const scene = classifyDreamScene(dreamOpen);
        const body = formatDreamBody(dreamOpen.content);
        return (
          <div onClick={() => setDreamOpen(null)} style={{ position: 'fixed', inset: 0, zIndex: 80, background: 'rgba(14,9,7,0.72)', backdropFilter: 'blur(4px)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 22 }}>
            <div className="hide-scrollbar" onClick={(e) => e.stopPropagation()} style={{ position: 'relative', width: '100%', maxWidth: 392, maxHeight: '80vh', overflowY: 'auto', borderRadius: 22, background: 'linear-gradient(172deg,#2E241D,#171009)', boxShadow: '0 40px 100px rgba(0,0,0,0.6)' }}>
              <div style={{ position: 'absolute', inset: 0, background: scene.glow, opacity: 0.16, pointerEvents: 'none' }} />
              <div className="vstack vstack-12" style={{ position: 'relative', padding: '24px 24px 22px', display: 'flex', flexDirection: 'column' }}>
                <div className="flex-wrap-gap-8" style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap' }}>
                  <span style={{ fontSize: 10, letterSpacing: 1.5, padding: '3px 10px', borderRadius: 999, background: 'rgba(223,178,94,0.14)', color: '#D9B87E' }}>{scene.label}</span>
                  <span style={{ fontFamily: fontFamilyForText(dreamOpen.dateLabel), fontSize: 10.5, color: 'rgba(233,214,190,0.55)', marginLeft: 'auto' }}>{dreamOpen.dateLabel}</span>
                </div>
                <span style={{ fontFamily: fontFamilyForText(dreamOpen.title), fontStyle: hasCJK(dreamOpen.title) ? 'normal' : 'italic', fontSize: 16, letterSpacing: 1, color: '#E8D3B0', lineHeight: 1.5 }}>{dreamOpen.title}</span>
                <div className="flex-wrap-gap-10" style={{ display: 'flex', flexWrap: 'wrap' }}>
                  <span style={{ fontFamily: FONT_DISPLAY, fontSize: 10.5, color: 'rgba(233,214,190,0.62)', letterSpacing: 1 }}>V {dreamOpen.valence >= 0 ? '+' : ''}{dreamOpen.valence.toFixed(2)}</span>
                  <span style={{ fontFamily: FONT_DISPLAY, fontSize: 10.5, color: 'rgba(233,214,190,0.62)', letterSpacing: 1 }}>A {dreamOpen.arousal.toFixed(2)}</span>
                </div>
                <div style={{ fontSize: 14, lineHeight: 2.05, color: '#EFE2D3', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{body}</div>
                <div className="hstack hstack-10" style={{ display: 'flex', alignItems: 'center', marginTop: 4 }}>
                  <span style={{ fontSize: 12, letterSpacing: 2, color: 'rgba(232,180,188,0.88)', background: 'transparent' }}>{dreamOpen.emotion}</span>
                  <div onClick={() => setDreamOpen(null)} style={{ cursor: 'pointer', marginLeft: 'auto', padding: '8px 20px', borderRadius: 999, border: '1px solid rgba(233,214,190,0.35)', color: '#E8D3B0', fontSize: 12, letterSpacing: 2 }}>离开梦境</div>
                </div>
              </div>
            </div>
          </div>
        );
      })()}

      {/* ── lightbox ── */}
      {lightbox && (
        <div onClick={() => setLightbox(null)} className="vstack vstack-16 c78-fill-fixed" style={{ zIndex: 80, background: 'rgba(24,16,14,0.88)', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: 24, cursor: 'zoom-out' }}>
          <img src={galleryPhotoUrl(lightbox.pid)} alt={lightbox.note} style={{ width: 'min(560px,92vw)', maxHeight: '70vh', objectFit: 'contain', borderRadius: 20, boxShadow: '0 40px 100px rgba(0,0,0,0.5)' }} />
          <div className="vstack vstack-4" style={{ alignItems: 'center' }}>
            <span style={{ fontSize: 13, color: 'rgba(247,237,234,0.9)', letterSpacing: 1 }}>{lightbox.note || lightbox.summary || '未命名'}</span>
            <span style={{ fontSize: 11, color: 'rgba(247,237,234,0.5)' }}>
              <span style={{ fontFamily: FONT_DISPLAY }}>{lightbox.time}</span>
              <span style={{ fontFamily: FONT_CN }}> · 点击任意处关闭</span>
            </span>
          </div>
        </div>
      )}

      {/* ── owner unlock ── */}
      {unlockOpen && (
        <div onClick={() => setUnlockOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 95, background: 'rgba(24,16,14,0.55)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24 }}>
          <div onClick={(e) => e.stopPropagation()} className="vstack vstack-12" style={{ width: '100%', maxWidth: 360, background: 'var(--card)', borderRadius: 18, padding: '18px 18px 16px', boxShadow: '0 20px 50px var(--shadow2)', display: 'flex', flexDirection: 'column' }}>
            <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--ink)' }}>主人授权</span>
            <span style={{ fontSize: 12.5, lineHeight: 1.7, color: 'var(--ink2)' }}>更换封面、点赞评论、情绪修正和工具开关需要输入服务端配置的口令。口令只用于换取 HttpOnly 会话，不会写进前端代码。</span>
            <input
              type="password"
              value={unlockDraft}
              onChange={(e) => setUnlockDraft(e.target.value)}
              placeholder="输入 MOMENTS_OWNER_TOKEN"
              autoComplete="off"
              style={{ border: '1px solid var(--line)', borderRadius: 12, padding: '10px 12px', background: 'var(--card2)', color: 'var(--ink)', fontSize: 13, outline: 'none' }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  void submitOwnerUnlock();
                }
              }}
            />
            <div className="hstack hstack-10" style={{ display: 'flex', justifyContent: 'flex-end' }}>
              <div onClick={() => setUnlockOpen(false)} style={{ cursor: 'pointer', padding: '8px 14px', borderRadius: 999, color: 'var(--ghost)', fontSize: 12.5 }}>取消</div>
              <div onClick={() => void submitOwnerUnlock()} style={{ cursor: unlockBusy ? 'default' : 'pointer', padding: '8px 16px', borderRadius: 999, background: 'var(--deep)', color: '#FBF3F0', fontSize: 12.5, opacity: unlockBusy ? 0.6 : 1 }}>{unlockBusy ? '验证中…' : '授权'}</div>
            </div>
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

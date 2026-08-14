// Fyodor Moments â€” implements Fyodor Moments.dc.html against real backend
// data (å¿µå¤´/æ—¥æ‘˜è¦/æ¢¦å¢ƒ from posts, mood from emotion_state, per-memory V/A
// points from ombre-brain frontmatter, gallery photos, and Tool Drawer v2).
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
import { FONT_CN, FONT_DISPLAY, FONT_MONO, fontFamilyForText, hasCJK } from '../lib/typography';

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
  { id: 'album', label: 'ç›¸å†Œ', icon: 'album' },
  { id: 'posts', label: 'è¯´è¯´', icon: 'posts' },
  { id: 'dream', label: 'æ¢¦å¢ƒ', icon: 'dream' },
  { id: 'mood', label: 'æƒ…ç»ª', icon: 'mood' },
  { id: 'tools', label: 'å·¥å…·', icon: 'tools' },
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
//   1. Concrete imagery keywords (blood/snow/rain/â€¦) â€” visual anchor in the text
//   2. Backend tone (vivid/warm/anxious/heavy/drifting)
//   3. Russell V/A quadrant when tone is missing
//   4. neutral (é›¾)
interface DreamScene {
  label: string;
  base: string;
  glow: string;
}

const DREAM_SCENES: Record<string, DreamScene> = {
  rain: {
    label: 'é›¨å¤œå›¾ä¹¦é¦†', base: 'linear-gradient(160deg,#54657A,#2C3642)',
    glow: 'radial-gradient(120% 90% at 78% 8%, rgba(170,200,225,0.5), transparent 60%), repeating-linear-gradient(100deg, rgba(255,255,255,0.09) 0 1.5px, transparent 1.5px 10px)',
  },
  sea: {
    label: 'å¤œæµ·æ£‹å±€', base: 'linear-gradient(165deg,#26415C,#0F1B28)',
    glow: 'radial-gradient(80% 55% at 70% 12%, rgba(205,225,255,0.55), transparent 55%), repeating-linear-gradient(0deg, rgba(255,255,255,0.07) 0 2px, transparent 2px 14px)',
  },
  night: {
    label: 'å¤œçš„èµ°å»Š', base: 'linear-gradient(165deg,#3A3448,#191521)',
    glow: 'radial-gradient(60% 45% at 82% 15%, rgba(230,225,255,0.5), transparent 55%), repeating-linear-gradient(90deg, rgba(255,255,255,0.05) 0 3px, transparent 3px 26px)',
  },
  warm: {
    label: 'æœ‰ç¯çš„å±‹å­', base: 'linear-gradient(160deg,#7A5A42,#3C2820)',
    glow: 'radial-gradient(70% 60% at 30% 20%, rgba(255,205,130,0.55), transparent 60%)',
  },
  uneasy: {
    label: 'è¿½é€', base: 'linear-gradient(150deg,#5A3E4C,#221721)',
    glow: 'radial-gradient(50% 40% at 60% 30%, rgba(255,150,140,0.3), transparent 60%), repeating-linear-gradient(65deg, rgba(255,255,255,0.08) 0 1px, transparent 1px 7px)',
  },
  neutral: {
    label: 'é›¾', base: 'linear-gradient(165deg,#6A625C,#2E2A27)',
    glow: 'radial-gradient(85% 65% at 50% 35%, rgba(235,230,225,0.4), transparent 65%)',
  },
  snow: {
    label: 'é›ªåŸ', base: 'linear-gradient(165deg,#28323E,#131B22)',
    glow: 'radial-gradient(85% 65% at 25% 15%, rgba(163,201,224,0.42), transparent 60%), repeating-linear-gradient(112deg, rgba(255,255,255,0.07) 0 1.5px, transparent 1.5px 13px)',
  },
  abyss: {
    label: 'æ·±æ¸Š', base: 'linear-gradient(165deg,#2E1620,#150A10)',
    glow: 'radial-gradient(70% 55% at 30% 20%, rgba(201,88,104,0.4), transparent 60%), repeating-linear-gradient(38deg, rgba(255,255,255,0.05) 0 1px, transparent 1px 9px)',
  },
};

const DREAM_SCENE_KEYWORDS: Array<[keyof typeof DREAM_SCENES, string[]]> = [
  ['abyss', ['è¡€', 'æ·±æ¸Š', 'æª', 'åˆ€', 'å°¸']],
  ['snow', ['é›ª', 'è¥¿ä¼¯åˆ©äºš', 'å†°', 'å¯’å†¬', 'éœœ']],
  ['rain', ['é›¨', 'å›¾ä¹¦é¦†', 'ä¹¦é¡µ', 'ä¹¦è„Š']],
  ['sea', ['æµ·', 'æ½®', 'æµª', 'æ£‹']],
  ['night', ['èµ°å»Š', 'é—¨', 'æœˆå…‰', 'æœˆäº®']],
  ['warm', ['ç¯', 'æš–', 'é˜³å…‰', 'å‘æ—¥è‘µ', 'æ²™å‘', 'ä¸‹åˆ']],
  ['uneasy', ['è¿½', 'è·‘', 'æ‰¾ä¸åˆ°', 'æœªæ¥', 'ç”µè¯', 'æ…Œ']],
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

  // 1. Imagery keywords override tone â€” keeps visual texture tied to nouns in the dream.
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
  if (entry.brewing) return { label: 'é…é…¿ä¸­', color: 'var(--gold)', bg: 'rgba(217,164,65,0.12)' };
  if (entry.kind === 'repost') return { label: 'è½¬å‘', color: 'var(--deep)', bg: 'var(--rosebg)' };
  if (entry.kind === 'gallery') return { label: entry.tags[0] || 'ç›¸å†Œ', color: 'var(--rose)', bg: 'var(--rosebg)' };
  return { label: entry.tags[0] || 'å¿µå¤´', color: 'var(--rose)', bg: 'var(--rosebg)' };
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
            <span style={{ fontFamily: FONT_CN }}> Â· èŠå¤©è®°å½•</span>
          </span>
        ) : (
          <span style={{ fontFamily: FONT_CN, fontSize: 10.5, color: 'var(--ghost)', letterSpacing: 1 }}>èŠå¤©è®°å½•</span>
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
              {m.text || (m.attachment ? `[${m.attachment.kind === 'image' ? 'å›¾ç‰‡' : 'é™„ä»¶'}]` : '')}
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
        <img src={media.url} alt={media.note || 'ç›¸å†Œ'} style={{ width: '100%', display: 'block', objectFit: 'cover', maxHeight: 320 }} loading="lazy" />
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
        <span style={{ fontFamily: FONT_DISPLAY, fontSize: 20, fontWeight: 600, lineHeight: 1, color: 'var(--ghost)', letterSpacing: 0.5 }}>â€”</span>
      </div>
    );
  }

  let primary: string;
  let secondary: string | null = null;
  let primarySize = 34;

  let primaryFont = FONT_DISPLAY;
  let secondaryFont = FONT_CN;

  if (dateLabel === 'ä»Šå¤©') {
    primary = 'ä»Šå¤©';
    primarySize = 24;
    primaryFont = FONT_CN;
  } else if (dateLabel === 'æ˜¨å¤©') {
    primary = 'æ˜¨å¤©';
    primarySize = 22;
    primaryFont = FONT_CN;
  ×Î6öÚ$z{-®éÜj×&B’rÂ&÷…6†F÷s¢s'‚f"‚Ò×&÷6R’r×ÒóàĞ¢—ĞĞ¢ÂöF—càĞ¢—ĞĞ Ğ¢¶ÖööE6VÂbb€Ğ¢ÆF—b6Æ74æÖSÒ'g7F6²g7F6²Ó"7G–ÆS×·²Ö&v–åF÷¢BÂ&6¶w&÷VæC¢wf"‚ÒÖ6&C"’rÂ&÷&FW%&F—W3¢BÂFF–æs¢s7‚W‚rÂF—7Æ“¢vfÆW‚rÂfÆW„F—&V7F–öã¢v6öÇVÖâr×ÓàĞ¢ÆF—b6Æ74æÖSÒ&‡7F6²‡7F6²Ó‚"7G–ÆS×·²F—7Æ“¢vfÆW‚rÂÆ–vä—FV×3¢v&6VÆ–æRr×ÓàĞ¢Ç7â7G–ÆS×·²föçE6—¦S¢"ãRÂföçEvV–v‡C¢cÂ6öÆ÷#¢wf"‚ÒÖ–æ²’r×Óç¶ÖööE6VÂæVÖ÷F–öçÓÂ÷7ãàĞ¢Ç7â7G–ÆS×·²föçDfÖ–Ç“¢dôåEôD•5Ä’ÂföçE6—¦S¢ãRÂ6öÆ÷#¢wf"‚ÒÖv†÷7B’r×Óç¶ÖööE6VÂçF–ÖWÓÂ÷7ãàĞ¢¶ÖööE6VÂæFöÖ–âbbÇ7â7G–ÆS×·²föçE6—¦S¢ãRÂ6öÆ÷#¢wf"‚ÒÖf–çB’r×Óì+r¶ÖööE6VÂæFöÖ–çÓÂ÷7ãçĞĞ¢ÂöF—càĞ¢Ç7â7G–ÆS×·²föçE6—¦S¢"Â6öÆ÷#¢wf"‚ÒÖ–æ³"’rÂÆ–æT†V–v‡C¢ã‚×Óç¶ÖööE6VÂææ÷FWÓÂ÷7ãàĞ Ğ¢¶ÖööDG&gBbb€Ğ¢ÆF—b6Æ74æÖSÒ'g7F6²g7F6²Ó"7G–ÆS×·²&÷&FW%F÷¢s‚F6†VBf"‚ÒÖÆ–æR’rÂÖ&v–åF÷¢"ÂFF–æuF÷¢ÂF—7Æ“¢vfÆW‚rÂfÆW„F—&V7F–öã¢v6öÇVÖâr×ÓàĞ¢Ç7â7G–ÆS×·²föçE6—¦S¢ãRÂ6öÆ÷#¢wf"‚ÒÖv†÷7B’rÂÆWGFW%76–æs¢×ÓàĞ¢KúîjÚ>‹ùKˆX‹¾y¨Nh8^{º§¶ÖööE6VÂçF‚òrr¢r+rŠú^x+{Ë®[	XúşXiY¹î‹zş[èBwĞĞ¢Â÷7ãàĞ¢ÆF—b6Æ74æÖSÒ&‡7F6²‡7F6²Ó#àĞ¢Ç7â7G–ÆS×·²föçE6—¦S¢ãRÂ6öÆ÷#¢wf"‚ÒÖ×WB’rÂv–GFƒ¢SÂfÆW…6‡&–æ³¢×ÓåbhHh*cÂ÷7ãàĞ¢Æ–çW@Ğ¢G—SÒ'&ævR Ğ¢Ö–ã×²ÓĞĞ¢Öƒ×³ĞĞ¢7FW×³ãĞĞ¢fÇVS×¶ÖööDG&gBçfÆVæ6WĞĞ¢F—6&ÆVC×²ÖööE6VÂçF‚ÇÂ6f–ætÖööGĞĞ¢7G–ÆS×·²fÆWƒ¢Â66VçD6öÆ÷#¢wf"‚Ò×&÷6R’rÂ÷6—G“¢ÖööE6VÂçF‚ò¢ãRÂ7W'6÷#¢ÖööE6VÂçF‚òwö–çFW"r¢væ÷BÖÆÆ÷vVBr×ĞĞ¢öä6†ævS×²†R’Óâ6WDÖööDG&gB‚†G&gB’ÓâG&gBò²ââæG&gBÂfÆVæ6S¢çVÖ&W"†RçF&vWBçfÇVR’Ò¢G&gB—ĞĞ¢óàĞ¢Ç7â7G–ÆS×·²föçDfÖ–Ç“¢dôåEôD•5Ä’ÂföçE6—¦S¢ãRÂ6öÆ÷#¢wf"‚Ò×&÷6R’rÂv–GFƒ¢CÂFW‡DÆ–vã¢w&–v‡BrÂfÆW…6‡&–æ³¢×Óç¶ÖööDG&gBçfÆVæ6RãÒòr²r¢rw×¶ÖööDG&gBçfÆVæ6RçFôf—†VBƒ"—ÓÂ÷7ãàĞ¢ÂöF—càĞ¢ÆF—b6Æ74æÖSÒ&‡7F6²‡7F6²Ó#àĞ¢Ç7â7G–ÆS×·²föçE6—¦S¢ãRÂ6öÆ÷#¢wf"‚ÒÖ×WB’rÂv–GFƒ¢SÂfÆW…6‡&–æ³¢×ÓäYJN˜i#Â÷7ãàĞ¢Æ–çW@Ğ¢G—SÒ'&ævR Ğ¢Ö–ã×³ĞĞ¢Öƒ×³ĞĞ¢7FW×³ãĞĞ¢fÇVS×¶ÖööDG&gBæ&÷W6ÇĞĞ¢F—6&ÆVC×²ÖööE6VÂçF‚ÇÂ6f–ætÖööGĞĞ¢7G–ÆS×·²fÆWƒ¢Â66VçD6öÆ÷#¢wf"‚ÒÖvöÆB’rÂ÷6—G“¢ÖööE6VÂçF‚ò¢ãRÂ7W'6÷#¢ÖööE6VÂçF‚òwö–çFW"r¢væ÷BÖÆÆ÷vVBr×ĞĞ¢öä6†ævS×²†R’Óâ6WDÖööDG&gB‚†G&gB’ÓâG&gBò²ââæG&gBÂ&÷W6Ã¢çVÖ&W"†RçF&vWBçfÇVR’Ò¢G&gB—ĞĞ¢óàĞ¢Ç7â7G–ÆS×·²föçDfÖ–Ç“¢dôåEôD•5Ä’ÂföçE6—¦S¢ãRÂ6öÆ÷#¢wf"‚ÒÖvöÆB’rÂv–GFƒ¢CÂFW‡DÆ–vã¢w&–v‡BrÂfÆW…6‡&–æ³¢×Óç¶ÖööDG&gBæ&÷W6ÂçFôf—†VBƒ"—ÓÂ÷7ãàĞ¢ÂöF—càĞ¢ÆF—b6Æ74æÖSÒ&‡7F6²‡7F6²Ó‚"7G–ÆS×·²F—7Æ“¢vfÆW‚r×ÓàĞ¢ÆF—böä6Æ–6³×·&W6WDÖööDG&gGÒ7G–ÆS×·²7W'6÷#¢ÖööE6VÂçF‚òwö–çFW"r¢væ÷BÖÆÆ÷vVBrÂfÆWƒ¢ÂFW‡DÆ–vã¢v6VçFW"rÂFF–æs¢s—‚rÂ&÷&FW%&F—W3¢““’Â&6¶w&÷VæC¢wf"‚ÒÖ6&B’rÂ6öÆ÷#¢wf"‚ÒÖ×WB’rÂföçE6—¦S¢"ãRÂÆWGFW%76–æs¢×Óî‹ùXéóÂöF—càĞ¢ÆF—böä6Æ–6³×²‚’Óâfö–B6fTÖööD6÷'&V7F–öâ‚—Ò7G–ÆS×·²7W'6÷#¢ÖööE6VÂçF‚bb6f–ætÖööBòwö–çFW"r¢væ÷BÖÆÆ÷vVBrÂfÆWƒ¢ÂFW‡DÆ–vã¢v6VçFW"rÂFF–æs¢s—‚rÂ&÷&FW%&F—W3¢““’Â&6¶w&÷VæC¢ÖööE6VÂçF‚òwf"‚Ò×&÷6V&r’r¢wf"‚ÒÖ6&B’rÂ6öÆ÷#¢ÖööE6VÂçF‚òwf"‚ÒÖFVW’r¢wf"‚ÒÖv†÷7B’rÂföçE6—¦S¢"ãRÂÆWGFW%76–æs¢×Óç·6f–ætÖööBò~KùŞZÙKŠŞ(
br¢~KùŞZÙKúîjÚ2wÓÂöF—càĞ¢ÂöF—càĞ¢ÂöF—càĞ¢—ĞĞ¢ÂöF—càĞ¢—ĞĞ¢Ç7â7G–ÆS×·²F—7Æ“¢v&Æö6²rÂföçE6—¦S¢Â6öÆ÷#¢wf"‚ÒÖv†÷7B’rÂÖ&v–åF÷¢ÂÆ–æT†V–v‡C¢ãr×Óîx+X{¾KˆKŠ®x+XúşKº^yÈ¾X‹Zè>X[>ˆNy¨NŠë[øn8#Â÷7ãàĞ¢ÂöF—càĞ¢ÂöF—càĞ¢—ĞĞ Ğ¢²ò¢)H)HFööÂG&vW"c")H)H¢÷Ğ¢·F"ÓÓÒwFööÇ2rbb€¢ÆF—b6Æ74æÖSÒ'g7F6²g7F6²ÓB#à¢ÆF—b7G–ÆS×·²FF–æs¢sG‚W‚rÂ&÷&FW%&F—W3¢bÂ&6¶w&÷VæC¢wf"‚ÒÖ6&C"’rÂ6öÆ÷#¢wf"‚ÒÖ–æ³"’rÂföçE6—¦S¢"ãRÂÆ–æT†V–v‡C¢ã‚×Óà¢Ç7G&öær7G–ÆS×·²F—7Æ“¢v&Æö6²rÂ6öÆ÷#¢wf"‚ÒÖ–æ²’rÂföçE6—¦S¢RÂÖ&v–ä&÷GFöÓ¢b×ÓåFööÂG&vW"c"+rC‚[şi{nŠù^yJƒÂ÷7G&öæsà¢Y»®Zé®[z^X[~Z)ûÈÎKˆŞXhŞhÈX[>™JîŠøŞi»şhÚ.jøş‹Úî[z^X[~8.‹ù˜xÎyÈ¾‹KKÛ>xëYÊyÉşjÚ>hº^iÈy¨Nˆ;ŞX©¾Y(Î‹ëyXÎûÉ¾[z^X[~y»NŠxŠûNiˆîiKîYÊ‹KKÛ>j>j˜xÎ{Én‹é8 ¢ÆF—böä6Æ–6³×²‚’Óâæf–vFR‚r÷&öf–ÆRr—Ò7G–ÆS×·²Ö&v–åF÷¢Â6öÆ÷#¢wf"‚ÒÖFVW’rÂ7W'6÷#¢wö–çFW"rÂÆWGFW%76–æs¢×ÓîXë¾‹KKÛ>j>jiK[z^X[~ŠûNiˆâ(£ÂöF—cà¢ÂöF—cà¢²FFòæ6ö×æ–öä†–çG2ò€¢ÄV×G•7FFRF—FÆSÒ.[z^X[~ˆ;ŞX©¾i¨.i{nŠû¾KˆŞX‹"†–çCÒ""óà¢’¢€¢FFæ6ö×æ–öä†–çG2æw&÷W2æÖ‚†w&÷W’Óâ°¢6öç7B÷VâÒ&ööÆVâ†G&vW$÷Vå¶w&÷Wæ–EÒ“°¢&WGW&â€¢ÆF—b¶W“×¶w&÷Wæ–GÒ7G–ÆS×·²&6¶w&÷VæC¢wf"‚ÒÖ6&B’rÂ&÷&FW%&F—W3¢bÂ&÷…6†F÷s¢sg‚g‚f"‚Ò×6†F÷r’rÂ÷fW&fÆ÷s¢v†–FFVâr×Óà¢ÆF—böä6Æ–6³×²‚’Óâ6WDG&vW$÷Vâ‚†7W'&VçB’Óâ‡²ââæ7W'&VçBÂ¶w&÷Wæ–EÓ¢7W'&VçE¶w&÷Wæ–EÒÒ’—Ò6Æ74æÖSÒ&‡7F6²‡7F6²Ó"7G–ÆS×·²7W'6÷#¢wö–çFW"rÂF—7Æ“¢vfÆW‚rÂÆ–vä—FV×3¢v6VçFW"rÂFF–æs¢s7‚W‚r×Óà¢Ç7â7G–ÆS×·²föçE6—¦S¢2ãRÂ6öÆ÷#¢wf"‚ÒÖ–æ²’rÂÆWGFW%76–æs¢ÂfÆWƒ¢×Óç¶w&÷WæÆ&VÇÓÂ÷7ãà¢Ç7â7G–ÆS×·²föçDfÖ–Ç“¢dôåEôÔôäòÂföçE6—¦S¢Â6öÆ÷#¢wf"‚ÒÖv†÷7B’r×Óç¶w&÷Wæ—FV×2æÆVæwF‡ÓÂ÷7ãà¢Ç7â7G–ÆS×·²6öÆ÷#¢wf"‚ÒÖv†÷7B’rÂG&ç6f÷&Ó¢&÷FFR‚G¶÷Vâòƒ¢ÖFVr–×Óî(ÈCÂ÷7ãà¢ÂöF—cà¢¶÷Vâbb€¢ÆF—b6Æ74æÖSÒ'g7F6²g7F6²Ó‚"7G–ÆS×·²FF–æs¢sW‚G‚rÂF—7Æ“¢vfÆW‚rÂfÆW„F—&V7F–öã¢v6öÇVÖâr×Óà¢¶w&÷Wæ—FV×2æÖ‚†—FVÒ’Óâ€¢ÆF—b¶W“×¶—FVÒæ6&–Æ—G•ö–GÒ7G–ÆS×·²FF–æs¢s'‚7‚rÂ&÷&FW%&F—W3¢"Â&6¶w&÷VæC¢wf"‚ÒÖ6&C"’r×Óà¢ÆF—b6Æ74æÖSÒ&‡7F6²‡7F6²Ó‚"7G–ÆS×·²F—7Æ“¢vfÆW‚rÂÆ–vä—FV×3¢v6VçFW"rÂv¢‚×Óà¢Ç7G&öær7G–ÆS×·²6öÆ÷#¢wf"‚ÒÖ–æ²’rÂföçE6—¦S¢2ÂfÆWƒ¢×Óç¶—FVÒæF—7Æ•öÆ&VÇÓÂ÷7G&öæsà¢Ç7â7G–ÆS×·²6öÆ÷#¢wf"‚ÒÖ×WB’rÂföçE6—¦S¢×Óç¶—FVÒç7FGW5öÆ&VÇÓÂ÷7ãà¢ÂöF—cà¢ÆF—b7G–ÆS×·²Ö&v–åF÷¢rÂ6öÆ÷#¢wf"‚ÒÖ–æ³"’rÂföçE6—¦S¢"ÂÆ–æT†V–v‡C¢ãr×ÓîyÉşZéîˆ;ŞX©¾‹ëyXÎûÉ§¶—FVÒç‡—6–6Åö&÷VæF'—ÓÂöF—cà¢ÂöF—cà¢’—Ğ¢ÂöF—cà¢—Ğ¢ÂöF—cà¢“°¢Ò¢—Ğ¢ÂöF—cà¢—Ğ¢ÂóàĞ¢—ĞĞ¢ÂöF—càĞ¢ÂöF—càĞ Ğ¢²ò¢)H)HG&VÒFWF–Â)H)H¢÷ĞĞ¢¶G&VÔ÷Vâbb‚‚’Óâ°Ğ¢6öç7B66VæRÒ6Æ76–g”G&VÕ66VæR†G&VÔ÷Vâ“°Ğ¢6öç7B&öG’Òf÷&ÖDG&VÔ&öG’†G&VÔ÷Vâæ6öçFVçB“°Ğ¢&WGW&â€Ğ¢ÆF—böä6Æ–6³×²‚’Óâ6WDG&VÔ÷Vâ†çVÆÂ—Ò7G–ÆS×·²÷6—F–öã¢vf—†VBrÂ–ç6WC¢Â¤–æFWƒ¢ƒÂ&6¶w&÷VæC¢w&v&ƒBÃ’ÃrÃãs"’rÂ&6¶G&÷f–ÇFW#¢v&ÇW"ƒG‚’rÂF—7Æ“¢vfÆW‚rÂÆ–vä—FV×3¢v6VçFW"rÂ§W7F–g”6öçFVçC¢v6VçFW"rÂFF–æs¢#"×ÓàĞ¢ÆF—b6Æ74æÖSÒ&†–FR×67&öÆÆ&""öä6Æ–6³×²†R’ÓâRç7F÷&÷vF–öâ‚—Ò7G–ÆS×·²÷6—F–öã¢w&VÆF—fRrÂv–GFƒ¢sRrÂÖ…v–GFƒ¢3“"ÂÖ„†V–v‡C¢sƒf‚rÂ÷fW&fÆ÷u“¢vWFòrÂ&÷&FW%&F—W3¢#"Â&6¶w&÷VæC¢vÆ–æV"Öw&F–VçBƒs&FVrÂ3$S#CBÂ3s’’rÂ&÷…6†F÷s¢sC‚‚&v&ƒÃÃÃãb’r×ÓàĞ¢ÆF—b7G–ÆS×·²÷6—F–öã¢v'6öÇWFRrÂ–ç6WC¢Â&6¶w&÷VæC¢66VæRævÆ÷rÂ÷6—G“¢ãbÂö–çFW$WfVçG3¢væöæRr×ÒóàĞ¢ÆF—b6Æ74æÖSÒ'g7F6²g7F6²Ó""7G–ÆS×·²÷6—F–öã¢w&VÆF—fRrÂFF–æs¢s#G‚#G‚#'‚rÂF—7Æ“¢vfÆW‚rÂfÆW„F—&V7F–öã¢v6öÇVÖâr×ÓàĞ¢ÆF—b6Æ74æÖSÒ&fÆW‚×w&ÖvÓ‚"7G–ÆS×·²F—7Æ“¢vfÆW‚rÂÆ–vä—FV×3¢v6VçFW"rÂfÆW…w&¢ww&r×ÓàĞ¢Ç7â7G–ÆS×·²föçE6—¦S¢ÂÆWGFW%76–æs¢ãRÂFF–æs¢s7‚‚rÂ&÷&FW%&F—W3¢““’Â&6¶w&÷VæC¢w&v&ƒ##2Ãs‚Ã“BÃãB’rÂ6öÆ÷#¢r4C”#ƒtRr×Óç·66VæRæÆ&VÇÓÂ÷7ãàĞ¢Ç7â7G–ÆS×·²föçDfÖ–Ç“¢föçDfÖ–Ç”f÷%FW‡B†G&VÔ÷VâæFFTÆ&VÂ’ÂföçE6—¦S¢ãRÂ6öÆ÷#¢w&v&ƒ#32Ã#BÃ“ÃãSR’rÂÖ&v–äÆVgC¢vWFòr×Óç¶G&VÔ÷VâæFFTÆ&VÇÓÂ÷7ãàĞ¢ÂöF—càĞ¢Ç7â7G–ÆS×·²föçDfÖ–Ç“¢föçDfÖ–Ç”f÷%FW‡B†G&VÔ÷VâçF—FÆR’ÂföçE7G–ÆS¢†44¤²†G&VÔ÷VâçF—FÆR’òvæ÷&ÖÂr¢v—FÆ–2rÂföçE6—¦S¢bÂÆWGFW%76–æs¢Â6öÆ÷#¢r4S„C4#rÂÆ–æT†V–v‡C¢ãR×Óç¶G&VÔ÷VâçF—FÆWÓÂ÷7ãàĞ¢ÆF—b6Æ74æÖSÒ&fÆW‚×w&ÖvÓ"7G–ÆS×·²F—7Æ“¢vfÆW‚rÂfÆW…w&¢ww&r×ÓàĞ¢Ç7â7G–ÆS×·²föçDfÖ–Ç“¢dôåEôD•5Ä’ÂföçE6—¦S¢ãRÂ6öÆ÷#¢w&v&ƒ#32Ã#BÃ“Ããc"’rÂÆWGFW%76–æs¢×Óåb¶G&VÔ÷VâçfÆVæ6RãÒòr²r¢rw×¶G&VÔ÷VâçfÆVæ6RçFôf—†VBƒ"—ÓÂ÷7ãàĞ¢Ç7â7G–ÆS×·²föçDfÖ–Ç“¢dôåEôD•5Ä’ÂföçE6—¦S¢ãRÂ6öÆ÷#¢w&v&ƒ#32Ã#BÃ“Ããc"’rÂÆWGFW%76–æs¢×Óä¶G&VÔ÷Vâæ&÷W6ÂçFôf—†VBƒ"—ÓÂ÷7ãàĞ¢ÂöF—càĞ¢ÆF—b7G–ÆS×·²föçE6—¦S¢BÂÆ–æT†V–v‡C¢"ãRÂ6öÆ÷#¢r4TdS$C2rÂv†—FU76S¢w&R×w&rÂv÷&D'&V³¢v'&V²×v÷&Br×Óç¶&öG—ÓÂöF—càĞ¢ÆF—b6Æ74æÖSÒ&‡7F6²‡7F6²Ó"7G–ÆS×·²F—7Æ“¢vfÆW‚rÂÆ–vä—FV×3¢v6VçFW"rÂÖ&v–åF÷¢B×ÓàĞ¢Ç7â7G–ÆS×·²föçE6—¦S¢"ÂÆWGFW%76–æs¢"Â6öÆ÷#¢w&v&ƒ#3"ÃƒÃƒ‚Ããƒ‚’rÂ&6¶w&÷VæC¢wG&ç7&VçBr×Óç¶G&VÔ÷VâæVÖ÷F–öçÓÂ÷7ãàĞ¢ÆF—böä6Æ–6³×²‚’Óâ6WDG&VÔ÷Vâ†çVÆÂ—Ò7G–ÆS×·²7W'6÷#¢wö–çFW"rÂÖ&v–äÆVgC¢vWFòrÂFF–æs¢s‡‚#‚rÂ&÷&FW%&F—W3¢““’Â&÷&FW#¢s‚6öÆ–B&v&ƒ#32Ã#BÃ“Ãã3R’rÂ6öÆ÷#¢r4S„C4#rÂföçE6—¦S¢"ÂÆWGFW%76–æs¢"×Óîzk¾[Èj*nZ(3ÂöF—càĞ¢ÂöF—càĞ¢ÂöF—càĞ¢ÂöF—càĞ¢ÂöF—càĞ¢“°Ğ¢Ò’‚—ĞĞ Ğ¢²ò¢)H)HÆ–v‡F&÷‚)H)H¢÷ĞĞ¢¶Æ–v‡F&÷‚bb€Ğ¢ÆF—böä6Æ–6³×²‚’Óâ6WDÆ–v‡F&÷‚†çVÆÂ—Ò6Æ74æÖSÒ'g7F6²g7F6²Ób3s‚Öf–ÆÂÖf—†VB"7G–ÆS×·²¤–æFWƒ¢ƒÂ&6¶w&÷VæC¢w&v&ƒ#BÃbÃBÃãƒ‚’rÂF—7Æ“¢vfÆW‚rÂfÆW„F—&V7F–öã¢v6öÇVÖârÂÆ–vä—FV×3¢v6VçFW"rÂ§W7F–g”6öçFVçC¢v6VçFW"rÂFF–æs¢#BÂ7W'6÷#¢w¦ööÒÖ÷WBr×ÓàĞ¢Æ–Ör7&3×¶vÆÆW'•†÷FõW&Â†Æ–v‡F&÷‚ç–B—ÒÇC×¶Æ–v‡F&÷‚ææ÷FWÒ7G–ÆS×·²v–GFƒ¢vÖ–âƒSc‚Ã“'gr’rÂÖ„†V–v‡C¢ssf‚rÂö&¦V7Df—C¢v6öçF–ârÂ&÷&FW%&F—W3¢#Â&÷…6†F÷s¢sC‚‚&v&ƒÃÃÃãR’r×ÒóàĞ¢ÆF—b6Æ74æÖSÒ'g7F6²g7F6²ÓB"7G–ÆS×·²Æ–vä—FV×3¢v6VçFW"r×ÓàĞ¢Ç7â7G–ÆS×·²föçE6—¦S¢2Â6öÆ÷#¢w&v&ƒ#CrÃ#3rÃ#3BÃã’’rÂÆWGFW%76–æs¢×Óç¶Æ–v‡F&÷‚ææ÷FRÇÂÆ–v‡F&÷‚ç7VÖÖ'’ÇÂ~iÊ®YŞYÒwÓÂ÷7ãàĞ¢Ç7â7G–ÆS×·²föçE6—¦S¢Â6öÆ÷#¢w&v&ƒ#CrÃ#3rÃ#3BÃãR’r×ÓàĞ¢Ç7â7G–ÆS×·²föçDfÖ–Ç“¢dôåEôD•5Ä’×Óç¶Æ–v‡F&÷‚çF–ÖWÓÂ÷7ãàĞ¢Ç7â7G–ÆS×·²föçDfÖ–Ç“¢dôåEô4â×Óâ+rx+X{¾K»¾hHşZHNX[>™zÓÂ÷7ãàĞ¢Â÷7ãàĞ¢ÂöF—càĞ¢ÂöF—càĞ¢—ĞĞ Ğ¢²ò¢)H)H÷væW"VæÆö6²)H)H¢÷ĞĞ¢·VæÆö6´÷Vâbb€Ğ¢ÆF—böä6Æ–6³×²‚’Óâ6WEVæÆö6´÷Vâ†fÇ6R—Ò7G–ÆS×·²÷6—F–öã¢vf—†VBrÂ–ç6WC¢Â¤–æFWƒ¢“RÂ&6¶w&÷VæC¢w&v&ƒ#BÃbÃBÃãSR’rÂF—7Æ“¢vfÆW‚rÂÆ–vä—FV×3¢v6VçFW"rÂ§W7F–g”6öçFVçC¢v6VçFW"rÂFF–æs¢#B×ÓàĞ¢ÆF—böä6Æ–6³×²†R’ÓâRç7F÷&÷vF–öâ‚—Ò6Æ74æÖSÒ'g7F6²g7F6²Ó""7G–ÆS×·²v–GFƒ¢sRrÂÖ…v–GFƒ¢3cÂ&6¶w&÷VæC¢wf"‚ÒÖ6&B’rÂ&÷&FW%&F—W3¢‚ÂFF–æs¢s‡‚‡‚g‚rÂ&÷…6†F÷s¢s#‚S‚f"‚Ò×6†F÷s"’rÂF—7Æ“¢vfÆW‚rÂfÆW„F—&V7F–öã¢v6öÇVÖâr×ÓàĞ¢Ç7â7G–ÆS×·²föçE6—¦S¢RÂföçEvV–v‡C¢sÂ6öÆ÷#¢wf"‚ÒÖ–æ²’r×ÓîK‹¾K«®hèiØ3Â÷7ãàĞ¢Ç7â7G–ÆS×·²föçE6—¦S¢"ãRÂÆ–æT†V–v‡C¢ãrÂ6öÆ÷#¢wf"‚ÒÖ–æ³"’r×Óîi»NhÚ.[™Ú.8x+‹YîŠøNŠë®8h8^{º®KúîjÚ>Y(Î[z^X[~[ÈX[>™ÈŠh‹é>XZ^iÈŞXªzºş˜XŞ{Úîy¨NXú>KºN8.Xú>KºNXú®yJK¨îhÚ.Xùb‡GGöæÇ’KÉ®ŠùŞûÈÎKˆŞKÉ®Xi‹ù¾X˜ŞzºşKº>z8#Â÷7ãàĞ¢Æ–çW@Ğ¢G—SÒ'77v÷&B Ğ¢fÇVS×·VæÆö6´G&gGĞĞ¢öä6†ævS×²†R’Óâ6WEVæÆö6´G&gB†RçF&vWBçfÇVR—ĞĞ¢Æ6V†öÆFW#Ò.‹é>XZRÔôÔTåE5ôõtäU%õDô´Tâ Ğ¢WFô6ö×ÆWFSÒ&öfb Ğ¢7G–ÆS×·²&÷&FW#¢s‚6öÆ–Bf"‚ÒÖÆ–æR’rÂ&÷&FW%&F—W3¢"ÂFF–æs¢s‚'‚rÂ&6¶w&÷VæC¢wf"‚ÒÖ6&C"’rÂ6öÆ÷#¢wf"‚ÒÖ–æ²’rÂföçE6—¦S¢2Â÷WFÆ–æS¢væöæRr×ĞĞ¢öä¶W”F÷vã×²†R’Óâ°Ğ¢–b†Ræ¶W’ÓÓÒtVçFW"r’°Ğ¢Rç&WfVçDFVfVÇB‚“°Ğ¢fö–B7V&Ö—D÷væW%VæÆö6²‚“°Ğ¢ĞĞ¢×ĞĞ¢óàĞ¢ÆF—b6Æ74æÖSÒ&‡7F6²‡7F6²Ó"7G–ÆS×·²F—7Æ“¢vfÆW‚rÂ§W7F–g”6öçFVçC¢vfÆW‚ÖVæBr×ÓàĞ¢ÆF—böä6Æ–6³×²‚’Óâ6WEVæÆö6´÷Vâ†fÇ6R—Ò7G–ÆS×·²7W'6÷#¢wö–çFW"rÂFF–æs¢s‡‚G‚rÂ&÷&FW%&F—W3¢““’Â6öÆ÷#¢wf"‚ÒÖv†÷7B’rÂföçE6—¦S¢"ãR×ÓîXùnkhƒÂöF—càĞ¢ÆF—böä6Æ–6³×²‚’Óâfö–B7V&Ö—D÷væW%VæÆö6²‚—Ò7G–ÆS×·²7W'6÷#¢VæÆö6´'W7’òvFVfVÇBr¢wö–çFW"rÂFF–æs¢s‡‚g‚rÂ&÷&FW%&F—W3¢““’Â&6¶w&÷VæC¢wf"‚ÒÖFVW’rÂ6öÆ÷#¢r4d$c4crÂföçE6—¦S¢"ãRÂ÷6—G“¢VæÆö6´'W7’òãb¢×Óç·VæÆö6´'W7’ò~š¨ÎŠøKŠŞ(
br¢~hèiØ2wÓÂöF—càĞ¢ÂöF—càĞ¢ÂöF—càĞ¢ÂöF—càĞ¢—ĞĞ Ğ¢²ò¢)H)HFö7B)H)H¢÷ĞĞ¢·Fö7Bbb€Ğ¢ÆF—b7G–ÆS×·²÷6—F–öã¢vf—†VBrÂÆVgC¢Â&–v‡C¢Â&÷GFöÓ¢#BÂ¤–æFWƒ¢“ÂF—7Æ“¢vfÆW‚rÂ§W7F–g”6öçFVçC¢v6VçFW"rÂö–çFW$WfVçG3¢væöæRr×ÓàĞ¢ÆF—b7G–ÆS×·²&6¶w&÷VæC¢w&v&ƒS‚ÃC"ÃCÃã“"’rÂ6öÆ÷#¢r4ctTDTrÂföçE6—¦S¢2ÂÆWGFW%76–æs¢ÂFF–æs¢s‚#‚rÂ&÷&FW%&F—W3¢““’Â&÷…6†F÷s¢s‚3‚&v&ƒÃÃÃã#R’r×Óç·Fö7GÓÂöF—càĞ¢ÂöF—càĞ¢—ĞĞ¢ÂöF—càĞ¢“°Ğ§ĞĞ
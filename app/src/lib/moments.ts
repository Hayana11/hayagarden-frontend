// Data layer for the Moments page (Fyodor Moments.dc.html), wired to the
// real ombre-brain / gallery / tool-drawer endpoints. Unlike the dashboard
// screens, Moments never falls back to fictional mock content on failure —
// a failed fetch surfaces an honest error state instead of fake data.
import { http } from './http';

// ── raw wire shapes ──

interface BrainItemsResponse<T> {
  ok: boolean;
  items?: T[];
  error?: string;
}

interface ThoughtRow {
  time: string; // 'HH:MM'
  content: string;
}

interface DreamRow {
  date: string; // 'YYYY-MM-DD'
  title: string;
  content: string;
  emotion: string; // hardcoded '朦胧' server-side today
}

interface DiaryRow {
  date: string; // 'YYYY-MM-DD'
  content: string;
}

interface EmotionMemoryRow {
  time: string; // 'YYYY-MM-DD'
  valence: number;
  arousal: number;
  emotion: string;
  note: string;
  domain: string;
}

interface EmotionStateResponse {
  ok: boolean;
  current?: {
    pa: number;
    na: number;
    valence: number;
    arousal: number;
    mood_word: string;
    longing: number;
    updated_at: string | null;
    sternberg_p: number;
    sternberg_i: number;
    sternberg_c: number;
  };
  error?: string;
}

interface GalleryPhotoRow {
  pid: string;
  album_id: number | null;
  note: string | null;
  width: number | null;
  height: number | null;
  favorite: boolean | number;
  created_at: string;
  saved_at: string | null;
  source_type: string | null;
  summary: string;
  emotion: string;
  keywords: string[];
  importance: number;
}

interface ToolDrawersResponse {
  ok: boolean;
  enabled: boolean;
  drawers: Array<{ id: string; label: string; tools: string[] }>;
  error?: string;
}

// ── frontend-facing types ──

export type FeedKind = 'thought' | 'diary' | 'dream';

export interface FeedEntry {
  kind: FeedKind;
  /** sortable ISO-ish key: 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD' */
  sortKey: string;
  dateLabel: string;
  timeLabel: string;
  content: string;
  title?: string;
}

export interface EmotionMemoryPoint {
  time: string;
  valence: number;
  arousal: number;
  emotion: string;
  note: string;
  domain: string;
}

export interface MoodState {
  valence: number;
  arousal: number;
  moodWord: string;
  longing: number;
  updatedAt: string | null;
}

export interface GalleryPhoto {
  pid: string;
  note: string;
  width: number | null;
  height: number | null;
  favorite: boolean;
  time: string;
  summary: string;
  emotion: string;
  keywords: string[];
}

export interface ToolDrawer {
  id: string;
  label: string;
  tools: string[];
}

export interface MomentsData {
  feed: FeedEntry[];
  gallery: GalleryPhoto[];
  mood: MoodState | null;
  emotionMemories: EmotionMemoryPoint[];
  drawers: ToolDrawer[];
  drawersEnabled: boolean;
  /** endpoints that failed to load, for an honest partial-failure notice */
  failedSources: string[];
}

function shortTime(hhmm: string | undefined): string {
  return (hhmm || '').slice(0, 5);
}

async function safe<T>(label: string, fn: () => Promise<T>, failed: string[]): Promise<T | null> {
  try {
    const r = await fn();
    return r;
  } catch {
    failed.push(label);
    return null;
  }
}

export async function fetchMomentsData(): Promise<MomentsData> {
  const failed: string[] = [];

  const [thoughts, dreams, diary, moodRes, emoMemRes, galleryRes, drawersRes] = await Promise.all([
    safe('thoughts', () => http.get<BrainItemsResponse<ThoughtRow>>('/api/brain/thoughts'), failed),
    safe('dreams', () => http.get<BrainItemsResponse<DreamRow>>('/api/brain/dreams'), failed),
    safe('diary', () => http.get<BrainItemsResponse<DiaryRow>>('/api/brain/diary'), failed),
    safe('mood', () => http.get<EmotionStateResponse>('/api/brain/emotion_state'), failed),
    safe('emotion memories', () => http.get<BrainItemsResponse<EmotionMemoryRow>>('/api/brain/emotions'), failed),
    safe('gallery', () => http.get<{ photos: GalleryPhotoRow[] }>('/api/gallery/photos'), failed),
    safe('tool drawers', () => http.get<ToolDrawersResponse>('/api/tools/drawers'), failed),
  ]);

  const feed: FeedEntry[] = [];
  if (thoughts?.ok) {
    for (const t of thoughts.items || []) {
      if (!t.content) continue;
      const today = new Date().toISOString().slice(0, 10);
      feed.push({ kind: 'thought', sortKey: `${today} ${shortTime(t.time)}`, dateLabel: '', timeLabel: shortTime(t.time), content: t.content });
    }
  } else if (thoughts && !thoughts.ok) failed.push('thoughts');

  if (dreams?.ok) {
    for (const d of dreams.items || []) {
      if (!d.content) continue;
      feed.push({ kind: 'dream', sortKey: `${d.date} 00:00`, dateLabel: d.date, timeLabel: '', content: d.content, title: d.title });
    }
  } else if (dreams && !dreams.ok) failed.push('dreams');

  if (diary?.ok) {
    for (const d of diary.items || []) {
      if (!d.content) continue;
      feed.push({ kind: 'diary', sortKey: `${d.date} 00:00`, dateLabel: d.date, timeLabel: '', content: d.content });
    }
  } else if (diary && !diary.ok) failed.push('diary');

  feed.sort((a, b) => (a.sortKey < b.sortKey ? 1 : a.sortKey > b.sortKey ? -1 : 0));

  const mood: MoodState | null = moodRes?.ok && moodRes.current
    ? {
        valence: moodRes.current.valence,
        arousal: moodRes.current.arousal,
        moodWord: moodRes.current.mood_word || '平静',
        longing: moodRes.current.longing,
        updatedAt: moodRes.current.updated_at,
      }
    : null;
  if (moodRes && !moodRes.ok) failed.push('mood');

  const emotionMemories: EmotionMemoryPoint[] = emoMemRes?.ok
    ? (emoMemRes.items || []).map((r) => ({ time: r.time, valence: r.valence, arousal: r.arousal, emotion: r.emotion, note: r.note, domain: r.domain }))
    : [];
  if (emoMemRes && !emoMemRes.ok) failed.push('emotion memories');

  const gallery: GalleryPhoto[] = (galleryRes?.photos || []).map((p) => ({
    pid: p.pid,
    note: p.note || '',
    width: p.width,
    height: p.height,
    favorite: Boolean(p.favorite),
    time: p.saved_at || p.created_at,
    summary: p.summary || '',
    emotion: p.emotion || '',
    keywords: p.keywords || [],
  }));

  const drawers: ToolDrawer[] = drawersRes?.ok ? drawersRes.drawers : [];
  if (drawersRes && !drawersRes.ok) failed.push('tool drawers');

  return {
    feed,
    gallery,
    mood,
    emotionMemories,
    drawers,
    drawersEnabled: Boolean(drawersRes?.enabled),
    failedSources: failed,
  };
}

export function galleryPhotoUrl(pid: string): string {
  return `/api/gallery/photo/${encodeURIComponent(pid)}`;
}

export function moodWordTone(valence: number): 'up' | 'down' | 'level' {
  if (valence >= 0.15) return 'up';
  if (valence <= -0.15) return 'down';
  return 'level';
}

export async function fetchMomentsCover(): Promise<string | null> {
  try {
    const r = await http.get<{ ok: boolean; url: string | null }>('/api/moments/cover');
    return r.ok && r.url ? r.url : null;
  } catch {
    return null;
  }
}

export async function uploadMomentsCover(file: File): Promise<string | null> {
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await fetch('/api/moments/cover', { method: 'POST', body: fd });
    const j = await r.json();
    return r.ok && j.ok && j.url ? j.url : null;
  } catch {
    return null;
  }
}

// Data layer for the Moments page (Fyodor Moments.dc.html), wired to the
// real ombre-brain / gallery / Gateway tool inventory endpoints. Unlike the dashboard
// screens, Moments never falls back to fictional mock content on failure —
// a failed fetch surfaces an honest error state instead of fake data.
import { http } from './http';

const SHANGHAI_TZ = 'Asia/Shanghai';

// ── raw wire shapes ──

interface BrainItemsResponse<T> {
  ok: boolean;
  items?: T[];
  has_more?: boolean;
  next_before?: number | null;
  error?: string;
}

interface DreamRow {
  id: number;
  author: string;
  created_at: string | null;
  date: string;
  title: string;
  content: string;
  emotion: string;
  tone: string;
  valence: number;
  arousal: number;
}

interface EmotionMemoryRow {
  path?: string;
  time: string;
  valence: number;
  arousal: number;
  emotion: string;
  note: string;
  domain: string;
  scale?: string;
}

interface EmotionHistoryRow {
  day: string;
  valence: number;
  arousal: number;
  mood_word: string;
  samples: number;
}

interface EmotionHistoryResponse {
  ok: boolean;
  days: number;
  series: EmotionHistoryRow[];
  error?: string;
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
  visual_description: string;
  first_impression: string;
  keywords: string[];
  importance: number;
}

import type { ToolInventoryGroup, ToolInventoryResponse } from './toolInventory';

interface RepostWireMessage {
  message_id: number;
  role: 'haya' | 'fyodor';
  text: string;
  created_at: string | null;
  attachment?: { kind: string; url?: string; name?: string };
}

interface FeedWireItem {
  item_key: string;
  post_id: number | null;
  collection_id: number | null;
  gallery_pid: string | null;
  kind: 'thought' | 'repost' | 'gallery';
  author: string;
  content: string;
  title: string | null;
  tags: string[];
  brewing: boolean;
  created_at: string | null;
  media: Array<{ pid: string; url: string; width: number | null; height: number | null; note?: string }>;
  repost: {
    source_label: string;
    messages: RepostWireMessage[];
  } | null;
  social: {
    likes: number;
    dislikes: number;
    comments: number;
    my_reaction: 'like' | 'dislike' | null;
  };
}

interface FeedWireResponse {
  items: FeedWireItem[];
  next_cursor: string | null;
  has_more: boolean;
  error?: string;
}

// ── frontend-facing types ──

export type FeedKind = 'thought' | 'repost' | 'gallery';

export interface RepostMessage {
  messageId: number;
  role: 'haya' | 'fyodor';
  text: string;
  createdAt: string | null;
  attachment?: { kind: string; url?: string; name?: string };
}

export interface FeedMedia {
  pid: string;
  url: string;
  width: number | null;
  height: number | null;
  note: string;
}

export interface FeedSocial {
  likes: number;
  dislikes: number;
  comments: number;
  myReaction: 'like' | 'dislike' | null;
}

export interface FeedEntry {
  itemKey: string;
  kind: FeedKind;
  postId: number | null;
  collectionId: number | null;
  galleryPid: string | null;
  author: string;
  content: string;
  createdAt: string | null;
  tags: string[];
  brewing: boolean;
  social: FeedSocial;
  media: FeedMedia[];
  repost: { sourceLabel: string; messages: RepostMessage[] } | null;
  dateLabel: string;
  timeLabel: string;
}

export interface DreamEntry {
  id: number;
  author: string;
  createdAt: string | null;
  dateLabel: string;
  title: string;
  content: string;
  emotion: string;
  tone: string;
  valence: number;
  arousal: number;
}

export interface MomentsFeedPage {
  items: FeedEntry[];
  nextCursor: string | null;
  hasMore: boolean;
}

export interface EmotionMemoryPoint {
  path?: string;
  time: string;
  valence: number;
  arousal: number;
  emotion: string;
  note: string;
  domain: string;
}

export interface EmotionHistoryPoint {
  day: string;
  valence: number;
  arousal: number;
  moodWord: string;
  samples: number;
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
  visualDescription: string;
  firstImpression: string;
  keywords: string[];
}

export interface MomentsData {
  dreams: DreamEntry[];
  dreamsHasMore: boolean;
  dreamsNextBefore: number | null;
  gallery: GalleryPhoto[];
  mood: MoodState | null;
  emotionMemories: EmotionMemoryPoint[];
  toolGroups: ToolInventoryGroup[];
  toolInventoryTotal: number;
  toolInventoryAvailable: number;
  failedSources: string[];
}

function shanghaiDateParts(iso: string | null): { year: number; month: number; day: number; hour: number; minute: number } | null {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: SHANGHAI_TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(date);
  const read = (type: string) => Number(parts.find((part) => part.type === type)?.value || '0');
  return {
    year: read('year'),
    month: read('month'),
    day: read('day'),
    hour: read('hour'),
    minute: read('minute'),
  };
}

function shanghaiTodayParts(): { year: number; month: number; day: number } {
  const parts = shanghaiDateParts(new Date().toISOString());
  return parts || { year: 1970, month: 1, day: 1 };
}

export function formatFeedLabels(createdAt: string | null): { dateLabel: string; timeLabel: string } {
  const parts = shanghaiDateParts(createdAt);
  if (!parts) return { dateLabel: '', timeLabel: '' };

  const today = shanghaiTodayParts();
  const yesterday = new Date(Date.UTC(today.year, today.month - 1, today.day));
  yesterday.setUTCDate(yesterday.getUTCDate() - 1);

  const sameDay = (a: { year: number; month: number; day: number }, b: { year: number; month: number; day: number }) =>
    a.year === b.year && a.month === b.month && a.day === b.day;

  let dateLabel: string;
  if (sameDay(parts, today)) {
    dateLabel = '今天';
  } else if (
    sameDay(parts, {
      year: yesterday.getUTCFullYear(),
      month: yesterday.getUTCMonth() + 1,
      day: yesterday.getUTCDate(),
    })
  ) {
    dateLabel = '昨天';
  } else {
    dateLabel = `${parts.month}月${parts.day}日`;
  }

  const timeLabel = `${String(parts.hour).padStart(2, '0')}:${String(parts.minute).padStart(2, '0')}`;
  return { dateLabel, timeLabel };
}

function mapFeedItem(item: FeedWireItem): FeedEntry {
  const labels = formatFeedLabels(item.created_at);
  return {
    itemKey: item.item_key,
    kind: item.kind,
    postId: item.post_id,
    collectionId: item.collection_id,
    galleryPid: item.gallery_pid,
    author: item.author,
    content: item.content,
    createdAt: item.created_at,
    tags: item.tags || [],
    brewing: Boolean(item.brewing),
    social: {
      likes: item.social?.likes ?? 0,
      dislikes: item.social?.dislikes ?? 0,
      comments: item.social?.comments ?? 0,
      myReaction: item.social?.my_reaction ?? null,
    },
    media: (item.media || []).map((m) => ({
      pid: m.pid,
      url: m.url,
      width: m.width,
      height: m.height,
      note: m.note || '',
    })),
    repost: item.repost
      ? {
          sourceLabel: item.repost.source_label || '',
          messages: (item.repost.messages || []).map((m) => ({
            messageId: m.message_id,
            role: m.role,
            text: m.text,
            createdAt: m.created_at,
            attachment: m.attachment,
          })),
        }
      : null,
    dateLabel: labels.dateLabel,
    timeLabel: labels.timeLabel,
  };
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

export type FeedType = 'all' | 'posts';

export interface DreamsPage {
  items: DreamEntry[];
  nextBefore: number | null;
  hasMore: boolean;
}

function mapDreamRow(d: DreamRow): DreamEntry {
  const labels = formatFeedLabels(d.created_at);
  return {
    id: d.id,
    author: d.author,
    createdAt: d.created_at,
    dateLabel: labels.dateLabel || d.date,
    title: d.title,
    content: d.content,
    emotion: d.emotion,
    tone: d.tone || 'drifting',
    valence: d.valence ?? 0,
    arousal: d.arousal ?? 0.5,
  };
}

export async function fetchDreamsPage(before?: number, limit = 20): Promise<DreamsPage> {
  const response = await http.get<BrainItemsResponse<DreamRow>>('/api/brain/dreams', {
    limit,
    before,
  });
  if (!response.ok) {
    throw new Error(response.error || 'dreams fetch failed');
  }
  return {
    items: (response.items || []).map(mapDreamRow),
    nextBefore: response.next_before ?? null,
    hasMore: Boolean(response.has_more),
  };
}

export async function fetchMomentsFeed(
  cursor?: string,
  limit = 20,
  feedType: FeedType = 'all',
): Promise<MomentsFeedPage> {
  const response = await http.get<FeedWireResponse>('/api/moments/feed', {
    cursor,
    limit,
    type: feedType,
  });
  if (response.error) {
    throw new Error(response.error);
  }
  return {
    items: (response.items || []).map(mapFeedItem),
    nextCursor: response.next_cursor,
    hasMore: Boolean(response.has_more),
  };
}

export async function fetchMomentsData(): Promise<MomentsData> {
  const failed: string[] = [];

  const [dreamsPage, moodRes, emoMemRes, galleryRes, inventoryRes] = await Promise.all([
    safe('dreams', () => fetchDreamsPage(undefined, 20), failed),
    safe('mood', () => http.get<EmotionStateResponse>('/api/brain/emotion_state'), failed),
    safe('emotion memories', () => http.get<BrainItemsResponse<EmotionMemoryRow>>('/api/brain/emotions'), failed),
    safe('gallery', () => http.get<{ photos: GalleryPhotoRow[] }>('/api/gallery/photos'), failed),
    safe('tool inventory', () => http.get<ToolInventoryResponse>('/api/tools/inventory'), failed),
  ]);

  const dreamEntries: DreamEntry[] = dreamsPage?.items || [];

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
    ? (emoMemRes.items || []).map((r) => ({
        path: r.path,
        time: r.time,
        valence: r.valence,
        arousal: r.arousal,
        emotion: r.emotion,
        note: r.note,
        domain: r.domain,
      }))
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
    visualDescription: p.visual_description || '',
    firstImpression: p.first_impression || '',
    keywords: p.keywords || [],
  }));

  const toolGroups: ToolInventoryGroup[] = inventoryRes?.ok ? inventoryRes.groups || [] : [];
  if (inventoryRes && !inventoryRes.ok) failed.push('tool inventory');

  return {
    dreams: dreamEntries,
    dreamsHasMore: Boolean(dreamsPage?.hasMore),
    dreamsNextBefore: dreamsPage?.nextBefore ?? null,
    gallery,
    mood,
    emotionMemories,
    toolGroups,
    toolInventoryTotal: inventoryRes?.ok ? inventoryRes.total : 0,
    toolInventoryAvailable: inventoryRes?.ok ? inventoryRes.available_count : 0,
    failedSources: failed,
  };
}

export async function fetchEmotionHistory(days: 7 | 30 = 7): Promise<EmotionHistoryPoint[]> {
  const response = await http.get<EmotionHistoryResponse>('/api/brain/emotion_history', { days });
  if (!response.ok) {
    throw new Error(response.error || 'emotion history failed');
  }
  return (response.series || []).map((row) => ({
    day: row.day,
    valence: row.valence,
    arousal: row.arousal,
    moodWord: row.mood_word || '',
    samples: row.samples,
  }));
}

export async function updateEmotionMemory(
  path: string,
  valence: number,
  arousal: number,
): Promise<EmotionMemoryPoint> {
  const response = await http.patch<{
    ok: boolean;
    item?: EmotionMemoryRow;
    error?: string;
  }>('/api/brain/emotions', { path, valence, arousal });
  if (!response.ok || !response.item) {
    throw new Error(response.error || 'emotion update failed');
  }
  const item = response.item;
  return {
    path: item.path,
    time: item.time,
    valence: item.valence,
    arousal: item.arousal,
    emotion: item.emotion,
    note: item.note,
    domain: item.domain,
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
  const base = import.meta.env.VITE_API_BASE_URL ?? '';
  try {
    const r = await fetch(`${base}/api/moments/cover`, { method: 'POST', body: fd, credentials: 'include' });
    const j = await r.json();
    return r.ok && j.ok && j.url ? j.url : null;
  } catch {
    return null;
  }
}

export interface MomentsOwnerStatus {
  configured: boolean;
  authenticated: boolean;
}

export async function fetchMomentsOwnerStatus(): Promise<MomentsOwnerStatus> {
  const response = await http.get<{ ok: boolean; configured: boolean; authenticated: boolean }>(
    '/api/moments/session',
  );
  return {
    configured: Boolean(response.configured),
    authenticated: Boolean(response.authenticated),
  };
}

export async function establishMomentsSession(token: string): Promise<void> {
  await http.post('/api/moments/session', { token });
}

export interface MomentComment {
  id: number;
  author: string;
  content: string;
  createdAt: string | null;
}

export async function reactToMoment(itemKey: string, reaction: 'like' | 'dislike'): Promise<FeedSocial> {
  const response = await http.post<{ ok: boolean; social: FeedWireItem['social']; error?: string }>(
    '/api/moments/react',
    { item_key: itemKey, reaction },
  );
  if (!response.social) {
    throw new Error(response.error || 'react failed');
  }
  return {
    likes: response.social.likes ?? 0,
    dislikes: response.social.dislikes ?? 0,
    comments: response.social.comments ?? 0,
    myReaction: response.social.my_reaction ?? null,
  };
}

export async function fetchMomentComments(itemKey: string): Promise<MomentComment[]> {
  const response = await http.get<{ items: Array<{ id: number; author: string; content: string; created_at: string | null }> }>(
    '/api/moments/comments',
    { item_key: itemKey },
  );
  return (response.items || []).map((c) => ({
    id: c.id,
    author: c.author,
    content: c.content,
    createdAt: c.created_at,
  }));
}

export async function postMomentComment(itemKey: string, content: string): Promise<{ comment: MomentComment; social: FeedSocial }> {
  const response = await http.post<{
    ok: boolean;
    comment: { id: number; author: string; content: string; created_at: string | null };
    social: FeedWireItem['social'];
    error?: string;
  }>('/api/moments/comments', { item_key: itemKey, content });
  if (!response.comment || !response.social) {
    throw new Error(response.error || 'comment failed');
  }
  return {
    comment: {
      id: response.comment.id,
      author: response.comment.author,
      content: response.comment.content,
      createdAt: response.comment.created_at,
    },
    social: {
      likes: response.social.likes ?? 0,
      dislikes: response.social.dislikes ?? 0,
      comments: response.social.comments ?? 0,
      myReaction: response.social.my_reaction ?? null,
    },
  };
}

export function feedDateKey(entry: FeedEntry): string {
  return entry.createdAt?.slice(0, 10) || '';
}

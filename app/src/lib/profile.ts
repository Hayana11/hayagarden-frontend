import { http } from './http';

export type SavedMemory = {
  id: string;
  content: string;
  enabled: boolean;
  source: string;
  createdAt: number;
  updatedAt: number;
  importedAt?: number;
  externalId?: string;
};

export type UserProfile = {
  fullName: string;
  nickname: string;
  savedMemories: SavedMemory[];
  preferences: {
    enabled: boolean;
    content: string;
  };
  claudeExportImport: Record<string, number>;
  updatedAt: number;
};

export function blankProfile(): UserProfile {
  return {
    fullName: '',
    nickname: '',
    savedMemories: [],
    preferences: { enabled: true, content: '' },
    claudeExportImport: {},
    updatedAt: Date.now(),
  };
}

export function cloneProfile(profile: UserProfile): UserProfile {
  return JSON.parse(JSON.stringify(profile)) as UserProfile;
}

function trimText(value: unknown, limit: number): string {
  return String(value ?? '').trim().slice(0, limit);
}

export function normalizeProfile(data: unknown): UserProfile {
  const base = blankProfile();
  if (!data || typeof data !== 'object') return base;
  const raw = data as Record<string, unknown>;
  base.fullName = trimText(raw.fullName, 200);
  base.nickname = trimText(raw.nickname, 200);

  const memories: SavedMemory[] = [];
  const list = Array.isArray(raw.savedMemories) ? raw.savedMemories : [];
  for (const item of list) {
    if (typeof item === 'string') {
      const content = trimText(item, 4000);
      if (!content) continue;
      const now = Date.now();
      memories.push({
        id: crypto.randomUUID?.() ?? `${now}-${memories.length}`,
        content,
        enabled: true,
        source: 'manual',
        createdAt: now,
        updatedAt: now,
      });
    } else if (item && typeof item === 'object') {
      const row = item as Record<string, unknown>;
      const content = trimText(row.content, 4000);
      if (!content) continue;
      const now = Date.now();
      memories.push({
        id: trimText(row.id, 120) || (crypto.randomUUID?.() ?? `${now}-${memories.length}`),
        content,
        enabled: row.enabled !== false,
        source: trimText(row.source, 80) || 'manual',
        createdAt: Number(row.createdAt) || now,
        updatedAt: Number(row.updatedAt) || now,
      });
    }
    if (memories.length >= 200) break;
  }
  base.savedMemories = memories;

  const prefs = raw.preferences;
  if (typeof prefs === 'string') {
    base.preferences = { enabled: true, content: trimText(prefs, 50_000) };
  } else if (prefs && typeof prefs === 'object') {
    const p = prefs as Record<string, unknown>;
    base.preferences = {
      enabled: p.enabled !== false,
      content: trimText(p.content, 50_000),
    };
  }
  base.updatedAt = Number(raw.updatedAt) || Date.now();
  return base;
}

export function newMemory(content = ''): SavedMemory {
  const now = Date.now();
  return {
    id: crypto.randomUUID?.() ?? `mem-${now}`,
    content,
    enabled: true,
    source: 'manual',
    createdAt: now,
    updatedAt: now,
  };
}

type ProfileResponse = { ok?: boolean; profile?: unknown; error?: string };

export async function fetchProfile(): Promise<UserProfile> {
  const data = await http.get<ProfileResponse>('/api/profile');
  return normalizeProfile(data.profile);
}

export async function saveProfile(profile: UserProfile): Promise<UserProfile> {
  const payload = normalizeProfile(profile);
  const data = await http.put<ProfileResponse>('/api/profile', payload);
  return normalizeProfile(data.profile ?? payload);
}

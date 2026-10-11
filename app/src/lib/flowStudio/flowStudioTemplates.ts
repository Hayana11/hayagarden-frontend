import ruotaAsset from './templates/ruota.json';
import type { FlowStudioPool } from './flowStudioMock';

export type RuotaSourceEntry = {
  zh?: string;
  en?: string;
  ja?: string;
  gore?: boolean;
};

export type RuotaTemplate = {
  id: string;
  label: string;
  entries: string[];
  sourceTotal: number;
  importedTotal: number;
  filteredTotal: number;
};

const RUOTA_DIMENSION_IDS = ['position', 'scenario', 'props', 'roleplay', 'physical', 'mental'] as const;

const BLOCKED_ZH_LABELS = new Set([
  '合意强制', '绑架幻想', '强暴幻想', '催眠', '睡姦', '正太攻', '逆強', '模糊同意',
  '强制高潮', '水责', '窒息', '刀play', '针刺play', '穿刺play', '血play', '乳头虐',
  '春药', '吸血', '窥视癖', '偷拍', '睡眠诱导', '针', '刀', '未成年', '儿童', '幼女',
  '小学生', '中学生', '萝莉', '正太', '男童', '女童',
]);

const BLOCKED_TEXT_PATTERNS = [
  /\b(?:consensual non-consent|non-consensual|nonconsensual|dubious consent|reverse rape|rape fantasy|forced orgasm|kidnapping|coercion|without consent|unconscious|sleep induction|somnophilia|brainwashing|hypnosis|groping|secret recording|voyeurism|underage|minor|teen|schoolgirl|schoolboy|shota|loli|child|children|blood play|blood sucking|wound|cutting|knife|needle|impalement|strangl(?:ing)?|asphyx(?:iation)?|choking|drown(?:ing)?|scald(?:ing)?|acid play|water torture|torture|severe branding|extreme|amputation|organ play|bone breaking|flesh wall|cannibal|vore|stitching|nailing|piercing play|dislocation play)\b/i,
];

function isRejected(entry: RuotaSourceEntry): boolean {
  const zh = typeof entry.zh === 'string' ? entry.zh.trim() : '';
  const searchable = [entry.zh, entry.en, entry.ja].filter(Boolean).join(' ');
  return entry.gore === true
    || BLOCKED_ZH_LABELS.has(zh)
    || BLOCKED_TEXT_PATTERNS.some((pattern) => pattern.test(searchable));
}

export function filterRuotaEntries(entries: RuotaSourceEntry[]): string[] {
  return entries
    .filter((entry) => !isRejected(entry))
    .map((entry) => entry.zh?.trim() ?? '')
    .filter(Boolean);
}

const assetDimensions = (ruotaAsset as {
  dimensions: Array<{
    id: string;
    label: string;
    entries: string[];
    sourceTotal: number;
    importedTotal: number;
    filteredTotal: number;
  }>;
}).dimensions;

export const RUOTA_TEMPLATES: RuotaTemplate[] = RUOTA_DIMENSION_IDS.map((id) => {
  const dimension = assetDimensions.find((item) => item.id === id);
  if (!dimension) throw new Error(`Missing Ruota dimension: ${id}`);
  return {
    id: dimension.id,
    label: dimension.label,
    entries: [...dimension.entries],
    sourceTotal: dimension.sourceTotal,
    importedTotal: dimension.importedTotal,
    filteredTotal: dimension.filteredTotal,
  };
});

export function createRuotaPoolId(templateId: string, existingPoolIds: Iterable<string>): string {
  const used = new Set(existingPoolIds);
  const base = `ruota_${templateId}`;
  let candidate = base;
  let suffix = 2;
  while (used.has(candidate)) {
    candidate = `${base}_${suffix}`;
    suffix += 1;
  }
  return candidate;
}

export function createRuotaPool(templateId: string, existingPoolIds: Iterable<string>): FlowStudioPool {
  const template = RUOTA_TEMPLATES.find((item) => item.id === templateId);
  if (!template) throw new Error(`Unknown Ruota template: ${templateId}`);
  const id = createRuotaPoolId(template.id, existingPoolIds);
  return {
    id,
    name: `Ruota · ${template.label}`,
    enabled: true,
    mode: 'perTurn',
    count: 1,
    entries: template.entries.map((text, index) => ({
      id: `${id}_entry_${String(index + 1).padStart(3, '0')}`,
      text,
      enabled: true,
    })),
  };
}

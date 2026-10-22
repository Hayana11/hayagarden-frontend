/**
 * Passive-recall review console (/dash/memory-review): the route log of
 * `tools/memory_surface.py`, one decision per message, with the owner's labels.
 * Real data only — there is no mock fallback for a review page.
 */
import { http } from './http';

export type ReviewLabel = 'ok' | 'missed' | 'wrong';
export type ReviewFilter = 'all' | 'unlabeled' | 'labeled' | 'surfaced' | 'blocked';

export type ReviewMemory = { ref: string; date: string; type: string; text: string };

export type ReviewRow = {
  id: number;
  userMessageId: number;
  createdAt: string;
  mode: string;
  decision: string;
  reason: string;
  verdict: string | null;
  evidence: number | null;
  committed: boolean;
  message: string;
  clues: string[];
  chatDf: Record<string, number>;
  top: ReviewMemory | null;
  runnerUp: ReviewMemory | null;
  /** The memory the owner says should have come (its text when labeled). */
  expected: ReviewMemory | null;
  /** Message and memories are the decision-time snapshot (false: logged before snapshots, current text). */
  frozen: boolean;
  /** The route-log row was pruned; the decision lives on in its label. */
  logPruned: boolean;
  label: { label: ReviewLabel; note: string; expectedRef: string | null; labeledAt: string } | null;
};

export type ReviewCounts = { total: number; labeled: number; ok: number; missed: number; wrong: number };
export type ReviewPage = { rows: ReviewRow[]; counts: ReviewCounts; nextBefore: number | null };

export const REVIEW_FILTERS: { key: ReviewFilter; label: string }[] = [
  { key: 'unlabeled', label: '待复盘' },
  { key: 'surfaced', label: '浮现了' },
  { key: 'blocked', label: '被拦下' },
  { key: 'labeled', label: '已标注' },
  { key: 'all', label: '全部' },
];

export const REVIEW_LABELS: { key: ReviewLabel; mark: string; text: string }[] = [
  { key: 'ok', mark: '✓', text: '判断对' },
  { key: 'missed', mark: '✗', text: '该想起' },
  { key: 'wrong', mark: '✗', text: '不该想起' },
];

const REASONS: Record<string, string> = {
  gate_passed: '过闸',
  gate_closed: '被拦下',
  no_clues: '没有稀有词',
  no_keywords: '没有关键词',
  no_candidates: '没有候选',
  empty_pool: '记忆池为空',
  too_short: '太短',
  no_text: '没有文字',
  command: '指令',
  replayed: '重放',
  replayed_ref_gone: '重放时记忆已退役',
  kernel_cutover_off: 'Kernel 未开',
  db_unavailable: '数据库不可用',
  log_write_failed: '日志写入失败',
  error: '出错',
};

const VERDICTS: Record<string, string> = {
  chosen: '选中',
  below_evidence: '证据不够',
  cooldown: '冷却中',
  in_context: '已在上下文',
};

export function reasonText(reason: string): string {
  return REASONS[reason] ?? reason;
}

export function verdictText(verdict: string | null): string {
  return verdict ? VERDICTS[verdict] ?? verdict : '';
}

export function decisionText(row: Pick<ReviewRow, 'decision' | 'mode' | 'committed'>): string {
  if (row.decision === 'surfaced') {
    if (row.mode === 'shadow') return '会浮现（shadow）';
    return row.committed ? '已浮现' : '浮现未落库';
  }
  if (row.decision === 'failed') return '失败';
  if (row.decision === 'skipped') return '跳过';
  return '没浮现';
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : v === null || v === undefined ? '' : String(v);
}

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function memory(v: unknown): ReviewMemory | null {
  if (!v || typeof v !== 'object') return null;
  const r = v as Record<string, unknown>;
  const ref = str(r.ref);
  return ref ? { ref, date: str(r.date), type: str(r.type) || 'MEMORY', text: str(r.text) } : null;
}

function isLabel(v: unknown): v is ReviewLabel {
  return v === 'ok' || v === 'missed' || v === 'wrong';
}

export function normalizeReviewRow(v: unknown): ReviewRow | null {
  if (!v || typeof v !== 'object') return null;
  const r = v as Record<string, unknown>;
  const id = num(r.id);
  if (id === null) return null;
  const rawLabel = r.label && typeof r.label === 'object' ? (r.label as Record<string, unknown>) : null;
  const chatDf: Record<string, number> = {};
  if (r.chat_df && typeof r.chat_df === 'object') {
    for (const [k, n] of Object.entries(r.chat_df as Record<string, unknown>)) {
      if (typeof n === 'number') chatDf[k] = n;
    }
  }
  return {
    id,
    userMessageId: num(r.user_message_id) ?? 0,
    createdAt: str(r.created_at),
    mode: str(r.mode),
    decision: str(r.decision),
    reason: str(r.reason),
    verdict: r.verdict ? str(r.verdict) : null,
    evidence: num(r.evidence),
    committed: r.committed === true,
    message: str(r.message),
    clues: Array.isArray(r.clues) ? r.clues.map(str).filter(Boolean) : [],
    chatDf,
    top: memory(r.top),
    runnerUp: memory(r.runner_up),
    expected: memory(r.expected),
    frozen: r.frozen === true,
    logPruned: r.log_pruned === true,
    label: rawLabel && isLabel(rawLabel.label)
      ? {
        label: rawLabel.label,
        note: str(rawLabel.note),
        expectedRef: rawLabel.expected_ref ? str(rawLabel.expected_ref) : null,
        labeledAt: str(rawLabel.labeled_at),
      }
      : null,
  };
}

export function normalizeReviewPage(v: unknown): ReviewPage {
  const r = v && typeof v === 'object' ? (v as Record<string, unknown>) : {};
  const c = r.counts && typeof r.counts === 'object' ? (r.counts as Record<string, unknown>) : {};
  const rows = Array.isArray(r.rows) ? r.rows.map(normalizeReviewRow).filter((x): x is ReviewRow => x !== null) : [];
  return {
    rows,
    counts: {
      total: num(c.total) ?? 0,
      labeled: num(c.labeled) ?? 0,
      ok: num(c.ok) ?? 0,
      missed: num(c.missed) ?? 0,
      wrong: num(c.wrong) ?? 0,
    },
    nextBefore: num(r.next_before),
  };
}

export async function fetchReviewPage(filter: ReviewFilter, before?: number | null): Promise<ReviewPage> {
  const raw = await http.get<unknown>('/api/memory-surface/review', {
    filter,
    limit: 30,
    before: before ?? undefined,
  });
  return normalizeReviewPage(raw);
}

export async function saveReviewLabel(
  logId: number,
  label: ReviewLabel | 'clear',
  note = '',
  expectedRef: string | null = null,
): Promise<ReviewRow | null> {
  const raw = await http.post<unknown>(`/api/memory-surface/review/${logId}/label`, {
    label,
    note,
    expected_ref: expectedRef,
  });
  // Clearing the label of a decision whose route-log row is pruned removes it entirely.
  if (raw && typeof raw === 'object' && (raw as Record<string, unknown>).removed === true) return null;
  return normalizeReviewRow(raw);
}

/** Whether a decision belongs in the list for this filter (mirrors the backend's filters). */
export function matchesReviewFilter(row: ReviewRow, filter: ReviewFilter): boolean {
  switch (filter) {
    case 'unlabeled': return row.label === null;
    case 'labeled': return row.label !== null;
    case 'surfaced': return row.decision === 'surfaced';
    case 'blocked': return row.decision === 'none' && row.reason === 'gate_closed';
    default: return true;
  }
}

/**
 * The list and counts after one label save. `saved` is null when clearing removed
 * the decision entirely (its route-log row was already pruned). A card that no
 * longer matches the current filter leaves the list right away.
 */
export function applyLabelSave(
  rows: ReviewRow[],
  counts: ReviewCounts | null,
  before: ReviewRow,
  saved: ReviewRow | null,
  filter: ReviewFilter,
): { rows: ReviewRow[]; counts: ReviewCounts | null } {
  const nextRows = saved && matchesReviewFilter(saved, filter)
    ? rows.map((r) => (r.id === saved.id ? saved : r))
    : rows.filter((r) => r.id !== before.id);
  if (!counts) return { rows: nextRows, counts };
  const next = { ...counts };
  if (before.label) next[before.label.label] -= 1;
  if (saved?.label) next[saved.label.label] += 1;
  if (!saved) next.total -= 1;
  next.labeled = next.ok + next.missed + next.wrong;
  return { rows: nextRows, counts: next };
}

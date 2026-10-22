import { useCallback, useEffect, useState } from 'react';
import type { CSSProperties } from 'react';
import { useNavigate } from 'react-router-dom';
import { Card, ScreenLayout } from '../components/Card';
import { PageHeader } from '../components/PageHeader';
import { ROUTES } from '../navigation';
import {
  REVIEW_FILTERS,
  REVIEW_LABELS,
  applyLabelSave,
  decisionText,
  fetchReviewPage,
  reasonText,
  saveReviewLabel,
  verdictText,
} from '../lib/memoryReview';
import type { ReviewCounts, ReviewFilter, ReviewLabel, ReviewMemory, ReviewRow } from '../lib/memoryReview';

const chip: CSSProperties = {
  display: 'inline-block',
  fontSize: 11,
  letterSpacing: 1,
  borderRadius: 999,
  padding: '4px 11px',
  border: '1px solid var(--color-border)',
  background: 'var(--color-surface)',
  color: 'var(--color-text-soft)',
  cursor: 'pointer',
};

const chipOn: CSSProperties = {
  ...chip,
  background: 'rgba(183,110,121,0.12)',
  color: '#9C3B4A',
  border: '1px solid rgba(183,110,121,0.35)',
};

const LABEL_COLORS: Record<ReviewLabel, string> = { ok: 'var(--color-green-deep)', missed: '#B7791F', wrong: '#9C3B4A' };

function MemoryBox({ title, memory }: { title: string; memory: ReviewMemory }) {
  return (
    <div style={{ borderLeft: '2px solid rgba(183,110,121,0.35)', padding: '2px 0 2px 10px' }}>
      <div style={{ fontSize: 11, color: 'var(--color-text-faint)', letterSpacing: 1 }}>
        {title} · {memory.type} · {memory.date}
      </div>
      <div style={{ fontSize: 13, lineHeight: 1.6, color: 'var(--color-text)', marginTop: 2, wordBreak: 'break-word' }}>
        {memory.text}
      </div>
    </div>
  );
}

function ReviewCard({ row, onLabel }: { row: ReviewRow; onLabel: (row: ReviewRow, label: ReviewLabel | 'clear', note: string) => Promise<void> }) {
  const [note, setNote] = useState(row.label?.note ?? '');
  const [busy, setBusy] = useState(false);
  const current = row.label?.label ?? null;

  const apply = async (label: ReviewLabel | 'clear', nextNote: string) => {
    setBusy(true);
    try {
      await onLabel(row, label, nextNote);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card style={{ padding: '14px 16px' }}>
      <div className="vstack vstack-10">
        <div style={{ fontSize: 11, color: 'var(--color-text-faint)', letterSpacing: 1 }}>
          {row.createdAt.slice(5, 16)} · {decisionText(row)} · {reasonText(row.reason)}
          {row.verdict ? ` · ${verdictText(row.verdict)}` : ''}
          {row.evidence !== null ? ` · 证据 ${row.evidence.toFixed(1)}` : ''}
          {row.frozen ? '' : ' · 当前文本（旧记录无快照）'}
          {row.logPruned ? ' · 日志已清理' : ''}
        </div>
        <div style={{ fontSize: 14, lineHeight: 1.6, color: 'var(--color-text)', wordBreak: 'break-word' }}>
          「{row.message || '（消息已不在）'}」
        </div>
        {row.clues.length ? (
          <div>
            <div className="flex-wrap-gap-6">
              {row.clues.map((word) => (
              <span key={word} style={{ fontSize: 11, color: 'var(--color-text-mute)', background: 'var(--color-bg-secondary)', borderRadius: 8, padding: '2px 7px' }}>
                {word}
                {row.chatDf[word] !== undefined ? <span style={{ opacity: 0.6 }}> {row.chatDf[word]}</span> : null}
              </span>
              ))}
            </div>
          </div>
        ) : null}
        {row.top ? <MemoryBox title={row.decision === 'surfaced' ? '浮现的记忆' : '第一候选'} memory={row.top} /> : null}
        {row.runnerUp ? <MemoryBox title="次选（不替补）" memory={row.runnerUp} /> : null}
        {row.expected ? <MemoryBox title="该想起的" memory={row.expected} /> : null}
        <div className="hstack hstack-6">
          {REVIEW_LABELS.map((item) => {
            const on = current === item.key;
            return (
              <button
                key={item.key}
                type="button"
                disabled={busy}
                onClick={() => apply(on ? 'clear' : item.key, note)}
                style={{
                  ...chip,
                  fontSize: 12,
                  ...(on ? { background: LABEL_COLORS[item.key], color: '#fff', border: `1px solid ${LABEL_COLORS[item.key]}` } : null),
                }}
              >
                {item.mark} {item.text}
              </button>
            );
          })}
        </div>
        {current ? (
          <div className="hstack hstack-6">
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder={current === 'missed' ? '该想起哪条？' : '备注'}
              maxLength={500}
              style={{ flex: 1, minWidth: 0, fontSize: 13, padding: '6px 10px', borderRadius: 10, border: '1px solid var(--color-border)', background: 'var(--color-bg)', color: 'var(--color-text)' }}
            />
            <button
              type="button"
              disabled={busy || note === (row.label?.note ?? '')}
              onClick={() => apply(current, note)}
              style={{ ...chip, fontSize: 12 }}
            >
              存备注
            </button>
          </div>
        ) : null}
      </div>
    </Card>
  );
}

export function MemoryReviewScreen() {
  const navigate = useNavigate();
  const [filter, setFilter] = useState<ReviewFilter>('unlabeled');
  const [rows, setRows] = useState<ReviewRow[]>([]);
  const [counts, setCounts] = useState<ReviewCounts | null>(null);
  const [nextBefore, setNextBefore] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async (which: ReviewFilter, before: number | null) => {
    setLoading(true);
    setError('');
    try {
      const page = await fetchReviewPage(which, before);
      setRows((prev) => (before === null ? page.rows : prev.concat(page.rows)));
      setCounts(page.counts);
      setNextBefore(page.nextBefore);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(filter, null);
  }, [filter, load]);

  const onLabel = useCallback(async (row: ReviewRow, label: ReviewLabel | 'clear', note: string) => {
    try {
      const saved = await saveReviewLabel(row.id, label, label === 'clear' ? '' : note);
      setRows((prev) => applyLabelSave(prev, null, row, saved, filter).rows);
      setCounts((prev) => applyLabelSave([], prev, row, saved, filter).counts);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [filter]);

  return (
    <ScreenLayout>
      <PageHeader
        title="记忆复盘"
        subtitle={counts ? `${counts.labeled}/${counts.total}` : undefined}
        onBack={() => navigate(ROUTES.memory)}
        backLabel="返回记忆库"
      />
      {counts ? (
        <div style={{ fontSize: 12, color: 'var(--color-text-mute)', letterSpacing: 1 }}>
          ✓ 判断对 {counts.ok} · ✗ 该想起 {counts.missed} · ✗ 不该想起 {counts.wrong}
        </div>
      ) : null}
      <div>
        <div className="flex-wrap-gap-6">
          {REVIEW_FILTERS.map((f) => (
            <button key={f.key} type="button" onClick={() => setFilter(f.key)} style={filter === f.key ? chipOn : chip}>
              {f.label}
            </button>
          ))}
        </div>
      </div>
      {error ? <div className="config-warning">{error}</div> : null}
      {rows.map((row) => (
        <ReviewCard key={`${row.id}:${row.label?.labeledAt ?? ''}`} row={row} onLabel={onLabel} />
      ))}
      {!loading && !error && rows.length === 0 ? (
        <div style={{ fontSize: 13, color: 'var(--color-text-faint)', textAlign: 'center', padding: '24px 0' }}>这里还没有记录</div>
      ) : null}
      {nextBefore !== null ? (
        <button type="button" disabled={loading} onClick={() => load(filter, nextBefore)} style={{ ...chip, alignSelf: 'center' }}>
          {loading ? '加载中…' : '更早的'}
        </button>
      ) : null}
    </ScreenLayout>
  );
}

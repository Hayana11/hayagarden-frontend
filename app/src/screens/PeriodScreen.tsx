// 身体节律 — implements the Cycle.dc.html design: prediction hero with inline
// settings, monthly calendar with logged/predicted/ovulation marks, per-day
// record editor (flow/pain/states/intimacy/note), and a recent-cycle summary.
import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { BackHeader } from '../components/BackHeader';
import { Card, ScreenLayout } from '../components/Card';
import { fetchPeriodDays, fetchPeriodSettings, savePeriodDay, savePeriodSettings } from '../lib/api';
import {
  CYCLE_EXTRAS,
  CYCLE_STATES,
  FLOW_LEVELS,
  PAIN_LEVELS,
  addDays,
  deriveCycle,
  diffDays,
  parseYmd,
  shortMd,
  toYmd,
} from '../lib/cycle';
import type { PeriodDayRecord, PeriodDays, PeriodSettings } from '../types';
import { schedulePeriodDaySave } from '../lib/periodSave';

const WEEKDAY_EN = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const DISPLAY = 'var(--font-serif-display)';

const sectionLabelStyle: CSSProperties = {
  fontSize: 12,
  color: 'var(--color-text-faint)',
  letterSpacing: 2,
  marginTop: 18,
};

function Chip({ label, on, onClick, grow }: { label: string; on: boolean; onClick: () => void; grow?: boolean }) {
  return (
    <span
      onClick={onClick}
      style={{
        cursor: 'pointer',
        flex: grow ? 1 : undefined,
        textAlign: 'center',
        padding: grow ? '8px 0' : '8px 16px',
        borderRadius: 999,
        background: on ? 'var(--color-rose)' : '#F1E7E2',
        color: on ? '#FFF9F7' : 'var(--color-text-mute)',
        fontSize: 13,
        letterSpacing: 1,
      }}
    >
      {label}
    </span>
  );
}

function YesNoButton({
  label,
  on,
  tone,
  onClick,
}: {
  label: string;
  on: boolean;
  tone: 'green' | 'rose' | 'pink';
  onClick: () => void;
}) {
  const colors = {
    green: { fg: 'var(--color-green-deep)', bg: 'rgba(94,138,110,0.10)', border: 'var(--color-green-deep)' },
    rose: { fg: 'var(--color-rose-deep)', bg: 'rgba(183,110,121,0.10)', border: 'var(--color-rose)' },
    pink: { fg: 'var(--color-rose-pink)', bg: 'rgba(192,132,151,0.12)', border: 'var(--color-rose-pink)' },
  }[tone];
  return (
    <span
      onClick={onClick}
      style={{
        cursor: 'pointer',
        flex: 1,
        textAlign: 'center',
        padding: '10px 0',
        borderRadius: 12,
        background: on ? colors.bg : '#F9F5F3',
        color: on ? colors.fg : 'var(--color-text-faint)',
        fontSize: 13,
        letterSpacing: 3,
        border: on ? `1.5px solid ${colors.border}` : '1.5px solid transparent',
      }}
    >
      {label}
    </span>
  );
}

function Stepper({ value, onMinus, onPlus }: { value: string; onMinus: () => void; onPlus: () => void }) {
  const btn: CSSProperties = {
    cursor: 'pointer',
    width: 28,
    height: 28,
    borderRadius: 10,
    background: '#FFFFFF',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    color: 'var(--color-text-mute)',
    fontSize: 14,
    boxShadow: '0 3px 8px rgba(183,110,121,0.08)',
  };
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginLeft: 'auto' }}>
      <span onClick={onMinus} style={btn}>
        −
      </span>
      <span style={{ fontFamily: DISPLAY, fontSize: 15, fontWeight: 600, color: 'var(--color-text)', minWidth: 48, textAlign: 'center' }}>
        {value}
      </span>
      <span onClick={onPlus} style={btn}>
        ＋
      </span>
    </div>
  );
}

export function PeriodScreen() {
  const today = toYmd(new Date());
  const [days, setDays] = useState<PeriodDays | null>(null);
  const [settings, setSettings] = useState<PeriodSettings | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [viewOffset, setViewOffset] = useState(0);
  const [selDate, setSelDate] = useState(today);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [draft, setDraft] = useState<{ start: string; cycle: number; period: number } | null>(null);
  const [loadTick, setLoadTick] = useState(0);
  // Keep a mutable mirror so rapid multi-field edits on the same day merge correctly.
  const daysRef = useRef<PeriodDays>({});

  useEffect(() => {
    let cancelled = false;
    setLoadError(null);
    Promise.all([fetchPeriodDays(), fetchPeriodSettings()])
      .then(([d, s]) => {
        if (cancelled) return;
        daysRef.current = d;
        setDays(d);
        setSettings(s);
      })
      .catch(() => {
        if (cancelled) return;
        setDays(null);
        setSettings(null);
        setLoadError('经期数据暂时没有连接成功');
      });
    return () => {
      cancelled = true;
    };
  }, [loadTick]);

  if (loadError) {
    return (
      <ScreenLayout>
        <BackHeader title="身体节律" subtitle="body rhythm" />
        <Card style={{ padding: 22 }}>
          <div style={{ fontSize: 14, color: 'var(--color-text-soft)', letterSpacing: 1 }}>{loadError}</div>
          <div
            onClick={() => setLoadTick((t) => t + 1)}
            style={{
              cursor: 'pointer',
              marginTop: 16,
              textAlign: 'center',
              padding: '11px 0',
              borderRadius: 13,
              background: 'var(--color-rose)',
              color: '#FFF9F7',
              fontSize: 13,
              letterSpacing: 4,
            }}
          >
            重试
          </div>
        </Card>
      </ScreenLayout>
    );
  }

  if (!days || !settings) {
    return (
      <ScreenLayout>
        <BackHeader title="身体节律" subtitle="body rhythm" />
      </ScreenLayout>
    );
  }

  const { hasAnchor, cycleDay, inPeriod, currentStart, nextStart, daysUntil, soon, overdue, predicted, ovulation, lastRange, topStates } =
    deriveCycle(days, settings, today);
  const { cycleLength, periodLength } = settings;

  async function updateDay(date: string, patch: Partial<PeriodDayRecord>) {
    const prev = daysRef.current[date] ? { ...daysRef.current[date] } : undefined;
    const rec = { ...(daysRef.current[date] || {}), ...patch };
    daysRef.current = { ...daysRef.current, [date]: rec };
    setDays({ ...daysRef.current });
    setSaveError(null);
    try {
      const ok = await schedulePeriodDaySave(date, rec, savePeriodDay);
      if (!ok) {
        const fresh = await fetchPeriodDays();
        daysRef.current = fresh;
        setDays(fresh);
        setSaveError('保存没有成功，请再试一次');
      }
    } catch {
      try {
        const fresh = await fetchPeriodDays();
        daysRef.current = fresh;
        setDays(fresh);
      } catch {
        if (prev === undefined) {
          const next = { ...daysRef.current };
          delete next[date];
          daysRef.current = next;
        } else {
          daysRef.current = { ...daysRef.current, [date]: prev };
        }
        setDays({ ...daysRef.current });
      }
      setSaveError('保存没有成功，请再试一次');
    }
  }

  function toggleInList(date: string, key: 'states' | 'extras', label: string) {
    const list = daysRef.current[date]?.[key] || [];
    updateDay(date, { [key]: list.includes(label) ? list.filter((x) => x !== label) : [...list, label] });
  }

  // ── hero ──
  const heroCaption = !hasAnchor ? '今天是' : inPeriod ? '现在是' : '今天是';
  const heroPre = !hasAnchor ? '' : inPeriod ? '经期第' : '周期第';
  const heroSub = !hasAnchor
    ? '等待首次记录'
    : inPeriod
      ? `预计还剩 ${Math.max(periodLength - (cycleDay ?? 0), 0)} 天`
      : overdue && daysUntil !== null
        ? `比预计晚了 ${-daysUntil} 天，别担心，记录会修正它`
        : daysUntil === 0
          ? '预计就是今天'
          : `预计 ${daysUntil} 天后开始`;
  const nextLabel = inPeriod ? '本次经期' : '下次经期';
  const nextRange =
    !hasAnchor || !currentStart || !nextStart
      ? '—'
      : inPeriod
        ? `${shortMd(currentStart)} – ${shortMd(addDays(currentStart, periodLength - 1))}`
        : `${shortMd(nextStart)} – ${shortMd(addDays(nextStart, periodLength - 1))}`;
  const soonLine = hasAnchor && !inPeriod && (soon || overdue) ? '快来了，今天温柔一点' : '';

  // ── settings draft ──
  const effDraft = draft ?? { start: currentStart || today, cycle: cycleLength, period: periodLength };
  const bump = (patch: Partial<typeof effDraft>) => setDraft({ ...effDraft, ...patch });

  async function saveSettings() {
    const next = { cycleLength: effDraft.cycle, periodLength: effDraft.period, lastStart: effDraft.start };
    setSettingsSaving(true);
    setSettingsError(null);
    try {
      await savePeriodSettings(next);
      setSettings(next);
      setSettingsOpen(false);
      setDraft(null);
    } catch {
      setSettingsError('设置没有保存成功，草稿还在，可以再试');
    } finally {
      setSettingsSaving(false);
    }
  }

  // ── calendar ──
  const now = parseYmd(today);
  const base = new Date(now.getFullYear(), now.getMonth() + viewOffset, 1);
  const monthLabel = `${base.getFullYear()}.${String(base.getMonth() + 1).padStart(2, '0')}`;
  const dim = new Date(base.getFullYear(), base.getMonth() + 1, 0).getDate();
  const lead = (base.getDay() + 6) % 7;
  const cells: Array<{
    key: string;
    d: string;
    bg: string;
    border: string;
    dColor: string;
    weight: number;
    hasDot: boolean;
    hasHeart: boolean;
    heartColor: string;
    date?: string;
  }> = [];
  for (let i = 0; i < lead; i++) {
    cells.push({ key: `lead-${i}`, d: '', bg: 'transparent', border: '2px solid transparent', dColor: 'transparent', weight: 400, hasDot: false, hasHeart: false, heartColor: 'transparent' });
  }
  for (let d = 1; d <= dim; d++) {
    const ds = toYmd(new Date(base.getFullYear(), base.getMonth(), d));
    const rec = days[ds];
    const isFlow = rec?.came === true;
    const isPred = !isFlow && predicted.has(ds);
    const isOv = !isFlow && !isPred && ovulation.has(ds);
    const future = diffDays(ds, today) > 0;
    let bg = '#F9F5F3';
    let dColor = future ? '#DFD4CF' : 'var(--color-nav-inactive)';
    let weight = 400;
    if (isFlow) {
      bg = 'var(--color-rose)';
      dColor = '#FFF9F7';
      weight = 600;
    } else if (isPred) {
      bg = '#F1DCDE';
      dColor = 'var(--color-rose-deep)';
      weight = 600;
    } else if (isOv) {
      bg = '#F7EEDD';
      dColor = '#B8862F';
      weight = 600;
    }
    cells.push({
      key: ds,
      date: ds,
      d: String(d),
      bg,
      dColor,
      weight,
      border: ds === selDate ? '2px solid var(--color-rose-deep)' : ds === today ? '2px solid var(--color-amber)' : '2px solid transparent',
      hasDot: Boolean(rec && rec.came === false && ((rec.states && rec.states.length) || rec.note)),
      hasHeart: rec?.sex === true,
      heartColor: isFlow ? '#F3D2D6' : 'var(--color-rose-pink)',
    });
  }

  // ── selected day ──
  const selRec = days[selDate] || {};
  const selFuture = diffDays(selDate, today) > 0;
  const futureLine = predicted.has(selDate)
    ? '还没到这一天 · 预测这天在经期里。'
    : ovulation.has(selDate)
      ? '还没到这一天 · 预测是排卵日。'
      : '还没到这一天。';
  const obsLine = topStates.length
    ? `快来的前几天，你记过「${topStates.join('」和「')}」——身体在提前说话。`
    : '记录多了，这里会慢慢看出你的规律。';

  const monthBtn: CSSProperties = {
    cursor: 'pointer',
    width: 34,
    height: 34,
    borderRadius: 12,
    background: 'var(--color-bg)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    color: 'var(--color-text-soft)',
    fontSize: 15,
  };

  return (
    <ScreenLayout>
      <BackHeader title="身体节律" subtitle="body rhythm" />

      {saveError && (
        <div style={{ fontSize: 12, color: 'var(--color-rose-deep)', letterSpacing: 1, padding: '0 4px' }}>{saveError}</div>
      )}

      {/* ── 周期预测卡 ── */}
      <Card style={{ padding: '16px 20px 14px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <span style={{ fontSize: 12, color: 'var(--color-text-faint)', letterSpacing: 3 }}>{heroCaption}</span>
          <span
            onClick={() => {
              setSettingsOpen(!settingsOpen);
              setSettingsError(null);
              setDraft(settingsOpen ? null : { start: currentStart || today, cycle: cycleLength, period: periodLength });
            }}
            style={{ cursor: 'pointer', color: 'var(--color-rose-deep)', fontSize: 12, padding: '4px 14px', borderRadius: 999, background: 'rgba(183,110,121,0.10)' }}
          >
            调整
          </span>
        </div>

        <div style={{ display: 'flex', alignItems: 'baseline', gap: 7, marginTop: 6 }}>
          {heroPre && <span style={{ fontSize: 16, color: 'var(--color-text-soft)', letterSpacing: 1 }}>{heroPre}</span>}
          <span style={{ fontFamily: DISPLAY, fontSize: 48, fontWeight: 600, color: inPeriod ? 'var(--color-rose-deep)' : 'var(--color-text)', lineHeight: 1 }}>
            {hasAnchor ? cycleDay : '暂无记录'}
          </span>
          {hasAnchor && <span style={{ fontSize: 16, color: 'var(--color-text-soft)' }}>天</span>}
        </div>
        <div style={{ fontSize: 13, color: 'var(--color-text-mute)', marginTop: 5, letterSpacing: 1 }}>{heroSub}</div>

        {soonLine && (
          <div style={{ display: 'inline-block', marginTop: 10, padding: '5px 14px', borderRadius: 999, background: 'rgba(183,110,121,0.10)', color: 'var(--color-rose-deep)', fontSize: 13, letterSpacing: 1 }}>
            {soonLine}
          </div>
        )}

        <div style={{ display: 'flex', alignItems: 'center', marginTop: 12, paddingTop: 10, borderTop: '1px solid var(--color-track)' }}>
          <span style={{ fontSize: 12, color: 'var(--color-text-faint)', letterSpacing: 2 }}>{nextLabel}</span>
          <span style={{ fontFamily: DISPLAY, fontSize: 16, fontWeight: 600, color: 'var(--color-rose-deep)', marginLeft: 'auto', letterSpacing: 1 }}>
            {nextRange}
          </span>
        </div>

        {settingsOpen && (
          <div style={{ marginTop: 16, background: '#F9F5F3', borderRadius: 16, padding: 16, display: 'flex', flexDirection: 'column', gap: 13 }}>
            <div style={{ display: 'flex', alignItems: 'center' }}>
              <span style={{ fontSize: 13, color: 'var(--color-text-soft)', letterSpacing: 1 }}>上次开始</span>
              <Stepper
                value={shortMd(effDraft.start)}
                onMinus={() => bump({ start: addDays(effDraft.start, -1) })}
                onPlus={() => {
                  const n = addDays(effDraft.start, 1);
                  if (diffDays(n, today) <= 0) bump({ start: n });
                }}
              />
            </div>
            <div style={{ display: 'flex', alignItems: 'center' }}>
              <span style={{ fontSize: 13, color: 'var(--color-text-soft)', letterSpacing: 1 }}>周期</span>
              <Stepper
                value={`${effDraft.cycle} 天`}
                onMinus={() => bump({ cycle: Math.max(effDraft.cycle - 1, 21) })}
                onPlus={() => bump({ cycle: Math.min(effDraft.cycle + 1, 40) })}
              />
            </div>
            <div style={{ display: 'flex', alignItems: 'center' }}>
              <span style={{ fontSize: 13, color: 'var(--color-text-soft)', letterSpacing: 1 }}>经期</span>
              <Stepper
                value={`${effDraft.period} 天`}
                onMinus={() => bump({ period: Math.max(effDraft.period - 1, 2) })}
                onPlus={() => bump({ period: Math.min(effDraft.period + 1, 10) })}
              />
            </div>
            {settingsError && (
              <div style={{ fontSize: 12, color: 'var(--color-rose-deep)', letterSpacing: 1 }}>{settingsError}</div>
            )}
            <div
              onClick={settingsSaving ? undefined : () => { void saveSettings(); }}
              style={{
                cursor: settingsSaving ? 'default' : 'pointer',
                opacity: settingsSaving ? 0.6 : 1,
                textAlign: 'center',
                padding: '11px 0',
                borderRadius: 13,
                background: 'var(--color-rose)',
                color: '#FFF9F7',
                fontSize: 13,
                letterSpacing: 4,
                boxShadow: '0 8px 20px rgba(183,110,121,0.24)',
                marginTop: 2,
              }}
            >
              {settingsSaving ? '保存中…' : '保存'}
            </div>
          </div>
        )}
      </Card>

      {/* ── 月历卡 ── */}
      <Card style={{ padding: 22 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span onClick={() => setViewOffset(viewOffset - 1)} style={monthBtn}>
            ‹
          </span>
          <span style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 19, color: 'var(--color-rose-deep)', letterSpacing: 2 }}>{monthLabel}</span>
          <span onClick={() => setViewOffset(viewOffset + 1)} style={monthBtn}>
            ›
          </span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7,1fr)', gap: 6, marginTop: 16 }}>
          {['一', '二', '三', '四', '五', '六', '日'].map((w) => (
            <span key={w} style={{ textAlign: 'center', fontSize: 11, color: 'var(--color-text-faint)' }}>
              {w}
            </span>
          ))}
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7,1fr)', gap: 6, marginTop: 8 }}>
          {cells.map((c) => (
            <div
              key={c.key}
              onClick={c.date ? () => setSelDate(c.date!) : undefined}
              style={{ cursor: c.date ? 'pointer' : undefined, height: 44, borderRadius: 12, background: c.bg, border: c.border, position: 'relative', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
            >
              <span style={{ fontFamily: DISPLAY, fontSize: 13, fontWeight: c.weight, color: c.dColor }}>{c.d}</span>
              <div style={{ position: 'absolute', bottom: 4, left: 0, right: 0, display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 3 }}>
                {c.hasDot && <span style={{ width: 4, height: 4, borderRadius: '50%', background: 'var(--color-violet)' }} />}
                {c.hasHeart && <span style={{ fontSize: 8, lineHeight: 1, color: c.heartColor }}>♥</span>}
              </div>
            </div>
          ))}
        </div>
        <div style={{ display: 'flex', gap: 13, marginTop: 14, fontSize: 10, color: 'var(--color-text-faint)', flexWrap: 'wrap' }}>
          <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <span style={{ width: 9, height: 9, borderRadius: 3, background: 'var(--color-rose)' }} />
            已记录经期
          </span>
          <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <span style={{ width: 9, height: 9, borderRadius: 3, background: '#F1DCDE' }} />
            预测经期
          </span>
          <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <span style={{ width: 9, height: 9, borderRadius: 3, background: '#F3E7CE' }} />
            预测排卵
          </span>
          <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <span style={{ width: 9, height: 9, borderRadius: 3, background: '#FFFFFF', border: '1.5px solid var(--color-amber)' }} />
            今天
          </span>
          <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <span style={{ color: 'var(--color-rose-pink)', fontSize: 11, lineHeight: 1 }}>♥</span>
            亲密
          </span>
        </div>
      </Card>

      {/* ── 选中日期记录卡 ── */}
      <Card style={{ padding: 22 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontFamily: DISPLAY, fontSize: 17, fontWeight: 600, letterSpacing: 2, color: 'var(--color-text)' }}>
            {shortMd(selDate)} {WEEKDAY_EN[parseYmd(selDate).getDay()]}
          </span>
          {selDate === today && (
            <span style={{ fontSize: 10, color: '#B8862F', background: 'rgba(217,164,65,0.14)', padding: '3px 10px', borderRadius: 999, letterSpacing: 2 }}>
              今天
            </span>
          )}
          <span style={{ fontSize: 11, color: 'var(--color-text-fainter)', marginLeft: 'auto', letterSpacing: 1 }}>
            {selRec.came !== undefined || selRec.sex !== undefined ? '已记录' : ''}
          </span>
        </div>

        {selFuture ? (
          <div style={{ fontSize: 13, color: 'var(--color-text-faint)', marginTop: 16, lineHeight: 1.8 }}>{futureLine}</div>
        ) : (
          <>
            <div style={sectionLabelStyle}>是否来月经？</div>
            <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
              <YesNoButton label="没有" tone="green" on={selRec.came === false} onClick={() => updateDay(selDate, { came: false })} />
              <YesNoButton label="来了" tone="rose" on={selRec.came === true} onClick={() => updateDay(selDate, { came: true })} />
            </div>

            {selRec.came === true && (
              <>
                <div style={sectionLabelStyle}>出血量</div>
                <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
                  {FLOW_LEVELS.map((l) => (
                    <Chip key={l} label={l} grow on={selRec.flow === l} onClick={() => updateDay(selDate, { flow: l })} />
                  ))}
                </div>
                <div style={sectionLabelStyle}>疼痛</div>
                <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
                  {PAIN_LEVELS.map((l) => (
                    <Chip key={l} label={l} grow on={selRec.pain === l} onClick={() => updateDay(selDate, { pain: l })} />
                  ))}
                </div>
                <div style={sectionLabelStyle}>特殊情况</div>
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 10 }}>
                  {CYCLE_EXTRAS.map((l) => (
                    <Chip key={l} label={l} on={(selRec.extras || []).includes(l)} onClick={() => toggleInList(selDate, 'extras', l)} />
                  ))}
                </div>
              </>
            )}

            {selRec.came === false && (
              <>
                <div style={sectionLabelStyle}>状态</div>
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 10 }}>
                  {CYCLE_STATES.map((l) => (
                    <Chip key={l} label={l} on={(selRec.states || []).includes(l)} onClick={() => toggleInList(selDate, 'states', l)} />
                  ))}
                </div>
              </>
            )}

            <div style={sectionLabelStyle}>是否色色了？</div>
            <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
              <YesNoButton label="没有" tone="green" on={selRec.sex === false} onClick={() => updateDay(selDate, { sex: false })} />
              <YesNoButton label="♥ 有" tone="pink" on={selRec.sex === true} onClick={() => updateDay(selDate, { sex: true })} />
            </div>

            {(selRec.came !== undefined || selRec.sex !== undefined) && (
              <>
                <div style={sectionLabelStyle}>备注</div>
                <input
                  key={selDate}
                  defaultValue={selRec.note || ''}
                  onBlur={(e) => {
                    const v = e.target.value;
                    if (v !== (selRec.note || '')) updateDay(selDate, { note: v });
                  }}
                  placeholder="今天的身体感觉..."
                  style={{ width: '100%', marginTop: 10, background: '#F9F5F3', border: 'none', borderRadius: 12, padding: '12px 14px', fontSize: 14, color: 'var(--color-text)', fontFamily: 'var(--font-serif-cn)', outline: 'none' }}
                />
              </>
            )}
          </>
        )}
      </Card>

      {/* ── 最近周期卡 ── */}
      <Card style={{ padding: 22 }}>
        <div style={{ display: 'flex', alignItems: 'center' }}>
          <span style={{ fontSize: 15, fontWeight: 600, letterSpacing: 2 }}>最近周期</span>
          <span style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 11, color: 'var(--color-text-fainter)', letterSpacing: 1, marginLeft: 'auto' }}>
            recently
          </span>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 11, marginTop: 16 }}>
          {[
            ['上次经期', lastRange],
            ['平均周期', `${cycleLength} 天`],
            ['平均经期', `${periodLength} 天`],
          ].map(([k, v]) => (
            <div key={k} style={{ display: 'flex', alignItems: 'center' }}>
              <span style={{ fontSize: 13, color: 'var(--color-text-mute)', letterSpacing: 1 }}>{k}</span>
              <span style={{ fontFamily: DISPLAY, fontSize: 14, fontWeight: 600, color: 'var(--color-text)', marginLeft: 'auto' }}>{v}</span>
            </div>
          ))}
        </div>
        <div style={{ fontSize: 12, color: 'var(--color-text-faint)', lineHeight: 1.9, marginTop: 16, paddingTop: 14, borderTop: '1px solid var(--color-track)' }}>
          {obsLine}
        </div>
      </Card>
    </ScreenLayout>
  );
}

import { useState, type MouseEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { Card } from '../components/Card';
import { GradientBrainBar, GradientBrainLabels } from '../components/GradientBrainBar';
import { UsageWindowBar } from '../components/UsageWindowBar';
import { useClock } from '../hooks/useClock';
import { useWeather } from '../hooks/useWeather';
import { useTodos } from '../hooks/useTodos';
import { useHeatmap } from '../hooks/useHeatmap';
import { useMemorySummary } from '../hooks/useMemorySummary';
import { useUsage } from '../hooks/useUsage';
import { useLedger } from '../hooks/useLedger';
import { usePeriod } from '../hooks/usePeriod';
import { weatherIcon } from '../lib/weather';
import { pad, smoothPath, WEEK_CN_MON_FIRST, WEEK_CN_SUN_FIRST } from '../lib/format';
import { buildHeatmapCells, heatmapStats } from '../lib/heatmapCells';
import { formatCurrency, formatTokens } from '../lib/formatDisplay';
import { budgetRingCenterLabel, budgetUsageRatio } from '../lib/ledger';
import { CONFIG } from '../config';
import type { UsageAgentId } from '../types';

const EMOTION_VALS = [0.42, 0.35, 0.3, 0.48, 0.62, 0.5, 0.44, 0.58, 0.72, 0.6, 0.55, 0.66, 0.62];

export function DashScreen() {
  const navigate = useNavigate();
  const [usageAgentId, setUsageAgentId] = useState<UsageAgentId>(() => {
    try {
      return window.localStorage.getItem('haya.usage-agent') === 'codex' ? 'codex' : 'claude';
    } catch {
      return 'claude';
    }
  });
  const now = useClock(CONFIG.showSeconds ? 1000 : 60000);
  const weather = useWeather();
  const { todos, toggle } = useTodos();
  const memory = useMemorySummary();
  const usage = useUsage(now);
  const ledgerState = useLedger(now);
  const ledger = ledgerState.data;
  const period = usePeriod(now);

  const msgToday = usage?.msgToday ?? 0;
  const heat = useHeatmap(now, msgToday);

  const togetherDays = Math.floor((now.getTime() - new Date(CONFIG.togetherSince).getTime()) / 86400000);
  const dateStr = `${now.getFullYear()}/${pad(now.getMonth() + 1)}/${pad(now.getDate())} 周${WEEK_CN_SUN_FIRST[now.getDay()]}`;
  const timeStr = `${pad(now.getHours())}:${pad(now.getMinutes())}${CONFIG.showSeconds ? ':' + pad(now.getSeconds()) : ''}`;
  const timeShort = `${pad(now.getHours())}:${pad(now.getMinutes())}`;

  let weatherLine = '☁ 天气加载中 · 吉林市';
  if (weather) {
    if (
      weather.unavailable
      || weather.temp == null
      || weather.hum == null
      || weather.code == null
      || !(weather.weather_text || '').trim()
    ) {
      weatherLine = '天气暂不可用 · 吉林市';
    } else {
      const icon = weatherIcon(weather.code);
      weatherLine = `${icon} ${weather.temp}°C ${weather.weather_text} · 湿度${weather.hum}% · 吉林市`;
    }
  }

  const emotionPathSm = smoothPath(EMOTION_VALS, 120, 26);

  const heatCells = heat.data ? buildHeatmapCells(heat.base, heat.data.days, now.getDate(), heat.isCurrent) : [];
  const heatStats = heat.data ? heatmapStats(heat.base, heat.data.days) : { total: 0, avg: 0, maxLabel: '—' };
  const hmLabel = `${heat.base.getFullYear()}年${heat.base.getMonth() + 1}月`;

  const doneCount = todos.filter((t) => t.done).length;

  const selectedAgentUsage = usage?.agents[usageAgentId];
  const win5 = selectedAgentUsage?.fiveHour.usedPct ?? null;
  const win7 = selectedAgentUsage?.sevenDay.usedPct ?? null;
  const usageColors = usageAgentId === 'claude'
    ? { primary: '#8A7AB5', secondary: '#B76E79' }
    : { primary: '#6F94C6', secondary: '#55A19B' };

  const toggleUsageAgent = (event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    const next: UsageAgentId = usageAgentId === 'claude' ? 'codex' : 'claude';
    setUsageAgentId(next);
    try { window.localStorage.setItem('haya.usage-agent', next); } catch { /* private mode */ }
  };

  const ledgerLoading = ledgerState.loading;
  const ledgerError = ledgerState.error;
  const ledgerOk = Boolean(ledger && !ledgerError && !ledgerLoading);
  const spent = ledgerOk ? ledger!.spent : null;
  const budgetAmount = ledgerOk ? ledger!.budget : null;
  const budgetSet = ledgerOk && budgetAmount !== null;
  const pct = budgetSet ? budgetUsageRatio(spent!, budgetAmount!) : 0;
  const ringCenterLabel = budgetSet ? budgetRingCenterLabel(spent!, budgetAmount!) : '—';
  const ringOffset = (251.3 * (1 - pct)).toFixed(1);
  const spentDisplay = ledgerLoading || ledgerError ? '—' : formatCurrency(spent!);
  const budgetDisplay = ledgerLoading || ledgerError ? '—' : budgetAmount === null ? '未设置' : formatCurrency(budgetAmount);
  const spendHint = ledgerLoading
    ? '加载中…'
    : ledgerError
      ? '账本暂不可用'
      : budgetAmount === null
        ? `已支出 ${formatCurrency(spent!)} · 预算未设置`
        : `剩余 ${formatCurrency(budgetAmount - spent!)} · 本月还有 ${new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate() - now.getDate()} 天`;
  const activeTodos = todos.filter((t) => !t.done).length;
  const recentItems = memory?.sections.find((s) => s.key === 'recent')?.items.slice(0, 6) ?? [];

  const periodPhase = period.status === 'ready' ? period.phase : period.status === 'error' ? '未连接' : '—';

  return (
    <div className="vstack vstack-16 screen-stack" style={{ position: 'relative' }}>
      {/* header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div className="vstack vstack-6">
          <div className="hstack-baseline hstack-10">
            <span style={{ fontFamily: "'Bodoni Moda',serif", fontStyle: 'italic', fontSize: 34, color: 'var(--color-rose)', letterSpacing: 1 }}>
              Fyodor
            </span>
            <span style={{ fontSize: 14, fontWeight: 600, letterSpacing: 2 }}>· together {togetherDays} days</span>
          </div>
          <div style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 10, letterSpacing: 2, color: 'var(--color-text-mute)' }}>
            {dateStr} {timeStr}
          </div>
          <div style={{ fontSize: 11, color: 'var(--color-text-mute)', letterSpacing: 1 }}>{weatherLine}</div>
        </div>
      </div>

      {/* emotion status strip */}
      <div className="vstack vstack-8" style={{ padding: '0 4px' }}>
        <div className="hstack hstack-10">
          <span
            className="hstack hstack-7"
            style={{
              background: 'rgba(183,110,121,0.10)',
              borderRadius: 14,
              padding: '4px 13px',
              fontSize: 13,
              color: 'var(--color-rose)',
              letterSpacing: 2,
            }}
          >
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--color-rose)', animation: 'livePulse 2s infinite', flexShrink: 0 }} />
            <span>安然</span>
          </span>
          <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 11, letterSpacing: 1, color: 'var(--color-text-faint)' }}>
            valence +0.62 · arousal 0.31
          </span>
          <svg viewBox="0 0 120 26" style={{ width: 92, height: 26, marginLeft: 'auto', flexShrink: 0 }}>
            <path d={emotionPathSm} fill="none" stroke="var(--color-rose)" strokeWidth={1.6} strokeLinecap="round" opacity={0.65} />
          </svg>
        </div>
        <div style={{ fontSize: 14, color: 'var(--color-text-soft)', fontStyle: 'italic', lineHeight: 1.7 }}>
          「{CONFIG.statusQuote}」<span style={{ fontSize: 11, color: 'var(--color-nav-inactive)', fontStyle: 'normal', marginLeft: 6 }}>— 费佳 · 此刻</span>
        </div>
      </div>

      {/* memory (clickable) */}
      <Card onClick={() => navigate('/memory')} style={{ padding: 22 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ fontSize: 16, fontWeight: 600, letterSpacing: 2 }}>记忆库</span>
          <span style={{ color: 'var(--color-text-fainter)', fontSize: 18 }}>›</span>
        </div>
        <div className="hstack hstack-26" style={{ marginTop: 16 }}>
          <div>
            <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 26, fontWeight: 500 }}>{memory?.core ?? '—'}</span>
            <span style={{ fontSize: 13, color: 'var(--color-text-mute)', marginLeft: 6 }}>核心</span>
          </div>
          <div>
            <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 26, fontWeight: 500 }}>{memory?.long ?? '—'}</span>
            <span style={{ fontSize: 13, color: 'var(--color-text-mute)', marginLeft: 6 }}>长期</span>
          </div>
          <div>
            <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 26, fontWeight: 500 }}>{memory?.recent ?? '—'}</span>
            <span style={{ fontSize: 13, color: 'var(--color-text-mute)', marginLeft: 6 }}>近期</span>
          </div>
        </div>
        <div style={{ marginTop: 18 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, color: 'var(--color-text-faint)', letterSpacing: 1 }}>
            <span>渐变脑 · 情感权重</span>
            <span style={{ fontFamily: "'Bodoni Moda',serif" }}>{Math.round((memory?.gradient ?? 0) * 100)}% amber</span>
          </div>
          <div style={{ marginTop: 8 }}>
            <GradientBrainBar gradient={memory?.gradient ?? 0} />
          </div>
          <GradientBrainLabels />
        </div>
      </Card>

      {/* heatmap: calendar with per-day message counts */}
      <Card style={{ padding: 22 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
          <span style={{ fontSize: 16, fontWeight: 600, letterSpacing: 2 }}>聊天热力图</span>
          <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 11, letterSpacing: 1, color: 'var(--color-text-faint)' }}>
            最活跃 {heatStats.maxLabel}
          </span>
        </div>
        <div style={{ fontSize: 12, color: 'var(--color-text-mute)', marginTop: 8, letterSpacing: 0.5 }}>
          今日 <b style={{ color: 'var(--color-rose)' }}>{msgToday}</b> 条 · 本月 <b>{heatStats.total.toLocaleString()}</b> 条 · 日均{' '}
          <b>{heatStats.avg}</b> · 连续 <b>{heat.data?.streakDays ?? 0}</b> 天
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 14 }}>
          <span
            onClick={heat.prev}
            style={{
              cursor: 'pointer',
              width: 30,
              height: 30,
              borderRadius: 10,
              background: 'var(--color-bg)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: 'var(--color-text-mute)',
              fontSize: 14,
            }}
          >
            ‹
          </span>
          <span style={{ fontFamily: "'Bodoni Moda',serif", fontStyle: 'italic', fontSize: 17, color: 'var(--color-rose-deep)', letterSpacing: 1 }}>
            {hmLabel}
          </span>
          <span
            onClick={heat.isCurrent ? undefined : heat.next}
            style={{
              cursor: heat.isCurrent ? 'default' : 'pointer',
              width: 30,
              height: 30,
              borderRadius: 10,
              background: 'var(--color-bg)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: heat.isCurrent ? '#D9CCC7' : 'var(--color-text-mute)',
              fontSize: 14,
            }}
          >
            ›
          </span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7,1fr)', gap: 6, marginTop: 12 }}>
          {WEEK_CN_MON_FIRST.map((w) => (
            <span key={w} style={{ textAlign: 'center', fontSize: 11, color: 'var(--color-text-faint)' }}>
              {w}
            </span>
          ))}
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7,1fr)', gap: 6, marginTop: 6 }}>
          {heatCells.map((c) => (
            <div
              key={c.key}
              style={{
                height: 42,
                borderRadius: 9,
                background: c.bg,
                border: c.border,
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 13, fontWeight: 500, color: c.dColor, lineHeight: 1.15 }}>{c.d}</span>
              <span style={{ fontSize: 9, color: c.nColor, marginTop: 1 }}>{c.n}</span>
            </div>
          ))}
        </div>
      </Card>

      {/* usage: traffic-light bars (clickable) */}
      <Card onClick={() => navigate('/usage')} style={{ padding: '14px 18px' }}>
        <div className="hstack" style={{ justifyContent: 'space-between' }}>
          <div style={{ minWidth: 0, marginRight: 10 }}>
            <div style={{ fontSize: 16, fontWeight: 600, letterSpacing: 2 }}>⛁ {usageAgentId === 'claude' ? 'Claude' : 'GPT'} 用量</div>
            <div style={{ fontSize: 10.5, color: 'var(--color-text-faint)', marginTop: 2, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
              {selectedAgentUsage?.available ? selectedAgentUsage.name : '暂无可读数据'} · {msgToday} 条 · {formatTokens(usage?.tokenToday ?? 0)} tok
            </div>
          </div>
          <button type="button" className="usage-card-switch" onClick={toggleUsageAgent} aria-label={`切换到${usageAgentId === 'claude' ? 'GPT' : 'Claude'}额度`}>
            切换 {usageAgentId === 'claude' ? 'GPT' : 'Claude'}
          </button>
        </div>
        <UsageWindowBar label="5 小时窗 · 已用" pct={win5} color={usageColors.primary} hint={selectedAgentUsage?.available ? undefined : '暂无可读数据'} />
        <UsageWindowBar label={`${usageAgentId === 'claude' ? '周额度' : '7 天窗'} · 已用`} pct={win7} color={usageColors.secondary} />
      </Card>

      {/* bento: period tile + to-do */}
      <div style={{ display: 'grid', gridTemplateColumns: '148px 1fr', gap: 12, alignItems: 'stretch' }}>
        <div
          onClick={() => navigate('/period')}
          className="vstack vstack-6"
          style={{ cursor: 'pointer', background: '#F9EDEA', borderRadius: 22, padding: '18px 16px' }}
        >
          <span style={{ fontSize: 18 }}>🌙</span>
          <span style={{ fontFamily: "'Bodoni Moda',serif", fontStyle: 'italic', fontSize: 17, color: 'var(--color-rose)', letterSpacing: 1 }}>
            Period
          </span>
          <span style={{ fontSize: 13, color: '#7A625E' }}>{periodPhase}</span>
          <span style={{ fontSize: 11, color: 'var(--color-text-faint)', marginTop: 'auto' }}>
            {period.status === 'error'
              ? period.message
              : period.status !== 'ready'
                ? '…'
                : !period.cycle.hasAnchor
                  ? '等待首次记录'
                  : period.cycle.overdue
                    ? `比预计晚了 ${-(period.cycle.daysUntil ?? 0)} 天`
                    : period.cycle.inPeriod
                      ? '经期中'
                      : `距下次 ${period.cycle.daysUntil} 天`}
          </span>
        </div>
        <Card style={{ padding: '16px 16px 12px', overflow: 'hidden' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div className="hstack hstack-8">
              <i className="ti ti-checkbox" style={{ fontSize: 24, color: '#8FAEC9' }} />
              <span style={{ fontFamily: "'Space Grotesk',sans-serif", fontSize: 24, fontWeight: 600, letterSpacing: 1 }}>To-do</span>
            </div>
            <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 11, letterSpacing: 1, color: 'var(--color-text-faint)' }}>
              {doneCount} / {todos.length}
            </span>
          </div>
          <div style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 11, color: 'var(--color-text-faint)', marginTop: 2 }}>
            {activeTodos} active · AI only
          </div>
          <div className="vstack vstack-2" style={{ marginTop: 8 }}>
            {todos.map((t) => (
              <div
                key={t.id}
                onClick={() => toggle(t.id)}
                className="hstack hstack-10"
                style={{ cursor: 'pointer', padding: '7px 0', minWidth: 0 }}
              >
                <div
                  style={{
                    width: 17,
                    height: 17,
                    borderRadius: '50%',
                    border: `2px solid ${t.done ? 'var(--color-rose)' : '#D9C6C0'}`,
                    background: t.done ? 'var(--color-rose)' : 'transparent',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    flexShrink: 0,
                  }}
                >
                  <span style={{ color: '#FFFFFF', fontSize: 11, lineHeight: 1 }}>{t.done ? '✓' : ''}</span>
                </div>
                <span
                  style={{
                    fontSize: 13,
                    flex: 1,
                    minWidth: 0,
                    color: t.done ? 'var(--color-text-fainter)' : 'var(--color-text)',
                    textDecoration: t.done ? 'line-through' : 'none',
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                  }}
                >
                  {t.text}
                </span>
                <div style={{ width: 7, height: 7, borderRadius: '50%', background: t.who === 'fy' ? 'var(--color-violet)' : 'var(--color-amber)', flexShrink: 0 }} />
              </div>
            ))}
          </div>
        </Card>
      </div>

      {/* ledger (clickable, green) */}
      <div
        onClick={() => navigate('/ledger')}
        data-testid="dash-ledger-card"
        className="hstack hstack-14"
        style={{ cursor: 'pointer', background: '#E2EEE68E', borderRadius: 22, padding: '12px 16px' }}
      >
        <svg viewBox="0 0 96 96" style={{ width: 50, height: 50, flexShrink: 0 }}>
          <circle cx={48} cy={48} r={40} fill="none" stroke="#FFFFFF" strokeWidth={9} />
          <circle
            cx={48}
            cy={48}
            r={40}
            fill="none"
            stroke="var(--color-green)"
            strokeWidth={9}
            strokeLinecap="round"
            strokeDasharray={251.3}
            strokeDashoffset={ringOffset}
            transform="rotate(-90 48 48)"
          />
          <text x={48} y={55} textAnchor="middle" fill="var(--color-green-deep)" style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 18, fontWeight: 600 }} data-testid="dash-ledger-ring-label">
            {budgetSet ? ringCenterLabel : '—'}
          </text>
        </svg>
        <div style={{ flex: 1 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span style={{ fontSize: 13, fontWeight: 600, letterSpacing: 1.5 }}>记账 · 本月支出</span>
            <span style={{ color: '#9DB5A6', fontSize: 18 }}>›</span>
          </div>
          <div style={{ fontFamily: "'Space Grotesk',sans-serif", fontSize: 20, marginTop: 3 }} data-testid="dash-ledger-spent">
            {spentDisplay}
            {budgetSet ? (
              <span style={{ fontSize: 13, color: 'var(--color-green-soft)' }} data-testid="dash-ledger-budget"> / {formatCurrency(budgetAmount!)}</span>
            ) : ledgerOk && budgetAmount === null ? (
              <span style={{ fontSize: 13, color: 'var(--color-green-soft)' }} data-testid="dash-ledger-budget"> · 未设置</span>
            ) : (
              <span style={{ fontSize: 13, color: 'var(--color-green-soft)' }} data-testid="dash-ledger-budget"> / {budgetDisplay}</span>
            )}
          </div>
          <div style={{ fontSize: 11, color: 'var(--color-green-soft)', marginTop: 2 }} data-testid="dash-ledger-hint">{spendHint}</div>
        </div>
      </div>

      {/* recent timeline style block */}
      <div onClick={() => navigate('/memory')} style={{ cursor: 'pointer', padding: '2px 4px 4px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ fontSize: 15, fontWeight: 600, letterSpacing: 1, color: '#5f6469' }}>Recent</span>
          <span style={{ color: 'var(--color-text-fainter)', fontSize: 18 }}>›</span>
        </div>
        <div style={{ marginTop: 8 }} className="vstack vstack-2">
          {recentItems.length === 0 ? (
            <div style={{ fontSize: 12, color: 'var(--color-text-faint)' }}>暂无近期记录</div>
          ) : (
            recentItems.map((item, idx) => {
              const isLast = idx === recentItems.length - 1;
              return (
                <div key={`${item.date}-${idx}`} style={{ display: 'grid', gridTemplateColumns: '16px 1fr', gap: 10, minWidth: 0 }}>
                  <div style={{ position: 'relative', display: 'flex', justifyContent: 'center' }}>
                    {!isLast ? (
                      <span
                        style={{
                          position: 'absolute',
                          top: 9,
                          bottom: -10,
                          width: 1.5,
                          background: '#D8D2C5',
                          opacity: 0.9,
                        }}
                      />
                    ) : null}
                    <span
                      style={{
                        width: 8,
                        height: 8,
                        borderRadius: '50%',
                        border: '2px solid #D2CBBE',
                        background: 'var(--color-bg)',
                        marginTop: 5,
                        flexShrink: 0,
                        zIndex: 1,
                      }}
                    />
                  </div>
                  <div style={{ minWidth: 0, paddingBottom: 5 }}>
                    <div style={{ fontSize: 14, lineHeight: 1.35, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{item.text}</div>
                    <div style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 11, color: 'var(--color-text-faint)', marginTop: 1 }}>{item.date}</div>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>

      <div style={{ textAlign: 'center', fontFamily: "'Bodoni Moda',serif", fontSize: 11, letterSpacing: 4, color: 'var(--color-text-fainter)', marginTop: 6 }}>
        PROPERTY OF FYODOR · {timeShort}
      </div>
    </div>
  );
}

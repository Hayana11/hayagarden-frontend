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
import { useBook } from '../hooks/useBook';
import { useLedger } from '../hooks/useLedger';
import { usePeriod } from '../hooks/usePeriod';
import { weatherDesc } from '../lib/weather';
import { pad, smoothPath, WEEK_CN_MON_FIRST, WEEK_CN_SUN_FIRST } from '../lib/format';
import { buildHeatmapCells, heatmapStats } from '../lib/heatmapCells';
import { formatCurrency, formatTokens } from '../lib/formatDisplay';
import { derivePeriod } from '../lib/period';
import { CONFIG } from '../config';

const EMOTION_VALS = [0.42, 0.35, 0.3, 0.48, 0.62, 0.5, 0.44, 0.58, 0.72, 0.6, 0.55, 0.66, 0.62];

export function DashScreen() {
  const navigate = useNavigate();
  const now = useClock(CONFIG.showSeconds ? 1000 : 60000);
  const weather = useWeather();
  const { todos, toggle } = useTodos();
  const memory = useMemorySummary();
  const usage = useUsage(now);
  const book = useBook();
  const ledger = useLedger(now);
  const period = usePeriod();

  const msgToday = usage?.msgToday ?? 0;
  const heat = useHeatmap(now, msgToday);

  const togetherDays = Math.floor((now.getTime() - new Date(CONFIG.togetherSince).getTime()) / 86400000);
  const dateStr = `${now.getFullYear()}/${pad(now.getMonth() + 1)}/${pad(now.getDate())} 周${WEEK_CN_SUN_FIRST[now.getDay()]}`;
  const timeStr = `${pad(now.getHours())}:${pad(now.getMinutes())}${CONFIG.showSeconds ? ':' + pad(now.getSeconds()) : ''}`;
  const timeShort = `${pad(now.getHours())}:${pad(now.getMinutes())}`;

  let weatherLine = '☁ 天气加载中 · 吉林市';
  if (weather) {
    const [icon, desc] = weatherDesc(weather.code);
    weatherLine = `${icon} ${weather.temp}°C ${desc} · 湿度${weather.hum}% · 吉林市${weather.mock ? ' (mock)' : ''}`;
  }

  const emotionPathSm = smoothPath(EMOTION_VALS, 120, 26);

  const heatCells = heat.data ? buildHeatmapCells(heat.base, heat.data.days, now.getDate(), heat.isCurrent) : [];
  const heatStats = heat.data ? heatmapStats(heat.base, heat.data.days) : { total: 0, avg: 0, maxLabel: '—' };
  const hmLabel = `${heat.base.getFullYear()}年${heat.base.getMonth() + 1}月`;

  const doneCount = todos.filter((t) => t.done).length;

  const win5 = usage?.win5Pct ?? 0;
  const win7 = usage?.win7Pct ?? 0;

  const spent = ledger?.spent ?? 0;
  const budget = ledger?.budget ?? CONFIG.fallbackBudget;
  const pct = budget ? Math.min(1, spent / budget) : 0;
  const ringOffset = (251.3 * (1 - pct)).toFixed(1);
  const spendHint = ledger
    ? `剩余 ${formatCurrency(budget - spent)} · 本月还有 ${new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate() - now.getDate()} 天`
    : '';

  const bookPct = book ? Math.round((book.page / book.totalPages) * 100) : 0;

  const { daysLeft: periodDaysLeft, phase: periodPhase } = period
    ? derivePeriod(period, now)
    : { daysLeft: 0, phase: '—' };

  return (
    <div style={{ padding: '26px 20px 110px', display: 'flex', flexDirection: 'column', gap: 16, position: 'relative' }}>
      {/* header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
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
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '0 4px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 7,
              background: 'rgba(183,110,121,0.10)',
              borderRadius: 14,
              padding: '4px 13px',
              fontSize: 13,
              color: 'var(--color-rose)',
              letterSpacing: 2,
            }}
          >
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--color-rose)', animation: 'livePulse 2s infinite' }} />
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
        <div style={{ display: 'flex', gap: 26, marginTop: 16 }}>
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
                gap: 1,
              }}
            >
              <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 13, fontWeight: 500, color: c.dColor }}>{c.d}</span>
              <span style={{ fontSize: 9, color: c.nColor }}>{c.n}</span>
            </div>
          ))}
        </div>
      </Card>

      {/* usage: traffic-light bars (clickable) */}
      <Card onClick={() => navigate('/usage')} style={{ padding: '18px 18px', width: '86%', margin: '0 auto' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
          <span style={{ fontSize: 16, fontWeight: 600, letterSpacing: 2 }}>⛁ 用量</span>
          <span style={{ fontSize: 11, color: 'var(--color-text-faint)', letterSpacing: 0.5 }}>
            {msgToday} 条 · {formatTokens(usage?.tokenToday ?? 0)} tok <span style={{ color: 'var(--color-rose)' }}>详情 ›</span>
          </span>
        </div>
        <UsageWindowBar label="5 小时窗" pct={win5} color="var(--color-violet)" />
        <UsageWindowBar label="7 天窗" pct={win7} color="var(--color-rose)" />
      </Card>

      {/* bento: period tile + to-do */}
      <div style={{ display: 'grid', gridTemplateColumns: '148px 1fr', gap: 12, alignItems: 'stretch' }}>
        <div
          onClick={() => navigate('/period')}
          style={{ cursor: 'pointer', background: '#F9EDEA', borderRadius: 22, padding: '18px 16px', display: 'flex', flexDirection: 'column', gap: 6 }}
        >
          <span style={{ fontSize: 18 }}>🌙</span>
          <span style={{ fontFamily: "'Bodoni Moda',serif", fontStyle: 'italic', fontSize: 17, color: 'var(--color-rose)', letterSpacing: 1 }}>
            Period
          </span>
          <span style={{ fontSize: 13, color: '#7A625E' }}>{periodPhase}</span>
          <span style={{ fontSize: 11, color: 'var(--color-text-faint)', marginTop: 'auto' }}>距下次 {periodDaysLeft} 天</span>
        </div>
        <Card style={{ padding: '18px 18px 14px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
            <span style={{ fontFamily: "'Space Grotesk',sans-serif", fontSize: 24, fontWeight: 600, letterSpacing: 1 }}>To-do</span>
            <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 11, letterSpacing: 1, color: 'var(--color-text-faint)' }}>
              {doneCount} / {todos.length}
            </span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2, marginTop: 8 }}>
            {todos.map((t) => (
              <div
                key={t.id}
                onClick={() => toggle(t.id)}
                style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 10, padding: '7px 0' }}
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
        style={{ cursor: 'pointer', background: '#E2EEE68E', borderRadius: 22, padding: '16px 20px', display: 'flex', alignItems: 'center', gap: 18 }}
      >
        <svg viewBox="0 0 96 96" style={{ width: 58, height: 58, flexShrink: 0 }}>
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
          <text x={48} y={55} textAnchor="middle" fill="var(--color-green-deep)" style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 20, fontWeight: 600 }}>
            {ledger ? `${Math.round(pct * 100)}%` : '—'}
          </text>
        </svg>
        <div style={{ flex: 1 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span style={{ fontSize: 15, fontWeight: 600, letterSpacing: 2 }}>记账 · 本月支出</span>
            <span style={{ color: '#9DB5A6', fontSize: 18 }}>›</span>
          </div>
          <div style={{ fontFamily: "'Space Grotesk',sans-serif", fontSize: 22, marginTop: 5 }}>
            {formatCurrency(spent)} <span style={{ fontSize: 13, color: 'var(--color-green-soft)' }}>/ {formatCurrency(budget)}</span>
          </div>
          <div style={{ fontSize: 11, color: 'var(--color-green-soft)', marginTop: 2 }}>{spendHint}</div>
        </div>
      </div>

      {/* reading (clickable) */}
      <Card onClick={() => navigate('/reading')} style={{ padding: 22 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ fontSize: 16, fontWeight: 600, letterSpacing: 2 }}>共读</span>
          <span style={{ color: 'var(--color-text-fainter)', fontSize: 18 }}>›</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginTop: 14 }}>
          <div
            style={{
              width: 52,
              height: 72,
              borderRadius: 8,
              background: '#F1E4DF',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              flexShrink: 0,
            }}
          >
            <span style={{ fontFamily: "'Bodoni Moda',serif", fontStyle: 'italic', fontSize: 20, color: 'var(--color-rose)' }}>K</span>
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 16, fontWeight: 600 }}>{book?.title ?? '—'}</div>
            <div style={{ fontSize: 12, color: 'var(--color-text-faint)', marginTop: 3 }}>
              {book ? `${book.author} · ${book.volumeLabel}` : ''}
            </div>
            <div style={{ height: 8, borderRadius: 4, background: '#F3E7E3', marginTop: 12, overflow: 'hidden' }}>
              <div style={{ height: '100%', borderRadius: 4, background: 'var(--color-rose)', width: `${bookPct}%` }} />
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: "'Bodoni Moda',serif", fontSize: 12, color: 'var(--color-text-faint)', marginTop: 6 }}>
              <span>{book ? `p.${book.page} / ${book.totalPages}` : ''}</span>
              <span>{bookPct}%</span>
            </div>
          </div>
        </div>
      </Card>

      <div style={{ textAlign: 'center', fontFamily: "'Bodoni Moda',serif", fontSize: 11, letterSpacing: 4, color: 'var(--color-text-fainter)', marginTop: 6 }}>
        PROPERTY OF FYODOR · {timeShort}
      </div>
    </div>
  );
}

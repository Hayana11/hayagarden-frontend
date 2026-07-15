import { useEffect, useMemo, useState } from 'react';
import { BackHeader } from '../components/BackHeader';
import { Card, ScreenLayout } from '../components/Card';
import { UsageWindowBar } from '../components/UsageWindowBar';
import { useUsage } from '../hooks/useUsage';
import { getDailyUsage, type DailyUsage, type DailyUsageResult } from '../lib/systemConfig';
import { formatResetHint, formatTokens } from '../lib/formatDisplay';
import type { AgentUsageSummary } from '../types';

const AGENT_COLORS = {
  claude: { primary: '#8A7AB5', secondary: '#B76E79' },
  codex: { primary: '#6F94C6', secondary: '#55A19B' },
} as const;

function shortDate(value: string): string {
  return `${Number(value.slice(5, 7))}-${Number(value.slice(8, 10))}`;
}

function fmtCost(value: number): string {
  if (value <= 0) return '¥0';
  if (value >= 100) return `¥${value.toFixed(1)}`;
  return `¥${value.toFixed(2)}`;
}

function dailyMetric(item: DailyUsage, mode: DailyUsageResult['mode']): number {
  if (mode === 'cost') return item.cost ?? 0;
  return item.count;
}

function sourceLabel(source: string): string {
  if (source === 'claude_legacy_usage') return 'Claude 原有数据';
  if (source === 'claude_oauth_usage') return 'Claude 官方账号额度';
  if (source === 'codex_oauth_usage') return 'Codex 官方账号额度';
  if (source === 'ccusage_blocks') return 'ccusage active block';
  if (source === 'codex_session_jsonl') return 'Codex session';
  if (source === 'unavailable') return '尚未接入';
  return source || '尚未接入';
}

function updatedLabel(value: string): string {
  if (!value) return '暂无更新时间';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '更新时间未知';
  return `更新 ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}

function windowHint(resetAt: string, remainingMinutes: number | null, now: Date): string {
  const parts: string[] = [];
  if (remainingMinutes !== null) parts.push(`剩余 ${Math.max(0, Math.round(remainingMinutes))} 分钟`);
  if (resetAt) parts.push(formatResetHint(resetAt, now));
  return parts.join(' · ');
}

function AgentQuotaCard({ agent, now }: { agent: AgentUsageSummary; now: Date }) {
  const colors = AGENT_COLORS[agent.id];
  const isClaude = agent.id === 'claude';
  const context = agent.contextTokens === null
    ? ''
    : agent.contextWindowTokens
      ? `上下文 ${formatTokens(agent.contextTokens)} / ${formatTokens(agent.contextWindowTokens)}`
      : `上下文 ${formatTokens(agent.contextTokens)}`;

  return (
    <Card style={{ padding: 22, border: `1px solid ${agent.available ? `${colors.primary}20` : '#F0E7E3'}` }}>
      <div className="usage-agent-heading">
        <span className="usage-agent-dot" style={{ backgroundColor: agent.available ? colors.primary : '#D8CCC7' }} />
        <div>
          <strong>{agent.name} 额度</strong>
          <small>{isClaude ? '官方订阅 · Claude Code' : '官方订阅 · GPT / Codex'}</small>
        </div>
        <em className={agent.available ? 'ready' : ''}>{agent.available ? '有数据' : '暂无数据'}</em>
      </div>

      {agent.effectiveLimit?.exhausted && (
        <div className="usage-limit-alert">
          <strong>当前额度已耗尽</strong>
          <span>{agent.effectiveLimit.resetText || '等待下一次额度窗口恢复'}</span>
        </div>
      )}

      {agent.available ? (
        <>
          <UsageWindowBar
            big
            label="5 小时窗 · 已用"
            pct={agent.fiveHour.usedPct}
            color={colors.primary}
            hint={windowHint(agent.fiveHour.resetAt, agent.fiveHour.remainingMinutes, now)}
          />
          <UsageWindowBar
            big
            label={`${isClaude ? '周额度' : '7 天窗'} · 已用`}
            pct={agent.sevenDay.usedPct}
            color={colors.secondary}
            hint={windowHint(agent.sevenDay.resetAt, agent.sevenDay.remainingMinutes, now)}
          />
        </>
      ) : (
        <div className="usage-agent-empty">
          <span>暂无可读数据</span>
          <small>{isClaude ? '等待 Claude Code 用量来源' : '等待 Codex session 采集器上报'}</small>
        </div>
      )}

      <div className="usage-agent-meta">
        <span>source · {sourceLabel(agent.source)}</span>
        <span>{context || updatedLabel(agent.updatedAt)}</span>
      </div>
      {context && <div className="usage-agent-updated">{updatedLabel(agent.updatedAt)}</div>}
    </Card>
  );
}

export function UsageScreen() {
  const now = new Date();
  const usage = useUsage(now);
  const [dailyUsage, setDailyUsage] = useState<DailyUsageResult>({
    mode: 'requests',
    days: [],
    relays: [],
    totalCost: null,
    totalCount: 0,
  });
  const [range, setRange] = useState<7 | 30>(30);
  const [selectedDay, setSelectedDay] = useState('');

  useEffect(() => {
    let alive = true;
    getDailyUsage(30)
      .then((result) => {
        if (!alive) return;
        setDailyUsage(result);
        setSelectedDay((current) => current || result.days.at(-1)?.date || '');
      })
      .catch(() => {
        if (alive) setDailyUsage((current) => ({ ...current, days: [] }));
      });
    return () => { alive = false; };
  }, []);

  const daily = dailyUsage.days;
  const dailyMode = dailyUsage.mode;
  const shownDaily = useMemo(() => range === 7 ? daily.slice(-7) : daily, [daily, range]);
  const selectedUsage = shownDaily.find((item) => item.date === selectedDay) || shownDaily.at(-1);
  const maxDaily = Math.max(1, ...shownDaily.map((item) => dailyMetric(item, dailyMode)));
  const totalRequests = shownDaily.reduce((sum, item) => sum + item.count, 0);

  function dailyCellLabel(item: DailyUsage): string {
    if (dailyMode === 'cost' && item.cost != null) return fmtCost(item.cost);
    return String(item.count);
  }

  return (
    <ScreenLayout>
      <BackHeader title="用量" />

      <div className="usage-page-intro">
        <span>QUOTA · 官方额度</span>
        <small>Claude Code 与 Codex 独立读取，不互相借用数据</small>
      </div>

      {usage ? (
        <>
          <AgentQuotaCard agent={usage.agents.claude} now={now} />
          <AgentQuotaCard agent={usage.agents.codex} now={now} />
        </>
      ) : (
        <Card style={{ padding: 22 }}><div className="usage-agent-empty"><span>正在读取额度…</span></div></Card>
      )}

      <Card style={{ padding: 22 }}>
        <div style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 13, letterSpacing: 5, color: 'var(--color-text-faint)' }}>TODAY</div>
        <div style={{ display: 'flex', gap: 32, marginTop: 14 }}>
          <div>
            <div style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 36, fontWeight: 500 }}>{usage?.msgToday ?? '—'}</div>
            <div style={{ fontSize: 13, color: 'var(--color-text-mute)', letterSpacing: 2, marginTop: 4 }}>消息数</div>
          </div>
          <div>
            <div style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 36, fontWeight: 500 }}>{usage ? formatTokens(usage.tokenToday) : '—'}</div>
            <div style={{ fontSize: 13, color: 'var(--color-text-mute)', letterSpacing: 2, marginTop: 4 }}>tokens</div>
          </div>
        </div>
      </Card>

      <Card style={{ padding: 22 }}>
        <div className="config-card-heading"><h2>用量日历</h2><span>按天 · 对话请求</span></div>
        <div className="config-segmented">
          <button type="button" className={range === 7 ? 'active' : ''} onClick={() => { setRange(7); setSelectedDay(daily.at(-1)?.date || ''); }}>一周</button>
          <button type="button" className={range === 30 ? 'active' : ''} onClick={() => { setRange(30); setSelectedDay(daily.at(-1)?.date || ''); }}>一个月</button>
        </div>
        {range === 7 ? (
          <div className="config-week-bars">
            {shownDaily.map((item) => {
              const metric = dailyMetric(item, dailyMode);
              return <button type="button" key={item.date} onClick={() => setSelectedDay(item.date)}><span>{dailyCellLabel(item)}</span><i className={selectedUsage?.date === item.date ? 'selected' : ''} style={{ height: `${Math.max(8, Math.round((metric / maxDaily) * 118))}px` }} /><small>{shortDate(item.date)}</small></button>;
            })}
          </div>
        ) : (
          <div className="config-month-grid">
            {shownDaily.map((item) => {
              const metric = dailyMetric(item, dailyMode);
              return <button type="button" key={item.date} onClick={() => setSelectedDay(item.date)}><span className={selectedUsage?.date === item.date ? 'selected' : ''}><i style={{ height: `${Math.max(5, Math.round((metric / maxDaily) * 100))}%` }} /><em>{dailyCellLabel(item)}</em></span><small>{shortDate(item.date)}</small></button>;
            })}
          </div>
        )}
        <div className="config-day-detail">{selectedUsage ? `${shortDate(selectedUsage.date)} · ${selectedUsage.count} 次请求` : '暂无用量数据'}</div>
        <div className="config-usage-summary"><span>合计 <b>{totalRequests}</b> 次请求</span><span>今日 <b>{usage?.msgToday ?? 0}</b> 条消息</span><span>约 <b>{formatTokens(usage?.tokenToday ?? 0)}</b> token</span></div>
      </Card>

      <div style={{ fontSize: 12, color: 'var(--color-text-fainter)', textAlign: 'center', letterSpacing: 1 }}>
        额度接口 · /api/context-usage · 页面每 15 秒刷新
      </div>
    </ScreenLayout>
  );
}

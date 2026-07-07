import { BackHeader } from '../components/BackHeader';
import { Card, ScreenLayout } from '../components/Card';
import { UsageWindowBar } from '../components/UsageWindowBar';
import { useUsage } from '../hooks/useUsage';
import { WEEK_CN_SUN_FIRST } from '../lib/format';
import { formatResetHint, formatTokens } from '../lib/formatDisplay';

function scaleUsageBars(bars: { fy: number; haya: number }[], maxPx: number) {
  const peak = Math.max(...bars.map((b) => b.fy + b.haya), 1);
  return bars.map((b) => ({
    fy: Math.max(2, Math.round((b.fy / peak) * maxPx)),
    haya: Math.max(2, Math.round((b.haya / peak) * maxPx)),
  }));
}

export function UsageScreen() {
  const now = new Date();
  const usage = useUsage(now);
  const scaledBars = scaleUsageBars(usage?.bars ?? [], 110);

  return (
    <ScreenLayout>
      <BackHeader title="用量" />

      <Card style={{ padding: 22 }}>
        <div style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 13, letterSpacing: 5, color: 'var(--color-text-faint)' }}>WINDOWS</div>
        <UsageWindowBar
          big
          label="5 小时窗"
          pct={usage?.win5Pct ?? 0}
          color="var(--color-violet)"
          hint={usage ? formatResetHint(usage.win5ResetAt, now) : ''}
        />
        <UsageWindowBar
          big
          label="7 天窗"
          pct={usage?.win7Pct ?? 0}
          color="var(--color-rose)"
          hint={usage ? formatResetHint(usage.win7ResetAt, now) : ''}
        />
      </Card>

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
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
          <span style={{ fontSize: 15, fontWeight: 600, letterSpacing: 2 }}>近 7 天 tokens</span>
          <span style={{ display: 'flex', gap: 12, fontSize: 11, color: 'var(--color-text-faint)' }}>
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
              <span style={{ width: 8, height: 8, borderRadius: 2, background: 'var(--color-violet)' }} />
              费佳
            </span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
              <span style={{ width: 8, height: 8, borderRadius: 2, background: 'var(--color-amber)' }} />
              哈娅
            </span>
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'flex-end', gap: 14, height: 140, marginTop: 18, padding: '0 4px', overflow: 'hidden' }}>
          {(usage?.bars ?? []).map((b, i) => {
            const label = `周${WEEK_CN_SUN_FIRST[new Date(b.date).getDay()]}`;
            const bar = scaledBars[i] ?? { fy: 0, haya: 0 };
            return (
              <div key={b.date} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6, minWidth: 0 }}>
                <div style={{ width: '100%', maxWidth: 26, height: 110, display: 'flex', flexDirection: 'column', justifyContent: 'flex-end' }}>
                  <div style={{ height: bar.fy, background: 'var(--color-violet)', borderRadius: '5px 5px 0 0' }} />
                  <div style={{ height: bar.haya, background: 'var(--color-amber)', borderRadius: '0 0 5px 5px' }} />
                </div>
                <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 11, color: 'var(--color-text-faint)' }}>{label}</span>
              </div>
            );
          })}
        </div>
      </Card>

      <div style={{ fontSize: 12, color: 'var(--color-text-fainter)', textAlign: 'center', letterSpacing: 1 }}>
        数据来源：今日消息数 + SSE usage 字段 · 接口位：/api/usage
      </div>
    </ScreenLayout>
  );
}

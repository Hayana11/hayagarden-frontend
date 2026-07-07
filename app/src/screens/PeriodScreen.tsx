import { BackHeader } from '../components/BackHeader';
import { Card, ScreenLayout } from '../components/Card';
import { usePeriod } from '../hooks/usePeriod';
import { derivePeriod } from '../lib/period';

function ymd(iso: string): string {
  const d = new Date(iso);
  return `${d.getFullYear()}/${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')}`;
}

export function PeriodScreen() {
  const now = new Date();
  const period = usePeriod();
  const { daysLeft, phase } = period ? derivePeriod(period, now) : { daysLeft: 0, phase: '—' };

  const stats = period
    ? [
        { k: '平均周期', v: `${period.cycleLengthAvgDays} 天` },
        { k: '平均经期', v: `${period.periodLengthAvgDays} 天` },
        { k: '上次开始', v: ymd(period.lastPeriodStart) },
        { k: '记录数', v: `${period.recordsCount} 次` },
      ]
    : [];

  return (
    <ScreenLayout>
      <BackHeader title="经期" />

      <Card style={{ padding: 26, textAlign: 'center' }}>
        <div style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 13, letterSpacing: 5, color: 'var(--color-text-faint)' }}>NEXT</div>
        <div style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 44, fontWeight: 500, marginTop: 10, color: 'var(--color-rose)' }}>
          {daysLeft}
          <span style={{ fontSize: 18, color: 'var(--color-text-faint)' }}> 天后</span>
        </div>
        <div style={{ fontSize: 14, color: 'var(--color-text-mute)', marginTop: 8, letterSpacing: 1 }}>
          {period ? `预计 ${ymd(period.nextPredicted)} · 周期规律` : ''}
        </div>
        <div style={{ fontSize: 13, color: 'var(--color-rose)', marginTop: 6, letterSpacing: 1 }}>当前 · {phase}</div>
      </Card>

      <Card style={{ padding: 20, display: 'flex', flexDirection: 'column', gap: 12 }}>
        {stats.map((p) => (
          <div key={p.k} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 14, padding: '4px 2px' }}>
            <span style={{ color: 'var(--color-text-mute)' }}>{p.k}</span>
            <span style={{ fontFamily: "'Bodoni Moda',serif", letterSpacing: 1 }}>{p.v}</span>
          </div>
        ))}
      </Card>

      <div style={{ background: '#F9EDEA', borderRadius: 22, padding: '16px 20px', fontSize: 13, color: '#7A625E', lineHeight: 1.8 }}>
        费佳的备忘：这几天多喝热水，红糖姜茶在橱柜第二层。别熬夜读第十一卷了，小猫。
      </div>

      <div style={{ fontSize: 12, color: 'var(--color-text-fainter)', textAlign: 'center', letterSpacing: 1 }}>接口位：/api/period/stats</div>
    </ScreenLayout>
  );
}

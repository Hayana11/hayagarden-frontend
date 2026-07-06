import { BackHeader } from '../components/BackHeader';
import { Card, ScreenLayout } from '../components/Card';
import { useLedger } from '../hooks/useLedger';
import { formatCurrency } from '../lib/formatDisplay';
import { CONFIG } from '../config';

export function LedgerScreen() {
  const now = new Date();
  const ledger = useLedger(now);

  const spent = ledger?.spent ?? 0;
  const budget = ledger?.budget ?? CONFIG.fallbackBudget;
  const pct = budget ? Math.min(1, spent / budget) : 0;
  const ringOffsetBig = (364.4 * (1 - pct)).toFixed(1);
  const daysLeftInMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate() - now.getDate();
  const maxAmount = ledger ? Math.max(...ledger.categories.map((c) => c.amount), 1) : 1;

  return (
    <ScreenLayout>
      <BackHeader title="本月支出" />

      <Card style={{ padding: 26, display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
        <svg viewBox="0 0 140 140" style={{ width: 150, height: 150 }}>
          <circle cx={70} cy={70} r={58} fill="none" stroke="#E4EDE7" strokeWidth={12} />
          <circle
            cx={70}
            cy={70}
            r={58}
            fill="none"
            stroke="var(--color-green)"
            strokeWidth={12}
            strokeLinecap="round"
            strokeDasharray={364.4}
            strokeDashoffset={ringOffsetBig}
            transform="rotate(-90 70 70)"
          />
          <text x={70} y={70} textAnchor="middle" fill="var(--color-text)" style={{ fontFamily: "'Space Grotesk',sans-serif", fontSize: 22, fontWeight: 600 }}>
            {formatCurrency(spent)}
          </text>
          <text x={70} y={90} textAnchor="middle" fill="var(--color-text-faint)" style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 12 }}>
            / {formatCurrency(budget)}
          </text>
        </svg>
        <div style={{ fontSize: 13, color: 'var(--color-text-mute)', marginTop: 8, letterSpacing: 1 }}>
          {ledger ? `剩余 ${formatCurrency(budget - spent)} · 本月还有 ${daysLeftInMonth} 天` : ''}
        </div>
      </Card>

      <Card style={{ padding: 20 }}>
        <div style={{ fontSize: 15, fontWeight: 600, letterSpacing: 2 }}>分类</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14, marginTop: 14 }}>
          {ledger?.categories.map((lc) => (
            <div key={lc.name}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 14 }}>
                <span>{lc.name}</span>
                <span style={{ fontFamily: "'Space Grotesk',sans-serif" }}>{formatCurrency(lc.amount)}</span>
              </div>
              <div style={{ height: 6, borderRadius: 3, background: '#EAF1EC', marginTop: 6, overflow: 'hidden' }}>
                <div style={{ height: '100%', borderRadius: 3, background: 'var(--color-green)', width: `${Math.round((lc.amount / maxAmount) * 100)}%` }} />
              </div>
            </div>
          ))}
        </div>
      </Card>

      <div style={{ fontSize: 12, color: 'var(--color-text-fainter)', textAlign: 'center', letterSpacing: 1 }}>接口位：/api/ledger/budget</div>
    </ScreenLayout>
  );
}

import { BackHeader } from '../components/BackHeader';
import { Card, ScreenLayout } from '../components/Card';
import { GradientBrainBar, GradientBrainLabels } from '../components/GradientBrainBar';
import { useMemoryCalendar } from '../hooks/useMemoryCalendar';
import { useMemorySummary } from '../hooks/useMemorySummary';
import { buildMemoryCells } from '../lib/memoryCells';
import { MONTH_EN, WEEK_CN_MON_FIRST } from '../lib/format';

export function MemoryScreen() {
  const now = new Date();
  const memCal = useMemoryCalendar(now);
  const memory = useMemorySummary();

  const cells = memCal.cal ? buildMemoryCells(memCal.base, memCal.cal.days, memCal.selected) : [];
  const calLabel = `${MONTH_EN[memCal.base.getMonth()]} ${memCal.base.getFullYear()}`;

  return (
    <ScreenLayout>
      <BackHeader title="记忆库" />

      {/* memory calendar */}
      <Card style={{ padding: 22 }}>
        <div style={{ fontSize: 13, color: 'var(--color-text-faint)', letterSpacing: 1 }}>{memCal.cal?.count ?? 0} memories</div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 12 }}>
          <span
            onClick={memCal.prev}
            style={{
              cursor: 'pointer',
              width: 34,
              height: 34,
              borderRadius: 12,
              background: 'var(--color-bg)',
              boxShadow: '0 3px 8px rgba(183,110,121,0.10)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: 'var(--color-text-soft)',
              fontSize: 14,
            }}
          >
            ◀
          </span>
          <span style={{ fontFamily: "'Bodoni Moda',serif", fontStyle: 'italic', fontSize: 20, color: 'var(--color-rose-deep)', letterSpacing: 1 }}>
            {calLabel}
          </span>
          <span
            onClick={memCal.isCurrent ? undefined : memCal.next}
            style={{
              cursor: memCal.isCurrent ? 'default' : 'pointer',
              width: 34,
              height: 34,
              borderRadius: 12,
              background: 'var(--color-bg)',
              boxShadow: '0 3px 8px rgba(183,110,121,0.10)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: memCal.isCurrent ? '#D9CCC7' : 'var(--color-text-soft)',
              fontSize: 14,
            }}
          >
            ▶
          </span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7,1fr)', gap: 2, marginTop: 16 }}>
          {WEEK_CN_MON_FIRST.map((w) => (
            <span key={w} style={{ textAlign: 'center', fontSize: 13, color: 'var(--color-text-mute)' }}>
              {w}
            </span>
          ))}
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7,1fr)', gap: 2, marginTop: 8 }}>
          {cells.map((c) => (
            <div
              key={c.key}
              onClick={c.day ? () => memCal.setSelectedDay(c.day as number) : undefined}
              style={{ cursor: c.day ? 'pointer' : 'default', height: 38, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
            >
              <span
                style={{
                  width: 32,
                  height: 32,
                  borderRadius: '50%',
                  border: c.ring,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  fontFamily: "'Bodoni Moda',serif",
                  fontSize: 15,
                  fontWeight: c.weight,
                  color: c.color,
                }}
              >
                {c.d}
              </span>
            </div>
          ))}
        </div>
        <div style={{ height: 1, background: '#F0E6E2', margin: '16px 0 4px' }} />
        {memCal.entries.map((e, i) => (
          <div key={i} style={{ padding: '12px 2px', borderBottom: '1px solid #F5EDE9' }}>
            <div style={{ fontFamily: "'Bodoni Moda',serif", fontStyle: 'italic', fontSize: 16, color: 'var(--color-rose-pink)', letterSpacing: 1 }}>
              {e.cat}
            </div>
            <div style={{ fontSize: 15, marginTop: 4 }}>{e.title}</div>
            <div style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 12, color: '#7E93AD', marginTop: 3 }}>{e.date}</div>
          </div>
        ))}
      </Card>

      {memory?.sections.map((sec) => (
        <Card key={sec.key} style={{ padding: 20 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
            <span style={{ fontSize: 15, fontWeight: 600, letterSpacing: 2 }}>{sec.title}</span>
            <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 13, color: 'var(--color-text-faint)' }}>{sec.count} 条</span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 12 }}>
            {sec.items.map((m, i) => (
              <div key={i} style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
                <div
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: '50%',
                    background: m.who === 'fy' ? 'var(--color-violet)' : 'var(--color-amber)',
                    marginTop: 7,
                    flexShrink: 0,
                  }}
                />
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 14, lineHeight: 1.6 }}>{m.text}</div>
                  <div style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 11, color: 'var(--color-text-fainter)', marginTop: 2 }}>{m.date}</div>
                </div>
              </div>
            ))}
          </div>
        </Card>
      ))}

      <Card style={{ padding: 20 }}>
        <div style={{ fontSize: 15, fontWeight: 600, letterSpacing: 2 }}>渐变脑</div>
        <div style={{ fontSize: 13, color: 'var(--color-text-mute)', lineHeight: 1.8, marginTop: 10 }}>
          情感权重当前偏向 amber 侧 —— 最近的记忆里，小猫出现的频率更高。费佳侧的权重集中在「共读」与「深夜长谈」两个簇。
        </div>
        <div style={{ marginTop: 16 }}>
          <GradientBrainBar gradient={memory?.gradient ?? 0} />
        </div>
        <GradientBrainLabels />
      </Card>
    </ScreenLayout>
  );
}

import { useEffect, useState } from 'react';
import { getLastThemePerf, subscribeThemePerf, type ThemePerfRecord } from '../lib/themePerfProbe';

const MONO = 'ui-monospace, Menlo, monospace';

function buildPerfRows(perf: ThemePerfRecord | null): { label: string; value: string }[] {
  if (!perf) return [{ label: 'theme perf', value: 'no switch recorded yet' }];
  return [
    { label: 'theme probe mode', value: perf.mode },
    { label: 'theme apply ms', value: perf.applyMs.toFixed(2) },
    { label: 'theme flush ms', value: perf.flushMs.toFixed(2) },
    { label: 'theme rAF1 ms', value: perf.raf1Ms.toFixed(2) },
    { label: 'theme rAF2 total ms', value: perf.raf2Ms.toFixed(2) },
  ];
}

/** Local perf display — subscribes to probe completion without ChatScreen parent rerender. */
export function ThemePerfRows() {
  const [perf, setPerf] = useState(() => getLastThemePerf());

  useEffect(() => subscribeThemePerf(setPerf), []);

  const rows = buildPerfRows(perf);

  return (
    <div
      className="vstack vstack-6"
      style={{
        background: 'var(--card2)',
        borderRadius: 14,
        padding: '12px 14px',
        fontFamily: MONO,
        fontSize: 11,
        lineHeight: 1.55,
        color: 'var(--ink2)',
      }}
    >
      {rows.map((row) => (
        <div key={row.label} className="hstack hstack-10" style={{ justifyContent: 'space-between' }}>
          <span style={{ color: 'var(--ghost)', flexShrink: 0 }}>{row.label}</span>
          <span style={{ textAlign: 'right', wordBreak: 'break-all' }}>{row.value}</span>
        </div>
      ))}
    </div>
  );
}

import type { ReactNode } from 'react';

/**
 * Standard-route surface container only — no viewport scaling.
 * Scale authority: modern CSS geometry, or legacyNativeCompat body zoom (outside this file).
 */
export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div
      style={{
        maxWidth: 452,
        margin: '0 auto',
        minHeight: '100vh',
        background: 'var(--color-bg)',
        position: 'relative',
      }}
    >
      {children}
    </div>
  );
}

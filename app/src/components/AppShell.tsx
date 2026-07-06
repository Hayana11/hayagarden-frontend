import type { ReactNode } from 'react';

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div
      style={{
        maxWidth: 452,
        margin: '0 auto',
        minHeight: '100vh',
        background: 'var(--color-bg)',
        position: 'relative',
        // Visual downscale so the whole page feels less bulky on mobile.
        zoom: 0.94,
      }}
    >
      {children}
    </div>
  );
}

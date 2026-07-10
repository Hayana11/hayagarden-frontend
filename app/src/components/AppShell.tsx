import { useEffect, useState, type ReactNode } from 'react';

function getShellZoom() {
  if (typeof window === 'undefined') return 0.94;
  // The Android WebView reports 360 CSS px on Hayana's phone. The dashboard
  // design was tuned closer to a 390 px viewport, so compact WebViews need a
  // small visual downscale to avoid crowded cards and calendar cells.
  return window.innerWidth <= 370 ? 0.86 : 0.94;
}

export function AppShell({ children }: { children: ReactNode }) {
  const [zoom, setZoom] = useState(getShellZoom);

  useEffect(() => {
    const onResize = () => setZoom(getShellZoom());
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  return (
    <div
      style={{
        maxWidth: 452,
        margin: '0 auto',
        minHeight: '100vh',
        background: 'var(--color-bg)',
        position: 'relative',
        zoom,
      }}
    >
      {children}
    </div>
  );
}

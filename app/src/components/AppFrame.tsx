import { useEffect, type ReactNode } from 'react';
import { useLocation } from 'react-router-dom';
import { AppShell } from './AppShell';
import { GlobalBottomNav } from './GlobalBottomNav';
import { isFullscreenPath, resolveRouteMeta } from '../navigation';

type AppFrameProps = {
  children: ReactNode;
};

/**
 * Unified React shell: standard vs fullscreen chrome, global nav visibility,
 * and bottom safe spacing — never product routes or viewport scaling.
 */
export function AppFrame({ children }: AppFrameProps) {
  const location = useLocation();
  const meta = resolveRouteMeta(location.pathname);
  const fullscreen = isFullscreenPath(location.pathname);

  useEffect(() => {
    document.documentElement.classList.toggle('dash-fullscreen', fullscreen);
    return () => document.documentElement.classList.remove('dash-fullscreen');
  }, [fullscreen]);

  const showGlobalNav = meta.globalNav;
  const navVariant = meta.globalNavPlacement ?? 'fixed';

  if (fullscreen && showGlobalNav && navVariant === 'embedded') {
    return (
      <div className="app-frame app-frame--fullscreen-embedded">
        <div className="app-frame__page">{children}</div>
        <GlobalBottomNav variant="embedded" />
      </div>
    );
  }

  if (fullscreen) {
    return <>{children}</>;
  }

  return (
    <AppShell>
      <div className={showGlobalNav ? 'app-frame app-frame--standard-nav' : 'app-frame'}>
        {children}
      </div>
      {showGlobalNav && navVariant === 'fixed' ? <GlobalBottomNav variant="fixed" /> : null}
    </AppShell>
  );
}

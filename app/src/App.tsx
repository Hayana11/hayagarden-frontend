import { useEffect } from 'react';
import { BrowserRouter, Routes, Route, useLocation } from 'react-router-dom';
import { AppShell } from './components/AppShell';
import { BottomNav } from './components/BottomNav';
import { DashScreen } from './screens/DashScreen';
import { MemoryScreen } from './screens/MemoryScreen';
import { UsageScreen } from './screens/UsageScreen';
import { ReadingScreen } from './screens/ReadingScreen';
import { LedgerScreen } from './screens/LedgerScreen';
import { PeriodScreen } from './screens/PeriodScreen';
import { ChatScreen } from './screens/ChatScreen';
import { SettingsScreen } from './screens/SettingsScreen';
import { GroupChatScreen } from './screens/GroupChatScreen';
import { MomentsScreen } from './screens/MomentsScreen';
import { ContactsScreen } from './screens/ContactsScreen';
import { CodexChatScreen } from './screens/CodexChatScreen';
import { ProfileScreen } from './screens/ProfileScreen';
import { MonopolyRoomScreen } from './screens/MonopolyRoomScreen';
import { DailySoftWindowPreviewScreen } from './screens/DailySoftWindowPreviewScreen';
import { ManualContextWindowPreviewScreen } from './screens/ManualContextWindowPreviewScreen';
import { MONOPOLY_ROOM_PATH, ROUTES, isFullscreenPath } from './navigation';

function Shell() {
  const location = useLocation();
  const fullscreen = isFullscreenPath(location.pathname);

  useEffect(() => {
    document.documentElement.classList.toggle('dash-fullscreen', fullscreen);
    return () => document.documentElement.classList.remove('dash-fullscreen');
  }, [fullscreen]);

  const routes = (
    <Routes>
      <Route path={ROUTES.dash} element={<DashScreen />} />
      <Route path={ROUTES.memory} element={<MemoryScreen />} />
      <Route path={ROUTES.usage} element={<UsageScreen />} />
      <Route path={ROUTES.reading} element={<ReadingScreen />} />
      <Route path={ROUTES.ledger} element={<LedgerScreen />} />
      <Route path={ROUTES.period} element={<PeriodScreen />} />
      <Route path={ROUTES.chat} element={<ChatScreen />} />
      <Route path={ROUTES.settings} element={<SettingsScreen />} />
      <Route path={ROUTES.groupChat} element={<GroupChatScreen />} />
      <Route path={ROUTES.moments} element={<MomentsScreen />} />
      <Route path={ROUTES.contacts} element={<ContactsScreen />} />
      <Route path={ROUTES.codexChat} element={<CodexChatScreen />} />
      <Route path={ROUTES.profile} element={<ProfileScreen />} />
      <Route path={MONOPOLY_ROOM_PATH} element={<MonopolyRoomScreen />} />
      <Route path={ROUTES.dailySoftWindow} element={<DailySoftWindowPreviewScreen />} />
      <Route path={ROUTES.manualContextWindow} element={<ManualContextWindowPreviewScreen />} />
    </Routes>
  );

  if (fullscreen) return routes;

  return (
    <AppShell>
      {routes}
      <BottomNav />
    </AppShell>
  );
}

export default function App() {
  const basename = window.location.pathname.startsWith('/dash') ? '/dash' : undefined;

  return (
    <BrowserRouter basename={basename}>
      <Shell />
    </BrowserRouter>
  );
}

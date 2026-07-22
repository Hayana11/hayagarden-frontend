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

const FULLSCREEN_PATHS = new Set(['/chat', '/settings', '/group-chat', '/moments', '/contacts', '/codex-chat', '/profile']);

function Shell() {
  const location = useLocation();
  const fullscreen = FULLSCREEN_PATHS.has(location.pathname) || location.pathname.startsWith('/monopoly/');

  useEffect(() => {
    document.documentElement.classList.toggle('dash-fullscreen', fullscreen);
    return () => document.documentElement.classList.remove('dash-fullscreen');
  }, [fullscreen]);

  const routes = (
    <Routes>
      <Route path="/" element={<DashScreen />} />
      <Route path="/memory" element={<MemoryScreen />} />
      <Route path="/usage" element={<UsageScreen />} />
      <Route path="/reading" element={<ReadingScreen />} />
      <Route path="/ledger" element={<LedgerScreen />} />
      <Route path="/period" element={<PeriodScreen />} />
      <Route path="/chat" element={<ChatScreen />} />
      <Route path="/settings" element={<SettingsScreen />} />
      <Route path="/group-chat" element={<GroupChatScreen />} />
      <Route path="/moments" element={<MomentsScreen />} />
      <Route path="/contacts" element={<ContactsScreen />} />
      <Route path="/codex-chat" element={<CodexChatScreen />} />
      <Route path="/profile" element={<ProfileScreen />} />
      <Route path="/monopoly/:roomId" element={<MonopolyRoomScreen />} />
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

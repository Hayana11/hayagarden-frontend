import { useEffect } from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { AppFrame } from './components/AppFrame';
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
import { useLegacyNativeCompat } from './hooks/useLegacyNativeCompat';
import { MONOPOLY_ROOM_PATH, ROUTES } from './navigation';

function AppRoutes() {
  return (
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
}

export default function App() {
  useLegacyNativeCompat();

  useEffect(() => {
    const random = (min: number, max: number) => Math.round(min + Math.random() * (max - min));
    const root = document.documentElement;
    root.style.setProperty('--haze-left-x', `${random(0, 16)}%`);
    root.style.setProperty('--haze-left-y', `${random(0, 14)}%`);
    root.style.setProperty('--haze-right-x', `${random(78, 100)}%`);
    root.style.setProperty('--haze-right-y', `${random(16, 38)}%`);
    root.style.setProperty('--haze-low-x', `${random(28, 70)}%`);
    root.style.setProperty('--haze-low-y', `${random(70, 94)}%`);
    return () => {
      root.style.removeProperty('--haze-left-x');
      root.style.removeProperty('--haze-left-y');
      root.style.removeProperty('--haze-right-x');
      root.style.removeProperty('--haze-right-y');
      root.style.removeProperty('--haze-low-x');
      root.style.removeProperty('--haze-low-y');
    };
  }, []);

  const basename = import.meta.env.BASE_URL.replace(/\/$/, '') || '/';

  return (
    <BrowserRouter basename={basename}>
      <AppFrame>
        <AppRoutes />
      </AppFrame>
    </BrowserRouter>
  );
}

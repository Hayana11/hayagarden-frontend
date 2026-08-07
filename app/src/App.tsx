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
  const basename = window.location.pathname.startsWith('/dash') ? '/dash' : undefined;

  return (
    <BrowserRouter basename={basename}>
      <AppFrame>
        <AppRoutes />
      </AppFrame>
    </BrowserRouter>
  );
}

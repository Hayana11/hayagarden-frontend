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

function Shell() {
  const location = useLocation();
  const fullscreen = location.pathname === '/chat';
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<DashScreen />} />
        <Route path="/memory" element={<MemoryScreen />} />
        <Route path="/usage" element={<UsageScreen />} />
        <Route path="/reading" element={<ReadingScreen />} />
        <Route path="/ledger" element={<LedgerScreen />} />
        <Route path="/period" element={<PeriodScreen />} />
        <Route path="/chat" element={<ChatScreen />} />
        <Route path="/settings" element={<SettingsScreen />} />
      </Routes>
      {!fullscreen && <BottomNav />}
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

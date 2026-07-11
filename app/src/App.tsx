import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { AppShell } from './components/AppShell';
import { BottomNav } from './components/BottomNav';
import { DashScreen } from './screens/DashScreen';
import { MemoryScreen } from './screens/MemoryScreen';
import { UsageScreen } from './screens/UsageScreen';
import { ReadingScreen } from './screens/ReadingScreen';
import { LedgerScreen } from './screens/LedgerScreen';
import { PeriodScreen } from './screens/PeriodScreen';

export default function App() {
  const basename = window.location.pathname.startsWith('/dash') ? '/dash' : undefined;

  return (
    <BrowserRouter basename={basename}>
      <AppShell>
        <Routes>
          <Route path="/" element={<DashScreen />} />
          <Route path="/memory" element={<MemoryScreen />} />
          <Route path="/usage" element={<UsageScreen />} />
          <Route path="/reading" element={<ReadingScreen />} />
          <Route path="/ledger" element={<LedgerScreen />} />
          <Route path="/period" element={<PeriodScreen />} />
        </Routes>
        <BottomNav />
      </AppShell>
    </BrowserRouter>
  );
}

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig(({ command }) => ({
  base: command === 'serve' ? '/preview/' : '/dash/',
  server: {
    host: '127.0.0.1',
    port: 5174,
    strictPort: true,
    allowedHosts: ['love-style.xyz'],
  },
  build: {
    // The Android shell still runs on WebView Chrome 78. Keep the dashboard
    // bundle parseable there; otherwise modern syntax leaves #root blank.
    target: 'chrome78',
  },
  plugins: [react()],
}))

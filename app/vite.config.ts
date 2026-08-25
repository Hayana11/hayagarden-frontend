import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig(({ command }) => ({
  base: command === 'serve' ? '/' : '/dash/',
  build: {
    // The Android shell still runs on WebView Chrome 78. Keep the dashboard
    // bundle parseable there; otherwise modern syntax leaves #root blank.
    target: 'chrome78',
  },
  plugins: [react()],
}))

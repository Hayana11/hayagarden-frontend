import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const rootRedirectPlugin = {
  name: 'root-to-dashboard-redirect',
  configureServer(server: { middlewares: { use: (handler: (req: { url?: string }, res: { statusCode: number; setHeader: (name: string, value: string) => void; end: () => void }, next: () => void) => void) => void } }) {
    server.middlewares.use((req, res, next) => {
      if (req.url === '/') {
        res.statusCode = 302
        res.setHeader('Location', '/dash/')
        res.end()
        return
      }
      next()
    })
  },
}

// https://vite.dev/config/
export default defineConfig({
  base: '/dash/',
  build: {
    // The Android shell still runs on WebView Chrome 78. Keep the dashboard
    // bundle parseable there; otherwise modern syntax leaves #root blank.
    target: 'chrome78',
  },
  plugins: [rootRedirectPlugin, react()],
})

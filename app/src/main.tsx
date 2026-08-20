import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { startRealityRuntime } from './lib/reality/realityRuntime'

declare global {
  interface Window {
    __dashProbe?: (message: string) => void
  }
}

startRealityRuntime()

window.__dashProbe?.('module loaded')

window.__dashProbe?.('render called')

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

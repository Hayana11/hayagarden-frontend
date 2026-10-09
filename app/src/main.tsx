import { StrictMode, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import './index.css'

declare global {
  interface Window {
    __dashProbe?: (message: string) => void
  }
}

function isFlowStudioPreviewPath(): boolean {
  if (import.meta.env.BASE_URL !== '/preview/') return false
  return window.location.pathname === '/preview/flow-studio-soft-glow'
    || window.location.pathname === '/preview/dash/flow-studio-soft-glow'
}

function renderRoot(element: ReactNode) {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>{element}</StrictMode>,
  )
}

async function boot() {
  if (isFlowStudioPreviewPath()) {
    const { FlowStudioSoftGlowScreen } = await import('./screens/FlowStudioSoftGlowScreen')
    renderRoot(
      <BrowserRouter basename="/preview">
        <FlowStudioSoftGlowScreen />
      </BrowserRouter>,
    )
    return
  }

  const [{ default: App }, { startRealityRuntime }, { installNativeTopInset }] = await Promise.all([
    import('./App.tsx'),
    import('./lib/reality/realityRuntime'),
    import('./lib/nativeTopInset'),
  ])
  installNativeTopInset()
  startRealityRuntime()
  window.__dashProbe?.('module loaded')
  window.__dashProbe?.('render called')
  renderRoot(<App />)
}

void boot()

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

type ProbeGeometry = {
  display: string;
  visibility: string;
  opacity: string;
  width: number;
  height: number;
};

function readProbeGeometry(element: Element | null): ProbeGeometry {
  if (!element) {
    return { display: 'na', visibility: 'na', opacity: 'na', width: 0, height: 0 };
  }

  const style = window.getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  return {
    display: style.display,
    visibility: style.visibility,
    opacity: style.opacity,
    width: Math.round(rect.width),
    height: Math.round(rect.height),
  };
}

function reportPostRenderProbe(stage: string) {
  const root = document.getElementById('root');
  const shell = document.querySelector('.app-shell');
  const rootGeometry = readProbeGeometry(root);
  const shellGeometry = readProbeGeometry(shell);

  window.__dashProbe?.([
    stage,
    `path=${window.location.pathname}`,
    `base=${import.meta.env.BASE_URL}`,
    `root=${root ? 1 : 0}`,
    `children=${root?.childElementCount ?? 0}`,
    `html=${root?.innerHTML.length ?? 0}`,
    `shell=${shell ? 1 : 0}`,
    `frame=${document.querySelector('.app-frame') ? 1 : 0}`,
    `screen=${document.querySelector('.screen-stack') ? 1 : 0}`,
    `nav=${document.querySelector('.global-bottom-nav') ? 1 : 0}`,
    `rootDisplay=${rootGeometry.display}`,
    `rootVisibility=${rootGeometry.visibility}`,
    `rootOpacity=${rootGeometry.opacity}`,
    `rootW=${rootGeometry.width}`,
    `rootH=${rootGeometry.height}`,
    `shellDisplay=${shellGeometry.display}`,
    `shellVisibility=${shellGeometry.visibility}`,
    `shellOpacity=${shellGeometry.opacity}`,
    `shellW=${shellGeometry.width}`,
    `shellH=${shellGeometry.height}`,
  ].join(';'));
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

window.requestAnimationFrame(() => reportPostRenderProbe('POST_RENDER_RAF'))
window.setTimeout(() => reportPostRenderProbe('POST_RENDER_100'), 100)
window.setTimeout(() => reportPostRenderProbe('POST_RENDER_1000'), 1000)

/** Capability-based legacy native compat (Capacitor + flex-gap), not device sniffing. */

export interface LegacyNativeCompatDetails {
  isNativeCapacitor: boolean;
  flexGapUnsupported: boolean;
  legacyNativeCompat: boolean;
}

interface CapacitorGlobal {
  isNativePlatform?: () => boolean;
  getPlatform?: () => string;
  platform?: string;
}

let cachedDetails: LegacyNativeCompatDetails | null = null;
let bodyZoomRestore: string | null = null;
let compatApplied = false;

function readCapacitorGlobal(): CapacitorGlobal | undefined {
  return (window as Window & { Capacitor?: CapacitorGlobal }).Capacitor;
}

/** True when running inside a Capacitor native shell (not desktop browser). */
export function isNativeCapacitor(): boolean {
  const cap = readCapacitorGlobal();
  if (!cap) return false;
  if (typeof cap.isNativePlatform === 'function') return cap.isNativePlatform();
  const platform = cap.getPlatform?.() ?? cap.platform;
  return !!platform && platform !== 'web';
}

/**
 * Runtime flex-gap probe: offscreen column, two 1px children, gap 1px.
 * Chrome < 84 (legacy System WebView) reports no gap → children stack at 1px offset.
 */
export function isFlexGapUnsupported(): boolean {
  const host = document.createElement('div');
  host.style.cssText = 'display:flex;flex-direction:column;gap:1px;position:absolute;visibility:hidden;pointer-events:none;top:-9999px;left:-9999px';

  const a = document.createElement('div');
  a.style.cssText = 'width:1px;height:1px;flex-shrink:0';
  const b = document.createElement('div');
  b.style.cssText = 'width:1px;height:1px;flex-shrink:0';

  host.append(a, b);
  document.body.appendChild(host);
  const gapPx = b.offsetTop - a.offsetTop;
  host.remove();

  return gapPx < 2;
}

export function getLegacyNativeCompatDetails(): LegacyNativeCompatDetails {
  if (cachedDetails) return cachedDetails;

  const native = isNativeCapacitor();
  const flexGapUnsupported = isFlexGapUnsupported();
  cachedDetails = {
    isNativeCapacitor: native,
    flexGapUnsupported,
    legacyNativeCompat: native && flexGapUnsupported,
  };
  return cachedDetails;
}

export function isLegacyNativeCompat(): boolean {
  return getLegacyNativeCompatDetails().legacyNativeCompat;
}

/** Apply body zoom 0.8 parity for legacy native WebView; restore prior zoom on cleanup. */
export function applyLegacyNativeCompat(): () => void {
  if (compatApplied || !isLegacyNativeCompat()) return () => {};

  const { body } = document;
  bodyZoomRestore = body.style.zoom;
  body.style.zoom = '0.8';
  body.setAttribute('data-legacy-native-compat', 'true');
  compatApplied = true;

  return () => {
    if (!compatApplied) return;
    if (bodyZoomRestore) body.style.zoom = bodyZoomRestore;
    else body.style.removeProperty('zoom');
    body.removeAttribute('data-legacy-native-compat');
    bodyZoomRestore = null;
    compatApplied = false;
  };
}
